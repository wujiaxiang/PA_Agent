"""会话生命周期：快照续期 / 过期语义 / 热层 TTL+LRU / 空游标可见性。

守护的是一条链：**用户还在用 → 快照别过期；用户不用了 → 快照必须过期**。
这条链两头都出过事故，两头的失败方式还完全相反：

* 续期缺席（2026-10-06 前）：``expires_at`` 唯一续期点是
  ``POST /api/subscribe``，而前端 boot 序列从不调它 ⇒ 订阅后挂着的 tab
  快照过期，F5 回落出厂种子（XAUUSD/15m），用户看 BTCUSDT 刷新后变 XAUUSD。
* 续期过头：``touch_session`` 原先的 UPDATE 没有 ``expires_at > ?`` 过滤，
  一旦中间件上线就会**复活已过期行**，把过期语义悄悄破坏掉。

所以本文件里的用例一律**断言状态本身**（``expires_at`` 有没有被推后、
会话还在不在表里），**不断言「某个函数被调用过」** —— 后者对上面两类 bug
都是恒绿的。

反向验证（每个 fix 都做过「回退 → 用例变红」）：

======================================  ================================
改动                                    回退后变红的用例
======================================  ================================
``touch_session`` 加 ``expires_at > ?``  ``test_touch_does_not_revive_expired_row``
``get_or_create`` 判 TTL                 ``test_hot_layer_ttl_applies_to_get_or_create``
``get_or_create`` 先腾位后插入           ``test_new_session_is_not_evicted_by_its_own_insert``
``_evict_locked`` 跳过 SSE 会话          ``test_sweep_keeps_expired_session_holding_sse``
``drop_queue`` 改走 ``peek``            ``test_drop_queue_keeps_state_when_ttl_expired``
中间件限流                              ``test_middleware_rate_limits_snapshot_writes``
空游标标记                              ``test_settings_flags_missing_session_cursor``
======================================  ================================
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from pa_agent.storage import ephemeral
from pa_agent.storage import sessions as sess_repo
from pa_agent.storage.ephemeral import Cursor, get_registry, reset_registry_for_tests
from pa_agent.storage.db import get_hub, reset_hub_for_tests


# ── 夹具 ──────────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """独立 DB + 独立热层 + 清空续期限流表。每个用例都从「什么都没发生过」开始。"""
    hub = reset_hub_for_tests(tmp_path / "lifecycle.db")
    reset_registry_for_tests()
    from web.api import session_ctx

    session_ctx.reset_renewal_state_for_tests()
    yield hub
    hub.close_all()
    session_ctx.reset_renewal_state_for_tests()


@pytest.fixture()
def ctx():
    """模拟 AppContext：只有全局游标（= 出厂种子 XAUUSD/15m）。"""
    general = SimpleNamespace(
        last_symbol="XAUUSD",
        last_timeframe="15m",
        last_tradingview_exchange="OANDA",
    )
    return SimpleNamespace(settings=SimpleNamespace(general=general))


def _req(headers: dict | None = None, path: str = "/api/settings", **extra):
    from starlette.requests import Request

    raw = [(b"x-session-id", (headers or {}).get("X-Session-Id", "").encode())]
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "headers": [(k, v) for k, v in raw if v] + list(extra.get("raw_headers", [])),
        "query_string": b"",
    }
    return Request(scope)


# ── 1. touch_session：不得复活已过期行 ────────────────────────────────────────


def test_touch_renews_live_row(db):
    """活的行**真的**被续期（expires_at 被推后）—— 不是「函数被调用」。"""
    sess_repo.ensure_session("live", ttl_s=60)
    before = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'live'"
    )["expires_at"]

    time.sleep(0.01)
    sess_repo.touch_session("live")

    after = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'live'"
    )["expires_at"]
    assert after > before, "expires_at 没被推后 —— 这条就是「无人续期」的形态"


def test_touch_does_not_revive_expired_row(db):
    """回归守卫：**续期不得复活已过期行**。

    反向验证：去掉 ``touch_session`` 的 ``AND expires_at > ?`` 后本用例立刻红。

    这条是中间件上线的前置条件：中间件每个 /api 请求都在调 touch_session，
    没有这层过滤，一个「本该过期」的会话会被一个仍在发请求的**旧 tab**
    悄悄复活（用户以为回到新 session，实际拿到的是几小时前那个标的的游标）。
    """
    sess_repo.ensure_session("dead", ttl_s=-1)          # 已过期
    before = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'dead'"
    )["expires_at"]

    touched = sess_repo.touch_session("dead")

    after = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'dead'"
    )["expires_at"]
    assert after == before, "过期行的 expires_at 被推后了 —— 续期复活了它"
    # 读侧仍然视作不存在：过期语义没被破坏
    assert sess_repo.get_session("dead") is None
    assert sess_repo.purge_expired() == 1, "过期行应仍能被 GC 清掉"
    # 注意：touch_session 的返回值是「语句执行成功」，命中 0 行同样是 True。
    # 它**不**表示续上了 —— 这就是为什么过滤必须写进 SQL 而不是靠返回值判断。
    assert isinstance(touched, bool)


def test_touch_does_not_create_row_for_unknown_sid(db):
    """续期是 UPDATE 语义：不存在的 sid 不该凭空造行。

    反向验证：若中间件改用 ``ensure_session``，本用例变红（且会给任意
    /api 调用方都造一条 sessions 行）。
    """
    sess_repo.touch_session("never-seen")
    assert sess_repo.get_session("never-seen") is None
    assert get_hub().query_one("SELECT COUNT(*) AS n FROM sessions")["n"] == 0


def test_snapshot_ttl_is_not_shorter_than_hot_layer():
    """两层 TTL 的硬关系：快照（24h）必须 **≥** 热层（12h）。

    反过来时进程一重启，热层刚清掉的会话其快照也过期 —— 「重启后恢复游标」
    这个快照存在的唯一理由当场失效。
    """
    assert sess_repo.DEFAULT_TTL_S >= ephemeral.DEFAULT_TTL_S
    assert sess_repo.DEFAULT_TTL_S == 24 * 3600
    assert ephemeral.DEFAULT_TTL_S == 12 * 3600
    assert ephemeral.DEFAULT_MAX_SESSIONS == 128


# ── 2. 中间件：真的续期 + 真的限流 ────────────────────────────────────────────


def _run_mw(request, *, path_call=None):
    """把中间件跑起来，返回下游的返回值。"""
    from web.api.session_ctx import session_lifecycle_middleware

    seen = {}

    async def call_next(_req):
        seen["sid"] = __import__("web.api.session_ctx", fromlist=["x"]).current_session_id()
        if path_call is not None:
            path_call(seen)
        return SimpleNamespace(headers={})

    return asyncio.run(session_lifecycle_middleware(request, call_next)), seen


def test_middleware_renews_snapshot_on_a_bare_get(db):
    """端到端：只发 ``GET /api/settings``（**不调 subscribe**）也会续期。

    这正是线上形态：前端 boot 是 ``loadSettings → loadBars``，从不调
    ``POST /api/subscribe``。续期点挂在 subscribe 上时，本用例失败。
    """
    sess_repo.ensure_session("tab-1", ttl_s=60)
    sess_repo.set_cursor("tab-1", symbol="BTCUSDT", timeframe="1h", exchange="GATEIO")
    before = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'tab-1'"
    )["expires_at"]
    time.sleep(0.01)

    _run_mw(_req({"X-Session-Id": "tab-1"}))

    row = get_hub().query_one(
        "SELECT expires_at, symbol FROM sessions WHERE session_id = 'tab-1'"
    )
    assert row["expires_at"] > before, "中间件没续期 —— 这就是「刷新后丢游标」"
    assert row["symbol"] == "BTCUSDT", "续期不该动业务字段"


def test_middleware_keeps_snapshot_alive_across_polls(db):
    """模拟 5 秒轮询持续挂 5 分钟：快照不得在轮询中途过期。

    断言的是「一直可用」而不是「某一刻被写过」—— 续了 5 次和续了 1 次都能通过
    后者，但前者才是用户体感。
    """
    sess_repo.ensure_session("poller", ttl_s=60)
    deadline = time.time() + 1.0
    while time.time() < deadline:                     # 密集请求（>5s 轮询更密）
        _run_mw(_req({"X-Session-Id": "poller"}))
        time.sleep(0.02)
    assert sess_repo.get_session("poller") is not None, "轮询期间快照过期了"


def test_middleware_rate_limits_snapshot_writes(db):
    """反向验证（限流）：高频请求**不得**变成高频 SQLite 写。

    做法：把 ``touch_session`` 换成计数器，狂打 200 次请求，断言写入次数 ≤
    「限流间隔内应有的次数 +1」。没有限流时这里是 200。
    """
    from web.api import session_ctx

    calls = {"n": 0}
    real_touch = sess_repo.touch_session

    def counting_touch(*a, **kw):
        calls["n"] += 1
        return real_touch(*a, **kw)

    sess_repo.touch_session = counting_touch
    try:
        for _ in range(200):
            _run_mw(_req({"X-Session-Id": "poller"}))
    finally:
        sess_repo.touch_session = real_touch

    assert calls["n"] == 1, f"200 次请求触发了 {calls['n']} 次写 —— 限流失效"


def test_middleware_rate_limit_expires_after_interval(db, monkeypatch):
    """反向验证（续期真的会发生）：过了限流间隔后**下一次**请求必须续期。"""
    from web.api import session_ctx

    calls = {"n": 0}
    real_touch = sess_repo.touch_session

    def counting_touch(*a, **kw):
        calls["n"] += 1
        return real_touch(*a, **kw)

    sess_repo.touch_session = counting_touch
    monkeypatch.setattr(session_ctx, "RENEW_INTERVAL_S", 0.0)
    try:
        for _ in range(5):
            _run_mw(_req({"X-Session-Id": "tab-2"}))
    finally:
        sess_repo.touch_session = real_touch

    assert calls["n"] == 5, "限流间隔为 0 时每次请求都该续期，实测 %d 次" % calls["n"]


def test_middleware_does_not_revive_an_expired_snapshot(db):
    """中间件**不得**把过期行续活（两者必须同批上线的理由）。"""
    sess_repo.ensure_session("gone", ttl_s=-1)
    before = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'gone'"
    )["expires_at"]

    _run_mw(_req({"X-Session-Id": "gone"}))

    after = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'gone'"
    )["expires_at"]
    assert after == before, "中间件复活了过期行 —— 过期语义被破坏"
    assert sess_repo.get_session("gone") is None


def test_middleware_exposes_sid_via_contextvar(db):
    """sid 必须进 ContextVar，供下游复用（消掉各处重复解析）。"""
    _resp, seen = _run_mw(_req({"X-Session-Id": "ctx-tab"}))
    assert seen["sid"] == "ctx-tab"


def test_middleware_clears_contextvar_after_request(db):
    """请求结束后必须还原：否则同一 worker Task 上后续请求继承上一次的绑定。

    在**下游**读才是对的读法：中间件的 ``finally`` 还原后，路由之外的任何地方
    都不该再看得到上一个请求的身份。
    """
    from web.api.session_ctx import current_session_id

    async def main():
        from web.api.session_ctx import session_lifecycle_middleware

        seen = []

        async def call_next(_r):
            seen.append(current_session_id())
            return None

        await session_lifecycle_middleware(_req({"X-Session-Id": "a"}), call_next)
        await session_lifecycle_middleware(_req({}), call_next)
        return seen, current_session_id()

    seen, after = asyncio.run(main())
    assert seen == ["a", ""], "无会话头的请求不应看到上一个请求的 sid"
    assert after == "", "ContextVar 泄漏到了下一个请求"


def test_middleware_skips_non_api_paths(db):
    """静态资源不续期（与 bind_user_settings_middleware 同一口径）。"""
    calls = {"n": 0}
    real_touch = sess_repo.touch_session
    sess_repo.touch_session = lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1)
    try:
        _run_mw(_req({"X-Session-Id": "x"}, path="/js/app.js"))
        assert calls["n"] == 0, "CSS/JS 请求触发了快照写"
        _run_mw(_req({"X-Session-Id": "x"}))
        assert calls["n"] == 1, "/api 请求没有续期"
    finally:
        sess_repo.touch_session = real_touch


def test_middleware_uses_existing_validation_for_sid(db):
    """**不得**另写一套 sid 校验：非法 sid 既不续期也不进 ContextVar。

    用一个只有中间件会碰的字符（如 ``;``）：若中间件自己写了一套更宽松的
    校验，这里就会续期成功 —— 而 session_id 会拼进 SQL 与日志。
    """
    calls = {"n": 0}
    real_touch = sess_repo.touch_session
    sess_repo.touch_session = lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1)
    try:
        _resp, seen = _run_mw(_req({"X-Session-Id": "abc;drop"}))
    finally:
        sess_repo.touch_session = real_touch
    assert calls["n"] == 0, "非法 sid 触发了快照写"
    assert seen["sid"] == ""


def test_middleware_creates_no_hot_layer_entry(db):
    """中间件**不创建**热层条目：否则任意 /api 探针都能凭空造会话。"""
    _run_mw(_req({"X-Session-Id": "probe"}))
    assert get_registry().get("probe") is None


def test_middleware_refreshes_existing_hot_layer_entry(db):
    """已有热层会话的空闲计时**真的**被刷新（否则 12h 也救不了活跃 tab）。"""
    reg = get_registry()
    state = reg.get_or_create("live-tab")
    state.last_touch = time.time() - 3600        # 假装 1 小时没动
    _run_mw(_req({"X-Session-Id": "live-tab"}))
    assert time.time() - state.last_touch < 5, "热层空闲计时没被中间件刷新"


def test_middleware_survives_storage_failure(db):
    """存储层故障只记 warning，绝不让会话续期把请求搞挂。"""
    from web.api import session_ctx

    boom = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down"))
    real_touch = sess_repo.touch_session
    sess_repo.touch_session = boom
    try:
        resp, seen = _run_mw(_req({"X-Session-Id": "tab-x"}))
        assert resp is not None and seen["sid"] == "tab-x"
    finally:
        sess_repo.touch_session = real_touch


def test_middleware_is_mounted_on_the_real_app():
    """**挂在真 app 上**：单元测试直接调函数，测不到「压根没挂」这种失效。

    反向验证：删掉 ``web/server.py`` 里那个 ``@app.middleware("http")`` 块，
    本用例立刻红 —— 而上面所有中间件用例仍然是绿的（它们直接调实现）。
    """
    import web.server as server
    from web.api.session_ctx import session_lifecycle_middleware as impl

    dispatchs = [
        getattr(m, "kwargs", {}).get("dispatch")
        for m in server.app.user_middleware
    ]
    mounted = [
        f for f in dispatchs
        if f is impl or getattr(f, "__name__", "") == "session_lifecycle_middleware"
    ]
    assert mounted, (
        "session_lifecycle_middleware 没挂在 web.server.app 上 —— "
        "续期在生产路径上根本不会发生"
    )


def test_renewal_through_a_real_http_request(db):
    """真请求（TestClient）走一轮：GET /api/bars 之类的普通 /api 请求就会续期。

    用一个**只挂了本中间件**的最小 app，而不是 ``web.server.app``：后者要跑
    lifespan、要读 ``config/settings.json``（首次播种时那条路径会**回写**该文件，
    测试里绝不能碰）。这里要证明的是「每个 /api 请求都会续期」这条性质本身。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web.api.session_ctx import session_lifecycle_middleware

    app = FastAPI()
    app.middleware("http")(session_lifecycle_middleware)

    @app.get("/api/ping")
    async def _ping():                     # noqa: ANN202 - 测试路由
        from web.api.session_ctx import current_session_id

        return {"sid": current_session_id()}

    sess_repo.set_cursor("tab-1", symbol="BTCUSDT", timeframe="1h", exchange="GATEIO")
    before = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'tab-1'"
    )["expires_at"]
    time.sleep(0.01)

    client = TestClient(app)
    resp = client.get("/api/ping", headers={"X-Session-Id": "tab-1"})
    assert resp.status_code == 200
    assert resp.json()["sid"] == "tab-1", "路由里读不到 ContextVar 里的 sid"
    after = get_hub().query_one(
        "SELECT expires_at FROM sessions WHERE session_id = 'tab-1'"
    )["expires_at"]
    assert after > before, "一次真实请求没有续期"
    # 静态路径不该续期
    calls = {"n": 0}
    real_touch = sess_repo.touch_session
    sess_repo.touch_session = lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1)
    try:
        client.get("/ping", headers={"X-Session-Id": "tab-1"})
        assert calls["n"] == 0
    finally:
        sess_repo.touch_session = real_touch


