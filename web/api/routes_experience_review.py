"""LLM post-mortem review for a settled or pending experience entry.

追问 asks "why did you do that?" against the analysis that just finished.
复盘 asks the harder question: given the **full recorded context** — what we
saw, what we concluded, the price window, and how it actually turned out —
was the reasoning sound?

That requires the entry to be self-describing. Without
``analysis_context`` + ``bars_snapshot`` the model can only stare at a ``pnl_pct``
number and produce hindsight, which is worse than no review at all.

Design mirrors ``routes_chat`` (SSE + bounded queue) so the UI can stream
reasoning/content into the same bubble component the 追问 tab already uses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from pa_agent.config.paths import EXPERIENCE_DIR
from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)
router = APIRouter(tags=["experience"])

_REVIEW_QUEUE_MAXSIZE = 128

_SYSTEM = """你是一位交易复盘分析师。你会拿到一条交易计划的完整档案：

1. **当时的判断**：阶段一的市场诊断（周期位置、形态、方向、置信度、闸门结果）
2. **当时的决策**：阶段二的下单方式、入场/止损/止盈价位、交易置信度、推理
3. **K 线窗口**：入场前后各若干根 K 线
4. **实际结果**：pending（还没走完）/ win / loss / unresolved（窗口内未触及任一价位），以及盈亏百分比

请严格按以下结构输出，用中文：

## 结论
一句话：这次是「判断对了但运气不好」「判断本身有误」还是「行情不可判定」。

## 归因
- **对的部分**：哪些判断被结果证实了（形态识别、周期位置、方向）。
- **错的部分**：哪些判断与走势相悖，具体错在哪一步。

## 当时能否预见
站在**入场那一刻**（不看结果），依据档案里的 K 线窗口，这个 setup 的胜算如何？
请指出当时就该看到的警示信号。

## 改进建议
具体、可执行的一条到三条（例如：止损该放哪、TP1 是否该提前、什么形态该直接放弃）。
每条都要能落到「下次遇到同类 setup 时怎么判」。

## 下次同类 setup 的判据
用两三行总结一个可复用的判断标准。

注意：
- 不要事后诸葛亮。复盘的价值在于指出**当时**可观察的信号，而不是用结果倒推理由。
- unresolved（未触及任一价位）是正常结局，不代表判断错误 —— 它只说明在给定的
  N 根 K 线窗口内价格没走到。评估它时要评价的是「这个窗口长度设置是否合理」。
- 不要给出投资建议，只做推理质量评估。"""


def _find_entry(record_id: str) -> tuple[Path, dict[str, Any]]:
    """Locate an entry by basename under the retrievable library dirs."""
    if not record_id or "/" in record_id or ".." in record_id:
        raise HTTPException(status_code=400, detail="非法 record_id")
    root = Path(EXPERIENCE_DIR)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="经验库不存在")
    from pa_agent.records.experience_writer import STATUS_DIRS

    for cycle_dir in sorted(root.iterdir()):
        if not cycle_dir.is_dir() or cycle_dir.name.startswith("."):
            continue
        for sub in STATUS_DIRS.values():
            p = cycle_dir / sub / record_id
            if p.is_file():
                try:
                    return p, json.loads(p.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    break
    raise HTTPException(status_code=404, detail="记录不存在")


def _fmt_bars(bars: list[dict[str, Any]], anchor_ms: int) -> str:
    """Render the snapshot, marking which side of the entry each bar is on."""
    if not bars:
        return "（未记录 K 线窗口）"
    lines = []
    for b in bars:
        mark = "入场后" if int(b.get("ts_open") or 0) > anchor_ms else "入场前"
        lines.append(
            f"  [{mark}] O={b.get('o')} H={b.get('h')} L={b.get('l')} C={b.get('c')}"
        )
    return "\n".join(lines)


def _fmt_ctx(ctx: dict[str, Any], limit: int = 4000) -> str:
    """Flatten the recorded context into prompt-ready text."""
    parts: list[str] = []
    s1 = ctx.get("stage1") or {}
    if s1:
        parts.append("【阶段一诊断】")
        for k in ("cycle_position", "alternative_cycle_position", "direction",
                  "detected_patterns", "diagnosis_confidence", "entry_setup",
                  "trend", "market_cycle", "next_cycle", "support_resistance",
                  "climax_risk", "htf_context", "gate_result"):
            if k in s1 and s1[k] not in (None, "", [], {}):
                parts.append(f"  {k}: {s1[k]}")
        for k in ("bar_by_bar_summary",):
            if s1.get(k):
                parts.append(f"  {k}: {str(s1[k])[:1200]}")
    s2 = ctx.get("stage2") or ctx.get("stage2_flat") or {}
    if s2:
        parts.append("\n【阶段二决策】")
        for k in ("order_type", "order_direction", "entry_price",
                  "stop_loss_price", "take_profit_price", "take_profit2_price",
                  "trade_confidence", "risk_reward_ratio", "estimated_win_rate",
                  "diagnosis_summary", "reasoning", "decision", "terminal"):
            if k in s2 and s2[k] not in (None, "", [], {}):
                parts.append(f"  {k}: {s2[k]}")
    text = "\n".join(parts)
    return text[:limit]


def _build_prompt(entry: dict[str, Any]) -> list[dict[str, str]]:
    status_label = {
        "pending": "待验证（K 线尚未走完，暂无法判定输赢）",
        "win": "盈利", "loss": "亏损",
        "unresolved": "未触及（判定窗口内既没到止盈也没到止损）",
    }.get(str(entry.get("status") or ""), str(entry.get("status") or "未知"))

    pnl = entry.get("pnl_pct")
    pnl_text = f"{pnl:+.2f}%" if isinstance(pnl, (int, float)) else "无"
    anchor = int(entry.get("entry_ts_open_ms") or 0)
    bars_seen = entry.get("bars_seen")

    body = f"""【交易计划档案】

