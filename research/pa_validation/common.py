"""Shared data loading + feature computation for PA prompt validation.

Mirrors the EXACT definitions used by the production agent code:
  - pa_agent/indicators/ema.py   (EMA20, SMA-seeded, alpha=2/21)
  - pa_agent/indicators/atr.py   (ATR14, Wilder smoothing)
  - pa_agent/ai/kline_features.py (bar classification)
  - pa_agent/ai/market_features.py (spike aftermath, breakout quality, H/L count)

Data is stored OLDEST-FIRST in parquet (time ascending). The production code
works NEWEST-FIRST (seq ascending = newest). For validation we work in natural
chronological (oldest-first) arrays and translate definitions accordingly.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

DATA_DIR = r"d:\MT5_K线数据"

# The three instruments the user asked to validate on.
INSTRUMENTS = {
    "XAUUSD": ["XAUUSD_M5", "XAUUSD_M15", "XAUUSD_H1", "XAUUSD_H4", "XAUUSD_D1"],
    "US100":  ["US100.cash_M5", "US100.cash_M15", "US100.cash_H1", "US100.cash_H4", "US100.cash_D1"],
    "US500":  ["US500.cash_M5", "US500.cash_M15", "US500.cash_H1", "US500.cash_D1"],
}


@dataclass
class Bars:
    """Chronological OHLC arrays (oldest first) + indicators."""
    name: str
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    ema20: np.ndarray
    atr14: np.ndarray

    def __len__(self) -> int:
        return len(self.c)


def ema_full(values: np.ndarray, period: int = 20) -> np.ndarray:
    """SMA-seeded EMA, matching pa_agent/indicators/ema.py exactly."""
    n = len(values)
    out = np.full(n, np.nan)
    if n < period:
        return out
    alpha = 2.0 / (period + 1)
    seed = values[:period].mean()
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = values[i] * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return out


def atr_full(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder ATR, matching pa_agent/indicators/atr.py exactly."""
    n = len(h)
    out = np.full(n, np.nan)
    if n < period:
        return out
    tr = np.empty(n)
    for i in range(n):
        hl = abs(h[i] - l[i])
        if i == 0:
            tr[i] = hl
        else:
            tr[i] = max(hl, abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    seed = tr[:period].mean()
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def load(symbol_file: str) -> Bars:
    path = os.path.join(DATA_DIR, f"{symbol_file}.parquet")
    df = pd.read_parquet(path)
    df = df.sort_values("time").reset_index(drop=True)
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    return Bars(
        name=symbol_file,
        o=o, h=h, l=l, c=c,
        ema20=ema_full(c, 20),
        atr14=atr_full(h, l, c, 14),
    )


# ── Bar classification (mirrors kline_features._classify_bar) ──────────────────

def bar_type(o, h, l, c, prev_h=None, prev_l=None) -> str:
    full = h - l
    if prev_h is not None and prev_l is not None:
        if h <= prev_h and l >= prev_l:
            return "inside"
        if h >= prev_h and l <= prev_l:
            return "outside_bull" if c >= o else "outside_bear"
    if full <= 0:
        return "flat"
    body_ratio = abs(c - o) / full
    close_pos = max(0.0, min(1.0, (c - l) / full))
    if body_ratio <= 0.25:
        return "doji"
    if c > o and close_pos >= 0.65:
        return "trend_bull"
    if c < o and close_pos <= 0.35:
        return "trend_bear"
    return "other"


def is_trend_bull(o, h, l, c) -> bool:
    full = h - l
    if full <= 0:
        return False
    return (abs(c - o) / full) > 0.25 and c > o and (c - l) / full >= 0.65


def is_trend_bear(o, h, l, c) -> bool:
    full = h - l
    if full <= 0:
        return False
    return (abs(c - o) / full) > 0.25 and c < o and (c - l) / full <= 0.35


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson confidence interval for a proportion."""
    if n == 0:
        return (0.0, 0.0)
    z = 1.96
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def pct(k: int, n: int) -> str:
    if n == 0:
        return "n/a (0 samples)"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100*k/n:.1f}% [95%CI {100*lo:.1f}-{100*hi:.1f}%]"