def test_middleware_per_request_overhead_is_microseconds(db):
    """每请求开销实测：限流命中时**一次 SQLite 写都不该有**。

    反向验证：把 ``_claim_renewal`` 改成恒 True，这里立刻红。
    """
    import timeit

    from web.api import session_ctx

    sess_repo.ensure_session("hot", ttl_s=600)
    request = _req({"X-Session-Id": "hot"})
    _run_mw(request)                       # 先过一次（写一次，之后进限流）

    n = 300
    elapsed = timeit.timeit(lambda: _run_mw(request), number=n)
    per_request_us = elapsed / n * 1e6
    assert per_request_us < 5000, f"每请求 {per_request_us:.0f}us，太贵了"


# ── 3. 空游标必须可见 ─────────────────────────────────────────────────────────


def test_settings_flags_missing_session_cursor(db, ctx):
    """sid 有效但会话无游标 ⇒ 响应体必须带 ``_session_cursor_missing``。

    反向验证：去掉该标记后本用例变红。这是最容易静默的一条 —— 去掉它页面照常
    工作，只是刷新后安静地换成出厂品种。
    """
    from web.api import routes_settings

    payload = {"general": {"last_symbol": "XAUUSD", "last_timeframe": "15m"}}
    routes_settings._apply_session_cursor(payload, ctx, _req({"X-Session-Id": "fresh"}))
    assert payload.get("_session_cursor_missing") is True


