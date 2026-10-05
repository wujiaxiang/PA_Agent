"""会话上下文解析：把「本次请求属于哪个标签页」这件事统一起来。

**身份来源**：前端把 UUID 存进 ``sessionStorage``，每次请求带 ``X-Session-Id``。
不能用 Cookie —— Cookie 同源共享，同一浏览器的所有 tab 拿到同一个 id，
「一个 tab 一个会话」直接失效。

**向后兼容**：没有该请求头时一律回落到全局 ``ctx.settings``，行为与改造前
完全一致。因此本模块可以逐个路由接入，不需要一次性切换。

分层语义见 ``docs/SESSION_STORAGE_DESIGN.md`` §2.1：游标是**会话级**，
增量锚点必须按会话隔离，否则 A tab 会捞到 B tab 标的的上一轮上下文。

**本模块还承担会话生命周期**（2026-10-06 新增）：

* :func:`current_session_id` —— ContextVar 形式的「本次请求是谁」。各路由不再
  各自调一次 :func:`session_id_of`（消掉散落的重复解析）。
* :func:`session_lifecycle_middleware` —— 每个 ``/api`` 请求刷新空闲计时，
  并按 600s 限流续期 ``sessions`` 快照。**它修的是「刷新后丢游标」**：
  在此之前，``expires_at`` 的唯一续期点是 ``POST /api/subscribe``，而前端 boot
  序列（``loadSettings`` → ``loadBars``）从不调它。
"""
from __future__ import annotations

import logging
import threading
import time
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("pa_agent.web.session")

#: 请求头名。前端 sessionStorage 里的 UUID。
SESSION_HEADER = "X-Session-Id"

#: query 参数名。原生 EventSource 带不了 header，只能走 query。
SESSION_QUERY_PARAM = "sid"

#: 单个 session_id 的长度上限。防御性：session_id 会进日志与 SQL，超长无意义。
_MAX_ID_LEN = 64

#: 快照续期的最小间隔（秒）。中间件据此限流，见
#: :func:`session_lifecycle_middleware`。
RENEW_INTERVAL_S = 600.0

#: 本次请求的会话身份。由 :func:`session_lifecycle_middleware` 写入。
#: 默认空串 = 「无会话」，与改造前「请求头缺失」的语义完全一致。
_current_session_id: ContextVar[str] = ContextVar("pa_session_id", default="")


def current_session_id() -> str:
    """本次请求的 session_id（已过 :func:`sanitize_session_id` 校验）。

    中间件之外调用（后台线程、GC）拿到的恒为 ``""`` —— ContextVar 不跨线程，
    这是**有意**的：后台任务不该「碰巧」继承某个请求的会话身份。
    """
    return _current_session_id.get()


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


def global_cursor(ctx: Any) -> tuple[str, str, str]:
    """全局 settings 里的游标 —— 会话游标缺失时的回落值。"""
    symbol = timeframe = exchange = ""
    try:
        symbol = ctx.settings.general.last_symbol or ""
        timeframe = ctx.settings.general.last_timeframe or ""
        exchange = getattr(ctx.settings.general, "last_tradingview_exchange", "") or ""
    except AttributeError:
        pass
    return symbol, timeframe, exchange


