"""PA Agent Web API server entry point.

Uses the same ``AppContext.bootstrap()`` as the desktop GUI then replaces
the Qt EventBus with an asyncio pub/sub for WebSocket / SSE streaming.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Ensure PA_Agent project root is on sys.path so pa_agent is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger("pa_agent.web")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bootstrap AppContext on startup, tear down on shutdown."""
    from pa_agent.app_context import AppContext
    from web.bridge.event_bus import AsyncEventBus

    ctx = AppContext.bootstrap()
    # Replace Qt EventBus with async stub so core emits don't crash
    ctx.event_bus = AsyncEventBus()
    app.state.ctx = ctx
    logger.info("PA Agent Web backend bootstrapped (data_source=%s)", type(ctx.data_source).__name__)
    yield
    # shutdown
    try:
        ctx.data_source.disconnect()
    except Exception:
        pass


app = FastAPI(
    title="PA Agent Web",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from web.api.routes_settings import router as settings_router
from web.api.routes_data import router as data_router
from web.api.routes_analyze import router as analyze_router
from web.api.routes_chat import router as chat_router

app.include_router(settings_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(analyze_router, prefix="/api")
app.include_router(chat_router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Serve static frontend at /
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
