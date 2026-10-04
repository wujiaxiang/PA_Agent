"""Close the experience-library loop: resolve a plan's outcome and write it back.

The library had a reader but no writer, so entries never grew and no analysis
ever fed back into Stage 1/Stage 2 retrieval. This module watches a decided
plan on a daemon thread and, once TP1 or SL is reached, persists a
``success_cases``/``failure_cases`` entry via :mod:`experience_writer`.

Design notes
------------
* **Daemon thread + never raises into the analysis flow.** Mirrors
  ``order_followup``: any failure is logged as a warning at most.
* **Polls the live data source**, so it needs no new plumbing from the client.
* **Bounded wait** (``max_wait_s``). If neither level is reached in time the
  plan is discarded rather than written — an unresolved trade tells us nothing
  about whether the setup worked.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("pa_agent.web.experience_watcher")

#: How often to re-read bars while waiting for TP/SL resolution.
_POLL_INTERVAL_S = 15.0

#: Longest we watch one plan before giving up.
DEFAULT_MAX_WAIT_S = 24 * 3600.0


def _bar_dicts(bars: Any) -> list[dict]:
    """Normalise KlineBar objects / dicts into dicts carrying OHLC + ts_open."""
    out: list[dict] = []
    for b in bars or []:
        if isinstance(b, dict):
            row = b
        else:
            row = {
                "ts_open": getattr(b, "ts_open", 0),
                "high": getattr(b, "high", None),
                "low": getattr(b, "low", None),
                "close": getattr(b, "close", None),
            }
        try:
            out.append(
                {
                    "ts_open": int(row.get("ts_open") or 0),
                    "high": float(row.get("high")) if row.get("high") is not None else None,
                    "low": float(row.get("low")) if row.get("low") is not None else None,
                }
            )
        except (TypeError, ValueError):
            continue
    return out


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (None,)


def _run_experience_watch(
    *,
    data_source: Any,
    symbol: str,
    timeframe: str,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    is_long: bool,
    cycle_position: str,
    direction: str,
    detected_patterns: list[str],
    confidence: Any,
    summary: str,
    after_ts_open_ms: int,
    max_wait_s: float = DEFAULT_MAX_WAIT_S,
) -> None:
    """Poll *data_source* until TP/SL resolves, then write one experience entry."""
    try:
        from pa_agent.records.experience_writer import ExperienceWriter, evaluate_outcome

        writer = ExperienceWriter(logger=logger)
        deadline = time.monotonic() + max(1.0, float(max_wait_s))

        while time.monotonic() < deadline:
            try:
                snapshot = data_source.latest_snapshot(200)
            except Exception as exc:  # noqa: BLE001
                logger.warning("experience watch: snapshot failed: %s", exc)
                return

            bars = [
                b for b in _bar_dicts(snapshot)
                if b["ts_open"] and b["ts_open"] > int(after_ts_open_ms or 0)
            ]
            if bars:
                outcome = evaluate_outcome(
                    bars,
                    entry_price=float(entry_price),
                    take_profit_price=float(take_profit_price),
                    stop_loss_price=float(stop_loss_price),
                    is_long=is_long,
                )
                if outcome is not None:
                    result, pnl_pct = outcome
                    writer.save(
                        cycle_position=cycle_position or "trending_tr",
                        direction=direction,
                        detected_patterns=detected_patterns,
                        confidence=confidence or 0,
                        summary=summary,
                        symbol=symbol,
                        timeframe=timeframe,
                        entry_price=float(entry_price),
                        success=(result == "win"),
                        pnl_pct=pnl_pct,
                        extra={"stop_loss_price": stop_loss_price,
                               "take_profit_price": take_profit_price},
                    )
                    return
            time.sleep(_POLL_INTERVAL_S)

        logger.info(
            "experience watch: %s %s unresolved after %.0fs — not writing an entry",
            symbol, timeframe, max_wait_s,
        )
    except Exception as exc:  # noqa: BLE001
        # 绝不冒泡进分析主流程
        logger.warning("experience watch failed: %s", exc)


def spawn_experience_watch(
    *,
    data_source: Any,
    settings: Any,
    symbol: str,
    timeframe: str,
    stage1: dict,
    stage2_flat: dict,
    last_closed_ts_open_ms: int = 0,
) -> Optional[threading.Thread]:
    """Start a daemon watcher if the plan is resolvable and watching is enabled.

    Returns the started thread, or ``None`` when watching is disabled or the
    decision lacks the levels needed to resolve an outcome.
    """
    try:
        prompt_cfg = getattr(settings, "prompt", None)
        if prompt_cfg is None or not getattr(prompt_cfg, "experience_auto_write", True):
            return None

        order_type = str(stage2_flat.get("order_type") or "")
        if order_type in ("不下单", "no_order", ""):
            return None

        entry = stage2_flat.get("entry_price")
        tp = stage2_flat.get("take_profit_price")
        sl = stage2_flat.get("stop_loss_price")
        if not (_is_num(entry) and _is_num(tp) and _is_num(sl)):
            return None
        if float(tp) == float(entry) or float(sl) == float(entry):
            return None

        direction = str(stage2_flat.get("order_direction") or "")
        is_long = direction.strip().lower() in ("long", "做多", "buy")
        is_short = direction.strip().lower() in ("short", "做空", "sell")
        if not (is_long or is_short):
            return None

        max_wait = float(getattr(prompt_cfg, "experience_max_wait_s", DEFAULT_MAX_WAIT_S))
        thread = threading.Thread(
            target=_run_experience_watch,
            kwargs={
                "data_source": data_source,
                "symbol": symbol,
                "timeframe": timeframe,
                "entry_price": float(entry),
                "take_profit_price": float(tp),
                "stop_loss_price": float(sl),
                "is_long": is_long,
                "cycle_position": str(stage1.get("cycle_position") or "trending_tr"),
                "direction": direction,
                "detected_patterns": list(stage1.get("detected_patterns") or []),
                "confidence": stage1.get("diagnosis_confidence") or 0,
                "summary": str(stage2_flat.get("reasoning") or stage2_flat.get("diagnosis_summary") or ""),
                "after_ts_open_ms": int(last_closed_ts_open_ms or 0),
                "max_wait_s": max_wait,
            },
            name="experience-watch",
            daemon=True,
        )
        thread.start()
        logger.info("experience watch started: %s %s entry=%s", symbol, timeframe, entry)
        return thread
    except Exception as exc:  # noqa: BLE001
        logger.warning("spawn_experience_watch failed: %s", exc)
        return None