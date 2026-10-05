"""Stage 2 of the two-stage experience library: resolve pending records.

Stage 1 (``ExperienceWriter.save_pending``) records a plan the moment the
signal fires. This module decides when that plan is *done*.

Why it cannot just poll the shared data source
----------------------------------------------
``ctx.data_source`` is a **single subscription-bound instance**: ``subscribe()``
rewrites ``self._symbol`` / ``self._timeframe``. A pending record can live for
hours and be verified long after the user navigated to another instrument.
Reusing the shared source would resolve the record against a *different*
symbol's price action — silently fabricating an outcome. (Reproduced: a
BTCUSDT plan, entry 100 / TP 120 / SL 80, evaluated against ETHUSDT bars,
settled as ``('loss', -20.0)``.)

So alignment is the hard invariant here:

* A shared source is reused **only** when ``(exchange, symbol, timeframe)``
  matches the record on all three axes.
* Otherwise a dedicated source is created for that record alone.
* Every fetched bar is sanity-checked against the record's ``entry_price``
  before evaluation — a magnitude mismatch means the bars are not this
  instrument, so the record is left pending rather than wrongly settled.

N-bar rule
----------
``experience_verify_bars`` (N) subsequent bars decide the terminal state:

* TP/SL touched        → ``win`` / ``loss`` with pnl
* N bars, neither hit   → ``unresolved`` (terminal, but no verdict)
* fewer than N bars     → stays ``pending``, keeps waiting

Only ``win`` / ``loss`` are reachable by :class:`ExperienceReader`, so an
undecided setup never gets fed back into the prompts as if it were a loss.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from pa_agent.records.experience_writer import (
    STATUS_LOSS,
    STATUS_PENDING,
    STATUS_UNRESOLVED,
    STATUS_WIN,
    ExperienceWriter,
    evaluate_outcome,
)

logger = logging.getLogger("pa_agent.web.experience_verifier")

#: Fetch at least this many bars so the N-bar window is covered even when the
#: pending record is older than the current chart's history.
_FETCH_BARS = 300

#: Guard rail: if the fetched bars sit more than this ratio away from the
#: record's entry price, they cannot be the same instrument.
_PRICE_SANITY_RATIO = 8.0

#: How many records one verification pass may settle.
_DEFAULT_BATCH = 5


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
        return float(v)
    return None


def _bar_rows(bars: Any) -> list[dict[str, Any]]:
    """Normalise bars to dicts carrying ts_open + OHLC."""
    out: list[dict[str, Any]] = []
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
        ts = row.get("ts_open")
        hi = _num(row.get("high"))
        lo = _num(row.get("low"))
        try:
            ts = int(ts or 0)
        except (TypeError, ValueError):
            continue
        if not ts or hi is None or lo is None:
            continue
        out.append({"ts_open": ts, "high": hi, "low": lo,
                    "close": _num(row.get("close"))})
    return out


def bars_belong_to_instrument(bars: list[dict[str, Any]], entry_price: float) -> bool:
    """Cheap guard against evaluating a record against another instrument.

    A wrong instrument's bars almost never straddle the record's entry price.
    Without this, the subscription-drift bug would sail straight through.
    """
    if not bars or entry_price <= 0:
        return False
    lo = min(b["low"] for b in bars)
    hi = max(b["high"] for b in bars)
    if lo <= entry_price <= hi:
        return True
    nearest = lo if entry_price < lo else hi
    return nearest > 0 and (entry_price / nearest if entry_price < lo
                            else nearest / entry_price) >= 1.0 / _PRICE_SANITY_RATIO


def _bars_after(anchor_ms: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bars strictly after the entry, oldest-first — the only ones that count."""
    return sorted(
        (r for r in rows if r["ts_open"] > int(anchor_ms or 0)),
        key=lambda r: r["ts_open"],
    )


class _DedicatedSource:
    """Fetch bars for exactly one (exchange, symbol, timeframe), then die.

    Used when the shared source is on a different subscription. Constructing a
    ``TradingViewSource`` is expensive, so instances are never pooled — a
    short-lived object is the only way to guarantee we never mutate the shared
    subscription while another caller is reading it.
    """

    def __init__(self, factory: Callable[[], Any], symbol: str, timeframe: str,
                 exchange: str, logger_: logging.Logger):
        self._factory = factory
        self._symbol = symbol
        self._timeframe = timeframe
        self._exchange = exchange
        self._log = logger_

    def fetch(self) -> list[dict[str, Any]]:
        src = None
        try:
            src = self._factory()
            src.connect()
            src.subscribe(symbol=self._symbol, exchange=self._exchange or "",
                          timeframe=self._timeframe)
            return _bar_rows(src.latest_snapshot(_FETCH_BARS))
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "experience verify: dedicated fetch failed for %s/%s/%s: %s",
                self._exchange, self._symbol, self._timeframe, exc,
            )
            return []
        finally:
            if src is not None:
                try:
                    src.disconnect()
                except Exception:  # noqa: BLE001
                    pass


