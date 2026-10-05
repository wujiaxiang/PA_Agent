"""``GET /api/experience`` 浏览端（只查库，2026-10-05 起无文件可回落）。

面板与检索端的关键差别：检索只取**已验证**的 win/loss，而面板必须把
pending 一并显示出来 —— 否则用户分不清「还没判定」和「系统坏了」。
但 pending 必须**可区分**，绝不能归进 success/failure 冒充结论。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    """每个用例一份干净的库。"""
    from pa_agent.storage.db import reset_hub_for_tests

    hub = reset_hub_for_tests(tmp_path / "iso.db")

    from web.api import routes_data

    app = FastAPI()
    app.include_router(routes_data.router, prefix="/api")
    yield TestClient(app)
    hub.close_all()


def _seed(symbol, status, summary, cycle="trending_tr", user_id="admin"):
    from pa_agent.storage.experience_repo import upsert_entry

    upsert_entry(
        {"symbol": symbol, "timeframe": "1h", "direction": "做多",
         "summary": summary, "pnl_pct": 1.0, "entry_price": 100.0,
         "detected_patterns": ["x"], "cycle_position": cycle},
        entry_id="%s_%s" % (user_id, summary),
        cycle_position=cycle, status=status, symbol=symbol, timeframe="1h",
        timestamp_ms=1_700_000_000_000, user_id=user_id,
    )


def test_browse_reads_from_db(client):
    _seed("BTCUSDT", "win", "A")
    _seed("NVDA", "loss", "B")
    d = client.get("/api/experience").json()
    assert d["total"] == 2
    assert d["status_counts"] == {"win": 1, "loss": 1}
    assert d["cycles"] == {"trending_tr": {"success": 1, "failure": 1}}
    assert sorted(e["summary"] for e in d["entries"]) == ["A", "B"]


def test_browse_shows_pending_and_terminal_separately(client):
    """pending 与已终态必须同时可见、且可区分。

    归错类的代价是直接的：面板会把「还没走完」显示成「亏了」，用户据此
    调整策略，而系统其实什么都没判定。
    """
    _seed("BTCUSDT", "win", "已赢")
    _seed("BTCUSDT", "pending", "待验证")
    d = client.get("/api/experience").json()
    by = {e["summary"]: e for e in d["entries"]}
    assert by["已赢"]["case_type"] == "success"
    assert by["待验证"]["case_type"] == "pending"
    assert by["待验证"]["is_pending"] is True
    assert d["status_counts"] == {"win": 1, "pending": 1}


def test_pending_not_counted_as_failure(client):
    """pending 不计入成败汇总 —— 它还没有结论。"""
    _seed("BTCUSDT", "win", "A")
    _seed("BTCUSDT", "pending", "P")
    d = client.get("/api/experience").json()
    assert d["cycles"] == {"trending_tr": {"success": 1, "failure": 0}}


def test_browse_survives_unreadable_store(client):
    """存储层读不出来时**不得** 500。

    库是唯一真源，没有文件可回落 —— 所以降级只能是「显示空 + 记 error」，
    而不是悄悄换数据源。返回 500 会让整个面板变成错误态，比空列表更糟。
    """
    from pa_agent.storage.db import get_hub

    _seed("BTCUSDT", "win", "A")
    get_hub().query("DROP TABLE experience_entries")
    r = client.get("/api/experience")
    assert r.status_code == 200, "存储故障不应让面板报错"
    assert r.json()["total"] == 0


def test_browse_filters_do_not_shrink_the_option_lists(client):
    """筛选后列表变少，但下拉选项仍来自**全量** —— 否则选项会随筛选漂移。"""
    _seed("BTCUSDT", "win", "A")
    _seed("NVDA", "loss", "B")
    d = client.get("/api/experience?symbol=BTCUSDT").json()
    assert d["total"] == 1
    assert d["symbols"] == ["BTCUSDT", "NVDA"], "下拉选项必须来自未过滤的全量"
    assert d["cycles"] == {"trending_tr": {"success": 1, "failure": 0}}


def test_browse_cycle_filter(client):
    _seed("BTCUSDT", "win", "A", cycle="trending_tr")
    _seed("XAUUSD", "win", "B", cycle="broad_channel")
    d = client.get("/api/experience?cycle=broad_channel").json()
    assert [o["value"] for o in d["cycle_options"]] == ["broad_channel"]
    assert [e["summary"] for e in d["entries"]] == ["B"]


def test_browse_respects_user_isolation(client):
    """匿名回落 admin，只看得到 admin 的。"""
    _seed("BTCUSDT", "win", "A的案例", user_id="alice")
    _seed("BTCUSDT", "win", "B的案例", user_id="bob")
    assert client.get("/api/experience").json()["total"] == 0


def test_browse_shows_own_entries_when_authenticated(client):
    """带令牌时只看得到自己的那一份。"""
    from pa_agent.storage.auth import issue_token

    _seed("BTCUSDT", "win", "A的案例", user_id="alice")
    _seed("BTCUSDT", "win", "B的案例", user_id="bob")
    token = issue_token("alice")
    d = client.get("/api/experience",
                   headers={"Authorization": "Bearer %s" % token}).json()
    assert d["total"] == 1
    assert d["entries"][0]["summary"] == "A的案例"
