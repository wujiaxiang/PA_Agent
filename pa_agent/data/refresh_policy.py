"""Refresh interval, cache TTL, and zombie-loop timing for data sources."""
from __future__ import annotations

HTTP_POLL_SOURCES: frozenset[str] = frozenset({"eastmoney", "akshare"})

HTTP_MIN_REFRESH_MS = 2500
HTTP_SNAPSHOT_CACHE_TTL_S = 8.0
HTTP_SNAPSHOT_CACHE_TTL_1D_S = 12.0
HTTP_ZOMBIE_JOIN_MS = 15_000
DEFAULT_ZOMBIE_JOIN_MS = 5000


def is_http_poll_source(kind: str) -> bool:
    return kind in HTTP_POLL_SOURCES


def effective_refresh_interval_ms(
    kind: str,
    user_ms: int,
    *,
    timeframe: str = "",
) -> int:
    """Clamp user refresh interval for slow HTTP/Baostock sources."""
    ms = max(500, int(user_ms or 1000))
    if kind in HTTP_POLL_SOURCES:
        ms = max(ms, HTTP_MIN_REFRESH_MS)
    if kind in HTTP_POLL_SOURCES and timeframe == "1d":
        ms = max(ms, 3000)
    return ms


def snapshot_cache_ttl_s(timeframe: str) -> float:
    if timeframe in ("1d", "1w", "1M"):
        return HTTP_SNAPSHOT_CACHE_TTL_1D_S
    if timeframe == "1m":
        return 4.0
    return HTTP_SNAPSHOT_CACHE_TTL_S


def zombie_join_timeout_ms(kind: str) -> int:
    if kind in HTTP_POLL_SOURCES:
        return HTTP_ZOMBIE_JOIN_MS
    return DEFAULT_ZOMBIE_JOIN_MS


def tv_cache_ttl_seconds(timeframe: str) -> int:
    """TradingView 源 latest_snapshot 的 TTL 缓存时长（秒），按周期分级。

    1m=4s，5m/15m/30m=8s，1h/4h=15s，1d/1w=60s，其他=8s。
    """
    tf = (timeframe or "").lower()
    if tf == "1m":
        return 4
    if tf in ("5m", "15m", "30m"):
        return 8
    if tf in ("1h", "4h"):
        return 15
    if tf in ("1d", "1w"):
        return 60
    return 8
