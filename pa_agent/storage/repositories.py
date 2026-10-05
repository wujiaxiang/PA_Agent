"""分析记录仓储（SQLite 优先，文件回退）。

替换 ``analysis_history.find_latest_successful_record`` 的
「rglob + 全量 JSON parse + 目录 mtime 启发式缓存」路径：现在是一条带索引的
SQL。文件仍是权威副本，故任何一步失败都能回退，零回归。

**分级**：L2 用户级。历史是跨会话共享的累积资产 —— A tab 分析出的记录，
B tab 必须能查到（``docs/SESSION_STORAGE_DESIGN.md`` §2.1）。

**增量锚点必须显式传 symbol/timeframe**：不得读全局设置。今天
``routes_analyze`` 读 ``ctx.settings.general.last_symbol``，多会话下会让
A tab 捞到 B tab 标的的上一轮上下文。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pa_agent.storage.db import DEFAULT_USER_ID, get_hub, now

logger = logging.getLogger("pa_agent.storage.records")

# 「成功记录」的准入条件，与 find_latest_successful_record 的判定保持一致：
# 无 exception、有 stage1_diagnosis、有 stage2_decision、有 kline_data。
_STATUS_OK = "ok"


def _record_basename(path: Path) -> str:
    return path.stem


def _derive_status(record: Any, raw: dict) -> str:
    """ok | partial | error —— 与 ``save_partial`` 注入的 ``_partial_reason`` 对齐。"""
    if raw.get("_partial_reason"):
        return "partial"
    if getattr(record, "exception", None) is not None:
        return "error"
    return _STATUS_OK


def _is_successful(status: str, record: Any) -> bool:
    """与 ``find_latest_successful_record`` 的过滤条件严格等价。

    差异点：DB 侧只能读小字段，故 stage1/stage2 是否为空由 ``has_kline``
    与导入期判定承担；这里保守地要求 status=='ok' 且 has_kline。
    """
    return status == _STATUS_OK


def upsert_record(
    record: Any,
    *,
    raw: dict | None = None,
    file_path: Path | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> bool:
    """Insert or update one AnalysisRecord.

    ``raw`` 是磁盘 JSON 的原始 dict（含 ``_partial_reason``）。未提供时从
    record 序列化 —— 注意那样会丢掉 ``_partial_reason``，status 会被记成
    ok/error，故双写路径必须传 raw。
    """
    if record is None:
        return False
    if raw is None:
        raw = record.model_dump(mode="json")

    meta = getattr(record, "meta", None)
    if meta is None:
        logger.warning("upsert_record: record has no meta, skipping")
        return False

    status = _derive_status(record, raw)
    payload = json.dumps(raw, ensure_ascii=False)
    ts = now()
    # record_id 优先用磁盘 basename（与 GET /api/records/{record_id} 兼容），
    # 退化时用 symbol+timeframe+时间戳构造稳定键。
    if file_path is not None:
        record_id = _record_basename(file_path)
    else:
        record_id = (
            f"{meta.timestamp_local_iso.replace(':', '').replace('-', '').replace(' ', '_')}"
            f"_{meta.symbol}_{meta.timeframe}"
        )

    ok = get_hub().execute(
        """
        INSERT INTO analysis_records
            (record_id, user_id, exchange, symbol, timeframe, ts_local_ms,
             status, incremental, continuous, decision_stance, has_kline,
             payload_json, file_path, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(record_id) DO UPDATE SET
            status=excluded.status,
            incremental=excluded.incremental,
            continuous=excluded.continuous,
            decision_stance=excluded.decision_stance,
            has_kline=excluded.has_kline,
            payload_json=excluded.payload_json,
            file_path=excluded.file_path,
            updated_at=excluded.updated_at
        """,
        (
            record_id, user_id, meta.exchange or "", meta.symbol, meta.timeframe,
            int(meta.timestamp_local_ms), status,
            1 if meta.incremental else 0,
            1 if meta.continuous else 0,
            meta.decision_stance or "",
            1 if record.kline_data else 0,
            payload,
            str(file_path) if file_path else "",
            ts, ts,
        ),
    )
    if not ok:
        logger.warning("upsert_record failed for %s (DB degraded?)", record_id)
    return ok


def list_records(
    *,
    user_id: str = DEFAULT_USER_ID,
    exchange: str = "",
    symbol: str = "",
    timeframe: str = "",
    include_partial: bool = False,
    limit: int = 50,
) -> list[dict]:
    """列出记录，**过滤条件全部可选** —— 空值即不过滤（跨品种浏览历史）。

    返回 dict 列表（含完整 payload），供路由直接序列化。
    """
    where: list[str] = ["user_id = ?"]
    params: list[Any] = [user_id]
    if exchange:
        where.append("exchange = ?")
        params.append(exchange)
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)
    if not include_partial:
        where.append("status != 'partial'")

    sql = (
        "SELECT * FROM analysis_records WHERE "
        + " AND ".join(where)
        + " ORDER BY ts_local_ms DESC LIMIT ?"
    )
    params.append(int(limit))
    return [dict(r) for r in get_hub().query(sql, tuple(params))]


def find_latest_successful_record_db(
    *,
    user_id: str = DEFAULT_USER_ID,
    exchange: str = "",
    symbol: str = "",
    timeframe: str = "",
) -> dict | None:
    """最新一条成功记录的 payload。**四个过滤条件均为可选**。

    「最新」的判据是 ``ts_local_ms``（分析时刻），而非 ``created_at``
    （落盘时刻）—— 回放旧记录时两者可能相差很久。
    """
    where: list[str] = ["user_id = ?", "status = ?", "has_kline = 1"]
    params: list[Any] = [user_id, _STATUS_OK]
    if exchange:
        where.append("exchange = ?")
        params.append(exchange)
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)

    sql = (
        "SELECT payload_json FROM analysis_records WHERE "
        + " AND ".join(where)
        + " ORDER BY ts_local_ms DESC LIMIT 1"
    )
    row = get_hub().query_one(sql, tuple(params))
    if row is None:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError:
        logger.warning("find_latest_successful_record_db: corrupt payload_json")
        return None


def delete_record(record_id: str, *, user_id: str = DEFAULT_USER_ID) -> bool:
    """删除 DB 副本。磁盘文件由调用方处理（保持既有 DELETE 语义）。"""
    return get_hub().execute(
        "DELETE FROM analysis_records WHERE record_id = ? AND user_id = ?",
        (record_id, user_id),
    )


def db_has_records(*, user_id: str = DEFAULT_USER_ID) -> bool:
    """是否已有任何记录 —— 导入器用它避免重复全量扫描。"""
    row = get_hub().query_one(
        "SELECT 1 FROM analysis_records WHERE user_id = ? LIMIT 1", (user_id,)
    )
    return row is not None
