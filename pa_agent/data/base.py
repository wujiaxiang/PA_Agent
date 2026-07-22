"""Core data types and DataSource abstract base class."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence


# ── KlineBar ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KlineBar:
    """A single OHLCV bar with sequence number and closed flag."""
    seq: int           # 1 = newest closed bar, N = oldest; 0 = forming bar (not counted)
    ts_open: float     # Unix timestamp in milliseconds (UTC) of bar open
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0   # turnover amount (成交额); 0 when unavailable
    pct_chg: float | None = None  # daily change % from API when available
    closed: bool = True   # False for the currently-forming bar


def normalize_kline_bar(bar: KlineBar) -> KlineBar:
    """Ensure canonical ``ts_open`` (ms), ``high >= low``, and ``low <= close <= high``."""
    from pa_agent.data.datetime_ts import ts_open_to_ms

    ts_ms = ts_open_to_ms(bar.ts_open)
    high = max(bar.high, bar.low)
    low = min(bar.high, bar.low)
    close = max(low, min(high, bar.close))
    if (
        high == bar.high
        and low == bar.low
        and close == bar.close
        and ts_ms == bar.ts_open
    ):
        return bar
    return KlineBar(
        seq=bar.seq,
        ts_open=ts_ms,
        open=bar.open,
        high=high,
        low=low,
        close=close,
        volume=bar.volume,
        amount=getattr(bar, "amount", 0.0),
        pct_chg=getattr(bar, "pct_chg", None),
        closed=bar.closed,
    )


# ── IndicatorBundle ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IndicatorBundle:
    """Per-bar indicator values aligned to a KlineFrame's bars list."""
    ema20: tuple[float, ...]   # len == len(bars); nan for warm-up period
    atr14: tuple[float, ...]   # len == len(bars); nan for warm-up period


# ── KlineFrame ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KlineFrame:
    """Immutable snapshot of N bars plus computed indicators.

    bars[0] is the newest bar (seq=1, closed=False).
    bars[-1] is the oldest bar (seq=N, closed=True).
    snapshot_ts_local_ms is the local machine time when the snapshot was taken.
    """
    symbol: str
    timeframe: str
    bars: tuple[KlineBar, ...]
    indicators: IndicatorBundle
    snapshot_ts_local_ms: int   # milliseconds since epoch, local time


# ── DataSource ABC ────────────────────────────────────────────────────────────

class DataSourceError(Exception):
    """Base class for data source errors."""


class DataSourceTransientError(DataSourceError):
    """Transient (retryable) error from a data source."""


class DataSource(ABC):
    """Abstract interface for K-line data providers.

    Implementations: TradingViewSource (active), MT5Source (stub).
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection / authenticate."""

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down connection cleanly."""

    @abstractmethod
    def list_symbols(self) -> list[str]:
        """Return available symbol names."""

    @abstractmethod
    def supported_timeframes(self) -> list[str]:
        """Return supported timeframe strings, e.g. ['1m','5m','1h','1d']."""

    @abstractmethod
    def subscribe(self, symbol: str, timeframe: str) -> None:
        """Subscribe to live updates for *symbol* at *timeframe*."""

    @abstractmethod
    def unsubscribe(self) -> None:
        """Cancel the current subscription."""

    @abstractmethod
    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return the *n* most recent bars (index 0 = newest, including forming bar).

        Contract (two modes):
        - Normal mode (market open): returns ``n + 1`` bars — 1 forming bar
          (``seq=0``, ``closed=False``) at index 0 plus ``n`` closed bars
          (``seq=1..n``).
        - Halt mode (market closed / weekend): returns ``n`` bars, all closed
          (``seq=1..n``). ``bars[0]`` is the newest closed bar (``seq=1``).

        ``bars[0]`` is always the newest bar in both modes.

        Implementations SHOULD call ``self._validate_snapshot(n, bars)`` before
        returning to enforce this contract (see TODO P2.2).

        Raises DataSourceTransientError on recoverable network issues.
        """

    def clear_snapshot_cache(self) -> None:
        """Clear any cached snapshot data.

        Subclasses that implement TTL caching for ``latest_snapshot`` should
        override this to invalidate their caches. This is called when a bar
        closes to ensure fresh data is returned on the next snapshot request.
        """

    def _validate_snapshot(self, n: int, bars: list[KlineBar]) -> list[KlineBar]:
        """Enforce the ``latest_snapshot`` contract (TODO P2.2).

        Verifies the returned list matches one of the two valid modes:
        - Normal: ``n + 1`` bars, ``bars[0]`` is forming (``closed=False``,
          ``seq=0``), ``bars[1..n]`` closed with ``seq=1..n``.
        - Halt: ``n`` bars, all closed with ``seq=1..n``.

        Raises ``ValueError`` with a clear message on violation so new
        DataSource implementations fail fast instead of producing subtle
        downstream bugs in ``build_analysis_frame``.

        Subclasses should call this at the end of ``latest_snapshot``:
            ``return self._validate_snapshot(n, bars)``
        """
        if len(bars) not in (n, n + 1):
            raise ValueError(
                f"{type(self).__name__}.latest_snapshot(n={n}) returned "
                f"{len(bars)} bars, expected exactly {n + 1} "
                f"(n closed bars + 1 forming bar) or {n} (halt mode). "
                f"See base.DataSource.latest_snapshot contract."
            )
        head = bars[0]
        if not head.closed:
            # Normal mode: bars[0] is the forming bar.
            if head.seq != 0:
                raise ValueError(
                    f"{type(self).__name__}.latest_snapshot(n={n}): bars[0].seq "
                    f"must be 0 (forming bar), got seq={head.seq}."
                )
            if len(bars) != n + 1:
                raise ValueError(
                    f"{type(self).__name__}.latest_snapshot(n={n}): forming bar "
                    f"present but returned {len(bars)} bars, expected {n + 1}."
                )
            for i, bar in enumerate(bars[1:], start=1):
                if not bar.closed:
                    raise ValueError(
                        f"{type(self).__name__}.latest_snapshot(n={n}): bars[{i}] must "
                        f"be closed (only bars[0] is the forming bar), got closed=False "
                        f"seq={bar.seq}."
                    )
                if bar.seq != i:
                    raise ValueError(
                        f"{type(self).__name__}.latest_snapshot(n={n}): bars[{i}].seq "
                        f"must be {i}, got seq={bar.seq}."
                    )
        else:
            # Halt mode: bars[0] is the newest closed bar (seq=1).
            if head.seq != 1:
                raise ValueError(
                    f"{type(self).__name__}.latest_snapshot(n={n}): halt-mode "
                    f"bars[0].seq must be 1, got seq={head.seq}."
                )
            if len(bars) != n:
                raise ValueError(
                    f"{type(self).__name__}.latest_snapshot(n={n}): halt mode "
                    f"returned {len(bars)} bars, expected {n} (no forming bar)."
                )
            for i, bar in enumerate(bars, start=1):
                if not bar.closed:
                    raise ValueError(
                        f"{type(self).__name__}.latest_snapshot(n={n}): halt mode "
                        f"requires all bars closed, but bars[{i - 1}] has "
                        f"closed=False."
                    )
                if bar.seq != i:
                    raise ValueError(
                        f"{type(self).__name__}.latest_snapshot(n={n}): bars[{i - 1}].seq "
                        f"must be {i}, got seq={bar.seq}."
                    )
        return bars
