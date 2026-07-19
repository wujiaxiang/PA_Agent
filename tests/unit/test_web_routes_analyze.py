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
    record.meta.last_close_bar_iso = ""
    record.stage1_messages = [
        {"role": "system", "content": "You are a stage1 diagnosis assistant."},
        {"role": "user", "content": "Analyze the latest 100 bars."},
    ]
    record.stage1_response = {
        "id": "resp-1",
        "content": "stage1 diagnosis JSON",
        "reasoning_content": "stage1 thinking step-by-step",
    }
    record.stage1_diagnosis = {
        "gate_trace": [{"node_id": "G1", "answer": "是"}],
        "gate_result": "proceed",
    }
    record.stage2_messages = [
        {"role": "system", "content": "You are a stage2 decision assistant."},
        {"role": "user", "content": "Make a trading decision."},
    ]
    record.stage2_response = {
        "id": "resp-2",
        "content": "stage2 decision JSON",
        "reasoning_content": "stage2 thinking step-by-step",
    }
    record.stage2_decision = {
        "decision_trace": [{"node_id": "10.3", "answer": "是"}],
        "terminal": {"node_id": "11.2", "outcome": "trade"},
        "gate_shortcircuited": False,
        "order_type": "突破单",
        "order_direction": "做多",
        "chart_overlay_active": True,
        "entry_price": 2010.5,
        "stop_loss_price": 1995.0,
        "take_profit_price": 2050.0,
        "take_profit_price_2": 2090.0,
    }
    record.strategy_files_used = ["上涨通道分析识别.txt"]
    record.experience_loaded = [
        {"filename": "success_2024_01_01.json", "case_type": "success"},
        {"filename": "failure_2024_01_02.json", "case_type": "failure"},
        {"filename": "success_2024_01_03.json", "case_type": "success"},
    ]
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

    # raw_debug_payload：包含 stage1/stage2 prompt + raw_response + validation + exception
    rdp = rec["raw_debug_payload"]
    assert rdp["stage1_system_prompt"] == "You are a stage1 diagnosis assistant."
    assert rdp["stage1_user_prompt"] == "Analyze the latest 100 bars."
    assert rdp["stage1_raw_response"]["reasoning_content"] == "stage1 thinking step-by-step"
    assert rdp["stage2_system_prompt"] == "You are a stage2 decision assistant."
    assert rdp["stage2_user_prompt"] == "Make a trading decision."
    assert rdp["stage2_raw_response"]["reasoning_content"] == "stage2 thinking step-by-step"
    val = rdp["validation"]
    assert val["stage1_valid"] is True
    assert val["stage2_valid"] is True
    assert val["stage1_missing_fields"] == []
    assert val["stage2_missing_fields"] == []
    assert val["stage1_invalid_fields"] == []
    assert val["stage2_invalid_fields"] == []
    assert rdp["exception"] is None

    # debug_files_payload：包含 stage1_files / stage2_files / experience_loaded / experience_count
    dfp = rec["debug_files_payload"]
    assert isinstance(dfp["stage1_files"], list)
    assert isinstance(dfp["stage2_files"], list)
    # 「上涨通道分析识别.txt」不属于 stage1 静态文件 → 归入 stage2
    assert "上涨通道分析识别.txt" in dfp["stage2_files"]
    assert dfp["experience_loaded"] == [
        "success_2024_01_01.json",
        "failure_2024_01_02.json",
        "success_2024_01_03.json",
    ]
    assert dfp["experience_count"] == {"success": 2, "failure": 1}


def test_analyze_stream_raw_debug_payload_with_exception():
    """exception 含 missing/invalid_fields 时，raw_debug_payload.validation 应反映出来。"""
    app = _make_app()
    record = _make_fake_record()
    # 模拟阶段一异常：exception 含 stage + missing_fields + invalid_fields
    record.exception = {
        "category": "d",
        "stage": "阶段一-诊断",
        "missing_fields": ["direction"],
        "invalid_fields": ["cycle_position"],
        "raw_text": "...",
    }
    # 同时把 stage1_diagnosis 置为 None（验证 valid=false）
    record.stage1_diagnosis = None

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        return record

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
         patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
        with TestClient(app) as c:
            resp = c.get("/api/analyze/stream?bar_count=5")

    events = _parse_sse(resp.text)
    done_data = next(d for e, d in events if e == "done")
    rdp = done_data["record"]["raw_debug_payload"]
    val = rdp["validation"]
    assert val["stage1_valid"] is False
    assert val["stage1_missing_fields"] == ["direction"]
    assert val["stage1_invalid_fields"] == ["cycle_position"]
    # 阶段二不受影响
    assert val["stage2_valid"] is True
    assert val["stage2_missing_fields"] == []
    assert rdp["exception"]["category"] == "d"


def test_analyze_stream_raw_debug_payload_api_key_sanitized():
    """api_key 出现在 stage1_user_prompt 中时，应被脱敏为 mask_secret 形式。"""
    app = _make_app()
    record = _make_fake_record()
    secret = "sk-test-1234567890abcdef"
    # 在 prompt 中嵌入 api_key，验证 _sanitize 递归替换
    record.stage1_messages = [
        {"role": "system", "content": f"auth token: {secret}"},
        {"role": "user", "content": "Analyze bars"},
    ]

    def fake_submit(frame, cancel_token=None, on_event=None, **kwargs):
        return record

    fake_orch = MagicMock()
    fake_orch.submit.side_effect = fake_submit

    with patch("web.api.routes_analyze.TwoStageOrchestrator", return_value=fake_orch), \
         patch("web.api.routes_analyze.build_display_frame", return_value=MagicMock()):
        with TestClient(app) as c:
            # 在 app startup 完成后修改 ctx.settings.provider.api_key
            c.app.state.ctx.settings.provider.api_key = secret
            resp = c.get("/api/analyze/stream?bar_count=5")

    events = _parse_sse(resp.text)
    done_data = next(d for e, d in events if e == "done")
    rdp = done_data["record"]["raw_debug_payload"]
    # api_key 应被替换为 mask_secret 形式：保留最后 4 字符，其余替换为 '*'
    # 原 prompt 为 "auth token: sk-test-1234567890abcdef"
    assert secret not in rdp["stage1_system_prompt"]
    # mask_secret 保留最后 4 字符
    assert rdp["stage1_system_prompt"].endswith("cdef")
    # mask_secret 把中间字符替换为 '*'，所以应至少出现 4 个连续 '*'
    assert "****" in rdp["stage1_system_prompt"]


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
