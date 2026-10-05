# -*- coding: utf-8 -*-
"""分析记录落库的 exchange 必须绑定「本会话游标」，而不是全局冻结值。

## 用户实测到的 bug

页面订阅的是 **NASDAQ / NVDA / 5m**，历史弹窗按三元组过滤却一条都查不到，
勾选「全部品种」反而能查到 25 条。写进库的记录 exchange 是 **GATEIO**。

## 根因

`routes_settings._CURSOR_FIELDS`（last_symbol / last_timeframe /
last_tradingview_exchange）是**只读冻结值**：每次请求由 `_apply_session_cursor()`
从会话游标派生后**只回给前端**，而 `POST /api/subscribe` 早已改为只写会话游标
（`web/api/routes_data.py` 的 docstring 明写「不再改 ctx.settings.general.last_*」）。
于是全局那三个字段永远停在「上一次某人订阅时的残留值」。

两处读者都踩了这个坑：

1. **写侧（真正的元凶）** —— `pa_agent/orchestrator/two_stage.py::_build_empty_record`
   原本用 `settings.general.last_tradingview_exchange` 填 `RecordMeta.exchange`。
   `KlineFrame` 根本没有 exchange 字段、`build_display_frame` 也不收该参数，
   所以 `resolve_view()` 拿到的 `view_exchange` **根本没有通道**流到落库的
   记录里 —— 用户订阅 NASDAQ，记录却写成 GATEIO。
2. **读侧** —— `POST /api/analyze/incremental` 的增量预检直接读那三个冻结值，
   导致按错误标的找上一轮记录：本会话明明有历史却误报 404，或捞到别的标的
   当增量锚点。

## 本文件的断言策略

**核心断言走真实生产路径，不 mock `resolve_view`、不 mock `upsert_record`、
不 mock `PendingWriter`**：真实 HTTP `POST /api/subscribe` 写会话游标 →
真实 `GET /api/analyze/stream` → 真实 `TwoStageOrchestrator` → 真实
`PendingWriter` → 真实 SQLite `analysis_records` 行 → 断言该行的 exchange。

分析在 Stage-1 之前就被 `check_preflight_data` 的「bar count >= 20」闸门挡下，
于是**不碰任何模型调用**就能走到真实的落库分支（`save_partial` →
`_mirror_to_sqlite` → `upsert_record`）。这条路径与真实分析共用同一段
`RecordMeta` 构造代码，所以断言有效又不花钱、不联网。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pa_agent.data.base import KlineBar
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.storage import db as db_mod
from pa_agent.storage.repositories import list_records

from web.api.routes_analyze import router as analyze_router
from web.api.routes_data import router as data_router

# ── 场景常量 ────────────────────────────────────────────────────────────────
SESSION_ID = "sess-nasdaq-nvda"
SESSION_SYMBOL = "NVDA"
SESSION_TIMEFRAME = "5m"
SESSION_EXCHANGE = "NASDAQ"          # 用户真正订阅的交易所

# 全局冻结值：上一次「别人」订阅留下的残留。修复前记录的 exchange 会被写成它。
STALE_SYMBOL = "BTCUSDT"
STALE_TIMEFRAME = "1h"
STALE_EXCHANGE = "GATEIO"

# preflight 要求 bar count >= 20 才放行去调模型。给 10 根已收盘 bar →
# 闸门拦下 → save_partial → 落库。全程零模型调用。
BAR_COUNT = 10
TOTAL_BARS = BAR_COUNT + 1           # +1 根 forming bar（newest-first 的 bars[0]）


def _make_bars(n: int = TOTAL_BARS, base_ts: float = 1_700_000_000_000.0) -> list[KlineBar]:
    """构造 newest-first 的 K 线：bars[0] 为未收盘 forming bar，其余已收盘。

    价位刻意围绕 100（不是 48000 这类加密货币量级），与 NVDA 的量级无关 ——
    本测试断言的是 exchange 列的绑定关系，不是价格。
    """
    step = 300_000.0                   # 5m 一根
    bars: list[KlineBar] = []
    for i in range(n):
        close = 100.0 + i * 0.1
        # i == 0 → forming bar；i >= 1 → 已收盘
        closed = i != 0
        bars.append(
            KlineBar(
                seq=0 if not closed else i,
                ts_open=base_ts + i * step,
                open=close - 0.05,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=1000.0 + i,
                closed=closed,
            )
        )
    return bars


class _StubDataSource:
    """只提供 `latest_snapshot` 的数据源替身。

    **这不是 mock 掉被测对象**：被测的 bug 在「记录落库时 exchange 取自哪里」，
    与行情从哪来无关。K 线必须是真实 `KlineBar`，才能真正走进
    `build_display_frame` 与 orchestrator 的记录构造。

    `connect/subscribe/set_exchange` 是为了让真实 `POST /api/subscribe` 能跑完 ——
    订阅本身也必须走真路由，否则测的就不是生产路径了。
    """

    def __init__(self, bars: list[KlineBar]) -> None:
        self._bars = bars
        self._connected = True          # 让 subscribe 跳过 connect() 重连
        self.subscribed: tuple[str, str] = ("", "")
        self.exchange: str = ""
        self.seen: list[dict] = []

    # ── /api/subscribe 用到的接口 ────────────────────────────────────
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def set_exchange(self, exchange: str) -> None:
        self.exchange = exchange

    def subscribe(self, symbol: str, timeframe: str) -> None:
        self.subscribed = (symbol, timeframe)

    # ── 取数 ──────────────────────────────────────────────────────────
    def latest_snapshot(self, n: int, **kwargs):
        self.seen.append({"n": n, **kwargs})
        return list(self._bars)


@pytest.fixture(autouse=True)
def record_scan_root(tmp_path, monkeypatch):
    """把 `find_latest_successful_record` 的**磁盘扫描根**隔离到 tmp。

    必须做：那条链路（`pa_agent/records/analysis_history.py`）扫的是
    `RECORDS_PENDING_DIR` 这个**进程级常量**指向的真实 `records/pending`，
    完全绕开 `PA_AGENT_DB_PATH` 的 DB 隔离 —— 不隔离的话，增量预检会拿开发者
    真实的历史记录当断言依据，而本仓库的 `records/pending` 里确实躺着生产数据。

    返回值同时用作 `PendingWriter` 的落盘根，保证「写的」与「扫的」是同一处。
    """
    from pa_agent.records import analysis_history

    target = tmp_path / "pending"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(analysis_history, "RECORDS_PENDING_DIR", target)
    yield target


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """把 hub 指向 tmp 下独立 DB，并建好 schema。测试绝不碰 records/pa_agent.db。"""
    target = tmp_path / "exchange_binding.db"
    monkeypatch.setattr(db_mod, "db_path", lambda: target)
    db_mod.reset_hub_for_tests(target, initialize=True)
    yield target
    db_mod.reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


@pytest.fixture()
def app_with_ctx(isolated_db, record_scan_root):
    """真实 FastAPI app + 真实 ctx 对象。

    关键设定：
    - `settings.general.last_*` = **冻结残留值**（GATEIO/BTCUSDT/1h）
    - `pending_writer` = 真实 PendingWriter，写到隔离根（与扫描根同一处）
    - `data_source` = 真实 K 线替身
    """
    from pa_agent.config.settings import Settings

    app = FastAPI()
    app.include_router(analyze_router, prefix="/api")
    app.include_router(data_router, prefix="/api")

    settings = Settings()
    settings.general.last_symbol = STALE_SYMBOL
    settings.general.last_timeframe = STALE_TIMEFRAME
    settings.general.last_tradingview_exchange = STALE_EXCHANGE
    settings.general.incremental_max_new_bars = 10
    # 与测试用的数据源替身同类型，避免 /api/subscribe 因「换了数据源」而
    # 重新 create_data_source() 把替身换成真实 TradingView 连接。
    settings.general.last_data_source = "tradingview"

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.settings = settings
    ctx.data_source = _StubDataSource(_make_bars())
    ctx.pending_writer = PendingWriter(pending_dir=record_scan_root)
    ctx.client = None
    ctx.assembler = None
    ctx.router = None
    ctx.validator = None
    ctx.exp_reader = None

    app.state.ctx = ctx
    return app, ctx


def _drain_sse(text: str) -> list[dict]:
    """把 SSE 响应体拆成事件 dict 列表。"""
    events: list[dict] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        payload: dict = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                payload["type"] = line[len("event: "):].strip()
            elif line.startswith("data: "):
                try:
                    payload["data"] = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    payload["data"] = line[len("data: "):]
        if payload:
            events.append(payload)
    return events


def _read_exchange_rows(db_file: Path) -> list[dict]:
    """直接从 SQLite 读 analysis_records 的 exchange 列 —— 不经任何读取端封装。"""
    import sqlite3

    conn = sqlite3.connect(str(db_file))
    try:
        cur = conn.execute(
            "SELECT record_id, exchange, symbol, timeframe, status, payload_json"
            " FROM analysis_records ORDER BY created_at"
        )
        cols = ["record_id", "exchange", "symbol", "timeframe", "status", "payload_json"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


# ── 核心断言：订阅 NASDAQ → 落库必须是 NASDAQ ────────────────────────────────

def test_analysis_writes_session_exchange_not_stale_global(app_with_ctx, isolated_db):
    """**本次修复的核心断言。**

    走完整生产路径：真实 `POST /api/subscribe` 订阅 NASDAQ/NVDA/5m →
    真实 `GET /api/analyze/stream` → 真实 orchestrator → 真实 PendingWriter →
    真实 SQLite。断言落库行的 exchange 是 NASDAQ，**不是**全局冻结的 GATEIO。

    不 mock `resolve_view`、不 mock `upsert_record`、不 mock `PendingWriter` ——
    mock 掉它们之后测试永远绿，而那正是这个 bug 的藏身之处。
    """
    app, ctx = app_with_ctx
    client = TestClient(app)
    headers = {"X-Session-Id": SESSION_ID}

    # ── 1. 真实订阅：会话游标写入 NASDAQ/NVDA/5m ──────────────────────
    r = client.post(
        "/api/subscribe",
        json={
            "kind": "tradingview",
            "symbol": SESSION_SYMBOL,
            "timeframe": SESSION_TIMEFRAME,
            "exchange": SESSION_EXCHANGE,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["exchange"] == SESSION_EXCHANGE

    # 全局冻结值**确实**还是别人的残留（证明这不是测试自己造出来的巧合）
    assert ctx.settings.general.last_tradingview_exchange == STALE_EXCHANGE

    # ── 2. 真实发起分析（bar_count=10 < preflight 的 20，零模型调用）────
    r = client.get(
        f"/api/analyze/stream?bar_count={BAR_COUNT}",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    events = _drain_sse(r.text)
    types = [e["type"] for e in events]
    assert "done" in types or "error" in types, types

    # ── 3. 直接查真实 SQLite ──────────────────────────────────────────
    rows = _read_exchange_rows(isolated_db)
    assert rows, "分析后 analysis_records 应至少有一行（落库链路没跑通）"

    assert len(rows) == 1, f"预期只写一条，实际 {[r_['record_id'] for r_ in rows]}"
    row = rows[0]

    # ★ 核心断言：落库 exchange 必须是本会话游标的 NASDAQ
    assert row["exchange"] == SESSION_EXCHANGE, (
        f"落库 exchange 应为 {SESSION_EXCHANGE!r}，实际 {row['exchange']!r} —— "
        f"说明记录取的是全局冻结值 {STALE_EXCHANGE!r}（用户实测到的 bug）"
    )
    assert row["exchange"] != STALE_EXCHANGE
    # 品种/周期必须也来自会话游标，且三者自洽
    assert row["symbol"] == SESSION_SYMBOL
    assert row["timeframe"] == SESSION_TIMEFRAME

    # payload 里的 meta.exchange 是同一个值（排除「只改了索引列」的可能）
    payload = json.loads(row["payload_json"])
    assert payload["meta"]["exchange"] == SESSION_EXCHANGE
    assert payload["meta"]["symbol"] == SESSION_SYMBOL
    assert payload["meta"]["timeframe"] == SESSION_TIMEFRAME


def test_taken_bars_use_session_cursor_exchange(app_with_ctx):
    """取数也必须按会话游标 —— 否则「分析对了、数据是别的标的的」。"""
    app, _ctx = app_with_ctx
    client = TestClient(app)
    headers = {"X-Session-Id": SESSION_ID}

    client.post(
        "/api/subscribe",
        json={
            "kind": "tradingview",
            "symbol": SESSION_SYMBOL,
            "timeframe": SESSION_TIMEFRAME,
            "exchange": SESSION_EXCHANGE,
        },
        headers=headers,
    )
    client.get(f"/api/analyze/stream?bar_count={BAR_COUNT}", headers=headers)

    seen = app.state.ctx.data_source.seen
    assert seen, "分析路径应取过一次行情"
    call = seen[-1]
    assert call["exchange"] == SESSION_EXCHANGE
    assert call["symbol"] == SESSION_SYMBOL
    assert call["timeframe"] == SESSION_TIMEFRAME


# ── 用户症状复现：三元组过滤查不到 / 「全部品种」能查到 ────────────────────

def test_record_findable_by_session_triple_but_not_by_stale_triple(
    app_with_ctx, isolated_db
):
    """复现「按当前品类查不到、勾选全部品种能查到」。

    历史弹窗按 (exchange, symbol, timeframe) 三元组精确匹配。所以 exchange
    落错时，**只有三元组过滤受影响**，不带过滤的列表照常有数据 ——
    这正是用户看到的矛盾现象。
    """
    app, _ctx = app_with_ctx
    client = TestClient(app)
    headers = {"X-Session-Id": SESSION_ID}

    client.post(
        "/api/subscribe",
        json={
            "kind": "tradingview",
            "symbol": SESSION_SYMBOL,
            "timeframe": SESSION_TIMEFRAME,
            "exchange": SESSION_EXCHANGE,
        },
        headers=headers,
    )
    client.get(f"/api/analyze/stream?bar_count={BAR_COUNT}", headers=headers)

    # 用户正在看的品类 → 必须查得到
    by_view = list_records(
        exchange=SESSION_EXCHANGE,
        symbol=SESSION_SYMBOL,
        timeframe=SESSION_TIMEFRAME,
        include_partial=True,
    )
    assert len(by_view) == 1, (
        f"按 {SESSION_EXCHANGE}/{SESSION_SYMBOL}/{SESSION_TIMEFRAME} 应查到 1 条，"
        f"实际 {len(by_view)} —— 用户的「历史弹窗查不到记录」未被修复"
    )

    # 冻结残留值那个三元组 → 一条都不该查到
    by_stale = list_records(
        exchange=STALE_EXCHANGE,
        symbol=STALE_SYMBOL,
        timeframe=STALE_TIMEFRAME,
        include_partial=True,
    )
    assert by_stale == []


# ── 读侧：增量预检也必须按会话游标 ──────────────────────────────────────────

def _seed_successful_record(pending_writer: PendingWriter, *, exchange: str,
                            symbol: str, timeframe: str, uid: int = 0) -> Path:
    """经**真实** ``PendingWriter.save_full`` 落一条「成功」的分析记录。

    必须是真的成功记录（stage1_diagnosis / stage2_decision / kline_data 齐备、
    无 exception），否则 ``find_latest_successful_record`` 一律跳过它 ——
    那条链路是扫磁盘 JSON 的，不是读 DB。
    """
    from datetime import datetime, timezone

    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    ts_ms = 1_700_000_000_000 + uid * 1000
    ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    )
    record = AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso=ts_iso,
            timestamp_local_ms=ts_ms,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
            bar_count=BAR_COUNT,
            ai_provider={"model": "test", "base_url": "", "api_key": "****"},
        ),
        kline_data=[
            {"ts_open": b.ts_open, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume, "closed": b.closed}
            for b in _make_bars()
        ],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis={"gate_result": "proceed", "direction": "bullish"},
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={"order_type": "limit", "order_direction": "做多"},
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={"total_tokens": 100},
    )
    return pending_writer.save_full(record)


def _subscribe(client, headers) -> None:
    r = client.post(
        "/api/subscribe",
        json={
            "kind": "tradingview",
            "symbol": SESSION_SYMBOL,
            "timeframe": SESSION_TIMEFRAME,
            "exchange": SESSION_EXCHANGE,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["exchange"] == SESSION_EXCHANGE


def test_incremental_precheck_uses_session_cursor(app_with_ctx):
    """`POST /api/analyze/incremental` 的预检必须按**会话游标**查上一轮记录。

    修复前它读全局冻结的 GATEIO/BTCUSDT/1h，于是本会话明明有一条 NASDAQ 的
    记录，它却报 404「无可用历史记录，请使用完整分析」—— 用户点「增量」
    永远被劝退；反过来若冻结值恰好有记录，则会捞到别的标的当增量锚点。
    """
    app, ctx = app_with_ctx
    client = TestClient(app)
    headers = {"X-Session-Id": SESSION_ID}

    # 真实落一条 NASDAQ/NVDA/5m 的成功记录（走真实 PendingWriter → 真实文件）
    _seed_successful_record(
        ctx.pending_writer,
        exchange=SESSION_EXCHANGE, symbol=SESSION_SYMBOL, timeframe=SESSION_TIMEFRAME,
    )
    _subscribe(client, headers)

    r = client.post("/api/analyze/incremental", headers=headers)
    assert r.status_code == 200, (
        f"按会话游标预检应通过，实际 {r.status_code}：{r.text} —— "
        f"修复前它按全局冻结值 {STALE_EXCHANGE}/{STALE_SYMBOL}/{STALE_TIMEFRAME} "
        f"去查，必然 404"
    )
    body = r.json()
    assert body["symbol"] == SESSION_SYMBOL
    assert body["timeframe"] == SESSION_TIMEFRAME


def test_incremental_precheck_still_404s_without_any_matching_record(
    app_with_ctx,
):
    """反向边界：库里/盘上没有对应记录时仍应 404，不能因为改了就无脑放行。"""
    app, ctx = app_with_ctx
    client = TestClient(app)
    headers = {"X-Session-Id": SESSION_ID}

    _subscribe(client, headers)
    r = client.post("/api/analyze/incremental", headers=headers)
    assert r.status_code == 404


def test_incremental_precheck_falls_back_to_global_without_session(
    app_with_ctx,
):
    """无会话（老客户端 / 直连 API）时仍回落全局 —— 向后兼容不得破坏。"""
    app, ctx = app_with_ctx
    client = TestClient(app)
    headers = {"X-Session-Id": SESSION_ID}

    _seed_successful_record(
        ctx.pending_writer,
        exchange=SESSION_EXCHANGE, symbol=SESSION_SYMBOL, timeframe=SESSION_TIMEFRAME,
    )
    _subscribe(client, headers)

    # 无 X-Session-Id → resolve_view 回落全局冻结值 → NASDAQ 那条查不到 → 404
    r = client.post("/api/analyze/incremental")
    assert r.status_code == 404