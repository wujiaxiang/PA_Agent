"""登录 / 登出 / 身份查询 / 改密 —— 让「鉴权」从占位变成真能用。

## 端点与形状（含取舍理由）

| 端点 | 鉴权 | 形状 |
|---|---|---|
| ``POST /api/auth/login`` | **免** | ``{token, token_type, expires_at, expires_in, user}`` |
| ``POST /api/auth/logout`` | 需 | ``{ok, revoked}`` |
| ``GET  /api/auth/me``    | 需 | ``{authenticated, user_id, display_name, role, is_admin, token_expires_at, auth_required}`` |
| ``POST /api/auth/password`` | 需 | ``{ok}`` |

**为什么不做注册**：用户这一轮明确只要登录。注册一旦开放，就同时引入了
「谁能建号」「口令强度策略」「建号频率限制」三件事，而这三件都需要产品决策
（单机自用 vs 对外服务），不该由一个路由文件替用户默认。

## 三条不可让步的约束

### 1. 「用户不存在」与「密码错」必须逐字同形

:func:`pa_agent.storage.users.authenticate` 已经把两者折叠成同一个 ``None``。
**路由层绝不能把它拆开**（例如先查一次用户是否存在再决定文案）—— 那等于把
已有的无枚举处理又还回去。所以失败路径只有一个出口
（:func:`~web.api.auth_ctx.auth_challenge_response`），文案恒为同一句，
且 :func:`_login_failed` 是唯一调用点。既有测试
``test_authenticate_unknown_user_looks_identical_to_wrong_password`` 守住原语侧。

### 2. 免鉴权路径由中间件统管，路由自己声明不了

登录端点**不能**靠「我在路由里不调 ``require_auth``」来免鉴权 —— 那样一旦路由
被挂到没挂中间件的 app 上，它就变成一个裸奔的入口。真正决定放行的是
:data:`web.api.auth_ctx.PUBLIC_API_PATHS` + ``enforce_auth_middleware``，
``tests/unit/test_auth_routes.py`` 里有专门用例锁住「挂到真 app 上仍然免鉴权」。

### 3. 自签令牌没有服务端状态，登出必须自己补一块

见 :func:`web.api.auth_ctx.revoke_token`。诚实边界：吊销表在进程内，
**重启后失效**；且改密无法吊销「同一用户的其它令牌」（名单按令牌记，不按用户记）。

## 前端契约（另一会话的 ``web/static/*`` 按此对接）

1. 启动：`GET /api/auth/me`
   * ``200`` → 已有登录态，按 ``user.display_name`` / ``user.is_admin`` 渲染
   * ``401`` → 无登录态，渲染登录框（**不要**自己判断 token 是否过期）
2. 登录：`POST /api/auth/login``，body ``{"user_id": "...", "password": "..."}``
   * 注意字段名是 ``user_id``（与 ``users.user_id``、``authenticate()`` 同名），
     **不是** ``username``
   * ``200`` → 把 ``data.token`` 存进 ``localStorage["pa_token"]``
   * ``401`` → 用户名或密码错误，停在登录框
3. 之后每个 API 请求带 ``Authorization: Bearer <token>``
4. 任意请求收到 ``401`` → 立刻 ``localStorage.removeItem("pa_token")`` 并回登录页。
   **不要自动重试**：令牌无效时重试只会打转。
5. 登出：`POST /api/auth/logout``（带令牌），无论返回什么都清 localStorage
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from pa_agent.storage.auth import issue_token
from pa_agent.storage.users import authenticate, get_user, set_password
from web.api.auth_ctx import (
    ALLOW_ANONYMOUS_ADMIN,
    AuthContext,
    auth_challenge_response,
    bearer_token,
    current_auth,
    login_token_ttl_s,
    revoke_user_tokens,
    revoke_token,
)

logger = logging.getLogger("pa_agent.web.auth.routes")

router = APIRouter(tags=["auth"])

#: 登录失败的**唯一**文案。用户不存在与口令错误共用它，逐字相同。
#:
#: 不能写成「用户不存在」/「密码错误」两支 —— 那是一个用户名枚举接口，
#: 且 :func:`pa_agent.storage.users.authenticate` 特意抹平的时序差异会被文案
#: 差异全部抵消。
_LOGIN_FAILED_DETAIL = "用户名或密码错误"

#: 改密要求的最短口令。
#:
#: 定下界的理由：口令是本机唯一一道人工兜底（无二次验证、无找回流程），
#: 4 位 PIN 级口令在 240k 轮 PBKDF2 下仍能被离线猜。需要更长请下轮加策略。
MIN_NEW_PASSWORD_LEN = 8


class LoginRequest(BaseModel):
    """登录请求。

    字段**必填**（缺字段 → 422「请求畸形」），但**不加 ``min_length`` 下界**：
    空用户名 / 空口令都交给 :func:`_login_failed` 走同一条 401 出口，而不是
    被 pydantic 挡成 422。少一条状态码分支，前端就少一处要区分的响应；
    而且 422 与 401 的区别（「请求畸形」vs「凭据不对」）与用户是否存在无关，
    不构成枚举。
    """

    user_id: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str = ""
    #: 长度下界只加在这里（改密是唯一能新建口令的入口），登录不加 ——
    #: 新口令的强度是**改密时**的事，不该让登录接口承担策略职责。
    new_password: str = Field(..., min_length=MIN_NEW_PASSWORD_LEN)


# ── 内部工具 ───────────────────────────────────────────────────────────────────


def _public_user(row: dict | None, *, user_id: str) -> dict:
    """对外的用户视图。**不含 ``password_hash``**（连散列都不给）。"""
    row = row or {}
    ctx = AuthContext(user_id=row.get("user_id") or user_id,
                      source="token", authenticated=True)
    return {
        "user_id": ctx.user_id,
        "display_name": row.get("display_name") or ctx.user_id,
        "role": row.get("role") or "user",
        # 复用 AuthContext 的判定，别在这里另写一份「谁是 admin」
        "is_admin": ctx.is_admin,
        # 前端据此提示「该账号还没设口令，登录一定会失败」——
        # 只在已认证场景返回，未认证时这条信息根本不该出现。
        "has_password": bool(row.get("password_hash")),
    }


def _login_failed(attempted_user_id: str = ""):
    """登录失败的**唯一**出口。改文案前先读模块 docstring 第 1 条。

    参数只进日志、绝不进响应体，也不参与任何判断 —— 打进响应就成了枚举。
    """
    logger.info("login attempt rejected (user_id=%r)", attempted_user_id)
    return auth_challenge_response(_LOGIN_FAILED_DETAIL)


def _auth_or_challenge(request: Request):
    """取本次身份；未认证时返回**已构造好的 401 响应**。

    为什么不直接用 :func:`~web.api.auth_ctx.require_auth`：它抛
    ``HTTPException``，响应体是 ``{"detail": ...}``，与中间件返回的
    ``{"error", "detail", "login_url"}`` 不同形。前端就要为「同一个 401」
    写两套解析。统一走 :func:`auth_challenge_response` 后，**无论中间件挂没挂**，
    同一个端点的 401 形状都一致。
    """
    auth = current_auth(request)
    if not auth.authenticated:
        logger.info("401 %s (no valid bearer token)", request.url.path)
        return auth_challenge_response("登录状态无效或已过期，请重新登录")
    return auth


# ── 端点 ───────────────────────────────────────────────────────────────────────


@router.post("/auth/login")
async def login(payload: LoginRequest, request: Request):
    """凭口令换令牌。**免鉴权**（见 :data:`PUBLIC_API_PATHS` 第一条）。

    成功：200 + 令牌。失败：401，与「用户不存在」逐字同形。
    """
    # authenticate() 才是授权判据：它内部查库、核对 PBKDF2 散列、更新
    # last_seen，并在失败时返回 None。路由层**不得**先 get_user() 再决定
    # 走哪个分支 —— 那就是枚举接口。
    user_id = authenticate(payload.user_id, payload.password)
    if not user_id:
        return _login_failed(payload.user_id)

    row = get_user(user_id)
    user = _public_user(row, user_id=user_id)
    if not user["has_password"]:
        # 走到这里说明 authenticate 放行了却有空散列 —— 不可能发生，
        # 但一旦发生（库里散列被手工清空）宁可不签发令牌。
        logger.error("user %s authenticated with empty password_hash; refusing", user_id)
        return _login_failed(payload.user_id)

    ttl_s = login_token_ttl_s()
    token = issue_token(user_id, ttl_s=ttl_s)
    now = time.time()
    logger.info("login ok: user_id=%s ttl=%ds", user_id, ttl_s)
    return {
        "token": token,
        "token_type": "bearer",
        "expires_at": round(now + ttl_s, 3),
        "expires_in": ttl_s,
        "user": user,
    }


@router.post("/auth/logout")
async def logout(request: Request):
    """登出：**需要**令牌。

    要求令牌不是为了「证明你登录过」—— 没有令牌的人本来也拿不到任何东西；
    而是为了让「点了登出」有确定的服务端语义：把这个令牌记进吊销名单，
    于是**同一浏览器里的其它标签页**、以及登出前截获过令牌的人，都立刻失效。
    没有这一步，登出只是前端删了个 localStorage。

    无令牌 / 令牌无效 → 401。幂等性未做是刻意的：前端对 401 的统一处理是
    「清本地令牌 + 回登录页」，第二次登出走这条路完全正确。
    """
    auth = _auth_or_challenge(request)
    if isinstance(auth, JSONResponse):
        return auth

    token = bearer_token(request)
    revoke_token(token, expires_at=auth.expires_at)
    logger.info("logout: user_id=%s token revoked=%s", auth.user_id, bool(token))
    return {"ok": True, "revoked": bool(token)}


@router.get("/auth/me")
async def me(request: Request):
    """当前身份与权限 —— **前端据此决定显示什么**。

    未认证时 401（而不是 200 + ``authenticated:false``），理由有二：

    * 未认证时**不能**返回任何身份字段。回落上下文里的 ``user_id`` 恒为
      ``admin``（见 :func:`~web.api.auth_ctx.current_auth` 的 fallback），
      把它回给未登录者等于白送一个用户名 —— 而我们费劲在登录失败上抹平的
      正是这类信息泄露。
    * 前端因此只需要一条规则：**401 就回登录页**，不必再区分「从没登录过」
      与「令牌过期了」。少一个状态就少一类「页面卡在半登录态」的 bug。

    顺带返回 ``auth_required``：部署方把 :data:`ALLOW_ANONYMOUS_ADMIN` 翻回
    True（反代已鉴权的场景）时，前端不必硬编码「一定要弹登录框」。
    """
    auth = _auth_or_challenge(request)
    if isinstance(auth, JSONResponse):
        return auth

    row = get_user(auth.user_id) or {}
    payload = {"authenticated": True, **_public_user(row, user_id=auth.user_id)}
    payload["token_expires_at"] = auth.expires_at
    payload["auth_required"] = not ALLOW_ANONYMOUS_ADMIN
    return payload


@router.post("/auth/password")
async def change_password(payload: ChangePasswordRequest, request: Request):
    """改自己的口令（需令牌 + 需当前口令）。

    **本轮补上的理由**：口令是**已播种**的、一次性写入生产库，没有任何 UI 或
    路由能改它。把它漏掉意味着「口令泄露后除了手写 SQL 之外无处可逃」——
    这不是「下一轮再说」能接受的，因为补救路径不存在本身就是缺陷。

    **要求当前口令**：令牌被窃取本身就等于全面沦陷，所以这一条不是安全边界，
    而是「确认此刻坐在机器前的是本人」的防误操作闸门。

    **改密会作废该用户此前所有令牌**（含本进程吊销名单里根本没记的那些 ——
    靠的是按用户的签发水位线，见
    :func:`web.api.auth_ctx.revoke_user_tokens`）。改密后**本请求用的这枚令牌
    也一并失效**，前端须重新登录；这与「改密成功却仍留在旧会话里」相比更安全。
    """
    auth = _auth_or_challenge(request)
    if isinstance(auth, JSONResponse):
        return auth

    # 同样走 authenticate()，不另写一份核对逻辑：口令核对只有一处实现，
    # 才谈得上「恒定时间」等性质。
    if not authenticate(auth.user_id, payload.current_password):
        logger.info("password change rejected for user_id=%s (current password wrong)", auth.user_id)
        # 与登录失败复用同一个出口和同一句话，避免「改密失败」成为
        # 「当前密码对但账号有问题」的侧信道。
        return auth_challenge_response(_LOGIN_FAILED_DETAIL)

    if not set_password(auth.user_id, payload.new_password):
        logger.error("set_password failed for user_id=%s", auth.user_id)
        return JSONResponse(
            status_code=503,
            content={"error": "password_not_persisted",
                     "detail": "口令未能写入，请检查存储层状态"},
        )
    # 改密是**账号级**动作：该用户此前所有设备上换到的令牌都应立刻失效。
    # 否则「改了密码」在最需要它生效的场景（令牌已被别人拿走）里恰恰不生效
    # —— 那把锁只挡住了还没偷到令牌的人。
    cutoff = revoke_user_tokens(auth.user_id)
    logger.info(
        "password changed for user_id=%s; tokens issued before %.3f revoked",
        auth.user_id, cutoff,
    )
    return {"ok": True}