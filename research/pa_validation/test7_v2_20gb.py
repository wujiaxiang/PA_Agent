"""Test 7 v2: first touch of EMA20 after a run of g gap bars ("20GB" when g >= 20).

v1 mistakes:
  * the scalp simulation assumed the entry was filled; if price simply fell away without ever
    trading through the entry level it was still booked as a "reversal".  28% vs 69% was therefore
    invalid.
  * no control: the claim is that 20+ bars are SPECIAL, so shorter gap runs (3-5, 6-9, ...) must
    be compared on the same statistic, and everything compared with the bar-shuffled null.

v2 (bull; bear mirrored by invert):
  gap bar     : low > EMA20.   Event bar T = first bar after >= 3 gap bars with low <= EMA20.
  g           : number of consecutive gap bars before T.  Buckets 3-5, 6-9, 10-14, 15-19, 20-29, 30+.
  (a) Brooks "test of the extreme": race from T+1, UP = run high (max high of the g gap bars)
        vs DOWN = EMA20(T) - d*ATR, d in {0.5, 1.0}; 40 bar horizon.
  (b) stop-entry scalp WITH FILL: buy stop = high[T] + 1 tick, live for bars T+1..T+3, cancelled if a
        bar trades below low[T] - 1 tick first.  Stop = low[T] - 1 tick.  Fill bar is conservative
        (low <= stop -> loss).  Target 1R / 2R, 30 bars, mark-to-market if unresolved.
        Reports fill rate and mean R.
  (c) drift: signed return (c[T+10]-c[T])/ATR and P(c[T+10] > EMA20[T+10]).
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from common import pct
from common2 import datasets, invert, load, mean_ci, surrogate, tick_of

BUCKETS = [("3-5", 3, 5), ("6-9", 6, 9), ("10-14", 10, 14), ("15-19", 15, 19), ("20-29", 20, 29), ("30+", 30, 10**9)]
HZ = 30


def bname(g):
    for name, lo, hi in BUCKETS:
        if lo <= g <= hi:
            return name
    return None


def race(b, start, up, dn, horizon=40):
    for k in range(start, min(start + horizon, len(b))):
        if b.l[k] <= dn:
            return 0
        if b.h[k] >= up:
            return 1
    return None


def sim_trade(b, k, entry, stop, risk, T):
    n = len(b)
    if b.l[k] <= stop:
        return -1.0
    tgt = entry + T * risk
    end = min(k + HZ, n - 1)
    for j in range(k + 1, end + 1):
        if b.l[j] <= stop:
            return -1.0
        if b.h[j] >= tgt:
            return float(T)
    return float((b.c[end] - entry) / risk)


def scan(b, tk, store, src):
    n = len(b)
    g = 0
    for t in range(25, n - HZ - 12):
        e = b.ema20[t]
        if np.isnan(e):
            g = 0
            continue
        if b.l[t] > e:
            g += 1
            continue
        if g >= 3:
            name = bname(g)
            atr = b.atr14[t]
            if name and not np.isnan(atr) and atr > 0:
                run_high = b.h[t - g:t].max()
                for d in (0.5, 1.0):
                    r = race(b, t + 1, run_high, e - d * atr)
                    if r is not None:
                        for key in (name, "ALL"):
                            store[(src, key, f"A_extreme_d{d}")].append(r)
                # (b) stop-entry scalp with fill
                entry = b.h[t] + tk
                stop = b.l[t] - tk
                risk = entry - stop
                if 0.2 * atr <= risk <= 3 * atr:
                    fill = None
                    for k in range(t + 1, t + 4):
                        hit_e = b.h[k] >= entry
                        hit_s = b.l[k] <= stop
                        if hit_e:
                            fill = k          # fill first (conservative: stop checked inside sim)
                            break
                        if hit_s:
                            break
                    for key in (name, "ALL"):
                        store[(src, key, "B_filled")].append(1 if fill is not None else 0)
                    if fill is not None:
                        for T in (1, 2):
                            r = sim_trade(b, fill, entry, stop, risk, T)
                            for key in (name, "ALL"):
                                store[(src, key, f"B_R{T}")].append(r)
                # (c) drift
                ret10 = (b.c[t + 10] - b.c[t]) / atr
                above = int(b.c[t + 10] > b.ema20[t + 10]) if not np.isnan(b.ema20[t + 10]) else None
                for key in (name, "ALL"):
                    store[(src, key, "C_ret10")].append(ret10)
                    if above is not None:
                        store[(src, key, "C_above")].append(above)
        g = 0


def main():
    store = defaultdict(list)
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        tk = tick_of(b)
        s = surrogate(b, 31)
        for series, src in ((b, "REAL"), (s, "NULL")):
            scan(series, tk, store, src)
            scan(invert(series), tk, store, src)
        print("done", f, file=sys.stderr)

    def p(src, key, m):
        x = np.array(store[(src, key, m)])
        return pct(int(x.sum()), len(x)) if len(x) else "n/a"

    def mr(src, key, m):
        x = np.array(store[(src, key, m)])
        if not len(x):
            return "n/a"
        mu, ci = mean_ci(x)
        return f"{mu:+.3f}±{ci:.3f} (n={len(x)})"

    print("=" * 112)
    print("TEST 7 v2  first EMA20 touch after g gap bars   (20GB = g>=20 ; shorter g are the controls)")
    print("=" * 112)
    keys = [b[0] for b in BUCKETS] + ["ALL"]
    for title, metric, fn in (
        ("(a) Brooks 'test of the extreme': P(new run high before EMA-0.5ATR)", "A_extreme_d0.5", p),
        ("(a') same, P(new run high before EMA-1.0ATR)", "A_extreme_d1.0", p),
        ("(b) buy-stop fill rate (high[T]+tick within 3 bars)", "B_filled", p),
        ("(b) filled scalp mean R, target 1R", "B_R1", mr),
        ("(b) filled scalp mean R, target 2R", "B_R2", mr),
        ("(c) P(close 10 bars later above EMA20)", "C_above", p),
        ("(c) mean 10-bar return in ATR", "C_ret10", mr),
    ):
        print(f"\n{title}")
        for k in keys:
            print(f"   g={k:6s} REAL {fn('REAL', k, metric):56s} | NULL {fn('NULL', k, metric)}")
    print("\nREAD: if g>=20 is not different from g=3-9 on the same statistic, '20GB' is not special.")


if __name__ == "__main__":
    main()
