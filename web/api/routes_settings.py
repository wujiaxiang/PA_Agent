"""REST routes for settings CRUD."""
from __future__ import annotations

from fastapi import APIRouter, Request

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import load_settings, save_settings

router = APIRouter(tags=["settings"])


@router.get("/settings")
async def get_settings(request: Request):
    """Return current settings with API key masked."""
    ctx = request.app.state.ctx
    settings = load_settings(SETTINGS_JSON_PATH)
    ctx.settings = settings  # sync live reference
    d = settings.model_dump()
    pk = d.get("provider", {})
    if pk.get("api_key"):
        pk["api_key"] = pk["api_key"][:4] + "****" + pk["api_key"][-4:] if len(pk["api_key"]) > 8 else "****"
        d["provider"] = pk
    return d


@router.put("/settings")
async def put_settings(request: Request, body: dict):
    """Merge *body* into current settings and save to disk."""
    ctx = request.app.state.ctx
    current = ctx.settings
    for section in ("provider", "prompt", "validation", "general", "feishu", "tushare", "pushplus"):
        if section in body and isinstance(body[section], dict):
            target = getattr(current, section, None)
            if target is not None:
                for k, v in body[section].items():
                    if hasattr(target, k):
                        setattr(target, k, v)
    save_settings(current, SETTINGS_JSON_PATH)

    from pa_agent.util.logging import update_api_key
    update_api_key(current.provider.api_key)

    # Rebuild AI client if provider changed
    from pa_agent.ai.client_factory import create_ai_client
    ctx.client = create_ai_client(current.provider, logger_=ctx.logger)

    return {"status": "saved"}
