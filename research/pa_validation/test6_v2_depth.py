"""Test 6 v2: does pullback depth carry information beyond pure geometry?

Concern: continuation = "new leg extreme before closing below leg origin".  Distance to the target
is `depth*leg`, distance to the stop is `(1-depth)*leg`.  Even a driftless random walk has
P(success) ~ (1 - depth) (gambler's ruin) -> a monotonic table is partly MECHANICAL.

So report, per depth bucket: REAL, NULL (bar-shuffled surrogate, same detector) and the mean
analytic gambler's-ruin value (1 - depth).  Information = REAL minus NULL.
"""
from __future__ import annotations

import sys

import numpy as np

from common import pct
from common2 import Acc, datasets, invert, load, surrogate

CONT_H = 30
MIN_LEG_ATR = 2.0


def swings(b):
    n = len(b)
    piv = []
    for i in range(1, n - 1):
        if b.h[i] > b.h[i - 1] and b.h[i] > b.h[i + 1]:
            piv.append((i, "H", b.h[i]))
        if b.l[i] < b.l[i - 1] and b.l[i] < b.l[i + 1]:
            piv.append((i, "L", b.l[i]))
    return piv


def bucket(d):
    return "tight<30" if d < 0.30 else "normal30-50" if d < 0.50 else "broad50-78.6" if d < 0.786 else "deep>78.6"


def scan(b, acc, rw, tag):
    n = len(b)
    piv = swings(b)
    for idx in range(len(piv) - 2):
        a, c, p = piv[idx], piv[idx + 1], piv[idx + 2]
        if not (a[1] == "L" and c[1] == "H" and p[1] == "L"):
            continue
        lo_p, hi_p, pb_low = a[2], c[2], p[2]
        atr = b.atr14[c[0]]
        if np.isnan(atr) or atr <= 0:
            continue
        leg = hi_p - lo_p
        if leg < MIN_LEG_ATR * atr:
            continue
        depth = (hi_p - pb_low) / leg
        if not (0 < depth <= 1.2):
            continue
        cont = False
        for k in range(p[0] + 1, min(p[0] + 1 + CONT_H, n)):
            if b.h[k] > hi_p:
                cont = True
                break
            if b.c[k] < lo_p:
                break
        key = bucket(depth)
        acc.add(f"{tag}|{key}", cont)
        rw.setdefault(f"{tag}|{key}", []).append(max(0.0, 1 - depth))


def main():
    real, sur = Acc(), Acc()
    rw = {}
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        s = surrogate(b, 41)
        for series, acc, nm in ((b, real, "R"), (s, sur, "S")):
            scan(series, acc, rw, nm)
            scan(invert(series), acc, rw, nm)
        print("done", f, file=sys.stderr)

    print("=" * 110)
    print("TEST 6 v2  pullback depth -> continuation, REAL vs NULL (bar-shuffled) vs analytic random walk (1-depth)")
    print("=" * 110)
    for k in ("tight<30", "normal30-50", "broad50-78.6", "deep>78.6"):
        kr, nr = real.get(f"R|{k}")
        ks, ns = sur.get(f"S|{k}")
        rwv = np.mean(rw.get(f"R|{k}", [np.nan]))
        print(f"  {k:13s} REAL {pct(kr, nr):48s} | NULL {pct(ks, ns):48s} | RW(1-depth)~{100*rwv:.1f}%")
    print("\nREAD: REAL - NULL is the information content of the depth bucket beyond geometry.")


if __name__ == "__main__":
    main()
