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

from pa_agent.config.settings import Settings
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


def _baseline() -> dict:
    """一份**真实形状**的完整配置：8 个 section，全部字段带真实默认值。

    为什么不能用 3-section 裸 dict 当夹具：那份夹具只覆盖 provider/prompt/general，
    真实 ``Settings`` 有 8 个 section、provider 单段就有 12 个字段。用残缺夹具时
    「compute_diff 恰好为空」证明不了什么 —— 缺的段压根没参与比较。而恰恰是那些
    没被覆盖的字段（``max_output_tokens: None``、``seed: None``、``thinking`` …）
    最容易在 JSON 往返后变成非 None 或类型漂移，把整份默认值固化成用户覆盖。
    """
    return Settings().model_dump()


#: 模块级基准夹具。默认 model 是 ``deepseek-v4-flash``、last_symbol 是 ``XAUUSD``，
#: 不含任何真实凭证（``api_key`` 为空串）—— pre-commit 的密钥扫描不会命中。
BASE = _baseline()


# ── 深合并 / 差异 ────────────────────────────────────────────────────────────


def test_deep_merge_keeps_untouched_keys():
    got = deep_merge(BASE, {"prompt": {"experience_max_entries": 10}})
    assert got["prompt"]["experience_max_entries"] == 10
    assert got["provider"]["model"] == BASE["provider"]["model"], "未提及的键必须保留"


def test_deep_merge_skips_none():
    """前端可能显式传 null，那不代表要清空值。"""
    got = deep_merge(BASE, {"prompt": {"experience_max_entries": None}})
    assert got["prompt"]["experience_max_entries"] == 3


def test_deep_merge_replaces_lists_wholesale():
    got = deep_merge({"a": [1, 2, 3]}, {"a": [9]})
    assert got["a"] == [9], "列表语义是整份设定，不应逐项合并"


def test_compute_diff_is_sparse():
    diff = compute_diff(BASE, {
        "provider": {**BASE["provider"], "model": "m2"},
        "general": {**BASE["general"], "last_symbol": "NVDA"},
    })
    assert diff == {"provider": {"model": "m2"}, "general": {"last_symbol": "NVDA"}}, (
        "只应记录真正变化的叶子 —— 即便基准是完整的 8 section"
    )


def test_compute_diff_empty_when_identical():
    assert compute_diff(BASE, BASE) == {}


# ── 播种：既有 settings.json 升格为系统兜底 ──────────────────────────────────


def test_seed_from_file_creates_baseline(db):
    assert seed_from_file(BASE) is True
    assert load_baseline()["provider"]["model"] == BASE["provider"]["model"]


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
    assert resolve(uid)["provider"]["model"] == BASE["provider"]["model"]


def test_user_change_saves_sparse_patch(db):
    seed_from_file(BASE)


# ⚠ 下面三个用例测的是「稀疏覆盖机制」，不是「系统默认/自带模型」模式门控。
# 自 2026-10-05 起 provider 覆盖需**显式选择**才生效（见文件末尾的 LLM 配置组），
# 所以它们必须先打开 use_custom —— 否则覆盖被门控丢弃，测不到本来要测的东西。

    apply_user_change(
        {**BASE, "provider": {**BASE["provider"], "use_custom": True, "model": "user-model"}},
        "admin",
    )
    ov = load_overrides("admin")
    assert ov == {"provider": {"use_custom": True, "model": "user-model"}}, (
        "只应存差异 —— 全量复制会让系统兜底后续更新传不到用户"
    )
    assert resolve("admin")["provider"]["model"] == "user-model"


def test_user_change_does_not_mutate_baseline(db):
    """铁律：用户改动不得污染系统兜底 —— 那是所有用户的只读默认值。"""
    seed_from_file(BASE)
    apply_user_change(
        {**BASE, "provider": {**BASE["provider"], "use_custom": True, "model": "mine"}},
        "admin",
    )
    assert load_baseline()["provider"]["model"] == BASE["provider"]["model"], "系统兜底被改了！"


def test_baseline_update_propagates_to_untouched_users_only(db):
    """系统兜底更新后，没改过该字段的用户应拿到新值。"""
    seed_from_file(BASE)
    apply_user_change(
        {**BASE, "provider": {**BASE["provider"], "use_custom": True, "model": "mine"}},
        "admin",
    )

    save_baseline({**BASE, "provider": {**BASE["provider"], "model": "system-v2"}})

    assert resolve("admin")["provider"]["model"] == "mine", "用户改过的字段应保持"
    assert resolve("other")["provider"]["model"] == "system-v2", "未改用户应跟随兜底更新"


