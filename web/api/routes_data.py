"""REST routes for data sources, symbols, timeframes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pa_agent.data.base import KlineBar, KlineFrame
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
    kind: str = "tradingview"
    symbol: str = "XAUUSD"
    timeframe: str = "15m"
    exchange: str = ""


@router.get("/datasources")
async def list_datasources():
    return [
        {"id": k, "label": v} for k, v in DATA_SOURCE_CHOICES
    ]


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

    if kind != old_kind:
        try:
            ctx.data_source.disconnect()
        except Exception:
            pass
        ctx.data_source = create_data_source(kind)

    try:
        ctx.data_source.connect()
        if kind == "tradingview" and hasattr(ctx.data_source, "set_exchange"):
            ctx.data_source.set_exchange(req.exchange)
        ctx.data_source.subscribe(req.symbol, req.timeframe)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    ctx.settings.general.last_data_source = kind
    ctx.settings.general.last_symbol = req.symbol
    ctx.settings.general.last_timeframe = req.timeframe
    if kind == "tradingview":
        ctx.settings.general.last_tradingview_exchange = req.exchange

    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import save_settings
    save_settings(ctx.settings, SETTINGS_JSON_PATH)

    return {
        "status": "subscribed",
        "kind": kind,
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "exchange": req.exchange,
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
        })
    return {
        "symbol": ctx.settings.general.last_symbol,
        "timeframe": ctx.settings.general.last_timeframe,
        "bars": bars,
    }
