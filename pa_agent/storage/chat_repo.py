"""追问历史仓储（L2 持久态 —— 跨重启可查，进程内不持有状态）。

对应 ``chat_turns`` 表（``pa_agent/storage/schema.py``）。

权威性：内存是热态，DB 是持久态，**DB 优先**
==========================================
追问历史此前只活在两处，**都不算持久化**：

1. 内存 ``web.api.routes_chat._chat_sessions`` → ``FreeChatSession._history_full``
   —— 进程重启即丢，且只在同一分桶键（``session_key``）命中时才续得上；
2. JSONL sidecar ``records/pending/**/<record_id>.jsonl`` —— 随分析记录走，
   删记录就没了，且它是「写单边」的审计副本、不是可查询的会话。

本表补的是**第三条**：一个可按 ``thread_key`` 查回的持久副本。

因此三者的定位必须写死，否则后来者会各写各的：

============  ==========================================================
层            定位
============  ==========================================================
内存热态      **继续对话靠它**。``FreeChatSession._cached_prefix`` 在
              ``__init__`` 时按 ``base_record`` 固化且永不变，续上下文
              只能靠 ``_history_full``；重建一个 ``FreeChatSession``
              去吃 DB 历史并不会让模型看到之前那几轮（那条前缀本身
              就是按「本轮首次提问」构造的）。
DB 持久态     **查证与恢复靠它**。回答「上周三那个标的追问过什么」只有
              这里答得出来。
============  ==========================================================

**DB 优先**的具体含义（两条，都不是「内存优先」）：

- **恢复 / 审计 / 跨会话读取一律以 DB 为准**：内存 miss **不代表**历史不存在
  —— 进程刚重启、换了 tab、内存 TTL（``_CHAT_SESSION_TTL_SEC``）刚过期，
  三种情况下内存都是空的，而 DB 里行还在。把内存的空当成「没聊过」，
  追问历史就会在用户眼里凭空消失。
- **DB 不得成为写盘的前置条件**（与 ``trade_repo`` / ``experience_repo``
  同一条硬约束）：``append_turn`` 失败只返回 ``False`` 并记 warning，
  **绝不冒泡** —— 调用点在追问 SSE 的生成线程里，用户已经拿到答案，
  一条审计写失败不该把整次追问变成 ``error`` 事件。

``user_id`` 取 ``db.DEFAULT_USER_ID``（``"admin"``），与其余 L2 仓储一致。

**TTL 归属：不在本模块**
------------------------
会话 TTL 过期后这些行怎么办，**由** ``pa_agent.storage.sessions`` **负责，
本模块刻意不重复实现**。具体地：

- ``sessions.purge_expired()`` **只删** ``sessions`` 表，**不碰** ``chat_turns``；
- ``sessions.purge_for_user_sessions()`` 才是删 ``chat_turns`` 的那个
  （``session_id`` 已不在 ``sessions`` 表中且超过 7 天宽限期）；
- 因此两者必须**按序**跑：先让 ``sessions`` 行消失，``chat_turns`` 才算孤儿。

注意（2026-10-05 核实）：``purge_for_user_sessions`` 目前**没有生产调用者**
（只有定义），即整条清理链尚未接上调度器。在调度器补上之前，这些行只增不减。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, now

logger = logging.getLogger("pa_agent.storage.chat")

#: ``role`` 列的合法取值。与 ``FreeChatSession._history_full`` 的 key 同名。
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
_VALID_ROLES = (ROLE_USER, ROLE_ASSISTANT)

#: 单线程回读上限。与 ``repositories.list_records`` 的 limit 语义一致：
#: 调用方拿不到更多就该视为「历史被截断」，而不是「这就是全部」。
DEFAULT_LIMIT = 200


def _usage_dict(usage: Any) -> dict[str, Any]:
    """Normalise *usage* into a JSON-serialisable dict（缺省全部 0）。

    **绝不接受 0 之外的默认填充**：``free_chat`` 在取消时就是写全 0 的 usage，
    「0 tokens」与「没记录 tokens」在审计上含义不同，故此处只做结构规整，
    不编造数值。
    """
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {str(k): v for k, v in usage.items()}
    # dataclass / SimpleNamespace 形态（AIReply.usage）
    d = getattr(usage, "__dict__", None)
    if isinstance(d, dict):
        return {str(k): v for k, v in d.items()}
    return {}


def append_turn(
    *,
    thread_key: str,
    turn: int,
    user: str,
    assistant: str = "",
    session_id: str = "",
    record_id: str = "",
    symbol: str = "",
    timeframe: str = "",
    reasoning: str | None = None,
    usage: Any = None,
    cancelled: bool = False,
    user_id: str = DEFAULT_USER_ID,
    ts_ms: int | None = None,
) -> bool:
    """Append one follow-up turn (a user row **and** an assistant row).

    *thread_key* 必须是内存侧的**同一个**分桶键（``routes_chat.session_key``），
    否则库里与内存里会长出两条互不相认的「同一段对话」。

    *turn* 是轮次序号，与 ``FreeChatSession._turn`` 同源（首次为 1）。
    调用方从内存会话取，不要另起一套计数 —— 两套计数会让同一段对话在库
    里出现重复轮次，而 ``ix_chat_thread (user_id, thread_key, turn)``
    **不是唯一索引**，写重了不报错，只在回读时顺序错乱。

    **两行必须同一个事务**：``list_turns`` 按 ``turn`` 成对消费，缺一半的
    轮次会让回读方看到「有提问没有回答」。

    **踩坑点**：``db.run_in_tx`` 的返回值就是 ``fn(conn)`` 的返回值 ——
    ``fn`` 返回 ``None`` 时**提交照样成功**，调用方却会把它当成失败
    （已实测：行落了、函数报 False、无任何报错）。故 ``_write`` 必须
    返回一个非 ``None`` 的哨兵，判据是 ``is not True`` 而不是 ``is None``。

    返回 True/False，**永不抛出** —— 见模块 docstring 的「DB 不得成为
    写盘的前置条件」。
    """
    key = str(thread_key or "").strip()
    if not key:
        logger.warning("chat append skipped: empty thread_key")
        return False
    try:
        turn_no = int(turn)
    except (TypeError, ValueError):
        logger.warning("chat append skipped: non-int turn %r", turn)
        return False

    stamp = int(ts_ms) if ts_ms is not None else int(now() * 1000)
    payload_usage = json.dumps(_usage_dict(usage), ensure_ascii=False)
    cancelled_flag = 1 if cancelled else 0

    def _write(conn: sqlite3.Connection) -> bool:
        # 返回 True 而非 None —— 见 docstring 的「踩坑点」。
        conn.execute(
            """
            INSERT INTO chat_turns
                (user_id, session_id, thread_key, record_id, symbol, timeframe,
                 turn, ts_ms, role, content, reasoning, usage_json, cancelled)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id, str(session_id or ""), key, str(record_id or ""),
                str(symbol or ""), str(timeframe or ""),
                turn_no, stamp, ROLE_USER, str(user or ""), None, "{}", cancelled_flag,
            ),
        )
        conn.execute(
            """
            INSERT INTO chat_turns
                (user_id, session_id, thread_key, record_id, symbol, timeframe,
                 turn, ts_ms, role, content, reasoning, usage_json, cancelled)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id, str(session_id or ""), key, str(record_id or ""),
                str(symbol or ""), str(timeframe or ""),
                turn_no, stamp, ROLE_ASSISTANT, str(assistant or ""),
                str(reasoning) if reasoning else None,
                payload_usage, cancelled_flag,
            ),
        )
        return True

    # ``db.run_in_tx`` 只捕获 ``sqlite3.Error``：连接池耗尽、hook 注入、
    # 未来某个非 SQLite 异常都会**直接冒泡**给调用方，而本函数的契约是
    # 「永不抛出」。契约必须由自己兜住，不能指望下游（``routes_chat``
    # 确实也有一层 try/except，但那是双保险，不是契约）。
    try:
        committed = get_hub().run_in_tx(_write)
    except Exception:  # noqa: BLE001
        logger.warning(
            "chat turn %d raised while persisting thread %s (ignored)",
            turn_no, key, exc_info=True,
        )
        return False
    if committed is not True:
        logger.warning("chat turn %d not persisted for thread %s (DB degraded?)",
                       turn_no, key)
        return False
    return True


def list_turns(
    thread_key: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """列出某个追问线程的全部消息行，按 ``(turn, id)`` 升序。

    **一「轮」是两行**（user + assistant），按 ``turn`` 分组即得对话。
    走 ``ix_chat_thread``，方向与索引一致。

    返回行里的 ``usage_json`` 已解析成 ``usage`` dict 并移除原列 ——
    调用方要的是数值而不是字符串。

    读失败与「确实没有」同形返回 ``[]``：追问不是资金面主链路，
    为它引入 ``experience_repo.QueryResult`` 那套失败标志并不划算
    （真正的权威副本仍在内存与 JSONL sidecar）。
    """
    key = str(thread_key or "").strip()
    if not key:
        return []
    rows = get_hub().query(
        "SELECT * FROM chat_turns "
        "WHERE user_id = ? AND thread_key = ? "
        "ORDER BY turn ASC, id ASC LIMIT ?",
        (user_id, key, int(limit)),
    )
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        raw = row.pop("usage_json", "") or "{}"
        try:
            row["usage"] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("chat turn %s has corrupt usage_json", row.get("id"))
            row["usage"] = {}
        out.append(row)
    return out


def clear_thread(
    thread_key: str,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> bool:
    """删除某个追问线程的**全部**行。返回是否执行成功。

    **不碰内存会话**：内存热态由 ``routes_chat`` 的 TTL 与断连逻辑管，
    两者是独立的生命周期。清库不清内存会在几秒内被内存重新写回一轮，
    那属于正常续聊而不是 bug。

    删不存在的行不算失败（与 ``trade_repo.delete_trade`` 同口径）：
    ``execute`` 只报告 SQL 是否执行，不报告命中行数。
    """
    key = str(thread_key or "").strip()
    if not key:
        return False
    return get_hub().execute(
        "DELETE FROM chat_turns WHERE user_id = ? AND thread_key = ?",
        (user_id, key),
    )
