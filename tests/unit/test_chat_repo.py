# -*- coding: utf-8 -*-
"""``chat_turns`` 仓储的守卫测试。

三条约定，各自有对应用例：

1. **一轮 = 两行**（user + assistant），且**必须同一事务** ——
   缺一半的轮次会让回读方看到「有提问没有回答」。
   ``test_append_writes_both_rows_in_order`` 与
   ``test_append_is_atomic_when_second_insert_fails`` 分别守住两半。
2. **DB 写失败不得冒泡** —— 调用点在追问 SSE 的生成线程里，用户此时已经
   拿到答案。``test_append_returns_false_when_db_degraded`` /
   ``test_append_never_raises_on_uninitialized_storage``。
3. **user_id 真源是 ``db.DEFAULT_USER_ID``（"admin"）**，且 L2 隔离真的生效
   —— ``test_user_id_defaults_to_admin_and_isolates_threads``。

**绝不写真实目录**：hub 一律重定向到 ``tmp_path``（见下方 ``db`` 夹具）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pa_agent.storage.chat_repo import (
    ROLE_ASSISTANT,
    ROLE_USER,
    append_turn,
    clear_thread,
    list_turns,
)
from pa_agent.storage.db import (
    DEFAULT_USER_ID,
    get_hub,
    reset_hub_for_tests,
)

THREAD = "sid-1|GATEIO/BTCUSDT/1d/2026-07-18_14-00-13|k"


@pytest.fixture()
def db(tmp_path: Path):
    """Hub 指向 tmp_path。收尾必须还原到会话级 DB —— 否则后续测试会连到一个
    已被 pytest 删掉的路径，``sqlite3.connect`` 会静默重建一个**空库**，
    于是所有人的 ``no such table`` 都被算到这次改动头上。"""
    hub = reset_hub_for_tests(tmp_path / "chat.db")
    yield hub
    hub.close_all()
    reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


def _turn(n: int, **over) -> bool:
    kw = dict(
        thread_key=THREAD,
        turn=n,
        user=f"问题{n}",
        assistant=f"回答{n}",
        session_id="sid-1",
        record_id="2026-07-18_14-00-13",
        symbol="BTCUSDT",
        timeframe="1d",
    )
    kw.update(over)
    return append_turn(**kw)


# ── 1. 写入形状：一轮两行，顺序稳定 ──────────────────────────────────────────

def test_append_writes_both_rows_in_order(db):
    assert _turn(1, reasoning="先看支撑", usage={"total_tokens": 42}) is True
    assert _turn(2) is True

    rows = list_turns(THREAD)
    assert [(r["turn"], r["role"]) for r in rows] == [
        (1, ROLE_USER), (1, ROLE_ASSISTANT),
        (2, ROLE_USER), (2, ROLE_ASSISTANT),
    ]
    assert [r["content"] for r in rows] == ["问题1", "回答1", "问题2", "回答2"]

    first = rows[0]
    assert first["session_id"] == "sid-1"
    assert first["record_id"] == "2026-07-18_14-00-13"
    assert (first["symbol"], first["timeframe"]) == ("BTCUSDT", "1d")
    assert first["reasoning"] is None, "reasoning 只属于 assistant 行"
    assert first["usage"] == {}, "usage_json 必须被解析成 usage dict"

    ai = rows[1]
    assert ai["reasoning"] == "先看支撑"
    assert ai["usage"] == {"total_tokens": 42}, "usage_json 必须被解析，不留裸串"
    assert "usage_json" not in ai, "解析后不应再把原始串暴露给调用方"


def test_cancelled_turn_is_marked_on_both_rows(db):
    assert _turn(1, assistant="", cancelled=True) is True
    rows = list_turns(THREAD)
    assert len(rows) == 2
    assert all(r["cancelled"] == 1 for r in rows), (
        "取消的一轮在库里必须有痕迹，否则「问过但没答」彻底消失"
    )
    assert [r["content"] for r in rows] == ["问题1", ""]


def test_append_is_atomic_when_second_insert_fails(db, monkeypatch):
    """assistant 行写失败时，user 行也必须回滚 —— 不留半轮。

    ``sqlite3.Connection`` 是不可 monkeypatch 的 C 类型，故用**代理连接**：
    真 hub 仍负责 ``with conn:`` 的 ROLLBACK，代理只负责在第 2 条 INSERT
    上抛错。这样验到的是真事务语义，而不是被替身糊弄过去。
    """
    import sqlite3

    import pa_agent.storage.chat_repo as cr

    class _FlakyConn:
        def __init__(self, real):
            self._real = real
            self.n = 0

        def execute(self, sql, *a, **k):
            if sql.lstrip().upper().startswith("INSERT INTO CHAT_TURNS"):
                self.n += 1
                if self.n == 2:
                    raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *a, **k)

    class _FlakyHub:
        def __init__(self, real):
            self._real = real
            self.conn = _FlakyConn(real.connect())

        def run_in_tx(self, fn):
            return self._real.run_in_tx(lambda _c: fn(self.conn))

    monkeypatch.setattr(cr, "get_hub", lambda: _FlakyHub(db))
    assert cr.append_turn(thread_key=THREAD, turn=1, user="q", assistant="a") is False
    monkeypatch.undo()

    assert list_turns(THREAD) == [], "半轮被留下来了：回读会看到有问无答"


def test_turn_number_must_be_int_and_key_non_empty(db):
    assert append_turn(thread_key=THREAD, turn="abc", user="x") is False
    assert append_turn(thread_key="   ", turn=1, user="x") is False
    assert list_turns(THREAD) == [], "参数非法时不得留下任何行"


# ── 2. 降级：DB 不可用时绝不冒泡 ─────────────────────────────────────────────

def test_append_returns_false_when_db_degraded(db, monkeypatch):
    import pa_agent.storage.chat_repo as cr

    monkeypatch.setattr(cr, "get_hub", lambda: _BrokenHub())
    assert cr.append_turn(thread_key=THREAD, turn=1, user="q", assistant="a") is False


class _BrokenHub:
    """``run_in_tx`` 抛异常的 hub：模拟传输层炸了，而不是 SQL 报错。"""

    def run_in_tx(self, _fn):
        raise RuntimeError("connection pool exhausted")


def test_append_never_raises_on_uninitialized_storage(tmp_path):
    """DB 未初始化（纯 GUI / CLI 模式）时返回 False 而不是抛异常。"""
    hub = reset_hub_for_tests(tmp_path / "never.db", initialize=False)
    try:
        assert append_turn(thread_key=THREAD, turn=1, user="q", assistant="a") is False
        assert list_turns(THREAD) == []
        assert clear_thread(THREAD) is False
    finally:
        hub.close_all()
        reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


# ── 3. user_id 与 L2 隔离 ────────────────────────────────────────────────────

def test_user_id_defaults_to_admin_and_isolates_threads(db):
    assert DEFAULT_USER_ID == "admin", "user_id 真源是 db.DEFAULT_USER_ID"
    _turn(1)
    _turn(2, thread_key="other-thread")

    rows = list_turns(THREAD)
    assert len(rows) == 2
    assert {r["user_id"] for r in rows} == {"admin"}

    # L2 隔离：别的用户看不到
    assert list_turns(THREAD, user_id="someone-else") == []
    # 同一用户下不同线程互不可见
    assert [r["turn"] for r in list_turns("other-thread")] == [2, 2]


# ── 4. 清线程 ────────────────────────────────────────────────────────────────

def test_clear_thread_only_clears_that_thread(db):
    _turn(1)
    _turn(1, thread_key="other-thread")

    assert clear_thread(THREAD) is True
    assert list_turns(THREAD) == []
    assert len(list_turns("other-thread")) == 2, "清一个线程不得误伤另一个"


def test_clear_missing_thread_is_not_a_failure(db):
    """删不存在的行不算失败（与 ``trade_repo.delete_trade`` 同口径）。"""
    assert clear_thread("never-existed") is True
    assert clear_thread("") is False, "空 key 是参数错误，不是「恰好没有」"
    assert list_turns("never-existed") == []


def test_limit_truncates_oldest_first(db):
    for i in range(1, 6):
        _turn(i)
    rows = list_turns(THREAD, limit=2)
    assert [r["turn"] for r in rows] == [1, 1], "limit 截断最早的轮次"


# ── 5. 接线：SSE 端点真的落库 ────────────────────────────────────────────────
#
# 上面全是仓储自身的用例。若只测到仓储，「路由里忘了调用」这个缺口本体
# 仍会全绿 —— 而缺口①当初正是这样漏过去的。因此这里从 HTTP 入口走一遍。


def _wait_rows(thread_key: str, n: int, timeout: float = 5.0) -> list[dict]:
    """轮询等 ``n`` 行落库。

    ``_run`` 跑在线程池里，而 SSE 在收到 ``done`` 后即结束响应 —— 测试拿到
    响应体的那一刻 worker 可能还在写。有界轮询是这里唯一诚实的做法：
    固定 sleep 会让测试在慢机器上假红，而可测的同步点根本不存在。
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = list_turns(thread_key)
        if len(rows) >= n:
            return rows
        time.sleep(0.02)
    return list_turns(thread_key)


