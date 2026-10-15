"""Validation Test 4: H2/L2 vs H1/L1 edge in with-trend pullbacks.

Prompt claim (提示词大纲 / 文件19 / 二元决策 §9.4,§9.6):
  "H2/L2 优于 H1/L1 (顺势回撤场景)" and "第二次进场机会几乎永远比第一次更容易产生有利可图的交易".

Method: Only in a with-trend context (bull = close > EMA20 and EMA20 rising).
  In a bull pullback, count H1 = first bar that trades above the prior bar's high
  after a pullback; H2 = second such trigger after a further pullback leg.
  Entry at trigger bar high + 1 tick (stop entry). Stop = signal (prior) bar low - 1 tick.
  Target = 1R (symmetric to risk). Win = target hit before stop within HORIZON bars.
  Compare win rate and expectancy of H1 vs H2 (and L1 vs L2).
"""
from __future__ import annotations

import numpy as np
from common import INSTRUMENTS, load, pct

HORIZON = 20
EMA_SLOPE_LB = 5


def _tick(b):
    diffs = np.abs(np.diff(b.c))
    diffs = diffs[diffs > 0]
    return max(np.percentile(diffs, 1), 1e-6) if len(diffs) else 0.01


def simulate_long(b, trig_idx, tick):
    """Enter long on break of trig bar high; stop below trig bar low; 1R target."""
    n = len(b)
    entry = b.h[trig_idx] + tick
    # must be triggered by a later bar
    start = trig_idx + 1
    fill = None
    for k in range(start, min(start + 3, n)):   # allow up to 3 bars to trigger
        if b.h[k] >= entry:
            fill = k
            break
    if fill is None:
        return None
    stop = b.l[trig_idx] - tick
    risk = entry - stop
    if risk <= 0:
        return None
    target = entry + risk
    for k in range(fill, min(fill + HORIZON, n)):
        hit_t = b.h[k] >= target
        hit_s = b.l[k] <= stop
        if hit_s and hit_t:
            return 0  # ambiguous same-bar -> count as loss (conservative)
        if hit_s:
            return 0
        if hit_t:
            return 1
    return 0  # neither within horizon -> treat as scratch/loss (conservative)


def simulate_short(b, trig_idx, tick):
    n = len(b)
    entry = b.l[trig_idx] - tick
    start = trig_idx + 1
    fill = None
    for k in range(start, min(start + 3, n)):
        if b.l[k] <= entry:
            fill = k
            break
    if fill is None:
        return None
    stop = b.h[trig_idx] + tick
    risk = stop - entry
    if risk <= 0:
        return None
    target = entry - risk
    for k in range(fill, min(fill + HORIZON, n)):
        hit_t = b.l[k] <= target
        hit_s = b.h[k] >= stop
        if hit_s and hit_t:
            return 0
        if hit_s:
            return 0
        if hit_t:
            return 1
    return 0


def run_symbol(name):
    b = load(name)
    n = len(b)
    tick = _tick(b)
    res = {"H1": [0, 0], "H2": [0, 0], "L1": [0, 0], "L2": [0, 0]}  # [wins, total]

    i = 25
    while i < n - 2:
        ema = b.ema20[i]
        if np.isnan(ema) or np.isnan(b.ema20[i - EMA_SLOPE_LB]):
            i += 1
            continue
        rising = b.ema20[i] > b.ema20[i - EMA_SLOPE_LB]
        falling = b.ema20[i] < b.ema20[i - EMA_SLOPE_LB]
        above = b.c[i] > ema
        below = b.c[i] < ema

        # Bull pullback: in uptrend, price has pulled back (>=2 bars with lower highs),
        # then H1 = first bar whose high > prior high. H2 = after another dip + re-break.
        if rising and above:
            # detect a pullback: at least 2 consecutive lower-high bars ending at i
            if b.h[i] < b.h[i - 1] and b.h[i - 1] < b.h[i - 2]:
                # H1 trigger = first subsequent bar breaking a prior-bar high
                h1 = None
                for k in range(i + 1, min(i + 6, n)):
                    if b.h[k] > b.h[k - 1]:
                        h1 = k
                        break
                if h1 is not None:
                    r = simulate_long(b, h1, tick)
                    if r is not None:
                        res["H1"][0] += r
                        res["H1"][1] += 1
                    # H2 = after h1, another pullback (lower high) then re-break
                    pb = None
                    for k in range(h1 + 1, min(h1 + 8, n)):
                        if b.h[k] < b.h[k - 1]:
                            pb = k
                            break
                    if pb is not None:
                        h2 = None
                        for k in range(pb + 1, min(pb + 6, n)):
                            if b.h[k] > b.h[k - 1]:
                                h2 = k
                                break
                        if h2 is not None:
                            r2 = simulate_long(b, h2, tick)
                            if r2 is not None:
                                res["H2"][0] += r2
                                res["H2"][1] += 1
                    i = h1 + 1
                    continue

        if falling and below:
            if b.l[i] > b.l[i - 1] and b.l[i - 1] > b.l[i - 2]:
                l1 = None
                for k in range(i + 1, min(i + 6, n)):
                    if b.l[k] < b.l[k - 1]:
                        l1 = k
                        break
                if l1 is not None:
                    r = simulate_short(b, l1, tick)
                    if r is not None:
                        res["L1"][0] += r
                        res["L1"][1] += 1
                    pb = None
                    for k in range(l1 + 1, min(l1 + 8, n)):
                        if b.l[k] > b.l[k - 1]:
                            pb = k
                            break
                    if pb is not None:
                        l2 = None
                        for k in range(pb + 1, min(pb + 6, n)):
                            if b.l[k] < b.l[k - 1]:
                                l2 = k
                                break
                        if l2 is not None:
                            r2 = simulate_short(b, l2, tick)
                            if r2 is not None:
                                res["L2"][0] += r2
                                res["L2"][1] += 1
                    i = l1 + 1
                    continue
        i += 1
    return name, res


def main():
    print("=" * 90)
    print("TEST 4: H2/L2 vs H1/L1 with-trend pullback win rate (1R target, stop beyond signal bar)")
    print(f"  with-trend filter: EMA20 slope over {EMA_SLOPE_LB} bars + close on trend side; HORIZON={HORIZON}")
    print("=" * 90)
    agg = {"H1": [0, 0], "H2": [0, 0], "L1": [0, 0], "L2": [0, 0]}
    for inst, files in INSTRUMENTS.items():
        print(f"\n### {inst}")
        for f in files:
            try:
                name, res = run_symbol(f)
            except FileNotFoundError:
                continue
            def wr(key):
                w, t = res[key]
                return pct(w, t)
            print(f"  {f:18s}")
            print(f"      H1 {wr('H1')}   | H2 {wr('H2')}")
            print(f"      L1 {wr('L1')}   | L2 {wr('L2')}")
            for k in agg:
                agg[k][0] += res[k][0]
                agg[k][1] += res[k][1]
    print("\n" + "=" * 90)
    print("AGGREGATE (all 3 instruments, all timeframes):")
    for k in ("H1", "H2", "L1", "L2"):
        print(f"  {k}: {pct(agg[k][0], agg[k][1])}")
    first_w = agg["H1"][0] + agg["L1"][0]; first_t = agg["H1"][1] + agg["L1"][1]
    sec_w = agg["H2"][0] + agg["L2"][0]; sec_t = agg["H2"][1] + agg["L2"][1]
    print(f"  First entry (H1+L1):  {pct(first_w, first_t)}")
    print(f"  Second entry (H2+L2): {pct(sec_w, sec_t)}")
    print("=" * 90)


if __name__ == "__main__":
    main()
