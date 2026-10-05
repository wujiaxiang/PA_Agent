"""REST routes for settings CRUD."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import asyncio

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from fastapi.responses import JSONResponse

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import load_settings, save_settings
from pa_agent.util.logging import register_settings_secrets

logger = logging.getLogger("pa_agent.web.settings")

router = APIRouter(tags=["settings"])


#: Credential fields masked in ``GET /api/settings``.
#:
#: Previously only ``provider.api_key`` was masked, so Feishu ``secret`` /
#: ``app_secret`` / ``webhook_url``, the PushPlus token, the Tushare token and the
#: TradingView password/session were returned in plaintext. Combined with
#: ``allow_origins=["*"]`` and no authentication, any web page the user visited
#: could read every one of them cross-origin.
#:
#: The settings form refills from this response and posts the value straight back,
#: so :func:`_is_masked_key` + the ``_SECRET_FIELDS`` guard in ``put_settings``
#: ensure a masked placeholder is never written back over the real value.
_SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("provider", "api_key"),
    ("feishu", "secret"),
    ("feishu", "app_secret"),
    ("feishu", "webhook_url"),
    ("pushplus", "token"),
    ("tushare", "token"),
    ("tradingview", "password"),
    ("tradingview", "session_id"),
)


def _mask_placeholder(value: str) -> str:
    """Render *value* as a non-reversible placeholder that round-trips safely."""
    if not isinstance(value, str) or not value.strip():
        return ""
    v = value.strip()
    if len(v) <= 8:
        return "****"
    return f"{v[:4]}****{v[-4:]}"


@router.get("/settings")
async def get_settings(request: Request):
    """Return current settings with every credential masked."""
    ctx = request.app.state.ctx
    settings = await asyncio.to_thread(load_settings, SETTINGS_JSON_PATH)
    ctx.settings = settings  # sync live reference
    # 每次载入都刷新脱敏注册表：换密钥后旧值失效、新值生效，
    # 否则新密钥会以明文写进 records/pending/*.json。
    register_settings_secrets(settings)
    d = settings.model_dump()
    for section, field in _SECRET_FIELDS:
        bucket = d.get(section)
        if isinstance(bucket, dict) and bucket.get(field):
            bucket[field] = _mask_placeholder(bucket[field])
    # 禁止缓存：防止前端读到旧 bar_count（TODO P0.2）
    return JSONResponse(content=d, headers={"Cache-Control": "no-store"})


def _is_masked_key(value) -> bool:
    """True when *value* is a masked placeholder emitted by ``GET /api/settings``.

    The GET handler renders every credential in :data:`_SECRET_FIELDS` through
    :func:`_mask_placeholder`, which always contains the ``****`` marker. The
    settings form refills from that response and posts the value straight back on
    save, so without this guard one "save settings" click overwrites the real
    credential on disk with the placeholder and every later call fails.
    """
    return isinstance(value, str) and "****" in value


def _should_keep_existing(section: str, field: str, value) -> bool:
    """Whether a submitted value must be ignored to protect the stored one."""
    if _is_masked_key(value):
        return True
    # For the provider key an empty submission means "form had nothing", not
    # "please wipe my key" — the form only ever sends back what it received.
    if (section, field) == ("provider", "api_key"):
        if isinstance(value, str) and not value.strip():
            return True
    return False


@router.put("/settings")
async def put_settings(request: Request, body: dict):
    """Merge *body* into current settings and save.

    赋值走 Pydantic 的 ``validate_assignment=True``：越界/类型不合法的值会被拒，
    而不是像 2026-10-05 那次一样被静默写入、把 base_url 与 api_key 冲成默认值。
    任一字段校验失败即整体回滚（见下方 staged copy），返回 400 并指明字段。
    """
    ctx = request.app.state.ctx
    current = ctx.settings
    masked_key_dropped = False
    # 先快照原值再统一提交：整段赋值是一体的，某个字段校验失败时无法只回滚
    # 那一个，必须靠快照还原 —— 否则会留下半套配置，比全盘拒绝更危险。
    staged: list[tuple[object, str, object, object]] = []
    for section in ("provider", "prompt", "validation", "general", "feishu", "tushare", "pushplus", "tradingview"):
        if section in body and isinstance(body[section], dict):
            target = getattr(current, section, None)
            if target is not None:
                for k, v in body[section].items():
                    if hasattr(target, k):
                        # Never let a masked placeholder overwrite a stored
                        # credential — applies to every field in _SECRET_FIELDS,
                        # not just the provider key.
                        if (section, k) in _SECRET_FIELDS and _should_keep_existing(section, k, v):
                            masked_key_dropped = True
                            continue
                        staged.append((target, k, getattr(target, k), v))

    for target, key, original, value in staged:
        try:
            setattr(target, key, value)
        except ValidationError as exc:
            for t2, k2, orig2, _v2 in staged:
                try:
                    setattr(t2, k2, orig2)
                except Exception:  # noqa: BLE001 - 还原的是已验证过的原值
                    logger.exception("回滚配置字段失败: %s.%s", type(t2).__name__, k2)
            first = exc.errors()[0] if exc.errors() else {}
            loc = ".".join(str(x) for x in (first.get("loc") or (key,)))
            raise HTTPException(
                status_code=400,
                detail=f"配置无效：{loc} = {value!r}（{first.get('msg', '校验失败')}）",
            ) from exc
    # 落盘两份：① 用户自己的配置区（稀疏覆盖，日后此处才是该用户的真源）
    # ② settings.json 作为灾备/遗留副本 —— DB 损坏时仍能凭它启动。
    # 系统兜底区**不被用户改动触碰**：它是所有用户的只读默认值。
    try:
        from pa_agent.storage.settings_store import apply_user_change
        from pa_agent.storage.users import default_user_id

        await asyncio.to_thread(
            apply_user_change, current.model_dump(), default_user_id()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("save settings to user overrides failed: %s", exc)

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
