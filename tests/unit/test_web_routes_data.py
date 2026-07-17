# -*- coding: utf-8 -*-
"""Unit tests for web/api/routes_data.py — 数据源相关路由。

覆盖：
- GET /api/datasources 返回数据源列表
- GET /api/timeframes 返回当前数据源支持的周期
- GET /api/bars 返回最新 N 根 K 线（含 closed 字段）

mock 策略：用真实 FastAPI app + 注入 mock ctx，避免依赖真实数据源。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pa_agent.data.base import KlineBar
from web.api.routes_data import router as data_router


def _make_mock_ctx(bars=None, timeframes=None):
    """构造一个最小化的 mock AppContext，仅满足 routes_data 的字段访问。"""
    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.general.last_symbol = "XAUUSD"
    ctx.settings.general.last_timeframe = "1h"
    ctx.settings.general.last_data_source = "mock"

    ds = MagicMock()
    ds.supported_timeframes.return_value = timeframes or ["1m", "5m", "15m", "1h", "4h", "1d"]
    ds.latest_snapshot.return_value = bars if bars is not None else _default_bars()
    ctx.data_source = ds
    return ctx


def _default_bars():
    """返回 5 根 K 线，最后一根为 forming（closed=False）。"""
    return [
        KlineBar(seq=5, ts_open=1_700_000_000_000, open=2000.0, high=2010.0, low=1995.0,
                 close=2005.0, volume=100.0, closed=True),
        KlineBar(seq=4, ts_open=1_700_000_060_000, open=2005.0, high=2015.0, low=2000.0,
                 close=2012.0, volume=110.0, closed=True),
        KlineBar(seq=3, ts_open=1_700_000_120_000, open=2012.0, high=2020.0, low=2008.0,
                 close=2018.0, volume=90.0, closed=True),
        KlineBar(seq=2, ts_open=1_700_000_180_000, open=2018.0, high=2025.0, low=2015.0,
                 close=2022.0, volume=120.0, closed=True),
        KlineBar(seq=0, ts_open=1_700_000_240_000, open=2022.0, high=2028.0, low=2020.0,
                 close=2026.0, volume=50.0, closed=False),  # forming bar
    ]


@pytest.fixture
def client():
    """构建一个仅挂载 data_router 的 FastAPI 测试 app。"""
    app = FastAPI()
    app.include_router(data_router, prefix="/api")

    # 用 lifespan-style 注入 ctx
    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = _make_mock_ctx()

    with TestClient(app) as c:
        yield c


def test_list_datasources(client):
    """GET /api/datasources 返回 [{id, label}, ...] 列表。"""
    resp = client.get("/api/datasources")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
    assert all("id" in d and "label" in d for d in data)


def test_list_timeframes(client):
    """GET /api/timeframes 返回当前数据源支持的周期列表。"""
    resp = client.get("/api/timeframes")
    assert resp.status_code == 200
    tfs = resp.json()
    assert isinstance(tfs, list)
    assert "1h" in tfs


def test_get_bars_returns_closed_field(client):
    """GET /api/bars 返回的每根 bar 必须包含 closed 字段。"""
    resp = client.get("/api/bars?count=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "XAUUSD"
    assert body["timeframe"] == "1h"
    assert "bars" in body
    bars = body["bars"]
    assert len(bars) == 5
    for b in bars:
        assert "closed" in b, "每根 bar 必须包含 closed 字段"
        assert isinstance(b["closed"], bool)
    # 最后一根应为 forming bar（closed=False）
    forming = [b for b in bars if b["closed"] is False]
    assert len(forming) == 1, "应有且仅有一根 forming bar"


def test_get_bars_count_param(client):
    """GET /api/bars?count=N 调用 latest_snapshot(N)。"""
    resp = client.get("/api/bars?count=3")
    assert resp.status_code == 200
    # 验证 latest_snapshot 收到 count 参数
    client.app.state.ctx.data_source.latest_snapshot.assert_called_with(3)
