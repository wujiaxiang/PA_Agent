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
    delete_entry,
    get_entry,
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


# ── 经验库：存储层关注点（业务行为见 test_experience_*）─────────────────────

def test_experience_upsert_and_fetch(db):
    upsert_entry({"symbol": "BTCUSDT", "pnl_pct": 3.2}, entry_id="admin_e1",
                 cycle_position="trending_tr", status="win",
                 symbol="BTCUSDT", timeframe="1h")
    got = get_entry("admin_e1", user_id="admin")
    assert got is not None and got["pnl_pct"] == 3.2
    assert got["status"] == "win", "status 以**列**为准，必须回填进 payload"
    assert count_by_status() == {"win": 1}


def test_entry_id_is_partitioned_by_user(db):
    """**回归守卫**：跨用户不得互相覆盖。

    ``entry_id`` 是全局 PRIMARY KEY，而 ``ON CONFLICT DO UPDATE SET`` 的列
    清单里没有 ``user_id``。曾经 entry_id 就是裸的 case id，两个用户各自产生
    同一条记录时后者会静默覆盖前者的 ``content_json``，归属仍留在原主 ——
    一方的案例凭空消失且无任何报错。
    """
    from pa_agent.storage.experience_repo import list_entries as _list

    upsert_entry({"who": "alice"}, entry_id="alice_case1", cycle_position="trending_tr",
                 status="win", symbol="X", timeframe="1h", user_id="alice")
    upsert_entry({"who": "bob"}, entry_id="bob_case1", cycle_position="trending_tr",
                 status="win", symbol="X", timeframe="1h", user_id="bob")

    assert len(_list(user_id="alice")) == 1 and len(_list(user_id="bob")) == 1
    assert "alice" in _list(user_id="alice")[0]["content_json"]
    assert "bob" in _list(user_id="bob")[0]["content_json"], "B 的内容不得被 A 覆盖"


def test_status_transition_is_update_not_duplicate(db):
    """pending → win 必须是同一行 UPDATE，不能每次结算多出一行。"""
    upsert_entry({"symbol": "BTCUSDT"}, entry_id="admin_c1",
                 cycle_position="trending_tr", status="pending",
                 symbol="BTCUSDT", timeframe="1h")
    upsert_entry({"symbol": "BTCUSDT"}, entry_id="admin_c1",
                 cycle_position="trending_tr", status="win",
                 symbol="BTCUSDT", timeframe="1h")
    assert count_by_status() == {"win": 1}, "结算后不应残留 pending 行"


def test_status_vocabulary_guard(db):
    """**回归守卫**：状态词表必须与 ``experience_writer.STATUS_*`` 一致。

    这里曾写成 ("success","failure",...)，而写入端发的是 "win"/"loss"，
    ``upsert_entry`` 的兜底分支把它们统统静默改写成 "pending" —— 每一条已结算
    的经验在库里都显示为待验证，检索端因此永远取不到，整个经验库静默失效。
    """
    from pa_agent.records.experience_writer import (
        STATUS_LOSS, STATUS_PENDING, STATUS_UNRESOLVED, STATUS_WIN,
    )
    from pa_agent.storage.experience_repo import _VALID_STATUSES

    assert set(_VALID_STATUSES) == {STATUS_WIN, STATUS_LOSS, STATUS_PENDING, STATUS_UNRESOLVED}

    for want in (STATUS_WIN, STATUS_LOSS):
        upsert_entry({"i": want}, entry_id=f"admin_{want}", cycle_position="trending_tr",
                     status=want, symbol="X", timeframe="1h")
    from pa_agent.storage.experience_repo import list_entries as _list

    got = {r["status"] for r in _list()}
    assert got == {"win", "loss"}, f"已结算状态被改写了：{got}"


def test_unknown_status_falls_back_to_pending(db):
    """未知状态归 pending（最保守），不得凭空造出可检索的成功经验。"""
    upsert_entry({"symbol": "X"}, entry_id="admin_x", cycle_position="trending_tr",
                 status="bogus", symbol="X", timeframe="1h")
    assert count_by_status() == {"pending": 1}


def test_list_entries_statuses_and_filters(db):
    from pa_agent.storage.experience_repo import list_entries as _list

    for i, (sym, st) in enumerate([("BTCUSDT", "win"), ("NVDA", "loss"),
                                   ("BTCUSDT", "win"), ("ETHUSDT", "pending")]):
        upsert_entry({"i": i}, entry_id=f"admin_e{i}", cycle_position="trending_tr",
                     status=st, symbol=sym, timeframe="1h")

    both = _list(statuses=["win", "loss"])
    assert {r["status"] for r in both} == {"win", "loss"}
    assert "pending" not in {r["status"] for r in both}, "未决不得被检索端拿到"
    assert len(_list(statuses=["win", "loss"], limit=2)) == 2, "limit 必须在合并之后生效"
    assert len(_list(symbol="BTCUSDT", statuses=["win", "loss"])) == 2
    assert count_by_status(symbol="BTCUSDT") == {"win": 2}


def test_get_entry_scoped_by_user(db):
    upsert_entry({"who": "alice"}, entry_id="alice_x", cycle_position="trending_tr",
                 status="win", symbol="X", timeframe="1h", user_id="alice")
    assert get_entry("alice_x", user_id="alice") is not None
    assert get_entry("alice_x", user_id="bob") is None
    assert get_entry("alice_x", user_id=None) is not None, "结算侧需要不过滤地读"


def test_experience_delete(db):
    upsert_entry({"symbol": "X"}, entry_id="admin_d", cycle_position="trending_tr",
                 status="win", symbol="X", timeframe="1h")
    assert delete_entry("admin_d") is True
    assert get_entry("admin_d") is None


# ── 查询结果的失败状态必须自带（P-1b）─────────────────────────────────────────

def test_query_result_reports_failure_explicitly(db):
    """``list_entries()`` 必须把「本次查询失败」随结果一起交出去。

    ``hub.query()`` 失败返回 ``[]``，与「库里确实没有」完全同形。调用方若靠
    事后去读 hub 上的标志位判断，就有读后时序：同线程内后一次成功读会把前一次
    的失败标记清掉，于是把故障放行成「空库」。
    """
    from pa_agent.storage.experience_repo import list_entries as _list

    upsert_entry({"symbol": "BTCUSDT"}, entry_id="admin_a", cycle_position="trending_tr",
                 status="win", symbol="BTCUSDT", timeframe="1h")

    ok = _list(statuses=["win", "loss"])
    assert isinstance(ok, list) and ok, "仍是 list，既有调用方不受影响"
    assert ok.failed is False and ok.error == ""

    db.query("DROP TABLE experience_entries")
    bad = _list(statuses=["win", "loss"])
    assert bad == []
    assert bad.failed is True, "读不出来必须被显式标记，而不是伪装成空库"
    assert bad.error
