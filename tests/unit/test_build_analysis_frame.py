"""Tests for analysis snapshots (closed bars only)."""
from __future__ import annotations

from pa_agent.data.base import KlineBar
from pa_agent.data.snapshot import build_analysis_frame, build_display_frame


def _bar(seq: int, ts: float, *, closed: bool) -> KlineBar:
    return KlineBar(
        seq=seq,
        ts_open=ts,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100.0,
        closed=closed,
    )


def test_build_analysis_frame_drops_forming_bar() -> None:
    raw = [
        _bar(1, 300.0, closed=False),  # forming — must be dropped
        _bar(2, 200.0, closed=True),
        _bar(3, 100.0, closed=True),
    ]
    frame = build_analysis_frame(raw, 2, "XAU", "5m")
    assert frame is not None
    assert len(frame.bars) == 2
    assert all(b.closed for b in frame.bars)
    assert frame.bars[0].ts_open == 200.0
    assert frame.bars[0].seq == 1
    assert frame.bars[1].ts_open == 100.0


def test_build_analysis_frame_insufficient_data() -> None:
    raw = [_bar(1, 300.0, closed=False), _bar(2, 200.0, closed=True)]
    assert build_analysis_frame(raw, 2, "XAU", "5m") is None


def test_display_frame_matches_analysis_frame() -> None:
    raw = [
        _bar(1, 300.0, closed=False),
        _bar(2, 200.0, closed=True),
        _bar(3, 100.0, closed=True),
    ]
    a = build_analysis_frame(raw, 2, "XAU", "5m")
    d = build_display_frame(raw, 2, "XAU", "5m")
    assert a is not None and d is not None
    assert [b.ts_open for b in a.bars] == [b.ts_open for b in d.bars]
    assert a.bars[0].seq == 1
