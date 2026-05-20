"""Tests for demo record loading."""
from __future__ import annotations

import json
from pathlib import Path

from pa_agent.demo.record_loader import (
    frame_from_record_klines,
    list_pending_record_paths,
    load_analysis_record,
)


def test_load_pending_sample_record() -> None:
    paths = list_pending_record_paths()
    if not paths:
        return
    record = load_analysis_record(paths[0])
    assert record.meta.symbol
    assert record.kline_data


def test_frame_from_record_klines() -> None:
    paths = list_pending_record_paths()
    if not paths:
        return
    record = load_analysis_record(paths[0])
    frame = frame_from_record_klines(
        record.kline_data,
        symbol=record.meta.symbol,
        timeframe=record.meta.timeframe,
    )
    assert frame.bars[0].seq == 1
    assert len(frame.indicators.ema20) == len(frame.bars)
