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

    # Periodic GC: in-memory session registry + chat sessions + expired DB rows.
    # 前三者（注册表 sweep / sessions.purge_expired / 孤儿 chat_turns 清理）此前
    # **有实现、无任何生产调用者** —— 见 web/api/storage_gc.py 模块 docstring。
    # 放在 lifespan 启动而非分析主路径：清理撞 SQLite 写锁会让用户的分析多等 5s。
    try:
        from web.api import storage_gc

        storage_gc.start(ctx)
    except Exception as exc:
        logger.warning("Failed to start storage GC: %s", exc)

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
            # 单机部署恒为 admin。UI 暂不做登录。将来接鉴权只改 default_user_id()
            logger.info("Default user ready: %s", ensure_admin_user())
            _warn_if_admin_cannot_log_in()
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
        from web.api import storage_gc

        storage_gc.stop()
    except Exception:
        pass
    try:
        ctx.data_source.disconnect()
    except Exception:
        pass


def _warn_if_admin_cannot_log_in() -> None:
    """admin 没有口令时打**响亮**告警。

    强制鉴权（``ALLOW_ANONYMOUS_ADMIN=False``）下，空 ``password_hash`` 意味着
    **谁也登不进去**，而且没有任何接口能把口令设回来 —— 新建一个空库部署就
    直接锁死。这类故障在现场只表现为「前端一直跳登录页」，非常难定位，
    所以必须在启动日志里就喊出来。

    **绝不因此自动放行**：空散列 + 「随便什么都能进」是灾难性组合。补救路径
    是执行一次
    ``python -c "from pa_agent.storage.db import initialize_storage; \\
    initialize_storage(); from pa_agent.storage.users import set_password; \\
    set_password('admin', '<新口令>')"``。
    """
    try:
        from pa_agent.storage.users import get_user

        row = get_user("admin") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("无法检查 admin 口令状态: %s", exc)
        return
    if row.get("password_hash"):
        return
    logger.error(
        "============================================================\n"
        "  admin 用户没有口令，所有 /api 接口都无法登录！\n"
        "  强制鉴权已开启（ALLOW_ANONYMOUS_ADMIN=False），\n"
        "  请先执行 set_password('admin', '<新口令>') 再使用本服务。\n"
        "============================================================"
    )


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

# Cross-origin policy —— **注意：本块必须留在所有 @app.middleware("http") 之后**，
# 见文件末尾的 `_install_cors()` 调用处。
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


