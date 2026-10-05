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


# ── 多会话推理：快照游标入参化 + 多槽缓存 ──────────────────────────────────────
# 回归背景：原先 latest_snapshot 只按 n 判 TTL（单槽），换品种后仍会命中上一个
# 标的的缓存；且游标绑定在 data_source.subscribe() 上，多标签页互相串味。


class _FakeBars(list):
    pass


def _mk_src(tmp_path):
    from pa_agent.data.tradingview import TradingViewSource

    s = TradingViewSource.__new__(TradingViewSource)
    import threading

    s._path_lock = threading.Lock()
    s._snapshot_lock = threading.RLock()
    s._snap_cache_by_key = {}
    s._exchange = "GATEIO"
    s._symbol = "BTCUSDT"
    s._timeframe = "1h"
    s._tv = object()
    calls: list[tuple] = []

    def inner(n, *, exchange=None, symbol=None, timeframe=None):
        calls.append((exchange, symbol, timeframe))
        return _FakeBars([{"close": float(len(calls))}])

    s._latest_snapshot_inner = inner
    return s, calls


def test_snapshot_accepts_cursor_args(tmp_path):
    """游标变成入参：入参优先于当前订阅（省略时才回落）。"""
    s, calls = _mk_src(tmp_path)
    s.latest_snapshot(10, symbol="NVDA", timeframe="1d", exchange="NASDAQ")
    assert calls[-1] == ("NASDAQ", "NVDA", "1d")


def test_snapshot_falls_back_to_subscription(tmp_path):
    """不传参时沿用当前订阅 —— GUI 与单标签页用法保持不变。"""
    s, calls = _mk_src(tmp_path)
    s.latest_snapshot(10)
    assert calls[-1] == ("GATEIO", "BTCUSDT", "1h")


def test_snapshot_cache_is_keyed_by_instrument(tmp_path):
    """核心回归：切品种后不得返回上一个标的的 K 线。"""
    s, _ = _mk_src(tmp_path)
    a = s.latest_snapshot(10, symbol="BTCUSDT")
    b = s.latest_snapshot(10, symbol="NVDA")
    assert a[0]["close"] != b[0]["close"], "两个标的命中了同一份缓存"


def test_snapshot_cache_reused_for_same_instrument(tmp_path):
    s, calls = _mk_src(tmp_path)
    s.latest_snapshot(10, symbol="NVDA")
    s.latest_snapshot(10, symbol="NVDA")
    assert len(calls) == 1, "同标的同根数应命中缓存，不重复取数"


def test_snapshot_cache_distinguishes_count(tmp_path):
    s, calls = _mk_src(tmp_path)
    s.latest_snapshot(10, symbol="NVDA")
    s.latest_snapshot(50, symbol="NVDA")
    assert len(calls) == 2, "根数不同不应共用缓存"


def test_snapshot_cache_is_bounded(tmp_path):
    """多槽后必须自限容量，否则「切过多少标的」会让缓存无限增长。"""
    from pa_agent.data.tradingview import _SNAP_CACHE_MAX_ENTRIES

    s, _ = _mk_src(tmp_path)
    for i in range(_SNAP_CACHE_MAX_ENTRIES + 20):
        s.latest_snapshot(10, symbol=f"SYM{i}")
    assert len(s._snap_cache_by_key) <= _SNAP_CACHE_MAX_ENTRIES


# ── schema.sql 与 Python 真源的漂移守护 ────────────────────────────────────────


def test_schema_sql_matches_python():
    """schema.sql 是派生产物，**必须与 Python 真源逐句一致**。

    没有这条，两者会悄悄漂移：改表结构只改了 Python，运维拿到的
    schema.sql 仍是旧结构，手工重建出来的库缺列，而应用启动时又走
    Python 定义 —— 两套结构并存，问题极难定位。
    """
    from pa_agent.storage.schema import MIGRATIONS, all_statements, render_sql

    sql = render_sql()
    for stmt in all_statements():
        assert stmt.strip() + ";" in sql, f"schema.sql 缺少语句：{stmt[:60]}"
    # 迁移必须以注释形式在册，但不能作为可执行语句 —— 否则空库重建会撞
    # duplicate column（CREATE 里已经有这些列了）。
    for _table, stmt in MIGRATIONS:
        assert stmt.strip() in sql, f"迁移未在 schema.sql 中留档：{stmt[:50]}"


def test_schema_sql_file_is_up_to_date():
    """落盘的 schema.sql 必须等于 render_sql() —— 忘了重新生成就红。"""
    from pathlib import Path as _P

    from pa_agent.storage.schema import render_sql

    on_disk = _P(__file__).resolve().parents[2] / "pa_agent/storage/schema.sql"
    assert on_disk.exists(), "schema.sql 缺失；跑 python -m pa_agent.storage.schema 生成"
    assert on_disk.read_text(encoding="utf-8") == render_sql(), (
        "schema.sql 已过期；跑 python -m pa_agent.storage.schema 重新生成"
    )


def test_rendered_sql_builds_every_table_on_empty_db():
    """渲染出的 SQL 能在空库上建出全部表。"""
    import sqlite3

    from pa_agent.storage.schema import render_sql, tables

    conn = sqlite3.connect(":memory:")
    conn.executescript(render_sql())
    got = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = set(tables()) - got
    assert not missing, f"渲染的 SQL 未建出这些表：{missing}"
    conn.close()


def test_migrate_tolerates_second_run(tmp_path):
    """应用启动路径**必须**可重复跑（migrate 容忍 duplicate column）。

    渲染出的裸 SQL 二次执行会报错 —— 这是刻意区分：手工重建跑一次即可，
    而应用启动每次都要能重跑（容器重启、测试复用同一个库）。
    """
    from pa_agent.storage.db import reset_hub_for_tests

    hub = reset_hub_for_tests(tmp_path / "twice.db")
    assert hub.migrate() is True, "首次迁移失败"
    assert hub.migrate() is True, "二次迁移失败 —— duplicate column 应当被容忍"