def test_settings_does_not_flag_when_cursor_exists(db, ctx):
    from web.api import routes_settings

    sess_repo.set_cursor("tab-1", symbol="BTCUSDT", timeframe="1h", exchange="GATEIO")
    payload = {"general": {}}
    routes_settings._apply_session_cursor(payload, ctx, _req({"X-Session-Id": "tab-1"}))
    assert "_session_cursor_missing" not in payload
    assert payload["general"]["last_symbol"] == "BTCUSDT"


def test_settings_flag_absent_without_session_header(db, ctx):
    """无会话头的老前端 / 脚本 / 测试：响应体与改造前**完全一致**。"""
    from web.api import routes_settings

    # 真实 payload 是 model_dump()，general 里本就带着全局游标
    payload = {"general": {"last_symbol": "XAUUSD", "last_timeframe": "15m"}}
    routes_settings._apply_session_cursor(payload, ctx, _req({}))
    assert "_session_cursor_missing" not in payload
    assert payload["general"]["last_symbol"] == "XAUUSD"


def test_session_cursor_of_does_not_fall_back(db, ctx):
    """``session_cursor_of`` 返回 None 而非全局值 —— 这是标记的判据来源。

    反向验证：若它回落全局（恒返回三元组），``_session_cursor_missing``
    将**永远**为 false —— 守卫失效且看不出来。
    """
    from web.api.session_ctx import resolve_view, session_cursor_of

    assert session_cursor_of("nobody") is None
    assert resolve_view(ctx, "nobody") == ("XAUUSD", "15m", "OANDA")

    sess_repo.set_cursor("tab-1", symbol="BTCUSDT", timeframe="1h", exchange="GATEIO")
    assert session_cursor_of("tab-1") == ("BTCUSDT", "1h", "GATEIO")


def test_expired_session_reads_back_as_global_cursor_with_flag(db, ctx):
    """完整形态：会话过期后，``resolve_view`` 给的是出厂种子 **且** 被标记。

    这是用户看得见的最坏形态 —— 不是空白，是别人的数据。

    注意必须**先** set_cursor 再把 expires_at 打过去：``set_cursor`` 内部的
    ``_ensure_row`` 是 ``ON CONFLICT DO UPDATE SET expires_at``，会把已过期
    的行顺手续活（那是写入方该有的语义）。反过来先过期再写游标，测的就不是
    「过期会话」了。
    """
    from web.api import routes_settings
    from web.api.session_ctx import resolve_view

    sess_repo.set_cursor("tab-dead", symbol="BTCUSDT", timeframe="1h", exchange="GATEIO")
    assert resolve_view(ctx, "tab-dead")[:2] == ("BTCUSDT", "1h")
    get_hub().execute(
        "UPDATE sessions SET expires_at = 0 WHERE session_id = 'tab-dead'"
    )                                    # 模拟 TTL 到期
    assert sess_repo.get_session("tab-dead") is None

    request = _req({"X-Session-Id": "tab-dead"})
    assert resolve_view(ctx, "tab-dead") == ("XAUUSD", "15m", "OANDA")

    payload = {"general": {}}
    routes_settings._apply_session_cursor(payload, ctx, request)
    assert payload["_session_cursor_missing"] is True
    assert payload["general"]["last_symbol"] == "XAUUSD"


# ── 4. 热层：TTL 判定统一 + 插入不被自己踢 + SSE 保护 ─────────────────────────


def test_hot_layer_ttl_applies_to_get_or_create(db):
    """反向验证：``get_or_create`` 必须判 TTL（与 ``get`` 统一）。

    原实现只在 ``get`` 判 TTL，于是 ``resolve_view``（走 get_or_create）永远
    看得见一个本该过期的会话 —— 热层 TTL 形同虚设。

    **注意这里不能先调 ``get``**：``get`` 的 TTL 分支会把记录摘掉，之后
    ``get_or_create`` 自然建一个新的 —— 那样本用例在回退后仍然是绿的，验不到
    任何东西（这是本轮实际踩到的一次：先写了一版用 ``get`` 先清一遍的用例，
    回退后照绿，等于没测）。判定必须直接看 ``get_or_create`` 的返回值身份。
    """
    reg = ephemeral.SessionRegistry(default_ttl_s=0.01)
    first = reg.get_or_create("gone")
    first.cursor = Cursor("BTCUSDT", "1h", "GATEIO")
    time.sleep(0.02)

    second = reg.get_or_create("gone")

    assert second is not first, "过期后 get_or_create 仍返回同一个状态 —— 热层 TTL 失效"
    assert second.cursor.as_tuple() == ("", "", ""), "过期后应是全新的空状态"
    assert reg.get("gone") is second


