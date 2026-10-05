"""会话上下文解析：把「本次请求属于哪个标签页」这件事统一起来。

**身份来源**：前端把 UUID 存进 ``sessionStorage``，每次请求带 ``X-Session-Id``。
不能用 Cookie —— Cookie 同源共享，同一浏览器的所有 tab 拿到同一个 id，
「一个 tab 一个会话」直接失效。

**向后兼容**：没有该请求头时一律回落到全局 ``ctx.settings``，行为与改造前
完全一致。因此本模块可以逐个路由接入，不需要一次性切换。

分层语义见 ``docs/SESSION_STORAGE_DESIGN.md`` §2.1：游标是**会话级**，
增量锚点必须按会话隔离，否则 A tab 会捞到 B tab 标的的上一轮上下文。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("pa_agent.web.session")

#: 请求头名。前端 sessionStorage 里的 UUID。
SESSION_HEADER = "X-Session-Id"

#: query 参数名。原生 EventSource 带不了 header，只能走 query。
SESSION_QUERY_PARAM = "sid"

#: 单个 session_id 的长度上限。防御性：session_id 会进日志与 SQL，超长无意义。
_MAX_ID_LEN = 64


def sanitize_session_id(raw: str) -> str:
    """校验并归一 session_id。header 与 query 两个入口共用。

    规则：非空、长度上限、只允许 ``[A-Za-z0-9_-]``。session_id 会拼进 SQL
    （虽走参数化）并出现在日志里，收紧字符集可避免日志注入。
    """
    raw = (raw or "").strip()
    if not raw or len(raw) > _MAX_ID_LEN:
        return ""
    if not all(c.isalnum() or c in "-_" for c in raw):
        return ""
    return raw


def session_id_of(request: Any) -> str:
    """从请求取 session_id：**header 优先，其次 query ``sid``**。

    为什么需要 query 这一路：浏览器原生 ``EventSource`` **无法设置请求头**
    （这是硬限制，不是配置问题）。SSE 端点若靠 ``X-Session-Id`` 区分会话，
    服务端永远拿不到值，会静默退回全局游标 —— 这正是 SSE 多会话隔离方案
    A 不可实施的原因。追问 SSE 也受同一限制。

    两路都过同一个 :func:`sanitize_session_id`，避免 query 成为绕过字符集
    校验的后门。
    """
    for getter in ("headers", "query_params"):
        try:
            src = getattr(request, getter, None)
            if src is None:
                continue
            raw = (
                src.get(SESSION_HEADER, "")
                if getter == "headers"
                else src.get(SESSION_QUERY_PARAM, "")
            ) or ""
            got = sanitize_session_id(raw)
            if got:
                return got
        except Exception:  # noqa: BLE001 - 任何异常都视作「无会话」
            continue
    return ""


def resolve_view(ctx: Any, session_id: str) -> tuple[str, str, str]:
    """Resolve ``(symbol, timeframe, exchange)`` for this request.

    优先取**会话游标**（L3）；无会话或游标为空时回落全局 settings（保持
    单标签页旧行为不变）。

    任何异常都回落，绝不让会话解析失败导致 K 线出不来。
    """
    # ── 回落值：全局设置（改造前的唯一来源）────────────────────────────
    symbol = timeframe = exchange = ""
    try:
        symbol = ctx.settings.general.last_symbol or ""
        timeframe = ctx.settings.general.last_timeframe or ""
        exchange = getattr(ctx.settings.general, "last_tradingview_exchange", "") or ""
    except AttributeError:
        pass

    if not session_id:
        return symbol, timeframe, exchange

    # ── 会话游标：内存热层 → SQLite 快照 ────────────────────────────────
    try:
        from pa_agent.storage.ephemeral import get_registry

        state = get_registry().get_or_create(session_id)
        if state.cursor.symbol and state.cursor.timeframe:
            return (
                state.cursor.symbol,
                state.cursor.timeframe,
                state.cursor.exchange or exchange,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("registry cursor lookup failed for %s: %s", session_id, exc)

    try:
        from pa_agent.storage import sessions as sess_repo

        row = sess_repo.get_session(session_id)
        if row and row.get("symbol") and row.get("timeframe"):
            return (
                row["symbol"],
                row["timeframe"],
                row.get("exchange") or exchange,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("session snapshot lookup failed for %s: %s", session_id, exc)

    return symbol, timeframe, exchange


def bind_session(request: Any, *, user_id: str = "default") -> str:
    """Ensure a session snapshot row exists for this request.

    返回 session_id（无有效请求头时返回空串）。快照是**缓存级**：带 TTL，
    过期即清，游标可恢复而运行时开关刻意不还原。
    """
    sid = session_id_of(request)
    if not sid:
        return ""
    try:
        from pa_agent.storage import sessions as sess_repo

        sess_repo.ensure_session(sid, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("bind_session failed for %s: %s", sid, exc)
    return sid
