"""TTL cache for ``TradingViewSource.latest_snapshot``.

Each ``latest_snapshot`` call normally opens a fresh WebSocket via
``tvDatafeed.get_hist()``. The TTL cache added in tradingview.py short-circuits
repeated calls within ``tv_cache_ttl_seconds(timeframe)`` so the WebSocket is
only opened once per TTL window. These tests mock ``_latest_snapshot_inner``
(the inner method that actually touches the network) and assert it is called
the expected number of times.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pa_agent.data.base import KlineBar
from pa_agent.data.refresh_policy import tv_cache_ttl_seconds
from pa_agent.data.tradingview import TradingViewSource


def _make_source() -> TradingViewSource:
    """Build a TradingViewSource wired to a mock tvDatafeed instance.

    The source is marked connected and subscribed to ``XAUUSD`` on the ``15m``
    timeframe so ``latest_snapshot`` can proceed past its precondition checks.
    """
    src = TradingViewSource()
    src._tv = MagicMock()
    src._connected = True
    src._symbol = "XAUUSD"
    src._timeframe = "15m"
    src._exchange = "OANDA"
    return src


def _bar(seq: int) -> KlineBar:
    return KlineBar(
        seq=seq,
        ts_open=seq * 60_000,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
        closed=True,
    )


def test_ttl_hit_within_window() -> None:
    """TTL 内命中缓存，不调用底层 TV API。"""
    src = _make_source()
    bars = [_bar(0), _bar(1)]

    with patch.object(src, "_latest_snapshot_inner", return_value=bars) as inner:
        out1 = src.latest_snapshot(100)
        out2 = src.latest_snapshot(100)

    assert inner.call_count == 1
    assert len(out1) == 2
    assert len(out2) == 2
    # Returned bars must match the inner result; the hit returns a copy.
    assert out1 == bars
    assert out2 == bars


def test_ttl_expired_refetches() -> None:
    """TTL 过期后重新拉取。"""
    src = _make_source()
    bars = [_bar(0), _bar(1)]
    ttl = tv_cache_ttl_seconds("15m")

    # Controllable clock: starts at t0, then advances past the TTL between
    # the first and second latest_snapshot() calls.
    t = [1000.0]

    def fake_time() -> float:
        return t[0]

    with (
        patch("pa_agent.data.tradingview.time.time", side_effect=fake_time),
        patch.object(src, "_latest_snapshot_inner", return_value=bars) as inner,
    ):
        out1 = src.latest_snapshot(100)
        t[0] = 1000.0 + ttl + 1  # advance past TTL window
        out2 = src.latest_snapshot(100)

    assert inner.call_count == 2
    assert len(out1) == 2
    assert len(out2) == 2


def test_different_count_no_hit() -> None:
    """不同 count 参数不命中缓存。"""
    src = _make_source()
    bars = [_bar(0), _bar(1)]

    with patch.object(src, "_latest_snapshot_inner", return_value=bars) as inner:
        out1 = src.latest_snapshot(100)
        out2 = src.latest_snapshot(200)

    assert inner.call_count == 2
    assert len(out1) == 2
    assert len(out2) == 2
