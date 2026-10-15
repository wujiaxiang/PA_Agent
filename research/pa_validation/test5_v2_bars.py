"""Test 5 v2: trend-bar follow-through and runs of trend bars, against CORRECT baselines.

v1 mistakes:
  * "follow-through" = any of the next 2 closes beyond the trend bar's close was 60-61% and was
    called an edge over 50%.  For a symmetric random walk P(c1>c0 or c2>c0) = 1 - 3/8 = 62.5%, so
    60-61% is BELOW the random baseline.  The right comparison is against all bars / non-trend bars.
  * "next bar closes same direction" is a statement about 1-bar autocorrelation, not about
    "strong trend" (which is about multi-bar follow-through and reaching targets).

v2:
  For each bar t classified (project definition) as trend_bull / trend_bear, measure in the signed
  direction d (+1 bull, -1 bear), using ATR at t:
     F1  P(next close beyond c_t)
     F2  P(any of next 2 closes beyond c_t)                (the old statistic)
     R3, R5, R10  mean signed forward return  d*(c_{t+N}-c_t)/ATR     (excess over the all-bars mean)
     X5  P(price extends >= 1 ATR beyond c_t before retracing >= 1 ATR) within 20 bars
  Baselines: ALL bars (same statistic with d = +1 and d = -1 averaged, i.e. no conditioning),
             NON-TREND bars (doji/other), and the analytic random-walk value for F2.
  Run length k = number of consecutive same-direction trend bars ending at t (1..6+).
  Direction-signed excess is computed relative to the unconditional signed mean of the same dir.
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from common import is_trend_bear, is_trend_bull, pct
from common2 import datasets, load, mean_ci

N_LIST = (3, 5, 10)
RACE_H = 20


def x_race(b, t, d, atr):
    c0 = b.c[t]
    for k in range(t + 1, min(t + 1 + RACE_H, len(b))):
        if d > 0:
            if b.l[k] <= c0 - atr:
                return 0
            if b.h[k] >= c0 + atr:
                return 1
        else:
            if b.h[k] >= c0 + atr:
                return 0
            if b.l[k] <= c0 - atr:
                return 1
    return None


def scan(b, store):
    n = len(b)
    run_len, run_dir = 0, 0
    for t in range(30, n - 12):
        atr = b.atr14[t]
        if np.isnan(atr) or atr <= 0:
            continue
        o, h, l, c = b.o[t], b.h[t], b.l[t], b.c[t]
        bull = is_trend_bull(o, h, l, c)
        bear = is_trend_bear(o, h, l, c)
        if bull:
            cls = "bull"
        elif bear:
            cls = "bear"
        else:
            cls = "non"
        if cls == "non":
            run_len, run_dir = 0, 0
        else:
            d = 1 if cls == "bull" else -1
            run_len = run_len + 1 if d == run_dir else 1
            run_dir = d

        # statistics for each of up (+1) and down (-1) direction evaluated on this bar
        for d in (1, -1):
            f1 = int((b.c[t + 1] - c) * d > 0)
            f2 = int(((b.c[t + 1] - c) * d > 0) or ((b.c[t + 2] - c) * d > 0))
            rets = {N: d * (b.c[t + N] - c) / atr for N in N_LIST}
            xr = x_race(b, t, d, atr)
            groups = ["ALL"]
            if cls == "non":
                groups.append("NONTREND")
            elif (d == 1 and cls == "bull") or (d == -1 and cls == "bear"):
                groups.append("TREND_WITH")        # bar in the direction being measured
                groups.append(f"RUN{min(run_len, 6)}")
                body = abs(c - o)
                full = h - l
                strong = body >= atr and body / full >= 0.6
                groups.append("STRONG" if strong else "WEAK")
            else:
                groups.append("TREND_AGAINST")     # trend bar opposite to measured direction
            for g in groups:
                store[(g, "F1")].append(f1)
                store[(g, "F2")].append(f2)
                for N in N_LIST:
                    store[(g, f"R{N}")].append(rets[N])
                if xr is not None:
                    store[(g, "X5")].append(xr)


def main():
    store = defaultdict(list)
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        scan(b, store)
        print("done", f, file=sys.stderr)

    def prob(g, m):
        x = np.array(store[(g, m)])
        return pct(int(x.sum()), len(x)) if len(x) else "n/a"

    def mean_(g, m):
        x = np.array(store[(g, m)])
        if len(x) == 0:
            return "n/a"
        mu, ci = mean_ci(x)
        return f"{mu:+.4f} ±{ci:.4f} ATR (n={len(x)})"

    print("=" * 110)
    print("TEST 5 v2  trend-bar follow-through vs correct baselines")
    print("Random-walk analytic value for F2 (any of next 2 closes beyond):  62.5%   for F1: 50%")
    print("=" * 110)
    for g in ("ALL", "NONTREND", "TREND_WITH", "STRONG", "WEAK", "TREND_AGAINST"):
        print(f"\n[{g}]")
        print(f"  F1 next close beyond           : {prob(g, 'F1')}")
        print(f"  F2 any of next 2 closes beyond : {prob(g, 'F2')}")
        print(f"  X5 +1ATR before -1ATR (20 bars): {prob(g, 'X5')}")
        for N in N_LIST:
            print(f"  signed fwd return {N:2d} bars    : {mean_(g, f'R{N}')}")

    print("\n" + "-" * 110)
    print("RUN LENGTH (consecutive same-direction trend bars ending at t) -- signed forward return, ATR units")
    base = {N: np.mean(store[("ALL", f"R{N}")]) for N in N_LIST}
    print(f"  ALL-bars baseline mean: " + ", ".join(f"R{N}={base[N]:+.4f}" for N in N_LIST))
    for k in range(1, 7):
        g = f"RUN{k}"
        row = [f"k={'6+' if k == 6 else k}"]
        row.append(f"F1 {prob(g, 'F1')}")
        for N in N_LIST:
            x = np.array(store[(g, f"R{N}")])
            if len(x):
                mu, ci = mean_ci(x)
                row.append(f"R{N} {mu:+.3f}±{ci:.3f}")
        row.append(f"X5 {prob(g, 'X5')}")
        print("  " + " | ".join(row))
    print("\nREAD: a statistic only means 'edge' if it differs from ALL / NONTREND, not from 50%.")


if __name__ == "__main__":
    main()
