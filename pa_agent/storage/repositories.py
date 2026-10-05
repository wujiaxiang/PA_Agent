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

    ## ``user_id`` 必须显式传，且**必须**出现在 ``ON CONFLICT`` 的列清单里

    ``record_id`` 是文件 stem，同名复用时走 DO UPDATE 分支。而该分支的列
    清单**曾经不含** ``user_id`` —— 归属只在 INSERT 时生效。后果：一条先以
    admin 身份落库、随后被非 admin 用户复写的记录，归属会**永远卡在 admin**，
    用户看到的仍然是一份空列表。写入方对「这条属于我」的声明必须能改写既有行。

    默认值 ``DEFAULT_USER_ID`` 保留给**一次性导入/播种**路径（那些地方确实
    不知道归属）；生产写入方（``PendingWriter._mirror_to_sqlite``）必须传。
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
            user_id=excluded.user_id,
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
    """删除 DB 副本。磁盘文件由调用方处理（保持既有 DELETE 语义）。

    返回值是「**确实删掉了行**」而不是「SQL 执行成功」—— 走
    ``DatabaseHub.execute_count``。``execute()`` 在 0 行匹配时同样返回 True，
    把它直接当「删干净了」上报，会让跨用户删除（``WHERE user_id`` 不匹配）
    在接口上显示成 ``db_deleted: true``，而索引行原封不动地留着 —— 接口
    撒谎比删不掉更难排查。

    返回类型仍是 ``bool``（不是 ``(ok, rows_deleted)`` 元组）：全仓调用方与
    既有断言都按 ``is True`` 用，改成元组是一次跨模块的契约变更，收益仅是
    多暴露一个本端点用不上的数字。需要行数的调用方改用
    ``get_hub().execute_count(...)`` 即可。
    """
    rows = get_hub().execute_count(
        "DELETE FROM analysis_records WHERE record_id = ? AND user_id = ?",
        (record_id, user_id),
    )
    if rows is None:
        logger.warning(
            "delete_record failed for %s (DB degraded? user_id=%s)", record_id, user_id
        )
        return False
    if rows == 0:
        # 0 行匹配有两种同形的原因：归属不匹配，或行本来就不存在。两者都不该
        # 上报成「删掉了」，故此处与「执行失败」同样返回 False。
        logger.info(
            "delete_record matched 0 rows for %s (user_id=%s) — nothing removed",
            record_id, user_id,
        )
    return rows > 0


def db_has_records(*, user_id: str = DEFAULT_USER_ID) -> bool:
    """是否已有任何记录 —— 导入器用它避免重复全量扫描。"""
    row = get_hub().query_one(
        "SELECT 1 FROM analysis_records WHERE user_id = ? LIMIT 1", (user_id,)
    )
    return row is not None


def get_record_detail(
    *, user_id: str = DEFAULT_USER_ID, file_path: str = ""
) -> dict | None:
    """按 ``file_path`` 取单条记录的**完整载荷**。

    路由侧的 ``record_id`` 是「相对 RECORDS_DIR 的路径、无 .json 后缀」，
    与主键列（文件 stem）不是一回事，故这里用 ``file_path`` 精确匹配。
    按 ``user_id`` 过滤 —— 详情接口此前直接读文件，任何人拿到 URL 都能
    读到别人的分析记录。
    """
    if not file_path:
        return None
    rows = get_hub().query(
        "SELECT * FROM analysis_records WHERE user_id = ? AND file_path = ?",
        (user_id, file_path),
    )
    return dict(rows[0]) if rows else None
