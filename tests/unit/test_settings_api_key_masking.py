"""Regression tests for the masked-API-key round-trip in /api/settings.

``GET /api/settings`` masks the provider key; the settings form refills from
that masked value and posts it back on save. Before the fix this overwrote the
real key on disk with the placeholder, permanently breaking every model call.
"""
from __future__ import annotations

import pytest

from web.api.routes_settings import _is_masked_key


@pytest.mark.parametrize(
    "value",
    [
        "****",
        "abcd****wxyz",
        "sk-a****bcd",
        "  ****  ",
    ],
)
def test_masked_or_placeholder_values_are_detected(value):
    assert _is_masked_key(value) is True


@pytest.mark.parametrize(
    "value",
    ["", "   ", "short", "sk-123", "abcdefgh", "abcdefghijkl"],
)
def test_real_short_values_are_not_placeholders(value):
    """Short-but-real secrets must survive a form save.

    Treating anything under 16 chars as a placeholder silently dropped short
    Feishu/Tushare credentials when the operator saved unrelated settings.
    """
    assert _is_masked_key(value) is False


def test_empty_provider_key_means_keep_existing():
    """An empty api_key submission means "form had nothing", not "wipe my key"."""
    from web.api.routes_settings import _should_keep_existing

    assert _should_keep_existing("provider", "api_key", "") is True
    assert _should_keep_existing("provider", "api_key", "   ") is True
    assert _should_keep_existing("provider", "api_key", "****abcd") is True
    assert _should_keep_existing("provider", "api_key", "sk-real-key-0123456789") is False


def test_empty_other_secret_means_clear_it():
    """For non-key fields an empty submission is a deliberate clear."""
    from web.api.routes_settings import _should_keep_existing

    assert _should_keep_existing("tushare", "token", "") is False
    assert _should_keep_existing("feishu", "secret", "****wxyz") is True


@pytest.mark.parametrize(
    "value",
    [
        "sk-abcdefghijklmnopqrstuvwxyz0123456789",
        "cr_abcdefghijklmnopqrstuvwxyz0123456789AB",
        "a" * 40,
    ],
)
def test_real_keys_are_not_treated_as_masked(value):
    assert _is_masked_key(value) is False


def test_non_string_is_not_masked_but_is_not_a_key():
    """Non-strings fall through as 'not masked' so the caller's own validation wins."""
    assert _is_masked_key(None) is False
    assert _is_masked_key(123) is False


def test_put_settings_preserves_real_key(tmp_path):
    """End-to-end: POSTing the masked value must not clobber the stored key."""
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pa_agent.config.settings import Settings, save_settings
    from web.api import routes_settings

    real_key = "sk-realkey0123456789abcdefghij"
    s = Settings()
    s.provider.api_key = real_key
    p = tmp_path / "settings.json"
    save_settings(s, p)

    app = FastAPI()
    app.include_router(routes_settings.router, prefix="/api")
    app.state.ctx = type("Ctx", (), {"settings": s, "client": None, "logger": None})()

    # Point the router at our temp file
    original = routes_settings.SETTINGS_JSON_PATH
    routes_settings.SETTINGS_JSON_PATH = p
    try:
        client = TestClient(app)
        masked = client.get("/api/settings").json()["provider"]["api_key"]
        assert masked != real_key and "****" in masked

        # What the browser does: refill the form, then save it back verbatim.
        r = client.put("/api/settings", json={"provider": {"api_key": masked}})
        assert r.status_code == 200
        assert r.json()["api_key_masked_ignored"] is True

        from pa_agent.config.settings import load_settings

        assert load_settings(p).provider.api_key == real_key, "real key must survive"
    finally:
        routes_settings.SETTINGS_JSON_PATH = original


