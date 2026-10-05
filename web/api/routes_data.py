"""REST routes for data sources, symbols, timeframes."""
from __future__ import annotations

import asyncio
import logging
import time
import json
from pathlib import Path
from types import SimpleNamespace

from pa_agent.ai.display_labels import (  # noqa: E402
    bilingual_case_type as _bilingual_case_type,
    bilingual_cycle as _bilingual_cycle,
    bilingual_direction as _bilingual_direction,
    bilingual_result as _bilingual_result,
)

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.bar_close_wait import seconds_until_bar_closes
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    create_data_source,
    data_source_label,
    default_symbol_for_kind,
    normalize_data_source_kind,
)
from pa_agent.data.snapshot import build_display_frame

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data"])


class SubscribeRequest(BaseModel):
    # Defaults are intentionally empty — the frontend must send the actual
    # values from the current settings (don't hardcode XAUUSD here, otherwise
    # switching to crypto would silently fall back to gold).
    kind: str = "tradingview"
    symbol: str = ""
    timeframe: str = ""
    exchange: str = ""


@router.get("/datasources")
async def list_datasources():
    return [
        {"id": k, "label": v} for k, v in DATA_SOURCE_CHOICES
    ]


@router.get("/tv/exchanges")
async def list_tv_exchanges(request: Request):
    """List TradingView exchange ids (curated preset).

    Returns ``[{"id": "GATEIO", "label": "Gate.io"}, ...]``.
    Empty id maps to label "自动（探测）".
    """
    from pa_agent.data.tradingview import TV_EXCHANGE_PRESETS

    label_map = {
        "GATEIO": "Gate.io",
        "BINANCE": "Binance",
        "BYBIT": "Bybit",
        "OKX": "OKX",
        "BITSTAMP": "Bitstamp",
        "COINBASE": "Coinbase",
        "OANDA": "OANDA",
        "PEPPERSTONE": "Pepperstone",
        "FOREXCOM": "FOREX.com",
        "TVC": "TVC（TradingView 自有）",
        "CAPITALCOM": "Capital.com",
        "SSE": "上交所",
        "SZSE": "深交所",
        "HKEX": "港交所",
        "SP": "S&P",
        "NYSE": "纽交所",
        "NASDAQ": "纳斯达克",
        "CBOT": "CBOT",
        "CME_MINI": "CME Mini",
        "": "自动（探测）",
    }
    return [
        {"id": e, "label": label_map.get(e, e)} for e in TV_EXCHANGE_PRESETS
    ]


@router.get("/tv/symbols")
async def list_tv_symbols(request: Request, exchange: str = ""):
    """List curated symbols for a TradingView *exchange*.

    Frontend should still allow free-text input — TradingView has no public
    "list all symbols" API, so this endpoint returns a curated preset.
    """
    ctx = request.app.state.ctx
    ds = ctx.data_source
    # Prefer the data source's list_symbols(exchange) if available (TradingView).
    # Offloaded: TradingViewSource.list_symbols() may hit the network.
    if hasattr(ds, "list_symbols"):
        try:
            syms = await asyncio.to_thread(ds.list_symbols, exchange)
        except TypeError:
            # Older data sources have list_symbols() without args
            syms = await asyncio.to_thread(ds.list_symbols)
    else:
        syms = []

    from pa_agent.data.tradingview import TV_SYMBOL_NAMES
    # 前端有 10 分钟缓存（symbolListCache + SYMBOL_CACHE_TTL），后端响应头
    # 配置 Cache-Control: max-age=600 与前端 TTL 对齐，避免中间代理/浏览器
    # 在缓存过期后立即重新请求导致后端被密集打。
    response = JSONResponse({
        "exchange": exchange,
        "symbols": [{"code": s, "name": TV_SYMBOL_NAMES.get(s, s)} for s in syms]
    })
    response.headers["Cache-Control"] = "max-age=600"
    return response


@router.get("/timeframes")
async def list_timeframes(request: Request):
    """Return timeframes supported by the current data source."""
    ctx = request.app.state.ctx
    try:
        tfs = await asyncio.to_thread(ctx.data_source.supported_timeframes)
    except Exception:
        tfs = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
    return tfs


