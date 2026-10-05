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
import os
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


def _loads(raw: object) -> dict | None:
    """解析 payload_json。坏行跳过，不让单条脏数据毁掉整批。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _request_user_id(request: Request) -> str:
    """本次请求的 user_id，取不到回落默认用户。"""
    try:
        from web.api.auth_ctx import current_user_id

        return current_user_id(request)
    except Exception:  # noqa: BLE001
        return _default_user_id()


def _record_id_from_row(row: dict) -> str:
    """DB 行 → URL 用的 record_id（RECORDS_DIR 下的相对路径，去掉 .json）。

    保持与改造前**逐字一致** —— 前端把它当 URL 路径段回放，改格式等于
    让所有历史回看点不动。``file_path`` 不在 RECORDS_DIR 下时（目录被
    monkeypatch、跨机迁移等）退回用主键列，至少不会 404。
    """
    fp = str(row.get("file_path") or "")
    if fp:
        try:
            f = Path(fp)
            rel = f.resolve().relative_to(RECORDS_DIR.resolve())
            return str(rel.with_suffix("")).replace("\\", "/")
        except (ValueError, OSError):
            pass
    return str(row.get("record_id") or "")


def _list_records(
    exchange: str,
    symbol: str,
    timeframe: str,
    limit: int,
    include_partial: bool,
    user_id: str = "",
) -> list[dict]:
    """列出记录摘要。**只查库，不再读任何文件。**

    2026-10-05 起分析记录与经验库对齐：库是唯一真源，正文取 ``payload_json``。
    这顺带补上一个真实的用户隔离漏洞 —— 旧的 ``_file_candidates`` 自愈回退
    按 exchange/symbol/timeframe 扫盘，**唯独没有 user_id 判断**：DB 一抖动或
    刚播种完没数据就走那条路，于是 A 能看到 B 的历史记录。而磁盘上的 JSON
    本身也不含任何用户标记，无法事后补救。

    ``exchange``/``symbol``/``timeframe`` **全部可选**：三者皆空时跨全部品种
    浏览（历史是 L2 用户级共享资产，见 docs/SESSION_STORAGE_DESIGN.md §2.1）。
    """
    from pa_agent.storage.repositories import list_records as db_list

    try:
        rows = db_list(
            user_id=user_id or _default_user_id(),
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            include_partial=include_partial,
            limit=limit,
        )
    except Exception:  # noqa: BLE001
        logger.warning("records browse failed", exc_info=True)
        return []

    result: list[dict] = []
    for row in rows:
        data = _loads(row.get("payload_json"))
        if data is None:
            continue
        # _partial_reason 由 save_partial 注入，不在 Pydantic schema 内
        # （extra=forbid），必须先弹出再校验。
        partial_reason = data.pop("_partial_reason", None)
        try:
            record = AnalysisRecord.model_validate(data)
        except Exception:
            continue  # 跳过损坏的载荷

        if not include_partial and record.exception is not None:
            continue

        s2 = record.stage2_decision
        # Stage2 实际结构为 {decision: {order_type, order_direction, ...}, ...}；
        # 直接 s2.get("order_type") 会得到 None。
        s2_decision_inner = s2.get("decision") if isinstance(s2, dict) else None
        result.append({
            "record_id": _record_id_from_row(row),
            "timestamp": record.meta.timestamp_local_iso,
            # 跨品种浏览时前端不知道每条记录属于哪个标的，必须回传这三个字段
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


def _default_user_id() -> str:
    from pa_agent.storage.db import DEFAULT_USER_ID

    return DEFAULT_USER_ID


# ── E2E 专用：播种一条可回看的合成记录 ────────────────────────────────────
# 端到端测试需要一条「历史记录」才能走回看链路，而 CI 是空库。
#
# **为什么必须由服务端播种**：宿主机与容器是两套文件系统视图
# （宿主 /root/.../records/pending vs 容器 /app/records/pending），
# 同一 inode 但路径不同。由服务端进程写盘、用它自己的 RECORDS_DIR、
# 走它自己的 upsert，才是对的。
#
# 安全：仅在 `PA_AGENT_E2E=1` 时注册路由，生产环境该路由**根本不存在**；
# 记录文件名固定带 `__e2e_seed` 便于识别与清理。
_E2E_ENABLED = os.environ.get("PA_AGENT_E2E", "") == "1"

if _E2E_ENABLED:  # pragma: no cover - 仅 E2E 环境

    @router.post("/records/__e2e_seed__")
    async def e2e_seed_record(
        exchange: str = "GATEIO",
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
    ) -> dict:
        """写入一条结构完整的合成记录，供 E2E 走回看链路。仅 E2E 环境可用。"""
        now = datetime.now(timezone.utc)
        # _safe_path_segment() 返回 str（不是 Path），不能直接用 / 串联
        part = "/".join(
            (
                _safe_path_segment(exchange),
                _safe_path_segment(symbol),
                _safe_path_segment(timeframe),
            )
        )
        target = RECORDS_DIR / part
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{now:%Y-%m-%d_%H-%M-%S}__e2e_seed.json"

        record = {
            "meta": {
                "timestamp_local_iso": now.isoformat(),
                "timestamp_local_ms": int(now.timestamp() * 1000),
                "exchange": exchange, "symbol": symbol, "timeframe": timeframe,
                "bar_count": 200,
                "ai_provider": {"model": "e2e-seed"},
                "decision_stance": "balanced",
                "incremental": False, "continuous": False,
                "last_close_bar_iso": now.isoformat(),
            },
            "kline_data": [], "htf_text": "",
            "stage1_messages": [], "stage1_response": {},
            "stage1_diagnosis": {
                "cycle_position": "normal_channel", "direction": "bullish",
                "detected_patterns": ["e2e_seed_pattern"],
                "diagnosis_confidence": 60,
                "bar_by_bar_summary": ["E2E 播种记录"],
            },
            "stage2_messages": [], "stage2_response": {},
            "stage2_decision": {
                "decision": {
                    "order_type": "限价单", "order_direction": "做多",
                    "entry_price": 85000.0, "take_profit_price": 86000.0,
                    "take_profit_price_2": 87000.0, "stop_loss_price": 84000.0,
                    "trade_confidence": 70,
                    "reasoning": "E2E 播种记录：用于验证模式切换时的面板重置",
                }
            },
            "strategy_files_used": [],
            "experience_loaded": [],
            "exception": None,
            "usage_total": {
                "prompt_tokens": 0, "cached_prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0,
            },
        }
        # 用服务端同一套 schema 校验，不通过就直接 500 —— 免得播种成功
        # 但被列表接口静默过滤，E2E 上表现为「什么都没测到」。
        parsed = AnalysisRecord.model_validate(record)
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            from pa_agent.storage.repositories import upsert_record

            upsert_record(parsed, raw=record, file_path=path)
        except Exception as exc:  # noqa: BLE001
            return {"seeded": True, "record_id": path.stem, "db_warning": str(exc)}
        return {"seeded": True, "record_id": path.stem}

@router.get("/records")
async def list_records(
    request: Request,
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
    # Offloaded: 载荷内嵌完整 stage1+stage2（数百 KB），JSON 解析是纯阻塞 CPU 工作，
    # inline 会卡住事件循环与所有 SSE 流。
    return await asyncio.to_thread(
        _list_records, exchange, symbol, timeframe, limit, include_partial,
        _request_user_id(request),
    )


@router.get("/records/{record_id:path}")
async def get_record(record_id: str, request: Request):
    """获取单条记录详情（已脱敏）。**只查库，按 user_id 过滤。**

    record_id 为相对 RECORDS_DIR 的路径（无 .json 后缀），例如
    ``GATEIO/BTCUSDT/1d/2026-07-18_14-00-13`` 或旧布局的
    ``2026-07-18_14-00-13_BTCUSDT_1d``。

    此前直接 ``open()`` 读文件且**完全不过滤用户** —— 任何人拿到 URL 都能
    读到别人的分析记录。记录详情含完整 stage1/stage2 推理，是这个系统里
    最敏感的数据之一。
    """
    # 路径遍历防护：复用 helper（含 .. / 绝对路径检测）
    target = _validate_record_id(record_id)

    from pa_agent.storage.repositories import get_record_detail

    row = get_record_detail(user_id=_request_user_id(request), file_path=str(target))
    data = _loads(row.get("payload_json")) if row else None
    if data is None:
        # 「不属于当前用户」与「不存在」刻意同形：区分开等于确认某 id 存在，
        # 那本身就是个信息泄露。
        raise HTTPException(status_code=404, detail="Record not found")

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
    """删除单条记录：**先删文件，再删 SQLite 索引行**。

    成功返回 ``{ok: true, record_id, db_deleted: bool}``；文件不存在返回 404；
    record_id 含路径遍历或绝对路径返回 400；文件删不掉返回 500（此时**索引行
    一定还在**，绝不出现「库里有、磁盘上没有」）。

    ## 为什么是这个顺序

    仍以磁盘为权威副本（交易 PNG 要发图、回放历史体量大），故删除顺序不变：
    **先删文件，成功后再删索引行**。

    - 文件删不掉（权限 / 只读挂载 / 被占用）→ 直接 500，索引行原样保留，一致。
    - 文件删成功、索引行删失败 → 留下一行指向已消失文件的**死索引行**。
      自 2026-10-05 读端只查库（``_list_records`` 直接读 ``payload_json``、
      不再校验 ``f.is_file()``）后，这类行会直接显示出来。代价是「列表里有个
      点不开的条目」，好过原先的静默不一致。

    反过来（先删索引行）失败时的残留是**用户看得见**的：文件还在、索引没了。
    读端不再回退扫盘，所以这条记录会从列表里消失 —— 仍是拿静默的行泄漏换
    一个删不掉的记录，划算。

    ## 为什么不级联清 ``experience_entries`` / ``trade_records``

    **没有可级联的外键，不凭空造。** 已核实（2026-10-05）：

    - ``pa_agent/storage/schema.py`` 的全部 DDL **没有一条 ``FOREIGN KEY`` /
      ``REFERENCES``**（``db.py`` 虽开了 ``PRAGMA foreign_keys=ON``，但没有
      声明的约束时它是空转的）；
    - ``experience_entries`` 与 ``trade_records`` 两张表**都没有 record_id 列**
      —— ``experience_entries`` 主键是 ``<user_id>_<文件 stem>``，
      ``trade_records`` 主键是 ``sha256(...)``，两者与 ``analysis_records``
      之间没有任何可 join 的键。

    也就是说「删记录连带删经验/交易」在当前 schema 下**无法用 SQL 正确表达**，
    靠文件名模糊匹配去删只会误伤（经验库是跨会话共享的 L2 资产，见
    ``docs/SESSION_STORAGE_DESIGN.md`` §2.1）。要级联就得先改 schema 加外键，
    那是一次独立的、必须评审的迁移，不属于「把 DELETE 接上仓储」。

    ## 残留缺口（只登记不修）

    手工从磁盘删掉记录文件时，本端点因文件不存在而返回 404，**不会**顺手清
    索引行。不在 404 路径上做删除，是因为 ``record_id`` 只是文件 basename
    （见下），不同分区下可能同名 —— 拿一个不存在的文件去按名删行，等于开了一条
    「误删他人记录」的新路径。这个洞应该由周期性 reconcile（扫库中
    ``file_path`` 已不存在的行）来补，而不是由 DELETE 端点补。
    """
    # 路径遍历防护：复用 helper（含 .. / 绝对路径检测）
    target = _validate_record_id(record_id)

    if not target.exists():
        raise HTTPException(status_code=404, detail="record not found")

    # ── 1. 先删磁盘文件 ────────────────────────────────────────────────────
    try:
        target.unlink()
    except Exception as e:
        # 索引行**刻意不动**：见 docstring「为什么是这个顺序」。
        raise HTTPException(status_code=500, detail=f"Failed to delete record: {e}")

    # ── 2. 再删 SQLite 索引行 ──────────────────────────────────────────────
    # 主键是 **文件 basename**，不是 URL 里的 record_id：
    # ``repositories.upsert_record`` 写入时用 ``_record_basename(file_path)``
    # 即 ``Path.stem`` 作 record_id（见 repositories.py:81）。分区布局下 URL
    # 是 ``GATEIO/BTCUSDT/1d/<stem>``，两者差一段目录 —— 用 record_id 去删
    # 会「删不掉任何行」且无任何报错，正是本函数要消灭的那类静默失败。
    db_deleted = True
    try:
        from pa_agent.storage.repositories import delete_record as repo_delete

        db_deleted = bool(repo_delete(target.stem))
    except Exception:
        # DB 不可用 / 未初始化 / 表不存在：记录本身已从磁盘消失，用户的意图
        # 已达成。**不得因此回 500** —— 那会告诉用户「删除失败」并诱发重试，
        # 而重试只会拿到 404。降级为 ok + db_deleted=false + 日志。
        logger.warning(
            "record %s deleted from disk but its DB row was not removed", record_id,
            exc_info=True,
        )
        db_deleted = False

    if not db_deleted:
        logger.warning(
            "record %s (stem=%s) deleted from disk, DB row still present", record_id, target.stem
        )

    return {"ok": True, "record_id": record_id, "db_deleted": db_deleted}
