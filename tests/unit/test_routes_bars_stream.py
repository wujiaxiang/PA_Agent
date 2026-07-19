# -*- coding: utf-8 -*-
"""Unit tests for web/api/routes_bars_stream.py — K 线 SSE 流式推送。

覆盖：
- GET /api/bars/stream 返回 text/event-stream 内容类型
- 订阅者添加 / 移除
- 广播事件到所有订阅者
- _format_bar 处理 dict / pydantic-like 对象
- 后台 Task 启动 / 停止（生命周期管理）

mock 策略：
- SSE 端点测试用最小 FastAPI app（仅挂 routes_bars_stream router，不启动后台 Task）
- 内部函数测试用 ``asyncio.run`` 直接调用异步 API
- 后台 Task 测试用 MagicMock 的 app（``app.state.ctx = None`` 让 loop 进入 sleep 分支）
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api import routes_bars_stream
from web.api.routes_bars_stream import router as bars_stream_router


# ── 公共 fixture ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每个测试前后清空订阅者列表与后台 Task，避免测试间泄漏。"""
    routes_bars_stream._subscribers.clear()
    yield
    # 测试结束后清理后台 Task（若有）
    task = routes_bars_stream._background_task
    if task is not None and not task.done():
        task.cancel()
        try:
            asyncio.run(routes_bars_stream.stop_background_task())
        except Exception:
            pass
    routes_bars_stream._subscribers.clear()


def _make_app() -> FastAPI:
    """构造最小 FastAPI app，仅挂 bars_stream router，不启动后台 Task。"""
    app = FastAPI()
    app.include_router(bars_stream_router, prefix="/api")
    return app


# ── SSE 端点 ──────────────────────────────────────────────────────────────────


def test_bars_stream_route_registered():
    """路由 /bars/stream 已注册到 bars_stream_router。"""
    paths = {getattr(r, "path", "") for r in bars_stream_router.routes}
    assert "/bars/stream" in paths


def test_bars_stream_route_mounted_with_api_prefix():
    """router 被 include 到 app 时带 /api 前缀。"""
    app = _make_app()
    # OpenAPI schema 包含所有已注册路由的完整路径
    schema = app.openapi()
    assert "/api/bars/stream" in schema["paths"]


def test_bars_stream_returns_event_source_response():
    """bars_stream 处理函数返回 EventSourceResponse（media_type=text/event-stream）。"""
    from sse_starlette.sse import EventSourceResponse

    # 用最小 mock request —— 我们不会真正迭代 generator，
    # 所以 is_disconnected 不会被调用。
    request = MagicMock()

    async def _call():
        return await routes_bars_stream.bars_stream(request)

    try:
        response = asyncio.run(_call())
        assert isinstance(response, EventSourceResponse)
        assert "text/event-stream" in (response.media_type or "")
    finally:
        # bars_stream 调用了 _add_subscriber 但 generator 未迭代，
        # 手动清理订阅者避免泄漏到后续测试。
        routes_bars_stream._subscribers.clear()


def test_bars_stream_generator_cleans_up_on_disconnect(monkeypatch):
    """generator 在 request.is_disconnected()=True 时退出并清理订阅者。

    直接迭代 EventSourceResponse.body_iterator（即 event_generator），
    避免通过 HTTP 层（sse_starlette 的 task group 在 ASGITransport 下
    会阻塞等待 generator 完成，导致 TestClient / httpx 挂死）。
    """
    from unittest.mock import AsyncMock

    # 缩短心跳超时，让 generator 快速循环
    monkeypatch.setattr(routes_bars_stream, "_SSE_HEARTBEAT_TIMEOUT_S", 0.1)

    request = MagicMock()
    # 第一次检查返回 False（让 generator 进入 wait_for），
    # 后续都返回 True（让 generator 退出）。
    request.is_disconnected = AsyncMock(side_effect=[False, True, True])

    async def _test():
        response = await routes_bars_stream.bars_stream(request)
        # bars_stream 添加了一个订阅者
        assert len(routes_bars_stream._subscribers) == 1
        # body_iterator 就是 event_generator
        gen = response.body_iterator
        events = []
        async for event in gen:
            events.append(event)
            if len(events) > 5:
                break  # 安全保护，不应到达
        return events

    events = asyncio.run(asyncio.wait_for(_test(), timeout=5))
    # generator 退出后，订阅者应被清理（finally 块调用 _remove_subscriber）
    assert routes_bars_stream._subscribers == [], (
        f"Subscribers not cleaned up: {routes_bars_stream._subscribers}"
    )
    # 第一次 is_disconnected=False → generator 进入 wait_for → timeout → yield ping
    # 第二次 is_disconnected=True → break
    assert len(events) >= 1
    # 第一个事件应该是 ping（heartbeat）
    assert events[0]["event"] == "ping"