@router.get("/order-opportunity-types")
async def list_order_opportunity_types():
    """Expose the single-source-of-truth order gate types to the browser.

    The frontend's browser-side alert must use exactly the same set as
    ``pa_agent.ai.order_opportunity.ORDER_OPPORTUNITY_TYPES`` (see AGENTS.md
    「下单信号推送」). Duplicating the list in JS previously caused the alert to
    compare Chinese stage-2 output against English labels and never fire.
    """
    from pa_agent.ai.order_opportunity import ORDER_OPPORTUNITY_TYPES

    return sorted(ORDER_OPPORTUNITY_TYPES)


@router.post("/subscribe")
async def subscribe(req: SubscribeRequest, request: Request):
    """Switch to a new symbol/timeframe/data-source.

    **游标只写本会话（L3），两层都不写**：游标是「这个 tab 现在在看什么」，
    写进 ``ctx.settings.general.last_*`` 等于把「上一个 tab 的标的」固化成全局值，
    写进 user_prefs 则让它跨重启存活 —— 两者都是「一次 F5 就串味」的来源
    （docs/SESSION_STORAGE_DESIGN.md §5.1）。
    """
    ctx = request.app.state.ctx
    kind = normalize_data_source_kind(req.kind)
    old_kind = normalize_data_source_kind(
        getattr(ctx.settings.general, "last_data_source", "mt5")
    )

    # 缺省值取**本会话**游标而非全局：前端只切一个维度时（如只换交易所），
    # 回落全局会让本 tab 订阅到别的 tab 的标的。
    view_symbol, view_timeframe, view_exchange = _resolve_view(request, ctx)

    # Fall back to the current view when the request omits a field.  This
    # keeps the endpoint safe when the frontend only wants to switch one
    # dimension (e.g. just the exchange).
    symbol = req.symbol or view_symbol
    timeframe = req.timeframe or view_timeframe
    exchange = req.exchange
    if kind == "tradingview" and not exchange:
        exchange = view_exchange

    if kind != old_kind:
        try:
            await asyncio.to_thread(ctx.data_source.disconnect)
        except Exception:
            pass
        ctx.data_source = create_data_source(kind)

    # switch-performance-refactor spec: 连接复用
    # 当数据源类型不变（仍是 tradingview）且 TvDatafeed 已连接时，
    # 跳过 connect() 重连（避免 WebSocket 重建带来的数秒级阻塞），
    # 仅调用 set_exchange + subscribe 即可。
    already_connected = (
        kind == old_kind == "tradingview"
        and bool(getattr(ctx.data_source, "_connected", False))
    )

    try:
        # connect() rebuilds a TradingView WebSocket and subscribe() fetches
        # history — both block for seconds. Offload so a switch cannot stall the
        # whole event loop (and with it every SSE stream).
        if not already_connected:
            await asyncio.to_thread(ctx.data_source.connect)
        if kind == "tradingview" and hasattr(ctx.data_source, "set_exchange"):
            ctx.data_source.set_exchange(exchange)
        await asyncio.to_thread(ctx.data_source.subscribe, symbol, timeframe)
    except Exception as exc:
        # switch-performance-refactor spec: 结构化错误响应
        # 根据异常信息分类返回 error_type，前端据此显示针对性提示：
        #   - symbol：包含 "symbol"/"not found"，品种无效
        #   - timeout：包含 "timeout"，请求超时
        #   - connection：其他错误，连接失败
        msg_lower = str(exc).lower()
        if "symbol" in msg_lower or "not found" in msg_lower:
            error_type = "symbol"
        elif "timeout" in msg_lower or "timed out" in msg_lower:
            error_type = "timeout"
        else:
            error_type = "connection"
        # 同时通过响应体（error_type 字段）和响应头（X-Error-Type）传递错误类型，
        # 前端 API.post 优先读取响应体；头作为兜底。
        response = JSONResponse(
            {"detail": str(exc), "error_type": error_type},
            status_code=500,
        )
        response.headers["X-Error-Type"] = error_type
        return response

    # ── 会话游标（L3）：本 endpoint 唯一的持久化动作 ──────────────────────
    # 同时写入本 tab 的游标：会话快照（SQLite，TTL 1800s）+ 内存热层。
    # 有了它，切品种不再只是改全局订阅 —— 别的 tab 仍读自己的游标
    # （docs/SESSION_STORAGE_DESIGN.md §3）。
    #
    # **不再改 ctx.settings.general.last_*，也不再 save_settings()**：
    #   - 改内存：多 tab 下立刻串味，且会污染 bars 等回落全局的读路径
    #   - 写文件：系统兜底一旦存在那份文件根本没人读 → 静默丢失
    #   - 写 user_prefs：游标跨重启存活，F5 之后回到上一个 tab 的标的
    # 快照写失败只记 warning：游标是缓存，丢了回落全局设置即可。
    try:
        from web.api.session_ctx import session_id_of

        sid = session_id_of(request)
        if sid:
            from pa_agent.storage import sessions as sess_repo
            from pa_agent.storage.ephemeral import Cursor, get_registry

            get_registry().get_or_create(sid).cursor = Cursor(
                symbol, timeframe, exchange
            )
            sess_repo.set_cursor(
                sid, symbol=symbol, timeframe=timeframe, exchange=exchange
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("session cursor persist failed: %s", exc)

    return {
        "status": "subscribed",
        "kind": kind,
        "symbol": symbol,
        "timeframe": timeframe,
        "exchange": exchange,
    }



def _resolve_view(request: Request, ctx) -> tuple[str, str, str]:
    """本请求的 (symbol, timeframe, exchange)，优先取会话游标。

    统一入口：bars / next-close / analyze / chat 都必须走它，否则多标签页下
    会出现「记录的标的与图上的 K 线不是同一个」的静默错配。
    """
    from web.api.session_ctx import resolve_view, session_id_of

    return resolve_view(ctx, session_id_of(request))


@router.get("/bars")
async def get_bars(request: Request, count: int = 100):
    """Fetch latest N bars and return as JSON for chart rendering."""
    ctx = request.app.state.ctx
    # latest_snapshot() can open a TradingView WebSocket + HTTP get_hist on a
    # cache miss. The frontend polls this every second, so calling it inline
    # would block the event loop and freeze every SSE stream. The background SSE
    # 轮询流同样 offloads 这同一个调用。
    # 按**本会话游标**取数，而不是全局订阅：多标签页各看各的标的时，
    # 读全局订阅会让 A tab 拿到 B tab 的 K 线（评审 H1）。
    view_symbol, view_timeframe, view_exchange = _resolve_view(request, ctx)
    bars_raw = await asyncio.to_thread(
        ctx.data_source.latest_snapshot, count,
        exchange=view_exchange or None,
        symbol=view_symbol or None,
        timeframe=view_timeframe or None,
    )
    bars: list[dict] = []
    for b in bars_raw:
        bars.append({
            "seq": b.seq,
            "ts_open": b.ts_open,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "closed": bool(b.closed),
        })
    # 回显的元数据必须与 bars 同一来源。此前这里读全局 settings，而 bars 取自
    # 会话游标 → 多标签页下响应自相矛盾（symbol 说是 A 标的，bars 是 B 标的），
    # 而前端正是靠这个字段判断当前图表品种。
    return {
        "symbol": view_symbol or ctx.settings.general.last_symbol,
        "timeframe": view_timeframe or ctx.settings.general.last_timeframe,
        "exchange": view_exchange or ctx.settings.general.last_tradingview_exchange,
        "bars": bars,
    }


@router.get("/bars/next-close")
async def get_next_close(
    request: Request,
    symbol: str = "",
    timeframe: str = "",
    exchange: str = "",
):
    """Return the next bar close timestamp and seconds remaining.

    Used by the frontend「等待收盘」countdown to know when the currently
    forming bar will close. Computes ``next_close_ts`` (= forming bar's
    ``ts_open`` + duration) and ``seconds_remaining`` via
    :func:`pa_agent.data.bar_close_wait.seconds_until_bar_closes`.

    Falls back to current settings when parameters are omitted. Returns
    ``seconds_remaining=None`` when the timeframe is unknown or no
    forming bar exists.

    休市检测：若 ``bars[0].closed == True``（无 forming bar，市场已收盘），
    返回 ``market_closed=True`` 与 ``next_close_ts=None``，前端据此清空
    倒计时并显示「休市中」。否则取模算法会基于过期 ts_open 计算出错误
    的未来周期边界时间戳，导致休市期间显示错误倒计时。
    """
    ctx = request.app.state.ctx
    # 优先级：**显式 query 参数 > 本会话游标 > 全局 settings**。
    # 此前 tf 直接取 query 或全局，而下面取 bar 用的是会话游标 —— 两者可能
    # 指向不同标的，于是倒计时按 A 的周期算、bar 取的是 B 的 K 线。
    view_symbol, view_timeframe, view_exchange = _resolve_view(request, ctx)
    sym = symbol or view_symbol or getattr(ctx.settings.general, "last_symbol", "")
    tf = timeframe or view_timeframe or getattr(ctx.settings.general, "last_timeframe", "") or ""
    ex = exchange or view_exchange or getattr(ctx.settings.general, "last_tradingview_exchange", "")
    bars_raw = await asyncio.to_thread(
        ctx.data_source.latest_snapshot, 2,
        exchange=view_exchange or None,
        symbol=view_symbol or None,
        timeframe=view_timeframe or None,
    )
    if not bars_raw:
        return {
            "symbol": sym,
            "timeframe": tf,
            "next_close_ts": None,
            "seconds_remaining": None,
            "market_closed": False,
        }
    forming = bars_raw[0]
    ts_open_ms = int(getattr(forming, "ts_open", 0))
    if ts_open_ms <= 0:
        return {
            "symbol": sym,
            "timeframe": tf,
            "next_close_ts": None,
            "seconds_remaining": None,
            "market_closed": False,
        }
    # 休市检测：head bar 已收盘 → 无 forming bar，市场已收盘/休市
    # 此时取模算法会返回错误的未来时间戳，必须短路返回
    is_market_closed = bool(getattr(forming, "closed", False))
    if is_market_closed:
        return {
            "symbol": sym,
            "timeframe": tf,
            "next_close_ts": None,
            "seconds_remaining": None,
            "market_closed": True,
        }
    seconds_remaining = seconds_until_bar_closes(ts_open_ms, tf)
    # 统一使用 routes_bars_stream._compute_next_close_ts 计算 next_close_ts
    # （SSE 和 REST 必须使用同一算法，避免时区偏移导致结果不一致）
    from .routes_bars_stream import _compute_next_close_ts

    next_close_ts = _compute_next_close_ts(ts_open_ms, tf)
    return {
        "symbol": sym,
        "timeframe": tf,
        "next_close_ts": next_close_ts,
        "seconds_remaining": seconds_remaining,
        "market_closed": False,
    }


# ── 经验库浏览 ────────────────────────────────────────────────────────────────
# 经验库此前只有 reader、没有任何写入方，也没有浏览入口 —— Web 端完全看不到
# 库里到底有什么、Stage2 到底检索到了什么。这里补上只读浏览接口。

def _status_label(status: str) -> str:
    """Bilingual label for a two-stage experience status."""
    return {
        "pending": "待验证 (pending)",
        "win": "盈利 (win)",
        "loss": "亏损 (loss)",
        "unresolved": "未触及 (unresolved)",
    }.get(str(status or "").strip().lower(), "")


@router.get("/experience")
async def list_experience(
    request: Request,
    cycle: str = Query(default="", description="按市场周期过滤，空=全部"),
    symbol: str = Query(default="", description="按交易对过滤，如 BTCUSDT；空=全部"),
    timeframe: str = Query(default="", description="按 K 线周期过滤，如 15m；空=全部"),
):
    """List experience-library entries, newest first, grouped by cycle.

    Scans on a worker thread: the library can hold many JSON files and this
    endpoint must not block the event loop.

    ``symbol`` / ``timeframe`` filter on the **entry content** rather than the
    filename, because the same code appears under different cycles. The UI
    defaults both to the currently-subscribed instrument so the panel shows
    "经验来自你正在看的这个标的" instead of an undifferentiated pile.

    数据源：**先查 SQLite，查不到或读不出来再扫文件**（设计文档 §7 的 C 阶段）。
    只读文件的话 ``user_id`` 那一列在浏览端也等于装饰品 —— 文件系统里没有
    用户概念，多用户隔离在面板上是假的。
    """
    sym_filter = (symbol or "").strip().upper()
    tf_filter = (timeframe or "").strip().lower()
    try:
        from web.api.auth_ctx import current_user_id

        uid = current_user_id(request)
    except Exception:  # noqa: BLE001
        from pa_agent.storage.db import DEFAULT_USER_ID

        uid = DEFAULT_USER_ID

    def _scan() -> dict:
        """只查库（2026-10-05 起文件布局已废弃）。

        pending / unresolved 一并取回：它们**不是**可检索的经验（不进提示词），
        但面板必须看得见，否则用户分不清「还没判定」和「系统坏了」。
        """
        from pa_agent.records.experience_writer import STATUS_PENDING, STATUS_WIN
        from pa_agent.storage.experience_repo import list_entries

        rows = list_entries(user_id=uid, limit=600)
        if getattr(rows, "failed", False):
            # 存储层读不出来：不能伪装成「库是空的」—— 那与「还没积累够案例」
            # 长得一模一样，用户无从分辨。只记 error，面板显示空。
            logger.error(
                "experience browse: store unreadable (%s)",
                getattr(rows, "error", "") or "unknown",
            )
        all_rows = [_row_dict(e) for e in _rows_to_entries(rows)]
        return _browse_payload(all_rows, _counts_by_cycle(all_rows))

    def _rows_to_entries(rows) -> list[SimpleNamespace]:
        """DB 行 → 与 reader 同构的 entry，供下游拼装逻辑复用。"""
        from pa_agent.records.experience_writer import STATUS_PENDING, STATUS_WIN

        out = []
        for r in rows:
            try:
                content = json.loads(r.get("content_json") or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(content, dict):
                continue
            status = str(r.get("status") or "").lower()
            out.append(SimpleNamespace(
                filename=str(r.get("entry_id") or ""),
                # unresolved 在面板上归入 failure（它同样是「没打出预期」）；
                # pending 单列 —— 它还没结论。
                case_type="pending" if status == STATUS_PENDING
                            else ("success" if status == STATUS_WIN else "failure"),
                cycle_position=str(r.get("cycle_position") or ""),
                timestamp_ms=int(r.get("timestamp_ms") or 0),
                content=content,
            ))
        return out

    def _counts_by_cycle(rows: list[dict]) -> dict[str, dict[str, int]]:
        """按 cycle_position 聚合。pending 不计入成败（它还没结论）。"""
        counts: dict[str, dict[str, int]] = {}
        for r in rows:
            c = counts.setdefault(r["cycle_position"], {"success": 0, "failure": 0})
            if r["case_type"] == "success":
                c["success"] += 1
            elif r["case_type"] == "failure":
                c["failure"] += 1
        return counts

    def _row_dict(e, fallback_cycle: str = "") -> dict:
        """entry → 面板行。字段口径与 reader 保持一致。"""
        content = getattr(e, "content", {}) or {}
        case_type = str(getattr(e, "case_type", "") or "")
        cycle_pos = (
            getattr(e, "cycle_position", None)
            or content.get("cycle_position") or fallback_cycle
        )
        st = str(content.get("status") or "").strip().lower()
        if st not in ("pending", "win", "loss", "unresolved"):
            st = {"success": "win", "pending": "pending"}.get(case_type, "loss")
        return {
            "filename": getattr(e, "filename", ""),
            "case_type": case_type,
            "status": st,
            "is_pending": st == "pending",
            "exchange": content.get("exchange", ""),
            "entry_price": content.get("entry_price"),
            "take_profit_price": content.get("take_profit_price"),
            "stop_loss_price": content.get("stop_loss_price"),
            "is_long": content.get("is_long", True),
            "entry_ts_open_ms": content.get("entry_ts_open_ms"),
            "resolved_ts_open_ms": content.get("resolved_ts_open_ms"),
            "bars_seen": content.get("bars_seen"),
            "cycle_position": cycle_pos,
            "cycle_label": _bilingual_cycle(cycle_pos),
            "direction_label": _bilingual_direction(content.get("direction", "")),
            "result_label": _bilingual_result(content.get("result", "")),
            "case_type_label": _status_label(st),
            "timestamp_ms": getattr(e, "timestamp_ms", 0),
            "symbol": content.get("symbol", ""),
            "timeframe": content.get("timeframe", ""),
            "direction": content.get("direction", ""),
            "result": content.get("result", ""),
            "pnl_pct": content.get("pnl_pct"),
            "confidence": content.get("confidence"),
            "summary": content.get("summary", ""),
            "detected_patterns": content.get("detected_patterns", []) or [],
        }

    def _browse_payload(all_rows: list[dict], counts: dict) -> dict:
        """过滤 / 汇总 / 排序。

        ``all_rows`` 必须是**未按 symbol/timeframe 过滤**的全量：下拉选项
        （symbols / timeframes）要从全量取，过滤后的集合会让选项随筛选漂移。
        """
        if cycle:
            all_rows = [r for r in all_rows if r["cycle_position"] == cycle]
            counts = {k: v for k, v in counts.items() if k == cycle}

        symbols = sorted({r["symbol"] for r in all_rows if r["symbol"]})
        timeframes = sorted({r["timeframe"] for r in all_rows if r["timeframe"]})

        rows = all_rows
        if sym_filter:
            rows = [r for r in rows if str(r["symbol"]).upper() == sym_filter]
        if tf_filter:
            rows = [r for r in rows if str(r["timeframe"]).lower() == tf_filter]
        # counts 也要跟着过滤，否则前端汇总数字对不上
        if sym_filter or tf_filter:
            per = {}
            for r in rows:
                c = per.setdefault(r["cycle_position"], {"success": 0, "failure": 0})
                if r["case_type"] == "success":
                    c["success"] += 1
                elif r["case_type"] == "failure":
                    c["failure"] += 1
            counts = {k: v for k, v in per.items() if v["success"] or v["failure"]}

        rows.sort(key=lambda x: x.get("timestamp_ms") or 0, reverse=True)
        cycle_options = [
            {"value": c, "label": _bilingual_cycle(c)} for c in sorted(counts)
        ]
        status_counts: dict[str, int] = {}
        for r in rows:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

        return {
            "total": len(rows),
            "entries": rows,
            "cycles": counts,
            "cycle_options": cycle_options,
            "status_counts": status_counts,
            "symbols": symbols,
            "timeframes": timeframes,
        }

    result = await asyncio.to_thread(_scan)
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


@router.get("/tv/search")
async def search_tv_symbols(
    q: str = Query(default="", description="搜索关键词（代码或名称）"),
    exchange: str = Query(default="", description="交易所 id，空=全市场"),
    limit: int = Query(default=50, ge=1, le=150),
):
    """Live TradingView symbol search via the scanner API.

    ``/api/tv/symbols`` returns only a small curated preset (offline, instant);
    this endpoint hits ``scanner.tradingview.com`` so users can find the full
    universe (64k crypto pairs / 20k US stocks / 52k futures / …). Any network
    failure yields ``[]`` instead of raising, so the frontend can fall back to
    the preset list.
    """
    from pa_agent.data.tradingview import search_tv_symbols as _search

    rows = await asyncio.to_thread(_search, q, exchange, limit)
    return JSONResponse(
        content={"query": q, "exchange": exchange, "count": len(rows), "results": rows},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/experience/verify")
async def verify_experience(request: Request, scope_current: bool = Query(default=True)):
    """Settle pending experience records that have enough post-entry bars.

    ``scope_current=true`` (the UI's "验证" button) settles only the records for
    the currently-viewed instrument, so the panel always reflects the chart.
    Off-thread: each non-matching record may need its own data source.
    """
    from web.api.experience_verifier import verify_pending

    ctx = request.app.state.ctx
    scope = None
    if scope_current:
        scope = (str(getattr(ctx.settings.general, "last_symbol", "") or ""),
                 str(getattr(ctx.settings.general, "last_timeframe", "") or ""))

    summary = await asyncio.to_thread(
        verify_pending,
        shared_source=ctx.data_source,
        source_factory=None,
        settings=ctx.settings,
        scope=scope,
    )
    return JSONResponse(content=summary, headers={"Cache-Control": "no-store"})


@router.post("/experience/verify/once")
async def verify_experience_once(request: Request):
    """Run exactly one background settlement pass (the scheduler's unit of work).

    Exposed so the UI can nudge settlement on demand without duplicating the
    scheduler's scope/guard logic — and so it stays testable.
    """
    from web.api import experience_scheduler

    # force=True：manual 模式只关掉**定时器**，不关掉用户点的「验证」按钮
    summary = await asyncio.to_thread(experience_scheduler.run_once,
                                      request.app.state.ctx, True)
    return JSONResponse(content=summary or {"skipped": True},
                        headers={"Cache-Control": "no-store"})
