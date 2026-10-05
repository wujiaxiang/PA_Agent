"""``web.api.storage_gc`` 单测：周期清理**真的会清**。

**每个用例都必须证明「数据没了」**，不能只断言「函数被调用过」—— mock 掉
``purge_*`` 之后测试永远绿，而那恰恰是本模块存在的理由：清理函数早就写好、
却长期没有任何生产调用者，没人发现它们根本没跑。故本文件一律造真实数据
（真 SQLite 行 / 真内存条目 / 真 asyncio 锁）再跑真 ``run_once()``。

守护的三条不变式（见 storage_gc 模块 docstring）：

1. 顺序敏感：``purge_expired()`` 必须先于 ``purge_for_user_sessions()``
2. 不绕过 ``SessionRegistry`` 自己的淘汰策略（会跳过仍挂 SSE 队列的会话）
3. 任何异常只记 warning、绝不让守卫永久闭锁

反向验证（把实现改坏，确认测试变红）见各用例注释里标「反向验证」的部分。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
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

    真实模块里那条清理 task 挂在 router 级 ``on_event("startup")`` 上，而
    FastAPI 0.142 起**不再转发** router 级 startup 事件 ⇒ 它从未启动。
    用例默认按这个真实状态跑（``_chat_cleanup_task = None``）。
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
    row = get_hub().query_one(f"SELECT COUNT(*) AS n FROM {table}")   # noqa: S608
    return int(row["n"]) if row else 0


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
    """两条不误清：① 孤儿但不足 7 天 ② session_id 为空（无会话身份的行）。"""
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
    assert _turns_of("t-nosid") == 2, "无会话身份的追问不该被清"


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
    """四步全炸时也不能冒泡 —— 它跑在后台线程里，冒泡只会变成无声的线程死掉。"""
    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    for name in ("_sweep_registry", "_sweep_chat_sessions", "_storage_ready",
                 "_purge_expired_sessions", "_purge_orphan_chat_turns"):
        monkeypatch.setattr(storage_gc, name, _boom)
    summary = storage_gc.run_once()
    assert summary is not None
    # storage_ready 炸了 ⇒ 判不出 DB 可用 ⇒ 后面两步**根本没跑**（顺序与前置
    # 条件都保住），故 errors 只有 3 条。
    assert len(summary["errors"]) == 3, summary["errors"]
    assert summary["total"] == 0
    assert summary["sessions_purged"] == 0 and summary["chat_turns_purged"] == 0


def test_db_step_failure_is_contained(monkeypatch, db):
    """DB 可用但两个删除都炸：仍返回统计，错误逐条落到 errors 里。"""
    def _boom(*_a, **_k):
        raise RuntimeError("db kaboom")

    monkeypatch.setattr(storage_gc, "_purge_expired_sessions", _boom)
    monkeypatch.setattr(storage_gc, "_purge_orphan_chat_turns", _boom)
    summary = storage_gc.run_once()
    assert summary is not None
    assert any("sessions.purge_expired" in e for e in summary["errors"]), summary
    assert any("chat_turns.purge_for_user_sessions" in e for e in summary["errors"])
    assert summary["sessions_purged"] == 0


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
        ("registry_evicted", "chat_sessions_evicted", "sessions_purged", "chat_turns_purged")
    )
    assert st["last"]["duration_ms"] >= 0.0
    assert st["last_run_at"] > 0
    for key in ("registry_evicted", "chat_sessions_evicted", "sessions_purged",
                "chat_turns_purged", "errors", "skipped"):
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
    for key in ("running", "interval_s", "next_run_at", "last_run_at", "last"):
        assert key in gc, f"/api/health 少暴露了 gc.{key}"