def test_bars_stream_generator_exits_immediately_if_disconnected(monkeypatch):
    """若连接一开始就断开，generator 应立即退出不产生任何事件。"""
    from unittest.mock import AsyncMock

    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)

    async def _test():
        response = await routes_bars_stream.bars_stream(request)
        gen = response.body_iterator
        events = []
        async for event in gen:
            events.append(event)
        return events

    events = asyncio.run(asyncio.wait_for(_test(), timeout=5))
    assert events == []
    assert routes_bars_stream._subscribers == []


# ── 订阅者管理 ────────────────────────────────────────────────────────────────


def test_add_and_remove_subscriber():
    """_add_subscriber / _remove_subscriber 正确管理订阅列表。"""

    async def _test():
        q = await routes_bars_stream._add_subscriber()
        assert q in routes_bars_stream._subscribers
        assert len(routes_bars_stream._subscribers) == 1

        await routes_bars_stream._remove_subscriber(q)
        assert q not in routes_bars_stream._subscribers
        assert len(routes_bars_stream._subscribers) == 0

        # 再次移除不应报错
        await routes_bars_stream._remove_subscriber(q)

    asyncio.run(_test())


def test_broadcast_to_subscribers():
    """广播事件应推送到所有订阅者队列。"""

    async def _test():
        q1 = await routes_bars_stream._add_subscriber()
        q2 = await routes_bars_stream._add_subscriber()
        event = {"event": "test", "data": "hello"}

        await routes_bars_stream._broadcast(event)

        e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert e1 == event
        assert e2 == event

        await routes_bars_stream._remove_subscriber(q1)
        await routes_bars_stream._remove_subscriber(q2)

    asyncio.run(_test())


def test_broadcast_with_no_subscribers_is_noop():
    """无订阅者时广播不应抛异常。"""

    async def _test():
        assert routes_bars_stream._subscribers == []
        await routes_bars_stream._broadcast({"event": "x", "data": "y"})

    asyncio.run(_test())


# ── _format_bar ───────────────────────────────────────────────────────────────


def test_format_bar_dict():
    """_format_bar 处理 dict 输入应原样返回。"""
    bar = {
        "open": 100.0,
        "high": 110.0,
        "low": 95.0,
        "close": 105.0,
        "ts_open": 1234567890000,
        "closed": False,
    }
    result = routes_bars_stream._format_bar(bar)
    assert result == bar


def test_format_bar_pydantic_like():
    """_format_bar 处理含 model_dump 方法的对象应调用它。"""

    class FakeBar:
        def model_dump(self, mode="python"):
            return {"open": 1.0, "close": 2.0, "mode": mode}

    result = routes_bars_stream._format_bar(FakeBar())
    assert result == {"open": 1.0, "close": 2.0, "mode": "json"}


def test_format_bar_plain_object():
    """_format_bar 处理普通对象应取 __dict__ 并过滤私有属性。"""

    class FakeBar:
        def __init__(self):
            self.open = 100.0
            self.close = 105.0
            self._private = "secret"

    result = routes_bars_stream._format_bar(FakeBar())
    assert result == {"open": 100.0, "close": 105.0}
    assert "_private" not in result


def test_format_bar_scalar():
    """_format_bar 处理标量应包装为 dict。"""
    result = routes_bars_stream._format_bar(42)
    assert result == {"value": "42"}


# ── 后台 Task 生命周期 ─────────────────────────────────────────────────────────


