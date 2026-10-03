"""Unit tests for the Web backend's post-order follow-up (trade log + push).

These must stay importable **without PyQt6** — the Docker image ships no Qt and
``pa_agent/gui/__init__.py`` pulls in ChartWidget, which is exactly why the
shared gate now lives in ``pa_agent.ai.order_opportunity``.
"""
from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

import pytest

from pa_agent.ai.order_opportunity import has_order_opportunity
from web.api.order_followup import _flat_stage2, should_send_order_signal, spawn_post_order_followup


def _record(order_type="限价单", confidence=72):
    return SimpleNamespace(
        stage2_decision={
            "decision": {
                "order_type": order_type,
                "order_direction": "long",
                "entry_price": 65000.0,
                "stop_loss_price": 63050.0,
                "take_profit_price": 68900.0,
                "trade_confidence": confidence,
            },
            # trade_confidence is stored at the TOP level of stage2_decision,
            # while order_type lives in .decision — the gate must see both.
            "trade_confidence": confidence,
            "diagnosis_summary": "中期上升趋势",
            "decision_trace": [],
            "terminal": {},
            "next_cycle_prediction": {},
            "bar_analysis": {},
        },
        stage1_diagnosis={"trend_direction": "上升", "bar_analysis": {"signal_bar": {}, "entry_bar": {}}},
    )


def _settings(**over):
    general = SimpleNamespace(
        alert_on_order_opportunity=True,
        decision_confidence_threshold=40,
        structure_flip_cooldown_bars=3,
        decision_stance="balanced",
    )
    for k, v in over.items():
        setattr(general, k, v)
    return SimpleNamespace(general=general, provider=SimpleNamespace(model="test-model"))


# ── flattening ────────────────────────────────────────────────────────────

def test_flat_stage2_merges_inner_decision_without_overwriting():
    flat = _flat_stage2(_record())
    assert flat["order_type"] == "限价单"
    assert flat["entry_price"] == 65000.0
    # top-level trade_confidence wins over the inner copy
    assert flat["trade_confidence"] == 72
    # original nested reference is preserved for other consumers
    assert flat["decision"]["order_type"] == "限价单"


def test_flat_stage2_handles_missing_stage2():
    assert _flat_stage2(SimpleNamespace(stage2_decision=None)) == {}
    assert _flat_stage2(SimpleNamespace()) == {}


# ── the gate ──────────────────────────────────────────────────────────────

def test_gate_true_for_order_above_threshold():
    assert should_send_order_signal(_record(), confidence_threshold=40) is True


def test_gate_false_below_confidence_threshold():
    assert should_send_order_signal(_record(confidence=10), confidence_threshold=40) is False


def test_gate_false_for_non_order_type():
    assert should_send_order_signal(_record(order_type="观望"), confidence_threshold=40) is False


def test_gate_false_when_alert_switch_off():
    assert should_send_order_signal(_record(), alert_enabled=False, confidence_threshold=40) is False


def test_gate_matches_shared_ai_helper():
    """The Web gate and the GUI gate must agree (same function, same answer)."""
    rec = _record()
    assert should_send_order_signal(rec, confidence_threshold=40) == has_order_opportunity(
        _flat_stage2(rec), confidence_threshold=40
    )


# ── dispatch ──────────────────────────────────────────────────────────────

def test_spawn_returns_false_when_settings_missing():
    assert spawn_post_order_followup(
        record=_record(), frame=None, settings=None, symbol="BTCUSDT", timeframe="15m"
    ) is False


def test_spawn_returns_false_when_gate_rejects():
    assert spawn_post_order_followup(
        record=_record(order_type="观望"),
        frame=None,
        settings=_settings(),
        symbol="BTCUSDT",
        timeframe="15m",
    ) is False