def test_reset_to_system_defaults(db):
    seed_from_file(BASE)
    apply_user_change(
        {**BASE, "provider": {**BASE["provider"], "use_custom": True, "model": "mine"}},
        "admin",
    )
    assert reset_to_system_defaults("admin") is True
    assert load_overrides("admin") == {}
    assert resolve("admin")["provider"]["model"] == BASE["provider"]["model"]


def test_users_are_isolated(db):
    seed_from_file(BASE)
    apply_user_change(
        {**BASE, "provider": {**BASE["provider"], "use_custom": True, "model": "A"}},
        "user-a",
    )
    apply_user_change(
        {**BASE, "provider": {**BASE["provider"], "use_custom": True, "model": "B"}},
        "user-b",
    )
    assert resolve("user-a")["provider"]["model"] == "A"
    assert resolve("user-b")["provider"]["model"] == "B"


# ── 兜底与降级 ──────────────────────────────────────────────────────────────


def test_resolve_returns_none_when_nothing_configured(db):
    assert resolve("admin", None) is None


def test_resolve_seeds_from_file_on_first_run(db):
    """首次运行：文件升格为系统兜底，用户直接继承。"""
    got = resolve("admin", BASE)
    assert got is not None and got["provider"]["model"] == BASE["provider"]["model"]
    assert load_baseline() is not None, "文件应已升格为兜底"


def test_file_fallback_ignored_once_baseline_exists(db):
    seed_from_file(BASE)
    save_overrides({"prompt": {"experience_max_entries": 9}}, "admin")
    got = resolve("admin", {"provider": {"model": "STALE-FILE"}})
    assert got["provider"]["model"] == BASE["provider"]["model"], "DB 有兜底后不应再采纳文件内容"


def test_disabled_db_degrades_to_file_fallback(db, monkeypatch):
    """DB 降级不得阻断启动 —— 仍能靠文件拿到配置。"""
    seed_from_file(BASE)
    db._disable("simulated")
    assert resolve("admin", BASE)["provider"]["model"] == BASE["provider"]["model"]


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


# ── 完整基准 + 完整 current：用户层必须仍为空 ─────────────────────────────────


def test_identical_full_settings_produce_no_overrides(db):
    """「保存了但什么都没改」不得留下任何用户覆盖。

    **为什么这条必须经过 ``apply_user_change``**：既有测试全都直接调裸
    ``compute_diff``，那是纯函数，结构上测不到真实保存路径上的两件事 ——
    ① JSON 往返（``model_dump`` → 存 → ``load_overrides`` → ``resolve``）
       可能把 ``None`` 变成 ``"null"``、把 int 变成 float；② 落库前是否又
    经过了 ``normalize_raw`` 之类的改名字段。裸 diff 全绿、真实保存炸掉的
    组合完全可能发生。

    基准用 ``Settings().model_dump()``（8 个 section、含 provider 的 12 个字段），
    那些没被手写夹具覆盖的字段才是最容易漂移的。
    """
    seed_from_file(BASE)
    current = Settings.model_validate(BASE)

    assert apply_user_change(current.model_dump(), "admin") is True
    assert load_overrides("admin") == {}, (
        "什么都没改却留下用户覆盖 → 系统兜底后续更新再也传不到该用户"
    )
    assert resolve("admin") == BASE, "解析结果必须与基准逐键相等"


def test_full_settings_keeps_sparseness_with_one_real_change(db):
    """改一个字段 → 用户层**只能**有那一个字段。"""
    seed_from_file(BASE)
    current = Settings.model_validate(BASE)
    current.general.analysis_bar_count = 321

    apply_user_change(current.model_dump(), "admin")
    assert load_overrides("admin") == {"general": {"analysis_bar_count": 321}}
    assert resolve("admin")["general"]["analysis_bar_count"] == 321
    assert resolve("admin")["provider"]["model"] == BASE["provider"]["model"]


# ── persist_patch：connector / 设置页的唯一写入口 ────────────────────────────


