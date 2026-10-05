"""会话注册表：内嵌 mini-Redis（内存热层 + TTL + LRU）。

对应 Redis 的概念：``SessionRegistry`` ≈ keyspace，``SessionState`` ≈ HASH，
``set/get/delete(ttl)`` ≈ HSET/HGET + EXPIRE，``sweep()`` ≈ 惰性过期，
``max_sessions`` + LRU ≈ ``maxmemory-policy``。设计见
``docs/SESSION_STORAGE_DESIGN.md`` §6。

**铁律：任何不能丢的东西绝对不能进这里。**
判据：丢了要不要重新跑一次分析 / 重新下载一次？要 → 可以放。

**已知限制（不可忽视）**

1. **多进程即失效**：本表是进程内内存态。``Dockerfile`` 的 CMD 不带
   ``--workers``（单进程）故当前安全；一旦加 ``--workers 2``，会话会按 hash
   落到不同进程，表现为「追问偶尔丢历史」。``EphemeralBackend`` 抽象就是为
   将来换真 Redis 预留的 —— 调用方只依赖本模块的公开 API。
2. **重启即丢**：L3 游标由 ``sessions`` 表快照恢复；运行时开关
   （keep_analysis / wait_close）刻意不还原 —— 那是缓存语义。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol

logger = logging.getLogger("pa_agent.storage.ephemeral")

# ── 默认参数 ──────────────────────────────────────────────────────────────────
#: 空闲多久回收一个会话。30 分钟覆盖「切个标签页看看，过会儿回来」。
DEFAULT_TTL_S = 1800.0
#: 硬上限。超出按 LRU 踢 —— 没有上限时，一个反复开关标签页的会话可以
#: 把内存吃光（AGENTS.md 遗留需求 3「SSE 长连接内存泄漏排查」）。
DEFAULT_MAX_SESSIONS = 64
#: 每会话 SSE 出站队列上限。沿用 routes_bars_stream.SUBSCRIBER_QUEUE_MAXSIZE
#: 的取值与理由：后台标签页不得无限累积事件。
DEFAULT_QUEUE_MAXSIZE = 256


class Cursor:
    """该会话当前正在看的标的 —— 今天的「游标」。

    存在会话级而非 settings.json，是因为切标签页不应改写全局订阅
    （``POST /api/subscribe`` 曾直接覆写 ``ctx.settings``，导致 A tab 切品种
    就把 B tab 的图切走）。
    """

    __slots__ = ("symbol", "timeframe", "exchange")

    def __init__(self, symbol: str = "", timeframe: str = "", exchange: str = "") -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange = exchange

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.symbol, self.timeframe, self.exchange)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Cursor) and self.as_tuple() == other.as_tuple()

    # 定义了 __eq__ 就必须显式给 __hash__，否则 __slots__ 类不可哈希 ——
    # 任何拿 Cursor 当 dict 键做分组的实现都会 TypeError（评审实测确认）。
    def __hash__(self) -> int:
        return hash(self.as_tuple())

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"Cursor({self.symbol!r}, {self.timeframe!r}, {self.exchange!r})"


class SessionState:
    """单个浏览器标签页的热状态。

    固定槽位直接走属性（高频访问），零 dict 查找；``scratch`` 承载低频的
    自定义键值，各自带 TTL —— 对应 Redis HASH + EXPIRE。
    """

    __slots__ = (
        "session_id",
        "user_id",
        "created_at",
        "last_touch",
        "cursor",
        "sse_queue",
        "chat",
        "last_record",
        "analyzing",
        "scratch",
    )

    def __init__(self, session_id: str, *, user_id: str = "default", now: float = 0.0) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.created_at = now or time.time()
        self.last_touch = self.created_at
        # 游标：会话级 L3，切 tab 不互相影响
        self.cursor = Cursor()
        # 该 tab 专属的 SSE 出站队列。有界，慢客户端不拖累他人。
        self.sse_queue: asyncio.Queue | None = None
        # 追问会话 {session: FreeChatSession, lock: Lock}。
        # 按 session_id 而非 (record|symbol|tf) 分 —— 后者让同记录的两个 tab
        # 共享对话历史，互相污染。
        self.chat: dict[str, Any] | None = None
        # 该 tab 最近一次分析结果。**必须会话级**：挂在全局 ctx 上时，
        # A tab 的分析结果会成为 B tab 追问的锚点。
        self.last_record: Any = None
        # 同 tab 分析重入守卫（AGENTS.md「持续分析」相关）
        self.analyzing = False
        # 低频临时键值：(key, (value, expires_at))
        self.scratch: dict[str, tuple[Any, float]] = {}

    # ── 临时键值（Redis HASH + EXPIRE 等价） ──────────────────────────────────
    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        exp = time.time() + ttl_s if ttl_s else 0.0
        self.scratch[key] = (value, exp)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self.scratch.get(key)
        if entry is None:
            return default
        value, exp = entry
        if exp and time.time() > exp:
            # 惰性过期 —— 与 Redis 一致，读取时才判定
            self.scratch.pop(key, None)
            return default
        return value

    def delete(self, key: str) -> None:
        self.scratch.pop(key, None)

    def sweep_scratch(self, now: float) -> int:
        """Drop expired scratch keys.  返回清理数量。"""
        dead = [k for k, (_, exp) in self.scratch.items() if exp and now > exp]
        for k in dead:
            self.scratch.pop(k, None)
        return len(dead)

    # ── SSE 队列 ──────────────────────────────────────────────────────────────
    def ensure_queue(self, maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> asyncio.Queue:
        if self.sse_queue is None:
            self.sse_queue = asyncio.Queue(maxsize=maxsize)
        return self.sse_queue

    def stats(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "age_s": round(time.time() - self.created_at, 1),
            "idle_s": round(time.time() - self.last_touch, 1),
            "cursor": self.cursor.as_tuple(),
            "queue_depth": self.sse_queue.qsize() if self.sse_queue else 0,
            "has_chat": self.chat is not None,
            "analyzing": self.analyzing,
            "scratch_keys": len(self.scratch),
        }


class EphemeralBackend(Protocol):
    """Redis 可替换落点。

    目前只有内存实现；将来若上多进程或需要跨实例共享，实现本协议即可，
    调用方（routes_*）无需改动 —— 它们只依赖 ``SessionRegistry`` 的公开 API。
    """

    def get_or_create(self, session_id: str) -> SessionState: ...
    def get(self, session_id: str) -> SessionState | None: ...
    def drop(self, session_id: str) -> None: ...
    def sweep(self) -> int: ...
    def all_sessions(self) -> list[SessionState]: ...


class InMemoryBackend:
    """默认实现：``OrderedDict`` 做 LRU + ``threading.Lock`` 保护。

    锁是必需的 —— 路由在事件循环里跑，``experience_scheduler`` 与
    ``order_followup`` 在后台线程里也会碰同一张表。
    """

    def __init__(self, *, max_sessions: int = DEFAULT_MAX_SESSIONS,
                 default_ttl_s: float = DEFAULT_TTL_S) -> None:
        self._max = max_sessions
        self._ttl = default_ttl_s
        self._lock = threading.RLock()
        # OrderedDict：move_to_end 即「标记为最近使用」，淘汰时从 oldest 端弹
        self._sessions: OrderedDict[str, SessionState] = OrderedDict()

    def get_or_create(self, session_id: str) -> SessionState:
        now = time.time()
        with self._lock:
            state = self._sessions.get(session_id)
            if state is not None:
                state.last_touch = now
                self._sessions.move_to_end(session_id)
                return state
            state = SessionState(session_id, now=now)
            self._sessions[session_id] = state
            self._evict_locked(now)
            return state

    def get(self, session_id: str) -> SessionState | None:
        now = time.time()
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return None
            # 已过期则视同不存在（惰性过期）
            if now - state.last_touch > self._ttl:
                self._sessions.pop(session_id, None)
                return None
            state.last_touch = now
            self._sessions.move_to_end(session_id)
            return state

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _evict_locked(self, now: float) -> None:
        """先清过期，再按 LRU 踢到上限以下。调用方须持有锁。"""
        expired = [k for k, v in self._sessions.items() if now - v.last_touch > self._ttl]
        for k in expired:
            self._sessions.pop(k, None)
            logger.debug("session expired: %s", k)
        # 硬上限：宁可踢最久未用的，也不能让内存无限涨。
        # 但**跳过仍有 SSE 队列的会话** —— 被弹的会话其 event_generator 会永久
        # await queue.get()，连接泄漏且 finally 里的清理永不执行。
        while len(self._sessions) > self._max:
            # OrderedDict 按「最近使用」排序，从头找第一个没有挂连接的会话。
            victim_id = next(
                (k for k, v in self._sessions.items() if v.sse_queue is None), None
            )
            if victim_id is None:
                logger.warning(
                    "session registry over capacity (%d) but every session holds an "
                    "SSE queue; keeping them rather than leaking connections",
                    len(self._sessions),
                )
                break        # 全都挂着连接，宁可暂时超限也不踢
            victim = self._sessions.pop(victim_id)
            logger.warning(
                "session registry at capacity (%d), evicted LRU session %s",
                self._max, victim.session_id,
            )
            logger.warning(
                "session registry at capacity (%d), evicted LRU session %s",
                self._max, victim.session_id,
            )

    def sweep(self) -> int:
        with self._lock:
            before = len(self._sessions)
            now = time.time()
            # 顺带清各会话过期的 scratch 键
            for state in self._sessions.values():
                state.sweep_scratch(now)
            self._evict_locked(now)
            return before - len(self._sessions)

    def all_sessions(self) -> list[SessionState]:
        with self._lock:
            return list(self._sessions.values())

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


class SessionRegistry:
    """会话注册表门面。

    唯一入口。路由层只应通过它拿会话，不要直接碰 backend —— 换 Redis 时
    这一层不动。
    """

    def __init__(
        self,
        *,
        backend: EphemeralBackend | None = None,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        default_ttl_s: float = DEFAULT_TTL_S,
    ) -> None:
        self._backend: EphemeralBackend = backend or InMemoryBackend(
            max_sessions=max_sessions, default_ttl_s=default_ttl_s
        )

    # ── 公开 API ──────────────────────────────────────────────────────────────
    def get_or_create(self, session_id: str) -> SessionState:
        if not session_id:
            raise ValueError("session_id must be non-empty")
        return self._backend.get_or_create(session_id)

    def get(self, session_id: str) -> SessionState | None:
        if not session_id:
            return None
        return self._backend.get(session_id)

    def drop(self, session_id: str) -> None:
        """整个会话丢弃（含 chat / cursor / last_record）。仅关 tab 时用。"""
        self._backend.drop(session_id)

    def drop_queue(self, session_id: str) -> None:
        """**只**清空该会话的 SSE 出站队列，其余状态原样保留。

        断连时必须用这个而不是 :meth:`drop` —— 浏览器会自动重连 EventSource，
        用 drop 会把追问历史和游标一起清掉（P2 隔离被静默摧毁的根因）。
        """
        state = self._backend.get(session_id)
        if state is not None:
            state.sse_queue = None

    def sweep(self) -> int:
        return self._backend.sweep()

    def all_sessions(self) -> list[SessionState]:
        return self._backend.all_sessions()

    def stats(self) -> dict[str, Any]:
        sessions = self.all_sessions()
        return {
            "count": len(sessions),
            "queue_depths": [s.stats()["queue_depth"] for s in sessions],
            "sessions": [s.stats() for s in sessions],
        }


# ── 进程级单例 ────────────────────────────────────────────────────────────────
_registry: SessionRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> SessionRegistry:
    """Process-wide registry."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = SessionRegistry()
    return _registry


def reset_registry_for_tests() -> SessionRegistry:
    """Rebuild a clean registry. 测试专用。"""
    global _registry
    with _registry_lock:
        _registry = SessionRegistry()
    return _registry
