"""经验库仓储（L2 用户级，跨会话共享）。

**分级理由**：经验库是**累积型知识**，多标签页必须共享同一份 —— A tab 分析出的
BTCUSDT 案例，B tab 看同一标的时就得能读到，否则经验库失去意义
（docs/SESSION_STORAGE_DESIGN.md §2.1、AGENTS.md「经验库范围恒等于当前 K 线」）。

**导入边界**：只扫非点号目录。``experience/.seed_demo_*/`` 是合成数据
（AGENTS.md：pnl_pct 成等差数列、mtime 集中在同一分钟即为合成），
``experience/.omc/`` 是工具状态 —— 两者都**不得进库**，否则会污染检索结果。
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, now

logger = logging.getLogger("pa_agent.storage.experience")

#: ``experience_entries.status`` 的合法取值。
#:
#: **必须与 ``experience_writer.STATUS_*`` 完全一致**（win/loss/pending/
#: unresolved）。这里曾写成 ("success","failure",...)，而写入端发的是
#: "win"/"loss"，于是 ``upsert_entry`` 的兜底分支把它们统统静默改写成
#: "pending" —— 每一条已结算的经验在库里都显示为待验证，检索端因此永远取不到。
#: 一个不会报错、只会让整个经验库悄悄失效的词表错配。
#:
#: 不 import experience_writer 是为了避免存储层反向依赖写入端，所以这份
#: 重复定义由 ``tests/unit/test_experience_repo_status_vocab.py`` 守护。
_VALID_STATUSES = ("pending", "win", "loss", "unresolved")


def _entry_id(stem: str, user_id: str = DEFAULT_USER_ID) -> str:
    """条目主键：``<user_id>_<stem>``。

    **为什么必须带 user_id**：``entry_id`` 是全局 PRIMARY KEY，且
    ``ON CONFLICT ... DO UPDATE SET`` 的列清单里**没有** ``user_id``。曾经
    entry_id 就是裸的 case id，于是两个用户各自产生同一条记录时，第二个写者
    会静默覆盖第一个的 ``content_json``，而归属仍留在原主 —— 无任何报错，
    一方的案例就没了。

    为什么改**取值**而不是改主键：SQLite 不能 ALTER 主键，改成
    ``(user_id, entry_id)`` 复合主键必须重建整张表。改取值零迁移成本。

    仍能保证「状态流转是 UPDATE 而非新增行」：``entry_id`` 由调用方在
    「入场那一刻」生成并随后一直复用，结算只改 status。
    """
    return f"{user_id}_{stem}"


def new_entry_id(user_id: str = DEFAULT_USER_ID) -> str:
    """为一条新的 pending 经验生成主键。

    库内不再有文件名，``entry_id`` 必须**自带唯一性** —— 旧实现靠
    ``<秒级时间戳>_<symbol>_<timeframe>`` 的文件名天然唯一，两用户同秒写同一
    标的会撞。现在用 uuid 后缀彻底消除该碰撞面。
    """
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return _entry_id(f"{stamp}_{uuid.uuid4().hex[:8]}", user_id)


def upsert_entry(
    content: dict[str, Any],
    *,
    cycle_position: str,
    status: str,
    symbol: str,
    timeframe: str,
    entry_id: str,
    timestamp_ms: int | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> bool:
    """Insert or update one experience entry.

    ``entry_id`` 取 ``<user_id>_<file_path.stem>`` —— ``ExperienceWriter._write``
    在状态流转时**沿用原文件名**（记录时间不因结算而改变），故同一 case 从
    ``pending`` 转 ``success`` 时是同一 ID 的 UPDATE，不会产生重复行。
    """
    st = status if status in _VALID_STATUSES else "pending"
    ts = now()
    payload = json.dumps(content, ensure_ascii=False)
    return get_hub().execute(
        """
        INSERT INTO experience_entries
            (entry_id, user_id, status, symbol, timeframe, exchange,
             cycle_position, timestamp_ms, pnl_pct, entry_price,
             content_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(entry_id) DO UPDATE SET
            status=excluded.status,
            symbol=excluded.symbol,
            timeframe=excluded.timeframe,
            exchange=excluded.exchange,
            cycle_position=excluded.cycle_position,
            timestamp_ms=excluded.timestamp_ms,
            pnl_pct=excluded.pnl_pct,
            entry_price=excluded.entry_price,
            content_json=excluded.content_json,
            updated_at=excluded.updated_at
        """,
        (
            entry_id, user_id, st, symbol, timeframe,
            str(content.get("exchange") or ""),
            str(cycle_position or ""),
            int(timestamp_ms if timestamp_ms is not None else content.get("timestamp_ms") or 0),
            _num(content.get("pnl_pct")),
            _num(content.get("entry_price")),
            payload,
            ts, ts,
        ),
    )


def _num(v: Any) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class QueryResult(list):
    """查询结果 + **本次查询**是否失败。

    继承 ``list`` 是为了不改动既有调用方（``len()`` / 迭代 / 下标 / ``== []``
    全部照常）。新增的 ``failed`` / ``error`` 让调用方**不必**在拿到结果后
    再去读 hub 上的标志位 —— 那是一个「查完再问」的时序，在同线程内连续多次
    查询时也会误判（后一次成功会清掉前一次的失败标记）。切读路径尤其怕这个：
    把「读不出来」当成「库里没有」会让经验库静默变空。

    ``db.read_failed`` 已于 P-1 改为线程局部，跨线程污染已消除；这里是
    再加一道 —— 把状态**随结果一起**交出去，从根上不依赖读后时序。
    """

    __slots__ = ("failed", "error")

    def __init__(self, rows: Any = (), *, failed: bool = False, error: str = "") -> None:
        super().__init__(rows)
        self.failed = bool(failed)
        self.error = str(error or "")


def _run(sql: str, params: tuple = ()) -> QueryResult:
    """执行一次读并**当场**捕获失败状态（同一线程、紧跟查询，无时序窗口）。"""
    hub = get_hub()
    rows = hub.query(sql, params)
    return QueryResult(rows, failed=hub.read_failed, error=hub.read_error)


def list_entries(
    *,
    user_id: str = DEFAULT_USER_ID,
    status: str | None = None,
    statuses: Sequence[str] | None = None,
    symbol: str = "",
    timeframe: str = "",
    cycle_position: str = "",
    limit: int = 200,
) -> QueryResult:
    """列出经验条目。过滤条件全部可选，空值即不过滤。

    ``status`` 传 ``None`` 表示不限；传具体值时精确匹配。``statuses`` 是**多值**
    版本，取「成功 + 失败」这类集合过滤时必须用它。

    **为什么需要多值**：读端要的是「两个目录合并后按时间取最新 N 条」，而不是
    「成功取 N 条 + 失败取 N 条」。用单值调两次会得到**最多 2N 条**候选，
    选中集与文件路径的语义不一致，且不会有任何报错 —— 这种偏差要等上线后
    才发现选出来的案例变了。

    读取端默认只应取 ``success``/``failure`` —— 未决的 ``pending``/``unresolved``
    不是已验证经验，不得当失败经验喂回提示词（AGENTS.md「两阶段状态机」）。
    """
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    if statuses:
        wanted = [s for s in statuses if s]
        if wanted:
            where.append(
                "status IN (%s)" % ",".join("?" * len(wanted))
            )
            params.extend(wanted)
    elif status:
        where.append("status = ?")
        params.append(status)
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)
    if cycle_position:
        where.append("cycle_position = ?")
        params.append(cycle_position)
    params.append(int(limit))
    res = _run(
        "SELECT * FROM experience_entries WHERE "
        + " AND ".join(where)
        + " ORDER BY timestamp_ms DESC LIMIT ?",
        tuple(params),
    )
    # sqlite3.Row 只支持 ["key"] 下标，而调用方（含 ExperienceReader 的 DB 路径）
    # 用的是 .get()，故必须转成 dict。失败状态一并带出。
    return QueryResult(
        [dict(r) for r in res], failed=res.failed, error=res.error,
    )


def get_entry(
    entry_id: str, *, user_id: str | None = DEFAULT_USER_ID
) -> dict | None:
    """取单条完整 payload。读失败返回 ``None``（与「不存在」同形，见类文档说明）。

    ``user_id=None`` 表示**不做用户过滤** —— 仅供结算侧使用。``entry_id`` 是
    全局唯一主键，且结算方拿到的 id 来自本进程 ``list_pending()`` 的结果，
    不是外部输入。之所以需要这条：当 user_id 为空时，归属只能从记录里读，
    而归属恰好在 ``content["user_id"]`` 里 —— 不先不过滤地读出来就永远读不到。
    """
    if user_id is None:
        row = _run(
            "SELECT content_json, user_id, status FROM experience_entries WHERE entry_id = ?",
            (entry_id,),
        )
    else:
        row = _run(
            "SELECT content_json, user_id, status FROM experience_entries"
            " WHERE entry_id = ? AND user_id = ?",
            (entry_id, user_id),
        )
    if not row:
        return None
    try:
        payload = json.loads(row[0]["content_json"])
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("experience entry %s has corrupt payload_json", entry_id)
        return None
    if isinstance(payload, dict):
        # 归属与状态都以**库里的列**为准：content 是可被复盘改写的载荷，
        # 不该当身份/状态来源（save() 写出的 content 压根没有 status 字段）。
        payload.setdefault("user_id", str(row[0]["user_id"] or ""))
        payload.setdefault("status", str(row[0]["status"] or ""))
    return payload


def delete_entry(entry_id: str, *, user_id: str = DEFAULT_USER_ID) -> bool:
    return get_hub().execute(
        "DELETE FROM experience_entries WHERE entry_id = ? AND user_id = ?",
        (entry_id, user_id),
    )


def count_by_status(
    *,
    user_id: str = DEFAULT_USER_ID,
    symbol: str = "",
    timeframe: str = "",
) -> dict[str, int]:
    """按状态计数。**过滤条件必须跟着走**，否则前端显示的数字与列表对不上
    （AGENTS.md「cycles 汇总计数必须跟着过滤」）。"""
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)
    result = _run(
        "SELECT status, COUNT(*) AS n FROM experience_entries WHERE "
        + " AND ".join(where)
        + " GROUP BY status",
        tuple(params),
    )
    return {r["status"]: int(r["n"]) for r in result}


# ── 复盘（P3） ─────────────────────────────────────────────────────────────────

def attach_review(
    payload: dict[str, Any],
    *,
    entry_id: str,
    user_id: str = DEFAULT_USER_ID,
    model: str = "",
    verdict: str = "",
    reusable_criteria: str = "",
    source: str = "llm",
) -> bool:
    """写一条复盘。

    **独立表而非 ``content_json`` 里的一个字段**：复盘会重跑（同一交易可能出
    多版结论），独立表才留得住历史与「当时用的是哪个模型」。``content_json``
    是给检索/渲染读的原始档案，不该被复盘反复改写。

    ``source`` 区分 ``program``（结算时程序算的，始终存在、确定性）与
    ``llm``（用户主动触发的语义加深，可重跑）。取用时优先 LLM 版 —— 它是在
    确定性事实之上再加的判断，不是替代。
    """
    return get_hub().execute(
        """
        INSERT INTO experience_reviews
            (entry_id, user_id, model, source, verdict, reusable_criteria,
             payload_json, created_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            entry_id, user_id, str(model or ""), str(source or "llm"),
            str(verdict or ""), str(reusable_criteria or ""),
            json.dumps(payload, ensure_ascii=False),
            now(),
        ),
    )


