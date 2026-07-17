# -*- coding: utf-8 -*-
"""Unit tests for SSE event parsing — 验证 Web SSE 事件流格式与事件类型覆盖。

覆盖（SubTask 3.4 + 14.2）：
- SSE 文本格式解析（event:/data: 行）
- 全部 11 个 OrchestratorEvent 透传
- Retry / Failed / InsufficientData 事件可见
- done 事件载荷结构（含 decision_tree / decision_overlay）
- error 事件载荷结构
- heartbeat 事件

策略：构造 SSE 文本片段，验证解析器正确提取 event type 与 JSON data。
"""
from __future__ import annotations

import json

import pytest

from pa_agent.util.threading import OrchestratorEvent


def parse_sse(text: str) -> list[tuple[str, object]]:
    """与 test_web_routes_analyze.py / test_web_routes_chat.py 一致的 SSE 解析器。"""
    events: list[tuple[str, object]] = []
    cur_event: str | None = None
    cur_data: list[str] = []
    for line in text.split("\n"):
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


def make_sse(event: str, data: dict | str) -> str:
    """构造单条 SSE 文本。"""
    if isinstance(data, str):
        return f"event: {event}\ndata: {data}\n\n"
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── SSE 格式解析 ──────────────────────────────────────────────────────────────


class TestSSEParsing:
    def test_single_event(self):
        text = make_sse("done", {"type": "done", "content": "ok"})
        events = parse_sse(text)
        assert len(events) == 1
        assert events[0][0] == "done"
        assert events[0][1]["content"] == "ok"

    def test_multiple_events(self):
        text = make_sse("orchestrator_event", {"event": "Stage1Started"}) + \
               make_sse("reasoning_token", {"chunk": "..."}) + \
               make_sse("done", {"type": "done"})
        events = parse_sse(text)
        assert len(events) == 3
        assert events[0][0] == "orchestrator_event"
        assert events[1][0] == "reasoning_token"
        assert events[2][0] == "done"

    def test_heartbeat(self):
        text = make_sse("heartbeat", "{}")
        events = parse_sse(text)
        assert len(events) == 1
        assert events[0][0] == "heartbeat"

    def test_chinese_data_preserved(self):
        text = make_sse("error", {"message": "没有已完成的交易分析记录"})
        events = parse_sse(text)
        assert "没有已完成的交易分析记录" in events[0][1]["message"]

    def test_non_json_data_falls_back_to_string(self):
        text = "event: raw\ndata: plain text\n\n"
        events = parse_sse(text)
        assert events[0][0] == "raw"
        assert events[0][1] == "plain text"


# ── OrchestratorEvent 全覆盖 ───────────────────────────────────────────────────


class TestOrchestratorEvents:
    """验证全部 11 个 OrchestratorEvent 成员可被 SSE 透传。"""

    ALL_EVENTS = [
        OrchestratorEvent.Stage1Started,
        OrchestratorEvent.Stage1Retry,
        OrchestratorEvent.Stage1Done,
        OrchestratorEvent.Stage1Failed,
        OrchestratorEvent.Stage2Started,
        OrchestratorEvent.Stage2Retry,
        OrchestratorEvent.Stage2Done,
        OrchestratorEvent.Stage2Failed,
        OrchestratorEvent.RecordSaved,
        OrchestratorEvent.Cancelled,
        OrchestratorEvent.InsufficientData,
    ]

    def test_all_11_events_exist(self):
        assert len(self.ALL_EVENTS) == 11

    @pytest.mark.parametrize("ev", ALL_EVENTS)
    def test_event_name_transmittable(self, ev):
        """每个事件 .name 可作为 SSE event 字段值。"""
        sse_text = make_sse("orchestrator_event", {
            "type": "orchestrator_event",
            "event": ev.name,
        })
        events = parse_sse(sse_text)
        assert events[0][1]["event"] == ev.name

    def test_retry_events(self):
        """Stage1Retry / Stage2Retry 事件可见。"""
        text = make_sse("orchestrator_event", {"event": "Stage1Retry"}) + \
               make_sse("orchestrator_event", {"event": "Stage2Retry"})
        events = parse_sse(text)
        names = [e[1]["event"] for e in events]
        assert "Stage1Retry" in names
        assert "Stage2Retry" in names

    def test_failed_events(self):
        """Stage1Failed / Stage2Failed 事件可见。"""
        text = make_sse("orchestrator_event", {"event": "Stage1Failed"}) + \
               make_sse("orchestrator_event", {"event": "Stage2Failed"})
        events = parse_sse(text)
        names = [e[1]["event"] for e in events]
        assert "Stage1Failed" in names
        assert "Stage2Failed" in names

    def test_insufficient_data_event(self):
        text = make_sse("orchestrator_event", {"event": "InsufficientData"})
        events = parse_sse(text)
        assert events[0][1]["event"] == "InsufficientData"

    def test_cancelled_event(self):
        text = make_sse("orchestrator_event", {"event": "Cancelled"})
        events = parse_sse(text)
        assert events[0][1]["event"] == "Cancelled"

    def test_record_saved_event(self):
        text = make_sse("orchestrator_event", {"event": "RecordSaved"})
        events = parse_sse(text)
        assert events[0][1]["event"] == "RecordSaved"