品种: {entry.get('symbol')}    周期: {entry.get('timeframe')}    交易所: {entry.get('exchange') or '—'}
方向: {'多头' if entry.get('is_long', True) else '空头'}    周期位置: {entry.get('cycle_position') or '—'}
识别形态: {', '.join(entry.get('detected_patterns') or []) or '—'}
置信度: {entry.get('confidence', '—')}

入场价: {entry.get('entry_price')}    止损: {entry.get('stop_loss_price')}    止盈: {entry.get('take_profit_price')}

实际结果: {status_label}    盈亏: {pnl_text}    已走 K 线数: {bars_seen if bars_seen is not None else '—'}

{_fmt_ctx(entry.get('analysis_context') or {})}

【K 线窗口】
{_fmt_bars(entry.get('bars_snapshot') or [], anchor)}
"""
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": body + "\n\n请对这次交易做复盘。"},
    ]


@router.get("/experience/review/stream")
async def experience_review_stream(
    request: Request,
    record_id: str = Query(..., description="经验条目的文件名（basename）"),
):
    """SSE stream of an LLM post-mortem for one experience entry."""
    _path, entry = _find_entry(record_id)
    ctx = request.app.state.ctx

    client = getattr(ctx, "client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="模型客户端不可用")

    history = _build_prompt(entry)
    queue: asyncio.Queue = asyncio.Queue(maxsize=_REVIEW_QUEUE_MAXSIZE)
    loop = asyncio.get_running_loop()
    cancel = CancelToken()

    async def emit(name: str, data: str) -> None:
        await queue.put({"event": name, "data": data})

    def worker() -> None:
        try:
            def on_reasoning(t: str) -> None:
                loop.call_soon_threadsafe(
                    lambda: queue.put_nowait({"event": "reasoning", "data": t})
                    if not queue.full() else None)

            def on_content(t: str) -> None:
                loop.call_soon_threadsafe(
                    lambda: queue.put_nowait({"event": "content", "data": t})
                    if not queue.full() else None)

            # 注意是 stream_chat 而非 chat：只有前者接受 token 回调。
            reply = client.stream_chat(
                history,
                on_reasoning_token=on_reasoning,
                on_content_token=on_content,
                cancel_token=cancel,
            )
            loop.call_soon_threadsafe(
                queue.put_nowait, {"event": "done", "data": ""})
            _ = reply
        except Exception as exc:  # noqa: BLE001
            logger.warning("experience review failed for %s: %s", record_id, exc)
            loop.call_soon_threadsafe(
                queue.put_nowait, {"event": "error", "data": str(exc)[:300]})
            loop.call_soon_threadsafe(
                queue.put_nowait, {"event": "done", "data": ""})

    thread = threading.Thread(target=worker, name="experience-review", daemon=True)
    thread.start()

    async def gen():
        try:
            while True:
                evt = await queue.get()
                yield evt
                if evt["event"] in ("done", "error"):
                    break
        finally:
            cancel.set()

    return EventSourceResponse(gen())