"""SSE route for post-analysis free-chat (追问).

**多标签页彻底隔离**（本文件改造的核心）
========================================
追问历史原先按 ``record_id|symbol|timeframe|快照标志`` 分桶。两个标签页回看
**同一条**记录时会拿到同一份 ``FreeChatSession``：A tab 问的「止损放哪」会出现在
B tab 的上下文里，互相污染。

分桶键因此**扩键**（不是替换）::

    session_key = f"{thread_key}|{record_key}|{'k' if attach_kline_snapshot else 'n'}"

- ``thread_key``：本标签页身份（``session_id``，见 ``web/api/session_ctx.py``）。
  ``record_id`` 是可由前端任意构造的 query 参数，**不能**当身份用。
- ``record_key``：原有三段（record_id / symbol / timeframe），语义不变。

**为什么必须保留 record 段**（极易踩错，已实测确认）
--------------------------------------------------
``FreeChatSession._cached_prefix`` 在 ``__init__`` 时按 ``base_record`` 构建
一次且**永不变**（``pa_agent/orchestrator/free_chat.py``），``send()`` 每轮把它
原样复制进 history。若把 record 段删掉、只用 session_id 分桶，则「先追问记录 A、
再回看记录 B」时，B 会携带 A 的 stage1/stage2 与 K 线描述 —— **答非所问且没有
任何报错**，比串味更危险。

其余会话化
----------
- 锚点 ``_last_record`` 读写 ``ephemeral.SessionState.last_record``（挂在全局
  ``ctx`` 上时，A tab 的分析结果会成为 B tab 追问的锚点，隔离只是装饰）；
- 订阅比对与历史回退一律走 ``_resolve_view(request, ctx)``（会话游标），不再读
  全局 ``ctx.settings``；
- 断连**只**摘 SSE 队列（``registry.drop_queue``），绝不 ``registry.drop(sid)``：
  浏览器会自动重连，drop 会把追问历史一起清掉。

追问历史的三处落点
================
生成成功后**同步写一份** ``chat_turns``（``pa_agent.storage.chat_repo``）。
此前追问历史只活在内存 ``_chat_sessions`` 与 JSONL sidecar 两处，**都不持久**
（进程重启即丢；sidecar 随分析记录一起被删）。DB 那一行的定位与权威性
（内存热态 / DB 持久态 / DB 优先）见 ``chat_repo`` 的模块 docstring，
这里只强调接线上的三条硬约束：

1. **审计写失败绝不影响追问**。调用点在生成线程里，用户此时已经拿到答案；
   一条 INSERT 失败不该把整次追问变成 ``error`` 事件。
2. **先推 ``done`` 再落库**。SQLite 是 ``busy_timeout=5000``，并发写撞锁时
   会阻塞最多 5 秒；先落库等于让用户在「生成中…」上白等 5 秒，而先推
   ``done`` 最多丢掉「客户端在同一毫秒内重载历史」这一种可见性。
3. **轮次号取自内存会话**（``len(history_full) // 2 + 1``），与
   ``FreeChatSession._turn`` 严格同源。两套计数会让同一段对话在库里出现
   重复轮次，而 ``ix_chat_thread (user_id, thread_key, turn)`` 不是唯一索引
   —— 写重了不报错，只在回读时顺序错乱。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from pa_agent.orchestrator.free_chat import FreeChatSession
from pa_agent.storage import chat_repo
from pa_agent.storage.ephemeral import get_registry
from pa_agent.util.threading import CancelToken

from web.api.routes_data import _resolve_view
from web.api.session_ctx import session_id_of

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

# 追问生成的线程池。**故意保持 2**：per-session 互斥锁已移到事件循环侧（见
# ``_run`` 上方注释），等待中的请求不再占用 worker，把锁留在池里才是死锁源。
_executor = ThreadPoolExecutor(max_workers=2)

# In-memory session cache: keyed by session_key -> {"session": FreeChatSession,
# "last_touch": ts, "lock": asyncio.Lock}
# 通过 TTL 机制（默认 30 分钟无活动）自动清理，避免内存泄漏
_CHAT_SESSION_TTL_SEC = 30 * 60
#: Per-request SSE queue depth (see routes_bars_stream for the same reasoning).
_CHAT_QUEUE_MAXSIZE = 256
_chat_sessions: dict[str, dict] = {}

#: 无 session_id 时的 thread_key 占位（老客户端 / 直连 API / 单测）。
_NO_SESSION = "nosession"

# 后台清理 task 句柄，避免重复启动
_chat_cleanup_task: asyncio.Task | None = None


async def _chat_cleanup_loop():
    """周期清理过期 chat session。"""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [
            k for k, v in _chat_sessions.items()
            if now - v.get("last_touch", 0) > _CHAT_SESSION_TTL_SEC
        ]
        for k in expired:
            entry = _chat_sessions.pop(k, None)
            if entry is not None:
                lock = entry.get("lock")
                # 正在等这把锁的请求只会永远等下去（没有等待者计数）。
                # 解锁让它立刻醒来，醒来后走「取不到会话」的错误分支。
                if isinstance(lock, asyncio.Lock) and lock.locked():
                    try:
                        lock.release()
                    except RuntimeError:  # pragma: no cover - 防御
                        pass
            logger.info("chat session expired, key=%s", k)


@router.on_event("startup")
async def _ensure_chat_cleanup():
    global _chat_cleanup_task
    if _chat_cleanup_task is None or _chat_cleanup_task.done():
        _chat_cleanup_task = asyncio.create_task(_chat_cleanup_loop())


def _touch_session(key: str, session: FreeChatSession, lock=None) -> None:
    """写入/覆盖分桶条目。

    *lock* 是 **asyncio.Lock**（事件循环侧），不是 ``threading.Lock``：见
    ``_run`` 上方关于死锁的说明。
    """
    _chat_sessions[key] = {
        "session": session,
        "last_touch": time.time(),
        # FreeChatSession.send() mutates _turn / _history_full without any
        # internal lock, so two concurrent follow-ups on the same session would
        # interleave history and duplicate turn numbers. One lock per session
        # serialises them.
        "lock": lock if lock is not None else asyncio.Lock(),
    }


def _get_session(key: str) -> FreeChatSession | None:
    entry = _chat_sessions.get(key)
    if entry is None:
        return None
    entry["last_touch"] = time.time()
    return entry["session"]


def _record_matches_subscription(record, symbol: str, timeframe: str) -> bool:
    """True when *record* was produced for the (symbol, timeframe) this tab is on.

    Without this check a follow-up asked after switching instruments silently
    anchored to the *previous* symbol's analysis, so the AI answered about a
    different market than the chart the user was looking at.

    *symbol* / *timeframe* 必须是**本会话游标**（``_resolve_view``）而不是全局
    ``ctx.settings`` —— 否则多标签页下 A tab 的追问会用 B tab 的订阅做比对。
    """
    if record is None:
        return False
    meta = getattr(record, "meta", None)
    if meta is None:
        return True  # no way to compare — keep prior behaviour
    # Only trust a comparison when both sides are real non-empty strings; any
    # other value means we cannot reliably tell, so stay permissive.
    def _mismatch(want: object, got: object) -> bool:
        if not isinstance(want, str) or not isinstance(got, str):
            return False
        want, got = want.strip(), got.strip()
        return bool(want) and bool(got) and want != got

    if _mismatch(symbol, getattr(meta, "symbol", "")):
        return False
    if _mismatch(timeframe, getattr(meta, "timeframe", "")):
        return False
    return True


def _kline_snapshot_fn(ctx, view: tuple[str, str, str] | None = None):
    """Capture current kline snapshot for the chat context.

    *view* = ``(symbol, timeframe, exchange)``，来自本会话游标。追问 prompt 里
    注入的 K 线必须与该 tab 图上的一致；省略 *view* 时沿用数据源当前订阅
    （向后兼容单标签页 / 老调用方）。
    """
    def _inner() -> str:
        try:
            bars_raw = None
            if view and view[0] and view[1]:
                try:
                    bars_raw = ctx.data_source.latest_snapshot(
                        20, exchange=view[2] or None,
                        symbol=view[0], timeframe=view[1],
                    )
                except TypeError:
                    # 老数据源实现的 latest_snapshot 不接受游标入参
                    bars_raw = ctx.data_source.latest_snapshot(20)
            if bars_raw is None:
                bars_raw = ctx.data_source.latest_snapshot(20)
            lines = ["seq  | 开       | 高       | 低       | 收       | 量"]
            for b in reversed(bars_raw[-10:]):
                lines.append(f"{b.seq:>3}  | {b.open:>8.2f} | {b.high:>8.2f} | {b.low:>8.2f} | {b.close:>8.2f} | {b.volume:>.0f}")
            return "\n".join(lines)
        except Exception:
            return ""
    return _inner


def _push(loop, queue: asyncio.Queue, payload: dict) -> None:
    """线程池 → 事件循环的安全投递。

    队列有界（客户端太慢 / 已断开），``put_nowait`` 抛 QueueFull 若直接冒到
    ``call_soon_threadsafe`` 的回调里会变成「Exception in callback」，把生成
    线程的异常面搅浑。这里就地吞掉：丢一个 token 不影响正确性。
    """
    def _do() -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass
    try:
        loop.call_soon_threadsafe(_do)
    except RuntimeError:  # pragma: no cover - 事件循环已关闭（服务停止中）
        pass


def _is_cancelled(exc: BaseException) -> bool:
    """True when *exc* 表示用户取消追问（而非生成失败）。

    ``free_chat.send`` 在取消时抛 ``deepseek_client.CancelledError``，它继承
    ``Exception``，所以会落进 ``except Exception`` —— 不区分就会把「取消」
    当成「失败」，库里不留痕。

    刻意用**类名**判定而不是 isinstance：为一个异常分支在模块顶部引入
    ``deepseek_client`` 的符号并不划算（它虽是 Qt-free 的，但会让「这个模块
    依赖了哪些包」更难读）。类名判定同样能命中测试里注入的同名桩。
    """
    return type(exc).__name__ == "CancelledError"


def _persist_turn(
    *,
    thread_key: str,
    turn_number: int,
    session_id: str,
    record_id: str,
    symbol: str,
    timeframe: str,
    user_text: str,
    assistant: str = "",
    reasoning: str | None = None,
    usage: dict | None = None,
    cancelled: bool = False,
) -> None:
    """把一轮追问落到 ``chat_turns``。**任何失败只记 warning，绝不冒泡。**

    见模块 docstring 的「追问历史的三处落点」。本函数刻意不返回状态：
    调用点在 SSE 生成线程里，返回值除了写日志无处可去，做成布尔只会诱导
    上层以为「可以不落库」。
    """
    try:
        chat_repo.append_turn(
            thread_key=thread_key,
            turn=turn_number,
            user=user_text,
            assistant=assistant,
            session_id=session_id,
            record_id=record_id,
            symbol=symbol,
            timeframe=timeframe,
            reasoning=reasoning,
            usage=usage,
            cancelled=cancelled,
        )
    except Exception:  # noqa: BLE001
        # 双保险：append_turn 本身已吞 DB 异常，这里挡的是它**参数构造**
        # 一侧的任何意外（可序列化失败等），同样不得打断追问。
        logger.warning("chat turn persistence failed (ignored)", exc_info=True)


@router.get("/chat/stream")
async def chat_stream(
    request: Request,
    text: str = Query(..., description="User question text"),
    record_id: str = Query(default="", description="Sidecar basename for followups"),
    attach_kline_snapshot: bool = Query(default=False, description="附加最新 K 线快照到追问 prompt"),
):
    """SSE endpoint for post-analysis free-chat."""
    ctx = request.app.state.ctx
    # Bounded: a paused tab must not accumulate tokens forever (same reasoning as
    # 原服务端 K 线广播队列的取值)。
    event_queue: asyncio.Queue = asyncio.Queue(maxsize=_CHAT_QUEUE_MAXSIZE)
    loop = asyncio.get_running_loop()

    # ── 会话身份 ────────────────────────────────────────────────────────────
    # header 优先，其次 ?sid=（原生 EventSource 带不了 header；前端走 API.sse()
    # 的 fetch 封装，两条路都留着）。
    sid = session_id_of(request)
    state = None
    if sid:
        try:
            state = get_registry().get_or_create(sid)
            # 挂上队列有两个作用：① LRU 淘汰会跳过有连接的会话（见
            # InMemoryBackend._evict_locked），② 断连时按身份摘除。
            state.sse_queue = event_queue
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat session state unavailable for %s: %s", sid, exc)

    # 本 tab 的游标（symbol/timeframe/exchange），不读全局 settings。
    view_symbol, view_timeframe, view_exchange = _resolve_view(request, ctx)

    # Anchor the follow-up to an analysis of the *currently viewed* instrument.
    # 锚点必须会话级：ctx._last_record 是全局的，A tab 分析完的结果会成为 B tab
    # 追问的锚点。没有 sid 时才回落全局（保持改造前行为）。
    record = state.last_record if state is not None else getattr(ctx, "_last_record", None)
    if not _record_matches_subscription(record, view_symbol, view_timeframe):
        record = None

    if record is None:
        # Try to load latest from history for this tab's instrument. Offloaded:
        # on a cache miss this rglobs + parses records — blocking file I/O.
        from pa_agent.records.analysis_history import find_latest_successful_record

        record = await asyncio.to_thread(
            find_latest_successful_record,
            symbol=view_symbol or "",
            timeframe=view_timeframe or "",
            exchange=view_exchange or "",
        )
        # Only promote to the shared hint when it is genuinely the newest one;
        # previously any fallback clobbered a fresh in-memory record.
        if record is not None:
            if state is not None:
                state.last_record = record
            else:
                ctx._last_record = record

    if record is None:
        await event_queue.put({"type": "error", "message": "没有已完成的交易分析记录，请先进行一次分析"})

        async def _error_gen():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue
                    yield {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}
                    break
            finally:
                _detach_queue(sid, event_queue)

        return EventSourceResponse(_error_gen())

    meta = getattr(record, "meta", None)

    def _key_part(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    # record 三段：保留原语义。扩键而非替换 —— 见模块 docstring 的实测说明。
    # db_record_id 单独取出来：内存分桶与 chat_turns.record_id 用的是同一个值，
    # 复制两份表达式迟早会漂移（漂移后库里存的是另一条记录的外键）。
    db_record_id = (
        record_id or _key_part(getattr(record, "_basename", "")) or "latest"
    )
    record_key = "|".join(
        [
            db_record_id,
            _key_part(getattr(meta, "symbol", "")),
            _key_part(getattr(meta, "timeframe", "")),
        ]
    )
    # thread 段：让「同一个 session 里的不同记录」互不污染，同时让「不同 tab 的
    # 同一记录」也互不污染。快照标志单独成段，切换它才真的生效。
    thread_key = sid or _NO_SESSION
    session_key = f"{thread_key}|{record_key}|{'k' if attach_kline_snapshot else 'n'}"

    session = _get_session(session_key)
    if session is None:
        # 仅当 attach_kline_snapshot=true 时附加最新 K 线快照（Phase C Task 3 SubTask 3.8）
        kline_fn = (
            _kline_snapshot_fn(ctx, (view_symbol, view_timeframe, view_exchange))
            if attach_kline_snapshot else None
        )
        session = FreeChatSession(
            base_record=record,
            client=ctx.client,
            assembler=ctx.assembler,
            pending_writer=ctx.pending_writer,
            ledger=ctx.ledger,
            settings=ctx.settings,
            kline_snapshot_fn=kline_fn,
        )
        _touch_session(session_key, session, asyncio.Lock())
    entry_lock = _chat_sessions[session_key]["lock"]

    def on_reasoning(c: str) -> None:
        _push(loop, event_queue, {"type": "reasoning_token", "chunk": c})

    def on_content(c: str) -> None:
        _push(loop, event_queue, {"type": "content_token", "chunk": c})

    def _run():
        # **不在这里加锁**。锁在事件循环侧（见 event_generator）。
        #
        # 原实现在线程池 worker 内 `with entry_lock:`，与
        # `ThreadPoolExecutor(max_workers=2)` 组合会锁死整个追问通道：
        # worker1 持锁跑 session.send()，worker2 卡在同一把锁上（同一 tab 的
        # 并发请求），两个 worker 全被占满 —— 其它 tab / 其它记录的追问全部排队
        # 等待，且 worker2 要等 worker1 结束才可能推进。分桶键加上 session_id
        # 之后「同一 tab 的两次并发请求必然撞同一把锁」，把这条死路变成常态。
        #
        # 移到事件循环侧后：等待发生在事件循环里，不占 worker，永远不会死锁，
        # 同时仍然保证 FreeChatSession 的互斥（send() 无内建锁）。
        #
        # 轮次号在 send 之前算：_history_full 初始为空、每轮追加 user+assistant
        # 两条，故「已完成的轮数」= len // 2，与 _turn（send 内先自增）严格同源。
        turn_number = len(session.history_full) // 2 + 1
        try:
            cancel_token = CancelToken()
            reply = session.send(text, cancel_token=cancel_token,
                                 on_reasoning_token=on_reasoning,
                                 on_content_token=on_content)
            _push(loop, event_queue, {
                "type": "done",
                "content": reply.content,
                "reasoning": reply.reasoning_content or "",
            })
            # 先推 done 再落库（见模块 docstring 的硬约束 2）。
            _persist_turn(
                thread_key=session_key,
                turn_number=turn_number,
                session_id=sid,
                record_id=db_record_id,
                symbol=_key_part(getattr(meta, "symbol", "")),
                timeframe=_key_part(getattr(meta, "timeframe", "")),
                user_text=text,
                assistant=reply.content or "",
                reasoning=reply.reasoning_content or None,
                usage={
                    "prompt_tokens": reply.usage.prompt_tokens,
                    "cached_prompt_tokens": reply.usage.cached_prompt_tokens,
                    "completion_tokens": reply.usage.completion_tokens,
                    "total_tokens": reply.usage.total_tokens,
                },
            )
        except Exception as exc:
            # 取消也是一种「发生了的轮次」：free_chat 已把它写进 JSONL sidecar，
            # 库里同样留一行，否则「问过但没答」的那次追问在审计里彻底消失。
            if _is_cancelled(exc):
                _persist_turn(
                    thread_key=session_key,
                    turn_number=turn_number,
                    session_id=sid,
                    record_id=db_record_id,
                    symbol=_key_part(getattr(meta, "symbol", "")),
                    timeframe=_key_part(getattr(meta, "timeframe", "")),
                    user_text=text,
                    cancelled=True,
                )
            _push(loop, event_queue, {"type": "error", "message": str(exc)})

    async def event_generator():
        try:
            await entry_lock.acquire()
        except BaseException:
            # 等锁期间就被断开（F5 / 关 tab）：还没派发 _run，但队列已经挂上
            # 了会话状态，必须摘掉，否则该会话会被 LRU 淘汰跳过。
            _detach_queue(sid, event_queue)
            raise
        fut = None
        try:
            fut = loop.run_in_executor(_executor, _run)
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
                    continue
                yield {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}
                event_queue.task_done()
                if event["type"] in ("done", "error"):
                    break
        finally:
            if fut is not None and not fut.done():
                # 客户端中途断开：worker 还在跑 LLM。此时**不能**立刻放锁，否则
                # 下一个请求会与它并发改同一份 _history_full（轮次错乱）。
                # 挂 done 回调，等 worker 真正结束再放（回调在事件循环线程执行，
                # asyncio.Lock 的 release 是安全的）。
                fut.add_done_callback(lambda _f: entry_lock.release())
            else:
                entry_lock.release()
            _detach_queue(sid, event_queue)

    return EventSourceResponse(event_generator())


def _detach_queue(sid: str, queue: asyncio.Queue) -> None:
    """断连清理：**只**摘 SSE 队列，绝不 ``registry.drop(sid)``。

    浏览器会自动重连（EventSource / fetch 重试），用 drop 会把该会话的追问
    历史、游标、last_record 一起清掉 —— 隔离能力被静默摧毁。
    只在队列仍是**本请求**挂上去的那一个时才摘，避免误摘同会话其它在途请求。
    """
    if not sid:
        return
    try:
        registry = get_registry()
        state = registry.get(sid)
        if state is not None and state.sse_queue is queue:
            registry.drop_queue(sid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("chat queue detach failed for %s: %s", sid, exc)