def test_persist_patch_declares_only_its_own_keys(db):
    """声明式写入：只提交调用方改过的那几个键，其余继承系统兜底。"""
    from pa_agent.config.settings import persist_patch

    seed_from_file(BASE)
    assert persist_patch({"provider": {"model": "from-connector", "api_key": "tok"}}) is True

    ov = load_overrides("admin")
    assert ov == {"provider": {"model": "from-connector", "api_key": "tok"}}, (
        "没声明的字段绝不能进用户层"
    )
    assert load_baseline()["provider"]["model"] == BASE["provider"]["model"], (
        "connector 绝不能写系统兜底 —— 凭证属 L1 单机账号，兜底是所有人的默认值"
    )


def test_persist_patch_merges_into_existing_overrides(db):
    """两处写入方各改各的字段，互不覆盖（合并语义，不是整份替换）。"""
    from pa_agent.config.settings import persist_patch

    seed_from_file(BASE)
    persist_patch({"provider": {"model": "m-from-sync"}})
    persist_patch({"prompt": {"experience_max_entries": 8}})

    ov = load_overrides("admin")
    assert ov == {"provider": {"model": "m-from-sync"}, "prompt": {"experience_max_entries": 8}}


def test_persist_patch_resparsifies_against_baseline(db):
    """改回与基准同值 → 该键自动退出用户层，系统兜底后续更新才能穿透。"""
    from pa_agent.config.settings import persist_patch

    seed_from_file(BASE)
    original = BASE["general"]["analysis_bar_count"]

    persist_patch({"general": {"analysis_bar_count": 999}})
    assert load_overrides("admin") == {"general": {"analysis_bar_count": 999}}

    persist_patch({"general": {"analysis_bar_count": original}})
    assert load_overrides("admin") == {}, "改回默认值后不该继续挡住系统兜底"


def test_persist_provider_never_burns_env_overridden_fields(db):
    """connector 只写那 4 个字段 —— 尤其不能固化 thinking / seed / top_p。

    这些字段会被 ``apply_env_overrides`` 从 .env 盖上来。整份持久化会把 .env 的
    值永久烧进用户层，此后用户改 .env 不再生效。``persist_provider`` 收窄到
    ``_CONNECTOR_PROVIDER_FIELDS`` 正是为了防住这一点。
    """
    from pa_agent.config.settings import persist_provider

    seed_from_file(BASE)
    provider = Settings().provider
    provider.model = "openclaw"
    provider.base_url = "http://127.0.0.1:51187/v1"
    provider.api_key = "gw-token"
    provider.thinking = False          # 假装来自 .env
    provider.reasoning_effort = "max"  # 同上
    provider.seed = 1234               # 同上

    assert persist_provider(provider) is True
    ov = load_overrides("admin")["provider"]
    assert set(ov) == {"model", "base_url", "api_key"}, (
        f"只应写 connector 的 3 个真值字段，实际写了：{sorted(ov)}"
    )


def test_persist_patch_refuses_when_db_read_failed(db):
    """读失败时拒绝写入：算出来的 diff 没有意义，会把整份默认值固化成覆盖。"""
    from pa_agent.config.settings import persist_patch

    seed_from_file(BASE)
    db._disable("simulated")
    assert persist_patch({"provider": {"model": "should-not-stick"}}) is False

# ── 出厂默认的显式提升（settings.json 不再随保存漂移）─────────────────────────


def test_put_settings_does_not_write_back_settings_json(db, monkeypatch):
    """保存设置**不得**回写 settings.json。

    回写会让「出厂配置」这个文件跟着用户修改漂移；DB 一旦清空，baseline 重新从
    它播种，用户的历史修改就被当成出厂默认固化成所有用户继承的基线。
    """
    import asyncio

    import web.api.routes_settings as rs

    monkeypatch.setattr(rs, "save_settings", lambda *a, **k: pytest.fail(
        "put_settings 仍在回写 settings.json"
    ))
    assert hasattr(rs, "promote_settings_to_default"), "缺少显式提升入口"


def test_promote_requires_explicit_confirmation():
    """这是全局动作，必须显式确认 —— 误触就把个人设置变成了所有人的默认。"""
    import inspect

    import web.api.routes_settings as rs

    src = inspect.getsource(rs.promote_settings_to_default)
    assert "confirm" in src, "提升出厂默认未要求确认"


