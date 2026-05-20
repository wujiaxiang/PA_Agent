"""Load pending analysis JSON records for demo replay."""
from __future__ import annotations

import json
import random
from pathlib import Path

from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.snapshot import compute_indicators
from pa_agent.records.schema import AnalysisRecord
from pa_agent.util.timefmt import now_local_ms


def list_pending_record_paths(directory: Path | None = None) -> list[Path]:
    """Return ``*.json`` analysis records under *directory* (sorted newest first)."""
    root = directory or RECORDS_PENDING_DIR
    if not root.is_dir():
        return []
    files = [p for p in root.glob("*.json") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def pick_random_record_path(directory: Path | None = None) -> Path | None:
    paths = list_pending_record_paths(directory)
    return random.choice(paths) if paths else None


def load_analysis_record(path: Path) -> AnalysisRecord:
    """Parse one pending record file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return AnalysisRecord.model_validate(raw)


def frame_from_record_klines(
    kline_data: list[dict],
    *,
    symbol: str,
    timeframe: str,
    snapshot_ts_local_ms: int | None = None,
) -> KlineFrame:
    """Build a chart/analysis frame from persisted ``kline_data`` (newest-first)."""
    rebased: list[KlineBar] = []
    for i, b in enumerate(kline_data):
        ts = float(b["ts_open"])
        if ts > 1e12:
            ts = ts / 1000.0
        rebased.append(
            KlineBar(
                seq=int(b.get("seq", i + 1)),
                ts_open=ts,
                open=float(b["open"]),
                high=float(b["high"]),
                low=float(b["low"]),
                close=float(b["close"]),
                volume=float(b.get("volume", 0)),
                closed=bool(b.get("closed", True)),
            )
        )
    if not rebased:
        raise ValueError("Record has no kline_data")
    return KlineFrame(
        symbol=symbol,
        timeframe=timeframe,
        bars=tuple(rebased),
        indicators=compute_indicators(rebased),
        snapshot_ts_local_ms=snapshot_ts_local_ms or now_local_ms(),
    )
