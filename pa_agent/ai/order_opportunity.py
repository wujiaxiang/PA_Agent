"""Detect stage-2 order opportunities and format alert text (Qt-free core).

This module is the single source of truth for "does stage-2 propose a real
order?".  It deliberately lives outside :mod:`pa_agent.gui` so that headless
callers — the Web backend (``web/api/routes_analyze.py``), the trade logger and
the notifiers — can reuse the exact same gate the desktop GUI uses, without
importing PyQt6.

The GUI-only presentation helpers (modal popup, alert sound) stay in
``pa_agent/gui/order_opportunity.py``, which re-exports everything from here.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: stage-2 ``order_type`` values that count as an actionable order.
ORDER_OPPORTUNITY_TYPES: frozenset[str] = frozenset({"限价单", "突破单", "市价单"})


def _parse_trade_confidence(decision: dict[str, Any]) -> int | None:
    """Extract trade_confidence as 0-100 int, or None if absent/invalid."""
    raw = decision.get("trade_confidence")
    if raw is None or raw == "":
        return None
    try:
        return max(0, min(100, int(float(str(raw).strip()))))
    except (ValueError, TypeError):
        return None


def has_order_opportunity(
    decision: dict[str, Any] | None,
    *,
    confidence_threshold: int | None = None,
) -> bool:
    """Return True when stage-2 decision proposes an actual order.

    When *confidence_threshold* is provided, the decision is only treated as
    an order opportunity when ``trade_confidence >= confidence_threshold``.
    """
    if not isinstance(decision, dict):
        return False
    if str(decision.get("order_type") or "") not in ORDER_OPPORTUNITY_TYPES:
        return False
    # Confidence gate: if threshold set, require trade_confidence >= threshold
    if confidence_threshold is not None and confidence_threshold > 0:
        conf = _parse_trade_confidence(decision)
        if conf is None or conf < confidence_threshold:
            return False
    return True


def _fmt_price(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def format_order_alert_message(decision: dict[str, Any]) -> str:
    """Short summary for the order-opportunity popup / push notification body."""
    direction = decision.get("order_direction") or "—"
    order_type = decision.get("order_type") or "—"
    entry = _fmt_price(decision.get("entry_price"))
    stop = _fmt_price(decision.get("stop_loss_price"))
    target = _fmt_price(decision.get("take_profit_price"))
    target2 = _fmt_price(decision.get("take_profit_price_2"))
    reasoning = str(decision.get("reasoning") or "").strip()
    lines = [
        f"方向：{direction}",
        f"方式：{order_type}",
        f"入场：{entry}",
        f"止损：{stop}",
        f"TP1：{target}",
        f"TP2：{target2}",
    ]
    if reasoning:
        preview = reasoning if len(reasoning) <= 200 else reasoning[:200] + "…"
        lines.append("")
        lines.append(preview)
    lines.append("")
    lines.append("已切换到「决策」页，请核对详情。")
    return "\n".join(lines)


def _windows_alert_wav_paths() -> list[str]:
    media = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media")
    names = (
        "notify.wav",
        "Windows Notify.wav",
        "Alarm01.wav",
        "Windows Exclamation.wav",
    )
    return [os.path.join(media, name) for name in names]