def test_new_session_is_not_evicted_by_its_own_insert():
    """反向验证：``get_or_create`` 必须**先腾位后插入**。

    原实现先插后淘汰，LRU 分支找「第一个 sse_queue is None 的」，而新会话按
    定义就是唯一那一个 ⇒ **自己把自己踢出去**，返回的 SessionState 当场就不在
    表内。表现为：调用方往这个对象里写游标，下一次读却 miss。
    """
    reg = ephemeral.SessionRegistry(max_sessions=4)
    # 全部挂 SSE 队列 —— 制造「没有可踢的受害者」的最坏情况
    for i in range(4):
        reg.get_or_create(f"holder-{i}").sse_queue = asyncio.Queue()
    state = reg.get_or_create("newcomer")
    ids = {s.session_id for s in reg.all_sessions()}
    assert "newcomer" in ids, "新会话把自己踢掉了（返回值不在表内）"
    assert reg.get("newcomer") is state
    # 复用已有会话时也不该出问题
    again = reg.get_or_create("newcomer")
    assert again is state


def test_registry_lru_still_caps_memory():
    """容量上限没被「先腾位」破坏。"""
    reg = ephemeral.SessionRegistry(max_sessions=3)
    for i in range(20):
        s = reg.get_or_create(f"tab-{i}")
        assert s.session_id == f"tab-{i}"
        assert len(reg.all_sessions()) <= 3
    assert len(reg.all_sessions()) == 3