@pytest.fixture()
def _clear_chat_sessions():
    from web.api import routes_chat

    routes_chat._chat_sessions.clear()
    yield
    routes_chat._chat_sessions.clear()


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events, cur_event, cur_data = [], None, []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("event: "):
            cur_event = line[len("event: "):].strip()
        elif line.startswith("data: "):
            cur_data.append(line[len("data: "):])
        elif line == "" and cur_event is not None:
            events.append((cur_event, json.loads("\n".join(cur_data))))
            cur_event, cur_data = None, []
    return events


def _chat_app(record):
    from fastapi import FastAPI

    from web.api.routes_chat import router as chat_router

    app = FastAPI()
    app.include_router(chat_router, prefix="/api")
    ctx = MagicMock()
    ctx._last_record = record
    ctx.data_source.latest_snapshot.return_value = []

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    return app


def _fake_session(content: str = "AI 回答", reasoning: str = "先看支撑"):
    from types import SimpleNamespace

    sess = MagicMock(name="FreeChatSession")
    sess.send.return_value = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        usage=SimpleNamespace(
            prompt_tokens=10, cached_prompt_tokens=2,
            completion_tokens=30, total_tokens=42,
        ),
    )
    return sess


def test_chat_stream_persists_turn_to_db(db, _clear_chat_sessions):
    """GET /api/chat/stream 成功后，``chat_turns`` 里必须出现这一轮。

    这是缺口②的正面守卫：**全仓零写入**时本用例必然失败。
    """
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    record = MagicMock()
    record._basename = "rec-1"
    record.meta.symbol = "BTCUSDT"
    record.meta.timeframe = "1d"
    session = _fake_session()

    with patch("web.api.routes_chat.FreeChatSession", return_value=session):
        with TestClient(_chat_app(record)) as c:
            resp = c.get("/api/chat/stream?text=止损放哪&record_id=rec-1")

    assert "done" in [e for e, _ in _sse_events(resp.text)]

    # 库里能查到：轮次 1、user/assistant 两行、内容与 token 用量都对得上。
    rows = _wait_rows("nosession|rec-1|BTCUSDT|1d|n", 2)
    assert len(rows) == 2, "SSE 成功但 chat_turns 没有落库 —— 追问历史仍在丢"
    assert [r["role"] for r in rows] == [ROLE_USER, ROLE_ASSISTANT]
    assert [r["content"] for r in rows] == ["止损放哪", "AI 回答"]
    assert rows[0]["record_id"] == "rec-1"
    assert (rows[0]["symbol"], rows[0]["timeframe"]) == ("BTCUSDT", "1d")
    assert rows[1]["reasoning"] == "先看支撑"
    assert rows[1]["usage"]["total_tokens"] == 42


