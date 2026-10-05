"""配置级联单测：系统兜底 → 用户覆盖。

守护需求原文：「用户默认使用系统配置；不能改，改了就保存到自己的配置区覆盖默认」。

三条铁律：

1. **用户层必须稀疏** —— 只存差异。全量复制会让系统兜底后续更新再也传不到用户
2. **系统兜底不被用户改动污染** —— 它是所有用户的只读默认值
3. **环境变量仍最高** —— 级联不改变 ``apply_env_overrides`` 的既有语义
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pa_agent.storage.db import reset_hub_for_tests
from pa_agent.storage.settings_store import (
    apply_user_change,
    clear_overrides,
    compute_diff,
    deep_merge,
    load_baseline,
    load_overrides,
    reset_to_system_defaults,
    resolve,
    save_baseline,
    save_overrides,
    seed_from_file,
)


@pytest.fixture()
def db(tmp_path: Path):
    hub = reset_hub_for_tests(tmp_path / "s.db")
    yield hub
    hub.close_all()


BASE = {
    # 键序：api_key 放最后是刻意的 —— pre-commit 的密钥扫描正则会从该键的
    # 值一路贪婪匹配到下一个引号，因此空值夹具若把后续 token 排在其后就会被
    # 误判为「含密钥」而阻断提交。本注释刻意不复现那个字面量。
    "provider": {"model": "m1", "context_window": 64000, "api_key": ""},
    "prompt": {"experience_max_entries": 3},
    "general": {"last_symbol": "BTCUSDT", "analysis_bar_count": 100},
}


# ── 深合并 / 差异 ────────────────────────────────────────────────────────────


def test_deep_merge_keeps_untouched_keys():
    got = deep_merge(BASE, {"prompt": {"experience_max_entries": 10}})
    assert got["prompt"]["experience_max_entries"] == 10
    assert got["provider"]["model"] == "m1", "未提及的键必须保留"


def test_deep_merge_skips_none():
    """前端可能显式传 null，那不代表要清空值。"""
    got = deep_merge(BASE, {"prompt": {"experience_max_entries": None}})
    assert got["prompt"]["experience_max_entries"] == 3


def test_deep_merge_replaces_lists_wholesale():
    got = deep_merge({"a": [1, 2, 3]}, {"a": [9]})
    assert got["a"] == [9], "列表语义是整份设定，不应逐项合并"


def test_compute_diff_is_sparse():
    diff = compute_diff(BASE, {
        "provider": {"model": "m2", "context_window": 64000, "api_key": ""},
        "prompt": {"experience_max_entries": 3},
        "general": {"last_symbol": "NVDA", "analysis_bar_count": 100},
    })
    assert diff == {"provider": {"model": "m2"}, "general": {"last_symbol": "NVDA"}}, (
        "只应记录真正变化的叶子"
    )


def test_compute_diff_empty_when_identical():
    assert compute_diff(BASE, BASE) == {}


# ── 播种：既有 settings.json 升格为系统兜底 ──────────────────────────────────


def test_seed_from_file_creates_baseline(db):
    assert seed_from_file(BASE) is True
    assert load_baseline()["provider"]["model"] == "m1"


def test_seed_does_not_overwrite_existing_baseline(db):
    """系统兜底已存在时不得用旧文件覆盖 —— 否则用户改动会被抹掉。"""
    seed_from_file(BASE)
    save_baseline({**BASE, "provider": {"model": "system-new"}})
    assert seed_from_file(BASE) is False
    assert load_baseline()["provider"]["model"] == "system-new"


# ── 核心语义：用户默认继承，改了才覆盖 ──────────────────────────────────────


def test_user_inherits_baseline_without_any_change(db):
    seed_from_file(BASE)
    uid = "admin"
    assert load_overrides(uid) == {}, "用户尚未改过，不应有任何覆盖"
    assert resolve(uid)["provider"]["model"] == "m1"


def test_user_change_saves_sparse_patch(db):
    seed_from_file(BASE)
    apply_user_change(
        {**BASE, "provider": {**BASE["provider"], "model": "user-model"}}, "admin"
    )
    ov = load_overrides("admin")
    assert ov == {"provider": {"model": "user-model"}}, (
        "只应存差异 —— 全量复制会让系统兜底后续更新传不到用户"
    )
    assert resolve("admin")["provider"]["model"] == "user-model"


def test_user_change_does_not_mutate_baseline(db):
    """铁律：用户改动不得污染系统兜底 —— 那是所有用户的只读默认值。"""
    seed_from_file(BASE)
    apply_user_change({**BASE, "provider": {**BASE["provider"], "model": "mine"}}, "admin")
    assert load_baseline()["provider"]["model"] == "m1", "系统兜底被改了！"


def test_baseline_update_propagates_to_untouched_users_only(db):
    """系统兜底更新后，没改过该字段的用户应拿到新值。"""
    seed_from_file(BASE)
    apply_user_change({**BASE, "provider": {**BASE["provider"], "model": "mine"}}, "admin")

    save_baseline({**BASE, "provider": {**BASE["provider"], "model": "system-v2"}})

    assert resolve("admin")["provider"]["model"] == "mine", "用户改过的字段应保持"
    assert resolve("other")["provider"]["model"] == "system-v2", "未改用户应跟随兜底更新"


def test_reset_to_system_defaults(db):
    seed_from_file(BASE)
    apply_user_change({**BASE, "provider": {**BASE["provider"], "model": "mine"}}, "admin")
    assert reset_to_system_defaults("admin") is True
    assert load_overrides("admin") == {}
    assert resolve("admin")["provider"]["model"] == "m1"


def test_users_are_isolated(db):
    seed_from_file(BASE)
    apply_user_change({**BASE, "provider": {**BASE["provider"], "model": "A"}}, "user-a")
    apply_user_change({**BASE, "provider": {**BASE["provider"], "model": "B"}}, "user-b")
    assert resolve("user-a")["provider"]["model"] == "A"
    assert resolve("user-b")["provider"]["model"] == "B"


# ── 兜底与降级 ──────────────────────────────────────────────────────────────


def test_resolve_returns_none_when_nothing_configured(db):
    assert resolve("admin", None) is None


def test_resolve_seeds_from_file_on_first_run(db):
    """首次运行：文件升格为系统兜底，用户直接继承。"""
    got = resolve("admin", BASE)
    assert got is not None and got["provider"]["model"] == "m1"
    assert load_baseline() is not None, "文件应已升格为兜底"


def test_file_fallback_ignored_once_baseline_exists(db):
    seed_from_file(BASE)
    save_overrides({"prompt": {"experience_max_entries": 9}}, "admin")
    got = resolve("admin", {"provider": {"model": "STALE-FILE"}})
    assert got["provider"]["model"] == "m1", "DB 有兜底后不应再采纳文件内容"


def test_disabled_db_degrades_to_file_fallback(db, monkeypatch):
    """DB 降级不得阻断启动 —— 仍能靠文件拿到配置。"""
    seed_from_file(BASE)
    db._disable("simulated")
    assert resolve("admin", BASE)["provider"]["model"] == "m1"


def test_save_overrides_replaces_wholesale(db):
    """PATCH 语义：body 就是我想要的覆盖层，合并会让已删字段复活。"""
    seed_from_file(BASE)
    save_overrides({"prompt": {"experience_max_entries": 5}, "general": {"x": 1}}, "admin")
    save_overrides({"prompt": {"experience_max_entries": 7}}, "admin")
    ov = load_overrides("admin")
    assert "general" not in ov, "整体替换，不该残留上一次覆盖"
    assert ov["prompt"]["experience_max_entries"] == 7


def test_clear_overrides(db):
    seed_from_file(BASE)
    save_overrides({"prompt": {"experience_max_entries": 5}}, "admin")
    assert clear_overrides("admin") is True
    assert load_overrides("admin") == {}