"""K 线「下一根收盘时间戳」计算（``/api/bars/next-close`` 与前端倒计时共用）。

历史沿革（2026-10 改造）
------------------------
本模块原本是 ``GET /api/bars/stream`` 的 SSE 服务端广播实现：后台 Task 从**全局**
``app.state.ctx`` 订阅里拉一次 K 线，再通过 ``_broadcast()`` 推给所有 SSE 连接。
两个浏览器标签页因此看到的是同一条数据流，与项目目标「每个标签页服务自己的
K 线图」直接冲突。

考虑过的替代方案「按游标分组广播」也被否决：浏览器原生 ``EventSource`` **无法
设置请求头**，服务端拿不到 ``X-Session-Id``，分组无从取值；且一个坏品种的
auto-probe 会持有 ``TradingViewSource._snapshot_lock`` 长达数十秒，全站
``/api/bars`` 排队（head-of-line blocking）。

最终改为**前端按自己游标轮询**（``GET /api/bars`` + ``GET /api/bars/next-close``）：
- 隐藏标签页会自动停（``document.hidden`` / ``visibilitychange``）
- 一个坏 tab 不会传染其它 tab
- 持续分析的「收盘触发」改由前端本地定时器完成

因此本模块删除了 ``_subscribers`` / ``_subscribers_lock`` / ``_broadcast`` /
``_add_subscriber`` / ``_remove_subscriber`` / ``_background_bars_loop`` /
``start_background_task`` / ``stop_background_task`` 以及 ``/api/bars/stream``
端点本身。``web/server.py`` 的 lifespan 里对 ``start_background_task`` /
``stop_background_task`` 的调用需要一并移除。

**跨模块硬契约**：``web/api/routes_data.py`` 有
``from .routes_bars_stream import _compute_next_close_ts``，
删除该函数会直接 AttributeError，务必保留。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from pa_agent.data.bar_close_wait import timeframe_to_seconds

logger = logging.getLogger(__name__)

#: 历史遗留的空 router。``web/server.py`` 仍会 ``include_router(bars_stream_router,
#: prefix="/api")``，保留它可让 server.py 的 router 导入继续工作而不必同步改动。
#: 本模块**不再注册任何路由** —— ``/api/bars/stream`` 已下线（访问返回 404）。
router = APIRouter(tags=["bars-stream"])


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
