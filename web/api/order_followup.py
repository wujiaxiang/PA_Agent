"""Post-order follow-up for the Web backend: trade log + push notifications.

Mirrors the desktop GUI's ``MainWindow._spawn_post_order_followup`` so a
Docker/always-on deployment gets the same behaviour the PyQt app had:

1. append the trade to ``trade_records/<symbol>_<tf>.csv`` (+ chart PNG), and
2. push an order signal to Feishu / PushPlus.

Why this lives here: before this module the *only* callers of
``pa_agent.records.trade_logger.save_trade_record`` and
``pa_agent.notify.*.send_order_signal`` in the whole repo were inside
``pa_agent/gui/main_window.py``.  The Web backend therefore shipped a Feishu /
PushPlus configuration UI and a working "test send" button while never sending
anything itself — a server-side deployment has no browser tab to rely on, so the
alerting was effectively dead.

Everything here is best-effort: it runs on a daemon thread and never raises
into the analysis pipeline.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from pa_agent.ai.order_opportunity import has_order_opportunity

logger = logging.getLogger(__name__)

_THREAD_NAME = "post-order-followup"


def _flat_stage2(record: Any) -> dict[str, Any]:
    """Return stage-2 flattened so ``order_type``/``trade_confidence`` sit at top level.

    The stored shape is ``{"decision": {...}, "diagnosis_summary": ...}`` while
    every consumer (chart overlay, trade logger, notifiers, the order gate)
    expects the flat fields.
    """
    s2 = getattr(record, "stage2_decision", None)
    if not isinstance(s2, dict):
        return {}
    flat = dict(s2)
    inner = s2.get("decision")
    if isinstance(inner, dict):
        for k, v in inner.items():
            flat.setdefault(k, v)
    return flat


def should_send_order_signal(
    record: Any,
    *,
    alert_enabled: bool = True,
    confidence_threshold: int | None = None,
) -> bool:
    """Master gate for trade-log persistence and push notifications.

    Mirrors the GUI: ``alert_on_order_opportunity`` is the master switch and
    ``has_order_opportunity`` additionally requires a real order type plus an
    optional confidence floor.
    """
    if not alert_enabled:
        return False
    return has_order_opportunity(_flat_stage2(record), confidence_threshold=confidence_threshold)


def _run_followup(
    *,
    record: Any,
    frame: Any,
    settings: Any,
    symbol: str,
    timeframe: str,
) -> None:
    """Body of the follow-up thread; every step is individually guarded."""
    stage2_flat = _flat_stage2(record)
    stage2_full = getattr(record, "stage2_decision", None) or {}
    stage1_diag = getattr(record, "stage1_diagnosis", None) or None

    model_name = getattr(getattr(settings, "provider", None), "model", "") or ""
    decision_stance = getattr(getattr(settings, "general", None), "decision_stance", "") or ""
    try:
        cooldown = int(getattr(settings.general, "structure_flip_cooldown_bars", 3) or 3)
    except (AttributeError, TypeError, ValueError):
        cooldown = 3

    # ── 1. trade record (CSV + chart PNG) ───────────────────────────────────
    try:
        from pa_agent.records.trade_logger import save_trade_record

        save_trade_record(
            decision_inner=stage2_flat,
            stage2_full=stage2_full,
            stage1_diagnosis=stage1_diag,
            frame=frame,
            meta_symbol=symbol,
            meta_timeframe=timeframe,
            decision_stance=decision_stance,
            model_name=model_name,
            structure_flip_cooldown_bars=cooldown,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trade record logging failed: %s", exc)

    # ── 2. push notifications ──────────────────────────────────────────────
    try:
        from pa_agent.notify.feishu_notifier import send_order_signal as send_feishu_order
        from pa_agent.notify.pushplus_notifier import pushplus_is_active
        from pa_agent.records.trade_logger import _TRADE_RECORDS_DIR

        safe_sym = symbol.replace("/", "-").replace("\\", "-")
        safe_tf = timeframe.replace("/", "-")
        candidates = sorted(
            _TRADE_RECORDS_DIR.glob(f"{safe_sym}_{safe_tf}_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        latest_img = candidates[0] if candidates else None

        send_feishu_order(
            decision_inner=stage2_flat,
            stage2_full=stage2_full,
            symbol=symbol,
            timeframe=timeframe,
            chart_image_path=latest_img,
            settings=settings,
        )

        if pushplus_is_active(settings):
            from pa_agent.notify.pushplus_notifier import send_order_signal as send_pushplus_order

            send_pushplus_order(
                decision_inner=stage2_flat,
                stage2_full=stage2_full,
                symbol=symbol,
                timeframe=timeframe,
                settings=settings,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("下单信号通知失败（不影响主流程）: %s", exc)


def spawn_post_order_followup(
    *,
    record: Any,
    frame: Any,
    settings: Any,
    symbol: str,
    timeframe: str,
    data_source: Any = None,
    user_id: str = "",
) -> bool:
    """Spawn the trade-log + notification follow-up on a daemon thread.

    Returns True when a thread was started. Never raises.

    ``user_id`` 必须在**派发时**（请求线程里）取好传进来：下面的
    ``save_pending_if_resolvable`` 是同步执行的，理论上还能问 ``request``，
    但线程一旦起来就没有请求上下文了，经验记录的归属就成了猜的。
    """
    if settings is None:
        return False
    alert_enabled = bool(getattr(settings.general, "alert_on_order_opportunity", True))
    threshold = getattr(settings.general, "decision_confidence_threshold", None)
    try:
        threshold_int = int(threshold) if threshold is not None else None
    except (TypeError, ValueError):
        threshold_int = None

    try:
        if not should_send_order_signal(
            record,
            alert_enabled=alert_enabled,
            confidence_threshold=threshold_int,
        ):
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("order opportunity gate failed: %s", exc)
        return False

    # ── 经验库阶段一：入场瞬间写 pending ──────────────────────────────────
    # 「我们做了什么」是事实，不需要等结果；终态由 experience_verifier /
    # experience_scheduler 按 N 根 K 线另行判定。与通知线程同样：
    # 失败只记 warning，绝不冒泡进分析主流程。
    try:
        from pa_agent.records.experience_writer import (
            ExperienceWriter,
            save_pending_if_resolvable,
        )

        bars = list(getattr(frame, "bars", None) or [])
        # 入场锚点必须是最后一根**已收盘** bar：bars[0] 是 forming bar，
        # 用它会把尚未收盘的走势算进「入场之后」。
        closed = [b for b in bars if getattr(b, "closed", False)]
        anchor_bar = closed[0] if closed else (bars[1] if len(bars) > 1 else None)
        anchor = int(getattr(anchor_bar, "ts_open", 0)) if anchor_bar is not None else 0

        staged = save_pending_if_resolvable(
            writer=ExperienceWriter(logger=logger),
            settings=settings,
            exchange=str(getattr(settings.general, "last_tradingview_exchange", "") or ""),
            symbol=symbol,
            timeframe=timeframe,
            stage1=dict(getattr(record, "stage1_diagnosis", None) or {}),
            stage2_flat=_flat_stage2(record),
            entry_ts_open_ms=anchor,
            bars=bars,
            user_id=user_id,
        )
        if staged is not None:
            logger.info("experience stage-1 pending written: %s %s -> %s",
                        symbol, timeframe, staged.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience stage-1 failed: %s", exc)

    try:
        threading.Thread(
            target=_run_followup,
            kwargs={
                "record": record,
                "frame": frame,
                "settings": settings,
                "symbol": symbol,
                "timeframe": timeframe,
            },
            name=_THREAD_NAME,
            daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to spawn %s: %s", _THREAD_NAME, exc)
        return False
    return True