def test_start_and_stop_background_task():
    """start_background_task / stop_background_task 正确管理 Task 生命周期。"""

    async def _test():
        app = MagicMock()
        # ctx = None → 后台 loop 进入 sleep 分支，不会真正拉数据
        app.state.ctx = None

        # 初始状态
        assert routes_bars_stream._background_task is None

        # 启动
        await routes_bars_stream.start_background_task(app)
        assert routes_bars_stream._background_task is not None
        assert not routes_bars_stream._background_task.done()

        # 停止
        await routes_bars_stream.stop_background_task()
        assert routes_bars_stream._background_task is None

    asyncio.run(_test())


def test_stop_background_task_idempotent():
    """stop_background_task 在没有运行 Task 时不应抛异常。"""

    async def _test():
        await routes_bars_stream.stop_background_task()
        await routes_bars_stream.stop_background_task()

    asyncio.run(_test())


def test_start_background_task_idempotent():
    """重复调用 start_background_task 不应创建多个 Task。"""

    async def _test():
        app = MagicMock()
        app.state.ctx = None

        await routes_bars_stream.start_background_task(app)
        first_task = routes_bars_stream._background_task
        assert first_task is not None

        # 再次调用不应创建新 Task（前一个仍在运行）
        await routes_bars_stream.start_background_task(app)
        assert routes_bars_stream._background_task is first_task

        await routes_bars_stream.stop_background_task()

    asyncio.run(_test())


# ── _resolve_symbol_timeframe ─────────────────────────────────────────────────


def test_resolve_symbol_timeframe_from_settings():
    """优先从 ctx.settings.general 读取 symbol/timeframe。"""
    ctx = MagicMock()
    ctx.settings.general.last_symbol = "BTCUSDT"
    ctx.settings.general.last_timeframe = "1h"
    source = MagicMock()
    source._symbol = "ETHUSDT"
    source._timeframe = "1d"

    symbol, timeframe = routes_bars_stream._resolve_symbol_timeframe(ctx, source)
    assert symbol == "BTCUSDT"
    assert timeframe == "1h"


def test_resolve_symbol_timeframe_fallback_to_source():
    """settings 缺失时从 source 私有属性读取。"""
    ctx = MagicMock()
    ctx.settings = None
    source = MagicMock()
    source._symbol = "ETHUSDT"
    source._timeframe = "4h"

    symbol, timeframe = routes_bars_stream._resolve_symbol_timeframe(ctx, source)
    assert symbol == "ETHUSDT"
    assert timeframe == "4h"


def test_resolve_symbol_timeframe_defaults():
    """settings 和 source 都缺失时使用默认值。"""
    ctx = MagicMock()
    ctx.settings = None
    source = MagicMock()
    del source._symbol
    del source._timeframe

    symbol, timeframe = routes_bars_stream._resolve_symbol_timeframe(ctx, source)
    assert symbol == "BTCUSDT"
    assert timeframe == "1d"


# ── _compute_next_close_ts ───────────────────────────────────────────────────


def test_compute_next_close_ts_basic():
    """1m timeframe：next_close_ts = ts_open + 60_000 ms。"""
    # ts_open = 1_700_000_000_000 (2023-11-14 22:13:20 UTC)
    ts_open = 1_700_000_000_000
    result = routes_bars_stream._compute_next_close_ts(ts_open, "1m")
    assert result == ts_open + 60_000


def test_compute_next_close_ts_various_timeframes():
    """不同 timeframe 都能正确换算为毫秒。"""
    ts_open = 1_700_000_000_000
    assert routes_bars_stream._compute_next_close_ts(ts_open, "5m") == ts_open + 5 * 60_000
    assert routes_bars_stream._compute_next_close_ts(ts_open, "15m") == ts_open + 15 * 60_000
    assert routes_bars_stream._compute_next_close_ts(ts_open, "1h") == ts_open + 60 * 60_000
    assert routes_bars_stream._compute_next_close_ts(ts_open, "4h") == ts_open + 4 * 60 * 60_000
    assert routes_bars_stream._compute_next_close_ts(ts_open, "1d") == ts_open + 24 * 60 * 60_000


def test_compute_next_close_ts_invalid_ts_open():
    """ts_open 为 0 / 负数 / None / 非数字 → 返回 None。"""
    assert routes_bars_stream._compute_next_close_ts(0, "1m") is None
    assert routes_bars_stream._compute_next_close_ts(-1, "1m") is None
    assert routes_bars_stream._compute_next_close_ts(None, "1m") is None
    assert routes_bars_stream._compute_next_close_ts("abc", "1m") is None


