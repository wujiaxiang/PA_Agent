"""``web.api.storage_gc`` 单测：周期清理**真的会清**，且**不会编数字**。

**每个用例都必须证明「数据没了」**，不能只断言「函数被调用过」—— mock 掉
``purge_*`` 之后测试永远绿，而那恰恰是本模块存在的理由：清理函数早就写好、
却长期没有任何生产调用者，没人发现它们根本没跑。故本文件一律造真实数据
（真 SQLite 行 / 真内存条目 / 真 asyncio 锁）再跑真 ``run_once()``。

守护的不变式（见 storage_gc 模块 docstring）：

1. 顺序敏感：``purge_expired()`` 必须先于 ``purge_for_user_sessions()``；且上一步
   失败时孤儿步要**跳过并标进 skipped**，不能照跑出一个无法与「真没孤儿」区分的 0
2. 不绕过 ``SessionRegistry`` 自己的淘汰策略（会跳过仍挂 SSE 队列的会话）
3. 任何异常只记 warning、绝不让守卫永久闭锁
4. **可观测性不许编数字**：``COUNT(*)`` 读不出来时（None）必须跳过本轮并记进
   ``errors``，绝不返回 ``before - 0`` 那种凭空捏造的正数
5. 无会话身份（``session_id=''``）的行有独立的**只按时间戳**的清理路径，且它与
   上面第 1 条的顺序**无关**

反向验证（把实现改坏，确认测试变红）见各用例注释里标「反向验证」的部分。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from pa_agent.storage import chat_repo, ephemeral, sessions
from pa_agent.storage.db import get_hub, reset_hub_for_tests
from web.api import storage_gc

#: 孤儿追问的保留期是 7 天（purge_for_user_sessions 的 SQL），故要造出
#: 「可被清掉」的孤儿必须用 8 天前的时间戳。
_EIGHT_DAYS_MS = int((8 * 86400) * 1000)


# ── 夹具 ─────────────────────────────────────────────────────────────────────
@pytest.fixture()
def db(tmp_path: Path):
    """每个用例一个独立 DB 文件（与 test_storage_layer 同款），退出时还原。"""
    hub = reset_hub_for_tests(tmp_path / "gc.db")
    yield hub
    hub.close_all()
    reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


@pytest.fixture(autouse=True)
def clean_gc_state():
    """清掉上一用例可能残留的 GC 线程 / 统计 / 守卫。

    守卫必须复位：某个用例若在 pass 中途死掉（断言失败、线程被杀），``_busy``
    会永远停在 True，之后**所有**用例的 ``run_once()`` 都返回 None 且不报错 ——
    表现为「测试莫名其妙全绿但什么都没跑」，是最难查的一类假绿。
    """
    storage_gc.stop(timeout=1.0)
    storage_gc._last_result = None
    storage_gc._last_run_ts = 0.0
    storage_gc._next_run_ts = 0.0
    storage_gc._interval_s = 0.0
    storage_gc._guard._busy = False
    yield
    storage_gc.stop(timeout=1.0)


@pytest.fixture()
def chat_table():
    """``routes_chat._chat_sessions`` 的独立副本，并让「事件循环侧清理器」缺席。

    真实模块里那条清理 task 挂在 router 级 ``on_event("startup")`` 上，且
    ``APIRouter.include_router`` **确实转发** ``on_startup``（router 级 startup 在
    TestClient 下真的会跑，由 ``tests/unit/test_router_startup_forwards.py`` 锁定）。
    本夹具把它摘掉，是为了**单独**驱动 ``storage_gc`` 这条兜底路径 ——
    另有 ``test_chat_sweep_defers_to_a_live_loop_task`` 覆盖「task 活着就让位」。
    ⚠️ 本夹具的旧注释称「FastAPI 0.142 起不再转发 router 级 startup ⇒ 它从未
    启动」，该结论已被证伪，勿据此改生产代码。
    """
    from web.api import routes_chat

    saved = dict(routes_chat._chat_sessions)
    saved_task = routes_chat._chat_cleanup_task
    routes_chat._chat_sessions.clear()
    routes_chat._chat_cleanup_task = None
    yield routes_chat._chat_sessions
    routes_chat._chat_sessions.clear()
    routes_chat._chat_sessions.update(saved)
    routes_chat._chat_cleanup_task = saved_task


def _rows(table: str) -> int:
    """表行数。**读失败直接判失败**：断言里 0 与 None 同形，会把「读不出来」误
    判成「数据没了」—— 那正是本文件要防的那一类假绿。"""
    row = get_hub().query_one(f"SELECT COUNT(*) AS n FROM {table}")   # noqa: S608
    assert row is not None, f"COUNT({table}) 读失败（DB 降级），断言无意义"
    return int(row["n"])


@contextmanager
def failing_counts(table: str, *, nth: int = 2):
    """让该表的第 *nth* 次 ``COUNT(*)`` 读失败（返回 ``None``）。

    复刻真实的失败形态：``hub.query()`` 读失败时返回 ``[]``，于是
    ``query_one()`` 返回 ``None`` —— 与「SELECT 没返回行」同形。
    ``nth=1`` ⇒ 删除**前**那次读失败；``nth=2`` ⇒ 删除**后**那次读失败。
    """
    hub = get_hub()
    original = hub.query_one
    seen = {"n": 0}

    def _patched(sql: str, params: tuple = ()):
        if f"FROM {table}" in sql and "COUNT(*)" in sql:
            seen["n"] += 1
            if seen["n"] == nth:
                return None
        return original(sql, params)

    hub.query_one = _patched                      # type: ignore[method-assign]
    try:
        yield
    finally:
        hub.query_one = original                  # type: ignore[method-assign]


def _turns_of(thread_key: str) -> int:
    return len(chat_repo.list_turns(thread_key))


# ── DB：sessions 表 ───────────────────────────────────────────────────────────
def test_pass_really_purges_expired_session_rows(db):
    """过期行**真的**从表里消失，且**在用**的行毫发无损。"""
    sessions.ensure_session("live", ttl_s=600)
    sessions.ensure_session("dead", ttl_s=-1)
    assert _rows("sessions") == 2

    summary = storage_gc.run_once()

    assert summary is not None
    assert summary["sessions_purged"] == 1, summary
    assert _rows("sessions") == 1
    assert sessions.get_session("dead") is None
    assert sessions.get_session("live") is not None, "还在用的会话被清了"
    assert sessions.get_session("live")["session_id"] == "live"
    assert summary["errors"] == []


def test_second_pass_is_a_noop(db):
    """幂等：没有新过期数据时第二轮清 0 —— 也是「没在重复删活数据」的证据。"""
    sessions.ensure_session("dead", ttl_s=-1)
    assert storage_gc.run_once()["sessions_purged"] == 1
    second = storage_gc.run_once()
    assert second["sessions_purged"] == 0
    assert second["chat_turns_purged"] == 0, "无孤儿时不该报删除数"
    assert second["total"] == 0
    assert _rows("sessions") == 0


# ── DB：孤儿 chat_turns（**顺序敏感**）────────────────────────────────────────
def test_orphan_turns_purged_only_after_session_row_is_gone(db):
    """顺序敏感的核心用例。

    ``purge_for_user_sessions()`` 的判据是
    ``session_id NOT IN (SELECT session_id FROM sessions)`` —— 单独跑它
    永远判不出任何孤儿（下面先证明这一点），只有 ``purge_expired()`` 先把
    过期行拿掉，这一轮才可能清出东西。``run_once()`` 必须按这个次序。
    """
    sessions.ensure_session("dead", ttl_s=-1)
    sessions.ensure_session("live", ttl_s=600)
    old = int((time.time() - 8 * 86400) * 1000)
    assert chat_repo.append_turn(
        thread_key="t-dead", turn=1, user="q", assistant="a",
        session_id="dead", ts_ms=old,
    )
    assert chat_repo.append_turn(
        thread_key="t-live", turn=1, user="q", assistant="a",
        session_id="live", ts_ms=old,
    )
    assert _turns_of("t-dead") == 2 and _turns_of("t-live") == 2

    # ── 反向验证：顺序反了会怎样（直接单调孤儿清理，不先清 sessions）──────
    assert sessions.purge_for_user_sessions() == 1, "DELETE 本身执行成功了"
    assert _turns_of("t-dead") == 2, "行还在 —— 因为 session_id 仍在 sessions 里"

    summary = storage_gc.run_once()

    assert summary["sessions_purged"] == 1
    assert summary["chat_turns_purged"] == 2, "一次追问落库是 user+assistant 两行"
    assert _turns_of("t-dead") == 0, "孤儿追问没被清掉（顺序被破坏了？）"
    assert _turns_of("t-live") == 2, "在用会话的追问历史被误清"
    assert sessions.get_session("live") is not None


def test_orphan_purge_keeps_recent_and_empty_session_rows(db):
    """孤儿 DELETE **只**碰「有身份 + 已孤儿 + 超 7 天」的行。

    两条不误清：① 孤儿但不足 7 天 ② ``session_id`` 为空（无会话身份的行 ——
    前者本来就清不到它们，清它们是另一条 DELETE 的职责，见下面一节）。
    """
    sessions.ensure_session("dead", ttl_s=-1)
    now_ms = int(time.time() * 1000)
    assert chat_repo.append_turn(
        thread_key="t-recent", turn=1, user="q", assistant="a",
        session_id="dead", ts_ms=now_ms,
    )
    assert chat_repo.append_turn(
        thread_key="t-nosid", turn=1, user="q", assistant="a",
        session_id="", ts_ms=int((time.time() - 8 * 86400) * 1000),
    )
    assert sessions.purge_expired() == 1

    assert sessions.purge_for_user_sessions() == 1
    assert _turns_of("t-recent") == 2, "不足 7 天的孤儿不该被清"
    assert _turns_of("t-nosid") == 2, "这条 DELETE 不负责无身份的行"


# ── DB：无会话身份（session_id='')的 chat_turns —— 绝对上限 DELETE ───────────
def test_anonymous_turns_really_purged_by_gc(db):
    """**破口①的核心用例**：``session_id=''`` 的行会被 GC 真的清掉。

    此前这些行**没有任何清理入口**：``purge_for_user_sessions()`` 的孤儿判定带
    ``session_id != ''``，把它们显式排除；而没有 ``X-Session-Id`` 的调用方
    （直连 API / 脚本 / 老客户端）写的恰恰全是它们。实测 6 条 30 天前的这类行
    跑完两个 purge 与多轮 GC 后一行不少，而 ``chat_turns_purged`` 全报 0。

    反向验证：去掉 ``run_once()`` 里对 ``_purge_anonymous_chat_turns`` 的调用，
    本用例立刻红（``chat_turns_anonymous_purged == 6`` 与行数归零两条断言）。
    """
    old = int((time.time() - 40 * 86400) * 1000)
    for i in range(3):                      # 3 轮追问 = 6 行（user + assistant）
        assert chat_repo.append_turn(
            thread_key=f"t-nosid-{i}", turn=1, user="q", assistant="a",
            session_id="", ts_ms=old,
        )
    assert _rows("chat_turns") == 6

    # 前置证明：既有两条 purge 对它们**完全无效**（这就是破口①本身）
    sessions.purge_expired()
    sessions.purge_for_user_sessions()
    assert _rows("chat_turns") == 6, "孤儿 DELETE 不该碰无身份的行（前提变了？）"

    summary = storage_gc.run_once()

    assert summary is not None
    assert summary["chat_turns_anonymous_purged"] == 6, summary
    assert summary["errors"] == [] and summary["skipped"] == [], summary
    assert _rows("chat_turns") == 0, "无身份的超期行没被清"
    for i in range(3):
        assert _turns_of(f"t-nosid-{i}") == 0
    # 幂等：第二轮没有新数据就必须是 0，且不许报错
    second = storage_gc.run_once()
    assert second["chat_turns_anonymous_purged"] == 0, second


def test_anonymous_purge_keeps_fresh_and_identified_rows(db):
    """**不误删**：30 天上限远大于一切正常写入，两类行都必须留着。"""
    now_ms = int(time.time() * 1000)
    assert chat_repo.append_turn(
        thread_key="t-fresh-nosid", turn=1, user="q", assistant="a",
        session_id="", ts_ms=now_ms,
    )
    sessions.ensure_session("live", ttl_s=600)
    assert chat_repo.append_turn(
        thread_key="t-old-with-id", turn=1, user="q", assistant="a",
        session_id="live", ts_ms=int((time.time() - 40 * 86400) * 1000),
    )

    summary = storage_gc.run_once()

    assert summary["chat_turns_anonymous_purged"] == 0, summary
    assert _turns_of("t-fresh-nosid") == 2, "刚写入的无身份行被超期 DELETE 误清"
    assert _turns_of("t-old-with-id") == 2, "有会话身份的 40 天前行不该被这条 DELETE 清"


def test_anonymous_purge_is_independent_of_session_purge_order(monkeypatch, db):
    """**顺序无关**：``purge_expired`` 失败时无身份行照样被清（它不查 sessions 表）。

    这是它与孤儿 DELETE 的根本差别，也是「为什么不去改那个 ``AND``」的另一半理由：
    顺序耦合只属于判据含子查询的那一条。反向验证：把它挪进「purge_expired 成功」
    分支里，本用例立刻红。
    """
    old = int((time.time() - 40 * 86400) * 1000)
    for i in range(2):
        assert chat_repo.append_turn(
            thread_key=f"t-nosid-{i}", turn=1, user="q", assistant="a",
            session_id="", ts_ms=old,
        )

    def _boom() -> int:
        raise RuntimeError("db locked")

    monkeypatch.setattr(storage_gc, "_purge_expired_sessions", _boom)
    summary = storage_gc.run_once()

    assert summary is not None
    assert any("sessions.purge_expired" in e for e in summary["errors"]), summary
    assert summary["chat_turns_anonymous_purged"] == 4, summary
    assert _rows("chat_turns") == 0


def test_anonymous_retention_is_a_parameter_not_a_constant(db):
    """保留期可调（测试用），且默认是 30 天 —— 远超 sessions TTL(30min) 与孤儿宽限(7d)。"""
    assert sessions.ANONYMOUS_TURN_RETENTION_S == 30 * 86400
    old = int((time.time() - 10 * 86400) * 1000)
    assert chat_repo.append_turn(
        thread_key="t-10d", turn=1, user="q", assistant="a", session_id="", ts_ms=old,
    )
    assert sessions.purge_anonymous_chat_turns() == 1
    assert _turns_of("t-10d") == 2, "10 天的无身份行不该被 30 天上限清掉"

    assert sessions.purge_anonymous_chat_turns(retention_s=5 * 86400) == 1
    assert _turns_of("t-10d") == 0, "调小保留期后应当被清"


# ── 内存：会话注册表 ─────────────────────────────────────────────────────────
def test_pass_really_evicts_expired_registry_sessions(monkeypatch, db):
    """注册表里的过期会话**真的**被淘汰（TTL=0 ⇒ 立刻过期）。"""
    reg = ephemeral.SessionRegistry(
        backend=ephemeral.InMemoryBackend(max_sessions=8, default_ttl_s=0.0)
    )
    monkeypatch.setattr(ephemeral, "_registry", reg)
    reg.get_or_create("ghost")
    assert len(reg.all_sessions()) == 1

    summary = storage_gc.run_once()

    assert summary["registry_evicted"] == 1
    assert reg.all_sessions() == [], "过期会话没被淘汰"
    assert reg.get("ghost") is None


def test_gc_does_not_bypass_registry_sse_protection(monkeypatch, db):
    """**不绕过注册表自己的淘汰策略**：仍挂 SSE 队列的会话不能被踢。

    直接往 backend 里塞第三条来制造「超限」状态 —— ``get_or_create`` 自己就会
    在每次新建时淘汰，构造不出「淘汰发生在 sweep 期间」的场景。反向验证：若
    GC 改成自己遍历 ``all_sessions()`` 去 pop，本用例立刻红（队列持有者被删）。
    """
    reg = ephemeral.SessionRegistry(
        backend=ephemeral.InMemoryBackend(max_sessions=2, default_ttl_s=10_000)
    )
    monkeypatch.setattr(ephemeral, "_registry", reg)
    holder = reg.get_or_create("keep-sse")
    holder.sse_queue = asyncio.Queue()          # 正挂着流式连接
    reg.get_or_create("normal")
    reg._backend._sessions["overflow"] = ephemeral.SessionState("overflow")

    summary = storage_gc.run_once()

    remaining = {s.session_id for s in reg.all_sessions()}
    assert "keep-sse" in remaining, "GC 绕过了注册表的 SSE 保护"
    assert holder.sse_queue is not None
    assert summary["registry_evicted"] == 1, "超限的普通会话应被 LRU 踢掉"
    assert remaining == {"keep-sse", "overflow"}


def test_capacity_eviction_logs_exactly_one_warning(monkeypatch, db, caplog):
    """一次淘汰只记一条 warning —— ``_evict_locked`` 曾把同一条**逐字重复**两次。

    重复日志不只是噪音：排查「会话被谁踢了」时，两条一模一样的信息会让日志
    看起来像「踢了两个会话」，与「只踢了一个」的事实不符（实测 3 次 warning
    里 2 次完全相同）。

    反向验证：把删掉的那条 warning 加回去，本用例立刻红。
    """
    reg = ephemeral.SessionRegistry(
        backend=ephemeral.InMemoryBackend(max_sessions=1, default_ttl_s=10_000)
    )
    monkeypatch.setattr(ephemeral, "_registry", reg)
    reg.get_or_create("first")

    with caplog.at_level("WARNING", logger="pa_agent.storage.ephemeral"):
        reg.get_or_create("second")

    evictions = [r.getMessage() for r in caplog.records if "evicted LRU session" in r.getMessage()]
    assert len(evictions) == 1, evictions
    assert "evicted LRU session first" in evictions[0], evictions


def test_registry_scratch_keys_are_swept_too(monkeypatch, db):
    """``sweep()`` 顺带清各会话的过期 scratch 键 —— 证明走的确实是注册表入口。"""
    reg = ephemeral.SessionRegistry(
        backend=ephemeral.InMemoryBackend(max_sessions=8, default_ttl_s=10_000)
    )
    monkeypatch.setattr(ephemeral, "_registry", reg)
    state = reg.get_or_create("s")
    state.set("dead", 1, ttl_s=-1)
    state.set("live", 2, ttl_s=600)

    storage_gc.run_once()

    assert state.get("dead") is None
    assert state.get("live") == 2


# ── 内存：追问会话 ───────────────────────────────────────────────────────────
def test_expired_chat_session_really_evicted(chat_table, db):
    chat_table["old"] = {"session": object(), "last_touch": time.time() - 10_000,
                          "lock": asyncio.Lock()}
    chat_table["new"] = {"session": object(), "last_touch": time.time(),
                         "lock": asyncio.Lock()}

    summary = storage_gc.run_once()

    assert summary["chat_sessions_evicted"] == 1, summary
    assert "old" not in chat_table, "过期追问会话没被回收"
    assert "new" in chat_table, "还在 TTL 内的追问会话被误删"


def test_chat_session_with_held_lock_is_not_evicted(chat_table, db):
    """锁被占用的条目跳过：``asyncio.Lock`` 归事件循环所有，后台线程不能 release。"""

    async def _hold() -> asyncio.Lock:
        lock = asyncio.Lock()
        await lock.acquire()
        return lock

    held = asyncio.run(_hold())
    chat_table["busy"] = {"session": object(), "last_touch": time.time() - 10_000,
                          "lock": held}

    summary = storage_gc.run_once()

    assert summary["chat_sessions_evicted"] == 0
    assert "busy" in chat_table, "正在被用的追问会话被回收了"


def test_chat_sweep_defers_to_a_live_loop_task(chat_table, db, monkeypatch):
    """事件循环侧清理器活着时**不与它抢同一批条目**（否则又多一个 pop 竞态窗口）。"""
    from web.api import routes_chat

    chat_table["old"] = {"session": object(), "last_touch": time.time() - 10_000,
                         "lock": asyncio.Lock()}

    class _LiveTask:
        def done(self) -> bool:
            return False

    monkeypatch.setattr(routes_chat, "_chat_cleanup_task", _LiveTask(), raising=False)
    assert storage_gc.run_once()["chat_sessions_evicted"] == 0
    assert "old" in chat_table

    class _DeadTask:
        def done(self) -> bool:
            return True

    monkeypatch.setattr(routes_chat, "_chat_cleanup_task", _DeadTask(), raising=False)
    assert storage_gc.run_once()["chat_sessions_evicted"] == 1
    assert "old" not in chat_table


# ── 单飞守卫 ─────────────────────────────────────────────────────────────────
def test_overlapping_pass_is_skipped(monkeypatch, db):
    """重叠的 pass 绝不能同时跑：第二个调用直接返回 None，且不重复执行。"""
    entered = threading.Event()
    calls: list[int] = []

    def _slow_sweep() -> int:
        calls.append(1)
        entered.set()
        time.sleep(0.3)
        return 0

    monkeypatch.setattr(storage_gc, "_sweep_registry", _slow_sweep)
    worker = threading.Thread(target=storage_gc.run_once)
    worker.start()
    try:
        assert entered.wait(3), "第一轮没进入 sweep"
        assert storage_gc.run_once() is None, "重叠的 pass 没被守卫拦住"
    finally:
        worker.join(timeout=5)
    assert len(calls) == 1, "重叠 pass 真的执行了"
    assert worker.is_alive() is False
    # 守卫已释放：紧接着的一轮正常执行
    assert storage_gc.run_once() is not None


def test_step_failure_is_contained_and_releases_guard(monkeypatch, db):
    """一步抛异常：只记 warning + 进 errors，其余步骤照跑，守卫不被闭锁。"""
    sessions.ensure_session("dead", ttl_s=-1)

    def _boom() -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(storage_gc, "_sweep_registry", _boom)
    summary = storage_gc.run_once()

    assert summary is not None
    assert any("boom" in e for e in summary["errors"]), summary["errors"]
    assert summary["sessions_purged"] == 1, "一步失败不该连累 DB 清理"
    assert _rows("sessions") == 0
    # 守卫必须已释放：第二步仍能跑完（同样的异常被再次记录，而不是返回 None）
    again = storage_gc.run_once()
    assert again is not None
    assert any("boom" in e for e in again["errors"])


def test_run_once_never_raises(monkeypatch, db, chat_table):
    """每一步都炸时也不能冒泡 —— 它跑在后台线程里，冒泡只会变成无声的线程死掉。"""
    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    for name in ("_sweep_registry", "_sweep_chat_sessions", "_storage_ready",
                 "_purge_expired_sessions", "_purge_orphan_chat_turns",
                 "_purge_anonymous_chat_turns"):
        monkeypatch.setattr(storage_gc, name, _boom)
    summary = storage_gc.run_once()
    assert summary is not None
    # storage_ready 炸了 ⇒ 判不出 DB 可用 ⇒ 后面三步**根本没跑**（顺序与前置
    # 条件都保住），故 errors 只有 3 条。
    assert len(summary["errors"]) == 3, summary["errors"]
    assert summary["total"] == 0
    assert summary["sessions_purged"] == 0 and summary["chat_turns_purged"] == 0
    assert summary["chat_turns_anonymous_purged"] == 0


def test_db_step_failure_is_contained(monkeypatch, db):
    """``sessions`` 清不掉时：孤儿步**跳过并标进 skipped**，且绝不被当成「跑过了」。

    上游失败时孤儿判定必然清不出东西，而它报 0 与「真的没孤儿」无法区分 ——
    旧行为正是 ``errors:['sessions.purge_expired: db kaboom']`` / ``skipped:[]`` /
    ``chat_turns_purged:0``，一条顺序故障长成了「一切正常」。

    反向验证：去掉「上游失败 ⇒ 跳过并标注」的分支（改回照跑），本用例的
    ``skipped`` 与「孤儿步不在 errors 里」两条断言立刻红。
    """
    def _boom(*_a, **_k):
        raise RuntimeError("db kaboom")

    monkeypatch.setattr(storage_gc, "_purge_expired_sessions", _boom)
    monkeypatch.setattr(storage_gc, "_purge_orphan_chat_turns", _boom)
    summary = storage_gc.run_once()
    assert summary is not None
    assert any("sessions.purge_expired" in e for e in summary["errors"]), summary
    # 孤儿步**根本没跑**：既不抛错（不在 errors 里），也不该伪装成「跑过了，清 0 条」
    assert not any("purge_for_user_sessions" in e for e in summary["errors"]), summary
    assert any("purge_for_user_sessions" in s and "skipped" in s
               for s in summary["skipped"]), summary["skipped"]
    assert summary["sessions_purged"] == 0
    assert summary["chat_turns_purged"] == 0


# ── 可观测性：读不出来 ≠ 清 0 条（**绝不返回编造的差值**）────────────────────
def test_count_rows_returns_none_on_read_failure(db):
    """``_count_rows`` 的失败语义是 **None**，不是 0。

    反向验证：把 ``return None`` 改回 ``return 0``，本用例立刻红 —— 而下面三条
    用例都建立在这个语义上，会跟着一起红。
    """
    sessions.ensure_session("live", ttl_s=600)
    assert storage_gc._count_rows("sessions") == 1
    with failing_counts("sessions", nth=1):
        assert storage_gc._count_rows("sessions") is None


def test_purge_reports_nothing_when_before_count_is_unreadable(db):
    """**删除前**读不出来 ⇒ 跳过本轮、不删、不报数，并留下自带原因的错误。

    「拿数据赌一个数字」不是可接受的行为：连清了什么都不知道就先动手。
    """
    sessions.ensure_session("dead", ttl_s=-1)
    assert _rows("sessions") == 1

    with failing_counts("sessions", nth=1):
        summary = storage_gc.run_once()

    assert summary is not None
    assert summary["sessions_purged"] == 0, summary
    assert any("COUNT(sessions) before purge" in e for e in summary["errors"]), summary
    assert _rows("sessions") == 1, "读不出来时不该盲删"


def test_purge_never_reports_a_fabricated_delta(db):
    """**破口③的核心用例**：第二次 COUNT 读失败时，绝不报出 ``before - 0``。

    旧行为实测：把第二次 COUNT 打成读失败后 ``_purge_orphan_chat_turns()`` 返回
    **18**，而 ``errors`` / ``skipped`` 全空 —— 「清了多少」这个字段在真正的故障
    时刻说了谎，而 R3 的全部可观测性挂在它上面。删除本身仍会发生（幂等，下轮
    重试），但**行数必须报「不知道」**。

    反向验证：把 ``_count_rows`` 改回失败返回 0，本用例立刻红（会看到 8）。
    """
    sessions.ensure_session("live", ttl_s=600)
    sessions.ensure_session("dead", ttl_s=-1)
    old = int((time.time() - 8 * 86400) * 1000)
    for key, sid in (("t-live", "live"), ("t-dead", "dead")):
        for i in range(2):
            assert chat_repo.append_turn(
                thread_key=f"{key}-{i}", turn=1, user="q", assistant="a",
                session_id=sid, ts_ms=old,
            )
    assert sessions.purge_expired() == 1          # 先让 dead 的行成为真孤儿
    assert _rows("chat_turns") == 8

    with failing_counts("chat_turns", nth=2):
        summary = storage_gc.run_once()

    assert summary is not None
    assert summary["chat_turns_purged"] == 0, (
        "第二次 COUNT 读失败时报出了一个编造的正数", summary
    )
    assert any("COUNT(chat_turns) after purge" in e for e in summary["errors"]), summary
    assert _turns_of("t-live-0") == 2, "在用会话的追问历史不能少"


def test_unmeasurable_step_is_distinguishable_from_zero(db):
    """None（没查成）与 0（查过、确实没东西可清）在 summary 里必须可区分。"""
    sessions.ensure_session("live", ttl_s=600)
    clean = storage_gc.run_once()
    assert clean["chat_turns_purged"] == 0 and clean["errors"] == []

    with failing_counts("chat_turns", nth=1):
        dirty = storage_gc.run_once()

    assert dirty["chat_turns_purged"] == 0
    assert dirty["errors"], "「没查成」必须留下错误，不能与干净的 0 同形"


def test_health_exposes_unmeasurable_as_an_error_not_a_number(chat_table, db):
    """None 语义一路贯通到 ``/api/health``：计数为 0 + 自带原因的错误条目。"""
    from fastapi.testclient import TestClient

    import web.server as server

    with failing_counts("chat_turns", nth=1):
        storage_gc.run_once()

    payload = TestClient(server.app).get("/api/health").json()
    last = payload["storage"]["gc"]["last"]
    assert last["chat_turns_purged"] == 0
    assert any("COUNT(chat_turns)" in e for e in last["errors"]), last


# ── DB 不可用时显式跳过（而不是「静默清 0 条」）────────────────────────────────
def test_db_steps_skipped_when_storage_not_initialized(db_path_isolated):
    summary = storage_gc.run_once()
    assert summary["sessions_purged"] == 0
    assert summary["chat_turns_purged"] == 0
    assert any("sqlite" in s for s in summary["skipped"]), summary["skipped"]
    assert summary["errors"] == []


# ── 线程生命周期 ─────────────────────────────────────────────────────────────
def test_start_is_idempotent_and_stop_joins(chat_table):
    t = storage_gc.start(interval_s=0.01)
    assert t is not None and t.is_alive(), "守护线程没起来"
    assert t.daemon is True, "必须是 daemon，否则会拖住进程退出"
    assert storage_gc.start(interval_s=0.01) is t, "重复 start 起了第二个线程"
    assert storage_gc.is_running() is True

    st = storage_gc.status()
    assert st["running"] is True
    assert st["interval_s"] == 60.0, "间隔应被 _MIN_INTERVAL_S 夹住"
    assert st["next_run_at"] is not None

    storage_gc.stop()
    assert t.is_alive() is False, "stop() 没有 join 线程"
    assert storage_gc.is_running() is False
    assert storage_gc.status()["next_run_at"] is None


def test_loop_really_runs_a_pass_by_itself(monkeypatch, chat_table):
    """不调 run_once，光靠 start() —— 线程自己就会跑第一轮。"""
    calls: list[int] = []

    def _count() -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(storage_gc, "_sweep_registry", _count)
    monkeypatch.setattr(storage_gc, "_FIRST_DELAY_S", 0.01)
    storage_gc.start(interval_s=600)
    try:
        deadline = time.time() + 5.0
        while not calls and time.time() < deadline:
            time.sleep(0.02)
    finally:
        storage_gc.stop()
    assert calls, "守护线程没有自动执行清理"


def test_default_interval_is_conservative():
    """间隔下限 60s、默认 600s：清理不是实时需求（各表 TTL 是 30 分钟）。"""
    assert storage_gc.DEFAULT_INTERVAL_S == 600.0
    assert storage_gc._MIN_INTERVAL_S == 60.0
    assert storage_gc.DEFAULT_INTERVAL_S < ephemeral.DEFAULT_TTL_S


# ── 可观测性 ─────────────────────────────────────────────────────────────────
def test_status_reports_last_result(db):
    sessions.ensure_session("dead", ttl_s=-1)
    storage_gc.run_once()
    st = storage_gc.status()
    assert st["last"]["sessions_purged"] == 1
    assert st["last"]["total"] == sum(
        st["last"][k] for k in
        ("registry_evicted", "chat_sessions_evicted", "sessions_purged",
         "chat_turns_purged", "chat_turns_anonymous_purged")
    )
    assert st["last"]["duration_ms"] >= 0.0
    assert st["last_run_at"] > 0
    for key in ("registry_evicted", "chat_sessions_evicted", "sessions_purged",
                "chat_turns_purged", "chat_turns_anonymous_purged", "errors", "skipped"):
        assert key in st["last"], f"health 少暴露了 {key}"


def test_status_before_any_pass(chat_table):
    st = storage_gc.status()
    assert st["running"] is False
    assert st["last"] is None
    assert st["next_run_at"] is None


def test_health_endpoint_exposes_gc(chat_table):
    """端到端：``/api/health`` 的 ``storage.gc`` 真的带上了最近一次结果。"""
    from fastapi.testclient import TestClient

    import web.server as server

    storage_gc.run_once()
    payload = TestClient(server.app).get("/api/health").json()
    gc = payload["storage"]["gc"]
    assert gc["running"] is False
    assert gc["last"] is not None and "total" in gc["last"]
    assert "chat_turns_anonymous_purged" in gc["last"], "health 少暴露了无身份行计数"
    for key in ("running", "interval_s", "next_run_at", "last_run_at", "last"):
        assert key in gc, f"/api/health 少暴露了 gc.{key}"
