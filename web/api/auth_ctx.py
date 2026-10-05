"""请求 → 用户身份：**全应用唯一的鉴权入口**。

## 定位

:func:`current_user_id` 是所有「当前用户是谁」问题的**唯一答案**。
仓储层、路由层、后台任务都不再自己调 :func:`pa_agent.storage.users.default_user_id`
拿常量 —— 那样一旦接入鉴权就会漏改，且漏改的表现是「数据落进 admin 名下」，
属于静默的数据归属错误。

## 现状：强制鉴权（2026-10-06 起）

:data:`ALLOW_ANONYMOUS_ADMIN` 已是 ``False``，鉴权由
:func:`enforce_auth_middleware` 在**最外层**统一把关：

* 有 ``Authorization: Bearer <token>`` 且校验通过 → 该令牌的 ``sub``
* 无令牌 / 令牌无效 / 已吊销 → **401**，不再回落 admin

**回落逻辑本身保留**（:func:`current_auth` 仍返回 ``authenticated=False`` 的
admin 上下文），因为后台线程、GC、启动初始化等**没有请求上下文**的地方仍要
拿到一个可用的 user_id；请求路径的拦截则一律交给中间件。两者分开，才能做到
「请求必须登录」与「进程内无人签发令牌也不会崩」同时成立。

## 免鉴权白名单：每一条都是「不开放就锁死」或「不开放就误伤」

见 :data:`PUBLIC_API_PATHS`。新增任何一条前先回答：**不放行会发生什么？**
答不上来就不要加。

## 为什么匿名回落要显式记录来源

``AuthContext.source`` 区分 ``token`` / ``header`` / ``fallback``。将来排查
「这条记录为什么挂在 admin 名下」时，能一眼看出是令牌解析还是兜底，而不是
靠猜。匿名回落若不留痕，鉴权接错了会很安静。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass

from pa_agent.storage.auth import TokenClaims, verify_token
from pa_agent.storage.users import ADMIN_USER_ID

logger = logging.getLogger("pa_agent.web.auth")

AUTH_HEADER = "Authorization"
BEARER_SCHEME = "bearer"

#: 匿名回落开关。**现为 False**：所有 ``/api/**`` 默认要求 Bearer 令牌。
#:
#: 只有 :data:`PUBLIC_API_PATHS` 里的路径例外。翻回 ``True`` 是应急退路
#: （例如部署方在前面挂了已鉴权的反代，把本服务当作内网组件），不要当常态。
ALLOW_ANONYMOUS_ADMIN = False

#: 上游已鉴权时可直传的用户身份头（网关/反代场景）。
#: 默认关闭 —— 若开着而部署方并未鉴权，任何人都能冒充任意 user_id。
TRUST_USER_HEADER = False
USER_HEADER = "X-User-Id"

#: 免鉴权的 API 路径。**逐条有理由**，新增前先回答「不放行会怎样」。
#:
#: 1. ``/api/auth/login`` —— 不放行就是**死锁**：谁也拿不到令牌，令牌又只有
#:    登录端点能签发。401 的提示无法告诉任何人怎么登录，因为登录页自己进不去。
#: 2. ``/api/health`` —— 不放行则是**可用性事故**：liveness 探针（k8s /
#:    docker / 反代）默认不带 ``Authorization``，401 会让编排系统判定容器
#:    不健康并反复重启，把一次鉴权配置问题升级成宕机。放行但**降级字段**
#:    （见 ``web/server.py::health``）—— 探针要的只是「进程活着吗」，
#:    而身份字段恰恰是未登录时最不该泄露的东西。
#:
#: 注意 ``/``、``/js/*``、``/css/*``、``/docs`` **不在本表里**：它们压根不是
#: ``/api`` 路径，中间件对非 ``/api`` 前缀直接放行（见 :func:`enforce_auth_middleware`）。
#: 这一点是结构性的，不是靠逐条登记 —— 忘记登记的新静态资源仍然能加载。
PUBLIC_API_PATHS: frozenset[str] = frozenset({
    "/api/auth/login",
    "/api/health",
})

#: 登录签发的令牌有效期（秒）。默认 7 天。
#:
#: 不用 :data:`pa_agent.storage.auth.DEFAULT_TOKEN_TTL_S`（30 天）：那是占位层
#: 写下的「单人使用够用」，不是一次安全判断。令牌落在 ``localStorage``，拿到
#: 它就等于拿到本机全部权限（读全部历史记录、改设置、驱动分析），30 天把这个
#: 窗口拉得过长；7 天既能覆盖「周末连续跑分析」这类长会话，又把暴露面压到一周。
#:
#: 运维可用 ``PA_AGENT_TOKEN_TTL_S`` 覆盖 —— 换密钥/改口令后调小它可以把
#: 「最坏暴露窗口」压到分钟级（见 AGENTS.md 的多会话协作约束）。
DEFAULT_LOGIN_TOKEN_TTL_S = 7 * 24 * 3600
_MIN_TOKEN_TTL_S = 60
_MAX_TOKEN_TTL_S = 365 * 24 * 3600


@dataclass(frozen=True)
class AuthContext:
    """一次请求的身份判定结果。"""

    user_id: str
    claims: TokenClaims | None = None
    source: str = "fallback"      # token | header | fallback
    authenticated: bool = False

    @property
    def is_admin(self) -> bool:
        return self.user_id == ADMIN_USER_ID

    @property
    def expires_at(self) -> float | None:
        """令牌绝对过期时刻（epoch 秒）；无令牌时为 ``None``。"""
        return self.claims.expires_at if self.claims else None


# ── 吊销名单 ───────────────────────────────────────────────────────────────────
#
# 自签 HMAC 令牌**没有服务端状态**，所以「登出」天然无法作废一个已发出去的令牌。
# 若登出只是回一个 200，它就是一句谎：前端从 localStorage 删了令牌，可同一个
# 浏览器里另一个标签页、或此前截获过令牌的人，手里的令牌照样能用满 30 天。
#
# 这里补一份**进程内**吊销表，让登出在本进程生命周期内真正生效。诚实的边界：
# **进程重启后吊销记录消失**，此前签发的令牌重新可用。所以它挡的是「共享浏览器
# 里点了登出但令牌还在」和「登出前截获令牌」，不是「令牌一旦签发即永久有效」。
# 要做到重启后依然可吊销，必须引入持久化存储 —— 本轮刻意不引入（见交付说明）。
#
# 键用 token 的 sha256 摘要而非原文：令牌常被记进日志，名单里存原文等于再造一份
# 可用副本。
_revoked: dict[str, float] = {}
#: 按**用户**记的吊销水位线：签发时刻早于 ``cutoff`` 的该用户令牌一律作废。
#:
#: 为什么不能只靠 ``_revoked``：那本按令牌哈希记，而改密是**账号级**动作 ——
#: 用户改了口令，此前所有浏览器/所有设备上换到的令牌都应立刻失效，否则
#: 「改了密码」在最需要它生效的场景（别人拿着旧令牌）里恰恰不生效。
#:
#: 存水位线而不是令牌集合，是因为要作废的是**过去签发的全部**令牌（含进程重启
#: 前签的、清单里根本没记的那些）；集合会让「重启后新登入的令牌」也一起被误杀。
#: user_id -> (签发水位线, 本条记录自身的失效时刻)
_revoked_users: dict[str, tuple[float, float]] = {}
_revoked_lock = threading.Lock()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def revoke_token(token: str, *, expires_at: float | None = None) -> None:
    """把令牌放进吊销名单。

    ``expires_at`` 取令牌自己的过期时刻：令牌本来就快过期时无需吊销，且吊销记录
    永远不会比令牌活得久（否则名单会随登出次数单调增长）。
    """
    if not token or not isinstance(token, str):
        return
    exp = float(expires_at) if expires_at else (time.time() + DEFAULT_LOGIN_TOKEN_TTL_S)
    now = time.time()
    if exp <= now:
        return                       # 已过期，登出无意义
    with _revoked_lock:
        _prune_revoked(now)
        _revoked[_token_digest(token)] = exp


def revoke_user_tokens(user_id: str, *, at: float | None = None) -> float:
    """作废该用户**此刻之前**签发的全部令牌，返回记录的水位线。

    用于改密。``at`` 是签发水位线：令牌载荷里的 ``iat`` 早于它即判失效。
    比它新的（理论上不存在，改密瞬间不会有并发登录成功）仍然有效。
    """
    if not user_id or not isinstance(user_id, str):
        return 0.0
    now = time.time()
    cutoff = float(at) if at else now
    # 本条记录自身的失效时刻 = 水位线 + 令牌最长剩余寿命。过了它就可以整条丢掉：
    # 那时任何还没过期的令牌都不可能早于水位线（否则它早该过期了）。
    expires = cutoff + float(login_token_ttl_s())
    with _revoked_lock:
        _prune_revoked(now)
        prev = _revoked_users.get(user_id)
        # 只前进不后退：并发改密时水位线不能被后一次调用的更早时间戳拉回去
        keep = prev[0] if prev is not None else 0.0
        _revoked_users[user_id] = (max(keep, cutoff), expires)
        return max(keep, cutoff)


def is_user_tokens_revoked(
    user_id: str, *, issued_at: float, at: float | None = None
) -> bool:
    """该用户这枚令牌是否因改密而作废（``issued_at`` 早于水位线）。"""
    if not user_id or not issued_at:
        return False
    now = float(at) if at else time.time()
    with _revoked_lock:
        entry = _revoked_users.get(user_id)
        if entry is None:
            return False
        cutoff, expires = entry
        if expires <= now:    # 这条吊销记录本身过期（很久没改密）
            _revoked_users.pop(user_id, None)
            return False
        # 令牌载荷的 iat 是 round(now, 3)（毫秒精度），而 cutoff 是原始
        # time.time()。同一毫秒内先后签发时，比较会因这半个毫秒差而把
        # 「改密之后签的那枚」误杀 —— 测试里两条语句连着跑就会复现。
        # 容忍 1ms：把暴露窗口从 0.5ms 提到 1ms，换来判定不抖动。
        return float(issued_at) < cutoff - 0.001


def is_token_revoked(token: str) -> bool:
    """令牌是否已被登出作废。**任何异常都按「未吊销」处理** —— 吊销表是加速
    手段，不是授权判据；它出错时最该放行的是「令牌本身有效」的情形，
    由中间件兜底报错，而不是把所有人挡在门外。"""
    if not token or not isinstance(token, str):
        return False
    try:
        key = _token_digest(token)
        with _revoked_lock:
            exp = _revoked.get(key)
            if exp is None:
                return False
            if exp <= time.time():
                _revoked.pop(key, None)     # 顺手回收，不必等下一轮
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("revocation lookup failed (treated as not revoked): %s", exc)
        return False


def _prune_revoked(now: float) -> None:
    """丢掉已过期的吊销记录。调用方持锁。"""
    for key in [k for k, exp in _revoked.items() if exp <= now]:
        _revoked.pop(key, None)
    for uid in [u for u, (_c, exp) in _revoked_users.items() if exp <= now]:
        _revoked_users.pop(uid, None)


def reset_revocation_state_for_tests() -> None:
    """清空吊销名单。测试专用（否则跨用例泄漏）。"""
    with _revoked_lock:
        _revoked.clear()
        _revoked_users.clear()


def login_token_ttl_s() -> int:
    """本次登录签发的有效期。env ``PA_AGENT_TOKEN_TTL_S`` 覆盖，越界即回落默认。

    越界必须回落而不是照单全收：写错一个值（``0`` / ``-1`` / 单位填成毫秒）
    会造出「登录即过期」或「永不过期」这两种都很难排查的行为。
    """
    raw = (os.environ.get("PA_AGENT_TOKEN_TTL_S") or "").strip()
    if not raw:
        return DEFAULT_LOGIN_TOKEN_TTL_S
    try:
        val = int(raw)
    except ValueError:
        logger.warning("PA_AGENT_TOKEN_TTL_S 不是整数（%r），用默认值", raw)
        return DEFAULT_LOGIN_TOKEN_TTL_S
    if not (_MIN_TOKEN_TTL_S <= val <= _MAX_TOKEN_TTL_S):
        logger.warning(
            "PA_AGENT_TOKEN_TTL_S=%s 超出 [%d, %d]，用默认值",
            val, _MIN_TOKEN_TTL_S, _MAX_TOKEN_TTL_S,
        )
        return DEFAULT_LOGIN_TOKEN_TTL_S
    return val


def bearer_token(request) -> str:
    """从 ``Authorization: Bearer <token>`` 取出令牌原文。

    只做提取与格式判断，**不校验**。大小写不敏感地匹配 scheme（RFC 7235）。
    """
    try:
        raw = request.headers.get(AUTH_HEADER, "") or ""
    except Exception:  # noqa: BLE001 — 任何异常都视作「无凭证」
        return ""
    parts = raw.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME:
        return ""
    return parts[1].strip()


def current_auth(request) -> AuthContext:
    """判定本次请求的身份。**任何情况下都不抛异常。**

    调用顺序即优先级：令牌 > 直传头 > 匿名回落。令牌优先是因为直传头只在
    受信上游场景可用，而令牌是我们自己签发、必然可信。

    **注意本函数不负责「要不要拒绝」**：它只回答「这次是谁」。是否 401 由
    :func:`enforce_auth_middleware` 决定 —— 两者分开，后台线程等无请求上下文的
    调用点才能继续拿到可用的 user_id 而不必各自写一遍 401 逻辑。
    """
    token = bearer_token(request)
    if token:
        claims = verify_token(token)
        if claims is not None and not is_token_revoked(token):
            # 改密是账号级动作：该用户此前所有令牌一并作废。放在逐令牌吊销
            # 之后判 —— 两者的判据都是「这枚令牌还能不能用」，短路顺序无所谓，
            # 但先跑便宜的哈希查表。
            if not is_user_tokens_revoked(
                claims.user_id, issued_at=claims.issued_at
            ):
                return AuthContext(
                    user_id=claims.user_id, claims=claims,
                    source="token", authenticated=True,
                )
            logger.info(
                "Bearer token superseded by password change for %s; rejected",
                claims.user_id,
            )
        elif claims is not None:
            logger.info("Bearer token revoked by logout; rejected")
        else:
            logger.debug("Bearer token invalid or expired; falling back")

    if TRUST_USER_HEADER:
        try:
            uid = (request.headers.get(USER_HEADER, "") or "").strip()
        except Exception:  # noqa: BLE001
            uid = ""
        if uid:
            return AuthContext(user_id=uid, source="header", authenticated=True)

    return AuthContext(user_id=ADMIN_USER_ID, source="fallback", authenticated=False)


def current_user_id(request) -> str:
    """本次请求的 user_id。**这是取代 ``default_user_id()`` 的唯一入口。**"""
    return current_auth(request).user_id


def resolve_request_settings(request) -> "object":
    """解析**本次请求**该用户生效的配置。

    这是「请求路径拿配置」的唯一入口，与 :func:`current_user_id` 配对使用。
    **它保证任何情况下都不抛异常**：配置层故障时回落到启动时的默认解析
    （``load_settings()``），宁可给一份可能不是本用户的配置，也不能让整个
    接口 500 —— 身份判定失败与配置解析失败是两件事，前者该 401，后者不该。

    普通业务代码**通常不需要直接调它**（见下）。它的用途是「拿不到
    ``ctx``、但确实需要按用户解析配置」的地方，例如后台任务里要为某个
    已记录 ``user_id`` 的记录结算。
    """
    from pa_agent.config.settings import load_settings, resolve_effective_settings

    try:
        user_id = current_user_id(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning("身份判定失败，回落启动默认配置: %s", exc)
        return load_settings()
    try:
        return resolve_effective_settings(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("按用户解析配置失败，回落启动默认配置: %s", exc)
        return load_settings()


def is_public_api_path(path: str) -> bool:
    """该路径是否免鉴权。白名单与理由见 :data:`PUBLIC_API_PATHS`。"""
    return path in PUBLIC_API_PATHS


def auth_challenge_response(detail: str = "缺少或无效的 Bearer 令牌"):
    """统一的 401 响应。

    **所有 401 必须走这里**，包括登录失败 —— 响应体逐字相同时，攻击者才无法
    从「文案不同」推断用户名是否存在。形状：

    * ``error``   机器可读的稳定标识（前端据此弹登录框，不要去匹配文案）
    * ``detail``  给人看的，一律不含「用户不存在」这类可枚举信息
    * ``login_url`` 登录端点，前端不必硬编码路径
    """
    from starlette.responses import JSONResponse

    return JSONResponse(
        status_code=401,
        content={
            "error": "authentication_required",
            "detail": detail,
            "login_url": "/api/auth/login",
        },
        headers={
            # RFC 7235：401 必须带 WWW-Authenticate，浏览器与网关据此知道
            # 该走哪种认证流程。
            "WWW-Authenticate": "Bearer",
            # 401 响应绝不能被缓存 —— 否则「登录已过期」会被缓存命中，
            # 表现为刷新后仍然进不去，且极难排查。
            "Cache-Control": "no-store",
        },
    )


async def enforce_auth_middleware(request, call_next):
    """强制鉴权：**唯一**的「这个请求要不要令牌」判定处。

    挂在 :mod:`web.server` 的**最外层**。为什么是中间件而不是逐路由依赖：

    * 逐路由依赖必然漏 —— 新增路由时忘了加依赖就是一个静默的未鉴权端点，
      而这种失效**没有任何报错**（请求照常 200）。
    * 中间件是唯一需要知道「这次是谁」的地方，一个前缀判断就覆盖全站，
      新路由零心智负担。

    放行的三类，逐条理由：

    1. **非 ``/api`` 路径** —— 静态资源（``/``、``/js/*``、``/css/*``）与
       ``/docs``、``/openapi.json``。它们是**登录页本身**：浏览器必须先拿到
       ``index.html`` 才谈得上渲染登录框，要求令牌就是死锁。结构性保证，
       不依赖白名单登记。
    2. **OPTIONS 预检** —— 预检按规范**不带** ``Authorization``（它在
       ``Access-Control-Request-Headers`` 里只是「申请许可」，真正的请求才带），
       所以放行预检不可能泄露任何东西；真正的请求仍会被本中间件拦住。
       这条对 ``PA_AGENT_CORS_ORIGINS`` 开启后的分离前端是必需的。
    3. **:data:`PUBLIC_API_PATHS`** —— 登录端点与健康探针，逐条理由见该常量。

    **顺序无关的兜底**：:data:`ALLOW_ANONYMOUS_ADMIN` 为 ``True`` 时本中间件
    完全空转（应急退路）。读的是模块全局，测试可以 monkeypatch。

    401 在**会话解析与配置解析之前**返回 —— 未认证流量不该消耗一次 SQLite 读，
    否则一次鉴权配置错误就能被放大成数据库压力。
    """
    # 1) 预检（见函数 docstring）
    if getattr(request, "method", "GET").upper() == "OPTIONS":
        return await call_next(request)

    path = getattr(getattr(request, "url", None), "path", "") or ""
    # 2) 非 /api：静态资源与文档 —— 结构性放行
    if not path.startswith("/api"):
        return await call_next(request)

    if ALLOW_ANONYMOUS_ADMIN:
        return await call_next(request)
    if is_public_api_path(path):
        return await call_next(request)

    if not current_auth(request).authenticated:
        logger.info("401 %s %s (no valid bearer token)", request.method, path)
        return auth_challenge_response()
    return await call_next(request)


def require_auth(request) -> AuthContext:
    """强制鉴权入口：未认证抛 :class:`fastapi.HTTPException` 401。

    **正常路由不需要用它** —— :func:`enforce_auth_middleware` 已在最外层统一
    把关，这里再逐个路由加一遍只会制造重复。

    保留它的两个理由：

    * 路由被**单独**挂到别的 app 上（单测夹具、将来的子应用）时，中间件不在，
      此时它是唯一的兜底。
    * 将来出现「免鉴权路径」里的局部再加一道鉴权（如 ``/api/health`` 的某个
      昂贵分支）。
    """
    auth = current_auth(request)
    if not auth.authenticated:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth