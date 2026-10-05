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
    """当前请求应当归属的用户。

    UI 暂不做登录 → 恒为 admin。接入鉴权时改为从请求上下文取，
    本函数是唯一的改动点。
    """
    return ADMIN_USER_ID


def get_user(user_id: str = "") -> dict | None:
    row = get_hub().query_one(
        "SELECT * FROM users WHERE user_id = ?", (user_id or ADMIN_USER_ID,)
    )
    return dict(row) if row else None


def list_users() -> list[dict]:
    return [dict(r) for r in get_hub().query("SELECT * FROM users ORDER BY created_at")]