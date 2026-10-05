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
3. **轮次号取自内存会话的 ``_turn``**（``_next_turn_number``），与
   ``FreeChatSession._turn`` 严格同源。两套计数会让同一段对话在库里出现
   重复轮次，而 ``ix_chat_thread (user_id, thread_key, turn)`` 不是唯一索引
   —— 写重了不报错，只在回读时顺序错乱。

刷新恢复：分桶键、读端与播种同批上线
====================================
刷新后追问历史归零有两个互不相干的成因，必须一起修：

1. **分桶键漂移**：``record_id`` 此前由**前端**拼，而它派生自 ``lastRecord``，
   而 ``lastRecord`` 每次页面加载重置为 ``null`` —— 实测同一 session 刷新前后
   三个键互不相同（``…|NVDA_15m_2026-10-05T10:31:02|NVDA|15m|k`` /
   ``…|chat_1791212104|…`` / ``…|chat_1791212855|…``），于是 ``FreeChatSession``
   在内存 TTL 内也**永远命中不了**。修法：前端**不再拼** ``record_id``，
   record 段一律由服务端从锚点记录推导（:func:`_resolve_thread_key`）。
2. **有写无读**：``chat_repo.list_turns`` 此前只有 tests 调用，没有 HTTP 端点，
   ``#chat-messages`` 又是空 div —— 刷新后聊天框 100% 为空。

补读端时**必须同时补播种**（:func:`_seed_session_history`），否则界面显示 2 轮
而模型以为这是第 1 轮 —— 比空白更糟。
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

#: ``_chat_sessions`` 的**条目数**上限。此前它只有 TTL、没有容量上限，
#: 而 TTL 是「空闲多久」不是「总共多少」—— 键是 ``sid|record|k/n``，于是
#: 单个 tab 只要不断改 ``record_id``（每次分析换一个）就能在 TTL 内堆出
#: 任意多条：实测 300 次约 3.6MB，且**没有任何地方会报错**。
#:
#: 淘汰的是最久没碰的那一条；仍挂着的 SSE 流由 ``event_generator`` 的
#: ``finally`` 释放锁，不受这里影响。**不收编进 SessionRegistry 的 LRU** ——
#: 那边的 TTL（12h）是为了「切标签页过会儿回来」，与追问分桶的 30 分钟语义
#: 不同，混用会让「活跃但会话旧」的分桶被提前回收。
_CHAT_MAX_SESSIONS = 128
#: Per-request SSE queue depth (see routes_bars_stream for the same reasoning).
_CHAT_QUEUE_MAXSIZE = 256
_chat_sessions: dict[str, dict] = {}

#: 无 session_id 时的 thread_key 占位（老客户端 / 直连 API / 单测）。
_NO_SESSION = "nosession"

# 后台清理 task 句柄，避免重复启动
_chat_cleanup_task: asyncio.Task | None = None


async def _chat_cleanup_loop():
    """周期清理过期的 chat session。

    **锁被占用的条目一律跳过**，这是本函数唯一不能省的判断：
    ``event_generator`` 持有那把锁直到它自己的 ``finally`` 才释放，而这里若先
    ``release()``，那条流稍后的 ``finally`` 会二次 release 抛
    ``RuntimeError: Lock is not acquired`` —— 异常抛在 SSE 响应的 finally 里，
    **整条流当场炸掉**。代价只是「正在追问的会话晚一轮回收」，语义无损。

    循环体整体兜底：一条畸形条目（``AttributeError``）曾让这个 task 静默死亡，
    而 ``_ensure_chat_cleanup`` 只在 startup 跑一次 ⇒ 清理**永久失效且不报错**。
    """
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            expired = []
            for k, v in list(_chat_sessions.items()):
                if not isinstance(v, dict):
                    continue
                if now - v.get("last_touch", 0) <= _CHAT_SESSION_TTL_SEC:
                    continue
                entry_lock = v.get("lock")
                if isinstance(entry_lock, asyncio.Lock) and entry_lock.locked():
                    continue  # 正在被追问占用：本轮放过
                expired.append(k)
            for k in expired:
                entry = _chat_sessions.pop(k, None)
                if entry is not None:
                    lock = entry.get("lock")
                    # 只剩「filter → pop 之间被抢」这一种竞态，仍兜底。
                    if isinstance(lock, asyncio.Lock) and lock.locked():
                        try:
                            lock.release()
                        except RuntimeError:  # pragma: no cover - 防御
                            pass
                logger.info("chat session expired, key=%s", k)
        except Exception:  # noqa: BLE001 —— 绝不让 task 带走
            logger.warning("chat session cleanup pass failed", exc_info=True)