def session_cursor_of(session_id: str) -> tuple[str, str, str] | None:
    """本会话的游标，**没有则返回 ``None``**（而不是回落全局）。

    与 :func:`resolve_view` 的区别正是「回落与否」：后者给的是可直接用的
    ``(symbol, timeframe, exchange)``，调用方分不清「这就是会话的游标」与
    「会话没游标，只好用全局的」。而这个区别对用户是**可见的** —— 会话过期后
    刷新，前端拿到的是**别人的/出厂的**标的（实测 XAUUSD/15m），不是空白。
    静默回落会被读成「数据丢了」，所以 :func:`GET /api/settings` 用本函数把
    这件事显式标出来（``_session_cursor_missing``）。

    判定逻辑与 ``resolve_view`` 完全同源（热层 → 快照），不另写一份。
    """
    if not session_id:
        return None
    # ── 会话游标：内存热层 → SQLite 快照 ────────────────────────────────
    try:
        from pa_agent.storage.ephemeral import get_registry

        state = get_registry().peek(session_id)
        if state is not None and state.cursor.symbol and state.cursor.timeframe:
            return state.cursor.as_tuple()
    except Exception as exc:  # noqa: BLE001
        logger.debug("registry cursor lookup failed for %s: %s", session_id, exc)

    try:
        from pa_agent.storage import sessions as sess_repo

        row = sess_repo.get_session(session_id)
        if row and row.get("symbol") and row.get("timeframe"):
            return (row["symbol"], row["timeframe"], row.get("exchange") or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("session snapshot lookup failed for %s: %s", session_id, exc)
    return None


def resolve_view(ctx: Any, session_id: str) -> tuple[str, str, str]:
    """Resolve ``(symbol, timeframe, exchange)`` for this request.

    优先取**会话游标**（L3）；无会话或游标为空时回落全局 settings（保持
    单标签页旧行为不变）。

    任何异常都回落，绝不让会话解析失败导致 K 线出不来。

    **注意这里用 ``get_or_create`` 而不是 ``peek``**：读游标的同时也是一次
    「这个 tab 还活着」的证据，热层要靠它续命（``GET /api/settings`` 与
    ``/api/bars`` 都经由这里）。真正不能有副作用的读走
    :func:`session_cursor_of`。
    """
    fallback = global_cursor(ctx)
    if not session_id:
        return fallback

    try:
        from pa_agent.storage.ephemeral import get_registry

        state = get_registry().get_or_create(session_id)
        if state.cursor.symbol and state.cursor.timeframe:
            return (
                state.cursor.symbol,
                state.cursor.timeframe,
                state.cursor.exchange or fallback[2],
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
                row.get("exchange") or fallback[2],
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("session snapshot lookup failed for %s: %s", session_id, exc)

    return fallback


def bind_session(request: Any, *, user_id: str = "") -> str:
    """Ensure a session snapshot row exists for this request.

    返回 session_id（无有效请求头时返回空串）。快照是**缓存级**：带 TTL，
    过期即清，游标可恢复而运行时开关刻意不还原。

    ``user_id`` 不再默认 ``"default"``：那是历史遗留的第三种身份，与
    ``db.DEFAULT_USER_ID``（``"admin"``）和 ``auth_ctx`` 的匿名回落各说各话，
    ``users`` 表里压根不存在 ``default`` 这个用户。三方分裂的直接后果是
    **按用户过滤时永远查不到数据**。留空则走 ``current_user_id(request)``
    ——「本次请求是谁」的唯一答案（令牌 > 直传头 > 匿名回落）。

    显式传 ``user_id`` 的调用方（测试、将来的系统级任务）保留优先权。
    """
    sid = session_id_of(request)
    if not sid:
        return ""
    if not user_id:
        try:
            from web.api.auth_ctx import current_user_id

            user_id = current_user_id(request)
        except Exception as exc:  # noqa: BLE001
            logger.debug("user identity unavailable, using default: %s", exc)
            from pa_agent.storage.db import DEFAULT_USER_ID

            user_id = DEFAULT_USER_ID
    try:
        from pa_agent.storage import sessions as sess_repo

        sess_repo.ensure_session(sid, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("bind_session failed for %s: %s", sid, exc)
    return sid


# ── 生命周期中间件 ─────────────────────────────────────────────────────────────
#: ``{session_id: 上次续期时刻}``，只用于**限流**，不是真源（真源是
#: ``sessions.expires_at`` / ``SessionState.last_touch``）。
#:
#: **为什么必须有个进程内表**：限流判据不能读 ``SessionState.last_touch`` ——
#: 那是**每次读都会刷新的**（``resolve_view`` → ``get_or_create``），拿它当
#: 「上次续期时刻」的话每请求都判成「刚续过」，续期永远不会发生（这正是
#: 「无人续期」的另一种写法）。也不能每次请求读一次 ``sessions.expires_at``：
#: ``/api/bars`` 是 5 秒轮询，那是每 5 秒一次 SQLite 读。
_renewed_at: dict[str, float] = {}
_renew_lock = threading.Lock()


def _claim_renewal(session_id: str, now: float) -> bool:
    """限流闸门：距上次续期超过 :data:`RENEW_INTERVAL_S` 才放行一次。

    返回 ``True`` 表示**本次调用负责续期**（无论续期本身是否成功 —— 失败也不
    该在 5 秒后立刻重试，那正是把一次 IO 故障放大成持续写压的路径）。
    """
    with _renew_lock:
        last = _renewed_at.get(session_id, 0.0)
        if now - last < RENEW_INTERVAL_S:
            return False
        _renewed_at[session_id] = now
        # 顺带清陈旧条目：限流表本身不能变成第二个只涨不消的注册表。
        if len(_renewed_at) > 256:
            cutoff = now - RENEW_INTERVAL_S * 4
            for k in [k for k, t in _renewed_at.items() if t < cutoff]:
                _renewed_at.pop(k, None)
        return True


def reset_renewal_state_for_tests() -> None:
    """清空限流表。测试专用（每个测试都该从「从没续过」开始）。"""
    with _renew_lock:
        _renewed_at.clear()


async def session_lifecycle_middleware(request: Any, call_next):
    """**每个请求刷新会话空闲计时**，并按 :data:`RENEW_INTERVAL_S` 限流续期快照。

    这是 ``sessions.expires_at`` 在生产路径上的**唯一续期点**。此前它只有
    ``POST /api/subscribe`` 一条，而前端 boot 序列从不调它 ⇒ 一个订阅后一直挂
    着不切品种的 tab，快照行在 TTL 到期后被读侧视作不存在，F5 就回落到出厂
    种子（实测 XAUUSD/15m）—— 用户看的是 BTCUSDT，刷新后变成 XAUUSD。

    三件事，按开销从大到小：

    1. **解析 sid**：走现成的 :func:`session_id_of`（内含
       :func:`sanitize_session_id`），**不另写一套校验** —— 两套校验迟早漂移，
       而漂移的那一套就是绕过字符集限制的后门。
    2. **挂 ContextVar**：供下游用 :func:`current_session_id` 读，消掉各路由
       各自解析一遍的重复。请求结束必须还原，否则同一 worker Task 上后续请求
       会继承上一次的绑定（与 ``bind_user_settings_middleware`` 同理）。
    3. **续期**：热层 :meth:`SessionRegistry.touch`（纯内存，每请求）+ 快照
       ``sessions.touch_session``（SQLite 写，**限流**，见
       :func:`_claim_renewal`）。

    **为什么限流是硬需求**：``/api/bars`` 每 5 秒轮询一次。不限流就是每 5 秒
    一次 SQLite 写；12 个活跃 tab ≈ 2.4 万次写/天，而且全是「把 expires_at 推
    到 now+24h」这种毫无信息量的写 —— WAL 会被它撑大，真正该写的事（游标变更）
    反而更频繁地被 checkpoint 打断。

    **热层用 ``touch`` 而不是 ``get_or_create``**：本中间件跑在**每一个**
    ``/api`` 请求上，包括健康探针和脚本。用 ``get_or_create`` 等于给任意调用方
    凭空造会话条目（注册表是用来 LRU 淘汰的，凭空造就绕过了淘汰的语义）。
    ``touch`` 只刷新已存在且未过期的会话，不存在就不管。

    **静默失败**：任何一步抛异常都只记 warning 并放行 —— 会话续期是缓存层，
    绝不能让它把请求搞挂。

    **只对 ``/api`` 生效**：与 :func:`bind_user_settings_middleware` 同一口径。
    静态资源不读会话，给它们续期纯属浪费。
    """
    if not getattr(getattr(request, "url", None), "path", "").startswith("/api"):
        return await call_next(request)

    try:
        sid = session_id_of(request)
    except Exception:  # noqa: BLE001 - 身份解析失败不该让请求 500
        sid = ""

    token = _current_session_id.set(sid) if sid else None
    try:
        if sid:
            now = time.time()
            try:
                from pa_agent.storage.ephemeral import get_registry

                get_registry().touch(sid)      # 纯内存：每请求刷新空闲计时
            except Exception as exc:  # noqa: BLE001
                logger.debug("hot-layer session touch failed for %s: %s", sid, exc)
            if _claim_renewal(sid, now):
                try:
                    from pa_agent.storage import sessions as sess_repo

                    # 过期行不会被 touch_session 复活（其 SQL 带 expires_at > ?）。
                    # 「真的还在用」才续得上；「已经过期」就让它过期。
                    sess_repo.touch_session(sid)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("session snapshot renewal failed for %s: %s", sid, exc)
        return await call_next(request)
    finally:
        if token is not None:
            _current_session_id.reset(token)
