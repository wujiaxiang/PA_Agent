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

from pa_agent.records.review_insights import INSIGHT_CODES as _INSIGHT_CODES, render_insights
from pa_agent.records.review_spec import parse_review, spec_hint

import asyncio
import json
import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

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
**只能从下面这份清单里选 1-3 项，逐字写出枚举码，不要写句子**：

{TP_TOO_WIDE} {TP_TOO_CLOSE} {STOP_TOO_TIGHT} {STOP_TOO_WIDE}
{ENTRY_TOO_LATE} {ENTRY_TOO_EARLY} {COUNTER_CYCLE} {PATTERN_UNCONFIRMED}
{NO_VALIDATION} {EXPIRED}

若都不适用，写 NONE。

注意：判据由系统用该笔交易的实际数值生成，你只负责选。
所以**不要在这一节写任何解释性文字或句子** —— 那部分不会被使用。

注意：
- 不要事后诸葛亮。复盘的价值在于指出**当时**可观察的信号，而不是用结果倒推理由。
- unresolved（未触及任一价位）是正常结局，不代表判断错误 —— 它只说明在给定的
  N 根 K 线窗口内价格没走到。评估它时要评价的是「这个窗口长度设置是否合理」。
- 不要给出投资建议，只做推理质量评估。"""

_SYSTEM += spec_hint()
_SYSTEM += "\n\n## 可选判据枚举码\n\n" + " ".join(_INSIGHT_CODES)


def _find_entry(record_id: str, *, user_id: str) -> dict[str, Any]:
    """按 ``entry_id`` 取经验条目。

    **只查库**（2026-10-05 起文件布局已废弃）。``user_id`` 必传：复盘里含
    当时的判断与推理，A 用户不该读到 B 用户的档案。
    """
    from pa_agent.storage.experience_repo import get_entry

    if not record_id or "/" in record_id or ".." in record_id:
        raise HTTPException(status_code=400, detail="非法 record_id")
    entry = get_entry(record_id, user_id=user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="记录不存在或不属于当前用户")
    return entry


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


def _request_user(request: Request) -> str:
    """本次请求的 user_id，取不到回落默认用户（身份判定本身不抛）。"""
    try:
        from web.api.auth_ctx import current_user_id

        return current_user_id(request)
    except Exception as exc:  # noqa: BLE001
        logger.debug("user identity unavailable, using default: %s", exc)
        from pa_agent.storage.db import DEFAULT_USER_ID

        return DEFAULT_USER_ID


def _extract_codes(raw: str) -> list[str]:
    """从模型输出里取枚举码。

    只认**逐字出现**的枚举码 —— 不做模糊匹配、不从自然语言里推断语义。
    任何"聪明的"解析都会变成给模型开后门：它能让一段自由文本被当成若干条
    有效枚举。
    """
    from pa_agent.records.review_insights import INSIGHT_CODES

    upper = (raw or "").upper()
    found = [c for c in INSIGHT_CODES if c in upper]
    return found


def _facts_for(entry: dict[str, Any]) -> dict[str, Any]:
    """本笔交易的确定性数值（供枚举模板填槽）。取不到就用程序层那份。"""
    from pa_agent.storage.experience_repo import program_review

    try:
        row = program_review(str(entry.get("entry_id") or ""), user_id=str(
            entry.get("user_id") or ""))
        if isinstance(row, dict) and isinstance(row.get("payload"), dict):
            return row["payload"]
    except Exception:  # noqa: BLE001
        logger.warning("cannot load program facts for insight rendering", exc_info=True)
    entry_px = float(entry.get("entry_price") or 0.0)
    sl = float(entry.get("stop_loss_price") or 0.0)
    tp = float(entry.get("take_profit_price") or 0.0)
    return {
        "risk_pct": abs(entry_px - sl) / entry_px * 100.0 if entry_px and sl else 0.0,
        "reward_pct": abs(tp - entry_px) / entry_px * 100.0 if entry_px and tp else 0.0,
        "bars_to_exit": entry.get("bars_seen"),
    }


def _persist_review(
    record_id: str, user_id: str, reply: Any, ctx: Any, entry: dict[str, Any] | None = None
) -> None:
    """把复盘结论写进 ``experience_reviews``。

    独立表 → 可以重跑并留历史，且**不改**经验条目本身的状态与时序。
    失败只记 warning：复盘是附加产物，丢一次不该让用户看到报错。
    """
    # **只接受受控枚举**：模型做选择题，句子由 review_insights 用本笔数值生成。
    # 自由文本（content）仍入库、仍给人看，但**永不进提示词** —— 净化器实测
    # 26 个绕过放行 19 个（黑名单挡不住「非命令句的权威断言」）。
    raw = str(getattr(reply, "content", None) or "")
    codes = _extract_codes(raw)
    entry = entry or {}
    facts = _facts_for(entry)
    criteria = render_insights(codes, entry, facts)
    content = raw
    if not content.strip():
        logger.warning("experience review produced empty content; not persisted (%s)", record_id)
        return

    # 按规格解析成结构化字段。**解析失败必须显式失败** —— 曾把 verdict/criteria
    # 留成空串，而渲染层 `if crit or verdict` 恒为假，于是复盘静默地从未进入
    # 任何提示词，且没有任何报错。解析失败照样存全文（人还能读），但 verdict
    # 与 criteria 留空 —— 半截复盘的判据没有意义，放它进提示词等于用垃圾换真值。
    spec = parse_review(content)
    if not spec["parsed"]:
        logger.warning(
            "LLM review does not match spec; stored read-only, verdict left empty "
            "(entry=%s missing=%s)", record_id, spec["missing"],
        )

    try:
        from pa_agent.records.experience_writer import ExperienceWriter

        ok = ExperienceWriter(logger=logger).attach_review(
            record_id,
            {"content": content, "spec": spec,
             "reasoning": str(getattr(reply, "reasoning_content", "") or "")},
            model=str(getattr(ctx.settings.provider, "model", "") or ""),
            verdict=spec["verdict"],
            reusable_criteria=criteria,
            source="llm",
            user_id=user_id,
        )
        if not ok:
            logger.warning("experience review persist failed: %s", record_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("experience review persist error for %s: %s", record_id, exc)


@router.get("/experience/review/stream")
async def experience_review_stream(
    request: Request,
    record_id: str = Query(..., description="经验条目 ID（experience_entries.entry_id）"),
):
    """SSE stream of an LLM post-mortem for one experience entry."""
    entry = _find_entry(record_id, user_id=_request_user(request))
    ctx = request.app.state.ctx

    client = getattr(ctx, "client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="模型客户端不可用")

    uid = _request_user(request)
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
            # 落盘必须在 done **之前**、且由后端做：SSE 一断前端就没了，
            # 依赖前端回报等于「用户关页面即丢失这次复盘」。
            _persist_review(record_id, uid, reply, ctx, entry)
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