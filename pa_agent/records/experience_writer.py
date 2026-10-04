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

# ── Two-stage library ────────────────────────────────────────────────────────
# Stage 1 (入场时) 立刻落一条 ``pending`` 记录 —— 记录"我们做了什么"是事实，
# 不需要等结果。
# Stage 2 (验证) 拿到入场后的 N 根 K 线再判定终态。
#   win        触及 TP
#   loss       触及 SL
#   unresolved 已看完 N 根 K 线仍未触及任一价位（终态，但无盈亏结论）
#   pending    K 线还不够 N 根，继续等
# 未结算的记录不能进 ExperienceReader —— 否则会把"还不知道输赢"的样本
# 当成失败经验喂回提示词。
STATUS_PENDING = "pending"
STATUS_WIN = "win"
STATUS_LOSS = "loss"
STATUS_UNRESOLVED = "unresolved"

#: status → 目录名。只有 pending/success/failure 会被 ExperienceReader 读到。
STATUS_DIRS: dict[str, str] = {
    STATUS_PENDING: "pending_cases",
    STATUS_WIN: "success_cases",
    STATUS_LOSS: "failure_cases",
    STATUS_UNRESOLVED: "unresolved_cases",
}

#: 这些状态是终态，不再需要验证
TERMINAL_STATUSES = frozenset({STATUS_WIN, STATUS_LOSS, STATUS_UNRESOLVED})

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

    def _status_subdir(self, cycle_position: str, status: str) -> Path:
        """Directory for a *status* (two-stage library)."""
        cycle = _safe_segment(cycle_position, fallback="trending_tr")
        # 注意：目录名来自 STATUS_DIRS[status]（pending → pending_cases）。
        # 曾经误把 status 本身当目录名，写出 pending/ 而不是 pending_cases/，
        # ExperienceReader 按 *_cases 扫描 → 结算后的记录读不到。
        st = str(status or "").strip().lower()
        sub = STATUS_DIRS.get(st)
        if sub is None:
            self._log.warning("experience: unknown status %r → pending", status)
            sub = STATUS_DIRS[STATUS_PENDING]
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
            # Previously cycle_position only reached the *directory* name, so
            # every entry's JSON came back with cycle_position == null and the
            # reader had to fall back to its parent folder. Persist it so an
            # entry file is self-describing.
            "cycle_position": str(cycle_position or ""),
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


    # ── two-stage API ────────────────────────────────────────────────────
    def save_pending(
        self,
        *,
        cycle_position: str,
        direction: str,
        detected_patterns: list[str] | None,
        confidence: int | float,
        summary: str,
        symbol: str,
        timeframe: str,
        exchange: str,
        entry_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        is_long: bool,
        entry_ts_open_ms: int,
        created_ts_ms: int | None = None,
        analysis_context: dict[str, Any] | None = None,
        bars_snapshot: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """Stage 1 — record the plan the moment the signal fires.

        Written immediately as ``pending``: whether the setup worked is not yet
        known, but *"we took this setup at this price"* already is. Deferring
        the write until the outcome is known loses everything if the process
        restarts mid-watch.
        """
        import time as _time

        content: dict[str, Any] = {
            "status": STATUS_PENDING,
            "cycle_position": str(cycle_position or ""),
            "direction": str(direction or ""),
            "detected_patterns": list(detected_patterns or []),
            "confidence": int(round(float(confidence or 0))),
            "summary": str(summary or "")[:500],
            "symbol": str(symbol or ""),
            "timeframe": str(timeframe or ""),
            "exchange": str(exchange or ""),
            "entry_price": round(float(entry_price), 6),
            "take_profit_price": round(float(take_profit_price), 6),
            "stop_loss_price": round(float(stop_loss_price), 6),
            "is_long": bool(is_long),
            # 判定窗口的起点：只有 ts_open 严格大于它的 K 线才算"入场之后"
            "entry_ts_open_ms": int(entry_ts_open_ms or 0),
            "created_ts_ms": int(created_ts_ms if created_ts_ms is not None else _time.time() * 1000),
            "bars_seen": 0,
        }
        # 复盘所需的完整上下文：只存决策要点，不存 prompt/response 原文
        # （后者动辄几万 token，一条记录存全会让库迅速膨胀且无复盘价值）。
        if analysis_context:
            content["analysis_context"] = _compact_analysis_context(analysis_context)
        # 进场前后的 K 线窗口：让复盘能对照「当时看到的形态」与「实际走势」
        if bars_snapshot:
            content["bars_snapshot"] = _compact_bars(bars_snapshot)
        if extra:
            for k, v in extra.items():
                if k not in content:
                    content[k] = v
        return self._write(content, cycle_position, STATUS_PENDING, symbol, timeframe)

    def finalize(
        self,
        path: Path | str,
        *,
        status: str,
        pnl_pct: float | None = None,
        bars_seen: int | None = None,
        resolved_ts_ms: int | None = None,
    ) -> Path | None:
        """Stage 2 — move a pending entry into its terminal directory.

        Returns the new path, or ``None`` when *path* is missing/unreadable.
        The file is rewritten (never copied then deleted) so a crash mid-way
        leaves either the pending or the terminal version, never a duplicate.
        """
        import time as _time

        src = Path(path)
        if not src.is_file():
            return None
        status = status if status in STATUS_DIRS else STATUS_UNRESOLVED
        try:
            content = json.loads(src.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("experience finalize: unreadable %s: %s", src.name, exc)
            return None
        if not isinstance(content, dict):
            return None

        content["status"] = status
        content["result"] = status if status in (STATUS_WIN, STATUS_LOSS) else None
        if pnl_pct is not None:
            content["pnl_pct"] = round(float(pnl_pct), 2)
        else:
            content.pop("pnl_pct", None)
        if bars_seen is not None:
            content["bars_seen"] = int(bars_seen)
        content["resolved_ts_ms"] = int(
            resolved_ts_ms if resolved_ts_ms is not None else _time.time() * 1000
        )
        content.pop("result_none_marker", None)
        if content.get("result") is None:
            content.pop("result", None)

        return self._write(
            content,
            str(content.get("cycle_position") or "trending_tr"),
            status,
            str(content.get("symbol") or ""),
            str(content.get("timeframe") or "1h"),
            source=src,
        )

    def update_pending_progress(
        self, path: Path | str, *, bars_seen: int
    ) -> None:
        """Persist how many bars a still-pending record has accumulated."""
        src = Path(path)
        if not src.is_file():
            return
        try:
            content = json.loads(src.read_text(encoding="utf-8"))
            if not isinstance(content, dict):
                return
            content["bars_seen"] = int(bars_seen)
            tmp = src.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, src)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("experience progress update failed for %s: %s", src.name, exc)

    def list_pending(self, limit: int = 100) -> list[tuple[Path, dict[str, Any]]]:
        """Return ``(path, content)`` for every pending record, oldest first."""
        out: list[tuple[Path, dict[str, Any]]] = []
        if not self._dir.is_dir():
            return out
        for cycle_dir in sorted(self._dir.iterdir()):
            if not cycle_dir.is_dir() or cycle_dir.name.startswith("."):
                continue
            pending_dir = cycle_dir / STATUS_DIRS[STATUS_PENDING]
            if not pending_dir.is_dir():
                continue
            for f in sorted(pending_dir.glob("*.json")):
                try:
                    content = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(content, dict):
                    out.append((f, content))
                if len(out) >= limit:
                    return out
        return out

    def _write(
        self,
        content: dict[str, Any],
        cycle_position: str,
        status: str,
        symbol: str,
        timeframe: str,
        source: Path | None = None,
    ) -> Path:
        with self._lock:
            subdir = self._status_subdir(cycle_position, status)
            # 状态流转时沿用原文件名（记录时间不该因为结算而改变）
            target = source.name if source is not None else self._filename(symbol, timeframe)
            dst = subdir / target
            if source is None and dst.exists():
                stem = dst.stem
                dst = subdir / f"{stem}_{uuid.uuid4().hex[:6]}{dst.suffix}"

            tmp = dst.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, dst)
                if source is not None and source != dst:
                    try:
                        source.unlink(missing_ok=True)
                    except Exception:
                        pass
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        self._log.info(
            "experience written: status=%s cycle=%s %s %s -> %s",
            status, cycle_position, symbol, timeframe, dst.name,
        )
        return dst


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

