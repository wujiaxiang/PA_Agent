"""历史分析记录查询 API。

提供三个端点：
- ``GET /api/records`` — 列出指定 (exchange, symbol, timeframe) 下的历史记录摘要。
- ``GET /api/records/{record_id}`` — 获取单条记录详情（已脱敏）。
- ``DELETE /api/records/{record_id}`` — 删除单条记录。

同时支持新分区布局 (``records/pending/{exchange}/{symbol}/{timeframe}/{ts}.json``)
和旧平铺布局 (``records/pending/{ts}_{symbol}_{timeframe}.json``)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from pa_agent.records.pending_writer import PendingWriter, _safe_path_segment
from pa_agent.records.schema import AnalysisRecord
# 复用 SSE done 事件的序列化逻辑，保证历史记录回看与实时分析字段一致
from web.api.routes_analyze import _serialize_record

logger = logging.getLogger("pa_agent.web.records")

router = APIRouter(tags=["records"])

# 记录根目录（相对于项目根）。作为模块级常量便于测试 monkeypatch。
RECORDS_DIR = Path(__file__).resolve().parents[2] / "records" / "pending"


def _terminal_outcome(stage2_decision: dict | None) -> str | None:
    """从 stage2_decision.terminal.outcome 提取终态结果。"""
    if not stage2_decision:
        return None
    terminal = stage2_decision.get("terminal")
    if isinstance(terminal, dict):
        return terminal.get("outcome")
    return None


def _looks_like_iso_datetime(s) -> bool:
    """判断字符串是否看起来像 ISO 时间，而非 K 线代号（如 "K1"/"K50-K1"）。

    用 ``datetime.fromisoformat`` 尝试解析，成功才返回 True。这能挡住
    "K1" 这类由阶段一 ``bar_analysis.last_closed_bar`` 返回的代号——它们
    不是时间字符串，前端 ``new Date("K1")`` 会得到 Invalid Date。
    """
    if not s or not isinstance(s, str):
        return False
    try:
        datetime.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False


def _derive_anchor_bar_ts_ms(record: AnalysisRecord) -> int:
    """Authoritative ts_open (ms) of the record's last **closed** bar.

    Derived from the record's own ``kline_data`` rather than from the stored
    ``meta.last_close_bar_iso``:

    * ``kline_data`` is immutable history, so the answer never drifts.
    * Records written before the off-by-one fix still carry a stale ISO value
      baked into their JSON. Re-deriving here repairs those at read time
      instead of rewriting history on disk.

    ``bars[0]`` is not always the forming bar — a closed market or a snapshot
    without one puts an already-closed bar at index 0. Index 1 then points a
    full bar into the past.
    """
    from pa_agent.orchestrator.two_stage import _pick_last_closed_bar

    bar = _pick_last_closed_bar(getattr(record, "kline_data", None))
    if not isinstance(bar, dict):
        return 0
    try:
        return int(bar.get("ts_open") or bar.get("time") or 0)
    except (TypeError, ValueError):
        return 0


def _derive_last_close_bar_iso(record: AnalysisRecord) -> str:
    """提取 last_close_bar_iso，优先 meta；为空则从 kline_data / stage1 派生。

    派生链：
      1. ``record.meta.last_close_bar_iso``（新记录由 orchestrator 写入）
      2. ``record.kline_data[1].ts_open``（ms 时间戳）→ 本地 ISO 字符串；
         kline_data 是 newest-first，bars[0] = forming bar，bars[1] = K1（刚收盘）；
         兼容旧字段 ``time``
      3. ``record.stage1_diagnosis.bar_analysis.last_closed_bar``（直接字符串），
         但必须通过 ISO 时间格式校验，过滤 "K1" 这类 K 线代号
      4. 全部失败则返回 ""（前端不渲染 close bar span）
    """
    last_close_bar_iso = getattr(record.meta, "last_close_bar_iso", "") or ""
    if last_close_bar_iso:
        return last_close_bar_iso

    ts_ms = _derive_anchor_bar_ts_ms(record)
    if ts_ms:
        try:
            return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            pass

    if record.stage1_diagnosis:
        try:
            bar_analysis = record.stage1_diagnosis.get("bar_analysis", {}) or {}
            lcb = bar_analysis.get("last_closed_bar", "")
            # 必须校验是 ISO 时间格式，过滤 "K1"/"K50-K1" 这类 K 线代号
            if lcb and _looks_like_iso_datetime(lcb):
                return str(lcb)
        except (TypeError, ValueError, AttributeError):
            pass

    return ""


def _glob_partitioned(
    exchange: str, symbol: str, timeframe: str, limit: int
) -> list[Path]:
    """三个过滤条件齐全时的快路径：直接定位分区目录。行为与改造前一致。"""
    target_dir = (
        RECORDS_DIR
        / _safe_path_segment(exchange)
        / _safe_path_segment(symbol)
        / _safe_path_segment(timeframe)
    )
    files: list[Path] = list(target_dir.glob("*.json")) if target_dir.exists() else []

    # 旧平铺布局: records/pending/{timestamp}_{symbol}_{timeframe}.json
    for f in RECORDS_DIR.glob("*.json"):
        if f.name.endswith(f"_{symbol}_{timeframe}.json"):
            files.append(f)

    # 去重（按解析后的绝对路径）
    seen: set[str] = set()
    unique_files: list[Path] = []
    for f in files:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            unique_files.append(f)

    # 按 mtime 倒序
    unique_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return unique_files[:limit]


def _browse_filtered(
    exchange: str, symbol: str, timeframe: str, limit: int
) -> list[Path]:
    """缺任一过滤条件时的路径：SQLite 优先，DB 未命中/不可用则回退全扫描。

    分区目录需要三者齐全才能定位，所以部分过滤（如只给 symbol）无法走
    ``_glob_partitioned``。SQLite 的索引天然支持可选过滤条件，故优先用它。
    """
    from pa_agent.records.analysis_history import list_record_paths, load_record

    # ① SQLite：有索引，跨品种浏览的主要快路径
    try:
        from pa_agent.storage.repositories import list_records as db_list

        rows = db_list(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
        )
        paths = [Path(r["file_path"]) for r in rows if r.get("file_path")]
        paths = [p for p in paths if p.is_file()]
        if paths:
            return paths
    except Exception:  # noqa: BLE001
        logger.debug("SQLite browse failed, falling back to full scan", exc_info=True)

    # ② 回退：全扫描 + 按记录 meta 过滤（DB 未导入或已降级时）
    paths = list_record_paths(RECORDS_DIR)
    if not (exchange or symbol or timeframe):
        return paths[:limit]

    out: list[Path] = []
    for p in paths:
        if len(out) >= limit:
            break
        rec = load_record(p)
        if rec is None:
            continue
        meta = rec.meta
        if exchange and (meta.exchange or "") != exchange:
            continue
        if symbol and meta.symbol != symbol:
            continue
        if timeframe and meta.timeframe != timeframe:
            continue
        out.append(p)
    return out


def _list_records(
    exchange: str,
    symbol: str,
    timeframe: str,
    limit: int,
    include_partial: bool,
) -> list[dict]:
    """列出记录摘要，按 mtime 倒序、去重、截断到 limit。

    ``exchange``/``symbol``/``timeframe`` **全部可选**：三者皆空时跨全部品种
    浏览（历史是 L2 用户级共享资产，A tab 分析出的记录 B tab 也要能查到，
    见 docs/SESSION_STORAGE_DESIGN.md §2.1）。给定过滤条件时行为与改造前一致。
    损坏的记录文件被静默跳过。

    两条路径：
    - **三个过滤条件齐全** → 分区目录 glob（既有快路径，行为完全不变）
    - **缺任意一个** → 无法用分区路径定位，改用 SQLite 索引查询；
      DB 未命中或不可用时回退全扫描 + 按记录 meta 过滤
    """
    if not RECORDS_DIR.exists():
        return []

    if exchange and symbol and timeframe:
        candidates = _glob_partitioned(exchange, symbol, timeframe, limit)
    else:
        candidates = _browse_filtered(exchange, symbol, timeframe, limit)

    result: list[dict] = []
    for f in candidates:
        try:
            with f.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            # _partial_reason 由 save_partial 注入，不在 Pydantic schema 内（extra=forbid），
            # 必须先弹出再校验。
            partial_reason = data.pop("_partial_reason", None)
            record = AnalysisRecord.model_validate(data)
        except Exception:
            continue  # 跳过损坏的记录文件

        # include_partial=False 时跳过失败记录
        if not include_partial and record.exception is not None:
            continue

        # record_id: 相对 RECORDS_DIR 的路径（去掉 .json）。
        # 强制使用正斜杠以便作为 URL 路径段（Windows 上 Path 会用反斜杠）。
        rel = f.relative_to(RECORDS_DIR)
        record_id = str(rel.with_suffix("")).replace("\\", "/")

        s2 = record.stage2_decision
        # 修复：Stage2 实际结构为 {decision: {order_type, order_direction, ...}, ...}
        # 直接 s2.get("order_type") 会返回 None，需从 .decision 子对象读取。
        # 参考：tests/integration/test_gate_shortcircuit.py:54 验证嵌套结构。
        s2_decision_inner = s2.get("decision") if isinstance(s2, dict) else None
        result.append({
            "record_id": record_id,
            "timestamp": record.meta.timestamp_local_iso,
            # 跨品种浏览时前端不知道每条记录属于哪个标的，必须回传这三个字段
            # —— 否则「历史数据都能看」拿到的是一堆无法区分的条目。
            "symbol": record.meta.symbol,
            "timeframe": record.meta.timeframe,
            "exchange": record.meta.exchange,
            "order_type": s2_decision_inner.get("order_type") if isinstance(s2_decision_inner, dict) else None,
            "direction": s2_decision_inner.get("order_direction") if isinstance(s2_decision_inner, dict) else None,
            "terminal_outcome": _terminal_outcome(s2),
            "partial_reason": partial_reason,
            "has_exception": record.exception is not None,
            "last_close_bar_iso": _derive_last_close_bar_iso(record),
            # 权威锚点（ms）。前端回放视窗与方向箭头优先用它，而不是
            # 可能已烙进旧记录 JSON 的 last_close_bar_iso。
            "anchor_bar_ts_ms": _derive_anchor_bar_ts_ms(record),
            "incremental": getattr(record.meta, "incremental", False),
            "continuous": getattr(record.meta, "continuous", False),
        })
    return result


@router.get("/records")
async def list_records(
    exchange: str = Query("", description="交易所，如 GATEIO；留空=不过滤"),
    symbol: str = Query("", description="品种，如 BTCUSDT；留空=不过滤"),
    timeframe: str = Query("", description="周期，如 1d；留空=不过滤"),
    limit: int = Query(50, ge=1, le=500, description="返回数量上限"),
    include_partial: bool = Query(False, description="是否包含失败记录"),
):
    """列出历史记录摘要。

    三个过滤条件**均为可选**：留空即跨全部品种返回 —— 历史记录是 L2 用户级
    共享资产，多标签页必须都能看到全量历史（docs/SESSION_STORAGE_DESIGN.md §2.1）。
    传入过滤条件时行为与改造前完全一致。
    """
    # Offloaded: _list_records globs the partitions, stats and JSON-parses every
    # candidate (records embed full stage1+stage2 payloads). Pure blocking file
    # I/O — inline it stalls the event loop and every SSE stream.
    return await asyncio.to_thread(
        _list_records, exchange, symbol, timeframe, limit, include_partial
    )


@router.get("/records/{record_id:path}")
async def get_record(record_id: str, request: Request):
    """获取单条记录详情（已脱敏）。

    record_id 为相对 RECORDS_DIR 的路径（无 .json 后缀），例如
    ``GATEIO/BTCUSDT/1d/2026-07-18_14-00-13`` 或旧布局的
    ``2026-07-18_14-00-13_BTCUSDT_1d``。
    """
    # 路径遍历防护：复用 helper（含 .. / 绝对路径检测）
    target = _validate_record_id(record_id)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Record not found")

    try:
        with target.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load record: {e}")

    # 校验 schema（弹出 _partial_reason 以兼容 extra=forbid）
    partial_reason = data.pop("_partial_reason", None)
    try:
        record = AnalysisRecord.model_validate(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Invalid record schema: {e}")

    # 关键：调用 _serialize_record 而非 model_dump，与 SSE done 事件保持一致，
    # 派生 decision_tree / decision_overlay / raw_debug_payload / debug_files_payload
    # 等前端 render 函数依赖的字段（否则 replayRecord 回显会拿不到数据）。
    result = _serialize_record(record)

    # 回填 _partial_reason（非 schema 字段，但前端可能需要）
    if partial_reason is not None:
        result["_partial_reason"] = partial_reason
    # 权威锚点：从记录自身的 kline_data 现算，顺带修正旧记录里
    # 已烙进 JSON 的差一根 last_close_bar_iso（见 _derive_anchor_bar_ts_ms）
    result["anchor_bar_ts_ms"] = _derive_anchor_bar_ts_ms(record)
    result["last_close_bar_iso"] = _derive_last_close_bar_iso(record)

    # 脱敏：复用 PendingWriter._sanitize，用当前 ctx 的 api_key 作为防御性二次脱敏
    # （磁盘上的记录在保存时已脱敏；此处针对未脱敏的遗留记录做兜底）。
    api_key = ""
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is not None:
        try:
            api_key = ctx.settings.provider.api_key or ""
        except AttributeError:
            api_key = ""

    sanitized = PendingWriter._sanitize(result, api_key)
    return sanitized


def _validate_record_id(record_id: str) -> Path:
    """校验 record_id 并返回解析后的目标文件路径（RECORDS_DIR / f"{record_id}.json"）。

    防护逻辑：
    1. 拒绝包含 ``..`` 段的路径（防路径遍历）。
    2. 解析后的绝对路径必须仍在 RECORDS_DIR 下（双重防护，含绝对路径检测）。

    任何违规均抛出 HTTPException(400, "Invalid record_id")。
    """
    if ".." in record_id.split("/"):
        raise HTTPException(status_code=400, detail="Invalid record_id")

    target = (RECORDS_DIR / f"{record_id}.json").resolve()
    # 双重防护：解析后的路径必须仍在 RECORDS_DIR 下
    try:
        target.relative_to(RECORDS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid record_id")

    return target


@router.delete("/records/{record_id:path}")
async def delete_record(record_id: str):
    """删除单条记录文件。

    成功返回 ``{ok: true, record_id}``；文件不存在返回 404；
    record_id 含路径遍历或绝对路径返回 400。
    """
    # 路径遍历防护：复用 helper（含 .. / 绝对路径检测）
    target = _validate_record_id(record_id)

    if not target.exists():
        raise HTTPException(status_code=404, detail="record not found")

    try:
        target.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete record: {e}")

    return {"ok": True, "record_id": record_id}