def program_review(
    entry_id: str, *, user_id: str = DEFAULT_USER_ID
) -> dict | None:
    """该条目的**程序化**复盘（结算时算出的确定性事实）。

    与 :func:`latest_llm_review` 分开取，而不是二选一：MFE / MAE / 触及时机
    是不可辩驳的算术结果，LLM 的结论是**推测**。曾用单条「取最新」且 LLM 优先，
    结果确定性层算出的事实会被猜测层整体覆盖 —— 一个全程未浮盈的单子（MFE=0，
    程序层判「判断与走势相悖」）会因模型写了「判断对了但运气不好」而被采信。
    """
    row = _run(
        """
        SELECT payload_json, model, source, verdict, reusable_criteria, created_at
        FROM experience_reviews
        WHERE entry_id = ? AND user_id = ? AND source = 'program'
        ORDER BY created_at DESC, review_id DESC LIMIT 1
        """,
        (entry_id, user_id),
    )
    return _review_payload(row, entry_id)


def latest_llm_review(
    entry_id: str, *, user_id: str = DEFAULT_USER_ID
) -> dict | None:
    """该条目**最新一版可用**的 LLM 复盘。

    只看**解析成功**的（``verdict <> ''``）：一份不合规格的复盘若被取用，
    它的空 verdict 加上空判据，等于把该有的程序化事实整个顶掉。
    """
    row = _run(
        """
        SELECT payload_json, model, source, verdict, reusable_criteria, created_at
        FROM experience_reviews
        WHERE entry_id = ? AND user_id = ? AND source = 'llm' AND verdict <> ''
        ORDER BY created_at DESC, review_id DESC LIMIT 1
        """,
        (entry_id, user_id),
    )
    return _review_payload(row, entry_id)


