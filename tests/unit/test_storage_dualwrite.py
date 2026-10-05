"""写双份验证：落盘后必须同步一份进 SQLite。

这是「写双份、读优先 SQLite 回退文件」策略的**写侧**守卫。文件是权威副本，
SQLite 只是索引/快照 —— 因此三条铁律：

1. 写成功后 SQLite 必须有对应行（否则读侧切到 SQLite 会看不到新记录）
2. **SQLite 失败绝不能影响文件写**（索引层故障不该让一条分析记录消失）
3. 传进库的必须是**脱敏后**的 data —— 否则 API key 绕过 ``_sanitize`` 进库

回归背景：``ExperienceWriter._write`` 在状态流转时沿用原文件名，故
pending → success 必须是同一 ``entry_id`` 的 UPDATE，不能产生重复行。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa_agent.storage import repositories
from pa_agent.storage.db import reset_hub_for_tests
from pa_agent.storage.experience_repo import (
    count_by_status,
    get_entry,
    list_entries,
    upsert_entry,
)


@pytest.fixture()
def db(tmp_path: Path):
    hub = reset_hub_for_tests(tmp_path / "w.db")
    yield hub
    hub.close_all()


def _rec(symbol="BTCUSDT", timeframe="1h", exchange="GATEIO"):
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-10-05_10-00-00",
            timestamp_local_ms=1000,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
            bar_count=100,
            ai_provider={"model": "x"},
        ),
        kline_data=[{"ts_open": 1, "open": 1, "high": 1, "low": 1, "close": 1}],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis={"a": 1},
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={"b": 2},
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


# ── PendingWriter 双写 ────────────────────────────────────────────────────────


def test_save_full_writes_both_file_and_db(db, tmp_path):
    """回归守卫：写双份。文件在 + DB 有行，缺一不可。"""
    from pa_agent.records.pending_writer import PendingWriter

    pending = tmp_path / "pending"
    w = PendingWriter(pending_dir=pending)
    rec = _rec()

    path = w.save_full(rec)

    assert path.is_file(), "文件必须落盘（权威副本）"
    rows = repositories.list_records()
    assert len(rows) == 1, "SQLite 必须有一行（索引/快照）"
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["file_path"] == str(path)


def test_save_partial_mirrors_partial_status(db, tmp_path):
    """失败记录必须记为 partial —— 否则会被当成增量分析的锚点候选。"""
    from pa_agent.records.pending_writer import PendingWriter

    w = PendingWriter(pending_dir=tmp_path / "pending")
    path = w.save_partial(_rec(), reason="network")
    assert path.is_file()
    rows = repositories.list_records(include_partial=True)
    assert len(rows) == 1
    assert rows[0]["status"] == "partial"


def test_sqlite_failure_does_not_block_file_write(db, tmp_path, monkeypatch):
    """核心铁律：索引层故障绝不能让记录消失。

    把 upsert 打炸，文件仍必须写成功 —— 调用方是分析主流程，异常会中断写入链。
    """
    from pa_agent.records.pending_writer import PendingWriter

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(repositories, "upsert_record", boom)

    w = PendingWriter(pending_dir=tmp_path / "pending")
    path = w.save_full(_rec())          # 不得抛异常
    assert path.is_file(), "DB 挂了也必须落盘"


def test_api_key_never_reaches_db(db, tmp_path):
    """脱敏必须在入库前 —— 否则 API key 会绕过 _sanitize 进数据库。"""
    from pa_agent.records.pending_writer import PendingWriter

    secret = "sk-secret-do-not-store"
    w = PendingWriter(pending_dir=tmp_path / "pending", api_key=secret)
    rec = _rec()
    rec.stage1_messages = [{"role": "user", "content": f"key={secret}"}]
    w.save_full(rec)

    rows = repositories.list_records()
    assert secret not in rows[0]["payload_json"], "API key 泄漏进数据库"


# ── ExperienceWriter 双写 + 状态流转 ─────────────────────────────────────────


def test_experience_upsert_and_fetch(db, tmp_path):
    path = tmp_path / "success_cases" / "2026-10-05_BTCUSDT_1h.json"
    path.parent.mkdir(parents=True)
    content = {"symbol": "BTCUSDT", "timeframe": "1h", "pnl_pct": 3.2, "entry_price": 100.0}
    assert upsert_entry(
        content, cycle_position="trending_tr", status="success",
        symbol="BTCUSDT", timeframe="1h", file_path=path,
    )
    got = get_entry("2026-10-05_BTCUSDT_1h")
    assert got is not None and got["pnl_pct"] == 3.2
    assert count_by_status() == {"success": 1}


def test_experience_status_transition_is_update_not_duplicate(db, tmp_path):
    """回归守卫：pending → success 沿用原文件名，故必须是同一行 UPDATE。

    ExperienceWriter._write 在状态流转时保留原文件名，若这里按 (entry_id,
    status) 建复合主键就会每次流转多出一行，经验库越用越重复。
    """
    path = tmp_path / "pending_cases" / "case1.json"
    path.parent.mkdir(parents=True)
    args = dict(cycle_position="trending_tr", symbol="BTCUSDT",
                timeframe="1h", file_path=path)
    upsert_entry({"symbol": "BTCUSDT", "timeframe": "1h"}, status="pending", **args)
    upsert_entry({"symbol": "BTCUSDT", "timeframe": "1h"}, status="success", **args)

    assert count_by_status() == {"success": 1}, "状态流转后不应残留 pending 行"


def test_experience_unknown_status_falls_back_to_pending(db, tmp_path):
    """未知状态归 pending（最保守），不得凭空造出可检索的成功经验。"""
    path = tmp_path / "success_cases" / "c.json"
    path.parent.mkdir(parents=True)
    upsert_entry({"symbol": "X"}, cycle_position="trending_tr", status="bogus",
                 symbol="X", timeframe="1h", file_path=path)
    assert count_by_status() == {"pending": 1}


def test_experience_filters_and_counts_agree(db, tmp_path):
    """汇总计数必须跟着过滤走，否则前端显示的数字与列表对不上。"""
    for i, (sym, tf) in enumerate([("BTCUSDT", "1h"), ("NVDA", "1h"), ("NVDA", "4h")]):
        p = tmp_path / "success_cases" / f"c{i}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        upsert_entry({"symbol": sym, "timeframe": tf}, cycle_position="trending_tr",
                     status="success", symbol=sym, timeframe=tf, file_path=p)
    assert len(list_entries(symbol="NVDA")) == 2
    assert count_by_status(symbol="NVDA") == {"success": 2}
    assert count_by_status(symbol="BTCUSDT") == {"success": 1}


def test_experience_delete(db, tmp_path):
    from pa_agent.storage.experience_repo import delete_entry

    p = tmp_path / "success_cases" / "d.json"
    p.parent.mkdir(parents=True)
    upsert_entry({"symbol": "X"}, cycle_position="trending_tr", status="success",
                 symbol="X", timeframe="1h", file_path=p)
    assert delete_entry("d") is True
    assert get_entry("d") is None


# ── 导入边界：合成数据绝不入库 ────────────────────────────────────────────────


def test_import_skips_dot_prefixed_dirs(db, tmp_path):
    """AGENTS.md 硬要求：.seed_demo_*/ 是合成数据，.omc/ 是工具状态，都不得进库。"""
    from pa_agent.storage.importer import import_experience_entries

    root = tmp_path / "experience"
    real = root / "trending_tr" / "success_cases"
    real.mkdir(parents=True)
    (real / "real.json").write_text(
        json.dumps({"symbol": "BTCUSDT", "timeframe": "1h"}), encoding="utf-8"
    )

    seed = root / ".seed_demo_20260817" / "trending_tr" / "success_cases"
    seed.mkdir(parents=True)
    (seed / "fake.json").write_text(
        json.dumps({"symbol": "FAKE", "timeframe": "1d"}), encoding="utf-8"
    )

    omc = root / ".omc" / "state"
    omc.mkdir(parents=True)
    (omc / "tool.json").write_text(json.dumps({"x": 1}), encoding="utf-8")

    stats = import_experience_entries(root)
    assert stats["imported"] == 1, "只应导入真实条目"
    assert count_by_status() == {"success": 1}
    assert get_entry("fake") is None, "合成数据不得入库"
    assert get_entry("tool") is None, "工具状态不得入库"


# ── admin 用户 ────────────────────────────────────────────────────────────────


def test_admin_user_seeded(db):
    from pa_agent.storage.users import (
        ADMIN_USER_ID, default_user_id, ensure_admin_user, get_user, list_users,
    )

    assert ensure_admin_user() == ADMIN_USER_ID
    u = get_user(ADMIN_USER_ID)
    assert u is not None and u["role"] == "admin" and u["is_default"] == 1
    assert default_user_id() == ADMIN_USER_ID, "UI 暂不做登录，默认恒为 admin"
    assert [x["user_id"] for x in list_users()] == [ADMIN_USER_ID]


def test_ensure_admin_is_idempotent(db):
    from pa_agent.storage.users import ensure_admin_user, list_users

    for _ in range(3):
        ensure_admin_user()
    assert len(list_users()) == 1, "重复播种不得产生重复用户"


def test_admin_seeding_tolerates_db_disabled(tmp_path):
    """DB 降级时不得抛异常 —— 那是设计里的降级路径，配置回落 settings.json。"""
    from pa_agent.storage.db import reset_hub_for_tests
    from pa_agent.storage.users import ADMIN_USER_ID, ensure_admin_user

    hub = reset_hub_for_tests(tmp_path / "x.db")
    hub._disable("simulated")
    assert ensure_admin_user() == ADMIN_USER_ID