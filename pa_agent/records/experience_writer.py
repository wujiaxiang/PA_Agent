"""ExperienceWriter — the write half of the experience library.

``ExperienceReader`` is documented as "strictly read-only", and nothing else in
the repo ever created a file under ``EXPERIENCE_DIR``. That made the library a
dead end: the 59 shipped entries never grow, and no analysis ever feeds back
into Stage 1/Stage 2 retrieval.

This module closes that loop. Entries are written to the same layout the reader
expects::

    experience/<cycle_position>/success_cases/<ts>_<symbol>_<timeframe>.json
    experience/<cycle_position>/failure_cases/<ts>_<symbol>_<timeframe>.json

so a file written here is immediately retrievable by ``ExperienceReader``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pa_agent.config.paths import EXPERIENCE_DIR

#: Timestamp format shared with ExperienceReader (``minutes use '-'``).
_TS_FORMAT = "%Y-%m-%d_%H-%M-%S"

#: Cycles that have no directory on disk yet; writing must create them.
_KNOWN_CYCLES = (
    "trending_tr", "trading_range", "broad_channel", "normal_channel",
    "tight_channel", "micro_channel", "spike", "extreme_tr",
)


def _default_logger() -> logging.Logger:
    return logging.getLogger("pa_agent.experience_writer")


def _safe_segment(value: str, fallback: str = "unknown") -> str:
    """Sanitise a path segment so a hostile symbol cannot escape the library."""
    text = str(value or "").strip()
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch in "-_.")
    cleaned = cleaned.lstrip(".")
    return cleaned or fallback


class ExperienceWriter:
    """Persist analysis outcomes into the experience library.

    Parameters
    ----------
    experience_dir:
        Root of the library. Defaults to the configured ``EXPERIENCE_DIR``.
    logger:
        Optional logger; a module logger is used when omitted.
    """

    def __init__(
        self,
        experience_dir: Path | str | None = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._dir = Path(experience_dir) if experience_dir else Path(EXPERIENCE_DIR)
        self._log = logger or _default_logger()
        self._lock = threading.Lock()

    # ── path helpers ─────────────────────────────────────────────────────
    def _subdir(self, cycle_position: str, success: bool) -> Path:
        cycle = _safe_segment(cycle_position, fallback="trending_tr")
        sub = "success_cases" if success else "failure_cases"
        path = self._dir / cycle / sub
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _filename(symbol: str, timeframe: str) -> str:
        stamp = datetime.now().strftime(_TS_FORMAT)
        return f"{stamp}_{_safe_segment(symbol)}_{_safe_segment(timeframe, '1h')}.json"

    # ── public API ───────────────────────────────────────────────────────
    def save(
        self,
        *,
        cycle_position: str,
        direction: str,
        detected_patterns: list[str] | None,
        confidence: int | float,
        summary: str,
        symbol: str,
        timeframe: str,
        entry_price: float,
        success: bool,
        pnl_pct: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """Write one experience entry and return its path.

        ``success`` selects the ``success_cases``/``failure_cases`` subdirectory;
        ``pnl_pct`` is rounded to 2 decimals and omitted when unknown.
        """
        content: dict[str, Any] = {
            "direction": str(direction or ""),
            "detected_patterns": list(detected_patterns or []),
            "confidence": int(round(float(confidence or 0))),
            "summary": str(summary or "")[:500],
            "symbol": str(symbol or ""),
            "timeframe": str(timeframe or ""),
            "entry_price": round(float(entry_price), 6),
            "result": "win" if success else "loss",
        }
        if pnl_pct is not None:
            content["pnl_pct"] = round(float(pnl_pct), 2)
        if extra:
            for k, v in extra.items():
                if k not in content:
                    content[k] = v

        with self._lock:
            subdir = self._subdir(cycle_position, success)
            target = subdir / self._filename(symbol, timeframe)
            # 同毫秒内两次落盘也不会互相覆盖
            if target.exists():
                stem = target.stem
                target = subdir / f"{stem}_{uuid.uuid4().hex[:6]}{target.suffix}"

            tmp = target.with_suffix(".json.tmp")
            try:
                tmp.write_text(
                    json.dumps(content, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, target)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise

        self._log.info(
            "experience saved: cycle=%s %s %s -> %s",
            cycle_position, symbol, timeframe, target.name,
        )
        return target


def evaluate_outcome(
    bars: list[dict[str, Any]],
    *,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    is_long: bool = True,
) -> tuple[str, float] | None:
    """Resolve a plan against subsequent bars.

    Scans *bars* (oldest-first or newest-first, both accepted) and returns
    ``(result, pnl_pct)`` for the first bar that touches TP or SL, or ``None``
    while neither has been reached.

    Within a single bar that spans both levels the **stop is assumed to hit
    first** — we cannot know the intrabar path from OHLC alone, and assuming the
    optimistic order would bias the library toward over-optimistic wins.
    """
    if not bars or entry_price <= 0:
        return None
    ordered = sorted(bars, key=lambda b: b.get("ts_open", 0))
    for bar in ordered:
        high = bar.get("high")
        low = bar.get("low")
        if high is None or low is None:
            continue
        hit_tp = high >= take_profit_price if is_long else low <= take_profit_price
        hit_sl = low <= stop_loss_price if is_long else high >= stop_loss_price
        if hit_tp and hit_sl:
            return ("loss", -abs(stop_loss_price - entry_price) / entry_price * 100.0)
        if hit_sl:
            return ("loss", -abs(stop_loss_price - entry_price) / entry_price * 100.0)
        if hit_tp:
            return ("win", abs(take_profit_price - entry_price) / entry_price * 100.0)
    return None


__all__ = ["ExperienceWriter", "evaluate_outcome", "_KNOWN_CYCLES"]