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

**TTL 的续期口径（2026-10-06 落地）**：``expires_at`` 由
:func:`ensure_session` / :func:`touch_session` / :func:`set_cursor` /
:func:`set_view_state` 续期。此前生产路径里**只有** ``POST /api/subscribe``
（``routes_data`` → :func:`set_cursor`）真的在调，而前端 boot 序列
（``loadSettings`` → ``loadBars``）**从不调 subscribe** —— 续期点形同虚设。
实测后果：一个订阅后一直挂着不切品种的 tab，快照行在 30 分钟后被
``get_session()`` 视作不存在（回落全局出厂种子 XAUUSD/15m），用户 F5 后
图表跳回别的标的（这就是「刷新后丢游标」）。

现在补上了请求路径上的续期：``web.api.session_ctx.session_lifecycle_middleware``
（每个 ``/api`` 请求都过）按 **600s 限流**调 :func:`touch_session`。

**限流不是优化，是硬需求**：``/api/bars`` 是**每 5 秒一次**的轮询，裸写就是
每 5 秒一次 SQLite 写；120 个活跃 tab × 12 次/分钟 = 1440 次/分钟。

**续期 ≠ 复活**：:func:`touch_session` 的 UPDATE 带 ``expires_at > ?`` 过滤
（理由见其 docstring）。中间件走的正是它，因此过期语义不会被续期悄悄破坏 ——
「过期」仍然是过期，只是「一直在用」会一直续。

**两层 TTL 的关系（2026-10-06 复核）**：本表（快照）:data:`DEFAULT_TTL_S`
= 24h，内存热层 ``ephemeral.DEFAULT_TTL_S`` = 12h。**快照必须 ≥ 热层**：
反过来的话进程一重启，热层刚清掉的东西快照也过期，游标就再也恢复不了，
重启恢复能力形同虚设。

**两条 ``chat_turns`` DELETE 的分工（2026-10-05 补）**
==================================================
清理孤儿追问有两条 DELETE，**不是同一条的两个条件**：

1. :func:`purge_for_user_sessions` —— 有会话身份但会话已消失的行。判据含
   ``NOT IN (SELECT session_id FROM sessions)``，**必须在 :func:`purge_expired`
   之后**跑（顺序是踩过坑的硬约束，见该函数 docstring）。
2. :func:`purge_anonymous_chat_turns` —— ``session_id = ''`` 的行。这类行**不属于
   孤儿**（没有会话可依附），前者一个都删不到，而它们是「无 ``X-Session-Id``
   调用方」的主要产物。判据**只有时间戳**，与上面那条的顺序**完全解耦**。

