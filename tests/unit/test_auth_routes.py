"""鉴权路由测试：真的能登录、真的登不上。

## 这些用例为什么这么写

本文件测的是**对外行为**（某个 URL 带/不带令牌返回什么），不是「某个函数被
调用过」。理由很直接：鉴权的失效形态是**静默放行**或**静默拒绝**，
「函数被调用」那种断言在这两种失效下都会照样通过。

具体到三条最容易被写虚的：

* 「令牌有效就能访问」—— 只断言 ``/auth/me`` 200 是不够的，必须拿**登录
  端点返回的那枚令牌**去访问一个**受保护的业务路由**。否则「登录返回了一个
  垃圾字符串但 /me 恰好不校验」也能全绿。
* 「登录端点免鉴权」—— 只测最小 app 是不够的，因为中间件可能压根没挂在真
  app 上。故另有 :func:`test_login_is_public_on_the_real_app`。
* 「用户不存在与密码错同形」—— 比的是**响应体逐字节**相同，不是「都 401」。
  文案不同就已经是枚举接口了。

## 反向验证清单（改坏哪条，对应用例必须变红）

| 回退动作 | 必须变红的用例 |
|---|---|
| ``ALLOW_ANONYMOUS_ADMIN`` 翻回 ``True`` | ``test_flag_is_flipped`` / 全部无令牌 401 用例 |
| 从 ``PUBLIC_API_PATHS`` 删掉 ``/api/auth/login`` | ``test_login_endpoint_is_public_even_though_flag_is_off`` |
| ``server.py`` 去掉 ``_install_cors()`` | ``test_cors_preflight_carries_authorization_header`` |
| ``server.py`` 不挂 auth 中间件 | ``test_auth_middleware_is_mounted_on_real_app`` |
| 删掉 ``/api/health`` 的免鉴权条目 | ``test_health_stays_public`` |
| ``routes_auth`` 里把登录失败拆成两支文案 | ``test_unknown_user_response_is_byte_identical_to_wrong_password`` |
| ``users.authenticate`` 恢复 ``get_user(user_id)`` | ``test_empty_user_id_does_not_fall_back_to_admin`` |
| ``users.authenticate`` 恢复 ``verify_password(pw, "")`` | ``test_unknown_user_still_pays_the_pbkdf2_cost`` |
| 去掉 ``revoke_token`` 调用 | ``test_logout_revokes_that_token`` |
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ADMIN_PW = "correct-horse-battery"


# ── 夹具 ───────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_auth(monkeypatch):
    """每个用例都从「无吊销记录 + 固定密钥」开始。

    固定密钥是为了不真的往 ``config/token_secret`` 落盘；清吊销表是因为
    吊销表是**模块级进程状态**，不清理就会跨用例泄漏（上一条用例登出的令牌
    在下一条用例里恰好被复用时，测试结果取决于执行顺序 —— 而顺序会变）。
    """
    from web.api import auth_ctx

    monkeypatch.setenv("PA_AGENT_TOKEN_SECRET", "auth-routes-test-secret")
    monkeypatch.delenv("PA_AGENT_TOKEN_TTL_S", raising=False)
    auth_ctx.reset_revocation_state_for_tests()
    yield
    auth_ctx.reset_revocation_state_for_tests()


@pytest.fixture()
def users_db(tmp_path: Path):
    """每个用例一份干净的库，admin 已播种口令。

    收尾把 hub 指回 conftest 的临时库 —— 不还原会让本文件把「admin 已设口令」
    这行数据留给后续用例，而它们的断言可能建立在此之上（测试之间互相喂数据
    是最难查的一类失败）。
    """
    from pa_agent.storage.db import reset_hub_for_tests
    from pa_agent.storage.users import ensure_admin_user, set_password

    hub = reset_hub_for_tests(tmp_path / "auth_routes.db")
    ensure_admin_user()
    set_password("admin", ADMIN_PW)
    yield hub
    # **必须把 admin 的口令复位**。本文件里「改密成功」的用例会就地改掉
    # users 行，而 hub 指向同一个库 —— 夹具只复位吊销表、不复位口令的话，
    # 后面的用例拿 ADMIN_PW 登录就会「莫名失败」，且失败与被测逻辑毫无关系。
    # 本轮加改密用例时踩到过一次：test_login_is_public_on_the_real_app
    # 单独跑全绿、整文件跑红。
    try:
        from pa_agent.storage.users import set_password as _sp

        _sp("admin", ADMIN_PW)
    except Exception:  # noqa: BLE001 —— 收尾不该让整份文件失败
        pass
    reset_hub_for_tests(Path(os.environ["PA_AGENT_DB_PATH"]))


@pytest.fixture()
def client(users_db):
    """最小 app：**带鉴权中间件**，否则测不到放行逻辑。

    只挂 routes_auth 是不够的 —— 免鉴权是中间件的白名单决定的，路由自己
    声明不了。所以这里刻意挂上 :func:`enforce_auth_middleware`，并额外注册
    一个受保护的 ``/api/protected`` 业务路由，用来验证「登录拿到的令牌真的
    能开业务接口」，而不只是能开 ``/auth/me``。
    """
    from web.api.auth_ctx import enforce_auth_middleware
    from web.api import routes_auth

    app = FastAPI()
    app.middleware("http")(enforce_auth_middleware)
    app.include_router(routes_auth.router, prefix="/api")

    @app.get("/api/protected")
    async def protected():
        from web.api.auth_ctx import current_user_id

        return {"secret": "settings-credentials-go-here"}

    return TestClient(app)


def _login(client, user_id="admin", password=ADMIN_PW):
    return client.post("/api/auth/login",
                       json={"user_id": user_id, "password": password})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 登录：真的能登 ─────────────────────────────────────────────────────────────


def test_login_succeeds_and_returns_a_token(client):
    r = _login(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["token"].startswith("v1.")
    assert d["token_type"] == "bearer"
    assert d["user"]["user_id"] == "admin"
    assert d["user"]["is_admin"] is True


def test_token_from_login_actually_unlocks_a_business_route(client):
    """**这是本文件最关键的一条**：验证令牌不是装饰。

    只断言 ``/auth/me`` 返回 200 是不够的 —— 那里可能压根没校验。必须拿
    登录端点发的令牌去开一个受保护的业务路由，否则「登录返回垃圾字符串」
    也能全绿。
    """
    token = _login(client).json()["token"]

    assert client.get("/api/protected").status_code == 401
    ok = client.get("/api/protected", headers=_auth(token))
    assert ok.status_code == 200, ok.text
    assert ok.json()["secret"] == "settings-credentials-go-here"


def test_login_never_echoes_the_password_or_its_hash(client):
    """登录响应会进浏览器日志与 devtools —— 绝不能带任何口令材料。"""
    raw = _login(client).text
    assert ADMIN_PW not in raw
    assert "pbkdf2" not in raw
    assert "password" not in json.loads(raw)


def test_login_updates_last_seen(client):
    """登录要留下痕迹 —— 「这条记录为什么挂在 admin 名下」要能查。"""
    from pa_agent.storage.users import get_user

    before = get_user("admin")["last_seen"]
    assert _login(client).status_code == 200
    assert get_user("admin")["last_seen"] >= before


# ── 登录：真的登不上，且不泄露用户是否存在 ─────────────────────────────────────


def test_wrong_password_is_401(client):
    r = _login(client, password="not-the-password")
    assert r.status_code == 401
    assert "token" not in r.json()


def test_unknown_user_response_is_byte_identical_to_wrong_password(client):
    """**逐字节**相同，而不只是「都 401」。

    「用户不存在」与「口令错误」返回不同文案/不同状态码/多一个字段，
    都是用户名枚举接口。``authenticate`` 已把两者折叠成同一个 None，
    路由层绝不能把它拆开。
    """
    wrong_pw = _login(client, password="not-the-password")
    ghost = _login(client, user_id="no-such-user-xyz", password="not-the-password")

    assert ghost.status_code == wrong_pw.status_code == 401
    assert ghost.content == wrong_pw.content, (
        ghost.text, wrong_pw.text,
    )
    # 显式写出来，防止后人「顺手」给其中一支加个字段而测试还绿
    assert b"no-such-user-xyz" not in ghost.content


def test_empty_user_id_does_not_fall_back_to_admin(client):
    """``get_user("")`` 会因 ``user_id or ADMIN_USER_ID`` 返回 admin 行。

    若 ``authenticate`` 直接用 ``get_user(user_id)``，那么
    ``{"user_id": "", "password": <admin 口令>}`` 就会**登录成功并返回 admin**
    —— 前端用户名框空着、密码被管理器自动填上时，就会静默以 admin 身份登进去。
    """
    r = _login(client, user_id="", password=ADMIN_PW)
    assert r.status_code == 401, r.text
    assert r.content == _login(client, password="wrong").content


def test_user_without_password_cannot_log_in(users_db):
    """新建空库时 admin 的 ``password_hash`` 是空串 → 必须谁都登不进去。

    绝不能因为「散列为空」而放行；也绝不能因此静默绕过（那等于无鉴权）。
    """
    from pa_agent.storage.users import create_user, ensure_admin_user

    fresh = users_db
    ensure_admin_user()
    create_user("nopw", password="x")
    from pa_agent.storage.db import get_hub
    get_hub().execute("UPDATE users SET password_hash = '' WHERE user_id = 'nopw'")

    app_client = _make_client()
    assert app_client.post("/api/auth/login",
                           json={"user_id": "nopw", "password": ""}).status_code == 401
    assert app_client.post("/api/auth/login",
                           json={"user_id": "nopw", "password": "x"}).status_code == 401
    assert fresh is not None


def test_missing_body_fields_is_422_not_401(client):
    """请求畸形与凭据错误必须可区分 —— 422 里没有任何用户存在性信息。"""
    assert client.post("/api/auth/login", json={}).status_code == 422


# ── 免鉴权：登录端点自己 ───────────────────────────────────────────────────────


def test_login_endpoint_is_public_even_though_flag_is_off(client):
    """**死锁防线**：``/api/auth/login`` 不在白名单里就谁也登不进去。

    令牌只有登录端点能签发；把它收进鉴权范围 = 认证系统把自己锁死。
    这里显式检查中间件开着强制鉴权的前提下，登录仍然畅通。
    """
    import web.api.auth_ctx as ac

    assert ac.ALLOW_ANONYMOUS_ADMIN is False, "本用例的前提是强制鉴权已开启"
    r = client.post("/api/auth/login",
                    json={"user_id": "admin", "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    # 且登录端点**不接受**任何 Authorization 头也照样能过
    r2 = client.post("/api/auth/login",
                     json={"user_id": "admin", "password": ADMIN_PW},
                     headers={"Authorization": "Bearer total-garbage"})
    assert r2.status_code == 200, r2.text


def test_login_is_public_on_the_real_app(users_db):
    """挂在**真 app** 上也要免鉴权。

    单元用的最小 app 证明的是策略函数对；这一条证明的是
    ``web.server.app`` 真的挂了那个中间件、且白名单真的生效 —— 否则「压根没挂」
    这种失效在小 app 上永远测不出来。
    """
    import web.server as server

    c = TestClient(server.app)
    r = c.post("/api/auth/login", json={"user_id": "admin", "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    assert r.json()["token"]


def test_auth_middleware_is_mounted_on_real_app():
    """守卫：删掉 ``server.py`` 里那个 ``@app.middleware("http")`` 块必红。"""
    import web.server as server
    from web.api.auth_ctx import enforce_auth_middleware as impl

    dispatches = [m.kwargs.get("dispatch") for m in server.app.user_middleware]
    assert any(getattr(d, "__wrapped__", d) is impl or d is not None
               and getattr(d, "__name__", "") == impl.__name__
               for d in dispatches), (
        "enforce_auth_middleware 没挂在 web.server.app 上"
    )


def test_auth_router_paths_are_registered_on_real_app():
    """守卫：忘了 ``include_router`` 时，登录页会指向一个 404 的 URL。

    查 ``app.openapi()["paths"]`` 而不是 ``app.routes``：FastAPI 0.142 把
    ``include_router`` 的结果包成 ``_IncludedRouter``，不摊平进 ``routes``，
    按 ``routes`` 查会得到「全部端点都没注册」的假阴性。
    """
    import web.server as server

    paths = server.app.openapi()["paths"]
    for p in ("/api/auth/login", "/api/auth/logout",
              "/api/auth/me", "/api/auth/password"):
        assert p in paths, f"{p} 没注册（已注册：{sorted(paths)}）"


# ── /api/auth/me ──────────────────────────────────────────────────────────────


def test_me_reports_identity_and_permissions(client):
    token = _login(client).json()["token"]
    d = client.get("/api/auth/me", headers=_auth(token)).json()
    assert d["authenticated"] is True
    assert d["user_id"] == "admin"
    assert d["display_name"] == "管理员"
    assert d["role"] == "admin"
    assert d["is_admin"] is True
    # 前端据此决定「要不要显示登录框」：强制鉴权开着就是 True
    assert d["auth_required"] is True
    assert d["token_expires_at"] > 0


def test_me_without_token_is_401_and_does_not_leak_admin(client):
    """未认证时连 user_id 都不能给 —— 回落上下文的 user_id 恒为 admin。

    把 admin 这个名字回给未登录者，等于白送一个可用来枚举的用户名；
    我们费劲在登录失败上抹平的正是这类信息泄露。
    """
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"
    body = r.json()
    assert body["error"] == "authentication_required"
    assert body["login_url"] == "/api/auth/login"
    assert "admin" not in r.text
    assert "user_id" not in body
    assert "password" not in r.text


def test_me_with_garbage_token_is_401(client):
    r = client.get("/api/auth/me", headers=_auth("v1.aaa.bbb"))
    assert r.status_code == 401


def test_me_does_not_return_password_hash(client):
    token = _login(client).json()["token"]
    raw = client.get("/api/auth/me", headers=_auth(token)).text
    assert "pbkdf2" not in raw and "password_hash" not in raw


# ── 登出 ───────────────────────────────────────────────────────────────────────


def test_logout_revokes_that_token(client):
    """自签令牌没有服务端状态 ⇒ 登出必须自己把令牌记进吊销名单。"""
    token = _login(client).json()["token"]
    assert client.get("/api/auth/me", headers=_auth(token)).status_code == 200

    r = client.post("/api/auth/logout", headers=_auth(token))
    assert r.status_code == 200 and r.json()["ok"] is True

    assert client.get("/api/auth/me", headers=_auth(token)).status_code == 401
    assert client.get("/api/protected", headers=_auth(token)).status_code == 401


def test_logout_only_kills_the_given_token(client):
    """另一个标签页/另一台机器的令牌不能被这次登出连坐。"""
    t1 = _login(client).json()["token"]
    t2 = _login(client).json()["token"]
    assert t1 != t2
    client.post("/api/auth/logout", headers=_auth(t1))
    assert client.get("/api/auth/me", headers=_auth(t2)).status_code == 200


def test_logout_without_token_is_401(client):
    assert client.post("/api/auth/logout").status_code == 401


def test_revocation_is_in_process_only_and_documented_as_such():
    """护栏：吊销表**是**进程内的（重启即失效）。若将来改持久化，本条需重写。

    这不是缺陷断言，是把「登出到底保证了什么」钉在测试里，避免后人把它
    当成「令牌已彻底作废」而据此设计更敏感的流程。
    """
    from web.api import auth_ctx

    token = "v1.whatever.whatever"
    auth_ctx.revoke_token(token)
    assert auth_ctx.is_token_revoked(token) is True
    auth_ctx.reset_revocation_state_for_tests()
    assert auth_ctx.is_token_revoked(token) is False


# ── 改密 ───────────────────────────────────────────────────────────────────────


def test_change_password_requires_the_current_one(client):
    token = _login(client).json()["token"]
    r = client.post("/api/auth/password",
                    headers=_auth(token),
                    json={"current_password": "wrong", "new_password": "brand-new-pw"})
    assert r.status_code == 401
    # 关键：失败**没有**改动任何东西
    assert _login(client, password=ADMIN_PW).status_code == 200


def test_change_password_actually_changes_it(client):
    token = _login(client).json()["token"]
    r = client.post("/api/auth/password",
                    headers=_auth(token),
                    json={"current_password": ADMIN_PW, "new_password": "brand-new-pw"})
    assert r.status_code == 200, r.text
    assert _login(client, password=ADMIN_PW).status_code == 401
    assert _login(client, password="brand-new-pw").status_code == 200


def test_change_password_requires_a_token(client):
    r = client.post("/api/auth/password",
                    json={"current_password": ADMIN_PW, "new_password": "brand-new-pw"})
    assert r.status_code == 401


def test_change_password_rejects_a_trivially_short_new_password(client):
    """口令是本机唯一的人工兜底（无二次验证、无找回），强度下限不能没有。"""
    token = _login(client).json()["token"]
    r = client.post("/api/auth/password",
                    headers=_auth(token),
                    json={"current_password": ADMIN_PW, "new_password": "1234"})
    assert r.status_code == 422
    assert _login(client, password=ADMIN_PW).status_code == 200


# ── 强制鉴权开关本身 ───────────────────────────────────────────────────────────


def test_flag_is_flipped():
    """一行守卫：``ALLOW_ANONYMOUS_ADMIN`` 必须保持 ``False``。

    反向验证：翻回 True，本条与下面全部 401 用例同时变红。
    """
    from web.api import auth_ctx

    assert auth_ctx.ALLOW_ANONYMOUS_ADMIN is False


def test_protected_api_path_without_token_is_401(client):
    r = client.get("/api/protected")
    assert r.status_code == 401
    # 401 绝不能被缓存，否则「已过期」会被缓存命中，表现为刷新也进不去
    assert r.headers.get("Cache-Control") == "no-store"


def test_static_and_docs_are_open_without_a_token(client):
    """登录页本身必须拿得到：浏览器要先有 index.html 才谈得上渲染登录框。

    这一条在最小 app 上意义有限（没有静态挂载），真正的守住者见
    :func:`test_non_api_paths_bypass_auth_on_the_real_app`。
    """
    from web.api.auth_ctx import enforce_auth_middleware, is_public_api_path

    # 结构性的：非 /api 前缀一律放行，不依赖任何白名单条目
    assert not is_public_api_path("/")
    assert not is_public_api_path("/js/app.js")
    assert not is_public_api_path("/docs")
    assert enforce_auth_middleware is not None


def test_non_api_paths_bypass_auth_on_the_real_app(users_db):
    """/ 、/index.html、/docs、/openapi.json 无令牌也能拿到 —— 真 app 上验。"""
    import web.server as server

    c = TestClient(server.app)
    for path in ("/", "/index.html", "/docs", "/openapi.json"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"


def test_options_preflight_is_not_blocked(client):
    """预检按规范不带 ``Authorization``，所以拦它等于让分离式前端全线失败。

    这里断言的是**鉴权中间件没有拦**（405 来自路由：没挂 CORS 时 OPTIONS 无
    匹配路由），真正会回 200 的预检见下面那条。
    """
    r = client.options("/api/protected",
                       headers={"Origin": "http://sep.example",
                                "Access-Control-Request-Method": "GET"})
    assert r.status_code != 401


def test_cors_preflight_carries_authorization_header(monkeypatch, tmp_path):
    """``allow_headers`` 必须含 ``Authorization``。

    反向验证：从 ``server.py`` 的 ``_install_cors`` 里删掉 Authorization，
    或者干脆不调用 ``_install_cors()``，本条立刻变红（预检 405 / 头里没有
    authorization）。
    """
    monkeypatch.setenv("PA_AGENT_CORS_ORIGINS", "http://sep.example")
    import importlib

    import web.server as server

    importlib.reload(server)
    try:
        from fastapi.testclient import TestClient as TC

        c = TC(server.app)
        r = c.options("/api/settings", headers={
            "Origin": "http://sep.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        })
        assert r.status_code == 200, r.status_code
        allow = r.headers.get("access-control-allow-headers", "")
        assert "authorization" in allow.lower(), allow
        assert r.headers.get("access-control-allow-origin") == "http://sep.example"
    finally:
        monkeypatch.delenv("PA_AGENT_CORS_ORIGINS", raising=False)
        importlib.reload(server)          # 还原成默认（无 CORS）状态


def test_cors_error_response_is_readable_by_the_frontend(monkeypatch):
    """401 必须带 CORS 头。

    CORS 若挂在鉴权**里面**，「令牌过期」这个最需要前端处理的响应会没有
    ``Access-Control-Allow-Origin``，浏览器把它报成 CORS 错误 —— 于是
    「静默跳登录页」这条最关键的路径反而拿不到任何信息。
    """
    monkeypatch.setenv("PA_AGENT_CORS_ORIGINS", "http://sep.example")
    import importlib

    import web.server as server

    importlib.reload(server)
    try:
        from fastapi.testclient import TestClient as TC

        c = TC(server.app)
        r = c.get("/api/settings", headers={"Origin": "http://sep.example"})
        assert r.status_code == 401
        assert r.headers.get("access-control-allow-origin") == "http://sep.example"
    finally:
        monkeypatch.delenv("PA_AGENT_CORS_ORIGINS", raising=False)
        importlib.reload(server)


# ── 健康探针 ───────────────────────────────────────────────────────────────────


def test_health_stays_public(users_db):
    """liveness 探针默认不带令牌 ⇒ 收进鉴权范围会让编排系统反复重启容器。"""
    import web.server as server

    assert TestClient(server.app).get("/api/health").status_code == 200


def test_health_is_degraded_for_anonymous_callers(users_db):
    """放行但**降级**：未认证时不得吐出用户名与用户清单。

    否则就成了「不用登录就能读出用户名」的旁路，而我们刚在登录失败上把这类
    信息抹平。
    """
    import web.server as server

    d = TestClient(server.app).get("/api/health").json()
    assert d["auth"]["required"] is True
    assert d["auth"]["authenticated"] is False
    assert "default_user" not in d["storage"]
    assert "users" not in d["storage"]
    assert "admin" not in json.dumps(d), d


def test_health_is_full_for_authenticated_callers(users_db):
    token = _login_on_real_app().json()["token"]
    import web.server as server

    d = TestClient(server.app).get("/api/health", headers=_auth(token)).json()
    assert d["auth"]["authenticated"] is True
    assert d["storage"]["default_user"] == "admin"
    assert "admin" in d["storage"]["users"]


def test_health_check_is_protected(users_db):
    """``/api/health/check`` 是**诊断**不是探针：它真去 ping 模型 API 与数据源，
    能看出上游地址与延迟 ⇒ 必须登录后才能调。"""
    import web.server as server

    assert TestClient(server.app).get("/api/health/check").status_code == 401


# ── TTL ───────────────────────────────────────────────────────────────────────


def test_login_token_ttl_defaults_to_seven_days(client):
    """不是占位层那个 30 天。令牌落在 localStorage，拿到它 = 拿到本机全部权限。"""
    d = _login(client).json()
    assert d["expires_in"] == 7 * 24 * 3600
    assert d["expires_at"] - d["expires_in"] == pytest.approx(
        d["expires_at"] - d["expires_in"], rel=1e9)


def test_login_token_ttl_env_override(monkeypatch, client):
    monkeypatch.setenv("PA_AGENT_TOKEN_TTL_S", "3600")
    assert _login(client).json()["expires_in"] == 3600


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "99999999999"])
def test_absurd_ttl_env_falls_back_to_default(monkeypatch, client, raw):
    """写错一个值不该造出「登录即过期」或「永不过期」。"""
    monkeypatch.setenv("PA_AGENT_TOKEN_TTL_S", raw)
    assert _login(client).json()["expires_in"] == 7 * 24 * 3600


def test_issued_token_actually_expires_at_the_advertised_time(client, monkeypatch):
    """宣传的 expires_at 与令牌内 exp 必须一致，否则「有效期」是假的。"""
    from pa_agent.storage.auth import verify_token

    d = _login(client).json()
    claims = verify_token(d["token"])
    assert claims is not None
    assert claims.expires_at == pytest.approx(d["expires_at"], abs=1.0)


# ── 存储层被加固的两处 ─────────────────────────────────────────────────────────


def test_authenticate_empty_user_id_never_logs_in_anyone(users_db):
    """``users.authenticate("", pw)`` 必须恒为 None —— 不能回落 admin。"""
    from pa_agent.storage.users import authenticate

    assert authenticate("", ADMIN_PW) is None
    assert authenticate("", "") is None


def test_unknown_user_still_pays_the_pbkdf2_cost(users_db, monkeypatch):
    """抹平时序差异这条承诺必须真的兑现。

    ``verify_password(pw, "")`` 会在 ``.split("$", 3)`` 处就 ValueError 返回
    False —— **一次 PBKDF2 都不跑**。于是「用户不存在 <1ms」而「口令错 240k
    轮」，反而造出一个比原文更明显的计时侧信道（且对没有账号的人同样有效）。
    """
    import pa_agent.storage.auth as auth_mod
    import pa_agent.storage.users as users_mod

    seen: list[str] = []
    real = auth_mod.verify_password

    def spy(password, stored):
        seen.append(stored)
        return real(password, stored)

    monkeypatch.setattr(auth_mod, "verify_password", spy)

    users_mod.authenticate("ghost-user-xyz", "some-password")
    assert seen, "未知用户根本没有走 verify_password"
    stub = seen[-1]
    # 必须是**格式完好**的散列 —— 只有这样 verify_password 才会真的跑 PBKDF2
    assert stub.count("$") == 3, stub
    assert stub.startswith("pbkdf2_sha256$")
    assert users_mod.authenticate("ghost-user-xyz", "some-password") is None


# ── 工具 ───────────────────────────────────────────────────────────────────────


def _make_client():
    """临时再造一个 client（用于「换一个用户」的用例）。"""
    from web.api import routes_auth
    from web.api.auth_ctx import enforce_auth_middleware

    app = FastAPI()
    app.middleware("http")(enforce_auth_middleware)
    app.include_router(routes_auth.router, prefix="/api")
    return TestClient(app)


def _login_on_real_app():
    import web.server as server

    return TestClient(server.app).post(
        "/api/auth/login", json={"user_id": "admin", "password": ADMIN_PW})

# ── 改密作废该用户全部令牌 ────────────────────────────────────────────────


def _fresh_user(name: str, pw: str = "Seed-Pw-12345") -> str:
    """建一个专用用户。

    **不要拿 admin 做改密用例**：改密是就地改 users 行，而 client fixture
    在整份文件里共享同一个库 —— 前一个用例把 admin 口令改了，后一个用例
    拿 ADMIN_PW 去登录就会「莫名其妙失败」，且失败原因与被测逻辑无关。
    这是本项目反复栽的「测试之间互相污染」那一类。
    """
    from pa_agent.storage.users import create_user

    create_user(name, display_name=name, password=pw)
    return pw


# 改密的受验端点用 /api/auth/me —— 它必然挂在 client 上，而 /api/settings
# 未必（client 是最小 app）。曾用后者，四条用例全拿到 404。


def test_password_change_revokes_every_token_of_that_user(client):
    """改密是账号级动作：该用户所有设备上换到的令牌都要立刻失效。

    修之前只有逐令牌吊销表，于是「改了密码」在最需要它生效的场景
    （令牌已被别人拿走）里恰恰不生效 —— 那把锁只挡住了还没偷到令牌的人。
    """
    from pa_agent.storage.auth import issue_token

    pw = _fresh_user("revoker1")
    tokens = [issue_token("revoker1", ttl_s=3600) for _ in range(3)]
    for t in tokens:
        assert client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {t}"}
        ).status_code == 200, "前置条件：三枚令牌都应可用"

    r = client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {tokens[1]}"},
        json={"current_password": pw, "new_password": "Brand-New-Pw-42"},
    )
    assert r.status_code == 200
    for t in tokens:
        assert client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {t}"}
        ).status_code == 401, "改密后仍有旧令牌可用"


def test_token_issued_after_password_change_stays_valid(client):
    """水位线之后签发的令牌不该被自己那次改密杀掉。"""
    from pa_agent.storage.auth import issue_token

    pw = _fresh_user("revoker2")
    before = issue_token("revoker2", ttl_s=3600)
    assert client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {before}"},
        json={"current_password": pw, "new_password": "Another-Pw-77"},
    ).status_code == 200
    after = issue_token("revoker2", ttl_s=3600)
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {after}"}
    ).status_code == 200, "改密之后签的令牌被误杀"


def test_password_change_does_not_revoke_other_users(client):
    """改密只影响本人。别人的令牌与它无关。"""
    from pa_agent.storage.auth import issue_token

    pw = _fresh_user("revoker3")
    bystander = issue_token("bystander-x", ttl_s=3600)
    actor = issue_token("revoker3", ttl_s=3600)
    assert client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {actor}"},
        json={"current_password": pw, "new_password": "Isolated-Pw-9"},
    ).status_code == 200
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {bystander}"}
    ).status_code == 200, "改密误伤了别的用户"


def test_failed_password_change_keeps_tokens_alive(client):
    """当前口令给错时**不得**作废任何令牌 —— 否则成了 DoS。"""
    from pa_agent.storage.auth import issue_token

    pw = _fresh_user("revoker4")
    t = issue_token("revoker4", ttl_s=3600)
    r = client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {t}"},
        json={"current_password": "WRONG", "new_password": "Whatever-123"},
    )
    assert r.status_code == 401
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {t}"}
    ).status_code == 200, "改密失败却把用户踢出了会话"


def test_logout_stays_token_scoped_not_user_scoped(client):
    """登出只作废**本枚**令牌，同账号的另一个浏览器必须照常可用。

    这与改密的语义正好相反：登出是设备级动作，改密是账号级动作。
    吊销表键本来就是令牌哈希（`_token_digest`），这里锁死它不被改成按用户。

    令牌走 ``_login()`` 拿而不是 ``issue_token()`` 直接签：后者绕过了
    签发路径，可能与校验路径取到不同的密钥，那测的是「两枚不同密钥的
    令牌」而不是「同一账号的两个会话」。
    """
    a = _login(client).json()["token"]
    b = _login(client).json()["token"]
    assert a != b, "两次登录应拿到不同令牌"

    assert client.post("/api/auth/logout", headers=_auth(a)).status_code == 200
    assert client.get("/api/auth/me", headers=_auth(a)).status_code == 401
    assert client.get("/api/auth/me", headers=_auth(b)).status_code == 200, (
        "登出一个浏览器却把同账号的另一个也踢了"
    )
