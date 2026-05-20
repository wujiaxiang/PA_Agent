"""KlineFrame snapshot builder."""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.data.kline_buffer import KlineBuffer
from pa_agent.util.timefmt import now_local_ms

if TYPE_CHECKING:
    pass


def take_snapshot(buffer: KlineBuffer, n: int, symbol: str, timeframe: str) -> KlineFrame:
    """Build an immutable KlineFrame from the *n* most recent bars in *buffer*.

    Sequence numbering:
    - bars[0].seq == 1, closed == False  (the forming bar)
    - bars[i].seq == i+1, closed == True  for i >= 1
    - {bar.seq for bar in bars} == {1, ..., n}  (bijection)

    Raises ValueError if the buffer has fewer than *n* bars.
    """
    raw = buffer.last_n_including_forming(n)
    if len(raw) < n:
        raise ValueError(
            f"Buffer has only {len(raw)} bars; requested {n}. "
            "Wait for more data before taking a snapshot."
        )

    # Re-assign seq numbers to guarantee the bijection invariant
    bars: list[KlineBar] = []
    for i, bar in enumerate(raw[:n]):
        bars.append(
            KlineBar(
                seq=i + 1,
                ts_open=bar.ts_open,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                closed=(i != 0),   # index 0 is always the forming bar
            )
        )

    indicators = compute_indicators(bars)

    return KlineFrame(
        symbol=symbol,
        timeframe=timeframe,
        bars=tuple(bars),
        indicators=indicators,
        snapshot_ts_local_ms=now_local_ms(),
    )


def compute_indicators(bars: list[KlineBar]) -> IndicatorBundle:
    """Compute EMA20 and ATR14 for *bars* (newest-first order).

    Indicators are computed on the reversed (oldest-first) sequence and then
    reversed back so that index 0 corresponds to bars[0] (the forming bar).
    """
    from pa_agent.indicators.ema import ema_full
    from pa_agent.indicators.atr import atr_full

    # bars is newest-first; indicators need oldest-first input
    bars_asc = list(reversed(bars))

    closes = [b.close for b in bars_asc]
    highs  = [b.high  for b in bars_asc]
    lows   = [b.low   for b in bars_asc]

    ema20_asc = ema_full(closes, period=20)
    atr14_asc = atr_full(highs, lows, closes, period=14)

    # Reverse back to newest-first
    ema20 = tuple(reversed(ema20_asc))
    atr14 = tuple(reversed(atr14_asc))

    return IndicatorBundle(ema20=ema20, atr14=atr14)


def build_display_frame(
    bars_raw: list[KlineBar],
    n: int,
    symbol: str,
    timeframe: str,
) -> KlineFrame | None:
    """Chart display frame — same semantics as AI (K1 = newest **closed** bar)."""
    return build_analysis_frame(bars_raw, n, symbol, timeframe)


def build_live_frame(
    bars_raw: list[KlineBar],
    n_closed: int,
    symbol: str,
    timeframe: str,
) -> KlineFrame | None:
    """Live chart frame: include the forming bar + *n_closed* closed bars.

    This is for UI only. The analysis snapshot must still use
    ``build_analysis_frame`` so AI always sees closed-only candles.
    """
    if len(bars_raw) < n_closed + 1:
        return None

    raw = bars_raw[: n_closed + 1]  # forming + N closed
    rebased: list[KlineBar] = [
        KlineBar(
            seq=i + 1,
            ts_open=b.ts_open,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
            closed=(i != 0),
        )
        for i, b in enumerate(raw)
    ]
    indicators = compute_indicators(rebased)
    return KlineFrame(
        symbol=symbol,
        timeframe=timeframe,
        bars=tuple(rebased),
        indicators=indicators,
        snapshot_ts_local_ms=now_local_ms(),
    )


def build_analysis_frame(
    bars_raw: list[KlineBar],
    n: int,
    symbol: str,
    timeframe: str,
) -> KlineFrame | None:
    """Build a snapshot for AI analysis: *n* newest **closed** bars only.

    *bars_raw* is newest-first; ``bars_raw[0]`` is the forming (unclosed) bar
    and is discarded. Returns None if fewer than ``n + 1`` bars are available.

    Chart and AI must both use this (or ``build_display_frame``) so K-line
    seq numbers refer to the same candles.
    """
    if len(bars_raw) < n + 1:
        return None

    closed_raw = bars_raw[1 : n + 1]
    rebased: list[KlineBar] = [
        KlineBar(
            seq=i + 1,
            ts_open=b.ts_open,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
            closed=True,
        )
        for i, b in enumerate(closed_raw)
    ]
    indicators = compute_indicators(rebased)
    return KlineFrame(
        symbol=symbol,
        timeframe=timeframe,
        bars=tuple(rebased),
        indicators=indicators,
        snapshot_ts_local_ms=now_local_ms(),
    )
