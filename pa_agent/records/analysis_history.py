"""Helpers for locating prior analysis records for incremental runs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.data.datetime_ts import format_epoch_for_display, ts_open_to_ms
from pa_agent.data.base import KlineFrame
from pa_agent.records.schema import AnalysisRecord
from pa_agent.records.pending_writer import _safe_path_segment

_TS_EPS_MS = 1.0  # milliseconds tolerance for bar open time matching


@dataclass(frozen=True)
class IncrementalBarDelta:
    """How many closed bars appeared since a previous record."""

    new_count: int
    anchor_ts_open: float
    new_bar_ts_opens: tuple[float, ...]


def format_bar_ts(ts_open: float) -> str:
    """Format bar open time for logs/UI (server-time epoch, no local TZ shift)."""
    return format_epoch_for_display(ts_open, short=False)


def list_record_paths(
    directory: Path | None = None,
    *,
    exchange: str = "",
    symbol: str = "",
    timeframe: str = "",
) -> list[Path]:
    """Return saved analysis record paths, newest modified first.

    Supports both the new partitioned layout
    (``{root}/{exchange}/{symbol}/{timeframe}/{timestamp}.json``) and the
    legacy flat layout
    (``{root}/{timestamp}_{symbol}_{timeframe}.json``).

    If ``exchange``/``symbol``/``timeframe`` are all provided, the narrow
    partition is scanned first; in any case, all ``.json`` files under
    ``root`` are also scanned recursively (via ``rglob``) so that both
    partitioned and flat-layout files are returned.
    """
    root = directory or RECORDS_PENDING_DIR
    if not root.is_dir():
        return []

    paths: list[Path] = []
    seen: set[Path] = set()

    # Narrow by partition if all three are provided.
    if exchange and symbol and timeframe:
        partition = (
            root
            / _safe_path_segment(exchange)
            / _safe_path_segment(symbol)
            / _safe_path_segment(timeframe)
        )
        if partition.is_dir():
            for p in partition.glob("*.json"):
                if p.is_file() and p not in seen:
                    seen.add(p)
                    paths.append(p)

    # Always also scan all .json files recursively. This catches:
    #   - flat-layout files at the top level (legacy)
    #   - partitioned files outside the narrow partition (if any)
    for p in root.rglob("*.json"):
        if p.is_file() and p not in seen:
            seen.add(p)
            paths.append(p)

    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


def load_record(path: Path) -> AnalysisRecord | None:
    """Load one AnalysisRecord, returning None for unreadable legacy files."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AnalysisRecord.model_validate(raw)
    except Exception:
        return None


def find_latest_successful_record(
    *,
    symbol: str = "",
    timeframe: str = "",
    exchange: str = "",
    directory: Path | None = None,
) -> AnalysisRecord | None:
    """Find the newest full successful record matching the given filters.

    If ``symbol``/``timeframe``/``exchange`` are empty (default), returns the
    latest successful record across all symbols/timeframes/exchanges.

    Works with both the new partitioned layout and the legacy flat layout —
    iteration does not assume a specific filename format. Filters are applied
    to the loaded record's meta fields (not to the filename) so both layouts
    are handled uniformly.
    """
    root = directory or RECORDS_PENDING_DIR
    cache_key = (str(root.resolve()), exchange, symbol, timeframe)
    try:
        dir_mtime = root.stat().st_mtime if root.is_dir() else 0.0
    except OSError:
        dir_mtime = 0.0
    cached = _LATEST_RECORD_CACHE.get(cache_key)
    if cached is not None and cached[0] == dir_mtime:
        return cached[1]

    result: AnalysisRecord | None = None
    for path in list_record_paths(
        directory, exchange=exchange, symbol=symbol, timeframe=timeframe
    ):
        record = load_record(path)
        if record is None:
            continue
        if symbol and record.meta.symbol != symbol:
            continue
        if timeframe and record.meta.timeframe != timeframe:
            continue
        if exchange and record.meta.exchange != exchange:
            continue
        if record.exception is not None:
            continue
        if not record.stage1_diagnosis or not record.stage2_decision:
            continue
        if not record.kline_data:
            continue
        result = record
        break
    _LATEST_RECORD_CACHE[cache_key] = (dir_mtime, result)
    return result


_LATEST_RECORD_CACHE: dict[
    tuple[str, str, str, str], tuple[float, AnalysisRecord | None]
] = {}


def invalidate_latest_record_cache() -> None:
    """Clear cached latest-record lookups (call after saving a new record)."""
    _LATEST_RECORD_CACHE.clear()


def compute_incremental_bar_delta(
    frame: KlineFrame,
    previous_record: AnalysisRecord,
) -> IncrementalBarDelta | None:
    """Return bars newer than the previous record's latest closed bar.

    ``frame.bars`` and ``previous_record.kline_data`` are newest-first. The anchor
    is ``kline_data[0]`` (K1 at the time of the previous analysis). New bars are
    those with ``ts_open`` strictly greater than the anchor — not merely bars
    appearing before the anchor index in the current window.
    """
    if not previous_record.kline_data:
        return None

    anchor_raw = previous_record.kline_data[0]["ts_open"]
    anchor = ts_open_to_ms(anchor_raw)

    anchor_seen = False
    new_ts: list[float] = []
    for bar in frame.bars:
        ts = ts_open_to_ms(bar.ts_open)
        if abs(ts - anchor) <= _TS_EPS_MS:
            anchor_seen = True
            continue
        if ts > anchor + _TS_EPS_MS:
            new_ts.append(bar.ts_open)

    if not anchor_seen:
        return None

    return IncrementalBarDelta(
        new_count=len(new_ts),
        anchor_ts_open=float(anchor_raw),
        new_bar_ts_opens=tuple(new_ts),
    )


def count_new_bars_since_record(
    frame: KlineFrame,
    previous_record: AnalysisRecord,
) -> int | None:
    """Backward-compatible wrapper returning only the new bar count."""
    delta = compute_incremental_bar_delta(frame, previous_record)
    if delta is None:
        return None
    return delta.new_count