def _install_cors() -> None:
    """挂 CORS 中间件。**必须最后调用**（理由见下）。

    :data:`_CORS_ORIGINS` 为空时什么都不做 —— 同源部署（默认，也是唯一的
    生产部署形态）连这个中间件都不存在。

    ## 为什么要挪到最后

    Starlette 的中间件按注册顺序**反向**包裹 ⇒ 最后注册的跑在最外层。
    强制鉴权要在最外层（未认证流量不该先落一次 SQLite），而 CORS 必须在它
    再外一层：

    * **预检**：浏览器发 OPTIONS 时不带 ``Authorization``，CORS 在最外层
      直接回 200，根本不会走到鉴权；
    * **401 也要有 CORS 头**：CORS 若在鉴权**里面**，那么「令牌过期」这个
      最需要前端处理的响应会**没有** ``Access-Control-Allow-Origin``，
      浏览器把它报成 CORS 错误，前端只看到一个不含 body 的网络失败 ——
      于是「静默跳登录页」这条最关键的路径反而拿不到任何信息。

    ## allow_headers 必须含 Authorization

    强制鉴权之后前端每个请求都带 ``Authorization``，带自定义头的跨域请求
    一定触发预检。若 ``allow_headers`` 只有 ``Content-Type``，预检就失败，
    分离式前端**一个请求都发不出去**。
    """
    if not _CORS_ORIGINS:
        return
    logger.warning(
        "CORS enabled for %s — /api/settings credentials are readable by these origins",
        _CORS_ORIGINS,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        # Authorization 是强制鉴权后**每个**请求的头，漏了它分离式前端全线失败。
        allow_headers=["Content-Type", "Authorization", "X-Session-Id", "X-Trace-Id"],
        # 不开 allow_credentials：身份走 Bearer 头而非 Cookie，开了反而允许
        # 跨站携带凭证，得不偿失。
        expose_headers=["X-Trace-Id"],
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


# 紧邻 bind_user_settings_middleware：两者是同一类活儿（每个 /api 请求绑定一份
# 请求级身份到 ContextVar），挂在一起便于对照。挂载顺序上它在配置中间件**之后**
# 注册 ⇒ Starlette 把中间件按注册顺序**反向**包裹，故本中间件在**外层**：
# 会话解析（含一次快照续期写）先于路由执行，且不依赖配置已解析。
@app.middleware("http")
async def session_lifecycle_middleware(request: Request, call_next):
    """刷新会话空闲计时 + 限流续期 ``sessions`` 快照（详见函数 docstring）。

    没有它，``expires_at`` 的唯一续期点是 ``POST /api/subscribe``，而前端 boot
    从不调它 —— 表现为「订阅后一直挂着不动，快照过期后 F5 游标回到出厂种子」。
    """
    from web.api.session_ctx import session_lifecycle_middleware as _impl

    return await _impl(request, call_next)


# 鉴权中间件 —— **必须注册在本文件最后**。
#
# Starlette 的 `add_middleware` 往 `user_middleware` 头部插入，栈按列表顺序
# 包裹 ⇒ **最后注册的跑在最外层**。它必须最外层：未认证的流量要在会话解析
# （`touch` + 限流写 SQLite）与按用户解析配置（一次读库）**之前**就被挡掉，
# 否则一次「忘了带令牌」的前端 bug 就等于把每个请求都变成一次 SQLite 写。
#
# 代价：401 走的是这条最外层的路径，`trace_id_middleware` 在它里面，所以
# 401 响应上**没有** `X-Trace-Id`。这是有意的取舍 —— 401 是「你没带令牌」，
# 按定义就不是一次需要跨系统追查的请求；真要追查，日志里有 `401 <method> <path>`。
@app.middleware("http")
async def enforce_auth_middleware(request: Request, call_next):
    """强制鉴权：``/api/**`` 默认要求 Bearer 令牌，放行项逐条见 auth_ctx。"""
    from web.api.auth_ctx import enforce_auth_middleware as _impl

    return await _impl(request, call_next)


# 全app 最后一个中间件注册 —— CORS 在最外层。理由与 allow_headers 的必要性
# 见 :func:`_install_cors` 的 docstring。**新增中间件时不要写在这行后面**。
_install_cors()

from web.api.routes_settings import router as settings_router
from web.api.routes_data import router as data_router
from web.api.routes_analyze import router as analyze_router
from web.api.routes_chat import router as chat_router
from web.api.routes_records import router as records_router
from web.api.routes_experience_review import router as experience_review_router
from web.api.routes_bars_stream import router as bars_stream_router
from web.api.routes_demo import router as demo_router
from web.api.routes_auth import router as auth_router

app.include_router(settings_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(analyze_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(records_router, prefix="/api")
app.include_router(bars_stream_router, prefix="/api")
app.include_router(demo_router, prefix="/api")
app.include_router(experience_review_router, prefix="/api")
# 鉴权路由**最先** include：它是唯一免鉴权的 /api 端点，路由表里的先后
# 不影响匹配，但把它排在前面能让读者一眼看到「登录在最外层」。
app.include_router(auth_router, prefix="/api")


@app.get("/api/health")
async def health(request: Request):
    """Lightweight liveness probe.

    Returns cached health status from the background heartbeat task.  If no
    heartbeat has run yet, returns ``"starting"`` so callers can retry.

    Also surfaces storage-layer state: 存储层是索引层，故障时数据仍可从文件
    读取，但让运维能一眼看出「DB 已降级」而不是静默变慢。``storage.gc`` 暴露
    周期性清理（``web.api.storage_gc``）的运行状态与最近一轮各清了多少 ——
    清理静默失效与没有清理在外部表现上完全一样。

    ## 为什么它是免鉴权路径（``web.api.auth_ctx.PUBLIC_API_PATHS``）

    liveness 探针（k8s / docker / 反代）默认**不带** ``Authorization``。把它
    收进鉴权范围，编排系统会判定容器不健康并反复重启 —— 一次鉴权配置问题被
    升级成宕机，而且现场只剩「容器不健康」这一个几乎无法定位的信号。

    ## 但身份字段必须对匿名者隐藏

    本端点在鉴权前返回过 ``storage.default_user`` 与 ``storage.users``，
    即 admin 的用户名与全部用户清单。对着已经登录才能用的
    ``GET /api/settings``（返回飞书 / PushPlus / Tushare / TradingView 凭证）
    加一个「不用登录就能读出用户名」的接口，等于把刚关上的门又开了条缝。
    所以：**带令牌 → 全量；不带令牌 → 去掉身份字段**，并显式标出
    ``auth_required``，让前端能据此决定要不要弹登录框。

    保留的 ``db`` / ``sessions`` / ``gc`` 是运维可见性，不含身份、也不含
    任何凭证 —— 拿它们去猜谁登录了是不可能的。
    """
    from web.api.auth_ctx import ALLOW_ANONYMOUS_ADMIN, current_auth

    auth_required = not ALLOW_ANONYMOUS_ADMIN
    report = getattr(app.state, "last_health_report", None)
    auth = current_auth(request)
    storage: dict[str, Any] = {}
    try:
        from pa_agent.storage.db import get_hub
        from pa_agent.storage.ephemeral import get_registry
        from pa_agent.storage.users import default_user_id, list_users

        storage = {
            "db": get_hub().stats(),
            "sessions": len(get_registry().all_sessions()),
        }
        if auth.authenticated:
            storage["default_user"] = default_user_id()
            storage["users"] = [u["user_id"] for u in list_users()]
    except Exception as exc:  # noqa: BLE001
        storage = {"error": str(exc)}
    # GC：清理静默失效与「没有 GC」表现完全一样（数据照涨），故必须可观测 ——
    # running / interval_s / next_run_at / last（各清了多少 + errors + skipped）。
    try:
        from web.api import storage_gc

        storage["gc"] = storage_gc.status()
    except Exception as exc:  # noqa: BLE001
        storage["gc"] = {"error": str(exc)}
    payload: dict[str, Any] = {
        "status": "starting" if report is None else report["status"],
        "storage": storage,
        # 前端据此决定「这台部署是否要求登录」：反代已鉴权、开关翻回 True 的
        # 部署不该弹登录框。鉴权状态本身也一并给出，省掉一次 /api/auth/me。
        "auth": {
            "required": auth_required,
            "authenticated": bool(auth.authenticated),
            "login_url": "/api/auth/login",
        },
    }
    if auth.authenticated:
        payload["auth"]["user_id"] = auth.user_id
    return payload


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
