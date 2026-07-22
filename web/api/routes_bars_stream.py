"""K 线 SSE 流式推送 API。

提供 GET /api/bars/stream 端点：
- ``bar_close`` 事件：bar 收盘时推送完整 bars 数组
- ``bar_update`` 事件：每 5 秒推送 forming bar 增量

后台 Task 在 app startup 时启动、shutdown 时取消；多个 SSE 客户端共享同一
广播队列，避免每个连接各自轮询数据源。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from pa_agent.data.bar_close_wait import seconds_until_bar_closes, timeframe_to_seconds

logger = logging.getLogger(__name__)
router = APIRouter(tags=["bars-stream"])

# forming bar 增量推送间隔（秒）
_UPDATE_INTERVAL_S = 5
# SSE 客户端心跳间隔（秒）。低于此值没有事件就发 ping。
_SSE_HEARTBEAT_TIMEOUT_S = 15
# sse_starlette ping 间隔（秒）。服务端每 N 秒发一条 ping comment，
# 用于检测客户端断开。设为 None 关闭。
_SSE_PING_INTERVAL_S = 15
# 数据源未就绪时的重试间隔（秒）
_NO_SOURCE_RETRY_S = 5
# seconds_until_bar_closes 返回 None / <=0 时的 fallback
_FALLBACK_WAIT_S = 60

# 全局订阅列表 + 后台 Task 引用（模块级单例）
_subscribers: list[asyncio.Queue] = []
_subscribers_lock = asyncio.Lock()
_background_task: asyncio.Task | None = None


# ── 订阅者管理 ─────────────────────────────────────────────────────────────────


async def _add_subscriber() -> asyncio.Queue:
    """添加一个订阅者，返回其专属 queue。"""
    q: asyncio.Queue = asyncio.Queue()
    async with _subscribers_lock:
        _subscribers.append(q)
    return q


async def _remove_subscriber(q: asyncio.Queue) -> None:
    """移除订阅者（若存在）。"""
    async with _subscribers_lock:
        if q in _subscribers:
            _subscribers.remove(q)


async def _broadcast(event: dict) -> None:
    """向所有订阅者广播事件。

    使用 ``put_nowait`` 避免一个慢客户端阻塞其他订阅者；队列满时丢弃事件
    并打印警告（订阅者队列默认无限大，正常不会触发）。
    """
    async with _subscribers_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Subscriber queue full, dropping event")


# ── bar 序列化 ────────────────────────────────────────────────────────────────


def _format_bar(bar: Any) -> dict:
    """把 bar 对象格式化为 JSON-safe dict。"""
    if isinstance(bar, dict):
        return bar
    if hasattr(bar, "model_dump"):
        return bar.model_dump(mode="json")
    if hasattr(bar, "__dict__"):
        return {k: v for k, v in bar.__dict__.items() if not k.startswith("_")}
    return {"value": str(bar)}


def _compute_next_close_ts(ts_open_ms: Any, timeframe: str, now_ms: int | None = None) -> int | None:
    """计算 forming bar 的下一根收盘时间戳（毫秒）。

    使用与 seconds_until_bar_closes 一致的算法：通过 elapsed % duration
    计算剩余时间，再加上当前时间，避免时区偏移导致的计算错误。
    timeframe 无法解析或 ts_open 无效时返回 None。

    Args:
        ts_open_ms: forming bar 的开盘时间戳（毫秒）
        timeframe: K线周期，如 "5m", "1h"
        now_ms: 当前时间戳（毫秒），用于测试注入。默认为 None，使用 time.time()
    """
    import time as _time
    try:
        ts_open = int(ts_open_ms)
    except (TypeError, ValueError):
        return None
    if ts_open <= 0:
        return None
    duration_s = timeframe_to_seconds(timeframe)
    if duration_s is None or duration_s <= 0:
        return None
    now = int(now_ms) if now_ms is not None else int(_time.time() * 1000)
    duration_ms = duration_s * 1000
    elapsed_ms = now - ts_open
    if elapsed_ms <= 0:
        return ts_open + duration_ms
    remainder_ms = elapsed_ms % duration_ms
    if remainder_ms == 0:
        return now + duration_ms
    return now + (duration_ms - remainder_ms)


# ── 后台 bar close 检测 Task ───────────────────────────────────────────────────


def _resolve_symbol_timeframe(ctx: Any, source: Any) -> tuple[str, str]:
    """从 ctx.settings / source 取当前 symbol/timeframe。"""
    symbol = ""
    timeframe = ""
    settings = getattr(ctx, "settings", None)
    if settings is not None:
        try:
            symbol = settings.general.last_symbol or ""
            timeframe = settings.general.last_timeframe or ""
        except AttributeError:
            pass
    if not symbol:
        symbol = getattr(source, "_symbol", "") or "BTCUSDT"
    if not timeframe:
        timeframe = getattr(source, "_timeframe", "") or "1d"
    return symbol, timeframe


async def _push_bar_update(source: Any, symbol: str, timeframe: str) -> None:
    """拉取最新 snapshot 并广播 forming bar 增量。"""
    try:
        bars = await asyncio.to_thread(source.latest_snapshot, 100)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bar_update latest_snapshot failed: %s", exc)
        return
    if not bars:
        return
    last_bar = _format_bar(bars[0])
    next_close_ts = _compute_next_close_ts(last_bar.get("ts_open"), timeframe)
    await _broadcast({
        "event": "bar_update",
        "data": json.dumps(
            {
                "last_bar": last_bar,
                "symbol": symbol,
                "timeframe": timeframe,
                "next_close_ts": next_close_ts,
            },
            ensure_ascii=False,
        ),
    })


async def _push_bar_close(source: Any, symbol: str, timeframe: str) -> None:
    """拉取最新 snapshot 并广播完整 bars 数组（bar_close 事件）。

    在拉取前先清除数据源缓存，确保获取到最新的 bar 数据（包含刚收盘的新 bar
    和新的 forming bar），避免 TTL 缓存返回过期数据导致序号和指标延迟更新。
    """
    try:
        # 强制清除缓存，确保拉取最新数据
        if hasattr(source, 'clear_snapshot_cache'):
            await asyncio.to_thread(source.clear_snapshot_cache)
        bars = await asyncio.to_thread(source.latest_snapshot, 100)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bar_close latest_snapshot failed: %s", exc)
        return
    formatted_bars = [_format_bar(b) for b in bars]
    new_bar_ts = formatted_bars[0].get("ts_open") if formatted_bars else None
    # bar_close 推送后，最新的 forming bar 是 bars[0]，next_close_ts 用它的 ts_open 计算
    next_close_ts = _compute_next_close_ts(new_bar_ts, timeframe)
    await _broadcast({
        "event": "bar_close",
        "data": json.dumps(
            {
                "bars": formatted_bars,
                "new_bar_ts": new_bar_ts,
                "symbol": symbol,
                "timeframe": timeframe,
                "next_close_ts": next_close_ts,
            },
            ensure_ascii=False,
        ),
    })


async def _background_bars_loop(app: Any) -> None:
    """后台 Task：检测 bar close 并推送事件。

    每次循环：
    1. 取当前 forming bar 的 ``ts_open`` 与 timeframe
    2. 计算 ``seconds_until_bar_closes``
    3. 每 ``_UPDATE_INTERVAL_S`` 秒推送 ``bar_update`` 直到收盘
    4. 收盘后推送 ``bar_close``，进入下一轮
    """
    logger.info("Background bars stream loop started")
    try:
        while True:
            ctx = getattr(app.state, "ctx", None)
            source = getattr(ctx, "data_source", None) if ctx is not None else None
            if ctx is None or source is None:
                await asyncio.sleep(_NO_SOURCE_RETRY_S)
                await _broadcast({"event": "ping", "data": ""})
                continue

            symbol, timeframe = _resolve_symbol_timeframe(ctx, source)

            try:
                bars = await asyncio.to_thread(source.latest_snapshot, 100)
            except Exception as exc:  # noqa: BLE001
                logger.warning("bars_stream snapshot fetch failed: %s", exc)
                await asyncio.sleep(_NO_SOURCE_RETRY_S)
                await _broadcast({"event": "ping", "data": ""})
                continue

            if not bars:
                await asyncio.sleep(_NO_SOURCE_RETRY_S)
                await _broadcast({"event": "ping", "data": ""})
                continue

            forming_bar = bars[0]
            ts_open_ms = int(getattr(forming_bar, "ts_open", 0))
            if ts_open_ms <= 0:
                await asyncio.sleep(_NO_SOURCE_RETRY_S)
                await _broadcast({"event": "ping", "data": ""})
                continue

            # Check if market is closed: head bar is already closed (no forming bar)
            is_market_closed = bool(getattr(forming_bar, "closed", False))

            if is_market_closed:
                # Market halted/closed: only send ping, do NOT push bar_close
                # (pushing bar_close every 60s would trigger repeated analysis in 持续分析 mode)
                await _broadcast({"event": "ping", "data": ""})
                await asyncio.sleep(_FALLBACK_WAIT_S)
                continue

            # Normal flow: forming bar exists, compute wait time
            wait_seconds = seconds_until_bar_closes(ts_open_ms, timeframe)
            if wait_seconds is None or wait_seconds <= 0:
                wait_seconds = _FALLBACK_WAIT_S

            # 在 wait_seconds 内每 _UPDATE_INTERVAL_S 秒推送 forming bar 增量
            # 同时监测 timeframe / symbol 变化（用户切换时立即 break 重新初始化，
            # 避免内层循环用旧 timeframe 导致 next_close_ts 错误、bar_close 永不触发）
            remaining = wait_seconds
            reinit_needed = False
            while remaining > 0:
                sleep_for = min(_UPDATE_INTERVAL_S, remaining)
                await asyncio.sleep(sleep_for)
                remaining -= sleep_for

                # 每次推送前重新读取 timeframe/symbol，感知用户切换
                new_symbol, new_timeframe = _resolve_symbol_timeframe(ctx, source)
                if new_symbol != symbol or new_timeframe != timeframe:
                    logger.info(
                        "bars_stream: symbol/timeframe changed during inner loop "
                        "(symbol=%s->%s, timeframe=%s->%s), reinit",
                        symbol, new_symbol, timeframe, new_timeframe,
                    )
                    reinit_needed = True
                    break  # 回到外层重新初始化

                if remaining > 0:
                    await _push_bar_update(source, symbol, timeframe)

            # 推送 bar_close 事件（仅在内层循环正常结束时；
            # 用户切换 timeframe 时跳过，避免用旧 timeframe 推送错误的 bar_close）
            if not reinit_needed:
                await _push_bar_close(source, symbol, timeframe)
    except asyncio.CancelledError:
        logger.info("Background bars stream loop cancelled")
        raise
    except Exception:  # noqa: BLE001
        # 兜底：不应到达此处，但防止未知异常让 Task 静默退出
        logger.exception("Background bars stream loop crashed unexpectedly")


# ── SSE 端点 ──────────────────────────────────────────────────────────────────


@router.get("/bars/stream")
async def bars_stream(request: Request):
    """SSE 端点：推送 K 线 ``bar_close`` / ``bar_update`` 事件。"""
    queue = await _add_subscriber()

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEARTBEAT_TIMEOUT_S
                    )
                    yield event
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield {"event": "ping", "data": ""}
        finally:
            await _remove_subscriber(queue)

    return EventSourceResponse(event_generator(), ping=_SSE_PING_INTERVAL_S)


# ── lifespan 钩子（供 web/server.py 调用） ─────────────────────────────────────


async def start_background_task(app: Any) -> None:
    """启动后台 bar close 检测 Task（幂等）。"""
    global _background_task
    if _background_task is None or _background_task.done():
        _background_task = asyncio.create_task(_background_bars_loop(app))
        logger.info("Background bars stream task created")
    else:
        logger.info("Background bars stream task already running")


async def stop_background_task() -> None:
    """取消后台 Task 并等待清理（幂等）。"""
    global _background_task
    if _background_task is None:
        return
    _background_task.cancel()
    try:
        await _background_task
    except asyncio.CancelledError:
        pass
    _background_task = None
