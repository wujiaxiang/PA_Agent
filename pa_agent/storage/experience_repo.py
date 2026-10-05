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
from pathlib import Path
from typing import Any

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, now

logger = logging.getLogger("pa_agent.storage.experience")

#: 与 ``experience_writer.STATUS_DIRS`` 的键一一对应。
#: 这里不 import experience_writer 是为了避免存储层反向依赖写入端。
_VALID_STATUSES = ("success", "failure", "unresolved", "pending")


def _entry_id(path: Path) -> str:
    """条目主键。用相对路径的 stem，保证同一 case 状态流转后 ID 不变。"""
    return path.stem


def upsert_entry(
    content: dict[str, Any],
    *,
    cycle_position: str,
    status: str,
    symbol: str,
    timeframe: str,
    file_path: Path,
    timestamp_ms: int | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> bool:
    """Insert or update one experience entry.

    ``entry_id`` 取 ``file_path.stem`` —— ``ExperienceWriter._write`` 在状态
    流转时**沿用原文件名**（记录时间不因结算而改变），故同一 case 从
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
             content_json, file_path, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            file_path=excluded.file_path,
            updated_at=excluded.updated_at
        """,
        (
            _entry_id(file_path), user_id, st, symbol, timeframe,
            str(content.get("exchange") or ""),
            str(cycle_position or ""),
            int(timestamp_ms if timestamp_ms is not None else content.get("timestamp_ms") or 0),
            _num(content.get("pnl_pct")),
            _num(content.get("entry_price")),
            payload,
            str(file_path),
            ts, ts,
        ),
    )


def _num(v: Any) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def list_entries(
    *,
    user_id: str = DEFAULT_USER_ID,
    status: str | None = None,
    symbol: str = "",
    timeframe: str = "",
    cycle_position: str = "",
    limit: int = 200,
) -> list[dict]:
    """列出经验条目。过滤条件全部可选，空值即不过滤。

    ``status`` 传 ``None`` 表示不限；传具体值时精确匹配。读取端默认只应取
    ``success``/``failure`` —— 未决的 ``pending``/``unresolved`` 不是已验证经验，
    不得当失败经验喂回提示词（AGENTS.md「两阶段状态机」）。
    """
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    if status:
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
    return [
        dict(r)
        for r in get_hub().query(
            "SELECT * FROM experience_entries WHERE "
            + " AND ".join(where)
            + " ORDER BY timestamp_ms DESC LIMIT ?",
            tuple(params),
        )
    ]


def get_entry(entry_id: str, *, user_id: str = DEFAULT_USER_ID) -> dict | None:
    """取单条完整 payload。"""
    row = get_hub().query_one(
        "SELECT content_json FROM experience_entries WHERE entry_id = ? AND user_id = ?",
        (entry_id, user_id),
    )
    if row is None:
        return None
    try:
        return json.loads(row["content_json"])
    except json.JSONDecodeError:
        logger.warning("experience entry %s has corrupt payload_json", entry_id)
        return None


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
    rows = get_hub().query(
        "SELECT status, COUNT(*) AS n FROM experience_entries WHERE "
        + " AND ".join(where)
        + " GROUP BY status",
        tuple(params),
    )
    return {r["status"]: int(r["n"]) for r in rows}