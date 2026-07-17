# -*- coding: utf-8 -*-
"""Unit tests for web/api/routes_analyze.py — SSE two-stage analysis stream.

覆盖：
- bar_count 参数校验（ge=2, le=5000）
- mock TwoStageOrchestrator.submit 触发事件序列 → SSE 包含 orchestrator_event
- done 事件包含 decision_tree 与 decision_overlay 载荷
- orchestrator 抛异常时 SSE 输出 error 事件

mock 策略：patch TwoStageOrchestrator 与 build_display_frame，让 submit 回调
直接同步触发事件，验证 SSE 事件流顺序与载荷。
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
    """构造一个含 decision_tree 来源字段的 mock AnalysisRecord。"""
    record = MagicMock()
    record.meta.symbol = "XAUUSD"
    record.meta.timeframe = "1h"
    record.stage1_diagnosis = {
        "gate_trace": [{"node_id": "G1", "answer": "是"}],
        "gate_result": "proceed",
    }
    record.stage2_response = {
        "decision_trace": [{"node_id": "10.3", "answer": "是"}],
        "terminal": {"node_id": "11.2", "outcome": "trade"},
        "gate_shortcircuited": False,
    }
    record.stage2_decision = {
        "order_type": "突破单",
        "order_direction": "做多",
        "chart_overlay_active": True,
        "entry_price": 2010.5,
        "stop_loss_price": 1995.0,
        "take_profit_price": 2050.0,
        "take_profit_price_2": 2090.0,
    }
    record.strategy_files_used = ["上涨通道分析识别.txt"]
    record.usage_total = {"total_tokens": 150}
    record.exception = None
    return record


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(analyze_router, prefix="/api")
    ctx = MagicMock()
    ctx.settings.general.last_symbol = "XAUUSD"
    ctx.settings.general.last_timeframe = "1h"
    ctx.data_source.latest_snapshot.return_value = []

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    return app


# ── 参数校验 ──────────────────────────────────────────────────────────────────


def test_analyze_bar_count_below_minimum_returns_422():
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/analyze/stream?bar_count=1")
    assert resp.status_code == 422


def test_analyze_bar_count_above_maximum_returns_422():
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/analyze/stream?bar_count=5001")
    assert resp.status_code == 422


# ── 事件序列 + done 载荷 ───────────────────────────────────────────────────────


def test_analyze_stream_emits_orchestrator_events_and_done():
    app = _make_app()
    record = _make_fake_record()

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        on_event(OrchestratorEvent.Stage1Started)
        on_event(OrchestratorEvent.Stage1Done)
        on_event(OrchestratorEvent.Stage2Started)
        on_event(OrchestratorEvent.Stage2Done)
        on_event(OrchestratorEvent.RecordSaved)
        return record

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
         patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/stream?bar_count=10")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types = [e for e, _ in events]

    # 应包含 orchestrator_event 与 done
    assert "orchestrator_event" in types
    orch_events = [d["event"] for e, d in events if e == "orchestrator_event"]
    assert "Stage1Started" in orch_events
    assert "Stage2Done" in orch_events
    assert "RecordSaved" in orch_events
    assert "done" in types

    # done 载荷含 decision_tree 与 decision_overlay
    done_data = next(d for e, d in events if e == "done")
    rec = done_data["record"]
    assert rec["symbol"] == "XAUUSD"
    assert rec["timeframe"] == "1h"

    dt = rec["decision_tree"]
    assert dt["gate_trace"] == [{"node_id": "G1", "answer": "是"}]
    assert dt["gate_result"] == "proceed"
    assert dt["decision_trace"] == [{"node_id": "10.3", "answer": "是"}]
    assert dt["terminal"] == {"node_id": "11.2", "outcome": "trade"}
    assert dt["gate_shortcircuited"] is False

    ov = rec["decision_overlay"]
    assert ov["order_type"] == "突破单"
    assert ov["order_direction"] == "做多"
    assert ov["chart_overlay_active"] is True
    assert ov["entry_price"] == 2010.5
    assert ov["stop_loss_price"] == 1995.0
    assert ov["take_profit_price"] == 2050.0
    assert ov["take_profit_price_2"] == 2090.0


def test_analyze_stream_includes_insufficient_data_event():
    """orchestrator 触发 InsufficientData 事件应在 SSE 中可见。"""
    app = _make_app()

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        on_event(OrchestratorEvent.InsufficientData)
        return _make_fake_record()

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
         patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/stream?bar_count=5")

    events = _parse_sse(resp.text)
    orch_events = [d["event"] for e, d in events if e == "orchestrator_event"]
    assert "InsufficientData" in orch_events


def test_analyze_stream_includes_cancelled_event():
    """orchestrator 触发 Cancelled 事件应在 SSE 中可见。"""
    app = _make_app()

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        on_event(OrchestratorEvent.Cancelled)
        return _make_fake_record()

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
         patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/stream?bar_count=5")

    events = _parse_sse(resp.text)
    orch_events = [d["event"] for e, d in events if e == "orchestrator_event"]
    assert "Cancelled" in orch_events


def test_analyze_stream_emits_retry_events():
    """Stage1Retry / Stage2Retry 事件应被透传到 SSE。"""
    app = _make_app()

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        on_event(OrchestratorEvent.Stage1Started)
        on_event(OrchestratorEvent.Stage1Retry)
        on_event(OrchestratorEvent.Stage1Done)
        on_event(OrchestratorEvent.Stage2Started)
        on_event(OrchestratorEvent.Stage2Retry)
        on_event(OrchestratorEvent.Stage2Done)
        return _make_fake_record()

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
         patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/stream?bar_count=5")

    events = _parse_sse(resp.text)
    orch_events = [d["event"] for e, d in events if e == "orchestrator_event"]
    assert "Stage1Retry" in orch_events
    assert "Stage2Retry" in orch_events


# ── 异常路径 ──────────────────────────────────────────────────────────────────


def test_analyze_stream_orchestrator_exception_returns_error():
    """orchestrator.submit 抛异常时 SSE 输出 error 事件。"""
    app = _make_app()

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = RuntimeError("模型超时")

    with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
         patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/stream?bar_count=5")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types = [e for e, _ in events]
    assert "error" in types
    err_data = next(d for e, d in events if e == "error")
    assert "模型超时" in err_data["message"]


def test_analyze_stream_failed_events_then_error():
    """Stage1Failed 事件透传，随后异常产生 error。"""
    app = _make_app()

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        on_event(OrchestratorEvent.Stage1Started)
        on_event(OrchestratorEvent.Stage1Failed)
        raise RuntimeError("stage1 crash")

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
         patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/stream?bar_count=5")

    events = _parse_sse(resp.text)
    orch_events = [d["event"] for e, d in events if e == "orchestrator_event"]
    types = [e for e, _ in events]
    assert "Stage1Failed" in orch_events
    assert "error" in types
