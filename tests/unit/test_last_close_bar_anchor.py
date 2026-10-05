"""`last_close_bar_iso` 必须指向真正最后一根已收盘 bar。

历史回看的视窗对齐与方向箭头都以它为锚。此前实现硬取
``kline_data[1]``，隐含假设 ``bars[0]`` 恒为未收盘 forming bar；休市或
快照未带 forming bar 时该假设不成立，锚点会整整差一根 → 箭头指向错误的
K 线（用户实机报告的问题）。
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from pa_agent.orchestrator.two_stage import _pick_last_closed_bar


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat(
        timespec="milliseconds")


def test_picks_first_closed_bar_when_forming_bar_present():
    """常规情况：bars[0] 未收盘，第二根才是最后已收盘。"""
    bars = [
        {"ts_open": 3000, "closed": False},
        {"ts_open": 2000, "closed": True},
        {"ts_open": 1000, "closed": True},
    ]
    assert _pick_last_closed_bar(bars)["ts_open"] == 2000


def test_picks_bars_zero_when_no_forming_bar():
    """休市/快照无 forming bar：bars[0] 本身就是已收盘的。

    旧实现硬取 index 1，这里会取到倒数第二根 —— 整整差一根。
    """
    bars = [
        {"ts_open": 3000, "closed": True},
        {"ts_open": 2000, "closed": True},
        {"ts_open": 1000, "closed": True},
    ]
    assert _pick_last_closed_bar(bars)["ts_open"] == 3000


def test_skips_multiple_unclosed_bars():
    bars = [
        {"ts_open": 5000, "closed": False},
        {"ts_open": 4000, "closed": False},
        {"ts_open": 3000, "closed": True},
    ]
    assert _pick_last_closed_bar(bars)["ts_open"] == 3000


def test_falls_back_when_no_closed_flag():
    """老格式没有 closed 标志：退回 index 1 的旧启发式。"""
    bars = [{"ts_open": 2000}, {"ts_open": 1000}]
    assert _pick_last_closed_bar(bars)["ts_open"] == 1000


def test_falls_back_for_single_bar():
    assert _pick_last_closed_bar([{"ts_open": 2000}])["ts_open"] == 2000


def test_empty_input_returns_none():
    assert _pick_last_closed_bar([]) is None
    assert _pick_last_closed_bar(None) is None


def test_anchor_iso_roundtrip():
    bar = _pick_last_closed_bar([{"ts_open": 1791129600000, "closed": True}])
    iso = _iso(bar["ts_open"])
    assert iso == "2026-10-04T16:00:00.000+00:00"
