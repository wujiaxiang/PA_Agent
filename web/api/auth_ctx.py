"""请求 → 用户身份：**全应用唯一的鉴权入口**。

## 定位

:func:`current_user_id` 是所有「当前用户是谁」问题的**唯一答案**。
仓储层、路由层、后台任务都不再自己调 :func:`pa_agent.storage.users.default_user_id`
拿常量 —— 那样一旦接入鉴权就会漏改，且漏改的表现是「数据落进 admin 名下」，
属于静默的数据归属错误。

## 现状：占位模式（未登录 → admin）

鉴权尚未实现，但**接口形态已经定型**：

* 有 ``Authorization: Bearer <token>`` 且校验通过 → 该令牌的 ``sub``
* 无令牌 / 令牌无效 → **开发态回落 admin**，并标记 ``is_authenticated=False``

回落而非直接 401，是为了不打断现有单机使用；等注册登录上线，把
:data:`ALLOW_ANONYMOUS_ADMIN` 改成 ``False`` 即可切换到强制鉴权 —— **一处开关，
不改任何调用方**。

## 为什么匿名回落要显式记录来源

``AuthContext.source`` 区分 ``token`` / ``header`` / ``fallback``。将来排查
「这条记录为什么挂在 admin 名下」时，能一眼看出是令牌解析还是兜底，而不是
靠猜。匿名回落若不留痕，鉴权接错了会很安静。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pa_agent.storage.auth import TokenClaims, verify_token
from pa_agent.storage.users import ADMIN_USER_ID

logger = logging.getLogger("pa_agent.web.auth")

AUTH_HEADER = "Authorization"
BEARER_SCHEME = "bearer"

#: 开发态回落开关。**接入鉴权时置 False**，全部接口立即要求 Bearer 令牌。
ALLOW_ANONYMOUS_ADMIN = True

#: 上游已鉴权时可直传的用户身份头（网关/反代场景）。
#: 默认关闭 —— 若开着而部署方并未鉴权，任何人都能冒充任意 user_id。
TRUST_USER_HEADER = False
USER_HEADER = "X-User-Id"


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
    """
    token = bearer_token(request)
    if token:
        claims = verify_token(token)
        if claims is not None:
            return AuthContext(
                user_id=claims.user_id, claims=claims,
                source="token", authenticated=True,
            )
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


def require_auth(request) -> AuthContext:
    """强制鉴权入口：未认证抛 401。

    给将来「必须登录才能访问」的接口用。**现在先别用** —— 鉴权未上线时
    所有请求都是匿名回落，逐个加上去会全线 401。
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