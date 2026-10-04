"""REST routes for data sources, symbols, timeframes."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

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
    """Switch to a new symbol/timeframe/data-source."""
    ctx = request.app.state.ctx
    kind = normalize_data_source_kind(req.kind)
    old_kind = normalize_data_source_kind(
        getattr(ctx.settings.general, "last_data_source", "mt5")
    )

    # Fall back to current settings when the request omits a field.  This
    # keeps the endpoint safe when the frontend only wants to switch one
    # dimension (e.g. just the exchange).
    symbol = req.symbol or ctx.settings.general.last_symbol
    timeframe = req.timeframe or ctx.settings.general.last_timeframe
    exchange = req.exchange
    if kind == "tradingview" and not exchange:
        exchange = getattr(ctx.settings.general, "last_tradingview_exchange", "")

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

    ctx.settings.general.last_data_source = kind
    ctx.settings.general.last_symbol = symbol
    ctx.settings.general.last_timeframe = timeframe
    if kind == "tradingview":
        ctx.settings.general.last_tradingview_exchange = exchange

    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import save_settings
    await asyncio.to_thread(save_settings, ctx.settings, SETTINGS_JSON_PATH)

    return {
        "status": "subscribed",
        "kind": kind,
        "symbol": symbol,
        "timeframe": timeframe,
        "exchange": exchange,
    }


@router.get("/bars")
async def get_bars(request: Request, count: int = 100):
    """Fetch latest N bars and return as JSON for chart rendering."""
    ctx = request.app.state.ctx
    # latest_snapshot() can open a TradingView WebSocket + HTTP get_hist on a
    # cache miss. The frontend polls this every second, so calling it inline
    # would block the event loop and freeze every SSE stream. The background SSE
    # loop already offloads the same call (see routes_bars_stream._push_bar_update).
    bars_raw = await asyncio.to_thread(ctx.data_source.latest_snapshot, count)
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
    return {
        "symbol": ctx.settings.general.last_symbol,
        "timeframe": ctx.settings.general.last_timeframe,
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
    tf = timeframe or getattr(ctx.settings.general, "last_timeframe", "") or ""
    # symbol / exchange are accepted for symmetry with /api/subscribe but
    # are not strictly required — we read the forming bar from the
    # current data source regardless.
    bars_raw = await asyncio.to_thread(ctx.data_source.latest_snapshot, 2)
    if not bars_raw:
        return {
            "symbol": symbol or getattr(ctx.settings.general, "last_symbol", ""),
            "timeframe": tf,
            "next_close_ts": None,
            "seconds_remaining": None,
            "market_closed": False,
        }
    forming = bars_raw[0]
    ts_open_ms = int(getattr(forming, "ts_open", 0))
    if ts_open_ms <= 0:
        return {
            "symbol": symbol or getattr(ctx.settings.general, "last_symbol", ""),
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
            "symbol": symbol or getattr(ctx.settings.general, "last_symbol", ""),
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
        "symbol": symbol or getattr(ctx.settings.general, "last_symbol", ""),
        "timeframe": tf,
        "next_close_ts": next_close_ts,
        "seconds_remaining": seconds_remaining,
        "market_closed": False,
    }


# ── 经验库浏览 ────────────────────────────────────────────────────────────────
# 经验库此前只有 reader、没有任何写入方，也没有浏览入口 —— Web 端完全看不到
# 库里到底有什么、Stage2 到底检索到了什么。这里补上只读浏览接口。

@router.get("/experience")
# 枚举中英标签（Qt-free，展示层专用，不影响业务判定）
async def list_experience(
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
    "经验来自你正在看的这个品种" instead of an undifferentiated pile.
    """
    from pa_agent.config.paths import EXPERIENCE_DIR

    sym_filter = (symbol or "").strip().upper()
    tf_filter = (timeframe or "").strip().lower()

    def _scan() -> dict:
        from pa_agent.records.experience_reader import ExperienceReader

        root = Path(EXPERIENCE_DIR)
        if not root.is_dir():
            return {"total": 0, "entries": [], "cycles": {}, "symbols": [], "timeframes": []}

        cycles = sorted(
            d.name for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        if cycle:
            cycles = [c for c in cycles if c == cycle]

        reader = ExperienceReader(experience_dir=root)
        entries: list[dict] = []
        counts: dict[str, dict[str, int]] = {}
        all_rows: list[dict] = []
        for name in cycles:
            try:
                found = reader.read_top5(name)
            except Exception:  # noqa: BLE001
                found = []
            success = 0
            failure = 0
            for e in found:
                content = getattr(e, "content", {}) or {}
                case_type = getattr(e, "case_type", "")
                if case_type == "success":
                    success += 1
                elif case_type == "failure":
                    failure += 1
                cycle_pos = getattr(e, "cycle_position", None) or content.get("cycle_position") or name
                all_rows.append({
                    "filename": getattr(e, "filename", ""),
                    "case_type": case_type,
                    "cycle_position": cycle_pos,
                    # 枚举同时给出中英标签：中文给操作者看，raw 给提示词/落盘路径对齐
                    "cycle_label": _bilingual_cycle(cycle_pos),
                    "direction_label": _bilingual_direction(content.get("direction", "")),
                    "result_label": _bilingual_result(content.get("result", "")),
                    "case_type_label": _bilingual_case_type(case_type),
                    "timestamp_ms": getattr(e, "timestamp_ms", 0),
                    "symbol": content.get("symbol", ""),
                    "timeframe": content.get("timeframe", ""),
                    "direction": content.get("direction", ""),
                    "result": content.get("result", ""),
                    "pnl_pct": content.get("pnl_pct"),
                    "confidence": content.get("confidence"),
                    "summary": content.get("summary", ""),
                    "detected_patterns": content.get("detected_patterns", []) or [],
                })
            counts[name] = {"success": success, "failure": failure}

        # 可选维度：来自全量（未按 symbol/tf 过滤）的集合，供前端下拉用
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
                c["failure" if r["case_type"] == "failure" else "success"] += 1
            counts = {k: v for k, v in per.items() if v["success"] or v["failure"]}

        rows.sort(key=lambda x: x.get("timestamp_ms") or 0, reverse=True)
        # 市场周期下拉选项：value 仍是 raw（用于过滤），label 走中英标签，
        # 避免前端出现 broad_channel / unknown 这类裸枚举
        cycle_options = [
            {"value": c, "label": _bilingual_cycle(c)} for c in sorted(counts)
        ]
        return {
            "total": len(rows),
            "entries": rows,
            "cycles": counts,
            "cycle_options": cycle_options,
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
