"""PA Agent Web API server entry point.

Uses the same ``AppContext.bootstrap()`` as the desktop GUI then replaces
the Qt EventBus with an asyncio pub/sub for WebSocket / SSE streaming.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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

    # ── 存储层一次性初始化：必须早于 AppContext.bootstrap() ────────────────
    # bootstrap() 内会 load_settings()，而配置真源在 DB。若 DB 还没建好就去读，
    # 会得到「no such table: user_prefs」并静默退回纯文件 —— 表现为「DB 明明
    # 开着，配置却一直走文件」。一个环境一个 DB 文件，路径在此时定死。
    try:
        from pa_agent.storage.db import initialize_storage

        hub = initialize_storage()
        logger.info("SQLite storage initialized: %s", hub.stats())
        if hub.disabled:
            logger.warning(
                "SQLite unavailable (%s) — running in file-only mode",
                hub.stats()["disabled_reason"],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Storage init failed, continuing file-only: %s", exc)

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

    # Start the two-stage experience settler (pending → win/loss/unresolved).
    # Without it a record stays pending forever unless the user presses 验证.
    try:
        from web.api import experience_scheduler

        experience_scheduler.start(ctx)
    except Exception as exc:
        logger.warning("Failed to start experience scheduler: %s", exc)

    # K 线实时推送已改为**前端按自己游标轮询** /api/bars（见 docs/REMAINING_PLAN.md
    # 的评审裁决）：原 SSE 后台广播从全局订阅取数并推给所有连接，多标签页下
    # 所有人收到同一条数据流；而原生 EventSource 又带不了 X-Session-Id，服务端
    # 根本拿不到会话身份，无法分组。轮询路径本就存在，且隐藏标签页会自动停，
    # 一个坏 tab 不会传染别人。服务端不再常驻任何 K 线推送任务。

    # ── 存储层（SQLite）+ 会话注册表 ────────────────────────────────────────
    # 纯增量接入：DB 只是索引/快照层，文件仍是权威副本。任何一步失败都降级
    # 为纯文件模式并继续启动 —— 绝不让存储层故障导致 K 线出不来。
    try:
        from pa_agent.storage.db import get_hub
        from pa_agent.storage.ephemeral import get_registry
        from pa_agent.storage.importer import import_all
        from pa_agent.storage.users import ensure_admin_user

        hub = get_hub()   # 上面已 initialize_storage()，此处只取实例
        if not hub.disabled:
            # 单机部署恒为 admin；UI 暂不做登录。将来接鉴权只改 default_user_id()
            logger.info("Default user ready: %s", ensure_admin_user())
            reg = get_registry()
            logger.info("Session registry ready (max=%d)", len(reg.all_sessions()))
            stats = await asyncio.to_thread(import_all)
            logger.info("Storage import on startup: %s", stats)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Storage layer init failed, continuing file-only: %s", exc)

    yield
    # shutdown
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    try:
        from web.api import experience_scheduler

        experience_scheduler.stop()
    except Exception:
        pass
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

# Cross-origin policy.
#
# The WebUI is served by this very app (static mount at "/" + /api), so it is
# always same-origin and needs no CORS at all. The previous `allow_origins=["*"]`
# meant any web page the operator visited could read /api/settings — which also
# returned the Feishu/PushPlus/Tushare/TradingView credentials in plaintext —
# and could drive POST /api/feishu/test and PUT /api/settings. There is no auth.
#
# Opt-in for a genuinely separate front-end via PA_AGENT_CORS_ORIGINS
# (comma-separated exact origins); anything unlisted is denied.
_CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("PA_AGENT_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
if _CORS_ORIGINS:
    logger.warning(
        "CORS enabled for %s — /api/settings credentials are readable by these origins",
        _CORS_ORIGINS,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
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


@app.middleware("http")
async def bind_user_settings_middleware(request: Request, call_next):
    """按请求身份绑定生效配置 —— **「每个路由手写一遍解析」由此消失的唯一原因**。

    每个请求进来时解析一次 ``current_user_id(request)`` 对应的配置，绑进
    :mod:`contextvars`；路由与下游服务照旧读 ``ctx.settings``，读到的却是
    **本请求用户**的那一份。请求结束（无论成功还是抛异常）立即还原，
    不跨请求残留。

    为什么用中间件而不是在各路由里解析：

    * ``ctx.settings`` 有 135 处读取点，分散在分析主流程、结算、图表取数里。
      逐个改成 ``resolve_request_settings(request)`` 是���次横跨半个仓库的重构，
      且**漏一处就静默串用户** —— 漏改的代码会继续用全局配置，不报错。
    * 中间件是**唯一**需要知道「这次请求是谁」的地方。新增路由自动按用户解析，
      零心智负担。

    线程切换用 ``asyncio.to_thread``：解析命中缓存时是纯内存操作，但未命中要读
    SQLite，放进线程池可保证**事件循环永远不被 DB IO 阻塞**（``/api/bars`` 与
    追问 SSE 共用同一个循环）。

    **只对 ``/api`` 生效**：静态资源（``/``、``/js/*``、``/css/*``）由浏览器频繁
    拉取且不读配置，给它们也解析一次纯属浪费；更重要的是，配置层降级时
    ``_load_settings_from_file`` 可能在判定文件「脏」时**回写 settings.json** ——
    不该由一次 CSS 请求触发播种源改写。
    """
    from pa_agent.app_context import bind_request_settings, reset_request_settings
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import resolve_effective_settings
    from web.api.auth_ctx import current_user_id

    if not request.url.path.startswith("/api"):
        return await call_next(request)

    try:
        user_id = current_user_id(request)
    except Exception:  # noqa: BLE001 - 身份判定失败不该让接口 500
        user_id = ""
    try:
        settings = await asyncio.to_thread(resolve_effective_settings, user_id)
    except Exception as exc:  # noqa: BLE001 - 配置层故障不阻断请求，退回 ctx 默认值
        logger.warning("按用户解析配置失败，本次请求用启动默认配置: %s", exc)
        settings = None

    token = bind_request_settings(settings) if settings is not None else None
    try:
        return await call_next(request)
    finally:
        # 必须还原，否则同一个 worker Task 上后续请求会继承上一次的绑定。
        # 未绑定（解析失败）时保持 ContextVar 为 None，:attr:`AppContext.settings`
        # 自然退回启动默认配置。
        if token is not None:
            reset_request_settings(token)

from web.api.routes_settings import router as settings_router
from web.api.routes_data import router as data_router
from web.api.routes_analyze import router as analyze_router
from web.api.routes_chat import router as chat_router
from web.api.routes_records import router as records_router
from web.api.routes_experience_review import router as experience_review_router
from web.api.routes_bars_stream import router as bars_stream_router
from web.api.routes_demo import router as demo_router

app.include_router(settings_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(analyze_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(records_router, prefix="/api")
app.include_router(bars_stream_router, prefix="/api")
app.include_router(demo_router, prefix="/api")
app.include_router(experience_review_router, prefix="/api")


@app.get("/api/health")
async def health():
    """Lightweight liveness probe.

    Returns cached health status from the background heartbeat task.  If no
    heartbeat has run yet, returns ``"starting"`` so callers can retry.

    Also surfaces storage-layer state: 存储层是索引层，故障时数据仍可从文件
    读取，但让运维能一眼看出「DB 已降级」而不是静默变慢。
    """
    report = getattr(app.state, "last_health_report", None)
    storage: dict[str, Any] = {}
    try:
        from pa_agent.storage.db import get_hub
        from pa_agent.storage.ephemeral import get_registry
        from pa_agent.storage.users import default_user_id, list_users

        storage = {
            "db": get_hub().stats(),
            "sessions": len(get_registry().all_sessions()),
            "default_user": default_user_id(),
            "users": [u["user_id"] for u in list_users()],
        }
    except Exception as exc:  # noqa: BLE001
        storage = {"error": str(exc)}
    if report is None:
        return {"status": "starting", "storage": storage}
    return {"status": report["status"], "storage": storage}


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
# 自定义 StaticFiles：禁用浏览器/webview 缓存（TRAE 内置 webview 忽略 ?v=N query string，
# 必须从响应头层面禁缓存才能确保前端代码改动立即生效）
class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount("/", _NoCacheStaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