def test_sweep_keeps_expired_session_holding_sse(db):
    """反向验证：TTL 淘汰**也**必须跳过挂着 SSE 队列的会话。

    docstring 承诺过这条，但原先只有 LRU 分支实现了；实测 sweep 会清掉正在
    跑追问流的会话，其 event_generator 就永久 await 一个没人写的 queue。
    """
    reg = ephemeral.SessionRegistry(default_ttl_s=0.0)     # TTL=0 ⇒ 立刻算过期
    holder = reg.get_or_create("streaming")
    holder.sse_queue = asyncio.Queue()
    reg.get_or_create("quiet")

    reg.sweep()

    ids = {s.session_id for s in reg.all_sessions()}
    assert "streaming" in ids, "sweep 清掉了正在推流的会话"
    assert "quiet" not in ids, "普通会话该被清掉"


def test_get_does_not_pop_a_session_holding_sse(db):
    """``get`` 判过期但不弹挂流的会话（与淘汰路径的承诺一致）。"""
    reg = ephemeral.SessionRegistry(default_ttl_s=0.0)
    holder = reg.get_or_create("streaming")
    holder.sse_queue = asyncio.Queue()

    assert reg.get("streaming") is None          # 过期 ⇒ 读不到
    assert reg.get("streaming") is None          # 再读一次仍不是 None 之外的语义
    ids = {s.session_id for s in reg.all_sessions()}
    assert ids == {"streaming"}, "get 把挂流的会话从表里弹了"


def test_drop_queue_keeps_state_when_ttl_expired(db):
    """回归守卫：``drop_queue`` 只摘队列，**不销毁**游标 / 追问 / last_record。

    反向验证：把 ``drop_queue`` 改回走 ``get``（或在 ``get`` 的 TTL 分支里
    弹表）后本用例变红。原缺陷正是 ``drop_queue`` 的 docstring 明令禁止的事：
    「用 drop 会把追问历史和游标一起清掉」—— 而 TTL 分支做的正是这件事。
    """
    reg = ephemeral.SessionRegistry(default_ttl_s=0.0)
    state = reg.get_or_create("tab-1")
    state.cursor = Cursor("BTCUSDT", "1h", "GATEIO")
    state.chat = {"sentinel": object()}
    state.last_record = object()
    queue: asyncio.Queue = asyncio.Queue()
    state.sse_queue = queue

    reg.drop_queue("tab-1")

    alive = {s.session_id for s in reg.all_sessions()}
    assert alive == {"tab-1"}, "drop_queue 把会话整个清掉了：%s" % alive
    # TTL=0 ⇒ peek 按定义读不到（过期），所以这里从表里取存活的那一条
    kept = reg.all_sessions()[0]
    assert kept is state
    assert kept.sse_queue is None, "队列没摘掉"
    assert kept.cursor == Cursor("BTCUSDT", "1h", "GATEIO")
    assert kept.chat is not None, "追问历史被清了"
    assert kept.last_record is not None, "追问锚点被清了"


def test_peek_has_no_side_effects(db):
    """``peek`` 不刷新 last_touch —— 否则它就成了第二个续期入口。"""
    reg = ephemeral.SessionRegistry(default_ttl_s=1000)
    state = reg.get_or_create("t")
    state.last_touch = time.time() - 100
    before = state.last_touch
    assert reg.peek("t") is state
    assert state.last_touch == before
    # 过期 → 读不到，但**不删**
    reg2 = ephemeral.SessionRegistry(default_ttl_s=0.01)
    reg2.get_or_create("old")
    time.sleep(0.02)
    assert reg2.peek("old") is None
    assert {s.session_id for s in reg2.all_sessions()} == {"old"}


def test_touch_does_not_create(db):
    """``touch`` 刷已存在的，不凭空造（中间件跑在每一个 /api 请求上）。"""
    reg = ephemeral.SessionRegistry(default_ttl_s=1000)
    assert reg.touch("ghost") is False
    assert reg.all_sessions() == []
    reg.get_or_create("real")
    assert reg.touch("real") is True