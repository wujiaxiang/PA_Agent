# -*- coding: utf-8 -*-
"""Unit tests for web/api/routes_bars_stream.py — K 线下一根收盘时间戳计算。

模块历史
--------
本文件原先覆盖的是「K 线 SSE 实时流」：订阅者增删、事件广播、后台 Task 生命周期、
``/api/bars/stream`` 端点。这些用例共 11 处直接操作模块级 ``_subscribers``，
随着服务端广播机制下线（多标签页串味 → 改为前端按自己游标轮询）已全部删除，
对应的 ``_subscribers`` / ``_subscribers_lock`` / ``_broadcast`` /
``_add_subscriber`` / ``_remove_subscriber`` / ``_background_bars_loop`` /
``start_background_task`` / ``stop_background_task`` 与 ``/api/bars/stream``
端点均已从模块中移除。

保留下来的是本模块**唯一仍有价值**的部分：``_compute_next_close_ts()``。
它是跨模块硬契约（``web/api/routes_data.py`` 里
``from .routes_bars_stream import _compute_next_close_ts``），删掉会 AttributeError；
同时「休市 / 取模算法」的正确性由 ``tests/unit/test_countdown_consistency.py``
与后端 ``pa_agent/data/bar_close_wait.py`` 共同守护，前端「距下次收盘」倒计时
与「等待收盘」全部依赖它的结果。

覆盖：
- 跨模块硬契约：``_compute_next_close_ts`` 必须仍可从本模块导入
- 残留符号必须已删除（防止有人把全局广播复活）
- ``/api/bars/stream`` 端点已下线（router 不再注册任何路由）
- 取模算法（elapsed % duration）与周期边界等价性
- 休市 / 过期 ts_open / 非法 timeframe 等边界返回 None
"""
from __future__ import annotations

import pathlib
import time

import pytest

from web.api import routes_bars_stream
from web.api.routes_bars_stream import router as bars_stream_router


# ── 跨模块硬契约 ──────────────────────────────────────────────────────────────

#: web/api/routes_data.py::bars_next_close 里的实际 import 语句。
#: 任何对模块的重构都必须保住它，否则 /api/bars/next-close 启动期 AttributeError。
CROSS_MODULE_CONSUMERS = ("web.api.routes_data",)


def test_compute_next_close_ts_is_importable_cross_module():
    """``_compute_next_close_ts`` 必须仍能从本模块按名导入（跨模块硬契约）。"""
    from web.api.routes_bars_stream import _compute_next_close_ts  # noqa: F401

    assert callable(_compute_next_close_ts)


def test_router_still_exposed():
    """``router`` 必须仍然导出 —— web/server.py 会 include_router 它。

    刻意保留（哪怕已不注册任何路由）是为了让 server.py 的 router 导入继续工作。
    """
    from fastapi import APIRouter

    assert isinstance(bars_stream_router, APIRouter)


# ── 已删除符号：防止全局广播被复活 ────────────────────────────────────────────

DELETED_NAMES = (
    "_subscribers",
    "_subscribers_lock",
    "_background_task",
    "_background_bars_loop",
    "_add_subscriber",
    "_remove_subscriber",
    "_broadcast",
    "start_background_task",
    "stop_background_task",
    "_push_bar_update",
    "_push_bar_close",
    "_resolve_symbol_timeframe",
    "_format_bar",
    "bars_stream",
)


@pytest.mark.parametrize("name", DELETED_NAMES)
def test_deleted_symbols_absent(name):
    """服务端广播机制的符号必须已从模块中消失。

    这是本次改造的**回归守卫**：``_subscribers`` 是「多标签页看到同一条数据流」的
    根因，任何把它加回来的改动都会让本用例失败。
    """
    assert not hasattr(routes_bars_stream, name), f"{name} 不应再存在于 routes_bars_stream"


def test_bars_stream_endpoint_removed():
    """``/api/bars/stream`` 端点已下线（前端改为轮询 /api/bars）。"""
    paths = {getattr(r, "path", "") for r in bars_stream_router.routes}
    assert "/bars/stream" not in paths
    assert paths == set(), f"本模块不应再注册任何路由，实际: {paths}"


def test_module_source_has_no_sse_or_asyncio_dependency():
    """模块源码里不再出现 SSE / asyncio 相关引用。

    ``sse_starlette`` 与后台 Task 都是 SSE 专属，端点下线后本模块不需要它们。
    """
    src = pathlib.Path(routes_bars_stream.__file__).read_text(encoding="utf-8")
    assert "EventSourceResponse" not in src
    assert "sse_starlette" not in src
    assert "asyncio" not in src
    assert "latest_snapshot" not in src, "本模块不再直接拉数据源（前端按自己游标轮询）"


# ── _compute_next_close_ts：基本与周期换算 ────────────────────────────────────

TS = 1_700_000_000_000  # 2023-11-14 22:13:20 UTC


def test_compute_next_close_ts_basic():
    """1m timeframe：now == ts_open（elapsed=0）时返回 ts_open + 60_000 ms。"""
    result = routes_bars_stream._compute_next_close_ts(TS, "1m", now_ms=TS)
    assert result == TS + 60_000