def save_pending_if_resolvable(
    *,
    writer: "ExperienceWriter",
    settings: Any,
    exchange: str,
    symbol: str,
    timeframe: str,
    stage1: dict[str, Any],
    stage2_flat: dict[str, Any],
    entry_ts_open_ms: int,
    bars: list[Any] | None = None,
) -> Optional[Path]:
    """Stage-1 gate: write a pending record only when the plan is resolvable.

    Kept as a module function (not a method) so the gating rules stay
    Qt-free and unit-testable without spinning up the web layer.

    Returns the written path, or ``None`` when the decision is not a
    tradeable plan or watching is disabled.
    """
    prompt_cfg = getattr(settings, "prompt", None)
    if prompt_cfg is None or not getattr(prompt_cfg, "experience_auto_write", True):
        return None

    order_type = str(stage2_flat.get("order_type") or "")
    if order_type in ("不下单", "no_order", ""):
        return None

    entry = _num(stage2_flat.get("entry_price"))
    tp = _num(stage2_flat.get("take_profit_price"))
    sl = _num(stage2_flat.get("stop_loss_price"))
    if entry is None or tp is None or sl is None or entry <= 0:
        return None
    if tp == entry or sl == entry:
        return None

    direction = str(stage2_flat.get("order_direction") or "")
    lowered = direction.strip().lower()
    is_long = lowered in ("long", "做多", "buy")
    is_short = lowered in ("short", "做空", "sell")
    if not (is_long or is_short):
        return None

    # 没有入场锚点就没法界定"入场之后"，宁可不写
    if int(entry_ts_open_ms or 0) <= 0:
        return None

    # 复盘上下文：阶段一原始判断 + 扁平化的阶段二决策 + K 线窗口
    context = {
        "stage1": dict(stage1 or {}),
        "stage2": dict(stage2_flat or {}),
    }

    return writer.save_pending(
        cycle_position=str(stage1.get("cycle_position") or "trending_tr"),
        direction=direction,
        detected_patterns=list(stage1.get("detected_patterns") or []),
        confidence=stage1.get("diagnosis_confidence") or 0,
        summary=str(stage2_flat.get("reasoning") or stage2_flat.get("diagnosis_summary") or ""),
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        entry_price=entry,
        take_profit_price=tp,
        stop_loss_price=sl,
        is_long=is_long,
        entry_ts_open_ms=int(entry_ts_open_ms),
        analysis_context=context,
        bars_snapshot=list(bars or []),
    )


