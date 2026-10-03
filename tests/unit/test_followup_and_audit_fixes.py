"""Tests for the follow-up chat anchoring/sessioning and other audit fixes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from web.api import routes_chat


def _ctx(symbol="BTCUSDT", timeframe="15m"):
    return SimpleNamespace(
        settings=SimpleNamespace(
            general=SimpleNamespace(
                last_symbol=symbol, last_timeframe=timeframe, last_tradingview_exchange="GATEIO"
            )
        )
    )


def _record(symbol="BTCUSDT", timeframe="15m"):
    return SimpleNamespace(meta=SimpleNamespace(symbol=symbol, timeframe=timeframe))


# ── follow-up anchoring ───────────────────────────────────────────────────────


def test_record_matching_subscription():
    ctx = _ctx("BTCUSDT", "15m")
    assert routes_chat._record_matches_subscription(_record("BTCUSDT", "15m"), ctx) is True


def test_record_from_other_symbol_is_rejected():
    """A follow-up after switching instruments must not answer about the old one."""
    ctx = _ctx("ETHUSDT", "1h")
    assert routes_chat._record_matches_subscription(_record("BTCUSDT", "15m"), ctx) is False


def test_record_from_other_timeframe_is_rejected():
    ctx = _ctx("BTCUSDT", "1h")
    assert routes_chat._record_matches_subscription(_record("BTCUSDT", "15m"), ctx) is False


def test_none_record_never_matches():
    assert routes_chat._record_matches_subscription(None, _ctx()) is False


def test_missing_settings_is_permissive():
    """No settings to compare against — keep prior behaviour rather than blocking."""
    assert routes_chat._record_matches_subscription(_record(), SimpleNamespace(settings=None)) is True


# ── session locking ──────────────────────────────────────────────────────────


def test_touch_session_registers_a_lock():
    import threading

    routes_chat._chat_sessions.clear()
    lock = threading.Lock()
    sentinel = object()
    routes_chat._touch_session("k", sentinel, lock)  # type: ignore[arg-type]
    entry = routes_chat._chat_sessions["k"]
    assert entry["lock"] is lock
    assert routes_chat._get_session("k") is sentinel
    routes_chat._chat_sessions.clear()


def test_get_session_missing_key():
    routes_chat._chat_sessions.clear()
    assert routes_chat._get_session("nope") is None


# ── record cache signature ────────────────────────────────────────────────────


def test_scan_signature_detects_nested_write(tmp_path):
    """Root mtime does not change when a record lands in a leaf partition."""
    from pa_agent.records.analysis_history import _scan_signature

    partition = tmp_path / "GATEIO" / "BTCUSDT" / "15m"
    partition.mkdir(parents=True)
    before = _scan_signature(tmp_path, "GATEIO", "BTCUSDT", "15m")

    import os
    import time

    time.sleep(0.02)
    (partition / "2026-10-03_20-00-00_000_abc123.json").write_text("{}")
    after = _scan_signature(tmp_path, "GATEIO", "BTCUSDT", "15m")

    root_before = dict(before)["%s" % tmp_path]
    root_after = dict(after)["%s" % tmp_path]
    assert root_before == root_after, "root mtime must NOT change (that's the old bug)"
    assert before != after, "signature must change so the cache invalidates"


def test_load_record_strips_partial_reason(tmp_path):
    """save_partial injects _partial_reason; schema forbids extras so it must be popped."""
    import json

    from pa_agent.records.analysis_history import load_record
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    meta = RecordMeta(
        timestamp_local_iso="2026-05-18T14:00:00+08:00",
        timestamp_local_ms=1_700_000_000_000,
        symbol="XAUUSD",
        timeframe="1h",
        bar_count=100,
        ai_provider={"model": "x"},
    )
    rec = AnalysisRecord(
        meta=meta,
        kline_data=[],
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis=None,
        stage2_messages=[],
        stage2_response=None,
        stage2_decision=None,
        strategy_files_used=[],
        experience_loaded=[],
        exception={"type": "x", "stage": "y", "message": "z"},
        usage_total={},
    )
    payload = rec.model_dump()
    payload["_partial_reason"] = "stage1_failed"

    p = tmp_path / "rec.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_record(p)
    assert loaded is not None, "partial records must remain loadable"
    assert loaded.meta.symbol == "XAUUSD"


# ── demo payload contract ─────────────────────────────────────────────────────


def test_demo_payload_matches_renderer_contract():
    """Demo must satisfy the same shape _serialize_record produces."""
    from web.api.routes_demo import _build_demo_record

    d = _build_demo_record()
    s2 = d["stage2_decision"]
    s1 = d["stage1_diagnosis"]

    # decision fields flattened to the top level (app.js renderDecision)
    assert s2["order_type"] == "限价单"
    assert s2["order_direction"] == "做多"
    assert isinstance(s2.get("entry_price"), (int, float))
    assert isinstance(s2.get("trade_confidence"), (int, float))

    # stage1 vitals read from the top level, not market_structure.*
    assert s1.get("direction")
    assert s1.get("cycle_position")

    # decision_tree.terminal must be an object with .outcome
    terminal = (d.get("decision_tree") or {}).get("terminal")
    assert isinstance(terminal, dict) and terminal.get("outcome")

    # forecast chips read probabilities maps
    assert isinstance(s2["next_bar_prediction"]["probabilities"], dict)
    assert isinstance(s2["next_cycle_prediction"]["probabilities"], dict)

    # close-bar + timestamp live at the top level
    assert d.get("last_close_bar_iso")
    assert d.get("timestamp_local_iso")
    assert d.get("meta", {}).get("symbol")


# ── health probe bounding ─────────────────────────────────────────────────────


def test_health_probe_caps_tokens_and_timeout():
    """The unauthenticated health endpoint must not price a full completion."""
    import inspect

    from pa_agent.util.startup_health_check import check_model_api

    src = inspect.getsource(check_model_api)
    assert "max_tokens=1" in src, "health probe must cap max_tokens"
    assert "timeout_s=10.0" in src, "health probe must bound the timeout"


def test_chat_accepts_max_tokens_override():
    import inspect

    from pa_agent.ai.deepseek_client import DeepSeekClient

    sig = inspect.signature(DeepSeekClient.chat)
    assert "max_tokens" in sig.parameters