GC（:mod:`web.api.storage_gc`）一轮里两步都跑，顺序仍是 ``purge_expired``
在前；第二条即使第一步失败也照跑（它不依赖 ``sessions`` 表）。
"""
from __future__ import annotations

import logging
from typing import Any

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, now

logger = logging.getLogger("pa_agent.storage.sessions")

#: 会话快照空闲多久过期（24h）。
#:
#: **必须 ≥ :data:`pa_agent.storage.ephemeral.DEFAULT_TTL_S`（热层 12h）**：
#: 反过来的话，进程一重启热层刚清掉的会话，其快照也早已过期 —— 「重启后恢复
#: 游标」这个快照存在的唯一理由当场失效（实测：原取值 1800s 与热层相等，
#: 进程重启恰好卡在边界上就会随机丢游标）。
#:
#: 历史取值 1800s 的问题不是「太长」而是「无人续期」：唯一续期点是
#: ``POST /api/subscribe``，而前端 boot 从不调它。现已由
#: ``web.api.session_ctx.session_lifecycle_middleware`` 按 600s 限流续期。
DEFAULT_TTL_S = 86400.0


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
    """仅续期，不改任何业务字段。

    ## 不得复活已过期的行

    UPDATE 带 ``expires_at > ?`` 过滤，是**过滤条件写进 SQL** 而不是
    「调用方先 :func:`get_session` 判一下再写」，理由有二：

    1. **原子性**。SELECT-then-UPDATE 之间存在窗口：``purge_expired``（GC
       守护线程每 10 分钟一轮）可以在两步之间把行删掉，于是续期静默无效；
       反过来更糟 —— 若将来再加一条写入路径，「忘了判」就是一次复活。
       写进 SQL 后，**任何**调用方都自动继承这条不变式。
    2. **返回值本来也分不出来**。``hub.execute()`` 只回答「这条语句有没有
       执行成功」，UPDATE 命中 0 行同样返回 ``True``。调用方先查再写，
       拿到的仍是同一个布尔值，无法据此判断到底续没续上。

    **返回值语义**：``True`` = 语句执行成功，**不代表命中了行**。已过期或根本
    不存在的 ``session_id`` 也返回 ``True``（这与 :func:`ensure_session` /
    :func:`set_cursor` 的既有语义一致）。要判断是否真的续上，请查
    :func:`get_session`（它本身就带 ``expires_at > now`` 过滤）。

    **过期行为什么不续**：过期意味着「读者已经当它不存在了」（``get_session``
    返回 ``None``、``resolve_view`` 回落全局）。此时把它复活，等于让一个
    用户以为「我明明还在用」的行重新出现，而它保存的游标可能来自几小时前的
    另一个标的 —— 这正是「刷新后丢游标」被**静默**化的最坏形态。正确的续期
    入口是**请求路径**（``session_lifecycle_middleware``），它只在用户真的还在
    发请求时才续。
    """
    ts = now()
    return get_hub().execute(
        "UPDATE sessions SET last_seen = ?, expires_at = ?"
        " WHERE session_id = ? AND expires_at > ?",
        (ts, _expiry(ttl_s), session_id, ts),
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
    """删除已过期快照（**只删 ``sessions`` 表**），返回删除行数。

    由 :mod:`web.api.storage_gc` 的守护线程周期调用（每 10 分钟一轮；
    ``/api/health`` 的 ``storage.gc.last.sessions_purged`` 可见其计数）。

    **删除是安全的**：``get_session()`` 早已把 ``expires_at <= now`` 的行当
    「不存在」（惰性过期，与 Redis 一致），故本函数只回收**任何读者都已经
    看不见**的行 —— 它不会让一个还在用的会话凭空消失。真正的 TTL 是 24 小时
    （:data:`DEFAULT_TTL_S`），不是清理间隔。

    **它不删 ``chat_turns``。** 这一点被反复搞错：``chat_turns`` 里存着追问
    历史（``pa_agent.storage.chat_repo`` 写入），而 ``session_id`` 一列正是
    本表的主键 —— 删掉 ``sessions`` 行正是让那些追问**变成孤儿**的动作。
    清孤儿由 :func:`purge_for_user_sessions` 负责，两者必须**按序**跑。
    """
    hub = get_hub()
    ts = now()
    before = hub.query_one("SELECT COUNT(*) AS n FROM sessions")
    n_before = int(before["n"]) if before else 0
    hub.execute("DELETE FROM sessions WHERE expires_at <= ?", (ts,))
    after = hub.query_one("SELECT COUNT(*) AS n FROM sessions")
    n_after = int(after["n"]) if after else 0
    return max(0, n_before - n_after)


def purge_for_user_sessions() -> int:
    """删除孤儿追问记录（``chat_turns``）：其 session_id 已不在 sessions 表中。

    chat_turns 是 L2 持久数据，但按 session 隔离线程。会话过期后这些行
    不该无限堆积 —— 按 TTL 保留一段时间后再清（当前实现：7 天，见下方 SQL）。

    **必须在 :func:`purge_expired` 之后调用**：本函数的判定是
    ``session_id NOT IN (SELECT session_id FROM sessions)``，而
    ``purge_expired`` 才是把过期行从 ``sessions`` 里拿掉的那一步。反过来
    （先清本函数）时行还在 ``sessions`` 里，永远判不出孤儿 —— 表现为
    「每轮都清 0 条」而非报错，极易被误读成「这轮没东西可清」。
    :mod:`web.api.storage_gc` 的 ``run_once()`` 把这两步放在同一分支内按序
    执行，并由单测 ``tests/unit/test_storage_gc.py`` 守护这个顺序。

    **返回值是 0/1 而非行数**：``hub.execute()`` 只回答「这次 DELETE 有没有
    成功执行」。拿它当「清了多少条」看会得到恒为 0 或 1 的假象，故 GC 的
    可观测字段 ``chat_turns_purged`` 由 GC 层用前后两次 ``COUNT(*)`` 的差值
    另行统计（见 ``web.api.storage_gc._measured_delta``），**不取本函数的返回值**。

    **它清不到 ``session_id = ''`` 的行**，那些行由
    :func:`purge_anonymous_chat_turns` 负责。
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


