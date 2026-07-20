"""TradingView data source using tvdatafeed."""
from __future__ import annotations

import logging
import threading
import time

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
    "GATEIO": [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT",
        "LTCUSDT", "TRXUSDT", "DOTUSDT", "BCHUSDT", "ATOMUSDT",
    ],
    "BINANCE": [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    ],
    "BYBIT": [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT",
    ],
    "OKX": [
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
        "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "TON-USDT",
    ],
    "BITSTAMP": ["BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD", "BCHUSD"],
    "COINBASE": ["BTCUSD", "ETHUSD", "SOLUSD", "ADAUSD", "XRPUSD"],
    "OANDA": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    "PEPPERSTONE": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    "FOREXCOM": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    "TVC": ["XAUUSD", "GOLD", "EURUSD", "GBPUSD", "USDJPY"],
    "CAPITALCOM": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"],
    "SSE": ["600519", "601318", "600036", "601398", "600276"],
    "SZSE": ["000001", "000333", "000651", "002594", "300750"],
    "HKEX": ["0700", "1810", "9988", "3690", "1299"],
    "SP": ["SPX", "SPX500", "NDX", "VIX"],
    "NYSE": ["AAPL", "MSFT", "AMZN", "TSLA", "JPM"],
    "NASDAQ": ["AAPL", "MSFT", "GOOGL", "META", "NVDA"],
    "CBOT": ["ZC", "ZS", "ZW", "ZL"],
    "CME_MINI": ["ES", "NQ", "YM", "RTY"],
    "": ["XAUUSD", "BTCUSDT", "ETHUSDT", "EURUSD", "GOLD"],
}

# Symbol name mappings for display purposes
TV_SYMBOL_NAMES: dict[str, str] = {
    # Crypto
    "BTCUSDT": "比特币",
    "ETHUSDT": "以太坊",
    "SOLUSDT": "Solana",
    "BNBUSDT": "币安币",
    "XRPUSDT": "瑞波币",
    "DOGEUSDT": "狗狗币",
    "ADAUSDT": "卡尔达诺",
    "AVAXUSDT": "Avalanche",
    "LINKUSDT": "Chainlink",
    "TONUSDT": "Toncoin",
    "LTCUSDT": "莱特币",
    "TRXUSDT": "波场",
    "DOTUSDT": "波卡",
    "BCHUSDT": "比特现金",
    "ATOMUSDT": "Cosmos",
    "BTC-USDT": "比特币",
    "ETH-USDT": "以太坊",
    "SOL-USDT": "Solana",
    "BNB-USDT": "币安币",
    "XRP-USDT": "瑞波币",
    "DOGE-USDT": "狗狗币",
    "ADA-USDT": "卡尔达诺",
    "AVAX-USDT": "Avalanche",
    "LINK-USDT": "Chainlink",
    "TON-USDT": "Toncoin",
    "BTCUSD": "比特币",
    "ETHUSD": "以太坊",
    "LTCUSD": "莱特币",
    "XRPUSD": "瑞波币",
    "BCHUSD": "比特现金",
    "SOLUSD": "Solana",
    # Forex
    "XAUUSD": "现货黄金",
    "EURUSD": "欧元/美元",
    "GBPUSD": "英镑/美元",
    "USDJPY": "美元/日元",
    "AUDUSD": "澳元/美元",
    "USDCAD": "美元/加元",
    "USDCHF": "美元/瑞郎",
    "GOLD": "黄金",
    # A-shares
    "600519": "贵州茅台",
    "601318": "中国平安",
    "600036": "招商银行",
    "601398": "工商银行",
    "600276": "恒瑞医药",
    "000001": "平安银行",
    "000333": "美的集团",
    "000651": "格力电器",
    "002594": "比亚迪",
    "300750": "宁德时代",
    # HK stocks
    "0700": "腾讯控股",
    "1810": "小米集团",
    "9988": "阿里巴巴",
    "3690": "美团",
    "1299": "友邦保险",
    # US stocks
    "AAPL": "苹果",
    "MSFT": "微软",
    "AMZN": "亚马逊",
    "TSLA": "特斯拉",
    "JPM": "摩根大通",
    "GOOGL": "谷歌",
    "META": "Meta",
    "NVDA": "英伟达",
    # Indices
    "SPX": "标普500",
    "SPX500": "标普500",
    "NDX": "纳斯达克100",
    "VIX": "波动率指数",
    # Futures
    "ZC": "玉米期货",
    "ZS": "大豆期货",
    "ZW": "小麦期货",
    "ZL": "豆油期货",
    "ES": "标普500期货",
    "NQ": "纳斯达克期货",
    "YM": "道琼斯期货",
    "RTY": "罗素2000期货",
}


class TradingViewSource(DataSource):
    """Live K-line data from TradingView via tvdatafeed."""

    def __init__(self, username: str = "", password: str = "") -> None:
        self._username = username
        self._password = password
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
            if self._username and self._password:
                self._tv = TvDatafeed(self._username, self._password)
            else:
                self._tv = TvDatafeed()  # anonymous
            try:
                setattr(self._tv, _TV_WS_TIMEOUT_ATTR, _TV_WS_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                logger.debug("Could not override tvDatafeed ws timeout", exc_info=True)
            self._connected = True
            logger.info("TradingViewSource connected (anonymous=%s)", not self._username)
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
