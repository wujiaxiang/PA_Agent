"""REST routes for data sources, symbols, timeframes."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request
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
    # Prefer the data source's list_symbols(exchange) if available (TradingView)
    if hasattr(ds, "list_symbols"):
        try:
            syms = ds.list_symbols(exchange)
        except TypeError:
            # Older data sources have list_symbols() without args
            syms = ds.list_symbols()
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
        tfs = ctx.data_source.supported_timeframes()
    except Exception:
        tfs = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
    return tfs


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
            ctx.data_source.disconnect()
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
        if not already_connected:
            ctx.data_source.connect()
        if kind == "tradingview" and hasattr(ctx.data_source, "set_exchange"):
            ctx.data_source.set_exchange(exchange)
        ctx.data_source.subscribe(symbol, timeframe)
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
    save_settings(ctx.settings, SETTINGS_JSON_PATH)

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
    bars_raw = ctx.data_source.latest_snapshot(count)
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
    """
    ctx = request.app.state.ctx
    tf = timeframe or getattr(ctx.settings.general, "last_timeframe", "") or ""
    # symbol / exchange are accepted for symmetry with /api/subscribe but
    # are not strictly required — we read the forming bar from the
    # current data source regardless.
    bars_raw = ctx.data_source.latest_snapshot(2)
    if not bars_raw:
        return {
            "symbol": symbol or getattr(ctx.settings.general, "last_symbol", ""),
            "timeframe": tf,
            "next_close_ts": None,
            "seconds_remaining": None,
        }
    forming = bars_raw[0]
    ts_open_ms = int(getattr(forming, "ts_open", 0))
    if ts_open_ms <= 0:
        return {
            "symbol": symbol or getattr(ctx.settings.general, "last_symbol", ""),
            "timeframe": tf,
            "next_close_ts": None,
            "seconds_remaining": None,
        }
    seconds_remaining = seconds_until_bar_closes(ts_open_ms, tf)
    # next_close_ts = ts_open + duration (ms)
    from pa_agent.data.bar_close_wait import timeframe_to_seconds

    duration_s = timeframe_to_seconds(tf)
    next_close_ts = (ts_open_ms + duration_s * 1000) if duration_s else None
    return {
        "symbol": symbol or getattr(ctx.settings.general, "last_symbol", ""),
        "timeframe": tf,
        "next_close_ts": next_close_ts,
        "seconds_remaining": seconds_remaining,
    }
