"""Validation Test 6: Channel pullback-depth distribution + bucket boundaries.

Prompt claim (市场诊断框架 状态3-5 / 文件13 判定树):
  Channel classification uses "最近一波回撤深度" as the PRIMARY key:
    tight  : pullback < 30%
    normal : 30% - 50%
    broad  : 50% - 78.6%
  Question we can test empirically:
    (a) What is the actual distribution of pullback depths in trending legs?
    (b) Does a shallower pullback (tight-channel regime) actually continue the
        trend more reliably than a deep pullback (broad-channel regime)?
        -> i.e. is the ordering tight > normal > broad in continuation prob,
        which is what justifies "tight channel = only with-trend".

Method: Identify an impulse leg (swing low->high for bull) via EMA-rising segments,
measure the following pullback depth as % of the leg, then check whether the trend
makes a new extreme (continues) before retracing beyond 100% of the leg.
"""
from __future__ import annotations

import numpy as np
from common import INSTRUMENTS, load, pct

CONT_HORIZON = 30
MIN_LEG_ATR = 2.0


def swings(b):
    """1-bar neighbour pivots, chronological; returns list of (idx, kind, price)."""
    n = len(b)
    piv = []
    for i in range(1, n - 1):
        if b.h[i] > b.h[i - 1] and b.h[i] > b.h[i + 1]:
            piv.append((i, "H", b.h[i]))
        if b.l[i] < b.l[i - 1] and b.l[i] < b.l[i + 1]:
            piv.append((i, "L", b.l[i]))
    return piv


def run_symbol(name):
    b = load(name)
    n = len(b)
    piv = swings(b)
    buckets = {"tight": [0, 0], "normal": [0, 0], "broad": [0, 0], "deep": [0, 0]}
    # iterate L->H->pullback for bull legs
    for idx in range(len(piv) - 2):
        a = piv[idx]
        c = piv[idx + 1]
        # bull leg: low then high
        if a[1] == "L" and c[1] == "H":
            lo_i, lo_p = a[0], a[2]
            hi_i, hi_p = c[0], c[2]
            atr = b.atr14[hi_i]
            if np.isnan(atr) or atr <= 0:
                continue
            leg = hi_p - lo_p
            if leg < MIN_LEG_ATR * atr:
                continue
            # pullback low after hi_i
            pb_i = piv[idx + 2]
            if pb_i[1] != "L":
                continue
            pb_low = pb_i[2]
            depth = (hi_p - pb_low) / leg
            if depth <= 0 or depth > 1.2:
                continue
            # continuation: makes new high > hi_p before closing below lo_p
            cont = False
            for k in range(pb_i[0] + 1, min(pb_i[0] + 1 + CONT_HORIZON, n)):
                if b.h[k] > hi_p:
                    cont = True; break
                if b.c[k] < lo_p:
                    break
            key = ("tight" if depth < 0.30 else
                   "normal" if depth < 0.50 else
                   "broad" if depth < 0.786 else "deep")
            buckets[key][0] += int(cont)
            buckets[key][1] += 1
        # bear leg: high then low
        if a[1] == "H" and c[1] == "L":
            hi_i, hi_p = a[0], a[2]
            lo_i, lo_p = c[0], c[2]
            atr = b.atr14[lo_i]
            if np.isnan(atr) or atr <= 0:
                continue
            leg = hi_p - lo_p
            if leg < MIN_LEG_ATR * atr:
                continue
            pb_i = piv[idx + 2]
            if pb_i[1] != "H":
                continue
            pb_high = pb_i[2]
            depth = (pb_high - lo_p) / leg
            if depth <= 0 or depth > 1.2:
                continue
            cont = False
            for k in range(pb_i[0] + 1, min(pb_i[0] + 1 + CONT_HORIZON, n)):
                if b.l[k] < lo_p:
                    cont = True; break
                if b.c[k] > hi_p:
                    break
            key = ("tight" if depth < 0.30 else
                   "normal" if depth < 0.50 else
                   "broad" if depth < 0.786 else "deep")
            buckets[key][0] += int(cont)
            buckets[key][1] += 1
    return name, buckets


def main():
    print("=" * 90)
    print("TEST 6: Pullback depth bucket -> trend continuation probability")
    print("  (tight<30% | normal 30-50% | broad 50-78.6% | deep >78.6%)")
    print(f"  continuation = new leg extreme before closing past leg origin, horizon {CONT_HORIZON}")
    print("=" * 90)
    agg = {"tight": [0, 0], "normal": [0, 0], "broad": [0, 0], "deep": [0, 0]}
    for inst, files in INSTRUMENTS.items():
        print(f"\n### {inst}")
        for f in files:
            try:
                name, bk = run_symbol(f)
            except FileNotFoundError:
                continue
            print(f"  {f:18s} tight {pct(*bk['tight'])} | normal {pct(*bk['normal'])} | "
                  f"broad {pct(*bk['broad'])} | deep {pct(*bk['deep'])}")
            for k in agg:
                agg[k][0] += bk[k][0]; agg[k][1] += bk[k][1]
    print("\n" + "=" * 90)
    print("AGGREGATE continuation probability by pullback-depth bucket:")
    for k in ("tight", "normal", "broad", "deep"):
        print(f"  {k:7s}: {pct(*agg[k])}")
    print("=" * 90)


if __name__ == "__main__":
    main()