def test_spawn_writes_trade_record(monkeypatch, tmp_path):
    """End-to-end: a qualifying decision appends a trade record row."""
    import pa_agent.records.trade_logger as tl
    import web.api.order_followup as of

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    sent: list[str] = []

    # The notifier modules are imported lazily inside the follow-up thread,
    # so they must be injected into sys.modules rather than patched as attrs.
    class _FakeFeishu:
        @staticmethod
        def send_order_signal(**kwargs):
            sent.append("feishu")

    class _FakePushplus:
        @staticmethod
        def pushplus_is_active(settings):
            return False

    monkeypatch.setitem(sys.modules, "pa_agent.notify.feishu_notifier", _FakeFeishu)
    monkeypatch.setitem(sys.modules, "pa_agent.notify.pushplus_notifier", _FakePushplus)

    assert of.spawn_post_order_followup(
        record=_record(),
        frame=None,
        settings=_settings(),
        symbol="BTCUSDT",
        timeframe="15m",
    ) is True

    _join_followup_threads()

    csv = tmp_path / "BTCUSDT_15m.csv"
    assert csv.exists(), "trade record CSV should be written"
    body = csv.read_text(encoding="utf-8-sig")
    assert "限价单" in body
    assert "BTCUSDT" in body
    assert sent == ["feishu"], "Feishu push should be attempted"


def test_spawn_survives_notifier_failure(monkeypatch, tmp_path):
    """A broken webhook must never propagate into the analysis pipeline."""
    import pa_agent.records.trade_logger as tl
    import web.api.order_followup as of

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)

    class _Boom:
        @staticmethod
        def send_order_signal(**kwargs):
            raise RuntimeError("webhook 500")

    class _FakePushplus:
        @staticmethod
        def pushplus_is_active(settings):
            return False

    monkeypatch.setitem(sys.modules, "pa_agent.notify.feishu_notifier", _Boom)
    monkeypatch.setitem(sys.modules, "pa_agent.notify.pushplus_notifier", _FakePushplus)

    assert of.spawn_post_order_followup(
        record=_record(),
        frame=None,
        settings=_settings(),
        symbol="BTCUSDT",
        timeframe="15m",
    ) is True
    _join_followup_threads()

    assert (tmp_path / "BTCUSDT_15m.csv").exists()


def test_pushplus_used_when_active(monkeypatch, tmp_path):
    import pa_agent.records.trade_logger as tl
    import web.api.order_followup as of

    monkeypatch.setattr(tl, "_TRADE_RECORDS_DIR", tmp_path)
    sent: list[str] = []

    class _FakeFeishu:
        @staticmethod
        def send_order_signal(**kwargs):
            sent.append("feishu")

    class _ActivePushplus:
        @staticmethod
        def pushplus_is_active(settings):
            return True

        @staticmethod
        def send_order_signal(**kwargs):
            sent.append("pushplus")

    monkeypatch.setitem(sys.modules, "pa_agent.notify.feishu_notifier", _FakeFeishu)
    monkeypatch.setitem(sys.modules, "pa_agent.notify.pushplus_notifier", _ActivePushplus)

    assert of.spawn_post_order_followup(
        record=_record(),
        frame=None,
        settings=_settings(),
        symbol="BTCUSDT",
        timeframe="15m",
    ) is True
    _join_followup_threads()

    assert sent == ["feishu", "pushplus"]


def test_shared_gate_module_has_no_qt_dependency():
    """The shared gate must import cleanly in the Qt-free Docker image.

    Checks real import statements via AST (the docstring legitimately mentions
    PyQt6 in prose, which a naive substring check would trip over).
    """
    import ast
    import inspect

    import pa_agent.ai.order_opportunity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "PyQt6" not in imported
    assert "pyqtgraph" not in imported


def test_gui_shim_reexports_shared_gate():
    """GUI callers keep working after the extraction."""
    from pa_agent.gui.order_opportunity import (
        ORDER_OPPORTUNITY_TYPES,
        format_order_alert_message,
        has_order_opportunity,
    )

    assert ORDER_OPPORTUNITY_TYPES == frozenset({"限价单", "突破单", "市价单"})
    assert has_order_opportunity({"order_type": "限价单"}) is True
    assert "入场" in format_order_alert_message({"order_type": "限价单"})


def _join_followup_threads():
    import threading

    for thread in [t for t in threading.enumerate() if t.name == "post-order-followup"]:
        thread.join(timeout=5)