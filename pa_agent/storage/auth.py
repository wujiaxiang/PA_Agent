"""认证占位层：口令散列 + 令牌签发/校验。

## 为什么现在就写

注册与登录是确定的后续需求。若等要做时才补，届时**每一个 HTTP 接口、每一次仓储
调用、每一个后台线程**都得回头补 user_id 透传 —— 那是一次覆盖全链路的重构。
现在把「令牌 → user_id」这条缝隙按最终形态立好，将来只替换实现、不动调用方。

## 令牌格式（紧凑、自签、可离线校验）

    v1.<base64url(payload_json)>.<base64url(hmac_sha256(payload)[:32])>

* **自签**（HMAC-SHA256）而非 JWT：无第三方依赖，且服务端本来就持有密钥，
  校验只需一次 hmac —— 与「本机单用户部署」的现实相符。
* payload 只放最小必要字段，**绝不放凭证**。
* 固定 ``v1.`` 前缀：将来换算法/格式时旧令牌可按前缀识别并拒绝，不产生歧义。

## 刻意留白的部分

* **无「登录接口」**：本模块不提供 ``POST /auth/login``。发令牌是账号服务的职责，
  这里只提供它需要的原语。
* **无角色判定**：``role`` 已在 users 表里，但鉴权中间件（哪些接口需登录、
  admin-only 如何拦截）属于 Web 层，放 :mod:`web.api.auth_ctx`。
* **口令策略不在此校验**：强度要求属注册接口，不属散列函数。

## 密钥来源

``TOKEN_SECRET`` 环境变量优先；未设置时开发态自动生成并落盘到
``config/token_secret``（权限 0600）。生产环境**必须**用环境变量注入，
否则多实例部署各持不同密钥，一台签发的令牌在另一台上必然校验失败。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("pa_agent.storage.auth")

TOKEN_PREFIX = "v1"
#: 令牌默认有效期。单人使用场景下够用；将来做「记住我」可延长。
DEFAULT_TOKEN_TTL_S = 30 * 24 * 3600
#: PBKDF2 迭代数。2026 年的合理下限；调高会拖慢登录，故不做成可调项。
_PBKDF2_ROUNDS = 240_000
_SALT_BYTES = 16


class AuthError(Exception):
    """认证失败。Web 层应转成 401，不要把内部细节透给客户端。"""


@dataclass(frozen=True)
class TokenClaims:
    """令牌载荷。只含身份与时效，**不含任何凭证**。"""

    user_id: str
    issued_at: float
    expires_at: float

    def is_expired(self, *, now: float | None = None, leeway_s: float = 0.0) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at + leeway_s


# ── base64url ─────────────────────────────────────────────────────────────────


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ── 口令 ───────────────────────────────────────────────────────────────────────


def new_salt() -> str:
    return _b64e(secrets.token_bytes(_SALT_BYTES))


def hash_password(password: str, *, salt: str | None = None) -> str:
    """PBKDF2-HMAC-SHA256 散列。返回可直接入库的自描述串。

    格式 ``pbkdf2_sha256$<rounds>$<salt_b64>$<dk_b64>`` —— 把参数写进结果，
    将来调高迭代数时**旧口令仍可校验**（读取时以串内参数为准），不必强制
    所有用户改密码。这是把它做成自描述而非三个独立列的关键理由。
    """
    if not isinstance(password, str) or not password:
        raise AuthError("password must be a non-empty string")
    salt_b64 = salt or new_salt()
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _b64d(salt_b64), _PBKDF2_ROUNDS
    )
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt_b64}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """恒定时间校验。任何格式异常都返回 False，绝不抛出。

    ``compare_digest`` 而非 ``==``：后者按字节短路比较，理论上可被计时攻击
    逐字节试探出正确前缀。
    """
    try:
        algo, rounds_s, salt_b64, dk_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _b64d(salt_b64), int(rounds_s)
        )
        return hmac.compare_digest(_b64e(dk), dk_b64)
    except (ValueError, TypeError, AttributeError):
        return False


# ── 令牌 ───────────────────────────────────────────────────────────────────────


def _secret_from_env_or_file() -> bytes:
    env = os.environ.get("PA_AGENT_TOKEN_SECRET", "").strip()
    if env:
        return env.encode("utf-8")

    # 开发态兜底：落盘以便重启后旧令牌仍有效。生产必须走环境变量。
    from pa_agent.config.paths import CONFIG_DIR

    path = Path(CONFIG_DIR) / "token_secret"
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                return data
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48).encode("ascii")
        path.write_bytes(secret)
        os.chmod(path, 0o600)
        logger.warning(
            "PA_AGENT_TOKEN_SECRET 未设置，已在 %s 生成开发用密钥。"
            "生产环境必须用环境变量注入，否则多实例间令牌互不认。", path
        )
        return secret
    except OSError as exc:
        logger.warning("无法持久化 token secret（%s），退回进程内临时密钥", exc)
        return secrets.token_urlsafe(48).encode("ascii")


def get_token_secret() -> bytes:
    return _secret_from_env_or_file()


def issue_token(
    user_id: str, *, secret: bytes | None = None, ttl_s: int = DEFAULT_TOKEN_TTL_S
) -> str:
    """签发令牌。**调用方负责先完成身份验证**（核对口令等）。"""
    if not user_id:
        raise AuthError("user_id must not be empty")
    now = time.time()
    payload = json.dumps(
        {"sub": user_id, "iat": round(now, 3), "exp": round(now + ttl_s, 3)},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    enc = _b64e(payload)
    sig = hmac.new(secret or get_token_secret(), enc.encode("ascii"), hashlib.sha256).digest()
    return f"{TOKEN_PREFIX}.{enc}.{_b64e(sig)}"


def verify_token(
    token: str, *, secret: bytes | None = None, leeway_s: float = 30.0
) -> TokenClaims | None:
    """校验令牌并返回 claims。**任何异常都返回 None**，不抛 AuthError。

    返回 None 而非抛异常，是因为调用点在每个请求的热路径上：令牌无效是常态
    （未登录、过期、被篡改），不是错误状态。把它当错误处理会让 Web 层到处
    写 try/except，反而更容易漏掉真正的异常。
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    _, enc, sig = parts

    expected = hmac.new(
        secret or get_token_secret(), enc.encode("ascii"), hashlib.sha256
    ).digest()
    # 先验签再解析载荷：签名不过就不该碰里面的内容
    if not hmac.compare_digest(_b64e(expected), sig):
        return None

    try:
        data = json.loads(_b64d(enc))
        claims = TokenClaims(
            user_id=str(data["sub"]),
            issued_at=float(data["iat"]),
            expires_at=float(data["exp"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    if not claims.user_id:
        return None
    return claims if not claims.is_expired(leeway_s=leeway_s) else None