def test_put_settings_accepts_a_genuinely_new_key(tmp_path):
    """A real, user-entered key must still be persisted."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pa_agent.config.settings import Settings, load_settings, save_settings
    from web.api import routes_settings

    s = Settings()
    s.provider.api_key = "sk-oldkey0123456789abcdefgh"
    p = tmp_path / "settings.json"
    save_settings(s, p)

    app = FastAPI()
    app.include_router(routes_settings.router, prefix="/api")
    app.state.ctx = type("Ctx", (), {"settings": s, "client": None, "logger": None})()

    original = routes_settings.SETTINGS_JSON_PATH
    routes_settings.SETTINGS_JSON_PATH = p
    try:
        client = TestClient(app)
        new_key = "sk-brandnewkey9876543210abcd"
        r = client.put("/api/settings", json={"provider": {"api_key": new_key}})
        assert r.status_code == 200
        assert r.json()["api_key_masked_ignored"] is False
        # PUT 不再回写 settings.json（那是出厂配置，不是当前状态）。
        # 要验的是新 key 真的生效了 —— 断言落点改为 ctx.settings。
        assert client.app.state.ctx.settings.provider.api_key == new_key
    finally:
        routes_settings.SETTINGS_JSON_PATH = original

# ── every credential is masked, and survives a form save ─────────────────────


def _populated_settings():
    from pa_agent.config.settings import Settings

    s = Settings()
    s.provider.api_key = "sk-provider-key-0123456789abcdef"
    s.feishu.webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/FEISHU-HOOK-TOKEN-123456"
    s.feishu.secret = "feishu-sign-secret-abcdef"
    s.feishu.app_secret = "feishu-app-secret-abcdef"
    s.pushplus.token = "pushplus-token-abcdef1234"
    s.tushare.token = "tushare-token-abcdef1234"
    s.tradingview.session_id = "tv-session-abcdef1234"
    s.tradingview.password = "tv-password-abcdef1234"
    return s


def test_get_settings_masks_every_credential(tmp_path):
    """No credential may leave the server in plaintext."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pa_agent.config.settings import save_settings
    from web.api import routes_settings

    s = _populated_settings()
    p = tmp_path / "settings.json"
    save_settings(s, p)

    app = FastAPI()
    app.include_router(routes_settings.router, prefix="/api")
    app.state.ctx = type("Ctx", (), {"settings": s, "client": None, "logger": None})()

    original = routes_settings.SETTINGS_JSON_PATH
    routes_settings.SETTINGS_JSON_PATH = p
    try:
        client = TestClient(app)
        body = client.get("/api/settings").text
    finally:
        routes_settings.SETTINGS_JSON_PATH = original

    secrets = [
        s.provider.api_key,
        s.feishu.webhook_url,
        s.feishu.secret,
        s.feishu.app_secret,
        s.pushplus.token,
        s.tushare.token,
        s.tradingview.session_id,
        s.tradingview.password,
    ]
    for value in secrets:
        assert value not in body, f"LEAKED in GET /api/settings: {value[:12]}..."
    assert "****" in body


def test_full_form_save_preserves_all_credentials(tmp_path):
    """Refill the form from GET, save it back verbatim — nothing may be lost."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pa_agent.config.settings import load_settings, save_settings
    from web.api import routes_settings

    s = _populated_settings()
    p = tmp_path / "settings.json"
    save_settings(s, p)

    app = FastAPI()
    app.include_router(routes_settings.router, prefix="/api")
    app.state.ctx = type("Ctx", (), {"settings": s, "client": None, "logger": None})()

    original = routes_settings.SETTINGS_JSON_PATH
    routes_settings.SETTINGS_JSON_PATH = p
    try:
        client = TestClient(app)
        form = client.get("/api/settings").json()
        # Browser behaviour: post the whole form back as received.
        r = client.put("/api/settings", json={
            "provider": form["provider"],
            "feishu": form["feishu"],
            "pushplus": form["pushplus"],
            "tushare": form["tushare"],
            "tradingview": form["tradingview"],
        })
        assert r.status_code == 200
        assert r.json()["api_key_masked_ignored"] is True
        saved = load_settings(p)
    finally:
        routes_settings.SETTINGS_JSON_PATH = original

    assert saved.provider.api_key == s.provider.api_key
    assert saved.feishu.webhook_url == s.feishu.webhook_url
    assert saved.feishu.secret == s.feishu.secret
    assert saved.feishu.app_secret == s.feishu.app_secret
    assert saved.pushplus.token == s.pushplus.token
    assert saved.tushare.token == s.tushare.token
    assert saved.tradingview.session_id == s.tradingview.session_id
    assert saved.tradingview.password == s.tradingview.password


def test_user_can_still_set_and_clear_secrets(tmp_path):
    """Masking must not make credentials unwritable."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pa_agent.config.settings import Settings, load_settings, save_settings
    from web.api import routes_settings

    s = Settings()
    p = tmp_path / "settings.json"
    save_settings(s, p)

    app = FastAPI()
    app.include_router(routes_settings.router, prefix="/api")
    app.state.ctx = type("Ctx", (), {"settings": s, "client": None, "logger": None})()

    original = routes_settings.SETTINGS_JSON_PATH
    routes_settings.SETTINGS_JSON_PATH = p
    try:
        client = TestClient(app)
        client.put("/api/settings", json={"tushare": {"token": "new-short-tok"}})
        assert client.app.state.ctx.settings.tushare.token == "new-short-tok"

        client.put("/api/settings", json={"tushare": {"token": ""}})
        assert client.app.state.ctx.settings.tushare.token == ""
    finally:
        routes_settings.SETTINGS_JSON_PATH = original