def _num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
        return float(v)
    return None


# ── 复盘上下文的压缩 ──────────────────────────────────────────────────────────
# LLM 复盘需要知道「当时我们看到了什么、为什么这么判」，否则它只会对着
# 一个 pnl 数字事后诸葛亮。保留决策要点，丢弃冗长原文。

_CONTEXT_KEEP: dict[str, tuple[str, ...]] = {
    "stage1": (
        "cycle_position", "alternative_cycle_position", "direction",
        "detected_patterns", "diagnosis_confidence", "entry_setup",
        "gate_result", "bar_by_bar_summary", "htf_context", "climax_risk",
        "trend", "market_cycle", "next_cycle", "support_resistance",
    ),
    "stage2": (
        "order_type", "order_direction", "entry_price", "stop_loss_price",
        "take_profit_price", "take_profit2_price", "trade_confidence",
        "risk_reward_ratio", "estimated_win_rate", "diagnosis_summary",
        "reasoning", "decision", "terminal",
    ),
}


def _compact_analysis_context(ctx: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for section, keys in _CONTEXT_KEEP.items():
        block = ctx.get(section) or {}
        if not isinstance(block, dict):
            continue
        keep: dict[str, Any] = {}
        for k in keys:
            if k not in block:
                continue
            v = block[k]
            # 长文本截断：保留结论，丢弃展开叙述
            if isinstance(v, str):
                keep[k] = v[:600]
            elif isinstance(v, (int, float, bool)) or v is None:
                keep[k] = v
            elif isinstance(v, list):
                keep[k] = v[:12]
            elif isinstance(v, dict):
                keep[k] = {kk: vv for kk, vv in list(v.items())[:12]}
        if keep:
            out[section] = keep
    for extra_key in ("order_type", "order_direction", "entry_price",
                      "stop_loss_price", "take_profit_price",
                      "trade_confidence", "reasoning"):
        if extra_key in ctx and extra_key not in (out.get("stage2") or {}):
            out.setdefault("stage2_flat", {})[extra_key] = ctx[extra_key]
    return out


def _compact_bars(bars: list[Any], *, before: int = 20, after: int = 20) -> list[dict[str, Any]]:
    """Keep a bounded window around the entry so review can see both sides."""
    rows: list[dict[str, Any]] = []
    for b in bars or []:
        if isinstance(b, dict):
            get = b.get
        else:
            get = lambda k, d=None: getattr(b, k, d)  # noqa: E731
        try:
            rows.append({
                "ts_open": int(get("ts_open", 0) or 0),
                "o": _rnd(get("open")),
                "h": _rnd(get("high")),
                "l": _rnd(get("low")),
                "c": _rnd(get("close")),
            })
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r["ts_open"])
    if len(rows) <= before + after:
        return rows
    mid = len(rows) // 2
    return rows[max(0, mid - before): mid + after]


def _rnd(v: Any) -> float | None:
    try:
        return round(float(v), 8) if v is not None else None
    except (TypeError, ValueError):
        return None
