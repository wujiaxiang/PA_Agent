"""周期性清理：内存态与过期数据。daemon 线程 + 单飞守卫 + 可观测。

为什么必须有这个模块
====================
四类数据都只增不减，其中三处的清理函数**早就写好、却没有任何生产调用者**：

===========================  =====================================  ==========
对象                          清理入口                                接线状态
===========================  =====================================  ==========
会话注册表（内存）              ``SessionRegistry.sweep()``         本模块首次调用
追问会话（内存）                ``routes_chat._chat_sessions``      本模块兜底（见下）
``sessions`` 表（DB）          ``sessions.purge_expired()``         本模块首次调用
孤儿 ``chat_turns``（DB）      ``sessions.purge_for_user_sessions()`` 本模块首次调用
===========================  =====================================  ==========

「有实现、无调用者」是最坏的一种状态：读代码的人以为它会跑，压测/长期运行时
数据却持续堆积，直到磁盘满或内存爆。故本模块**不新增任何清理语义**，只做一件事
——把既有的四处清理接进一个守护线程。

为什么不能放在分析主路径上
==========================
清理是**与用户请求无关的资源回收**，把它塞进任何请求路径都会把三个风险带进
主流程：

1. **延迟不可控**。一次 ``DELETE FROM sessions`` + 一次孤儿全表扫描都可能撞上
   SQLite 写锁（``busy_timeout=5000``），让用户的「分析」按钮多等 5 秒。
2. **失败即故障**。清理抛异常若无兜底，会把一次 K 线分析变成 500；而它失败
   本身没有任何用户可感知的损失（下一轮还会再清）。
3. **清理会被高频请求放大**。前端每 5s 轮询一次 ``/api/bars``，挂上清理等于
   每 5s 跑一次全表扫描。

故沿用 :mod:`web.api.experience_scheduler` 的全部约定：**daemon 线程 + 自己的
单飞守卫 + ``start``/``stop`` 成对 + 任何异常只记 warning 绝不冒泡**。

本模块**不新增清理语义**的三条硬约束
====================================

1. **顺序敏感**::

       purge_expired()  →  purge_for_user_sessions()

   后者按 ``session_id NOT IN (SELECT session_id FROM sessions)`` 判孤儿，
   而**只有前者**能把过期行从 ``sessions`` 里拿掉。反过来（先清孤儿）时行还在
   ``sessions`` 里，永远判不出任何孤儿 —— 该函数会静默地返回 0，看着像「这轮
   没东西可清」，实际是永远不会发生。故 ``run_once()`` 里这两行必须紧挨着按序
   执行，且中间不得有「DB 不可用就跳过第二段」的分支（那等于把顺序保护丢掉）。

2. **不绕过注册表自己的淘汰策略**。``SessionRegistry.sweep()`` 里的
   ``_evict_locked()`` 会**跳过仍挂 SSE 队列的会话** —— 被弹掉的会话，其
   ``event_generator`` 会永远 ``await queue.get()``，连接泄漏且 ``finally``
   里的清理永不执行。本模块只调 ``sweep()``，**不自己遍历 ``all_sessions()``
   去 pop**：那份判定逻辑只存在于注册表内部，绕过去就等于取消它。

3. **不重复实现追问会话的清理**。``routes_chat._chat_cleanup_loop`` 是它的
   权威清理方（事件循环侧、60s 一轮）。本模块只在**那条 task 没在跑**时补一刀，
   且**绝不碰被占用的条目**（详见 :func:`_sweep_chat_sessions`）—— 跨线程
   ``release()`` 一把 ``asyncio.Lock`` 不是能做的事。

间隔取值
========
:data:`DEFAULT_INTERVAL_S` = 600s（10 分钟）。清理**不是实时需求**：四类数据的
TTL 分别是 30 分钟（会话快照 / 内存会话）与 30 分钟（追问内存会话），孤儿追问
的保留期是 7 天。即最坏情况下的实际回收延迟是「TTL + 一个间隔」（10.5 分钟量级），
对「会话快照 / 内存会话」这种缓存级数据完全够用；取更短只会白白增加 SQLite 写
次数，而 ``sessions`` 表带 TTL —— 过早清理会让用户还在用的会话凭空消失。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("pa_agent.web.storage_gc")

#: Pass interval. 见模块 docstring「间隔取值」—— 10 分钟是 TTL(30min) 的 1/3。
DEFAULT_INTERVAL_S = 600.0

#: 无论传入什么都不快于此。清理不是实时需求，而 SQLite 写锁是共享资源。
_MIN_INTERVAL_S = 60.0

#: 启动后先等这么久再跑第一轮：让启动期的导入 / 首个请求过去，别在 boot 阶段
#: 抢写锁。第一轮**照跑**（不是「等一个间隔再跑」），否则重启后上一进程留下的
#: 过期数据要等满一个间隔才清。
_FIRST_DELAY_S = 20.0

#: ``stop()`` 的 join 超时。守护线程的等待点都是 ``_stop.wait()``（可被事件
#: 立刻唤醒），正常情况下 join 是毫秒级返回的 —— 设长没有意义，只会拖慢停机。
_STOP_JOIN_TIMEOUT_S = 5.0


class _Guard:
    """Single-flight guard — a pass must never overlap itself.

    与 ``experience_scheduler._Guard`` 逐字同构（两处都是各自模块的进程级单例，
    合并成一个共享守卫反而会让两个调度器互相阻塞）。
    """

    def __init__(self) -> None:
        self._flag = threading.Lock()
        self._busy = False

    def try_acquire(self) -> bool:
        with self._flag:
            if self._busy:
                return False
            self._busy = True
            return True

    def release(self) -> None:
        with self._flag:
            self._busy = False


_guard = _Guard()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

# ── 可观测状态（/api/health 读）──────────────────────────────────────────────
_interval_s = 0.0
_next_run_ts = 0.0
_last_run_ts = 0.0
_last_result: Optional[dict[str, Any]] = None


# ── 四个清理步骤 ──────────────────────────────────────────────────────────────
def _sweep_registry() -> int:
    """内存：会话注册表。返回被淘汰的会话数。

    **只调 ``sweep()``，绝不自己 pop**（见模块 docstring 约束 2）。``sweep()``
    内部顺带清每个会话的过期 scratch 键，并走注册表自己的 LRU/SSE 保护。
    """
    from pa_agent.storage.ephemeral import get_registry

    return int(get_registry().sweep() or 0)


def _sweep_chat_sessions() -> int:
    """内存：``routes_chat._chat_sessions`` 里的追问会话。返回被回收的条目数。

    这一处的权威清理方是 ``routes_chat._chat_cleanup_loop``（事件循环侧、60s
    一轮，会在弹出后 ``release()`` 被占用的锁把等待者唤醒）。本函数只在
    **那条 task 没在跑**时补位 —— 正常情况下恒返回 0，不与它抢同一批条目。
    之所以需要这层兜底：该清理 task 挂在 ``@router.on_event("startup")`` 上，
    而 FastAPI **已不再执行 router 级 startup 事件**（0.142 实测
    ``APIRouter.include_router`` 不转发 ``on_startup``），故它实际上从未启动，
    ``_chat_sessions`` 一直只增不减。

    两条与权威清理方的差异，都是有意为之：

    * **锁被占用的条目一律跳过**。``asyncio.Lock`` 归事件循环所有，从后台线程
      ``release()`` 它不是能做的事（唤醒动作要排到那个循环上）；而「正在被用
      的会话」本就不该被回收。这类条目交给权威清理方，本轮放过 —— 锁只在一轮
      追问请求期间被持有，下一轮就腾出来了。
    * 判过期用 ``time.time()`` 与 ``last_touch``，与 ``routes_chat`` 的取值
      口径一致（它同样用 ``time.time()``）。

    模块不存在 / 属性改名 / 导入失败都返回 0：清理是可选的，**绝不能因为
    ``routes_chat`` 出问题而让整轮 pass 失败**。
    """
    import asyncio

    from web.api import routes_chat

    task = getattr(routes_chat, "_chat_cleanup_task", None)
    if task is not None and not getattr(task, "done", bool)():
        logger.debug("storage gc: chat cleanup task alive, skipping in-memory chat sweep")
        return 0
    table = getattr(routes_chat, "_chat_sessions", None)
    try:
        ttl = float(getattr(routes_chat, "_CHAT_SESSION_TTL_SEC", 0.0) or 0.0)
    except (TypeError, ValueError):
        ttl = 0.0
    if not isinstance(table, dict) or ttl <= 0.0:
        return 0

    now = time.time()
    removed = 0
    # 复制键列表再遍历：``table`` 会被事件循环侧并发改（_touch_session 写入、
    # _chat_cleanup_loop 弹出），直接迭代 dict 会 RuntimeError。
    for key in list(table.keys()):
        entry = table.get(key)
        if not isinstance(entry, dict):
            continue
        touched = entry.get("last_touch", 0)
        try:
            age = now - float(touched)
        except (TypeError, ValueError):
            age = 0.0
        if age <= ttl:
            continue
        lock = entry.get("lock")
        if isinstance(lock, asyncio.Lock) and lock.locked():
            continue                      # 正在被用 —— 留给权威清理方
        if table.pop(key, None) is not None:
            removed += 1
    return removed


def _storage_ready() -> bool:
    """DB 可用且已初始化才做 DB 清理。

    未初始化时 ``hub.execute()`` 会拒绝写入（返回 False），``query()`` 返回空表 ——
    于是 ``purge_expired()`` 会算出「删了 0 行」，看起来像「这轮没东西可清」。
    与其让这种静默通过，不如显式跳过并在 ``/api/health`` 上说清楚。
    """
    from pa_agent.storage.db import get_hub

    hub = get_hub()
    if hub.disabled:
        return False
    stats = hub.stats()
    return bool(stats.get("enabled")) and bool(stats.get("initialized"))


def _count_rows(table: str) -> int:
    """``SELECT COUNT(*)``。读失败返回 0（此时下面的差值也只是 0，不影响正确性）。"""
    from pa_agent.storage.db import get_hub

    row = get_hub().query_one(f"SELECT COUNT(*) AS n FROM {table}")   # noqa: S608
    return int(row["n"]) if row else 0


def _purge_expired_sessions() -> int:
    """DB：过期 ``sessions`` 快照行。返回删除行数。**必须在孤儿清理之前跑。**"""
    from pa_agent.storage.sessions import purge_expired

    return int(purge_expired() or 0)


def _purge_orphan_chat_turns() -> int:
    """DB：孤儿 ``chat_turns`` 行（其 session_id 已不在 ``sessions`` 表中）。

    **依赖上一步已经跑过**（见模块 docstring 约束 1）。单独调用它永远判不出孤儿。

    行数是**按表行数差**量的，不是 ``purge_for_user_sessions()`` 的返回值 ——
    后者只回答「这次 DELETE 有没有执行成功」（恒为 0/1，删掉 0 条与删掉 40 条
    长得一模一样），当不了「清了多少」的统计。这里刻意**不改**
    ``sessions.py`` 的语义，只在自己这层换一种量法：两次 ``COUNT(*)`` 之间
    除了本函数没有别的删除方，故差值即本轮清出的行数。
    """
    from pa_agent.storage.sessions import purge_for_user_sessions

    before = _count_rows("chat_turns")
    purge_for_user_sessions()
    return max(0, before - _count_rows("chat_turns"))


# ── 一轮 pass ────────────────────────────────────────────────────────────────
def _step(errors: list[str], name: str, fn: Callable[[], int]) -> int:
    """跑一个清理步骤，异常只记 warning 并记进 ``errors``，绝不冒泡。

    分步兜底的理由：某一处（DB 锁、某个模块 import 失败）不该让其余三处也不跑。
    顺序敏感的两步放在同一个 ``_storage_ready()`` 分支里，故上一步失败时下一步
    仍会跑（只是这一轮孤儿几乎判不出来），**顺序本身永不被破坏**。
    """
    try:
        return int(fn() or 0)
    except Exception as exc:  # noqa: BLE001 - 后台线程，任何异常只记 warning
        logger.warning("storage gc: step %s failed: %s", name, exc)
        errors.append(f"{name}: {exc}")
        return 0


def run_once() -> dict[str, Any] | None:
    """跑一轮清理。返回本轮统计，或 None（本轮被单飞守卫跳过）。

    **永不抛异常、永不与自身重叠。** 统计 dict 同时进 ``/api/health``。
    """
    if not _guard.try_acquire():
        logger.debug("storage gc: previous pass still running, skipping")
        return None

    started = time.time()
    summary: dict[str, Any] = {
        "registry_evicted": 0,
        "chat_sessions_evicted": 0,
        "sessions_purged": 0,
        "chat_turns_purged": 0,
        "errors": [],
        "skipped": [],
    }
    try:
        summary["registry_evicted"] = _step(
            summary["errors"], "registry.sweep", _sweep_registry
        )
        summary["chat_sessions_evicted"] = _step(
            summary["errors"], "chat_sessions", _sweep_chat_sessions
        )
        if _step(summary["errors"], "storage_ready", _storage_ready):
            # ── 顺序敏感：这两行必须按序，且同在一个分支内 ──────────────────
            # purge_for_user_sessions() 的孤儿判定是
            # ``session_id NOT IN (SELECT session_id FROM sessions)``；
            # 只有 purge_expired() 能把过期行从 sessions 里拿掉。
            # 反序执行会让孤儿永远判不出来 —— 表现为「每轮都清 0 条」。
            summary["sessions_purged"] = _step(
                summary["errors"], "sessions.purge_expired", _purge_expired_sessions
            )
            summary["chat_turns_purged"] = _step(
                summary["errors"],
                "chat_turns.purge_for_user_sessions",
                _purge_orphan_chat_turns,
            )
        else:
            summary["skipped"].append("sqlite: storage unavailable or not initialized")
    finally:
        # 守卫必须在 finally 里释放：否则一个死循环的步骤会把 GC 永久闭锁。
        _guard.release()

    summary["total"] = (
        summary["registry_evicted"]
        + summary["chat_sessions_evicted"]
        + summary["sessions_purged"]
        + summary["chat_turns_purged"]
    )
    summary["duration_ms"] = round((time.time() - started) * 1000.0, 1)
    _publish(summary)
    if summary["total"] or summary["errors"] or summary["skipped"]:
        logger.info("storage gc pass: %s", summary)
    return summary


def _publish(summary: dict[str, Any]) -> None:
    global _last_result, _last_run_ts
    _last_result = summary
    _last_run_ts = time.time()


# ── 线程生命周期 ─────────────────────────────────────────────────────────────
def _loop(interval_s: float) -> None:
    global _next_run_ts
    logger.info("storage gc started (every %.0fs)", interval_s)
    first = min(_FIRST_DELAY_S, interval_s)
    _next_run_ts = time.time() + first
    while not _stop.is_set():
        if _stop.wait(first):
            break
        run_once()
        first = interval_s
        _next_run_ts = time.time() + interval_s
        if _stop.wait(interval_s):
            break
    _next_run_ts = 0.0
    logger.info("storage gc stopped")


def start(ctx: Any = None, interval_s: float | None = None) -> Optional[threading.Thread]:
    """Start the GC once. Returns the thread, or None if already up.

    Parameters
    ----------
    ctx:
        **刻意不使用**，只为与 :func:`web.api.experience_scheduler.start` 签名一致
        （调用点成对好读）。GC 的回收阈值全部来自各表/注册表**自己的 TTL**，
        不读配置层 —— 配置层一坏不该连累清理停摆，那正好是最需要它的时候。
    interval_s:
        覆盖默认间隔，小于 :data:`_MIN_INTERVAL_S` 会被夹住。
    """
    global _thread, _interval_s
    if _thread is not None and _thread.is_alive():
        return _thread
    try:
        interval = float(interval_s) if interval_s is not None else DEFAULT_INTERVAL_S
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_S
    interval = max(_MIN_INTERVAL_S, interval)

    _interval_s = interval
    _stop.clear()
    t = threading.Thread(target=_loop, args=(interval,), name="storage-gc", daemon=True)
    t.start()
    _thread = t
    return t


def stop(timeout: float = _STOP_JOIN_TIMEOUT_S) -> None:
    """Signal the thread and **join it**（超时 ``timeout`` 秒，不无限等）。

    join 是必须的：lifespan 退出后若线程还活着，它会在应用已停的进程里继续
    写 SQLite。守护线程被解释器杀掉只是兜底，不是设计。
    """
    global _thread, _next_run_ts
    _stop.set()
    t, _thread = _thread, None
    _next_run_ts = 0.0
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


def is_running() -> bool:
    t = _thread
    return bool(t is not None and t.is_alive())


def status() -> dict[str, Any]:
    """给 ``/api/health`` 用的快照：最近一轮的结果 + 下次计划时间。

    清理「有没有在跑」和「跑出了什么」都属于运维面，必须能一眼看出 —— 一个
    静默失效的 GC 与没有 GC 表现完全一样（数据照涨）。
    """
    last = dict(_last_result) if _last_result is not None else None
    if last is not None:
        last["errors"] = list(last.get("errors") or [])
        last["skipped"] = list(last.get("skipped") or [])
        last["at"] = _last_run_ts
    return {
        "running": is_running(),
        "interval_s": _interval_s or None,
        "next_run_at": _next_run_ts or None,
        "last_run_at": _last_run_ts or None,
        "last": last,
    }
