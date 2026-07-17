# -*- coding: utf-8 -*-
"""Unit tests for web/api/routes_settings.py — settings CRUD + feishu test endpoint.

覆盖：
- GET /api/settings 返回脱敏 api_key，飞书字段明文
- PUT /api/settings 合并 section 到当前 settings 并落盘
- POST /api/feishu/test 成功/失败/空 webhook 路径

mock 策略：用真实 FastAPI app + 真实 Settings 对象作为 ctx.settings，
patch 掉落盘/AI client/飞书 HTTP 调用，避免真实副作用。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pa_agent.config.settings import Settings, AIProviderSettings, FeishuSettings
from web.api.routes_settings import router as settings_router


def _make_ctx(settings: Settings | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.settings = settings or Settings(
        provider=AIProviderSettings(api_key="sk-1234567890abcdef"),
        feishu=FeishuSettings(webhook_url="https://example.com/hook", secret="sec"),
    )
    ctx.logger = MagicMock()
    return ctx


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(settings_router, prefix="/api")

    @app.on_event("startup")
    async def _set_ctx():
        app.state.ctx = _make_ctx()

    with TestClient(app) as c:
        yield c


# ── GET /api/settings ─────────────────────────────────────────────────────────


def test_get_settings_masks_long_api_key(client):
    """长 api_key 应脱敏为 前四位****后四位。"""
    s = Settings(provider=AIProviderSettings(api_key="sk-1234567890abcdef"))
    with patch("web.api.routes_settings.load_settings", return_value=s):
        resp = client.get("/api/settings")

    assert resp.status_code == 200
    pk = resp.json()["provider"]
    assert pk["api_key"] == "sk-1****cdef"
    assert pk["api_key"] != "sk-1234567890abcdef"


def test_get_settings_masks_short_api_key(client):
    """短于等于 8 位的 api_key 全部显示为 ****。"""
    s = Settings(provider=AIProviderSettings(api_key="sk-ab"))
    with patch("web.api.routes_settings.load_settings", return_value=s):
        resp = client.get("/api/settings")

    assert resp.status_code == 200
    assert resp.json()["provider"]["api_key"] == "****"


def test_get_settings_returns_feishu_fields(client):
    """飞书 secret/app_secret 应明文返回以适配表单回填。"""
    s = Settings(
        feishu=FeishuSettings(
            webhook_url="https://example.com/hook",
            secret="my-secret",
            app_id="cli_xxx",
            app_secret="app-secret-val",
        )
    )
    with patch("web.api.routes_settings.load_settings", return_value=s):
        resp = client.get("/api/settings")

    feishu = resp.json()["feishu"]
    assert feishu["secret"] == "my-secret"
    assert feishu["app_secret"] == "app-secret-val"
    assert feishu["webhook_url"] == "https://example.com/hook"


# ── PUT /api/settings ─────────────────────────────────────────────────────────


def test_put_settings_merges_sections(client):
    """PUT 合并 provider / feishu section 并返回 saved。"""
    # 替换 ctx.settings 为可变真实 Settings
    real = Settings()
    client.app.state.ctx.settings = real

    with patch("web.api.routes_settings.save_settings") as mock_save, \
         patch("pa_agent.util.logging.update_api_key"), \
         patch("pa_agent.ai.client_factory.create_ai_client") as mock_create:
        mock_create.return_value = MagicMock(name="new_client")
        body = {"provider": {"model": "gpt-4o"}, "feishu": {"enabled": False}}
        resp = client.put("/api/settings", json=body)

    assert resp.status_code == 200
    assert resp.json()["status"] == "saved"
    assert real.provider.model == "gpt-4o"
    assert real.feishu.enabled is False
    mock_save.assert_called_once()
    mock_create.assert_called_once()
    # ctx.client 应被重建
    assert client.app.state.ctx.client is mock_create.return_value


def test_put_settings_ignores_unknown_sections(client):
    """未知 section / 未知字段不影响已有设置。"""
    real = Settings()
    original_model = real.provider.model
    client.app.state.ctx.settings = real

    with patch("web.api.routes_settings.save_settings"), \
         patch("pa_agent.util.logging.update_api_key"), \
         patch("pa_agent.ai.client_factory.create_ai_client"):
        body = {"unknown_section": {"foo": "bar"}, "provider": {"nonexistent_field": 1}}
        resp = client.put("/api/settings", json=body)

    assert resp.status_code == 200
    assert real.provider.model == original_model


def test_put_settings_partial_merge_keeps_other_fields(client):
    """只更新 provider.model 时，其他 provider 字段保持不变。"""
    real = Settings(provider=AIProviderSettings(model="orig-model", thinking=True))
    client.app.state.ctx.settings = real

    with patch("web.api.routes_settings.save_settings"), \
         patch("pa_agent.util.logging.update_api_key"), \
         patch("pa_agent.ai.client_factory.create_ai_client"):
        resp = client.put("/api/settings", json={"provider": {"model": "new-model"}})

    assert resp.status_code == 200
    assert real.provider.model == "new-model"
    assert real.provider.thinking is True  # 未被覆盖


# ── POST /api/feishu/test ─────────────────────────────────────────────────────


def test_feishu_test_success_with_secret(client):
    """带 secret 的成功路径：payload 包含 timestamp + sign。"""
    with patch("web.api.routes_settings.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "StatusCode": 0}
        mock_post.return_value = mock_resp
        body = {"webhook_url": "https://example.com/hook", "secret": "sec"}
        resp = client.post("/api/feishu/test", json=body)

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload["msg_type"] == "text"
    assert "timestamp" in payload
    assert "sign" in payload


def test_feishu_test_success_without_secret(client):
    """无 secret 时 payload 不带签名字段。"""
    with patch("web.api.routes_settings.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "StatusCode": 0}
        mock_post.return_value = mock_resp
        body = {"webhook_url": "https://example.com/hook"}
        resp = client.post("/api/feishu/test", json=body)

    assert resp.status_code == 200
    payload = mock_post.call_args.kwargs["json"]
    assert "timestamp" not in payload
    assert "sign" not in payload


def test_feishu_test_empty_webhook_returns_400(client):
    """空 webhook 返回 400。"""
    resp = client.post("/api/feishu/test", json={"webhook_url": ""})
    assert resp.status_code == 400


def test_feishu_test_error_code_returns_502_with_hint(client):
    """飞书返回错误 code 时返回 502 并附中文 hint。"""
    with patch("web.api.routes_settings.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 19022, "msg": "secret mismatch"}
        mock_post.return_value = mock_resp
        body = {"webhook_url": "https://example.com/hook", "secret": "wrong"}
        resp = client.post("/api/feishu/test", json=body)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "19022" in detail
    assert "secret" in detail  # hint 含 secret 关键字


def test_feishu_test_request_exception_returns_502(client):
    """requests.post 抛异常时返回 502。"""
    with patch("web.api.routes_settings.requests.post", side_effect=RuntimeError("network down")):
        body = {"webhook_url": "https://example.com/hook"}
        resp = client.post("/api/feishu/test", json=body)

    assert resp.status_code == 502
    assert "network down" in resp.json()["detail"]


def test_feishu_test_non_json_response_returns_502(client):
    """非 JSON 响应返回 502。"""
    with patch("web.api.routes_settings.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("not json")
        mock_resp.text = "<html>error</html>"
        mock_post.return_value = mock_resp
        body = {"webhook_url": "https://example.com/hook"}
        resp = client.post("/api/feishu/test", json=body)

    assert resp.status_code == 502
    assert "非 JSON" in resp.json()["detail"]
