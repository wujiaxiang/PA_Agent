"""周期性清理：内存态与过期数据。daemon 线程 + 单飞守卫 + 可观测。

为什么必须有这个模块
====================
五类数据都只增不减，其中四处的清理函数**早就写好、却没有任何生产调用者**：

===========================  =====================================  ==========
对象                          清理入口                                接线状态
===========================  =====================================  ==========
会话注册表（内存）              ``SessionRegistry.sweep()``         本模块首次调用
追问会话（内存）                ``routes_chat._chat_sessions``      本模块兜底（见下）
``sessions`` 表（DB）          ``sessions.purge_expired()``         本模块首次调用
孤儿 ``chat_turns``（DB）      ``sessions.purge_for_user_sessions()`` 本模块首次调用
无身份 ``chat_turns``（DB）    ``sessions.purge_anonymous_chat_turns()`` 本模块新增
===========================  =====================================  ==========

「有实现、无调用者」是最坏的一种状态：读代码的人以为它会跑，压测/长期运行时
数据却持续堆积，直到磁盘满或内存爆。故本模块**原则上只做一件事** ——把既有的
清理接进一个守护线程（第五行是唯一的例外，见下）。

**为什么第五行是新增的清理语义（而不是又一条接线）**
==================================================
实测：写入 6 条 30 天前的 ``session_id = ''`` 行，跑完两个 purge 再跑多轮 GC，
**一行不少**，而 ``sessions_purged`` / ``chat_turns_purged`` 全报 0、
``errors`` 与 ``skipped`` 全空 —— 不报错、没在清理。根因是
``purge_for_user_sessions`` 的孤儿判定里有个 ``session_id != ''``：没有会话
身份的行被排除在孤儿之外，而**无 ``X-Session-Id`` 的调用方（直连 API / 脚本 /
老客户端）写的恰恰全是这种行**。既有清理语义在这条路径上**根本没有覆盖**，
只能新加一条**只按时间戳**的 DELETE
(:func:`pa_agent.storage.sessions.purge_anonymous_chat_turns`)；为什么**不去掉**
那个 ``AND``（去掉它会让刚落库、还没建会话快照的活跃行立刻变成孤儿，而顺序耦合并
未解除），见该函数 docstring。

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

本模块的硬约束
==============

1. **顺序敏感，且只对「判据含子查询」的那一步敏感**::

       purge_expired()  →  purge_for_user_sessions()

   后者按 ``session_id NOT IN (SELECT session_id FROM sessions)`` 判孤儿，
   而**只有前者**能把过期行从 ``sessions`` 里拿掉。反过来（先清孤儿）时行还在
   ``sessions`` 里，永远判不出任何孤儿 —— 该函数会静默地返回 0，看着像「这轮
   没东西可清」，实际是永远不会发生。故 ``run_once()`` 里这两行必须紧挨着按序
   执行，且中间不得有「DB 不可用就跳过第二段」的分支（那等于把顺序保护丢掉）。

   **上游失败 ⇒ 孤儿步必须跳过并写进 ``skipped``**，而不是照跑。上一步没把过期行
   拿掉时，孤儿步**必然清不出东西**，而它报 0 与「真的没东西可清」无法区分 ——
   实测现象正是 ``errors:['sessions.purge_expired: db locked']``、``skipped:[]``、
   ``chat_turns_purged:0``：一条顺序故障长成了「一切正常」。顺序敏感必须同时体现在
   「不反序」与「不装作没失败」两处。

   第三步 :func:`pa_agent.storage.sessions.purge_anonymous_chat_turns` **不在这个
   耦合里**（它不查 ``sessions`` 表），故上一步失败时它照跑。

2. **不绕过注册表自己的淘汰策略**。``SessionRegistry.sweep()`` 里的
   ``_evict_locked()`` 会**跳过仍挂 SSE 队列的会话** —— 被弹掉的会话，其
   ``event_generator`` 会永远 ``await queue.get()``，连接泄漏且 ``finally``
   里的清理永不执行。本模块只调 ``sweep()``，**不自己遍历 ``all_sessions()``
   去 pop**：那份判定逻辑只存在于注册表内部，绕过去就等于取消它。

3. **不重复实现追问会话的清理**。``routes_chat._chat_cleanup_loop`` 是它的
   权威清理方（事件循环侧、60s 一轮）。本模块只在**那条 task 没在跑**时补一刀，
   且**绝不碰被占用的条目**（详见 :func:`_sweep_chat_sessions`）—— 跨线程
   ``release()`` 一把 ``asyncio.Lock`` 不是能做的事。

4. **可观测性不许编数字**。DB 两步的「清了多少」是用**前后两次 ``COUNT(*)`` 的
   差值**量的，而这个量法有一个致命前提：**读必须成功**。``hub.query`` 读失败时
   返回 ``[]``，与「表里确实是空的」同形；``_count_rows`` 曾把它折成 ``0``，于是
   ``before - 0`` 报出一个凭空捏造的正数 —— 实测把第二次 COUNT 打成读失败时，
   ``_purge_orphan_chat_turns()`` 返回 **18**，而 ``errors`` / ``skipped`` 全空、
   ``chat_turns_purged:18`` 摆在 ``/api/health`` 上，实际删了几行无从得知。R3 的
   **全部**可观测性挂在这些字段上，恰好在最该报警时它们说「一切正常，清了 18 条」。
   故 :func:`_count_rows` 的失败语义是 **None（读不出来）而不是 0**；None 时该步
   **跳过本轮并写进 ``errors``**，**绝不返回差值**（见 :func:`_measured_delta`
   与 :class:`_Unmeasurable`）。

间隔取值
========
:data:`DEFAULT_INTERVAL_S` = 600s（10 分钟）。清理**不是实时需求**：各表与注册表的
TTL：内存热层 12h、会话快照 24h（必须 ≥ 热层，否则进程重启即丢数据）、
内存追问会话 30 分钟；无身份追问的绝对保留期 30 天，正常孤儿 7 天，
无身份追问的绝对上限是 30 天。即最坏情况下的实际回收延迟是「TTL + 一个间隔」
（10.5 分钟量级），对「会话快照 / 内存会话」这种缓存级数据完全够用；取更短只会白白
增加 SQLite 写次数，而 ``sessions`` 表带 TTL —— 过早清理会让用户还在用的会话凭空
消失。
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
    **⚠️ 更正一条此前的错误结论**：本函数最初的注释称「FastAPI 已不再执行
    router 级 startup 事件，故该清理 task 从未启动」。**该结论是错的**，
    已实测证伪：``APIRouter.include_router`` 确实转发 ``on_startup``，
    router 级 startup 在 TestClient 下真的执行。行为由
    ``tests/unit/test_router_startup_forwards.py`` 锁定 —— 将来 FastAPI 真改了
    行为，那条测试会红，届时可以名正言顺地改代码，而不是凭一次误判加兜底。
    本清扫因此是**双保险**（权威 task 失活 / 逻辑有漏）而非「唯一防线」。

    两条与权威清理方的差异，都是有意为之：

    * **锁被占用的条目一律跳过**。``asyncio.Lock`` 归事件循环所有，从后台线程
      ``release()`` 它不是能做的事（唤醒动作要排到那个循环上）；而「正在被用
      的会话」本就不该被回收。这类条目交给权威清理方，本轮放过 —— 锁只在一轮
      追问请求期间被持有，下一轮就腾出来了。
    * 判过期用 ``time.time()`` 与 ``last_touch``，与 ``routes_chat`` 的取值
      口径一致（它同样用 ``time.time()``）。

    模块不存在 / 属性改名 / 导入失败都返回 0：清理是可选的，**绝不能因为
    ``routes_chat`` 出问题而让整轮 pass 失败**。

    **⚠️ 本清扫救不了权威清理方的两个已知缺陷**（修在
    ``routes_chat._chat_cleanup_loop``，不在本模块）：

    1. **它裸 ``release()`` 别人正持有的锁**。命中过期后直接
       ``entry.release()``（``routes_chat._chat_cleanup_loop`` 里那句
       ``lock.release()``，2026-10-06 实测在 124-128 行），而那把锁此刻正被
       ``event_generator`` 持着 —— 该流稍后在 ``finally`` 里二次 release 抛
       ``RuntimeError: Lock is not acquired``，**异常发生在 SSE 响应的 finally
       里，整条流直接炸掉**。
    2. **循环体没有 try/except，也没有保活**。一条非 dict 条目就
       ``AttributeError`` 让 task 当场死亡，而 ``_ensure_chat_cleanup`` 只在
       router startup 调一次，死掉即静默失效。

    本模块**绕不过**它们：本函数在 task 活着时恒返回 0（不与它抢条目），
    而这两处缺陷全部发生在它自己的循环体内。
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


def _count_rows(table: str) -> Optional[int]:
    """``SELECT COUNT(*)``。**读失败返回 None（读不出来），不是 0。**

    「读不出来」与「读出来是 0」必须分开，否则下面 :func:`_measured_delta` 的
    ``before - after`` 会凭空造出数字：``hub.query()`` 读失败时返回 ``[]``，
    ``query_one()`` 因此返回 ``None`` —— 与「SELECT 没返回行」同形。曾把它折成
    ``0``，实测第二次 COUNT 失败时 ``_purge_orphan_chat_turns()`` 返回 **18**，
    ``errors`` / ``skipped`` 全空，「清了多少」这个字段在真正的故障时刻说了谎。

    任何拿 ``0`` 当「表是空的」去算差值的地方都不合格：宁可报「不知道」。
    """
    from pa_agent.storage.db import get_hub

    hub = get_hub()
    row = hub.query_one(f"SELECT COUNT(*) AS n FROM {table}")   # noqa: S608
    if row is None:
        logger.warning(
            "storage gc: COUNT(%s) unreadable (read_error=%r) — reporting 'unknown', not 0",
            table, getattr(hub, "read_error", "") or "no row returned",
        )
        return None
    try:
        return int(row["n"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("storage gc: COUNT(%s) returned malformed row %r: %s",
                       table, dict(row), exc)
        return None


class _Unmeasurable(Exception):
    """本步的「清了多少」**不可知** —— 读不出来，不是「清 0 条」。

    由 :func:`_measured_delta` 抛出，被 :func:`_step` 捕获后写进 ``errors``，
    并让该步的计数变成 ``None``（在 summary 里落成 0 且**没有**任何数字含义）。
    """


def _measured_delta(table: str, purge: Callable[[], Any]) -> int:
    """``purge()`` 前后各 ``COUNT(*)`` 一次，返回行数差。**读不出来就抛。**

    行数差是「清了多少」的唯一可信来源：``sessions.purge_*`` 的返回值只回答
    「这次 DELETE 有没有执行成功」（恒为 0/1，删 0 条与删 40 条长得一样）。
    两次 COUNT 之间除了本函数没有别的删除方，故差值即本轮清出的行数。

    任一次读不出来 ⇒ 抛 :class:`_Unmeasurable`，**绝不返回差值**：

    - ``before`` 读不出来 ⇒ **不执行删除**：无法确认清了什么就动手，是拿数据
      赌一个数字；本轮跳过，下轮再试。
    - ``after`` 读不出来 ⇒ 删除**已经发生**，但行数无从得知 ⇒ 同样只报错误，
      不报数（错误串里会写明「已执行但不可知」）。这不是「清 0 条」。
    """
    before = _count_rows(table)
    if before is None:
        raise _Unmeasurable(f"COUNT({table}) before purge is unreadable — step skipped")
    purge()
    after = _count_rows(table)
    if after is None:
        raise _Unmeasurable(
            f"COUNT({table}) after purge is unreadable — DELETE ran but row count unknown"
        )
    return max(0, before - after)


def _purge_expired_sessions() -> int:
    """DB：过期 ``sessions`` 快照行。返回删除行数。**必须在孤儿清理之前跑。**

    行数**不取** ``purge_expired()`` 的返回值 —— 它内部也用「两次 COUNT 求差」
    统计，但那里的失败被折成 ``0``（见 :func:`_count_rows` 的坑）。这里改为**在
    GC 这一层**用可失败的量法重新统计一次：读不出来就跳过本轮（抛
    :class:`_Unmeasurable`），而不是继承一个可能编出来的正数。
    """
    from pa_agent.storage.sessions import purge_expired

    return _measured_delta("sessions", purge_expired)


def _purge_orphan_chat_turns() -> int:
    """DB：孤儿 ``chat_turns`` 行（其 session_id 已不在 ``sessions`` 表中）。

    **依赖上一步已经跑过**（见模块 docstring 约束 1）。单独调用它永远判不出孤儿。
    """
    from pa_agent.storage.sessions import purge_for_user_sessions

    return _measured_delta("chat_turns", purge_for_user_sessions)


def _purge_anonymous_chat_turns() -> int:
    """DB：**无会话身份**的 ``chat_turns`` 行（``session_id = ''``），只按时间戳。

    与上一步**故意解耦**：它不查 ``sessions`` 表，所以既不依赖
    ``purge_expired()`` 有没有先跑，也不该因为上一步失败而被跳过。缺了它，
    「无 ``X-Session-Id`` 的调用方」写下的行**永远不会被清** —— 实测 6 条 30 天前
    的这类行跑完两个 purge 与多轮 GC 后一行不少，而可观测字段全报 0。

    单独成一个字段（``chat_turns_anonymous_purged``）而不是并进
    ``chat_turns_purged``：后者衡量的是「孤儿判定清出了什么」，两条判据混在
    一个数字里就再也分不清是哪个机制在起作用、哪个机制在空转。
    """
    from pa_agent.storage.sessions import purge_anonymous_chat_turns

    return _measured_delta("chat_turns", purge_anonymous_chat_turns)


# ── 一轮 pass ────────────────────────────────────────────────────────────────
def _step(errors: list[str], name: str, fn: Callable[[], Any]) -> Optional[int]:
    """跑一个清理步骤，异常只记 warning 并记进 ``errors``，绝不冒泡。

    返回 ``int`` 表示「这一步清了多少」，返回 **None** 表示「**不知道清了多少**」
    —— 抛了异常（含 :class:`_Unmeasurable`：计数读不出来）时一律 None，错误串
    已经落进 ``errors``。**None 与 0 必须分开**：0 是「查过了，确实没东西可清」，
    None 是「没查成」。调用方据此决定后续依赖步骤能不能跑（见 :func:`run_once`
    的顺序分支）。

    分步兜底的理由：某一处（DB 锁、某个模块 import 失败）不该让其余几处也不跑。
    顺序敏感的两步放在同一个 ``_storage_ready()`` 分支里，且上游失败时下一步
    **跳过并标注** —— 顺序保护与「不装作没失败」必须同时成立。
    """
    try:
        value = fn()
        return int(value) if value is not None else None
    except Exception as exc:  # noqa: BLE001 - 后台线程，任何异常只记 warning
        logger.warning("storage gc: step %s failed: %s", name, exc)
        errors.append(f"{name}: {exc}")
        return None


def run_once() -> Optional[dict[str, Any]]:
    """跑一轮清理。返回本轮统计，或 None（本轮被单飞守卫跳过）。

    **永不抛异常、永不与自身重叠。** 统计 dict 同时进 ``/api/health``。

    **计数为 0 时先看 ``errors`` / ``skipped``**：``0`` 既可能是「查过、确实没东西
    可清」，也可能是「没查成/没跑」被降级成 0（见 :func:`_step`）。两者只有靠
    ``errors``（读不出来、步骤抛错）与 ``skipped``（因前置失败或顺序而跳过）才能
    区分开 —— 这正是「可观测性不许编数字」这条约束的落点。
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
        "chat_turns_anonymous_purged": 0,
        "errors": [],
        "skipped": [],
    }
    try:
        summary["registry_evicted"] = _step(
            summary["errors"], "registry.sweep", _sweep_registry
        ) or 0
        summary["chat_sessions_evicted"] = _step(
            summary["errors"], "chat_sessions", _sweep_chat_sessions
        ) or 0
        if _step(summary["errors"], "storage_ready", _storage_ready):
            # ── 顺序敏感：这两步必须按序，且同在一个分支内 ──────────────────
            # purge_for_user_sessions() 的孤儿判定是
            # ``session_id NOT IN (SELECT session_id FROM sessions)``；
            # 只有 purge_expired() 能把过期行从 sessions 里拿掉。
            # 反序执行会让孤儿永远判不出来 —— 表现为「每轮都清 0 条」。
            sessions_purged = _step(
                summary["errors"], "sessions.purge_expired", _purge_expired_sessions
            )
            if sessions_purged is None:
                summary["sessions_purged"] = 0
                # 上游没跑成 ⇒ sessions 里的过期行还在 ⇒ 孤儿步**必然**清不出
                # 东西。照跑只会报出一个无法与「真没孤儿」区分的 0（实测：
                # errors 只有上游那一条、skipped 为空、chat_turns_purged:0）。
                # 跳过 + 显式标注，顺序保护与可观测性一起守住。
                summary["skipped"].append(
                    "chat_turns.purge_for_user_sessions: skipped — "
                    "sessions.purge_expired did not complete this round "
                    "(orphan rows would be undetectable)"
                )
            else:
                summary["sessions_purged"] = sessions_purged
                summary["chat_turns_purged"] = _step(
                    summary["errors"],
                    "chat_turns.purge_for_user_sessions",
                    _purge_orphan_chat_turns,
                ) or 0
            # ── 顺序无关：只按时间戳的无身份行清理，上一步成败都照跑 ────────
            summary["chat_turns_anonymous_purged"] = _step(
                summary["errors"],
                "chat_turns.purge_anonymous",
                _purge_anonymous_chat_turns,
            ) or 0
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
        + summary["chat_turns_anonymous_purged"]
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

    **怎么读 ``last``**：``*_purged`` 是「上一轮清了多少行」，**只有在同轮的
    ``errors`` / ``skipped`` 都为空时**才是「真的清了这些」。计数读不出来
    （``_count_rows`` 返回 None）或步骤抛错时，该字段被降级成 0 并在
    ``errors`` 里留下一条自带原因的记录；因顺序或前置失败而没跑的步骤则出现在
    ``skipped`` 里。**这就是 ``_count_rows`` 的 None 语义在 health 上的落点**：
    「不知道」永远带着它的原因一起出现，不会伪装成「一切正常，清了 N 条」。
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
