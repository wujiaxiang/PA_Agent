"""TradingView data source using tvdatafeed."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from pa_agent.data.base import (
    DataSource,
    DataSourceTransientError,
    KlineBar,
    normalize_kline_bar,
)
from pa_agent.data.datetime_ts import datetime_to_ts_ms
from pa_agent.data.market_defaults import (
    is_tv_exchange_auto,
    resolve_tv_fetch_pair,
    tv_auto_probe_plan,
)
from pa_agent.data.refresh_policy import tv_cache_ttl_seconds
from pa_agent.data.tv_symbol_lookup import TvSymbolNotFoundError, is_tv_name_input
from pa_agent.data.tradingview_errors import format_tradingview_fetch_error

logger = logging.getLogger(__name__)

# One attempt per fetch cycle. Each tvDatafeed get_hist() that times out
# blocks for up to _TV_WS_TIMEOUT_S, so retrying here multiplies the worst-case
# wait the user sees on a slow/blocked connection. The RefreshLoop already does
# its own exponential backoff + retry across ticks, so a per-call retry only
# stacks latency without adding resilience.
_TV_FETCH_RETRIES = 1
_TV_FETCH_RETRY_SLEEP_S = 0.5

# Override tvDatafeed's hardcoded 15s WebSocket timeout. Once the socket leak
# (see _close_tv_socket) is fixed, healthy fetches complete in 1-3s, so this
# only bounds the worst case on a stalled connection.
_TV_WS_TIMEOUT_S = 10.0

# Name-mangled attribute tvDatafeed uses internally for its socket timeout.
_TV_WS_TIMEOUT_ATTR = "_TvDatafeed__ws_timeout"

# Name-mangled attribute tvDatafeed uses for its WebSocket handshake headers.
# tvdatafeed 2.1.0 ships `__ws_headers = json.dumps({"Origin": ...})` which
# serialises the dict to a JSON string; websocket-client then treats each
# character as a header key and the handshake fails. We patch it back to a
# real dict before any TvDatafeed instance is constructed.
_TV_WS_HEADERS_ATTR = "_TvDatafeed__ws_headers"
_tv_ws_headers_patched = False


def _patch_tvdatafeed_ws_headers() -> None:
    """Monkey-patch tvdatafeed's WebSocket headers from JSON string to dict.

    Idempotent: subsequent calls are no-ops. See TODO.md §1.3 / P0.3 for
    background. Implemented as runtime patch (not source fork) so we don't
    have to maintain a tvdatafeed fork.
    """
    global _tv_ws_headers_patched
    if _tv_ws_headers_patched:
        return
    try:
        from tvDatafeed import TvDatafeed  # type: ignore[import]

        cur = getattr(TvDatafeed, _TV_WS_HEADERS_ATTR, None)
        if isinstance(cur, str):
            import json

            try:
                setattr(TvDatafeed, _TV_WS_HEADERS_ATTR, json.loads(cur))
                logger.info("Patched tvDatafeed __ws_headers (str → dict)")
            except json.JSONDecodeError:
                # Already a dict-like string we can't parse — force the canonical value.
                setattr(
                    TvDatafeed,
                    _TV_WS_HEADERS_ATTR,
                    {"Origin": "https://data.tradingview.com"},
                )
                logger.warning(
                    "tvDatafeed __ws_headers unparseable, set to canonical Origin dict"
                )
    except Exception:  # noqa: BLE001
        logger.debug("tvDatafeed ws headers patch skipped", exc_info=True)
    finally:
        _tv_ws_headers_patched = True


# Browser-like User-Agent for TradingView signin. tvDatafeed 2.1.0 uses
# requests' default UA which TradingView silently rate-limits; a browser UA
# is required for the /accounts/signin/ endpoint to return real auth tokens.
_TV_AUTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Name-mangled attribute tvDatafeed 2.1.0 uses for its signin method.
_TV_AUTH_ATTR = "_TvDatafeed__auth"

_tv_auth_patched = False

# ── auth token cache ────────────────────────────────────────────────────────
# tvDatafeed 2.1.0 calls __auth on every TvDatafeed(username, password)
# construction. Our connect() is invoked on server startup, on data-source
# type switch, and on reconnect-after-error — each login attempt hits
# TradingView's /accounts/signin/ endpoint, and rapid repeated logins
# trigger risk control (rate_limit, recaptcha_required). To avoid that,
# we cache the auth_token both in-memory (process lifetime) and on disk
# (across restarts), keyed by (username, password_hash).
#
# TradingView's JWT auth_token is valid for ~7 days; we use a conservative
# 24h TTL so a long-running server won't keep using a stale token.
import hashlib
import json as _json
import os as _os
import re as _re
import time as _time
from pathlib import Path as _Path

_TV_TOKEN_TTL_S = 24 * 3600  # 24 hours
_TV_TOKEN_CACHE_ENV = _os.environ.get("PA_AGENT_TV_TOKEN_CACHE", "")
_TV_TOKEN_CACHE_FILE = (
    _Path(_TV_TOKEN_CACHE_ENV)
    if _TV_TOKEN_CACHE_ENV
    else _Path(__file__).resolve().parent.parent.parent / "config" / ".tv_token_cache.json"
)

# In-memory cache: (username, password_hash) -> (token, saved_at_epoch)
_tv_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


def _hash_password(password: str) -> str:
    """SHA-256 of password — stored on disk instead of plaintext creds."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _load_disk_cache() -> dict[tuple[str, str], tuple[str, float]]:
    """Read the on-disk token cache. Returns empty dict on any error."""
    try:
        if not _TV_TOKEN_CACHE_FILE.exists():
            return {}
        raw = _json.loads(_TV_TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        out: dict[tuple[str, str], tuple[str, float]] = {}
        for entry in raw.get("tokens", []) if isinstance(raw, dict) else []:
            try:
                u = str(entry.get("username", ""))
                h = str(entry.get("password_hash", ""))
                t = str(entry.get("token", ""))
                ts = float(entry.get("saved_at", 0))
                if u and h and t and ts > 0:
                    out[(u, h)] = (t, ts)
            except (TypeError, ValueError):
                continue
        return out
    except Exception:  # noqa: BLE001
        return {}


def _save_disk_cache() -> None:
    """Persist the in-memory cache to disk. Best-effort, never raises."""
    try:
        _TV_TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "username": u,
                "password_hash": h,
                "token": t,
                "saved_at": ts,
            }
            for (u, h), (t, ts) in _tv_token_cache.items()
        ]
        _TV_TOKEN_CACHE_FILE.write_text(
            _json.dumps({"tokens": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("tvDatafeed token cache write failed: %s", exc)


def _patch_tvdatafeed_auth() -> None:
    """Monkey-patch tvDatafeed 2.1.0's ``__auth`` to use a real session.

    The stock implementation calls ``requests.post`` directly with no
    session, no User-Agent, and no cookies. TradingView's /accounts/signin/
    endpoint returns ``{"error": "...", "code": "rate_limit"}`` for such
    bare requests — even when the credentials are correct — so tvDatafeed
    silently falls back to anonymous mode and logs ``error while signin``.

    Fix: open a ``requests.Session``, set a browser UA, GET the homepage
    to seed cookies, then POST the signin form. Verified to return a valid
    ``user.auth_token`` (846-char JWT) for accounts that can log in via Web.

    Token caching: the resulting auth_token is cached both in-memory and
    on disk (``config/.tv_token_cache.json``), keyed by
    ``(username, sha256(password))`` with a 24h TTL. Subsequent calls
    (new TvDatafeed constructions, server restarts) return the cached
    token directly without hitting /accounts/signin/ — this avoids
    triggering TradingView's risk control (recaptcha_required) on
    frequent logins.

    Idempotent — subsequent calls are no-ops.
    """
    global _tv_auth_patched
    if _tv_auth_patched:
        return

    # Load disk cache into memory once at patch time
    _tv_token_cache.update(_load_disk_cache())
    if _tv_token_cache:
        logger.info(
            "tvDatafeed token cache loaded: %d entr(y|ies) from %s",
            len(_tv_token_cache), _TV_TOKEN_CACHE_FILE,
        )

    try:
        import requests
        from tvDatafeed import TvDatafeed  # type: ignore[import]

        # Preserve the original so we can fall back if our path fails
        # unexpectedly (e.g. TradingView changes its signin API entirely).
        original_auth = getattr(TvDatafeed, _TV_AUTH_ATTR, None)

        def _patched_auth(self, username, password):
            if not username or not password:
                return None

            # 1. Cache lookup — if we have a fresh token for these creds,
            #    return it without hitting TradingView.
            cache_key = (username, _hash_password(password))
            cached = _tv_token_cache.get(cache_key)
            if cached is not None:
                token, saved_at = cached
                age = _time.time() - saved_at
                if age < _TV_TOKEN_TTL_S and token:
                    logger.info(
                        "tvDatafeed auth: using cached token (age=%.1fh, ttl=%.0fh)",
                        age / 3600, _TV_TOKEN_TTL_S / 3600,
                    )
                    return token
                # Expired — evict; fall through to network login
                logger.info(
                    "tvDatafeed auth: cached token expired (age=%.1fh), re-login",
                    age / 3600,
                )
                _tv_token_cache.pop(cache_key, None)

            # 2. Network login — session + UA + cookies.
            session = requests.Session()
            session.headers.update({"User-Agent": _TV_AUTH_USER_AGENT})
            try:
                # 2a. Seed session cookies (TradingView rejects POSTs without them)
                session.get("https://www.tradingview.com/", timeout=15)
                # 2b. POST signin form
                resp = session.post(
                    "https://www.tradingview.com/accounts/signin/",
                    data={
                        "username": username,
                        "password": password,
                        "remember": "on",
                    },
                    headers={"Referer": "https://www.tradingview.com"},
                    timeout=15,
                )
                data = resp.json()
                token = (data.get("user") or {}).get("auth_token")
                if token:
                    # 3. Persist to in-memory + disk cache
                    _tv_token_cache[cache_key] = (token, _time.time())
                    _save_disk_cache()
                    logger.info(
                        "tvDatafeed auth: login OK, token cached (len=%d)",
                        len(token),
                    )
                    return token
                # TradingView returns {"error": "...", "code": "..."} on failure
                err = data.get("error") or "unknown error"
                code = data.get("code") or ""
                logger.warning(
                    "tvDatafeed auth failed: error=%r code=%r", err, code,
                )
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "tvDatafeed patched __auth exception: %s; "
                    "falling back to original implementation", exc,
                )
                if original_auth is not None:
                    return original_auth(self, username, password)
                return None

        setattr(TvDatafeed, _TV_AUTH_ATTR, _patched_auth)
        logger.info("Patched tvDatafeed __auth (session + UA + token cache)")
    except ImportError:
        logger.debug("tvDatafeed not installed; __auth patch skipped")
    except Exception as exc:  # noqa: BLE001
        logger.warning("tvDatafeed __auth patch failed: %s", exc)
    finally:
        _tv_auth_patched = True

# Map our timeframe strings to tvDatafeed Interval enum names
_TF_MAP: dict[str, str] = {
    "1m":  "in_1_minute",
    "3m":  "in_3_minute",
    "5m":  "in_5_minute",
    "15m": "in_15_minute",
    "30m": "in_30_minute",
    "45m": "in_45_minute",
    "1h":  "in_1_hour",
    "2h":  "in_2_hour",
    "3h":  "in_3_hour",
    "4h":  "in_4_hour",
    "1d":  "in_daily",
    "1w":  "in_weekly",
    "1M":  "in_monthly",
}

# Forex / spot gold and China A-share (tvDatafeed exchange ids)
TV_EXCHANGE_PRESETS: tuple[str, ...] = (
    "GATEIO",
    "BINANCE",
    "BYBIT",
    "OKX",
    "BITSTAMP",
    "COINBASE",
    "OANDA",
    "PEPPERSTONE",
    "FOREXCOM",
    "TVC",
    "CAPITALCOM",
    "SSE",
    "SZSE",
    "HKEX",
    "SP",
    "NYSE",
    "NASDAQ",
    "CBOT",
    "CME_MINI",
    "",
)

# Common symbols per exchange — TradingView has no public "list all symbols" API,
# so we provide a curated preset per exchange.  Users can still type any symbol
# manually in the frontend (the symbol input is a combobox, not a strict select).
TV_SYMBOL_PRESETS: dict[str, list[str]] = {
    # ── 加密货币 ──────────────────────────────────────────────────────────
    "GATEIO": [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT",
        "LTCUSDT", "TRXUSDT", "DOTUSDT", "BCHUSDT", "ATOMUSDT",
        "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
        "RNDRUSDT", "FILUSDT", "ETCUSDT", "UNIUSDT", "AAVEUSDT",
        "SUIUSDT", "SEIUSDT", "TIAUSDT", "PEPEUSDT", "WIFUSDT",
    ],
    "BINANCE": [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
        "LTCUSDT", "TRXUSDT", "BCHUSDT", "NEARUSDT", "APTUSDT",
        "ARBUSDT", "OPUSDT", "FILUSDT", "ETCUSDT", "UNIUSDT",
        "ATOMUSDT", "ICPUSDT", "INJUSDT", "SUIUSDT", "TIAUSDT",
    ],
    "BYBIT": [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT",
        "LTCUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
        "WIFUSDT", "PEPEUSDT", "SUIUSDT", "TIAUSDT", "SEIUSDT",
    ],
    "OKX": [
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
        "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "TON-USDT",
        "LTC-USDT", "TRX-USDT", "DOT-USDT", "ARB-USDT", "OP-USDT",
        "APT-USDT", "NEAR-USDT", "FIL-USDT", "SUI-USDT", "TIA-USDT",
    ],
    "BITSTAMP": ["BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD", "BCHUSD",
                 "LINKUSD", "SOLUSD", "ADAUSD", "DOGEUSD", "DOTUSD"],
    "COINBASE": ["BTCUSD", "ETHUSD", "SOLUSD", "ADAUSD", "XRPUSD",
                 "LTCUSD", "DOGEUSD", "AVAXUSD", "LINKUSD", "MATICUSD"],
    # ── 外汇 / 贵金属 ─────────────────────────────────────────────────────
    "OANDA": [
        "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
        "USDCAD", "NZDUSD", "USDCHF", "EURJPY", "GBPJPY",
        "EURGBP", "AUDJPY", "USDMXN", "USDZAR", "XAGUSD",
        "XPTUSD", "USDSGD", "USDHKD", "GBPAUD", "EURCHF",
    ],
    "PEPPERSTONE": [
        "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
        "USDCAD", "NZDUSD", "USDCHF", "EURJPY", "GBPJPY",
        "XAGUSD", "EURGBP", "AUDJPY", "USDMXN", "USDSGD",
    ],
    "FOREXCOM": [
        "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
        "USDCAD", "NZDUSD", "USDCHF", "EURJPY", "GBPJPY",
        "XAGUSD", "EURGBP", "AUDJPY", "USDMXN", "USDZAR",
    ],
    "TVC": [
        "XAUUSD", "GOLD", "EURUSD", "GBPUSD", "USDJPY",
        "AUDUSD", "USDCAD", "NZDUSD", "USDCHF", "XAGUSD",
        "DJI", "SPX", "IXIC", "US10Y", "VIX",
    ],
    "CAPITALCOM": [
        "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD",
        "ETHUSD", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF",
    ],
    # ── A 股 ─────────────────────────────────────────────────────────────
    "SSE": [
        "600519", "601318", "600036", "601398", "600276",
        "601899", "600900", "601166", "600030", "600887",
        "601012", "600585", "601888", "600031", "601628",
    ],
    "SZSE": [
        "000001", "000333", "000651", "002594", "300750",
        "000858", "002415", "000725", "000063", "002230",
        "300059", "300124", "000568", "002352", "300760",
    ],
    # ── 港股 ─────────────────────────────────────────────────────────────
    # 注意：TradingView 的港股代码**不补前导零**。此前写成 0700 / 0388 / 0688 / 0961
    # 的那些代码在 TV 上根本不存在，订阅必然取不到数据；已按 scanner 实测校正为
    # 700（腾讯）/ 388（港交所）/ 688（中国海外发展）/ 1193（华润燃气）等。
    "HKEX": [
        "700", "1810", "9988", "3690", "1299",
        "9618", "9888", "1024", "9999", "9868",
        "2318", "388", "688", "1193", "9992",
    ],
    # ── 美股 ─────────────────────────────────────────────────────────────
    "SP": ["SPX", "SPX500", "NDX", "VIX", "DJI", "RUT", "MID", "S5TH"],

    "NYSE": [
        "AAPL", "MSFT", "AMZN", "TSLA", "JPM", "V", "MA", "WMT",
        "XOM", "JNJ", "PG", "HD", "UNH", "DIS", "BAC", "CVX",
    ],
    "NASDAQ": [
        "AAPL", "MSFT", "GOOGL", "META", "NVDA", "AMD", "NFLX",
        "TSLA", "AVGO", "AMZN", "ADBE", "CRM", "INTC", "QCOM",
        "MU", "COST", "SMCI", "PLTR",
    ],
    # ── 期货 / 商品 ──────────────────────────────────────────────────────
    "CBOT": ["ZC", "ZS", "ZW", "ZL", "ZM", "ZO", "LE", "GF", "HE",
             "KC", "CC", "CT", "SB"],
    "CME_MINI": ["ES", "NQ", "YM", "RTY", "CL", "NG", "GC", "SI",
                 "6E", "6J", "6B", "BTC", "MBT"],
    # ── 无交易所上下文时的通用清单（按「贵金属 / 加密 / 外汇」顺序）──
    "": [
        "XAUUSD", "XAGUSD", "BTCUSDT", "ETHUSDT", "SOLUSDT",
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
        "GOLD", "BTCUSD", "ETHUSD", "GBPUSD", "USDCHF",
    ],
}

# Symbol name mappings for display purposes
TV_SYMBOL_NAMES: dict[str, str] = {
    # ── 加密 ────────────────────────────────────────────────────────────
    "BTCUSDT": "比特币", "ETHUSDT": "以太坊", "SOLUSDT": "Solana",
    "BNBUSDT": "币安币", "XRPUSDT": "瑞波币", "DOGEUSDT": "狗狗币",
    "ADAUSDT": "卡尔达诺", "AVAXUSDT": "Avalanche", "LINKUSDT": "Chainlink",
    "TONUSDT": "Toncoin", "LTCUSDT": "莱特币", "TRXUSDT": "波场",
    "DOTUSDT": "Polkadot", "BCHUSDT": "比特币现金", "ATOMUSDT": "Cosmos",
    "NEARUSDT": "NEAR", "APTUSDT": "Aptos", "ARBUSDT": "Arbitrum",
    "OPUSDT": "Optimism", "INJUSDT": "Injective", "RNDRUSDT": "Render",
    "FILUSDT": "Filecoin", "ETCUSDT": "以太经典", "UNIUSDT": "Uniswap",
    "AAVEUSDT": "Aave", "SUIUSDT": "Sui", "SEIUSDT": "Sei",
    "TIAUSDT": "Celestia", "PEPEUSDT": "佩佩", "WIFUSDT": "dogwifhat",
    "ICPUSDT": "ICP", "MATICUSD": "Polygon",
    "BTC-USDT": "比特币", "ETH-USDT": "以太坊", "SOL-USDT": "Solana",
    "BNB-USDT": "币安币", "XRP-USDT": "瑞波币", "DOGE-USDT": "狗狗币",
    "ADA-USDT": "卡尔达诺", "AVAX-USDT": "Avalanche", "LINK-USDT": "Chainlink",
    "TON-USDT": "Toncoin", "LTC-USDT": "莱特币", "TRX-USDT": "波场",
    "DOT-USDT": "Polkadot", "ARB-USDT": "Arbitrum", "OP-USDT": "Optimism",
    "APT-USDT": "Aptos", "NEAR-USDT": "NEAR", "FIL-USDT": "Filecoin",
    "SUI-USDT": "Sui", "TIA-USDT": "Celestia",
    "BTCUSD": "比特币", "ETHUSD": "以太坊", "SOLUSD": "Solana",
    "LTCUSD": "莱特币", "XRPUSD": "瑞波币", "BCHUSD": "比特币现金",
    "LINKUSD": "Chainlink", "ADAUSD": "卡尔达诺", "DOGEUSD": "狗狗币",
    "DOTUSD": "Polkadot", "AVAXUSD": "Avalanche",
    # ── 外汇 / 贵金属 ────────────────────────────────────────────────────
    "XAUUSD": "黄金", "XAGUSD": "白银", "XPTUSD": "铂金", "GOLD": "黄金(GC)",
    "EURUSD": "欧元/美元", "GBPUSD": "英镑/美元", "USDJPY": "美元/日元",
    "AUDUSD": "澳元/美元", "USDCAD": "美元/加元", "NZDUSD": "纽元/美元",
    "USDCHF": "美元/瑞郎", "EURJPY": "欧元/日元", "GBPJPY": "英镑/日元",
    "EURGBP": "欧元/英镑", "AUDJPY": "澳元/日元", "GBPAUD": "英镑/澳元",
    "EURCHF": "欧元/瑞郎", "USDMXN": "美元/比索", "USDZAR": "美元/兰特",
    "USDSGD": "美元/新元", "USDHKD": "美元/港币",
    # ── 美股 ────────────────────────────────────────────────────────────
    "AAPL": "苹果", "MSFT": "微软", "AMZN": "亚马逊", "TSLA": "特斯拉",
    "NVDA": "英伟达", "GOOGL": "谷歌", "META": "Meta", "AMD": "AMD超微",
    "NFLX": "奈飞", "AVGO": "博通", "ADBE": "Adobe", "CRM": "Salesforce",
    "INTC": "英特尔", "QCOM": "高通", "MU": "美光", "COST": "好市多",
    "SMCI": "超微电脑", "PLTR": "Palantir", "JPM": "摩根大通", "V": "维萨",
    "MA": "万事达", "WMT": "沃尔玛", "XOM": "埃克森美孚", "JNJ": "强生",
    "PG": "宝洁", "HD": "家得宝", "UNH": "联合健康", "DIS": "迪士尼",
    "BAC": "美国银行", "CVX": "雪佛龙",
    "SPX": "标普500", "SPX500": "标普500期货", "NDX": "纳斯达克100",
    "VIX": "恐慌指数", "DJI": "道琼斯", "IXIC": "纳斯达克综指",
    "US10Y": "美债10年",
    # ── A 股 ────────────────────────────────────────────────────────────
    "600519": "贵州茅台", "601318": "中国平安", "600036": "招商银行",
    "601398": "工商银行", "600276": "恒瑞医药", "601899": "紫金矿业",
    "600900": "长江电力", "601166": "兴业银行", "600030": "中信证券",
    "600887": "伊利股份", "601012": "隆基绿能", "600585": "海螺水泥",
    "601888": "中国中免", "600031": "三一重工", "601628": "中国人寿",
    "000001": "平安银行", "000333": "美的集团", "000651": "格力电器",
    "002594": "比亚迪", "300750": "宁德时代", "000858": "五粮液",
    "002415": "海康威视", "000725": "京东方A", "000063": "中兴通讯",
    "002230": "科大讯飞", "300059": "东方财富", "300124": "汇川技术",
    "000568": "泸州老窖", "300760": "迈瑞医疗", "002352": "顺丰控股",
    # ── 港股 ────────────────────────────────────────────────────────────
    "700": "腾讯控股", "1810": "小米集团", "9988": "阿里巴巴",
    "3690": "美团", "1299": "友邦保险", "9618": "京东集团",
    "9888": "百度", "1024": "快手", "9999": "网易", "9868": "小鹏汽车",
    "2318": "中国平安(H)", "388": "香港交易所", "688": "中国海外发展",
    "1193": "华润燃气", "9992": "泡泡玛特", "1698": "腾讯音乐",
    # ── 期货 ────────────────────────────────────────────────────────────
    "ZC": "玉米", "ZS": "大豆", "ZW": "小麦", "ZL": "豆油",
    "ZM": "豆粕", "ZO": "燕麦", "LE": "活牛", "GF": " feeder cattle",
    "HE": "瘦肉猪", "ES": "标普期货", "NQ": "纳指期货", "YM": "道指期货",
    "RTY": "罗素期货", "CL": "原油", "NG": "天然气", "GC": "黄金期货",
    "SI": "白银期货", "RUT": "罗素2000", "MID": "标普中盘400",
    "S5TH": "标普500等权", "KC": "红 KC 冬小麦", "CC": "可可",
    "CT": "棉花", "SB": "糖11号", "6E": "欧元期货", "6J": "日元期货",
    "6B": "英镑期货", "BTC": "比特币期货", "MBT": "微型比特币",
}



# ── Session ID → auth_token extraction ──────────────────────────────────────

_TV_CHART_URL = "https://www.tradingview.com/chart/"
_TV_AUTH_TOKEN_PATTERN = _re.compile(r'"auth_token"\s*:\s*"([^"]+)"')


def _fetch_auth_token_via_session(session_id: str) -> str | None:
    """Extract TradingView ``auth_token`` (JWT) using a ``sessionid`` cookie.

    Instead of POSTing to ``/accounts/signin/`` (which triggers reCAPTCHA),
    we GET the chart page with the ``sessionid`` cookie and extract the
    ``auth_token`` from the embedded JSON. This works because TradingView's
    server-side rendering includes the auth_token when the user is logged in.

    Returns the JWT string (typically ~846 chars) or ``None`` on failure.
    """
    import requests as _requests

    session = _requests.Session()
    session.headers.update({
        "User-Agent": _TV_AUTH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    session.cookies.set("sessionid", session_id, domain=".tradingview.com", path="/")

    try:
        resp = session.get(_TV_CHART_URL, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            logger.warning(
                "session_id auth: chart page returned status %d", resp.status_code
            )
            return None

        match = _TV_AUTH_TOKEN_PATTERN.search(resp.text)
        if match:
            token = match.group(1)
            if len(token) > 100:  # sanity check: JWT should be 800+ chars
                return token
            logger.warning("session_id auth: extracted token too short (%d chars)", len(token))
            return None

        logger.warning("session_id auth: auth_token not found in chart page HTML")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_id auth: request failed: %s", exc)
        return None


class TradingViewSource(DataSource):
    """Live K-line data from TradingView via tvdatafeed."""

    def __init__(self, username: str = "", password: str = "", session_id: str = "") -> None:
        self._username = username
        self._password = password
        self._session_id = session_id
        self._tv = None          # tvDatafeed instance
        self._connected: bool = False
        self._symbol: str = ""
        self._timeframe: str = ""
        self._exchange: str = ""
        # Mutex: tvDatafeed is NOT thread-safe — its get_hist() creates a
        # WebSocket and stores it on self.ws; concurrent calls clobber the
        # same socket and cause C++ segfaults.
        self._snapshot_lock = threading.Lock()
        # TTL cache for latest_snapshot(): avoids opening a fresh WebSocket
        # on every call. Mirrors EastMoneySource's _snap_cache_* pattern.
        self._snap_cache_bars: list | None = None
        self._snap_cache_n: int = 0
        self._snap_cache_ts: float = 0.0
        # Callback for status updates during auto-probe: fn(symbol, exchange, label)
        self.on_probe_status = None

    @property
    def exchange(self) -> str:
        return self._exchange

    def set_exchange(self, exchange: str) -> None:
        """Set TradingView exchange id (e.g. ``BINANCE``); empty = auto-detect."""
        self._exchange = (exchange or "").strip().upper()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            from tvDatafeed import TvDatafeed  # type: ignore[import]
            _patch_tvdatafeed_ws_headers()
            _patch_tvdatafeed_auth()
            if self._session_id:
                # Use sessionid cookie to extract auth_token from chart page HTML.
                # This bypasses /accounts/signin/ entirely — no reCAPTCHA risk.
                auth_token = _fetch_auth_token_via_session(self._session_id)
                if auth_token:
                    self._tv = TvDatafeed()
                    self._tv.token = auth_token
                    self._tv.authenticated = True
                    logger.info("TradingViewSource connected (session_id → auth_token, length=%d)", len(auth_token))
                else:
                    logger.warning("session_id auth failed (could not extract auth_token), falling back to anonymous")
                    self._tv = TvDatafeed()
            elif self._username and self._password:
                self._tv = TvDatafeed(self._username, self._password)
            else:
                self._tv = TvDatafeed()  # anonymous
            try:
                setattr(self._tv, _TV_WS_TIMEOUT_ATTR, _TV_WS_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                logger.debug("Could not override tvDatafeed ws timeout", exc_info=True)
            self._connected = True
            auth_mode = "session_id" if self._session_id else ("credentials" if self._username else "anonymous")
            logger.info("TradingViewSource connected (%s)", auth_mode)
        except ImportError as exc:
            self._connected = False
            msg = str(exc)
            if "numpy" in msg.lower() or "X86_V2" in msg:
                logger.warning(
                    "NumPy 与当前 CPU 不兼容，无法获取 TradingView 数据。"
                    "请尝试安装兼容的 NumPy 版本或使用 A股数据源。"
                )
            else:
                logger.warning(
                    "tvDatafeed 未安装，无法获取 TradingView 实时数据。请执行: "
                    "pip install git+https://github.com/rongardF/tvdatafeed.git"
                )
        except Exception as exc:
            self._connected = False
            logger.warning("TradingView 连接失败：%s", exc)

    def disconnect(self) -> None:
        self._close_tv_socket()
        self._tv = None
        self._connected = False
        logger.info("TradingViewSource disconnected")

    def _close_tv_socket(self) -> None:
        """Close the live tvDatafeed WebSocket, if any.

        tvDatafeed 2.x opens a brand-new socket on *every* ``get_hist()`` call
        and never closes the previous one — a leak that piles up half-open
        connections and trips TradingView's rate limiting. Closing the socket
        after each fetch fixes the leak, and closing it mid-flight is also the
        only way to abort a ``recv()`` that is blocked waiting on a stalled
        connection (e.g. when the user switches symbol/timeframe).

        Safe to call from another thread: ``socket.close()`` will raise inside
        the blocked ``recv()``, which tvDatafeed catches and turns into an
        empty result.
        """
        tv = self._tv
        if tv is None:
            return
        ws = getattr(tv, "ws", None)
        if ws is None:
            return
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            logger.debug("tvDatafeed socket close failed", exc_info=True)
        finally:
            try:
                tv.ws = None
            except Exception:  # noqa: BLE001
                pass

    # ── Discovery ─────────────────────────────────────────────────────────────

    def list_exchanges(self) -> list[str]:
        """Return curated TradingView exchange ids (empty string = auto)."""
        return [e for e in TV_EXCHANGE_PRESETS]

    def list_symbols(self, exchange: str = "") -> list[str]:
        """Return curated symbols for *exchange* (empty = generic list)."""
        ex = (exchange or "").strip().upper()
        if ex and ex in TV_SYMBOL_PRESETS:
            return list(TV_SYMBOL_PRESETS[ex])
        return list(TV_SYMBOL_PRESETS.get("", ["XAUUSD", "BTCUSDT", "ETHUSDT"]))

    def supported_timeframes(self) -> list[str]:
        return list(_TF_MAP.keys())

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _TF_MAP:
            raise ValueError(f"Unsupported timeframe: {timeframe!r}. Use one of {list(_TF_MAP)}")
        if symbol.strip() != self._symbol or timeframe != self._timeframe:
            # Invalidate snapshot cache so the next latest_snapshot() doesn't
            # return bars for the previous symbol/timeframe.
            self._snap_cache_bars = None
            self._snap_cache_n = 0
            self._snap_cache_ts = 0.0
        self._timeframe = timeframe
        self._symbol = symbol.strip()
        # Abort any in-flight get_hist() blocked on a stalled connection so the
        # new symbol/timeframe takes effect immediately instead of waiting out
        # the previous request's timeout. Closing the socket raises inside the
        # worker thread's recv(); the next fetch transparently reconnects.
        self._close_tv_socket()
        logger.info(
            "TradingViewSource subscribed: %s %s exchange=%s",
            self._symbol,
            timeframe,
            self._exchange or "(auto)",
        )

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""
        self._snap_cache_bars = None
        self._snap_cache_n = 0
        self._snap_cache_ts = 0.0
        logger.info("TradingViewSource unsubscribed")

    # ── Data fetch ────────────────────────────────────────────────────────────

    def _fetch_hist_with_retry(
        self,
        *,
        symbol: str,
        exchange: str,
        interval: object,
        n_bars: int,
    ):
        """Call tvDatafeed get_hist with retries (timeouts / empty are common)."""
        logger.debug(
            "TradingView get_hist: symbol=%s, exchange=%s, interval=%s, n_bars=%d",
            symbol, exchange, interval, n_bars,
        )
        last_exc: BaseException | None = None
        for attempt in range(1, _TV_FETCH_RETRIES + 1):
            try:
                df = self._tv.get_hist(
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    n_bars=n_bars,
                )
                if df is not None and not df.empty:
                    return df
                logger.warning(
                    "TradingView get_hist attempt %s/%s returned empty data: symbol=%s, exchange=%s, interval=%s",
                    attempt, _TV_FETCH_RETRIES, symbol, exchange, interval,
                )
                last_exc = None
            except Exception as exc:
                last_exc = exc
                logger.debug(
                    "TradingView get_hist attempt %s/%s failed: %s",
                    attempt,
                    _TV_FETCH_RETRIES,
                    exc,
                )
            finally:
                # tvDatafeed leaks the WebSocket it opens on every get_hist()
                # call. Close it here so half-open sockets don't accumulate and
                # trip TradingView rate limiting; the next call reconnects.
                self._close_tv_socket()
            if attempt < _TV_FETCH_RETRIES:
                time.sleep(_TV_FETCH_RETRY_SLEEP_S)
        if last_exc is not None:
            raise last_exc
        return None

    def _fetch_tv_auto_probe(
        self,
        *,
        symbol: str,
        plan: list[tuple[str, str]],
        interval: object,
        n_bars: int,
    ) -> tuple[object, str]:
        """Try each (exchange, symbol) in *plan* until one returns bars."""
        if not plan:
            raise DataSourceTransientError(
                f"TradingView 无法识别品种「{symbol}」；"
                "请用 A 股 6 位代码、港股代码（如 1810）、"
                "指数代码（如 SPX、NDX、VIX）、"
                "外汇/黄金代码或已支持的股票名称"
            )
        last_exc: BaseException | None = None
        tried: list[str] = []
        for exchange, code in plan:
            label = f"{exchange}:{code}"
            tried.append(label)
            # Notify GUI about current probe attempt
            if self.on_probe_status is not None:
                try:
                    self.on_probe_status(symbol, exchange, label)
                except Exception:  # noqa: BLE001
                    pass
            try:
                df = self._fetch_hist_with_retry(
                    symbol=code,
                    exchange=exchange,
                    interval=interval,
                    n_bars=n_bars,
                )
            except Exception as exc:
                last_exc = exc
                logger.info("TradingView auto probe %s failed: %s", label, exc)
                continue
            if df is not None and not df.empty:
                logger.info(
                    "TradingView auto probe picked %s (tried %s)",
                    label,
                    ", ".join(tried),
                )
                return df, exchange
        if last_exc is not None:
            raise last_exc
        raise DataSourceTransientError(
            f"TradingView 自动探测失败（{symbol}）：已尝试 {', '.join(tried)} 均无 K 线"
        )

    def clear_snapshot_cache(self) -> None:
        """Clear the TTL snapshot cache to force a fresh fetch on next call."""
        with self._snapshot_lock:
            self._snap_cache_bars = None
            self._snap_cache_n = 0
            self._snap_cache_ts = 0.0

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        """Return *n* bars newest-first; bars[0] is the forming (unclosed) bar.

        Thread-safety: serialized via ``_snapshot_lock`` because
        ``TvDatafeed.get_hist()`` is NOT thread-safe — it writes to
        ``self.ws`` on each call, and concurrent access clobbers the
        WebSocket, causing C++ segfaults.

        A TTL cache (``_snap_cache_*``) short-circuits repeated calls within
        ``tv_cache_ttl_seconds(self._timeframe)`` so we don't open a fresh
        WebSocket on every poll. Cache read AND write happen inside the lock
        to prevent races with concurrent callers and with ``subscribe()``.
        """
        with self._snapshot_lock:
            ttl = tv_cache_ttl_seconds(self._timeframe)
            if (
                self._snap_cache_bars is not None
                and self._snap_cache_n == n
                and (time.time() - self._snap_cache_ts) < ttl
            ):
                return list(self._snap_cache_bars)
            bars = self._latest_snapshot_inner(n)
            self._snap_cache_bars = list(bars)
            self._snap_cache_n = n
            self._snap_cache_ts = time.time()
            return bars

    def _latest_snapshot_inner(self, n: int) -> list[KlineBar]:
        """Actual snapshot logic — caller holds ``_snapshot_lock``."""
        if self._tv is None:
            raise DataSourceTransientError("TradingView 未连接，请先选择数据来源 TradingView")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("TradingView 未订阅品种/周期")

        user_symbol = self._symbol
        req_exchange = self._exchange
        exchange = req_exchange or ""
        fetch_symbol = user_symbol
        auto_probe = is_tv_exchange_auto(req_exchange)
        probe_plan = tv_auto_probe_plan(user_symbol) if auto_probe else []
        try:
            from tvDatafeed import Interval  # type: ignore[import]
            interval = getattr(Interval, _TF_MAP[self._timeframe])
            if auto_probe and probe_plan:
                df, exchange = self._fetch_tv_auto_probe(
                    symbol=user_symbol,
                    plan=probe_plan,
                    interval=interval,
                    n_bars=n + 2,
                )
            else:
                try:
                    exchange, fetch_symbol = resolve_tv_fetch_pair(
                        req_exchange, user_symbol
                    )
                except TvSymbolNotFoundError as exc:
                    raise DataSourceTransientError(str(exc)) from exc
                df = self._fetch_hist_with_retry(
                    symbol=fetch_symbol,
                    exchange=exchange,
                    interval=interval,
                    n_bars=n + 2,
                )
        except DataSourceTransientError:
            raise
        except Exception as exc:
            msg = format_tradingview_fetch_error(
                user_symbol, exchange or req_exchange or "自动", cause=exc,
            )
            logger.warning("TradingView fetch failed: %s", exc)
            raise DataSourceTransientError(msg) from exc

        if df is None or df.empty:
            msg = format_tradingview_fetch_error(
                user_symbol, exchange or req_exchange or "自动", empty_data=True,
            )
            logger.debug(
                "TradingView empty data for %s exchange=%s",
                user_symbol,
                exchange or req_exchange or "(auto)",
            )
            raise DataSourceTransientError(msg)

        df = df.iloc[::-1].reset_index()

        bars: list[KlineBar] = []
        for i, row in enumerate(df.itertuples(index=False)):
            ts_ms = _row_ts_ms(row)
            if i == 0:
                # bars[0]: either forming (seq=0) or, when market is closed,
                # the newest closed bar (seq=1) — tvDatafeed returns only
                # closed bars during halt/weekend.
                from pa_agent.data.bar_close_wait import seconds_until_bar_closes

                secs_left = seconds_until_bar_closes(
                    ts_ms, self._timeframe, now_ms=None
                )
                still_forming = secs_left is not None and secs_left > 0
                if still_forming:
                    bar = KlineBar(
                        seq=0,
                        ts_open=ts_ms,
                        open=float(row.open),
                        high=float(row.high),
                        low=float(row.low),
                        close=float(row.close),
                        volume=float(getattr(row, "volume", 0.0)),
                        closed=False,
                    )
                else:
                    # Market-halt mode: bars[0] is the newest closed bar.
                    bar = KlineBar(
                        seq=1,
                        ts_open=ts_ms,
                        open=float(row.open),
                        high=float(row.high),
                        low=float(row.low),
                        close=float(row.close),
                        volume=float(getattr(row, "volume", 0.0)),
                        closed=True,
                    )
            else:
                # Closed bar: seq increments from 1 in halt mode, from 1
                # (starting at index 1) in normal mode.
                head_closed = bool(bars[0].closed)
                seq = i + 1 if head_closed else i
                bar = KlineBar(
                    seq=seq,
                    ts_open=ts_ms,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(getattr(row, "volume", 0.0)),
                    closed=True,
                )
            bars.append(normalize_kline_bar(bar))
            # Normal mode: n+1 bars (1 forming + n closed).
            # Halt mode: n bars (all closed).
            target_len = n if (bars and bars[0].closed) else n + 1
            if len(bars) >= target_len:
                break

        return self._validate_snapshot(n, bars)


def _row_ts_ms(row) -> int:
    """Extract bar open time in milliseconds from a tvDatafeed DataFrame row.

    tvDatafeed returns a naive DatetimeIndex (tz=None) whose wall-clock values
    are in the exchange's local time (typically the host server timezone, e.g.
    UTC+8 for a Shanghai server). The generic ``datetime_to_ts_ms`` treats
    naive values as UTC, which would shift the epoch by the local offset
    (8h for UTC+8) and make bars appear in the future on the chart.

    Fix: localize naive Timestamps to the host timezone, then convert to UTC
    before computing the epoch milliseconds. Timezone-aware values pass through
    unchanged.
    """
    import time as _time
    from datetime import timezone as _tz, timedelta as _td

    dt = getattr(row, "datetime", None)
    if dt is None:
        return int(_time.time() * 1000)
    try:
        import pandas as pd

        if isinstance(dt, pd.Timestamp):
            if dt.tz is None:
                # Naive → assume host local time (matches tvDatafeed behavior)
                local_offset = _tz(_td(seconds=-_time.timezone))
                dt = dt.tz_localize(local_offset).tz_convert("UTC")
            return int(dt.timestamp() * 1000)
    except ImportError:
        pass
    return datetime_to_ts_ms(dt)


# ── TradingView scanner 品种搜索 ─────────────────────────────────────────────
# tvDatafeed 2.1.0 自带的 search_symbol() 已失效（对任意查询都返回非 JSON），
# 但 scanner.tradingview.com 走的是另一条路径，无需登录且实测可用：
#   crypto totalCount=64412 / america=20069 / futures=52721 / china=7476
#   / forex=6333 / hongkong=3060
# 这里用它提供真正的全市场品种搜索，返回值只取「币种/品种 code」，
# 格式与 TV_SYMBOL_PRESETS 中的 code 一致（不含 "EXCHANGE:" 前缀）。
TV_SEARCH_URL_TEMPLATE = "https://scanner.tradingview.com/{market}/scan"

#: 我们的 exchange → scanner market 端点
TV_SEARCH_MARKET_BY_EXCHANGE: dict[str, str] = {
    "GATEIO": "crypto", "BINANCE": "crypto", "BYBIT": "crypto",
    "OKX": "crypto", "BITSTAMP": "crypto", "COINBASE": "crypto",
    "NASDAQ": "america", "NYSE": "america", "SP": "america", "AMEX": "america",
    "OANDA": "forex", "PEPPERSTONE": "forex", "FOREXCOM": "forex",
    "CAPITALCOM": "forex", "TVC": "forex",
    "SSE": "china", "SZSE": "china",
    "HKEX": "hongkong",
    "CBOT": "futures", "CME_MINI": "futures",
}

#: 我们的 exchange → scanner 里的交易所名（不一致的在这里映射）
TV_SEARCH_EXCHANGE_ALIASES: dict[str, str] = {
    "GATEIO": "GATE",
    "CME_MINI": "CBOT_MINI",
}

#: 默认市场（未指定交易所时）
TV_SEARCH_DEFAULT_MARKET = "global"

_TV_SEARCH_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
}


#: 衍生品后缀特征：永续 .P / .F、杠杆代币 3L/3S/5L/5S、期权 .C/.P、
#: 指数/币本位合约、wrapped 与质押变体。
_DERIV_SUFFIXES = (".P", ".F", ".C", ".I", ".PERP", ".PERPETUAL")
#: 外汇/指数的「同品种不同券商」变体：EURUSD 在 OANDA 上同时存在
#: EURUSD / EURUSD.ONE / EURUSD.SML.ONE / EURUSD.PRO.OTMS 等十余个变种，
#: 用户要的是主代码，不是各家的合约规格。
_DERIV_TAIL_TOKENS = (
    "ONE", "PRO", "SML", "ECN", "RAW", "MIC", "SB", "CENT", "MINI", "ULTRA",
    "OTMS", "ECN2", "SPOT", "FUT", "MTM", "PREM", "INV",
)
_DERIV_INFIXES = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")


def _is_derivative(code: str) -> bool:
    """Heuristic: is this ticker a derivative rather than a plain spot pair?"""
    import re as _re
    c = str(code or "").upper()
    if c.endswith(_DERIV_SUFFIXES):
        return True
    # 杠杆代币后缀形如 .3L / .5S / .2L（"BASE" 部分是现货、后面才是杠杆）
    if _re.search(r"\.\d+[LS]$", c):
        return True
    # 券商/规格变体：EURUSD.PRO.OTMS / EURUSD.SML.ONE / GBPUSD.ECN
    for part in c.split(".")[1:]:
        if part in _DERIV_TAIL_TOKENS:
            return True
    base = c.split(".")[0]
    # WBTC / STETH / PUMPBTC —— wrapped / staked / 杠杆包装币不是现货对
    if base.startswith(("W", "ST")) and base[2:5].isalpha() and len(base) > 5:
        if base not in ("WIFUSDT",):
            return True
    # 含计价币之外的中间段，例如 ETHFIUSDT.3L
    if any(seg in base for seg in ("UP", "DOWN", "BULL", "BEAR")):
        return True
    return False


def _relevance(code: str, query: str) -> tuple:
    """Rank: exact > prefix > word-start > plain substring > other."""
    c = str(code or "").upper()
    q = str(query or "").upper()
    if not q:
        return (5, c)
    if c == q:
        return (0, c)
    if c.startswith(q):
        # BTCUSDT 比 BTCUSD.P 更贴合直觉：紧跟计价币的优先
        tail = c[len(q):]
        return (1, len(tail), c)
    if q in c:
        return (2, c.index(q), c)
    return (3, c)


def _tv_search_request(payload: dict, timeout: float = 8.0) -> dict:
    """POST 到 scanner 并返回解析后的 JSON（失败抛异常，由调用方兜底）。"""
    import json as _json
    import urllib.request as _urlreq

    market = payload.get("market") or TV_SEARCH_DEFAULT_MARKET
    url = TV_SEARCH_URL_TEMPLATE.format(market=market)
    body = {k: v for k, v in payload.items() if k != "market"}
    req = _urlreq.Request(
        url,
        data=_json.dumps(body).encode("utf-8"),
        headers=_TV_SEARCH_HEADERS,
        method="POST",
    )
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def search_tv_symbols(
    query: str = "",
    exchange: str = "",
    limit: int = 50,
    logger: Optional[logging.Logger] = None,
) -> list[dict]:
    """Search TradingView's live symbol universe via the scanner API.

    Parameters
    ----------
    query:
        Free text matched against the instrument name. Empty means "list the
        most active symbols on this exchange".
    exchange:
        Our exchange id (e.g. ``GATEIO``); mapped to a scanner market and
        exchange. Empty searches across the whole global universe.
    limit:
        Maximum rows to return (capped at 150).

    Returns
    -------
    list[dict]
        ``{"code", "name", "exchange", "description", "close"}`` per row, where
        ``code`` is the bare ticker (``BTCUSDT``) suitable for
        ``get_hist(symbol=..., exchange=...)`` — i.e. exactly the format used by
        ``TV_SYMBOL_PRESETS``.

    Never raises: any network/protocol failure yields ``[]`` so callers can fall
    back to the curated preset list.
    """
    log = logger or logging.getLogger(__name__)
    ex = (exchange or "").strip().upper()
    market = TV_SEARCH_MARKET_BY_EXCHANGE.get(ex, "")
    scanner_ex = TV_SEARCH_EXCHANGE_ALIASES.get(ex, ex)
    if not ex:
        market = TV_SEARCH_DEFAULT_MARKET
    limit = max(1, min(int(limit or 50), 150))
    q = (query or "").strip()

    filters: list[dict] = []
    if scanner_ex:
        filters.append({"left": "exchange", "operation": "match", "right": scanner_ex})
    if q:
        filters.append({"left": "name", "operation": "match", "right": q})

    payload: dict = {
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": ["name", "description", "close", "exchange", "volume"],
        "options": {"lang": "en"},
        "range": [0, limit],
    }
    if filters:
        payload["filter"] = filters
    # 全市场浏览时按成交量降序；仅加密市场有意义（股票/外汇的 volume 列
    # 会返回一批毫无意义的冷门代码，反而更难用）。
    if not q and not scanner_ex and market == "crypto":
        payload["sort"] = {"sortBy": "volume", "sortOrder": "desc"}
    # 多取一些再做清洗与排序，否则清洗掉衍生品后结果不足 limit
    payload["range"] = [0, min(300, limit * 4)]
    payload["market"] = market or TV_SEARCH_DEFAULT_MARKET

    try:
        data = _tv_search_request(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("search_tv_symbols failed (market=%s q=%r): %s", market, q, exc)
        return []

    rows: list[dict] = []
    for item in (data.get("data") or []):
        cols = item.get("d") or []
        raw = item.get("s") or ""
        code = raw.split(":", 1)[1] if ":" in raw else raw
        if not code:
            continue
        rows.append({
            "code": code,
            "name": cols[1] if len(cols) > 1 else code,
            "close": cols[2] if len(cols) > 2 else None,
            "exchange": cols[3] if len(cols) > 3 else ex,
            "description": cols[1] if len(cols) > 1 else "",
            "volume": cols[4] if len(cols) > 4 else None,
            "_deriv": _is_derivative(code),
        })

    # 现货优先：scanner 对 "BTC" 会返回 PUMPBTCUSDT / WBTCUSDT / BTCUSD.P，
    # 对 "EURUSD" 会返回 EURUSD.ONE / EURUSD.PRO.OTMS 等券商变种 ——
    # 用户要的是能直接订阅的主品种。
    #
    # 注意顺序：必须**先按相关性排序、再过滤**，反过来会把衍生品重新混进来
    # （分组后又整体重排等于没过滤）。
    if q:
        rows.sort(key=lambda r: _relevance(r["code"], q))
        spot = [r for r in rows if not r["_deriv"]]
        if spot:
            rows = spot
        # 现货一个都没有（如用户搜的是某个合约代码）才退回全部结果
    else:
        # 浏览模式：按成交量给回顺序，只把衍生品压后
        rows.sort(key=lambda r: (1 if r["_deriv"] else 0, -(r.get("volume") or 0)))

    for r in rows:
        r.pop("_deriv", None)
    return rows[:limit]
