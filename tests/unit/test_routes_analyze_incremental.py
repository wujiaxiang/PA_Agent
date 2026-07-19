# -*- coding: utf-8 -*-
"""Unit tests for web/api/routes_analyze.py — incremental analysis endpoint.

覆盖：
- GET /api/analyze/incremental/stream 在无历史记录时返回 404
- GET /api/analyze/incremental/stream 在有历史记录时透传 SSE 事件并附带
  ``incremental=True`` 与 ``incremental_new_bar_count`` 元数据
- POST /api/analyze/incremental 在无历史记录时返回 404，有历史记录时返回
  ``status=ok`` JSON
- bar_count 参数校验（ge=2, le=5000）

mock 策略：
- 用真实 FastAPI app + 注入 mock ctx
- patch ``find_latest_successful_record`` 控制是否有历史记录
- patch ``TwoStageOrchestrator`` 与 ``build_display_frame`` 让 submit 回调
  直接同步触发事件
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pa_agent.util.threading import OrchestratorEvent
from web.api.routes_analyze import router as analyze_router


def _parse_sse(text: str) -> list[tuple[str, object]]:
    """简易 SSE 解析：返回 [(event, data_obj), ...]。"""
    events: list[tuple[str, object]] = []
    cur_event: str | None = None
    cur_data: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("event: "):
            cur_event = line[len("event: "):].strip()
        elif line.startswith("data: "):
            cur_data.append(line[len("data: "):])
        elif line == "" and cur_event is not None:
            data_str = "\n".join(cur_data)
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                data = data_str
            events.append((cur_event, data))
            cur_event = None
            cur_data = []
    return events


def _make_fake_record() -> MagicMock:
    record = MagicMock()
    record.meta.symbol = "XAUUSD"
    record.meta.timeframe = "1h"
    record.meta.last_close_bar_iso = ""
    record.stage1_diagnosis = {"gate_result": "proceed"}
    record.stage2_decision = {
        "order_type": "limit",
        "order_direction": "做多",
        "entry_price": 2010.5,
    }
    record.strategy_files_used = ["上涨通道.txt"]
    record.usage_total = {"total_tokens": 100}
    record.exception = None
    # raw_debug_payload / debug_files_payload 序列化需要这些字段为 JSON 可序列化类型
    # （MagicMock 默认返回 MagicMock，json.dumps 会失败）
    record.stage1_messages = []
    record.stage2_messages = []
    record.stage1_response = None
    record.stage2_response = None
    record.experience_loaded = []
    return record


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(analyze_router, prefix="/api")
    ctx = MagicMock()
    ctx.settings.general.last_symbol = "XAUUSD"
    ctx.settings.general.last_timeframe = "1h"
    ctx.settings.general.last_tradingview_exchange = "GATEIO"
    ctx.settings.general.incremental_max_new_bars = 10
    ctx.data_source.latest_snapshot.return_value = []
    # kline_data 用于 count_new_bars_since_record；用 MagicMock 简化
    return app, ctx


# ── 参数校验 ──────────────────────────────────────────────────────────────────


def test_incremental_stream_bar_count_below_minimum_returns_422():
    app, _ = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/analyze/incremental/stream?bar_count=1")
    assert resp.status_code == 422


def test_incremental_stream_bar_count_above_maximum_returns_422():
    app, _ = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/analyze/incremental/stream?bar_count=5001")
    assert resp.status_code == 422


# ── 无历史记录 → 404 ──────────────────────────────────────────────────────────


def test_incremental_stream_returns_404_when_no_previous_record():
    app, ctx = _make_app()

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    with patch(
        "web.api.routes_analyze.find_latest_successful_record", return_value=None
    ):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/incremental/stream?bar_count=10")
    assert resp.status_code == 404
    assert "无可用历史记录" in resp.json()["detail"]


def test_incremental_post_returns_404_when_no_previous_record():
    app, ctx = _make_app()

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    with patch(
        "web.api.routes_analyze.find_latest_successful_record", return_value=None
    ):
        with TestClient(app) as c:
            resp = c.post("/api/analyze/incremental")
    assert resp.status_code == 404
    assert "无可用历史记录" in resp.json()["detail"]


# ── 有历史记录 → SSE 事件流 + 增量元数据 ──────────────────────────────────────


def test_incremental_stream_emits_events_with_incremental_metadata():
    app, ctx = _make_app()

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    record = _make_fake_record()
    # mock previous_record：含有 kline_data（count_new_bars_since_record 使用）
    prev_record = MagicMock()
    prev_record.kline_data = [{"ts_open": 1_700_000_000_000}]

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        # 验证 orchestrator 收到 previous_record 与 incremental_new_bar_count
        assert "previous_record" in kwargs
        assert "incremental_new_bar_count" in kwargs
        on_event(OrchestratorEvent.Stage1Started)
        on_event(OrchestratorEvent.Stage1Done)
        on_event(OrchestratorEvent.Stage2Started)
        on_event(OrchestratorEvent.Stage2Done)
        on_event(OrchestratorEvent.RecordSaved)
        return record

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch(
        "web.api.routes_analyze.find_latest_successful_record",
        return_value=prev_record,
    ), patch(
        "web.api.routes_analyze.count_new_bars_since_record", return_value=3
    ), patch(
        "web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch
    ), patch(
        "web.api.routes_analyze.build_display_frame", return_value=MagicMock()
    ):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/incremental/stream?bar_count=10")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types = [e for e, _ in events]
    assert "orchestrator_event" in types
    assert "done" in types

    # done 事件附带 incremental=True 与 incremental_new_bar_count=3
    done_data = next(d for e, d in events if e == "done")
    assert done_data["incremental"] is True
    assert done_data["incremental_new_bar_count"] == 3
    rec = done_data["record"]
    assert rec["symbol"] == "XAUUSD"


def test_incremental_stream_passes_previous_record_to_orchestrator():
    """orchestrator.submit 收到 previous_record 与 incremental_new_bar_count 参数。"""
    app, ctx = _make_app()

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    record = _make_fake_record()
    prev_record = MagicMock()
    prev_record.kline_data = [{"ts_open": 1_700_000_000_000}]

    captured = {}

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        captured["previous_record"] = kwargs.get("previous_record")
        captured["incremental_new_bar_count"] = kwargs.get(
            "incremental_new_bar_count"
        )
        on_event(OrchestratorEvent.Stage1Started)
        on_event(OrchestratorEvent.Stage1Done)
        on_event(OrchestratorEvent.Stage2Done)
        on_event(OrchestratorEvent.RecordSaved)
        return record

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch(
        "web.api.routes_analyze.find_latest_successful_record",
        return_value=prev_record,
    ), patch(
        "web.api.routes_analyze.count_new_bars_since_record", return_value=5
    ), patch(
        "web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch
    ), patch(
        "web.api.routes_analyze.build_display_frame", return_value=MagicMock()
    ):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/incremental/stream?bar_count=20")

    assert resp.status_code == 200
    assert captured["previous_record"] is prev_record
    assert captured["incremental_new_bar_count"] == 5


# ── 新增 K 线超过阈值 → 降级为完整分析（previous_record=None） ───────────────


def test_incremental_stream_falls_back_when_new_bars_exceed_threshold():
    """新增 K 线数 > incremental_max_new_bars 时降级为完整分析。"""
    app, ctx = _make_app()
    # 阈值 = 10，模拟新增 50 根 → 超阈值
    ctx.settings.general.incremental_max_new_bars = 10

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    record = _make_fake_record()
    prev_record = MagicMock()
    prev_record.kline_data = [{"ts_open": 1_700_000_000_000}]

    captured = {}

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        captured["previous_record"] = kwargs.get("previous_record")
        captured["incremental_new_bar_count"] = kwargs.get(
            "incremental_new_bar_count"
        )
        on_event(OrchestratorEvent.Stage1Started)
        on_event(OrchestratorEvent.Stage2Done)
        on_event(OrchestratorEvent.RecordSaved)
        return record

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch(
        "web.api.routes_analyze.find_latest_successful_record",
        return_value=prev_record,
    ), patch(
        "web.api.routes_analyze.count_new_bars_since_record", return_value=50
    ), patch(
        "web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch
    ), patch(
        "web.api.routes_analyze.build_display_frame", return_value=MagicMock()
    ):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/incremental/stream?bar_count=10")

    assert resp.status_code == 200
    # 降级为完整分析 → previous_record=None
    assert captured["previous_record"] is None
    assert captured["incremental_new_bar_count"] is None
    events = _parse_sse(resp.text)
    done_data = next(d for e, d in events if e == "done")
    assert done_data["incremental"] is False


# ── POST 端点：有历史记录时返回 200 ───────────────────────────────────────────


def test_incremental_post_returns_ok_when_previous_record_exists():
    app, ctx = _make_app()

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    prev_record = MagicMock()
    prev_record.meta.timestamp_local_iso = "2026-07-18T14:00:00"

    with patch(
        "web.api.routes_analyze.find_latest_successful_record",
        return_value=prev_record,
    ):
        with TestClient(app) as c:
            resp = c.post("/api/analyze/incremental")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["symbol"] == "XAUUSD"
    assert body["timeframe"] == "1h"
    assert body["previous_record_id"] == "2026-07-18T14:00:00"