def test_compute_next_close_ts_invalid_timeframe():
    """timeframe 无法解析 → 返回 None。"""
    assert routes_bars_stream._compute_next_close_ts(1_700_000_000_000, "") is None
    assert routes_bars_stream._compute_next_close_ts(1_700_000_000_000, "xyz") is None
    assert routes_bars_stream._compute_next_close_ts(1_700_000_000_000, None) is None


# ── _push_bar_update / _push_bar_close 携带 next_close_ts ────────────────────


def test_push_bar_update_includes_next_close_ts():
    """_push_bar_update 广播的事件 data 中应包含 next_close_ts 字段。"""

    class FakeBar:
        """模拟 KlineBar：dict-like + ts_open 字段。"""

        def __init__(self, ts_open: int):
            self.open = 100.0
            self.high = 110.0
            self.low = 95.0
            self.close = 105.0
            self.ts_open = ts_open
            self.closed = False

    ts_open = 1_700_000_000_000
    source = MagicMock()
    source.latest_snapshot = MagicMock(return_value=[FakeBar(ts_open)])

    async def _test():
        # 加一个订阅者接收事件
        q = await routes_bars_stream._add_subscriber()
        await routes_bars_stream._push_bar_update(source, "BTCUSDT", "1m")
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        await routes_bars_stream._remove_subscriber(q)
        return event

    event = asyncio.run(_test())
    assert event["event"] == "bar_update"
    import json as _json
    data = _json.loads(event["data"])
    # 验证 next_close_ts 字段存在且等于 ts_open + 60_000
    assert "next_close_ts" in data
    assert data["next_close_ts"] == ts_open + 60_000
    # 顺带验证其他字段仍存在
    assert data["symbol"] == "BTCUSDT"
    assert data["timeframe"] == "1m"
    assert data["last_bar"]["ts_open"] == ts_open


def test_push_bar_close_includes_next_close_ts():
    """_push_bar_close 广播的事件 data 中应包含 next_close_ts 字段（基于新 forming bar）。"""

    class FakeBar:
        def __init__(self, ts_open: int):
            self.open = 100.0
            self.high = 110.0
            self.low = 95.0
            self.close = 105.0
            self.ts_open = ts_open
            self.closed = True

    # bars[0] 是新 forming bar（即下一根 K 线的 ts_open）
    new_ts_open = 1_700_000_060_000
    source = MagicMock()
    source.latest_snapshot = MagicMock(
        return_value=[FakeBar(new_ts_open), FakeBar(new_ts_open - 60_000)]
    )

    async def _test():
        q = await routes_bars_stream._add_subscriber()
        await routes_bars_stream._push_bar_close(source, "BTCUSDT", "1m")
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        await routes_bars_stream._remove_subscriber(q)
        return event

    event = asyncio.run(_test())
    assert event["event"] == "bar_close"
    import json as _json
    data = _json.loads(event["data"])
    # 验证 next_close_ts = new_ts_open + 60_000
    assert "next_close_ts" in data
    assert data["next_close_ts"] == new_ts_open + 60_000
    assert data["new_bar_ts"] == new_ts_open
    assert data["symbol"] == "BTCUSDT"
    assert data["timeframe"] == "1m"
    assert len(data["bars"]) == 2


def test_push_bar_update_next_close_ts_none_for_invalid_timeframe():
    """timeframe 无法解析时，next_close_ts 应为 None（不抛异常）。"""

    class FakeBar:
        def __init__(self):
            self.open = 100.0
            self.high = 110.0
            self.low = 95.0
            self.close = 105.0
            self.ts_open = 1_700_000_000_000
            self.closed = False

    source = MagicMock()
    source.latest_snapshot = MagicMock(return_value=[FakeBar()])

    async def _test():
        q = await routes_bars_stream._add_subscriber()
        await routes_bars_stream._push_bar_update(source, "BTCUSDT", "xyz")
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        await routes_bars_stream._remove_subscriber(q)
        return event

    event = asyncio.run(_test())
    import json as _json
    data = _json.loads(event["data"])
    assert "next_close_ts" in data
    assert data["next_close_ts"] is None
