"""Descriptive sanity numbers for the gap detector: counts, symmetry, size distribution.

Deliberately NO outcome statistics (test / fill rates) here: those belong to the pre-registered step.
"""
from __future__ import annotations

import numpy as np

from common3 import surrogate_vol
from common2 import make_bars
from gap_defs import detect, load_file, tf_seconds

FILES = [
    "XAUUSD_M5", "XAUUSD_M15", "XAUUSD_H1", "XAUUSD_H4",
    "US100.cash_M5", "US100.cash_M15", "US100.cash_H1", "US100.cash_H4",
    "US500.cash_M5", "US500.cash_M15", "US500.cash_H1",
]


def row(name, b, times):
    ev = detect(b, times, tf_seconds(name.replace("~vol", "")))
    n = len(b)
    bull, bear = int((ev.d == 1).sum()), int((ev.d == -1).sum())
    g3t = int(ev.trend.sum())
    g3s = int(ev.strong.sum())
    first = int(ev.first_g3.sum())
    q = np.percentile(ev.size_atr, [25, 50, 75, 90]) if len(ev) else [np.nan] * 4
    return (
        f"{name:16s} bars {n:6d} | G3 {len(ev):5d} (bull {bull:5d}/bear {bear:5d}) per1000 {1000*len(ev)/n:5.1f} | "
        f"G3T {g3t:5d} G3S {g3s:5d} first-G3 {first:5d} | covers {100*ev.covers.mean():4.0f}% | "
        f"size/ATR q25/50/75/90 {q[0]:.2f}/{q[1]:.2f}/{q[2]:.2f}/{q[3]:.2f} | "
        f"touch {ev.n_touch:4d} session-break-excluded {ev.n_noncontig:4d}"
    )


def main():
    print("REAL data")
    for name in FILES:
        b, times = load_file(name)
        print(row(name, b, times))
    print("\nVOL-null (random bar direction, bar sizes kept) -- same detector, for scale only")
    for name in ("XAUUSD_M15", "US100.cash_M15", "US500.cash_H1"):
        b, times = load_file(name)
        v = surrogate_vol(b, 4)
        print(row(name + "~vol", v, times))


if __name__ == "__main__":
    main()
