"""Validation Test 5: Trend bar follow-through reliability.

Prompt claim (市场诊断框架 第三层 / 文件16 / 逐棒检查单):
  "趋势K线后必须有跟随 (下一根K线继续同方向)，没有跟随的趋势K线可能失败"
  Also: "连续2-3根趋势棒更强；连续4根以上=极强趋势".

Also tests signal-bar quality dependence: does a STRONG trend signal bar
(body_ratio high, close near extreme) have better follow-through than a weak one?

Follow-through definition (matches kline_features._follow_through_1_2 spirit):
  For a bull trend bar at i, "yes" if next 1-2 bars close above the trend bar's close.
Here we measure: P(next bar closes beyond trend-bar close | trend bar), split by
trend-bar strength, and the continuation of a RUN (k consecutive trend bars ->
P next bar same direction).
"""
from __future__ import annotations

import numpy as np
from common import INSTRUMENTS, load, pct, is_trend_bull, is_trend_bear

HORIZON = 2


def run_symbol(name):
    b = load(name)
    n = len(b)
    # follow-through by strength bucket
    ft = {"strong": [0, 0], "weak": [0, 0]}  # [follow, total]
    # run continuation: after k consecutive same-dir trend bars, next bar same dir close?
    run_cont = {1: [0, 0], 2: [0, 0], 3: [0, 0], 4: [0, 0]}

    run_len = 0
    run_dir = 0
    for i in range(1, n - HORIZON):
        atr = b.atr14[i]
        if np.isnan(atr) or atr <= 0:
            continue
        bull = is_trend_bull(b.o[i], b.h[i], b.l[i], b.c[i])
        bear = is_trend_bear(b.o[i], b.h[i], b.l[i], b.c[i])
        if not (bull or bear):
            run_len = 0
            run_dir = 0
            continue
        d = 1 if bull else -1
        full = b.h[i] - b.l[i]
        body = abs(b.c[i] - b.o[i])
        strong = body >= 1.0 * atr and (body / full) >= 0.6
        # follow-through: any of next HORIZON bars close beyond this bar's close in dir d
        follow = False
        for k in range(i + 1, i + 1 + HORIZON):
            if d > 0 and b.c[k] > b.c[i]:
                follow = True; break
            if d < 0 and b.c[k] < b.c[i]:
                follow = True; break
        key = "strong" if strong else "weak"
        ft[key][0] += int(follow)
        ft[key][1] += 1

        # run continuation
        if d == run_dir:
            run_len += 1
        else:
            run_dir = d
            run_len = 1
        bucket = min(run_len, 4)
        nxt = b.c[i + 1]
        same = (nxt > b.c[i]) if d > 0 else (nxt < b.c[i])
        run_cont[bucket][0] += int(same)
        run_cont[bucket][1] += 1
    return name, ft, run_cont


def main():
    print("=" * 90)
    print("TEST 5: Trend-bar follow-through reliability (next 1-2 bars close beyond)")
    print("=" * 90)
    agg_ft = {"strong": [0, 0], "weak": [0, 0]}
    agg_run = {1: [0, 0], 2: [0, 0], 3: [0, 0], 4: [0, 0]}
    for inst, files in INSTRUMENTS.items():
        print(f"\n### {inst}")
        for f in files:
            try:
                name, ft, run_cont = run_symbol(f)
            except FileNotFoundError:
                continue
            print(f"  {f:18s} strong FT {pct(*ft['strong'])} | weak FT {pct(*ft['weak'])}")
            for k in agg_ft:
                agg_ft[k][0] += ft[k][0]; agg_ft[k][1] += ft[k][1]
            for k in agg_run:
                agg_run[k][0] += run_cont[k][0]; agg_run[k][1] += run_cont[k][1]
    print("\n" + "=" * 90)
    print("AGGREGATE:")
    print(f"  Strong trend bar follow-through: {pct(*agg_ft['strong'])}")
    print(f"  Weak   trend bar follow-through: {pct(*agg_ft['weak'])}")
    print("  Run continuation P(next bar closes same-dir | run length):")
    for k in (1, 2, 3, 4):
        label = f"{k}+" if k == 4 else str(k)
        print(f"    after {label} consecutive trend bars: {pct(*agg_run[k])}")
    print("=" * 90)


if __name__ == "__main__":
    main()
