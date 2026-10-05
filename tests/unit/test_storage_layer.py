"""存储层单测：SQLite 连接、会话注册表、导入器幂等、DB↔文件等价性。

守护的设计铁律（见 docs/SESSION_STORAGE_DESIGN.md）：

- 三个 tab 的游标必须互不干扰
- 过期必须真的清掉（否则 SSE 长连接内存泄漏无从排查）
- 导入必须幂等（重复跑不能产生重复行）
- DB 路径必须与文件路径逐例等价（阶段 C 切读的合法性依据）
- DB 故障必须降级而不是冒泡（K 线是核心功能，不能被索引层拖垮）
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pa_agent.storage import ephemeral, importer, repositories, sessions
from pa_agent.storage.db import get_hub, initialize_storage, reset_hub_for_tests
from pa_agent.storage.schema import SCHEMA_VERSION, all_statements, tables


@pytest.fixture()
def db(tmp_path: Path):
    """每个测试一个独立 DB 文件。"""
    hub = reset_hub_for_tests(tmp_path / "test.db")
    yield hub
    hub.close_all()


# ── schema / 连接管理 ─────────────────────────────────────────────────────────


def test_schema_creates_every_table(db):
    names = {r["name"] for r in get_hub().query(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert not (set(tables()) - names), "DDL 漏建表"
    assert get_hub().schema_version() == SCHEMA_VERSION


def test_all_statements_are_single_statements():
    """回归守卫：sqlite3.execute 只接受单语句，曾因多语句 DDL 静默降级。"""
    for stmt in all_statements():
        # 去掉结尾分号与空白后不得含分号
        core = stmt.strip().rstrip(";").strip()
        assert ";" not in core, f"DDL 语句含多条：{core[:60]}"


def test_migrate_is_idempotent(db):
    """重复迁移不得报错，也不得重建表。"""
    assert db.migrate() is True
    assert db.migrate() is True
    assert db.schema_version() == SCHEMA_VERSION


def test_execute_failure_does_not_raise(db):
    """SQL 出错只返回 False —— 绝不冒泡进业务流。"""
    assert db.execute("INSERT INTO no_such_table VALUES (1)") is False


def test_query_failure_returns_empty(db):
    assert db.query("SELECT * FROM no_such_table") == []


def test_run_in_tx_rolls_back(db):
    """事务失败必须整体回滚，不能留半套数据。"""
    hub = get_hub()

    def half_write(conn):
        conn.execute(
            "INSERT INTO global_config (key, value_json, updated_at) VALUES ('k','1',0)"
        )
        raise sqlite3.Error("boom")

    assert hub.run_in_tx(half_write) is None
    assert hub.query("SELECT * FROM global_config") == []


# ── 会话注册表（内存热层）─────────────────────────────────────────────────────


def test_sessions_are_isolated_from_each_other():
    """核心铁律：A tab 的游标改动不得影响 B tab。"""
    reg = ephemeral.reset_registry_for_tests()
    reg.get_or_create("tab-1").cursor.symbol = "BTCUSDT"
    reg.get_or_create("tab-2").cursor.symbol = "NVDA"
    assert reg.get("tab-1").cursor.symbol == "BTCUSDT"
    assert reg.get("tab-2").cursor.symbol == "NVDA"


def test_registry_ttl_expires_idle_session():
    reg = ephemeral.SessionRegistry(default_ttl_s=0.01)
    reg.get_or_create("gone")
    import time

    time.sleep(0.02)
    assert reg.get("gone") is None


def test_registry_lru_caps_memory():
    """无上限时反复开关标签页可吃光内存（AGENTS.md 遗留需求 3）。"""
    reg = ephemeral.SessionRegistry(max_sessions=3)
    for i in range(10):
        reg.get_or_create(f"tab-{i}")
    assert len(reg.all_sessions()) == 3


def test_registry_lru_evicts_least_recently_used():
    reg = ephemeral.SessionRegistry(max_sessions=3)
    reg.get_or_create("old")
    reg.get_or_create("mid")
    reg.get_or_create("new")
    reg.get_or_create("mid")   # 刷新 mid 的最近使用
    reg.get_or_create("newer") # 触发淘汰
    ids = {s.session_id for s in reg.all_sessions()}
    assert "old" not in ids, "最久未用的应被淘汰"
    assert "mid" in ids


def test_scratch_key_expires():
    reg = ephemeral.reset_registry_for_tests()
    s = reg.get_or_create("t")
    s.set("k", "v", ttl_s=0.01)
    import time

    time.sleep(0.02)
    assert s.get("k", "GONE") == "GONE"


def test_scratch_key_without_ttl_survives():
    reg = ephemeral.reset_registry_for_tests()
    s = reg.get_or_create("t")
    s.set("k", "v")
    assert s.get("k") == "v"


def test_drop_releases_immediately():
    """SSE 断开时立刻释放，不等 TTL。"""
    reg = ephemeral.reset_registry_for_tests()
    reg.get_or_create("t")
    reg.drop("t")
    assert reg.get("t") is None


def test_get_or_create_rejects_empty_id():
    reg = ephemeral.reset_registry_for_tests()
    with pytest.raises(ValueError):
        reg.get_or_create("")


# ── sessions 快照（L3）────────────────────────────────────────────────────────


def test_session_cursor_is_per_tab(db):
    sessions.ensure_session("tab-1")
    sessions.ensure_session("tab-2")
    sessions.set_cursor("tab-1", symbol="BTCUSDT", timeframe="1h")
    assert sessions.get_session("tab-1")["symbol"] == "BTCUSDT"
    assert sessions.get_session("tab-2")["symbol"] == "", "B tab 不该被 A 的游标污染"


def test_ensure_session_does_not_clobber_cursor(db):
    """每次请求都续期是正常行为，不该把用户设的品种刷掉。"""
    sessions.ensure_session("tab-1")
    sessions.set_cursor("tab-1", symbol="NVDA", timeframe="1d")
    sessions.ensure_session("tab-1")
    assert sessions.get_session("tab-1")["symbol"] == "NVDA"


def test_set_view_state_updates_only_given_fields(db):
    sessions.ensure_session("t")
    sessions.set_cursor("t", symbol="ETHUSDT", timeframe="15m")
    sessions.set_view_state("t", keep_analysis=True)
    row = sessions.get_session("t")
    assert row["keep_analysis"] == 1
    assert row["symbol"] == "ETHUSDT", "未传的字段不应被改动"
    assert row["wait_close"] == 0


def test_expired_session_reads_as_absent(db):
    sessions.ensure_session("t", ttl_s=-1)  # 已过期
    assert sessions.get_session("t") is None


def test_purge_expired_removes_only_dead(db):
    sessions.ensure_session("alive", ttl_s=600)
    sessions.ensure_session("dead", ttl_s=-1)
    assert sessions.purge_expired() == 1
    assert sessions.get_session("alive") is not None
    assert sessions.get_session("dead") is None


def test_drop_session(db):
    sessions.ensure_session("t")
    assert sessions.drop_session("t") is True
    assert sessions.get_session("t") is None


def test_set_cursor_creates_missing_row(db):
    """回归守卫：写入方是 UPDATE 语义，行不存在时会**静默空操作**。

    曾因此导致游标根本存不进去 —— 订阅接口调 set_cursor 却从未建行，
    且不报任何错。新增写入路径时必须先过 _ensure_row。
    """
    assert sessions.get_session("fresh") is None
    assert sessions.set_cursor("fresh", symbol="NVDA", timeframe="1d") is True
    row = sessions.get_session("fresh")
    assert row is not None and row["symbol"] == "NVDA"


def test_set_view_state_creates_missing_row(db):
    assert sessions.set_view_state("fresh2", keep_analysis=True) is True
    row = sessions.get_session("fresh2")
    assert row is not None and row["keep_analysis"] == 1


# ── 仓储 ──────────────────────────────────────────────────────────────────────


def _make_record(symbol="BTCUSDT", timeframe="1h", exchange="GATEIO", ts=1000):
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-10-04_12:00:00",
            timestamp_local_ms=ts,
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


def test_upsert_and_fetch(db):
    rec = _make_record()
    assert repositories.upsert_record(rec, file_path=Path("/tmp/r1.json")) is True
    found = repositories.find_latest_successful_record_db(
        symbol="BTCUSDT", timeframe="1h"
    )
    assert found is not None
    assert found["meta"]["symbol"] == "BTCUSDT"


def test_incremental_anchor_is_scoped_per_symbol(db):
    """核心铁律：A tab 的 BTCUSDT 增量不得捞到 NVDA 的上下文。"""
    repositories.upsert_record(_make_record("BTCUSDT", "1h", ts=1000),
                               file_path=Path("/tmp/a.json"))
    repositories.upsert_record(_make_record("NVDA", "1h", ts=2000),
                               file_path=Path("/tmp/b.json"))
    got = repositories.find_latest_successful_record_db(symbol="NVDA")
    assert got["meta"]["symbol"] == "NVDA", "跨标的串味"


def test_incremental_anchor_picks_newest(db):
    repositories.upsert_record(_make_record("BTCUSDT", "1h", ts=1000),
                               file_path=Path("/tmp/a.json"))
    repositories.upsert_record(_make_record("BTCUSDT", "1h", ts=5000),
                               file_path=Path("/tmp/b.json"))
    got = repositories.find_latest_successful_record_db(symbol="BTCUSDT")
    assert got["meta"]["timestamp_local_ms"] == 5000


def test_cross_symbol_browse_when_filters_empty(db):
    """「历史数据都能看」：过滤条件全空时必须跨品种返回。"""
    repositories.upsert_record(_make_record("BTCUSDT", ts=1), file_path=Path("/tmp/a.json"))
    repositories.upsert_record(_make_record("NVDA", ts=2), file_path=Path("/tmp/b.json"))
    symbols = {r["symbol"] for r in repositories.list_records()}
    assert symbols == {"BTCUSDT", "NVDA"}


def test_partial_records_excluded_by_default(db):
    rec = _make_record()
    raw = rec.model_dump()
    raw["_partial_reason"] = "network"
    repositories.upsert_record(rec, raw=raw, file_path=Path("/tmp/p.json"))
    assert repositories.list_records() == []
    assert len(repositories.list_records(include_partial=True)) == 1


def test_upsert_is_idempotent(db):
    rec = _make_record()
    path = Path("/tmp/same.json")
    repositories.upsert_record(rec, file_path=path)
    repositories.upsert_record(rec, file_path=path)
    assert len(repositories.list_records()) == 1


def test_record_without_kline_excluded_from_anchor(db):
    rec = _make_record()
    rec.kline_data = []
    repositories.upsert_record(rec, file_path=Path("/tmp/n.json"))
    assert repositories.find_latest_successful_record_db(symbol="BTCUSDT") is None


def test_delete_record(db):
    rec = _make_record()
    repositories.upsert_record(rec, file_path=Path("/tmp/d.json"))
    row = repositories.list_records()[0]
    assert repositories.delete_record(row["record_id"]) is True
    assert repositories.list_records() == []


# ── 导入器 ────────────────────────────────────────────────────────────────────


def test_import_is_idempotent(db, tmp_path):
    src = tmp_path / "records"
    src.mkdir()
    payload = _make_record().model_dump()
    (src / "r1.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    first = importer.import_analysis_records(src)
    assert first == {"scanned": 1, "imported": 1, "skipped": 0}
    second = importer.import_analysis_records(src)
    assert second["imported"] == 1
    assert len(repositories.list_records()) == 1, "重复导入产生了重复行"


def test_import_skips_bad_file_without_aborting(db, tmp_path):
    """一条坏文件不得中断整批导入。"""
    src = tmp_path / "records"
    src.mkdir()
    good = _make_record().model_dump()
    (src / "good.json").write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    (src / "truncated.json").write_text('{"meta": {"symbol": "X"', encoding="utf-8")
    (src / "garbage.json").write_text("not json at all", encoding="utf-8")

    stats = importer.import_analysis_records(src)
    assert stats["imported"] == 1
    assert stats["skipped"] == 2


def test_import_empty_dir(db, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert importer.import_analysis_records(empty) == {
        "scanned": 0, "imported": 0, "skipped": 0
    }


def test_import_missing_dir(db, tmp_path):
    assert importer.import_analysis_records(tmp_path / "nope") == {
        "scanned": 0, "imported": 0, "skipped": 0
    }


# ── 一次性初始化不变式（2026-10-05 评审 B5/B6）───────────────────────────────
# 原则：一个环境只有一个 DB 文件，路径启动时定死，只在启动时初始化一次。


def test_get_hub_does_not_create_db(db_path_isolated: Path):
    """get_hub 只取实例，**绝不建库**。

    建库必须显式 initialize_storage()。此前 get_hub 内部顺手 migrate()，
    于是任何一处 import 触发调用都可能在真实数据目录里建出库 —— 测试写脏
    开发者真实数据的根因。
    """
    from pa_agent.storage.db import get_hub, initialize_storage

    hub = get_hub()
    assert not db_path_isolated.exists(), "get_hub 不该建库"
    initialize_storage()
    assert db_path_isolated.exists(), "initialize_storage 才建库"


def test_execute_refused_before_initialization(db_path_isolated: Path):
    hub = get_hub()
    assert hub.execute("CREATE TABLE t(a)") is False
    initialize_storage()
    assert hub.execute("CREATE TABLE IF NOT EXISTS t(a)") is True


def test_initialize_storage_is_idempotent(db_path_isolated: Path):
    h1 = initialize_storage()
    h2 = initialize_storage()
    assert h1 is h2 and h2._initialized


def test_reset_hub_for_tests_requires_explicit_path():
    """省略 path 曾静默回落到真实 records/pa_agent.db —— 测试因此写脏真实数据。"""
    from pa_agent.storage.db import reset_hub_for_tests

    with pytest.raises(ValueError, match="必须显式传 path"):
        reset_hub_for_tests()


def test_stats_does_not_raise(db_path_isolated: Path):
    """回归守卫：stats() 曾引用不存在的属性，导致启动时整个存储初始化失败。"""
    initialize_storage()
    st = get_hub().stats()
    assert st["initialized"] is True
    assert "read_failed" in st and st["read_failed"] is False


def test_concurrent_lock_error_does_not_latch(db_path_isolated: Path):
    """『database is locked』是并发问题，不是致命故障 —— 闩死会让进程永久退化。"""
    hub = initialize_storage()
    hub._maybe_latch("database is locked")
    assert hub.disabled is False


def test_corruption_does_latch(db_path_isolated: Path):
    hub = initialize_storage()
    hub._maybe_latch("file is not a database")
    assert hub.disabled is True


def test_read_failure_is_distinguishable_from_empty(db_path_isolated: Path):
    """读失败 ≠ 表里没数据。把两者混同会把损坏文件升格成系统兜底。"""
    hub = initialize_storage()
    assert hub.query("SELECT * FROM global_config") == []
    assert hub.read_failed is False, "正常读到空表不算失败"

    hub._read_error = "no such table: nope"
    assert hub.query("SELECT * FROM nope") == []
    assert hub.read_failed is True, "读失败必须能被上层识别"
