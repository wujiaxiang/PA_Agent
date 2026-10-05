"""用户（当前单机部署恒为 1 个 admin；UI 暂不做登录）。

**为什么现在就建表**：目标里明写「数据层面支持多用户」。若等真要登录才加，
届时所有 L2/L3 表都要回填 user_id 并重写索引 —— 那是破坏性迁移。现在把
``user_id`` 铺进表结构、值恒为 ``admin``，将来只��改「解析成哪个用户」。

**默认进 admin**：UI 暂不做登录，故 ``default_user_id()`` 永远返回 admin。
接入鉴权时，只需让该函数改为从请求上下文取当前用户，其余代码不动。
"""
from __future__ import annotations

import logging

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, now

logger = logging.getLogger("pa_agent.storage.users")

#: 单机部署下的唯一用户。**别名到 db.DEFAULT_USER_ID**，避免两处各定义一份
#: 而产生「记录指向 users 表里不存在的用户」这种静默错配（评审 H5）。
ADMIN_USER_ID = DEFAULT_USER_ID
ADMIN_DISPLAY_NAME = "管理员"


def ensure_admin_user() -> str:
    """幂等创建 admin 用户并标记为默认。返回 user_id。

    启动时调用。``INSERT OR IGNORE`` 保证重复启动不报错、也不覆盖已有行
    （``last_seen`` 只更新，不动 role / display_name）。
    """
    hub = get_hub()
    if hub.disabled:
        # DB 不可用不是致命错误：配置会整体回退到 settings.json，
        # 这正是设计里的降级路径（见 docs/SESSION_STORAGE_DESIGN.md §7）。
        logger.warning("ensure_admin_user: DB disabled, skipping (settings stay file-backed)")
        return ADMIN_USER_ID

    ts = now()
    hub.execute(
        """
        INSERT INTO users (user_id, display_name, role, is_default, created_at, last_seen)
        VALUES (?, ?, 'admin', 1, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (ADMIN_USER_ID, ADMIN_DISPLAY_NAME, ts, ts),
    )
    return ADMIN_USER_ID


def default_user_id() -> str:
    """进程级兜底用户 —— **仅供无请求上下文的场景**（启动初始化、CLI）。

    ⚠️ **请求路径一律不要用它**：那是「不知道用户是谁就猜 admin」的写法，
    接入鉴权后会导致数据错挂到 admin 名下且无任何报错。请求路径请用
    ``web.api.auth_ctx.current_user_id(request)``。
    """
    return ADMIN_USER_ID


def get_user(user_id: str = "") -> dict | None:
    row = get_hub().query_one(
        "SELECT * FROM users WHERE user_id = ?", (user_id or ADMIN_USER_ID,)
    )
    return dict(row) if row else None


def list_users() -> list[dict]:
    return [dict(r) for r in get_hub().query("SELECT * FROM users ORDER BY created_at")]


# ── 注册 / 登录（占位：原语齐备，接口留给将来的账号服务）───────────────────────


def create_user(
    user_id: str, *, password: str, display_name: str = "", role: str = "user"
) -> str:
    """建号并写入口令散列。返回 user_id。

    ``user_id`` 非空且经 :func:`_validate_user_id` 归一 —— 它会进 SQL 与
    令牌载荷，字符集必须收紧（与 session_id 同理）。
    已存在则抛 :class:`pa_agent.storage.auth.AuthError`，**不静默覆盖口令**。
    """
    from pa_agent.storage.auth import AuthError, hash_password

    uid = _validate_user_id(user_id)
    if get_user(uid) is not None:
        raise AuthError(f"user already exists: {uid}")

    hub = get_hub()
    if hub.disabled:
        raise AuthError("storage unavailable: cannot create user")

    ts = now()
    ok = hub.execute(
        """
        INSERT INTO users (user_id, display_name, role, password_hash,
                           is_default, created_at, last_seen)
        VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        (uid, display_name or uid, role, hash_password(password), ts, ts),
    )
    return uid if ok else uid     # execute 只记 warning，不抛；行是否落库由读侧兜底


