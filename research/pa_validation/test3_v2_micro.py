"""Test 3 v2: micro-channel first break ("first pullback") -> does the trend resume?

What was wrong in v1 (second version): "break" = any bar with a lower low than the previous
bar, "failed" = price trades above the break bar's high before below its low. Two adjacent
levels -> first-passage ~ coin flip BY CONSTRUCTION. 46% therefore meant nothing.

v2:
  Bull micro channel = run of L >= Lmin bars where every low >= previous low (no lower low),
      net progress (high_e - low_s) >= 2 ATR.   (bear is mirrored with invert)
  The run ends at the first bar B with low_B < low_prev  -> "first break of the micro channel".
  Race from bar B (inclusive of B's close info only through B+1 onward):
      RESUME  = high >= channel_high (+ x*ATR)      [trend makes a new extreme]
      BREAKS  = low  <= low_B - d*ATR                [the break extends]
  Reported for (x, d) in {(0,0.5),(0,1.0),(0.5,1.0)}.
  Controls (same race geometry):
    CTRL_normal : first lower-low bar after an ordinary strong up-leg (>=2 ATR in the last 5 bars)
                  that does NOT satisfy the micro-channel criterion
    NULL        : both detectors on a bar-shuffled surrogate
"""
from __future__ import annotations

import sys

import numpy as np

from common import pct
from common2 import Acc, datasets, invert, load, surrogate

H = 30
GEOMS = [(0.0, 0.5), (0.0, 1.0), (0.5, 1.0)]


def race(b, start, up, dn):
    n = len(b)
    for k in range(start, min(start + H, n)):
        if b.l[k] <= dn:
            return 0
        if b.h[k] >= up:
            return 1
    return None


def scan(b, Lmin, acc, tag):
    n = len(b)
    i = 25
    while i < n - H - 3:
        atr = b.atr14[i]
        if np.isnan(atr) or atr <= 0:
            i += 1
            continue
        # try to start a no-lower-low run at i
        j = i
        while j + 1 < n and b.l[j + 1] >= b.l[j]:
            j += 1
        L = j - i + 1
        B = j + 1
        if B >= n - H - 2:
            break
        if L >= Lmin:
            atr0 = b.atr14[j]
            progress = b.h[i:j + 1].max() - b.l[i]
            if atr0 > 0 and progress >= 2.0 * atr0 and b.l[B] < b.l[j]:
                ch_high = b.h[i:j + 1].max()
                for x, d in GEOMS:
                    res = race(b, B, ch_high + x * atr0, b.l[B] - d * atr0)
                    if res is not None:
                        acc.add(f"{tag}|micro|x{x}_d{d}", res == 1)
                i = B + 1
                continue
            i = j + 1
            continue
        # control: ordinary strong up-leg (not micro): first lower-low after a >=2ATR 5-bar push
        i += 1


def scan_control(b, acc, tag):
    """First lower-low bar B after a strong 5-bar push that is NOT a micro channel."""
    n = len(b)
    k = 30
    while k < n - H - 3:
        atr = b.atr14[k]
        if np.isnan(atr) or atr <= 0:
            k += 1
            continue
        seg_h = b.h[k - 4:k + 1]
        seg_l = b.l[k - 4:k + 1]
        push = seg_h.max() - seg_l[0]
        had_lower_low = bool((seg_l[1:] < seg_l[:-1]).any())
        closes_up = b.c[k] > b.c[k - 4]
        if push >= 2.0 * atr and had_lower_low and closes_up and b.l[k + 1] < b.l[k]:
            B = k + 1
            ch_high = seg_h.max()
            for x, d in GEOMS:
                res = race(b, B, ch_high + x * atr, b.l[B] - d * atr)
                if res is not None:
                    acc.add(f"{tag}|ctrl|x{x}_d{d}", res == 1)
            k += 8
        else:
            k += 1


def main():
    Lmins = [5, 7]
    real = {L: Acc() for L in Lmins}
    sur = {L: Acc() for L in Lmins}
    ctrl_r, ctrl_s = Acc(), Acc()
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        s = surrogate(b, 11)
        for L in Lmins:
            for series, a in ((b, real[L]), (s, sur[L])):
                scan(series, L, a, "up")
                scan(invert(series), L, a, "dn")
        for series, a in ((b, ctrl_r), (s, ctrl_s)):
            scan_control(series, a, "up")
            scan_control(invert(series), a, "dn")
        print("done", f, file=sys.stderr)

    def agg(acc, kind, geom):
        k = n = 0
        for d in ("up", "dn"):
            a, c = acc.get(f"{d}|{kind}|{geom}")
            k += a
            n += c
        return k, n

    print("=" * 100)
    print("TEST 3 v2  micro-channel FIRST break: P(trend makes new extreme BEFORE the break extends)")
    print("geometry x_d : resume level = channel high + x*ATR ; break-extends level = low_B - d*ATR")
    print("=" * 100)
    for x, d in GEOMS:
        g = f"x{x}_d{d}"
        print(f"\n--- geometry {g} ---")
        for L in Lmins:
            print(f"  micro L>={L}   REAL {pct(*agg(real[L], 'micro', g)):50s} | NULL {pct(*agg(sur[L], 'micro', g))}")
        print(f"  ordinary strong leg control  REAL {pct(*agg(ctrl_r, 'ctrl', g)):42s} | NULL {pct(*agg(ctrl_s, 'ctrl', g))}")
    print("\nREAD: 'resume' = the trend wins the race. Brooks' '>80% of first breaks fail' would show ~80% here")
    print("and clearly above BOTH the null and the ordinary-strong-leg control.")


if __name__ == "__main__":
    main()