#: 无会话身份行的**绝对**保留期（30 天）。见 :func:`purge_anonymous_chat_turns`。
ANONYMOUS_TURN_RETENTION_S = 30 * 86400.0


def purge_anonymous_chat_turns(
    *,
    retention_s: float = ANONYMOUS_TURN_RETENTION_S,
) -> int:
    """删除**没有会话身份**的追问行（``session_id = ''``），判据**只有时间戳**。

    为什么需要这第二条 DELETE
    =========================
    :func:`purge_for_user_sessions` 的孤儿判定带着 ``session_id != ''``：
    「没有会话身份」的行被**显式排除在孤儿之外**，于是一行都不会被删。而这类行
    恰恰是主要来源 —— 没有 ``X-Session-Id`` 的调用方（直连 API / 脚本 / 老
    客户端）写的全是它们，``chat_repo.append_turn`` 的 ``session_id`` 默认值
    也是 ``''``。实测：写入 6 条 30 天前的 ``session_id=''`` 行，跑完两个
    purge 再跑多轮 GC，仍然一行不少，而 GC 的可观测字段全部报 0、无任何异常 ——
    「不报错但没在清理」的典型形态。

    为什么**不去掉**那个 ``AND session_id != ''``（被否决的方案）
    ==========================================================
    删掉它会让「刚写进来、还没建会话快照」的活跃行**立刻**变成孤儿：
    ``append_turn`` 的默认 ``session_id`` 就是空串，一次在
    :func:`ensure_session` 之前落库的追问（脚本直调、补写、回滚后重试）会被
    同一轮清掉，7 天宽限期是唯一的缓冲。更关键的是判据合并后 WHERE 里
    ``NOT IN (SELECT ... FROM sessions)`` **仍然在** —— 顺序耦合原封不动，
    等于用一个新的误删风险换不到任何解耦。

    本函数的解耦点
    =============
    WHERE 只有 ``session_id = '' AND ts_ms < cutoff``：

    - **不查 ``sessions`` 表** ⇒ 与 :func:`purge_expired` 的先后**完全无关**，
      可单独调用，可在上一步失败时照跑（顺序耦合只属于那条带子查询的 DELETE）；
    - **绝对上限** ⇒ 无身份的行最多活 ``retention_s``，不会「等一个孤儿条件
      成立」而永不回收；
    - 30 天远大于 :data:`DEFAULT_TTL_S`（24 小时）与孤儿宽限期（7 天），
      正常调用方写下的行远达不到这个年龄，**不会误删在用数据**。

    ``retention_s`` 可被调小以便测试。**返回值与兄弟函数同口径**：0/1 表示
    「这次 DELETE 有没有执行成功」，不是删除行数。
    """
    hub = get_hub()
    try:
        retention = max(0.0, float(retention_s))
    except (TypeError, ValueError):
        retention = ANONYMOUS_TURN_RETENTION_S
    res = hub.execute(
        "DELETE FROM chat_turns WHERE session_id = '' AND ts_ms < ?",
        (int((now() - retention) * 1000),),
    )
    return 1 if res else 0