def set_password(user_id: str, password: str) -> bool:
    """重置口令。不碰其它字段，也不改 role。"""
    from pa_agent.storage.auth import hash_password

    hub = get_hub()
    if get_user(user_id) is None:
        return False
    return hub.execute(
        "UPDATE users SET password_hash = ? WHERE user_id = ?",
        (hash_password(password), user_id),
    )


def authenticate(user_id: str, password: str) -> str | None:
    """核对口令。**成功返回 user_id，失败返回 None** —— 不抛异常、不区分
    「用户不存在」与「口令错误」。

    区分这两种情况等于给攻击者一个用户名枚举接口：两者响应时间/文案一致。
    用户不存在时仍跑一遍 :func:`verify_password` 的开销以抹平时序差异。

    ⚠️ 空 ``user_id`` **不**回落 admin。``get_user("")`` 因为
    ``user_id or ADMIN_USER_ID`` 会返回 admin 行 —— 于是
    ``authenticate("", <admin 的口令>)`` 会**登录成功并返回 "admin"**。
    那不是「多一条登录途径」（拿着 admin 口令本来就能登 admin），但它是本模块
    自己在每处都警告「请求路径不许回落 admin」的同时漏掉的一个洞：前端用户名
    空着、口令被密码管理器自动填上的那一次，会静默变成以 admin 身份登录。
    """
    from pa_agent.storage.auth import verify_password

    # 绕开 get_user 的「空串即 admin」默认：这里是授权路径，不能有默认身份。
    row = get_user(user_id) if user_id else None
    stored = (row or {}).get("password_hash") or ""
    if not row or not stored:
        # 用一份**格式完好**的诱饵散列跑一遍完整 PBKDF2。
        #
        # 为什么不能直接 verify_password(password, "")：空串在
        # verify_password 里于 `.split("$", 3)` 处就 ValueError 返回 False，
        # **一次 PBKDF2 都不跑** —— 那这条 docstring 承诺的「抹平时序差异」
        # 根本没兑现，反而留下一个比原文更明显的计时侧信道（用户不存在：
        # <1ms；用户存在但口令错：240k 轮）。侧信道比文案泄露更值钱，
        # 因为它对**没有账号的人**同样有效。
        ok = verify_password(password, _decoy_hash())
    else:
        ok = verify_password(password, stored)
    if not ok:
        logger.info("authenticate failed for user_id=%s", user_id)
        return None
    assert row is not None
    get_hub().execute(
        "UPDATE users SET last_seen = ? WHERE user_id = ?", (now(), user_id)
    )
    return row["user_id"]


#: 「用户不存在 / 未设口令」时用于抹平时序的诱饵散列。进程内懒生成一次。
#:
#: 懒生成而非模块级常量：构造它要跑一遍 240k 轮 PBKDF2（约几十毫秒），
#: 放在 import 期就是**每个进程启动都付一次**，而绝大多数部署根本不会有
#: 「对不存在的用户登录」这种请求。
_decoy_hash_cache: str | None = None


def _decoy_hash() -> str:
    """返回一份格式完好、无人知其口令的 PBKDF2 散列（只算一次）。"""
    global _decoy_hash_cache
    if _decoy_hash_cache is None:
        from pa_agent.storage.auth import hash_password, new_salt

        _decoy_hash_cache = hash_password(new_salt())   # 口令 = 随机盐，无人知道
    return _decoy_hash_cache


def _validate_user_id(raw: str) -> str:
    """归一 user_id：非空、长度上限、只允许安全字符集。

    与 session_id 同规格 —— 它同样进 SQL（走参数化）与令牌载荷。
    """
    uid = (raw or "").strip()
    if not uid or len(uid) > 64:
        raise ValueError("user_id must be 1..64 characters")
    if not all(c.isalnum() or c in "-_." for c in uid):
        raise ValueError("user_id contains unsupported characters")
    return uid