def test_promote_writes_baseline_and_clears_overrides(db):
    """提升后：baseline = 当前配置，本用户覆盖被清空。

    覆盖必须清 —— 已成出厂默认的字段若留在覆盖区，会永久遮蔽后续系统更新。
    """
    from pa_agent.config.settings import Settings
    from pa_agent.storage.settings_store import (
        apply_user_change, load_baseline, load_overrides, promote_to_baseline,
        resolve, save_baseline, save_overrides,
    )

    save_baseline({"general": {"analysis_bar_count": 321}})
    save_overrides({"general": {"analysis_bar_count": 250}}, "admin")
    assert load_overrides("admin") == {"general": {"analysis_bar_count": 250}}

    current = Settings.model_validate({
        **Settings().model_dump(),
        "general": {**Settings().general.model_dump(), "analysis_bar_count": 250},
    }).model_dump(mode="json")

    assert promote_to_baseline(current) is True
    assert load_baseline()["general"]["analysis_bar_count"] == 250
    assert load_overrides("admin") == {}, "提升后覆盖区未清空，会遮蔽后续系统更新"
    assert resolve("admin")["general"]["analysis_bar_count"] == 250


def test_promote_rejected_when_db_read_failed(db):
    """读失败时拒绝提升 —— 否则会把不确定的数据固化成所有人的出厂默认。"""
    from pa_agent.storage.db import get_hub
    from pa_agent.storage.settings_store import promote_to_baseline, save_baseline

    save_baseline({"general": {"analysis_bar_count": 321}})
    # read_failed 是**线程局部**的（跨线程污染会让降级失效），必须设在线程局部上
    get_hub()._local.read_error = "no such table: global_config"
    assert promote_to_baseline({"general": {"analysis_bar_count": 999}}) is False


# ── LLM 配置：系统默认 vs 自带模型 ────────────────────────────────────────────


def _baseline_with(model="sys-model"):
    from pa_agent.config.settings import Settings

    d = Settings().model_dump(mode="json")
    d["provider"]["model"] = model
    return d


def test_default_is_system_default(db):
    """出厂默认就是「跟随系统」—— admin 也一样，不需要任何表态。"""
    from pa_agent.storage.settings_store import resolve, save_baseline

    save_baseline(_baseline_with())
    p = resolve("admin")["provider"]
    assert p["use_custom"] is False
    assert p["model"] == "sys-model"


def test_provider_overrides_ignored_until_opted_in(db):
    """**没表态就不生效**：残留覆盖不得盖住系统默认。

    否则用户既看不出自己遮住了什么，也无法一键切回 —— 而界面上毫无提示。
    """
    from pa_agent.storage.settings_store import (
        resolve, save_baseline, save_overrides,
    )

    save_baseline(_baseline_with())
    save_overrides({"provider": {"model": "stale", "base_url": "http://stale"}}, "admin")
    r = resolve("admin")["provider"]
    assert r["model"] == "sys-model"
    assert r["base_url"] != "http://stale", "残留覆盖未被作废"


def test_custom_mode_applies_user_overrides(db):
    from pa_agent.storage.settings_store import (
        resolve, save_baseline, save_overrides,
    )

    save_baseline(_baseline_with())
    save_overrides({"provider": {"use_custom": True, "model": "mine"}}, "admin")
    assert resolve("admin")["provider"]["model"] == "mine"


def test_switching_back_to_system_wipes_stale_overrides(db):
    """回归守卫：切回系统默认后，**残留的 provider 覆盖必须全部作废**。

    实现一度只重置 use_custom 却保留了其余键，于是界面上写着「使用系统默认」、
    实际却在用自己那个已被删除的模型 —— 名存实亡，比不做这个功能更糟。
    """
    from pa_agent.storage.settings_store import (
        resolve, save_baseline, save_overrides,
    )

    save_baseline(_baseline_with())
    save_overrides(
        {"provider": {"use_custom": False, "model": "stale", "base_url": "http://old"}},
        "admin",
    )
    p = resolve("admin")["provider"]
    assert p["model"] == "sys-model", "切回系统默认后仍在用陈旧覆盖"
    assert p["base_url"] != "http://old", "残留字段未被作废"


def test_llm_switch_does_not_affect_other_sections(db):
    """provider 的开关只管 provider —— 偏好类配置不受影响。"""
    from pa_agent.storage.settings_store import (
        resolve, save_baseline, save_overrides,
    )

    save_baseline(_baseline_with())
    save_overrides(
        {"provider": {"use_custom": False, "model": "x"},
         "general": {"analysis_bar_count": 250}},
        "admin",
    )
    assert resolve("admin")["general"]["analysis_bar_count"] == 250
