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
        "",
        "   ",
        "short",
        "sk-123",
    ],
)
def test_masked_or_placeholder_values_are_detected(value):
    assert _is_masked_key(value) is True


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
        assert load_settings(p).provider.api_key == new_key
    finally:
        routes_settings.SETTINGS_JSON_PATH = original