"""Test 2 v2: spike aftermath, REAL vs bar-shuffled NULL (same detector, same outcome rule)."""
from __future__ import annotations

import sys

from common import pct
from common2 import datasets, load, surrogate
from test2_spike import detect_spikes, ground_truth, HORIZON


def tally(b):
    c = {"continued": 0, "range": 0, "reversal": 0}
    for (end_idx, d, origin, spike_end, move) in detect_spikes(b):
        gt = ground_truth(b, end_idx, d, origin, spike_end, move)
        if gt:
            c[gt] += 1
    return c


def main():
    R = {"continued": 0, "range": 0, "reversal": 0}
    S = {"continued": 0, "range": 0, "reversal": 0}
    per_tf = {}
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        s = surrogate(b, 51)
        r, sn = tally(b), tally(s)
        tf = f.split("_")[-1]
        for k in R:
            R[k] += r[k]
            S[k] += sn[k]
            per_tf.setdefault(tf, [{"continued": 0, "range": 0, "reversal": 0}, {"continued": 0, "range": 0, "reversal": 0}])
            per_tf[tf][0][k] += r[k]
            per_tf[tf][1][k] += sn[k]
        print("done", f, file=sys.stderr)
    print("=" * 100)
    print(f"TEST 2 v2  spike aftermath, horizon {HORIZON} bars: REAL vs NULL")
    for label, rr, ss in [("ALL", R, S)] + [(tf, v[0], v[1]) for tf, v in per_tf.items()]:
        nr, ns = sum(rr.values()), sum(ss.values())
        print(f"\n[{label}]  REAL n={nr}  NULL n={ns}")
        for k in ("continued", "range", "reversal"):
            print(f"   {k:10s} REAL {pct(rr[k], nr):46s} | NULL {pct(ss[k], ns)}")


if __name__ == "__main__":
    main()
