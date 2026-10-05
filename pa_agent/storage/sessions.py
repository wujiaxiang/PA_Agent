"""会话仓储（L3，缓存级快照）。

**分级**：L3 会话级。只存「游标 + 视图模式 + 运行时开关」，且**带 TTL 过期即清**。

与内存热层（``ephemeral.SessionRegistry``）的关系是**写穿透**：

    请求 → registry.get_or_create(sid)        # 热层，命中即返回
            └─ miss → 读本表 → 命中则回填热层
                     └─ miss → 建默认行
    变更 → 内存立即 + 本表持久（快照）
    过期 → sweep() 清热层；本表按 expires_at 索引批量清
    重启 → 从本表恢复游标；运行时开关刻意不还原（那是缓存语义）

**为什么不把会话塞进 Redis**：见 ``docs/SESSION_STORAGE_DESIGN.md`` §1。
本表提供持久快照，热层提供 Redis 语义，外部依赖为零。
"""
from __future__ import annotations

import logging
from typing import Any

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, now

logger = logging.getLogger("pa_agent.storage.sessions")

#: 会话空闲多久过期。与 ephemeral.DEFAULT_TTL_S 保持一致。
DEFAULT_TTL_S = 1800.0


def _expiry(ttl_s: float | None) -> float:
    return now() + (ttl_s if ttl_s is not None else DEFAULT_TTL_S)


def ensure_session(
    session_id: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    ttl_s: float | None = None,
) -> dict[str, Any]:
    """创建或续期一行会话快照。返回该行 dict。

    幂等：已存在则只更新 ``last_seen`` / ``expires_at``，**不动游标**
    —— 每次请求都续期是正常行为，不该把用户设的品种刷掉。
    """
    hub = get_hub()
    ts = now()
    row = hub.query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    if row is None:
        hub.execute(
            """
            INSERT INTO sessions
                (session_id, user_id, symbol, timeframe, exchange, data_mode,
                 replay_record_id, keep_analysis, wait_close,
                 created_at, last_seen, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (session_id, user_id, "", "", "", "live", "", 0, 0, ts, ts, _expiry(ttl_s)),
        )
        row = hub.query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        return dict(row) if row else {}

    hub.execute(
        "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE session_id = ?",
        (ts, _expiry(ttl_s), session_id),
    )
    return dict(row)


def touch_session(session_id: str, *, ttl_s: float | None = None) -> bool:
    """仅续期，不改任何业务字段。"""
    return get_hub().execute(
        "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE session_id = ?",
        (now(), _expiry(ttl_s), session_id),
    )


def get_session(session_id: str) -> dict[str, Any] | None:
    """读取会话快照。已过期视同不存在（惰性过期，与 Redis 一致）。"""
    row = get_hub().query_one(
        "SELECT * FROM sessions WHERE session_id = ? AND expires_at > ?",
        (session_id, now()),
    )
    return dict(row) if row else None


def _ensure_row(session_id: str, user_id: str, ttl_s: float | None) -> None:
    """确保会话快照行存在，并续期。

    写入方（``set_cursor`` / ``set_view_state``）都是 UPDATE 语义，行不存在时
    会**静默空操作** —— 游标存不进去且不报错。故所有写入前先过这里。
    """
    ts = now()
    get_hub().execute(
        """
        INSERT INTO sessions
            (session_id, user_id, symbol, timeframe, exchange, data_mode,
             replay_record_id, keep_analysis, wait_close,
             created_at, last_seen, expires_at)
        VALUES (?,?,'','','','live','',0,0,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            last_seen=excluded.last_seen, expires_at=excluded.expires_at
        """,
        (session_id, user_id, ts, ts, _expiry(ttl_s)),
    )


def set_cursor(
    session_id: str,
    *,
    symbol: str,
    timeframe: str,
    exchange: str = "",
    ttl_s: float | None = None,
) -> bool:
    """写游标 —— 今天这三个值存在全局 ``settings.json`` 里，是切 tab 互踩的根源。"""
    _ensure_row(session_id, DEFAULT_USER_ID, ttl_s)
    return get_hub().execute(
        """
        UPDATE sessions
           SET symbol = ?, timeframe = ?, exchange = ?,
               last_seen = ?, expires_at = ?
         WHERE session_id = ?
        """,
        (symbol, timeframe, exchange, now(), _expiry(ttl_s), session_id),
    )


def set_view_state(
    session_id: str,
    *,
    data_mode: str | None = None,
    replay_record_id: str | None = None,
    keep_analysis: bool | None = None,
    wait_close: bool | None = None,
    ttl_s: float | None = None,
) -> bool:
    """写视图模式 / 运行时开关。只更新显式传入的字段（COALESCE 语义）。

    ``keep_analysis`` / ``wait_close`` 是**运行时**开关：刻意不跨重启还原
    —— 重启后自动继续跑分析会造成「没人盯着却在烧 token」。
    """
    sets: list[str] = []
    params: list[Any] = []
    if data_mode is not None:
        sets.append("data_mode = ?")
        params.append(data_mode)
    if replay_record_id is not None:
        sets.append("replay_record_id = ?")
        params.append(replay_record_id)
    if keep_analysis is not None:
        sets.append("keep_analysis = ?")
        params.append(1 if keep_analysis else 0)
    if wait_close is not None:
        sets.append("wait_close = ?")
        params.append(1 if wait_close else 0)
    if not sets:
        return False
    # 同 set_cursor：UPDATE 命中 0 行时必须先建行，否则静默丢失开关状态
    _ensure_row(session_id, DEFAULT_USER_ID, ttl_s)
    params.append(now())
    params.append(session_id)
    return get_hub().execute(
        f"UPDATE sessions SET {', '.join(sets)}, last_seen = ? WHERE session_id = ?",
        tuple(params),
    )


def drop_session(session_id: str) -> bool:
    """删除会话快照。tab 关闭时调用。"""
    return get_hub().execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def purge_expired() -> int:
    """删除已过期快照，返回删除行数。由 housekeeping 周期调用。"""
    hub = get_hub()
    ts = now()
    before = hub.query_one("SELECT COUNT(*) AS n FROM sessions")
    n_before = int(before["n"]) if before else 0
    hub.execute("DELETE FROM sessions WHERE expires_at <= ?", (ts,))
    after = hub.query_one("SELECT COUNT(*) AS n FROM sessions")
    n_after = int(after["n"]) if after else 0
    return max(0, n_before - n_after)


def purge_for_user_sessions() -> int:
    """删除孤儿追问记录：其 session_id 已不在 sessions 表中。

    chat_turns 是 L2 持久数据，但按 session 隔离线程。会话过期后这些行
    不该无限堆积 —— 按 TTL 保留一段时间后再清。
    """
    hub = get_hub()
    res = hub.execute(
        """
        DELETE FROM chat_turns
         WHERE session_id != ''
           AND session_id NOT IN (SELECT session_id FROM sessions)
           AND ts_ms < ?
        """,
        (int((now() - 7 * 86400) * 1000),),
    )
    return 1 if res else 0
