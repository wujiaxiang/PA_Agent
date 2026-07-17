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

        Contract:
        - Must return exactly ``n + 1`` bars: ``n`` closed bars (seq=1..n) plus
          1 forming bar (seq=0, ``closed=False``) at index 0.
        - ``bars[0]`` is the forming bar (newest, not yet closed).
        - ``bars[1]`` is the most recent closed bar (seq=1).
        - ``bars[-1]`` is the oldest closed bar (seq=n).

        Implementations SHOULD call ``self._validate_snapshot(n, bars)`` before
        returning to enforce this contract (see TODO P2.2).

        Raises DataSourceTransientError on recoverable network issues.
        """

    def _validate_snapshot(self, n: int, bars: list[KlineBar]) -> list[KlineBar]:
        """Enforce the ``latest_snapshot`` contract (TODO P2.2).

        Verifies the returned list contains exactly ``n + 1`` bars with the
        forming bar at index 0 (``closed=False``, ``seq=0``) and ``n`` closed
        bars (``seq=1..n``).  Raises ``ValueError`` with a clear message on
        violation so new DataSource implementations fail fast instead of
        producing subtle downstream bugs in ``build_analysis_frame``.

        Subclasses should call this at the end of ``latest_snapshot``:
            ``return self._validate_snapshot(n, bars)``
        """
        if len(bars) != n + 1:
            raise ValueError(
                f"{type(self).__name__}.latest_snapshot(n={n}) returned "
                f"{len(bars)} bars, expected exactly {n + 1} "
                f"(n closed bars + 1 forming bar). "
                f"See base.DataSource.latest_snapshot contract."
            )
        forming = bars[0]
        if forming.closed:
            raise ValueError(
                f"{type(self).__name__}.latest_snapshot(n={n}): bars[0] must be "
                f"the forming bar (closed=False), got closed={forming.closed} "
                f"seq={forming.seq}."
            )
        if forming.seq != 0:
            raise ValueError(
                f"{type(self).__name__}.latest_snapshot(n={n}): bars[0].seq must "
                f"be 0 (forming bar), got seq={forming.seq}."
            )
        for i, bar in enumerate(bars[1:], start=1):
            if not bar.closed:
                raise ValueError(
                    f"{type(self).__name__}.latest_snapshot(n={n}): bars[{i}] must "
                    f"be closed (only bars[0] is the forming bar), got closed=False "
                    f"seq={bar.seq}."
                )
            expected_seq = i
            if bar.seq != expected_seq:
                raise ValueError(
                    f"{type(self).__name__}.latest_snapshot(n={n}): bars[{i}].seq "
                    f"must be {expected_seq}, got seq={bar.seq}."
                )
        return bars