def test_persist_failure_never_breaks_the_stream(db, _clear_chat_sessions, monkeypatch):
    """落库炸了，``done`` 事件照发 —— 审计写失败不得把追问变成 error。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    import pa_agent.storage.chat_repo as cr

    record = MagicMock()
    record._basename = "rec-boom"
    record.meta.symbol = "BTCUSDT"
    record.meta.timeframe = "1d"

    def boom(**_kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(cr, "append_turn", boom)

    with patch("web.api.routes_chat.FreeChatSession", return_value=_fake_session("答")):
        with TestClient(_chat_app(record)) as c:
            resp = c.get("/api/chat/stream?text=q&record_id=rec-boom")

    types = [e for e, _ in _sse_events(resp.text)]
    assert "done" in types and "error" not in types, "落库失败把追问变成了 error"


def test_cancelled_turn_is_persisted_as_cancelled(db, _clear_chat_sessions):
    """取消的追问也要在库里留痕（``cancelled=1``），否则审计里彻底消失。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from pa_agent.ai.deepseek_client import CancelledError

    record = MagicMock()
    record._basename = "rec-cancel"
    record.meta.symbol = "BTCUSDT"
    record.meta.timeframe = "1d"
    session = MagicMock(name="FreeChatSession")
    session.send.side_effect = CancelledError("用户取消")

    with patch("web.api.routes_chat.FreeChatSession", return_value=session):
        with TestClient(_chat_app(record)) as c:
            resp = c.get("/api/chat/stream?text=q&record_id=rec-cancel")

    assert "error" in [e for e, _ in _sse_events(resp.text)]

    rows = _wait_rows("nosession|rec-cancel|BTCUSDT|1d|n", 2)
    assert len(rows) == 2
    assert all(r["cancelled"] == 1 for r in rows)
    assert [r["role"] for r in rows] == [ROLE_USER, ROLE_ASSISTANT]