@pytest.mark.parametrize(
    "timeframe,duration_ms",
    [
        ("1m", 60_000),
        ("5m", 5 * 60_000),
        ("15m", 15 * 60_000),
        ("30m", 30 * 60_000),
        ("1h", 60 * 60_000),
        ("2h", 2 * 60 * 60_000),
        ("4h", 4 * 60 * 60_000),
        ("1d", 24 * 60 * 60_000),
        ("1w", 7 * 24 * 60 * 60_000),
    ],
)
def test_compute_next_close_ts_various_timeframes(timeframe, duration_ms):
    """各周期都能正确换算为毫秒（注入 now_ms=ts_open 确保 elapsed=0）。"""
    assert routes_bars_stream._compute_next_close_ts(TS, timeframe, now_ms=TS) == TS + duration_ms


@pytest.mark.parametrize("timeframe", ["1m", "5m", "1h", "4h", "1d"])
def test_compute_next_close_ts_modulo_not_naive_add(timeframe):
    """必须用 ``elapsed % duration`` 取模算法，不能是简单的 ``ts_open + duration``。

    朴素算法在 ts_open 与周期边界不对齐时会整体偏移（时区偏移），
    这里用一个「非整周期起点」验证：结果必须落在 (now, now+duration] 内。
    """
    duration_ms = routes_bars_stream._compute_next_close_ts(TS, timeframe, now_ms=TS) - TS
    now = TS + 7_919  # 任意非整周期偏移
    result = routes_bars_stream._compute_next_close_ts(TS, timeframe, now_ms=now)
    assert result == TS + duration_ms, "非对齐起点必须靠取模回到同一条周期边界"
    assert now < result <= now + duration_ms


def test_compute_next_close_ts_exact_boundary_rolls_to_next_period():
    """now 恰好落在周期边界上（remainder==0）时返回 now + duration，而不是 now。"""
    now = TS + 5 * 60_000  # 5m 周期的第 1 条边界
    result = routes_bars_stream._compute_next_close_ts(TS, "5m", now_ms=now)
    assert result == now + 5 * 60_000


def test_compute_next_close_ts_is_stable_within_period():
    """同一根 forming bar 内多次查询必须返回**同一个**边界时间戳。"""
    results = {
        routes_bars_stream._compute_next_close_ts(TS, "15m", now_ms=TS + offset)
        for offset in (0, 1_000, 60_000, 899_999)
    }
    assert len(results) == 1
    assert results.pop() == TS + 15 * 60_000


def test_compute_next_close_ts_handles_stale_ts_open():
    """ts_open 已远早于 now（休市后残留 / 拉取延迟）时仍返回「下一个未来边界」。

    取模算法保证返回值恒 > now，不会返回一个过去的时间戳。
    """
    now = int(time.time() * 1000)
    for timeframe in ("1m", "5m", "1h", "1d"):
        result = routes_bars_stream._compute_next_close_ts(now - 37 * 86_400_000, timeframe, now_ms=now)
        assert result is not None
        assert result > now, f"{timeframe} 返回了过去的时间戳: {result}"


def test_compute_next_close_ts_uses_wall_clock_when_now_omitted():
    """不传 now_ms 时用 time.time()，返回值必须落在未来且不超过 1 个周期。"""
    before_ms = int(time.time() * 1000)
    result = routes_bars_stream._compute_next_close_ts(before_ms - 30_000, "5m")
    after_ms = int(time.time() * 1000)
    assert result is not None
    assert before_ms <= result <= after_ms + 5 * 60_000


# ── 休市 / 非法输入：一律返回 None ───────────────────────────────────────────

@pytest.mark.parametrize("ts_open", [0, -1, -1_700_000_000_000])
def test_compute_next_close_ts_nonpositive_ts_open(ts_open):
    """ts_open <= 0 → None（休市快照里 ts_open 可能为 0）。"""
    assert routes_bars_stream._compute_next_close_ts(ts_open, "1m") is None


@pytest.mark.parametrize("ts_open", [None, "abc", "", [], {}, float("nan")])
def test_compute_next_close_ts_unparsable_ts_open(ts_open):
    """ts_open 无法转 int → None（绝不抛异常：调用方在 REST 路径上）。"""
    assert routes_bars_stream._compute_next_close_ts(ts_open, "1m") is None


@pytest.mark.parametrize("timeframe", ["", None, "xyz", "0m", "m", "5x"])
def test_compute_next_close_ts_invalid_timeframe(timeframe):
    """timeframe 无法解析 → None。

    休市路径尤其重要：/api/bars/next-close 必须在 bars[0].closed == True 时
    **短路**返回 market_closed:true，绝不能落到这里算出错误的未来边界。
    """
    assert routes_bars_stream._compute_next_close_ts(TS, timeframe) is None


def test_compute_next_close_ts_never_raises_on_garbage():
    """任意垃圾输入都只返回 None 或 int，绝不抛异常（调用方在 REST 路径上）。"""
    for ts_open in (None, "", "x", 0, -5):
        for timeframe in (None, "", "??", "1m"):
            got = routes_bars_stream._compute_next_close_ts(ts_open, timeframe)
            assert got is None or isinstance(got, int)
