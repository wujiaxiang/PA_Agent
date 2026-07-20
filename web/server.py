"""PA Agent Web API server entry point.

Uses the same ``AppContext.bootstrap()`` as the desktop GUI then replaces
the Qt EventBus with an asyncio pub/sub for WebSocket / SSE streaming.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Ensure PA_Agent project root is on sys.path so pa_agent is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger("pa_agent.web")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


# Re-exported for trace_id_middleware — imported lazily to avoid circular import.
def get_trace_id() -> str:
    from pa_agent.util.logging import get_trace_id as _gti
    return _gti()


# Heartbeat interval (seconds). Trade-off: too short burns tokens, too long
# delays detection. 5 min matches typical monitoring expectations.
_HEALTH_HEARTBEAT_INTERVAL_S = 300.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bootstrap AppContext on startup, tear down on shutdown."""
    from pa_agent.app_context import AppContext
    from web.bridge.event_bus import AsyncEventBus

    logger.info("Lifespan startup: beginning bootstrap")
    ctx = AppContext.bootstrap()
    # Replace Qt EventBus with async stub so core emits don't crash
    ctx.event_bus = AsyncEventBus()
    app.state.ctx = ctx
    # Last cached health report (updated by heartbeat task). None = no check yet.
    app.state.last_health_report = None
    logger.info("PA Agent Web backend bootstrapped (data_source=%s)", type(ctx.data_source).__name__)

    # Start background heartbeat task (TODO P1.3)
    heartbeat_task = asyncio.create_task(_health_heartbeat(app))
    logger.info("Health heartbeat task created")

    # Start background bars-stream task (SSE /api/bars/stream)
    from web.api import routes_bars_stream
    logger.info("Starting bars stream background task...")
    try:
        await routes_bars_stream.start_background_task(app)
        logger.info("Bars stream background task started successfully")
    except Exception as exc:
        logger.error("Failed to start bars stream background task: %s", exc)

    yield
    # shutdown
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    await routes_bars_stream.stop_background_task()
    try:
        ctx.data_source.disconnect()
    except Exception:
        pass


async def _health_heartbeat(app: FastAPI) -> None:
    """Background task: ping model API + data source every 5 min.

    Caches the result in ``app.state.last_health_report`` so ``/api/health``
    can return the cached status without re-running the check on every call.
    Cancels cleanly on shutdown.

    The first check runs in the background (not awaited at startup) so it
    doesn't block lifespan from completing — a slow model API ping should
    not delay server readiness.  /api/health returns "starting" until the
    first check completes.
    """
    while True:
        try:
            await _run_and_cache_health(app)
            await asyncio.sleep(_HEALTH_HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Health heartbeat iteration failed", exc_info=True)
            # On failure, wait the full interval before retrying to avoid
            # hammering a broken upstream.
            await asyncio.sleep(_HEALTH_HEARTBEAT_INTERVAL_S)


async def _run_and_cache_health(app: FastAPI) -> None:
    """Run full health check in a thread (blocking I/O) and cache result."""
    from pa_agent.util.startup_health_check import run_full_check

    ctx = app.state.ctx
    # Run blocking check in threadpool to avoid blocking event loop
    report = await asyncio.to_thread(run_full_check, ctx)
    app.state.last_health_report = report.to_dict()
    if report.status != "ok":
        logger.warning(
            "Health check status=%s: %s",
            report.status,
            report.to_dict(),
        )


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


@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    """Disable browser caching for development."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/js/") or path.startswith("/css/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """Assign trace_id to every request for structured log correlation (TODO P2.4).

    Reads ``X-Trace-Id`` header if present (for upstream propagation),
    otherwise generates a 12-char hex id.  The id is exposed in the response
    header so clients can reference it when reporting issues.
    """
    from pa_agent.util.logging import set_trace_id

    tid = request.headers.get("X-Trace-Id") or None
    set_trace_id(tid)
    response = await call_next(request)
    response.headers["X-Trace-Id"] = get_trace_id()
    return response

from web.api.routes_settings import router as settings_router
from web.api.routes_data import router as data_router
from web.api.routes_analyze import router as analyze_router
from web.api.routes_chat import router as chat_router
from web.api.routes_records import router as records_router
from web.api.routes_bars_stream import router as bars_stream_router

app.include_router(settings_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(analyze_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(records_router, prefix="/api")
app.include_router(bars_stream_router, prefix="/api")


@app.get("/api/health")
async def health():
    """Lightweight liveness probe.

    Returns cached health status from the background heartbeat task.  If no
    heartbeat has run yet, returns ``"starting"`` so callers can retry.
    """
    report = getattr(app.state, "last_health_report", None)
    if report is None:
        return {"status": "starting"}
    return {"status": report["status"]}


@app.get("/api/health/check")
async def health_check():
    """Full health check (runs synchronously, may take a few seconds).

    Pings the model API (1-token chat completion) and data source
    (latest_snapshot(2)).  Returns per-component details with latency.

    Use this for diagnostics; use ``/api/health`` for fast liveness probes.
    """
    from pa_agent.util.startup_health_check import run_full_check

    ctx = app.state.ctx
    report = await asyncio.to_thread(run_full_check, ctx)
    # Also update the cached report
    app.state.last_health_report = report.to_dict()
    return report.to_dict()


# Serve static frontend at /
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