def _review_payload(row: Any, entry_id: str) -> dict | None:
    if not row:
        return None
    try:
        payload = json.loads(row[0]["payload_json"])
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("experience review %s has corrupt payload", entry_id)
        return None
    keys = row[0].keys()
    return {
        "model": row[0]["model"],
        "source": row[0]["source"] if "source" in keys else "llm",
        "verdict": row[0]["verdict"],
        "reusable_criteria": row[0]["reusable_criteria"],
        "created_at": row[0]["created_at"],
        "payload": payload if isinstance(payload, dict) else {},
    }


def latest_review(
    entry_id: str, *, user_id: str = DEFAULT_USER_ID
) -> dict | None:
    """兼容入口：程序化优先（确定性事实），LLM 版作为兜底。

    新代码请分别调 :func:`program_review` 与 :func:`latest_llm_review` ——
    本函数返回**单份**记录，不表达「事实 + 推测」的合并语义。
    """
    return program_review(entry_id, user_id=user_id) or latest_llm_review(
        entry_id, user_id=user_id
    )


def delete_reviews(entry_id: str, *, user_id: str = DEFAULT_USER_ID) -> bool:
    return get_hub().execute(
        "DELETE FROM experience_reviews WHERE entry_id = ? AND user_id = ?",
        (entry_id, user_id),
    )