@router.on_event("startup")
async def _ensure_chat_cleanup():
    global _chat_cleanup_task
    if _chat_cleanup_task is None or _chat_cleanup_task.done():
        _chat_cleanup_task = asyncio.create_task(_chat_cleanup_loop())
        _chat_cleanup_task.add_done_callback(_respawn_chat_cleanup)


def _respawn_chat_cleanup(task) -> None:
    """清理 task 异常退出后重拉。

    循环体已有 try/except，走到这里必是异常。``_ensure_chat_cleanup`` 只在
    startup 跑一次，没有保活的话 task 一死清理就静默永久失效。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.warning("chat cleanup task died (%r); respawning", exc)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 事件循环已关
        return
    global _chat_cleanup_task
    _chat_cleanup_task = loop.create_task(_chat_cleanup_loop())
    _chat_cleanup_task.add_done_callback(_respawn_chat_cleanup)


def _evict_chat_sessions_if_needed() -> int:
    """条目数超上限时回收最久没碰的那些。返回回收条数。

    与 ``_chat_cleanup_loop`` 的 TTL 清理是两件事：那个按**空闲时长**清，
    这个按**总条数**收。缺了后者，一个不停换 record_id 的 tab 就能在 TTL
    窗口内把内存撑起来 —— 键基数是 tab x 记录，TTL 根本管不住。
    """
    overflow = len(_chat_sessions) - _CHAT_MAX_SESSIONS
    if overflow <= 0:
        return 0
    victims = sorted(_chat_sessions.items(), key=lambda kv: kv[1].get("last_touch", 0))
    removed = 0
    for key, entry in victims[:overflow]:
        entry_lock = entry.get("lock") if isinstance(entry, dict) else None
        if isinstance(entry_lock, asyncio.Lock) and entry_lock.locked():
            continue  # 正在追问：宁可留到下一轮
        if _chat_sessions.pop(key, None) is not None:
            removed += 1
    return removed


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
    # 写入即收：只在**新键**时可能超限，覆盖已有键不会。先 insert 再收是
    # 必须的 —— 反过来会把刚写入的这一条当成最久没碰的收掉。
    if len(_chat_sessions) > _CHAT_MAX_SESSIONS:
        _evict_chat_sessions_if_needed()


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


def _key_part(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _derive_record_key_id(record) -> str:
    """record 段的**服务端**推导值：``symbol|timeframe|timestamp_local_ms``。

    只依赖 ``record.meta``，因此刷新后、从别的 tab 发出的请求、进程重启后
    重新解析出的**另一个对象**，算出来的都是同一个值 —— 这正是破口①要的性质。

    **为什么不用 ``free_chat._derive_record_id``**：它的格式串是
    ``%Y-%m-%d_%H-%m-%S``（分钟位写成了**月** ``%m``，小写），同一小时内同一秒的
    两条记录会推出**完全相同**的 id。拿它分桶等于把「不同记录挤进同一个桶」
    这件事重新引回来 —— 而那正是模块 docstring 里最危险的那种静默错答
    （携带上一条的 stage1/stage2，无任何报错）。``free_chat`` 不在本文件写集内，
    故此处不复用，也不假装它是对的。

    **为什么带毫秒**：`timestamp_local_iso` 只到秒。同一秒内对同一标的跑两次
    分析再各自追问，就会共用一个桶。毫秒是记录自身就有的字段，不需要任何额外
    的全局状态，且刷新前后完全一致。

    取不到 meta 字段时返回空串，由调用方回落到 ``"latest"``。
    """
    meta = getattr(record, "meta", None)
    symbol = _key_part(getattr(meta, "symbol", ""))
    timeframe = _key_part(getattr(meta, "timeframe", ""))
    ms = getattr(meta, "timestamp_local_ms", None)
    if isinstance(ms, bool) or not isinstance(ms, int):
        ms = ""
    if not (symbol or timeframe or ms):
        return ""
    return "|".join([symbol, timeframe, str(ms)]).strip("|")


def _record_key_id(record, record_id: str = "") -> str:
    """``record_key`` 的第一段：锚点记录的唯一标识。**服务端自己推导。**

    优先级：

    1. 调用方显式传入的 *record_id* —— 保留只是为了让老客户端 / 直连 API 还能
       指定锚点；前端**不再使用**（见模块 docstring 的「刷新恢复」）。
    2. ``record._basename`` —— 磁盘文件名 stem。调用方挂上时优先；没挂时返回
       空串（全仓生产代码目前都不会挂，保留只是不掐断这条既有约定）。
    3. :func:`_derive_record_key_id` —— 从 ``record.meta`` 现算，刷新后唯一
       稳定的来源。
    4. ``"latest"`` —— 记录连 meta 都没有时的占位（此时也无法区分记录，
       但那本就不是真记录）。

    **为什么不让前端拼**：前端唯一的输入是 ``lastRecord``，而它每次页面加载
    重置为 ``null``，拼出来的 id 每次刷新都不同 ⇒ 分桶键漂移 ⇒ 内存桶永远命中
    不了（实测三连刷新得到三个不同的键）。
    """
    explicit = _key_part(record_id)
    if explicit:
        return explicit
    basename = _key_part(getattr(record, "_basename", ""))
    if basename:
        return basename
    try:
        derived = _key_part(_derive_record_key_id(record))
    except Exception as exc:  # noqa: BLE001 - 派生失败只降级，不该让追问 500
        logger.debug("record key derivation failed: %s", exc)
        derived = ""
    return derived or "latest"


def _resolve_thread_key(
    record,
    *,
    session_id: str = "",
    record_id: str = "",
    attach_kline_snapshot: bool = False,
) -> tuple[str, str]:
    """推出 ``(db_record_id, session_key)``。**SSE 与 GET 读端共用这一个实现。**

    ``session_key`` 的形状::

        f"{thread_key}|{record_key}|{'k' if attach_kline_snapshot else 'n'}"

    三个段的语义见模块 docstring。**两处各拼一遍的后果是静默错答**：
    ``FreeChatSession._cached_prefix`` 在构造时按 ``base_record`` 固化，携带
    上一条的 stage1/stage2 会让模型答非所问且**无任何报错** —— 分桶键拼错不会
    抛异常，只会让「回填出来的历史」与「模型以为的历史」悄悄分家。

    *db_record_id* 同时是 ``chat_turns.record_id`` 的取值，必须与内存分桶用
    同一个值，复制两份表达式迟早漂移（漂移后库里存的是另一条记录的外键）。
    """
    meta = getattr(record, "meta", None)
    db_record_id = _record_key_id(record, record_id)
    record_key = "|".join([
        db_record_id,
        _key_part(getattr(meta, "symbol", "")),
        _key_part(getattr(meta, "timeframe", "")),
    ])
    # thread 段：让「同一个 session 里的不同记录」互不污染，同时让「不同 tab 的
    # 同一记录」也互不污染。快照标志单独成段，切换它才真的生效。
    thread_key = session_id or _NO_SESSION
    session_key = f"{thread_key}|{record_key}|{'k' if attach_kline_snapshot else 'n'}"
    return db_record_id, session_key


async def _resolve_anchor(request, ctx, state):
    """解析追问的锚点记录，返回 ``(record, view)``。

    锚点必须**会话级**：``ctx._last_record`` 是全局的，A tab 分析完的结果会成为
    B tab 追问的锚点。没有 sid 时才回落全局（保持改造前行为）。

    与 ``_resolve_thread_key`` 同样被 SSE 与读端共用 —— 锚点一旦分叉，两条路
    会算出一个键相同但 ``_cached_prefix`` 不同的会话，比键不同还难查。
    """
    # 本 tab 的游标（symbol/timeframe/exchange），不读全局 settings。
    view = _resolve_view(request, ctx)
    view_symbol, view_timeframe, _ = view
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
            exchange=view[2] or "",
        )
        # Only promote to the shared hint when it is genuinely the newest one;
        # previously any fallback clobbered a fresh in-memory record.
        if record is not None:
            if state is not None:
                state.last_record = record
            else:
                ctx._last_record = record
    return record, view


def _new_session(ctx, record, view, attach_kline_snapshot: bool) -> FreeChatSession:
    """构造一个新的 ``FreeChatSession``（**不做任何历史播种**）。"""
    # 仅当 attach_kline_snapshot=true 时附加最新 K 线快照（Phase C Task 3 SubTask 3.8）
    kline_fn = (
        _kline_snapshot_fn(ctx, view) if attach_kline_snapshot else None
    )
    return FreeChatSession(
        base_record=record,
        client=ctx.client,
        assembler=ctx.assembler,
        pending_writer=ctx.pending_writer,
        ledger=ctx.ledger,
        settings=ctx.settings,
        kline_snapshot_fn=kline_fn,
    )


def _seed_session_history(session, turns: list[dict]) -> int:
    """把库里已有的追问历史播种进 ``session._history_full``，返回消息条数。

    **这一步与读端必须同批上线**：``FreeChatSession.__init__`` 把 ``_turn`` 置 0、
    ``_history_full`` 置空，而 ``send()`` 只把 ``_history_full`` 拼进
    ``history_for_api``。只回填界面而不播种，模型看到的就是「这是第一轮」——
    用户眼前 2 轮对话、模型只记得第 1 轮，比空白更糟。

    ``_turn`` 同步顶到 ``max(turn)``，一次解决两处：

    - 轮次号接着往上涨，而不是从 1 重来（``ix_chat_thread`` 非唯一，写重不报错）；
    - ``_next_turn_number`` 算出的下一轮号与内存里已有的轮次严格同源。

    *turns* 是 ``chat_repo.load_thread`` 的折叠结果。空列表时**什么都不做**：
    库里没有历史，硬塞一个 ``_turn = 0`` 的假分桶只会掩盖「确实没聊过」。
    """
    try:
        messages, max_turn = chat_repo.seed_messages(turns)
    except Exception:  # noqa: BLE001 - 播种失败不得阻断追问
        logger.warning("chat history seeding failed (ignored)", exc_info=True)
        return 0
    if not messages:
        return 0
    session._history_full = list(messages)
    try:
        session._turn = int(max_turn)
    except (TypeError, ValueError):
        session._turn = len(messages) // 2
    logger.info("chat history seeded: %d messages, turn=%s", len(messages), max_turn)
    return len(messages)


def _next_turn_number(session) -> int:
    """下一轮的轮次号：与 ``FreeChatSession._turn`` 严格同源。

    **不能用 ``len(history_full) // 2 + 1``**：那条式子只在「每轮恰好追加
    user + assistant 两条」时成立，而播种出来的历史里**取消的一轮只有一条**
    （``seed_messages`` 不为它造空回答），算出来的号必然偏小；内存桶回收后
    更是直接从 1 重来。``_turn`` 是 ``send()`` 自己维护的真源。

    ``_turn`` 是私有属性且 ``free_chat`` 不在本文件写集内，故只能这样读；
    取不到（非 int）时回落长度法，并保证返回 ``int`` —— 轮次号会被
    ``chat_repo.append_turn`` 用 ``int()`` 强转，类型不对就整轮不落库。
    """
    raw = getattr(session, "_turn", None)
    base = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    if base < 0:
        base = 0
    try:
        by_len = len(session.history_full) // 2
    except TypeError:  # pragma: no cover - 仅测试替身可能走到
        by_len = 0
    return max(base, by_len) + 1


async def _load_thread_turns(session_key: str, limit: int = 0) -> list[dict]:
    """读一个线程的历史轮次（折叠后）。SQLite 同步调用一律 offload。

    *limit* 传 0 表示用仓储默认。读端宁可多读：截断只影响「显示到第几轮」，
    而播种截断会让模型丢掉最老的上下文 —— 那比少显示几轮糟得多。
    """
    try:
        if limit:
            return await asyncio.to_thread(
                lambda: chat_repo.load_thread(session_key, limit=limit)
            )
        return await asyncio.to_thread(chat_repo.load_thread, session_key)
    except Exception:  # noqa: BLE001 - 读不到历史不该让整个端点失败
        logger.warning("chat thread read failed (ignored)", exc_info=True)
        return []


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

    record, view = await _resolve_anchor(request, ctx, state)

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

    # record 三段：保留原语义。扩键而非替换 —— 见模块 docstring 的实测说明。
    db_record_id, session_key = _resolve_thread_key(
        record,
        session_id=sid,
        record_id=record_id,
        attach_kline_snapshot=attach_kline_snapshot,
    )

    session = _get_session(session_key)
    if session is None:
        # **先播种再建会话的顺序不能反**：_seed_session_history 只写
        # _history_full / _turn，两者都是构造后即固定、不参与 _cached_prefix
        # 的字段，先建后播种是唯一安全的方向。
        turns = await _load_thread_turns(session_key)
        session = _new_session(ctx, record, view, attach_kline_snapshot)
        _seed_session_history(session, turns)
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
        # 轮次号在 send 之前算（send() 内部会先自增 _turn，故此刻它就是
        # 「已完成的轮数」）。必须走 _next_turn_number 而不是
        # len(history_full)//2 + 1 —— 播种出来的历史里取消的一轮只有一条消息，
        # 长度法会算出偏小的号。见该函数 docstring。
        turn_number = _next_turn_number(session)
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


#: 回填端点的轮次上限。与 ``chat_repo.DEFAULT_LIMIT`` 同值但**独立声明**：
#: 一个是仓储的默认值，一个是 HTTP 契约的默认值，绑在一起改会牵动别处。
_CHAT_RESTORE_LIMIT = 200


@router.get("/chat/turns")
async def chat_turns(
    request: Request,
    record_id: str = Query(default="", description="Sidecar basename for followups"),
    attach_kline_snapshot: bool = Query(
        default=True,
        description="必须与发起追问时用的取值一致（同属分桶键的第三段）",
    ),
):
    """读端：回填某个追问线程的历史（**不是 SSE**）。

    **为什么不是 SSE**：这是纯粹的「取一份已经存在的快照」，没有增量、没有
    长时间连接。SSE 在这里只有两个害处：① 原生 ``EventSource`` 带不了请求头，
    拿不到 ``X-Session-Id`` ⇒ 分不出 tab ⇒ 读回别人的追问；前端得为此把
    ``api.js`` 的 ``API.sse`` fetch 封装整套借过来（已经借过一次了）；
    ② 一次性的 4KB JSON 要占一条长连接与心跳。
    走普通 ``fetch`` 则自带 ``X-Session-Id``（``API.get`` 已带），
    顺带因此能用上浏览器缓存/错误码那一套正常语义。

    **返回形状为什么是「一轮一条」而不是「一行一条」**：``chat_turns`` 的物理
    形状是一轮两行（user + assistant），但那只是存储细节 —— 消费方要回答的
    是「第 N 轮我问了什么、它答了什么」。折叠之后：

    - 前端一次 ``for`` 就能按对话渲染，不必自己去配对；
    - 取消的一轮（只有 user 行、``cancelled=1``）能作为**完整的一轮**出现并
      带 ``cancelled`` 标记，而不是让前端发现「assistant 怎么少了一条」去猜；
    - **与播种共用同一份折叠**（``chat_repo.seed_messages``），界面上看到的
      与模型上下文里恢复的一定是同一段对话。二者若各行其是，界面 2 轮、
      模型第 1 轮 —— 那比空白更糟。

    ``record_id`` / ``attach_kline_snapshot`` 仍开放是为了与 SSE 端点**对称**：
    两者都过 :func:`_resolve_thread_key`，任何一条改键的规则都不会只改一半。
    前端两者都不传（record 段由服务端推导，快照段恒为 ``true``）。
    """
    ctx = request.app.state.ctx
    sid = session_id_of(request)
    state = None
    if sid:
        try:
            state = get_registry().get_or_create(sid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat restore state unavailable for %s: %s", sid, exc)

    record, _view = await _resolve_anchor(request, ctx, state)

    if record is None:
        # 没锚点 ⇒ 这个标的压根没分析过 ⇒ 真的没聊过。
        # 200 + 空列表，而不是 404/503：前端据此渲染「还没有追问记录」，
        # 与「分析过但没追问」走同一条渲染路径，避免出现第三种说不清的态。
        return {
            "thread_key": "",
            "record_id": "",
            "symbol": "",
            "timeframe": "",
            "source": "no_anchor",
            "turn_count": 0,
            "turns": [],
        }

    db_record_id, session_key = _resolve_thread_key(
        record,
        session_id=sid,
        record_id=record_id,
        attach_kline_snapshot=attach_kline_snapshot,
    )
    meta = getattr(record, "meta", None)

    turns = await _load_thread_turns(session_key, limit=_CHAT_RESTORE_LIMIT)

    # **读端与播种同批**：内存桶不在就顺手建一个并播种，界面上看到的历史与
    # 模型上下文从这一刻起就是同一段。只回填界面不播种，下一次 send() 时模型
    # 仍会以为这是第一轮（见模块 docstring 的「刷新恢复」）。
    # 空历史不建桶：没有东西可播种，凭空登记一个空会话只会让「没聊过」在内存里
    # 也长得像「聊过」，掩盖真正的空态。
    if turns and _get_session(session_key) is None:
        session = _new_session(ctx, record, _view, attach_kline_snapshot)
        _seed_session_history(session, turns)
        _touch_session(session_key, session, asyncio.Lock())

    return {
        "thread_key": session_key,
        "record_id": db_record_id,
        "symbol": _key_part(getattr(meta, "symbol", "")),
        "timeframe": _key_part(getattr(meta, "timeframe", "")),
        # 明说历史是从哪儿来的：库里读到 / 当前标的下压根没有分析记录。
        # 「两者都没有」就是「真的没聊过」，前端据此显示确定的空态而不是
        # 「加载中」以外任何含糊的中间态。
        "source": "db" if turns else "empty",
        "turn_count": len(turns),
        "turns": turns,
    }


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
        # 用 clear_queue 而非 peek/get：get() 对「已过期且挂流」的会话返回
        # None（TTL 分支为保护 SSE 刻意不弹表），照旧走 get 会让断连清理
        # 静默变成空操作 —— 队列再没人写，而 TTL/LRU 淘汰都跳过挂队列者。
        registry.drop_queue(sid, expected=queue)
    except Exception as exc:  # noqa: BLE001
        logger.debug("chat queue detach failed for %s: %s", sid, exc)