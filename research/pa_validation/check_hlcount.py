"""Check what the PROGRAM's hl_count feature outputs on real 100-bar windows.

History: the original _compute_hl_count incremented bull_count on EVERY bar whose high exceeded
the previous bar's high anywhere in the 40-bar window (reset only by a >=1.2 ATR opposite bar).
Measured on 900 real windows that gave h3 in 68% (bull) / 65% (bear) of cases, i.e. it was not
Brooks' H1/H2.  It was replaced by a per-pullback-leg count (see market_features._count_pullback_triggers).
Re-run this script to see the current distribution (after the fix: h1 ~21%, h2 ~14%, h3 ~23%, none ~42%).
"""
from __future__ import annotations

import os
import sys
from collections import Counter

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame  # noqa: E402
from pa_agent.ai.market_features import compute_simple_market_features  # noqa: E402

from common2 import load  # noqa: E402

FILES = ["XAUUSD_M15", "XAUUSD_H1", "US100.cash_M15", "US100.cash_H1", "US500.cash_M15", "US500.cash_H1"]
SAMPLES = 150
WIN = 100


def frame_at(b, end):
    """Window ending at index `end` (inclusive) as a newest-first KlineFrame."""
    idx = list(range(end, end - WIN, -1))
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(1_700_000_000_000 + (end - j) * 60_000), open=float(b.o[j]),
                 high=float(b.h[j]), low=float(b.l[j]), close=float(b.c[j]), volume=0.0)
        for i, j in enumerate(idx)
    )
    ind = IndicatorBundle(ema20=tuple(float(b.ema20[j]) for j in idx), atr14=tuple(float(b.atr14[j]) for j in idx))
    return KlineFrame(symbol="X", timeframe="x", bars=bars, indicators=ind, snapshot_ts_local_ms=0)


def main():
    rng = np.random.default_rng(5)
    bull_c, bear_c = Counter(), Counter()
    bull_n, bear_n = [], []
    for f in FILES:
        b = load(f)
        ends = rng.integers(200, len(b) - 1, SAMPLES)
        for e in ends:
            fr = frame_at(b, int(e))
            hc = compute_simple_market_features(fr).hl_count
            bull_c[hc.bull_candidate] += 1
            bear_c[hc.bear_candidate] += 1
            bull_n.append(hc.bull_count)
            bear_n.append(hc.bear_count)
    tot = sum(bull_c.values())
    print("samples:", tot)
    print("bull_candidate:", {k: f"{100*v/tot:.1f}%" for k, v in sorted(bull_c.items())})
    print("bear_candidate:", {k: f"{100*v/tot:.1f}%" for k, v in sorted(bear_c.items())})
    print("bull_count  median/mean/max:", np.median(bull_n), round(float(np.mean(bull_n)), 1), max(bull_n))
    print("bear_count  median/mean/max:", np.median(bear_n), round(float(np.mean(bear_n)), 1), max(bear_n))


if __name__ == "__main__":
    main()
