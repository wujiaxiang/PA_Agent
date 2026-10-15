"""reg9: do swing highs/lows cluster at round prices?

Pivot = fractal (2 bars each side).  For a step S, offset = (price mod S)/S in [0,1); the nearest round
number is at offset 0 / 1.  Window = +-4% of S around a multiple (expected share 8% if uniform).
Highs are split into 'just below' [-4%,0) and 'just above' [0,+4%] a round number; lows likewise.
"""
from __future__ import annotations

import numpy as np

from common import pct
from common2 import datasets, load
from common3 import inst_of, tf_of

STEPS = {"XAUUSD": [100, 50, 10], "US100": [500, 100, 50], "US500": [100, 50, 25]}
W = 0.04


def pivots(b):
    h, l = b.h, b.l
    hi = (h[2:-2] > h[1:-3]) & (h[2:-2] > h[:-4]) & (h[2:-2] > h[3:-1]) & (h[2:-2] > h[4:])
    lo = (l[2:-2] < l[1:-3]) & (l[2:-2] < l[:-4]) & (l[2:-2] < l[3:-1]) & (l[2:-2] < l[4:])
    return h[2:-2][hi], l[2:-2][lo]


def main():
    agg = {}
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        ph, pl = pivots(b)
        tf = tf_of(f)
        for S in STEPS[inst_of(f)]:
            for kind, arr in (("high", ph), ("low", pl)):
                off = (arr % S) / S
                near = (off < W) | (off > 1 - W)
                below = off > 1 - W          # just below a round number
                above = off < W              # just above
                k = (inst_of(f), S, kind)
                a = agg.setdefault(k, {"n": 0, "near": 0, "below": 0, "above": 0})
                a["n"] += len(arr)
                a["near"] += int(near.sum())
                a["below"] += int(below.sum())
                a["above"] += int(above.sum())
        print("done", f, file=__import__("sys").stderr)

    print("=" * 100)
    print(f"reg9  swing pivots near round prices (window +-{int(W*100)}% of step; uniform expectation 8.0% total, 4.0% each side)")
    print("=" * 100)
    for inst in STEPS:
        for S in STEPS[inst]:
            for kind in ("high", "low"):
                a = agg[(inst, S, kind)]
                n = a["n"]
                z = (a["near"] - 0.08 * n) / np.sqrt(n * 0.08 * 0.92)
                zb = (a["below"] - 0.04 * n) / np.sqrt(n * 0.04 * 0.96)
                za = (a["above"] - 0.04 * n) / np.sqrt(n * 0.04 * 0.96)
                print(f"  {inst:7s} step {S:4d} {kind:4s} n={n:7d}  near {100*a['near']/n:5.2f}% (z={z:+5.1f})"
                      f" | just below {100*a['below']/n:5.2f}% (z={zb:+5.1f}) | just above {100*a['above']/n:5.2f}% (z={za:+5.1f})")


if __name__ == "__main__":
    main()