def _shared_fetch(shared: Any, rec_symbol: str, rec_timeframe: str,
                  rec_exchange: str) -> list[dict[str, Any]]:
    """Reuse the shared source only on a **three-axis** match."""
    cur_symbol = str(getattr(shared, "_symbol", "") or "").strip()
    cur_timeframe = str(getattr(shared, "_timeframe", "") or "").strip()
    cur_exchange = str(getattr(shared, "_exchange", "") or "").strip().upper()
    if cur_symbol != rec_symbol or cur_timeframe != rec_timeframe:
        return []
    if rec_exchange and cur_exchange and cur_exchange != rec_exchange.upper():
        return []
    try:
        return _bar_rows(shared.latest_snapshot(_FETCH_BARS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience verify: shared snapshot failed: %s", exc)
        return []


def settle_record(
    writer: ExperienceWriter,
    entry_id: str,
    content: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    verify_bars: int,
) -> tuple[str, int]:
    """Apply the N-bar rule to one record. Returns ``(status, bars_seen)``.

    * ``(win|loss, n)``     — a level was touched
    * ``(unresolved, n)``   — N bars elapsed without a touch (terminal)
    * ``(pending, n)``      — fewer than N bars so far; keep waiting
    """
    after = _bars_after(int(content.get("entry_ts_open_ms") or 0), bars)
    seen = len(after)

    outcome = evaluate_outcome(
        after,
        entry_price=float(content.get("entry_price") or 0.0),
        take_profit_price=float(content.get("take_profit_price") or 0.0),
        stop_loss_price=float(content.get("stop_loss_price") or 0.0),
        is_long=bool(content.get("is_long", True)),
    )
    if outcome is not None:
        result, pnl = outcome
        status = STATUS_WIN if result == "win" else STATUS_LOSS
        writer.finalize(entry_id, status=status, pnl_pct=pnl, bars_seen=seen)
        return status, seen

    if seen >= max(1, int(verify_bars)):
        writer.finalize(entry_id, status=STATUS_UNRESOLVED, bars_seen=seen)
        return STATUS_UNRESOLVED, seen

    writer.update_pending_progress(entry_id, bars_seen=seen)
    return STATUS_PENDING, seen


def verify_pending(
    *,
    shared_source: Any = None,
    source_factory: Callable[[], Any] | None = None,
    settings: Any = None,
    verify_bars: int | None = None,
    batch: int | None = None,
    scope: tuple[str, str] | None = None,
    writer: ExperienceWriter | None = None,
) -> dict[str, Any]:
    """Settle as many pending records as the N-bar rule allows.

    Parameters
    ----------
    shared_source:
        The app's data source. Reused only when its subscription matches the
        record on exchange + symbol + timeframe.
    source_factory:
        Zero-arg callable building a fresh data source, used when the shared
        one is on a different subscription. Without it, non-matching records
        are simply left pending (they will be settled once the user switches
        back, or when a factory is supplied).
    scope:
        Optional ``(symbol, timeframe)`` — settle only records for this
        instrument. The UI passes the currently-viewed K-line so the library
        panel always reflects what is on screen.

    Returns a summary dict; never raises.
    """
    summary = {"checked": 0, "win": 0, "loss": 0, "unresolved": 0,
               "pending": 0, "skipped_no_data": 0, "skipped_misaligned": 0}
    w = writer or ExperienceWriter(logger=logger)
    try:
        n_bars = int(verify_bars if verify_bars is not None
                     else getattr(getattr(settings, "prompt", None),
                                  "experience_verify_bars", 20))
        limit = int(batch if batch is not None
                    else getattr(getattr(settings, "prompt", None),
                                 "experience_verify_batch", _DEFAULT_BATCH))
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience verify: bad settings: %s", exc)
        return summary

    try:
        pending = w.list_pending(limit=max(limit * 5, 50))
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience verify: listing failed: %s", exc)
        return summary

    for entry_id, content in pending:
        if summary["checked"] >= limit:
            break
        symbol = str(content.get("symbol") or "")
        timeframe = str(content.get("timeframe") or "")
        exchange = str(content.get("exchange") or "")
        if scope and (symbol != scope[0] or timeframe != scope[1]):
            continue

        bars = _shared_fetch(shared_source, symbol, timeframe, exchange) \
            if shared_source is not None else []
        if not bars:
            if source_factory is None:
                summary["skipped_no_data"] += 1
                continue
            bars = _DedicatedSource(source_factory, symbol, timeframe,
                                    exchange, logger).fetch()
        if not bars:
            summary["skipped_no_data"] += 1
            continue

        # 价格量级兜底：bars 与本记录价格不在同一量级 → 一定不是这个标的
        if not bars_belong_to_instrument(bars, float(content.get("entry_price") or 0.0)):
            logger.warning(
                "experience verify: bars for %s/%s do not straddle entry %s — "
                "leaving record pending (possible instrument mismatch)",
                exchange, symbol, content.get("entry_price"),
            )
            summary["skipped_misaligned"] += 1
            continue

        summary["checked"] += 1
        try:
            status, _seen = settle_record(w, entry_id, content, bars,
                                          verify_bars=n_bars)
        except Exception as exc:  # noqa: BLE001
            logger.warning("experience verify: settle failed for %s: %s",
                           entry_id, exc)
            continue
        summary[status if status in summary else "pending"] += 1

    logger.info("experience verify: %s", summary)
    return summary
