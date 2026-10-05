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


def _db_candidates(
    exchange: str,
    symbol: str,
    timeframe: str,
    limit: int,
    include_partial: bool,
) -> list[Path]:
    """**数据库为唯一真源**：返回 DB 指向的、确实存在于 RECORDS_DIR 的记录文件。

    ``include_partial`` 必须下推到 SQL —— 否则失败记录先占掉 LIMIT 配额，
    真实记录被挤掉，历史面板表现为「明明有记录却显示为空」（评审 H4）。

    返回空列表 = 「库里没有」或「库不可用」，调用方据此回退磁盘（自愈路径，
    不是双轨：磁盘只是补种来源，读的权威始终是库）。
    """
    try:
        from pa_agent.storage.repositories import list_records as db_list

        rows = db_list(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            include_partial=include_partial,
            limit=limit,
        )
        # 校验「文件存在」且「位于 RECORDS_DIR 内」：DB 里的 file_path 可能已
        # 移动，而本函数契约是只返回 RECORDS_DIR 下的记录（该目录可被 monkeypatch）。
        root = RECORDS_DIR.resolve()
        paths: list[Path] = []
        for r in rows:
            fp = r.get("file_path")
            if not fp:
                continue
            f = Path(fp)
            if not f.is_file():
                continue
            try:
                f.resolve().relative_to(root)
            except ValueError:
                continue
            paths.append(f)
        return paths
    except Exception:  # noqa: BLE001
        logger.warning("SQLite browse failed, falling back to files", exc_info=True)
        return []


def _file_candidates(
    exchange: str, symbol: str, timeframe: str, limit: int
) -> list[Path]:
    """自愈回退：直接从磁盘定位记录文件。

    三条件齐全 → 分区目录 glob（快）；缺任意一个 → 分区路径无法定位，
    退化为全扫描 + 按记录 meta 过滤。仅用于「库里还没有这条记录」的场景。
    """
    from pa_agent.records.analysis_history import list_record_paths, load_record

    if exchange and symbol and timeframe:
        return _glob_partitioned(exchange, symbol, timeframe, limit)

    paths = list_record_paths(RECORDS_DIR)
    if not (exchange or symbol or timeframe):
        return paths[:limit]

    out: list[Path] = []
    for f in paths:
        if len(out) >= limit:
            break
        rec = load_record(f)
        if rec is None:
            continue
        meta = rec.meta
        if exchange and (meta.exchange or "") != exchange:
            continue
        if symbol and meta.symbol != symbol:
            continue
        if timeframe and meta.timeframe != timeframe:
            continue
        out.append(f)
    return out


def _list_records(
    exchange: str,
    symbol: str,
    timeframe: str,
    limit: int,
    include_partial: bool,
) -> list[dict]:
    """列出记录摘要。

    **数据库是唯一真源**：先查 SQLite，只有「库里没有 / 库不可用」才回退磁盘
    自愈。老的文件体系不再参与读取决策 —— 它只是补种来源，不是权威。

    ``exchange``/``symbol``/``timeframe`` **全部可选**：三者皆空时跨全部品种
    浏览（历史是 L2 用户级共享资产，A tab 分析出的记录 B tab 也要能查到，
    见 docs/SESSION_STORAGE_DESIGN.md §2.1）。
    """
    if not RECORDS_DIR.exists():
        return []

    candidates = _db_candidates(
        exchange, symbol, timeframe, limit, include_partial
    )
    if not candidates:
        candidates = _file_candidates(exchange, symbol, timeframe, limit)

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


# ── E2E 专用：播种一条可回看的合成记录 ────────────────────────────────────
# 端到端测试需要一条「历史记录」才能走回看链路，而 CI 是空库。
#
# **为什么必须由服务端播种**：宿主机与容器是两套文件系统视图
# （宿主 /root/.../records/pending vs 容器 /app/records/pending），
# 同一 inode 但路径不同。测试进程自己写库必然过不了
# `_db_candidates` 的 `f.resolve().relative_to(RECORDS_DIR)` 校验。
# 由服务端进程写盘、用它自己的 RECORDS_DIR、走它自己的 upsert，才是对的。
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
    """删除单条记录：**先删文件，再删 SQLite 索引行**。

    成功返回 ``{ok: true, record_id, db_deleted: bool}``；文件不存在返回 404；
    record_id 含路径遍历或绝对路径返回 400；文件删不掉返回 500（此时**索引行
    一定还在**，绝不出现「库里有、磁盘上没有」）。

    ## 为什么是这个顺序

    **磁盘是权威副本，SQLite 只是索引**（``repositories`` 模块 docstring）。
    顺序必须让「索引指向真实存在的文件」这条不变式在**任何失败点**都成立：

    - 文件删不掉（权限 / 只读挂载 / 被占用）→ 直接 500，索引行原样保留。
      库里指向一个还存在的文件，一致。
    - 文件删成功、索引行删失败 → 留下一行指向已消失文件的**死索引行**。
      它不会显示（``_db_candidates`` 用 ``f.is_file()`` 自愈过滤），也不会
      让记录复活（``_file_candidates`` 扫的是磁盘，文件已不在），只是留一行。

    反过来（先删索引行）失败时的残留是**用户看得见**的：文件还在、索引没了，
    而 ``_list_records`` 在「DB 结果为空」时会回退全盘扫描 —— 于是这条记录
    又出现在列表里，用户再点删除、再失败，且**没有任何报错解释为什么删不掉**。
    拿一个静默的行泄漏换一个删不掉的记录，明显不划算。

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
