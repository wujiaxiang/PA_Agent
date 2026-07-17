# -*- coding: utf-8 -*-
"""Unit tests for web/api/routes_chat.py — chat SSE + TTL session cache.

覆盖：
- _touch_session / _get_session 增删改 + last_touch 刷新
- _chat_cleanup_loop 移除过期 session
- _kline_snapshot_fn 格式化 K 线表 / 异常兜底
- GET /api/chat/stream 无记录时返回 error 事件
- GET /api/chat/stream 有记录 + mock FreeChatSession 时输出 done 事件
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.api.routes_chat as chat_mod
from web.api.routes_chat import (
    _CHAT_SESSION_TTL_SEC,
    _chat_cleanup_loop,
    _chat_sessions,
    _get_session,
    _kline_snapshot_fn,
    _touch_session,
    router as chat_router,
)
from pa_agent.data.base import KlineBar


@pytest.fixture(autouse=True)
def _reset_chat_globals():
    """每个测试前清空 session 缓存，避免相互污染。"""
    _chat_sessions.clear()
    chat_mod._chat_cleanup_task = None
    yield
    _chat_sessions.clear()


# ── TTL helper 单元测试 ────────────────────────────────────────────────────────


def test_touch_then_get_returns_same_session():
    """_touch_session 写入后 _get_session 返回同一对象。"""
    sess = MagicMock(name="sess1")
    _touch_session("key1", sess)
    assert "key1" in _chat_sessions
    got = _get_session("key1")
    assert got is sess


def test_get_session_updates_last_touch():
    """_get_session 刷新 last_touch 时间戳。"""
    _touch_session("key1", MagicMock())
    first = _chat_sessions["key1"]["last_touch"]
    time.sleep(0.01)
    _get_session("key1")
    assert _chat_sessions["key1"]["last_touch"] > first


def test_get_session_returns_none_for_missing_key():
    assert _get_session("nonexistent") is None


def test_touch_overwrites_existing_key():
    """相同 key 二次 touch 覆盖旧 session。"""
    s1 = MagicMock(name="s1")
    s2 = MagicMock(name="s2")
    _touch_session("k", s1)
    _touch_session("k", s2)
    assert _get_session("k") is s2


# ── cleanup loop ──────────────────────────────────────────────────────────────


def test_cleanup_loop_removes_expired_sessions():
    """超过 TTL 的 session 被清理循环移除，未过期保留。"""
    expired_ts = time.time() - (_CHAT_SESSION_TTL_SEC + 1)
    _chat_sessions["expired"] = {"session": MagicMock(), "last_touch": expired_ts}
    _chat_sessions["fresh"] = {"session": MagicMock(), "last_touch": time.time()}

    call_count = 0

    async def fake_sleep(_t):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError()
        # 第一次 sleep 立即返回，让清理逻辑执行一次

    async def run():
        with patch.object(chat_mod.asyncio, "sleep", fake_sleep):
            try:
                await _chat_cleanup_loop()
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    assert "expired" not in _chat_sessions, "过期 session 应被清理"
    assert "fresh" in _chat_sessions, "未过期 session 应保留"


# ── _kline_snapshot_fn ────────────────────────────────────────────────────────


def test_kline_snapshot_fn_formats_bars():
    """snapshot 函数输出含表头和价格的文本表。"""
    bars = [
        KlineBar(seq=1, ts_open=1, open=2000.0, high=2010.0, low=1995.0,
                 close=2005.0, volume=100.0, closed=True),
        KlineBar(seq=2, ts_open=2, open=2005.0, high=2015.0, low=2000.0,
                 close=2012.0, volume=110.0, closed=True),
    ]
    ctx = MagicMock()
    ctx.data_source.latest_snapshot.return_value = bars
    text = _kline_snapshot_fn(ctx)()
    assert "seq" in text
    assert "2000.00" in text
    assert "2005.00" in text


def test_kline_snapshot_fn_handles_exception():
    """数据源异常时返回空字符串。"""
    ctx = MagicMock()
    ctx.data_source.latest_snapshot.side_effect = RuntimeError("boom")
    assert _kline_snapshot_fn(ctx)() == ""


# ── GET /api/chat/stream ──────────────────────────────────────────────────────


def _parse_sse(text: str) -> list[tuple[str, object]]:
    """简易 SSE 解析：返回 [(event, data_obj), ...]。"""
    events: list[tuple[str, object]] = []
    cur_event: str | None = None
    cur_data: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("event: "):
            cur_event = line[len("event: "):].strip()
        elif line.startswith("data: "):
            cur_data.append(line[len("data: "):])
        elif line == "" and cur_event is not None:
            data_str = "\n".join(cur_data)
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                data = data_str
            events.append((cur_event, data))
            cur_event = None
            cur_data = []
    return events


def test_chat_stream_no_record_returns_error():
    """ctx._last_record 为空且 history 无记录时，SSE 返回 error 事件。"""
    app = FastAPI()
    app.include_router(chat_router, prefix="/api")
    ctx = MagicMock()
    ctx._last_record = None

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    with patch("pa_agent.records.analysis_history.find_latest_successful_record",
               return_value=None):
        with TestClient(app) as c:
            resp = c.get("/api/chat/stream?text=你好")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types = [e for e, _ in events]
    assert "error" in types
    err_data = next(d for e, d in events if e == "error")
    assert "没有已完成的交易分析记录" in err_data["message"]


def test_chat_stream_success_emits_done():
    """有记录 + mock FreeChatSession.send 成功时，SSE 输出 done 事件。"""
    app = FastAPI()
    app.include_router(chat_router, prefix="/api")

    record = MagicMock()
    record._basename = "test_record"
    ctx = MagicMock()
    ctx._last_record = record
    ctx.data_source.latest_snapshot.return_value = []

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    fake_reply = MagicMock()
    fake_reply.content = "AI 回答"
    fake_reply.reasoning_content = ""
    fake_session = MagicMock()
    fake_session.send.return_value = fake_reply

    with patch("web.api.routes_chat.FreeChatSession", return_value=fake_session):
        with TestClient(app) as c:
            resp = c.get("/api/chat/stream?text=追问内容")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types = [e for e, _ in events]
    assert "done" in types
    done_data = next(d for e, d in events if e == "done")
    assert done_data["content"] == "AI 回答"
    fake_session.send.assert_called_once()


def test_chat_stream_reuses_cached_session():
    """同一 record_id 的二次请求复用缓存的 FreeChatSession。"""
    app = FastAPI()
    app.include_router(chat_router, prefix="/api")

    record = MagicMock()
    record._basename = "reuse_record"
    ctx = MagicMock()
    ctx._last_record = record
    ctx.data_source.latest_snapshot.return_value = []

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = ctx

    fake_reply = MagicMock()
    fake_reply.content = "ok"
    fake_reply.reasoning_content = ""
    fake_session = MagicMock()
    fake_session.send.return_value = fake_reply

    with patch("web.api.routes_chat.FreeChatSession", return_value=fake_session) as mock_ctor:
        with TestClient(app) as c:
            c.get("/api/chat/stream?text=q1&record_id=r1")
            c.get("/api/chat/stream?text=q2&record_id=r1")

    # 第二次应复用缓存，构造器只调用一次
    assert mock_ctor.call_count == 1
    assert fake_session.send.call_count == 2