# ── done 事件载荷 ─────────────────────────────────────────────────────────────


class TestDoneEventPayload:
    """done 事件应包含 record.decision_tree 与 record.decision_overlay。"""

    def test_done_contains_decision_tree(self):
        payload = {
            "type": "done",
            "record": {
                "symbol": "XAUUSD",
                "timeframe": "1h",
                "decision_tree": {
                    "gate_trace": [{"node_id": "G1"}],
                    "decision_trace": [{"node_id": "10.3"}],
                    "terminal": {"outcome": "trade"},
                    "gate_result": "proceed",
                    "gate_shortcircuited": False,
                },
            },
        }
        text = make_sse("done", payload)
        events = parse_sse(text)
        rec = events[0][1]["record"]
        assert "decision_tree" in rec
        dt = rec["decision_tree"]
        assert dt["gate_trace"] is not None
        assert dt["decision_trace"] is not None
        assert dt["terminal"]["outcome"] == "trade"

    def test_done_contains_decision_overlay(self):
        payload = {
            "type": "done",
            "record": {
                "decision_overlay": {
                    "order_type": "突破单",
                    "entry_price": 2010.0,
                    "stop_loss_price": 1995.0,
                    "take_profit_price": 2050.0,
                    "take_profit_price_2": 2090.0,
                    "chart_overlay_active": True,
                },
            },
        }
        text = make_sse("done", payload)
        events = parse_sse(text)
        ov = events[0][1]["record"]["decision_overlay"]
        assert ov["entry_price"] == 2010.0
        assert ov["chart_overlay_active"] is True

    def test_done_with_no_order_overlay(self):
        """no_order 场景下 overlay 仍存在但价格字段为 None。"""
        payload = {
            "type": "done",
            "record": {
                "decision_overlay": {
                    "order_type": "不下单",
                    "entry_price": None,
                    "stop_loss_price": None,
                    "chart_overlay_active": False,
                },
            },
        }
        text = make_sse("done", payload)
        events = parse_sse(text)
        ov = events[0][1]["record"]["decision_overlay"]
        assert ov["order_type"] == "不下单"
        assert ov["entry_price"] is None
        assert ov["chart_overlay_active"] is False


# ── error 事件 ────────────────────────────────────────────────────────────────


class TestErrorEvent:
    def test_error_event_structure(self):
        text = make_sse("error", {"type": "error", "message": "模型超时"})
        events = parse_sse(text)
        assert events[0][1]["type"] == "error"
        assert "模型超时" in events[0][1]["message"]

    def test_error_after_failed_events(self):
        """Failed 事件后跟 error 事件的序列。"""
        text = make_sse("orchestrator_event", {"event": "Stage2Failed"}) + \
               make_sse("error", {"type": "error", "message": "crash"})
        events = parse_sse(text)
        types = [e[0] for e in events]
        assert "orchestrator_event" in types
        assert "error" in types


# ── 事件流顺序 ────────────────────────────────────────────────────────────────


class TestEventSequence:
    def test_full_analysis_sequence(self):
        """完整分析事件流顺序：Stage1 → Stage2 → RecordSaved → done。"""
        seq = [
            ("orchestrator_event", {"event": "Stage1Started"}),
            ("reasoning_token", {"chunk": "思考"}),
            ("content_token", {"chunk": "内容"}),
            ("orchestrator_event", {"event": "Stage1Done"}),
            ("orchestrator_event", {"event": "Stage2Started"}),
            ("orchestrator_event", {"event": "Stage2Done"}),
            ("orchestrator_event", {"event": "RecordSaved"}),
            ("done", {"type": "done", "record": {}}),
        ]
        text = "".join(make_sse(e, d) for e, d in seq)
        events = parse_sse(text)
        assert len(events) == 8
        # done 必须是最后一个
        assert events[-1][0] == "done"
        # Stage1Started 必须在 Stage2Started 之前
        orch_names = [e[1]["event"] for e in events if e[0] == "orchestrator_event"]
        assert orch_names.index("Stage1Started") < orch_names.index("Stage2Started")
        assert orch_names.index("Stage2Done") < orch_names.index("RecordSaved")

    def test_retry_sequence(self):
        """重试序列：Started → Retry → Done。"""
        seq = [
            ("orchestrator_event", {"event": "Stage1Started"}),
            ("orchestrator_event", {"event": "Stage1Retry"}),
            ("orchestrator_event", {"event": "Stage1Done"}),
        ]
        text = "".join(make_sse(e, d) for e, d in seq)
        events = parse_sse(text)
        orch = [e[1]["event"] for e in events]
        assert orch == ["Stage1Started", "Stage1Retry", "Stage1Done"]
