"""REST routes for settings CRUD."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import asyncio

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import load_settings, save_settings
from pa_agent.util.logging import register_settings_secrets

router = APIRouter(tags=["settings"])


@router.get("/settings")
async def get_settings(request: Request):
    """Return current settings with API key masked."""
    ctx = request.app.state.ctx
    settings = await asyncio.to_thread(load_settings, SETTINGS_JSON_PATH)
    ctx.settings = settings  # sync live reference
    # 每次载入都刷新脱敏注册表：换密钥后旧值失效、新值生效，
    # 否则新密钥会以明文写进 records/pending/*.json。
    register_settings_secrets(settings)
    d = settings.model_dump()
    pk = d.get("provider", {})
    if pk.get("api_key"):
        pk["api_key"] = pk["api_key"][:4] + "****" + pk["api_key"][-4:] if len(pk["api_key"]) > 8 else "****"
        d["provider"] = pk
    # 飞书 secret/app_secret 保持明文返回以适配表单回填（与 PyQt6 行为一致）
    # 禁止缓存：防止前端读到旧 bar_count（TODO P0.2）
    return JSONResponse(content=d, headers={"Cache-Control": "no-store"})


def _is_masked_key(value) -> bool:
    """True when *value* is a masked/placeholder API key rather than a real one.

    ``GET /api/settings`` deliberately masks the provider key before returning it
    (``abcd****wxyz``, or ``****`` for short keys). The settings form refills
    itself from that response and posts the value straight back on save, so
    without this guard a single "save settings" click overwrites the real key on
    disk with the placeholder and every later model call fails with 401.
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return True
    if v == "****" or "****" in v:
        return True
    # Cheap structural guards: real keys are long and hex/base64-ish.
    return len(v) < 16


@router.put("/settings")
async def put_settings(request: Request, body: dict):
    """Merge *body* into current settings and save to disk."""
    ctx = request.app.state.ctx
    current = ctx.settings
    masked_key_dropped = False
    for section in ("provider", "prompt", "validation", "general", "feishu", "tushare", "pushplus", "tradingview"):
        if section in body and isinstance(body[section], dict):
            target = getattr(current, section, None)
            if target is not None:
                for k, v in body[section].items():
                    if hasattr(target, k):
                        # Never let a masked placeholder overwrite the stored key.
                        if section == "provider" and k == "api_key" and _is_masked_key(v):
                            masked_key_dropped = True
                            continue
                        setattr(target, k, v)
    await asyncio.to_thread(save_settings, current, SETTINGS_JSON_PATH)

    from pa_agent.util.logging import register_settings_secrets, update_api_key
    update_api_key(current.provider.api_key)
    # 注册全部凭据（飞书 webhook/secret/app_secret、PushPlus/Tushare token、
    # TradingView 凭证）到脱敏注册表，避免它们经由 requests 异常串进日志。
    register_settings_secrets(current)

    # Rebuild AI client if provider changed (may probe the provider; offload it)
    from pa_agent.ai.client_factory import create_ai_client
    ctx.client = await asyncio.to_thread(
        create_ai_client, current.provider, logger_=ctx.logger
    )

    return {"status": "saved", "api_key_masked_ignored": masked_key_dropped}


_FEISHU_ERR_HINT = {
    19021: "IP 不在白名单（机器人已被禁用）",
    19022: "secret 与机器人配置不匹配",
    19024: "Webhook 已失效或被删除",
}


@router.post("/feishu/test")
async def feishu_test(request: Request, body: dict):
    """发送一条飞书测试消息，验证 webhook 与 secret 配置是否有效。

    Body 字段：webhook_url / secret（与 PUT /api/settings.feishu 一致）。
    """
    webhook = (body.get("webhook_url") or "").strip()
    secret = (body.get("secret") or "").strip()
    if not webhook:
        raise HTTPException(status_code=400, detail="webhook_url 不能为空")

    payload = {
        "msg_type": "text",
        "content": {"text": "PA Agent Web 飞书通知测试"},
    }
    if secret:
        ts = str(int(time.time()))
        string_to_sign = f"{ts}\n{secret}"
        sign = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        payload["timestamp"] = ts
        payload["sign"] = sign

    try:
        # Offloaded: this is an outbound HTTPS POST with a 10s timeout. Calling it
        # inline would block the whole event loop for up to 10 seconds, freezing
        # every SSE stream and every other in-flight request.
        resp = await asyncio.to_thread(
            requests.post, webhook, json=payload, timeout=10
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"请求失败: {exc}")

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"非 JSON 响应: {resp.text[:200]}")

    code = data.get("code", 0)
    status_code = data.get("StatusCode", 0)
    if code == 0 and status_code == 0:
        return {"status": "ok", "raw": data}

    hint = _FEISHU_ERR_HINT.get(code) or _FEISHU_ERR_HINT.get(status_code)
    msg = f"飞书返回错误 code={code} StatusCode={status_code} msg={data.get('msg', '')}"
    if hint:
        msg += f"（{hint}）"
    raise HTTPException(status_code=502, detail=msg)
