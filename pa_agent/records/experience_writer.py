"""ExperienceWriter — 经验库写入端。

**2026-10-05 起：数据库是唯一真源，本模块不再写任何文件。**

此前每个案例是一个 JSON 文件，落在 ``experience/<cycle>/<status>_cases/`` 下，
「目录即状态」（pending → success/failure/unresolved 靠移动文件实现）。那条
路已经废弃，理由是它同时踩了三个坑：

- **多用户无从隔离**：文件系统里没有用户概念，A 用户的案例 B 用户照样能读到，
  ``user_id`` 那一列在读端形同装饰
- **状态流转要搬文件**：结算一个 pending 要把文件移到另一个目录，中途崩溃会
  留下半套状态；且文件名成了跨模块的隐式契约
- **读者必须扫目录**：每次分析都 rglob 一遍全局目录，既慢又没法用索引

现在状态是 ``experience_entries.status`` 这一列，流转是一条 UPDATE。
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

from pa_agent.storage.experience_repo import new_entry_id

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

#: **兼容字典**。库里状态是 ``experience_entries.status`` 一列，不再有目录；
#: 保留映射仅为让读侧能按目录名习惯取状态，以及旧调用点不必立刻改写。
#: 新代码请直接用 STATUS_* 常量。
STATUS_DIRS: dict[str, str] = {
    STATUS_PENDING: "pending_cases",
    STATUS_WIN: "success_cases",
    STATUS_LOSS: "failure_cases",
    STATUS_UNRESOLVED: "unresolved_cases",
}

#: 读端唯一允许检索的状态。未决的 pending / 无盈亏的 unresolved 都不是
#: 已验证经验，不得当成失败经验喂回提示词（AGENTS.md）。
RETRIEVABLE_STATUSES: tuple[str, ...] = (STATUS_WIN, STATUS_LOSS)

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
        **已废弃**：库内不再有文件，本参数只为兼容既有调用点而保留，不影响行为。
    logger:
        Optional logger; a module logger is used when omitted.
    """

    def __init__(
        self,
        experience_dir: Path | str | None = None,   # 兼容参数，已无效果
        logger: Optional[logging.Logger] = None,
    ) -> None:
        # 兼容参数：不再决定任何写入位置（写库，不写文件）
        self._legacy_dir = str(experience_dir) if experience_dir else ""
        self._log = logger or _default_logger()
        self._lock = threading.Lock()

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
        user_id: str = "",
    ) -> str:
        """写入一条**已终态**的经验，返回 ``entry_id``。

        ``success`` 决定 status 是 win 还是 loss；``pnl_pct`` 保留 2 位小数，
        未知则不写该字段。``entry_id`` 自带唯一性（uuid 后缀），不再依赖
        文件名 —— 旧实现里两个用户在同一秒为同一标的写盘会撞文件名。
        """
        content: dict[str, Any] = {
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
        owner = self._owner(user_id)
        if owner:
            content["user_id"] = owner
        return self._upsert(
            content,
            cycle_position=cycle_position,
            status=STATUS_WIN if success else STATUS_LOSS,
            symbol=symbol, timeframe=timeframe, user_id=owner,
        )

    @staticmethod
    def _owner(user_id: str | None) -> str | None:
        """归属用户。

        - 给了名字 → 用它
        - ``None`` → **不过滤**（后台结算要遍历所有用户）
        - 空串 ``""`` → 回落存储层默认用户（单机部署下它就是正确答案）

        这三档必须分开：结算若回落成默认用户，就只结算 admin 的记录，
        其他用户的经验永远停在 pending（真机/单测都验证过这个失效）。
        """
        if user_id is None:
            return None
        if user_id:
            return str(user_id)
        from pa_agent.storage.db import DEFAULT_USER_ID

        return DEFAULT_USER_ID

    def _upsert(
        self,
        content: dict[str, Any],
        *,
        cycle_position: str,
        status: str,
        symbol: str,
        timeframe: str,
        user_id: str = "",
        entry_id: str | None = None,
    ) -> str:
        """把一条经验写进库里，返回 ``entry_id``。

        状态流转就是**同一条**记录的 UPDATE（``entry_id`` 复用），所以不会
        因为结算而多出一行。DB 写失败只记 warning —— 经验库是锦上添花的输出，
        绝不能因为它把分析主流程带崩。
        """
        owner = self._owner(user_id)
        eid = entry_id or new_entry_id(owner)
        try:
            from pa_agent.storage.experience_repo import upsert_entry

            upsert_entry(
                content,
                entry_id=eid,
                cycle_position=cycle_position,
                status=status,
                symbol=symbol,
                timeframe=timeframe,
                user_id=owner,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("experience write failed (cycle=%s): %s", cycle_position, exc)
        return eid

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
        user_id: str = "",
    ) -> str:
        """Stage 1 — 信号出现那一刻就记录这笔计划，返回 ``entry_id``。

        立即以 ``pending`` 落库：setup 走没走出来还不知道，但「我们在���价位
        接了这笔单」已经是事实。等结果出来再写会丢掉中途重启的一切。

        ``user_id`` **写进 content 本身**，不只是传给 DB。阶段二结算跑在后台
        调度器线程上、结算的是几小时前那条 pending —— 那时没有请求上下文，
        唯一能知道这条属于谁的地方就是记录自己。
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
        owner = self._owner(user_id)
        content["user_id"] = owner
        return self._upsert(
            content, cycle_position=cycle_position, status=STATUS_PENDING,
            symbol=symbol, timeframe=timeframe, user_id=owner,
        )

    def finalize(
        self,
        entry_id: str,
        *,
        status: str,
        pnl_pct: float | None = None,
        bars_seen: int | None = None,
        resolved_ts_ms: int | None = None,
        user_id: str = "",
    ) -> bool:
        """Stage 2 — 把一条 pending 结成终态。

        现在只是一条 UPDATE（``entry_id`` 不变，只改 status），不再有
        「把文件搬到另一个目录」这一步 —— 崩溃不再可能留下半套状态。
        返回是否结算成功；记录不存在或读不出来时为 ``False``。

        ``user_id`` 不传则沿用记录自带的 —— 阶段二跑在后台调度器线程上，
        没有请求上下文可问，记录本身是唯一的归属依据。
        """
        import time as _time

        if status not in (STATUS_WIN, STATUS_LOSS, STATUS_UNRESOLVED):
            status = STATUS_UNRESOLVED
        try:
            from pa_agent.storage.experience_repo import get_entry

            # user_id 为空时先不过滤地读出来：归属就在这条记录里，
            # 而归属又决定了用什么 user_id 去查 —— 不这样就永远查不到自己。
            content = get_entry(entry_id, user_id=user_id or None)
            if content is None:
                self._log.warning("experience finalize: %s not found", entry_id)
                return False
            owner = self._owner(user_id or str(content.get("user_id") or ""))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("experience finalize read failed for %s: %s", entry_id, exc)
            return False

        content["status"] = status
        if status in (STATUS_WIN, STATUS_LOSS):
            content["result"] = status
        else:
            content.pop("result", None)
        if pnl_pct is not None:
            content["pnl_pct"] = round(float(pnl_pct), 2)
        else:
            content.pop("pnl_pct", None)
        if bars_seen is not None:
            content["bars_seen"] = int(bars_seen)
        content["resolved_ts_ms"] = int(
            resolved_ts_ms if resolved_ts_ms is not None else _time.time() * 1000
        )
        self._upsert(
            content,
            cycle_position=str(content.get("cycle_position") or "trending_tr"),
            status=status,
            symbol=str(content.get("symbol") or ""),
            timeframe=str(content.get("timeframe") or "1h"),
            user_id=owner,
            entry_id=entry_id,
        )
        return True

    def update_pending_progress(self, entry_id: str, *, bars_seen: int) -> bool:
        """记录一条仍是 pending 的案例已经走过多少根 K 线。"""
        try:
            from pa_agent.storage.experience_repo import get_entry

            content = get_entry(entry_id, user_id=None)
            if content is None:
                return False
            content["bars_seen"] = int(bars_seen)
            self._upsert(
                content,
                cycle_position=str(content.get("cycle_position") or "trending_tr"),
                status=STATUS_PENDING,
                symbol=str(content.get("symbol") or ""),
                timeframe=str(content.get("timeframe") or "1h"),
                user_id=str(content.get("user_id") or ""),
                entry_id=entry_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self._log.warning("experience progress update failed for %s: %s", entry_id, exc)
            return False

    def attach_review(
        self,
        entry_id: str,
        payload: dict[str, Any],
        *,
        model: str = "",
        verdict: str = "",
        reusable_criteria: str = "",
        source: str = "llm",
        user_id: str = "",
    ) -> bool:
        """把一次 LLM 复盘挂到条目上（P3）。

        独立表，因此**可以重跑并留历史**。不改变条目本身的状态与时序。
        """
        try:
            from pa_agent.storage.experience_repo import attach_review as _attach

            return _attach(
                payload, entry_id=entry_id, user_id=self._owner(user_id),
                model=model, source=source, verdict=verdict,
                reusable_criteria=reusable_criteria,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("experience review attach failed for %s: %s", entry_id, exc)
            return False

    def list_pending(
        self, limit: int = 100, *, user_id: str | None = ""
    ) -> list[tuple[str, dict[str, Any]]]:
        """返回 ``(entry_id, content)``，按创建时间**由旧到新**。

        结算必须从最旧的开始：先给等得最久的案例定性，才不会让它们永远排不上。
        """
        try:
            from pa_agent.storage.experience_repo import list_entries

            rows = list_entries(
                user_id=self._owner(user_id), status=STATUS_PENDING, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("experience list_pending failed: %s", exc)
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        for row in reversed(list(rows)):          # 查询按 ts 倒序，反转成由旧到新
            content = _loads(row.get("content_json"))
            if content is None:
                continue
            out.append((str(row.get("entry_id") or ""), content))
        return out



def resolve_exit(
    bars: list[dict[str, Any]],
    *,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    is_long: bool = True,
) -> dict[str, Any] | None:
    """Resolve a plan against subsequent bars, **plus what happened on the way**.

    返回首个触及 TP/SL 那根 bar 及其之前的全部信息：

    ``{result, pnl_pct, exit_index, bars_to_exit, mfe_pct, mae_pct, touched}``

    ``touched`` 为 False 表示窗口内两个价位都没碰到（未决终态），此时
    ``exit_index`` 为 None，MFE/MAE 覆盖**全部** ``bars``。

    **MFE/MAE 只统计到出场那根为止（含）**：拿入场后全部 K 线算最大浮盈，
    会把出场之后的行情算进这笔交易头上，于是「TP 后又回落」的 loss 会被
    误判成「先大赚过」，运气成分完全失真。

    同一根 bar 同时触及 TP 与 SL 时**按止损计** —— OHLC 无法还原 intrabar
    路径，按乐观计会把经验库偏向虚高胜率。
    """
    if not bars or entry_price <= 0:
        return None
    ordered = sorted(bars, key=lambda b: b.get("ts_open", 0))

    def _excursion(window: list[dict[str, Any]]) -> tuple[float, float]:
        """(最大浮盈%, 最大回撤%)，均以 entry 为基准，回撤为非负数。"""
        hi = lo = None
        for b in window:
            h, l = b.get("high"), b.get("low")
            if h is not None:
                hi = h if hi is None else max(hi, h)
            if l is not None:
                lo = l if lo is None else min(lo, l)
        if hi is None or lo is None:
            return 0.0, 0.0
        mfe = (hi - entry_price) / entry_price * 100.0 if is_long else (entry_price - lo) / entry_price * 100.0
        mae = (entry_price - lo) / entry_price * 100.0 if is_long else (hi - entry_price) / entry_price * 100.0
        return max(0.0, mfe), max(0.0, mae)

    for i, bar in enumerate(ordered):
        high, low = bar.get("high"), bar.get("low")
        if high is None or low is None:
            continue
        hit_tp = high >= take_profit_price if is_long else low <= take_profit_price
        hit_sl = low <= stop_loss_price if is_long else high >= stop_loss_price
        if not (hit_tp or hit_sl):
            continue
        if hit_sl:
            result, pnl = "loss", -abs(stop_loss_price - entry_price) / entry_price * 100.0
        else:
            result, pnl = "win", abs(take_profit_price - entry_price) / entry_price * 100.0
        mfe, mae = _excursion(ordered[: i + 1])
        return {
            "result": result,
            "pnl_pct": pnl,
            "exit_index": i,
            "bars_to_exit": i + 1,
            "mfe_pct": mfe,
            "mae_pct": mae,
            "touched": True,
        }

    mfe, mae = _excursion(ordered)
    return {
        "result": None,
        "pnl_pct": None,
        "exit_index": None,
        "bars_to_exit": len(ordered),
        "mfe_pct": mfe,
        "mae_pct": mae,
        "touched": False,
    }


def evaluate_outcome(
    bars: list[dict[str, Any]],
    *,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    is_long: bool = True,
) -> tuple[str, float] | None:
    """Resolve a plan against subsequent bars.

    Returns ``(result, pnl_pct)`` for the first bar that touches TP or SL, or
    ``None`` while neither has been reached. Thin wrapper over :func:`resolve_exit`
    so the excursion maths can never drift from the exit rule.
    """
    info = resolve_exit(
        bars, entry_price=entry_price, take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price, is_long=is_long,
    )
    if info is None or not info["touched"]:
        return None
    return info["result"], info["pnl_pct"]


__all__ = ["ExperienceWriter", "evaluate_outcome", "resolve_exit", "_KNOWN_CYCLES"]

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
    user_id: str = "",
) -> Optional[str]:
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
        user_id=user_id,
    )


def _loads(raw: Any) -> Optional[dict]:
    """解析 content_json。坏行跳过，不让单条脏数据毁掉整批。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


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
