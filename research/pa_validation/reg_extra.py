"""Extra checks:
 (a) 'contraction -> expansion': range of the next bar(s) after inside bars (i, ii, iii) and after
     runs of narrow bars, relative to the all-bar mean.
 (b) round-number asymmetry consistency across the 14 series (main step per instrument).
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import common3 as c3
from common2 import datasets, load
from common3 import compare, inst_of, tf_of


def stats(b):
    out = {}
    h, l, atr = b.h, b.l, b.atr14
    n = len(h)
    ok = (~np.isnan(atr)) & (atr > 0)
    rng = h - l
    r = np.where(ok, rng / np.where(ok, atr, 1.0), np.nan)       # range relative to the ATR at the same bar
    r = np.clip(r, 0, 8)
    base = np.nanmean(r)
    ins = np.zeros(n, bool)
    ins[1:] = (h[1:] <= h[:-1]) & (l[1:] >= l[:-1])
    ii = np.zeros(n, bool)
    ii[1:] = ins[1:] & ins[:-1]
    iii = np.zeros(n, bool)
    iii[2:] = ins[2:] & ins[1:-1] & ins[:-2]
    q25 = np.nanpercentile(r, 25)
    nar = (r < q25)
    n3 = np.zeros(n, bool)
    n3[2:] = nar[2:] & nar[1:-1] & nar[:-2]
    nxt1 = np.full(n, np.nan)
    nxt1[:-1] = r[1:]
    mx5 = pd.Series(r).rolling(5).max().shift(-5).to_numpy()
    base5 = np.nanmean(mx5)
    for name, m in (("inside", ins), ("ii", ii), ("iii", iii), ("3 narrow bars", n3)):
        mm = m & ~np.isnan(nxt1) & ~np.isnan(mx5)
        if mm.sum() > 40:
            out[f"next range after {name}"] = (float(nxt1[mm].mean() / base), int(mm.sum()))
            out[f"max range next5 after {name}"] = (float(mx5[mm].mean() / base5), int(mm.sum()))
    return out


def roundnum():
    MAIN = {"XAUUSD": 50, "US100": 100, "US500": 50}
    print("\n(b) round-number asymmetry per series (main step), share difference in percentage points")
    print("    highs: just-below minus just-above ; lows: just-above minus just-below   (barrier => both > 0)")
    pos_h = pos_l = tot = 0
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        S = MAIN[inst_of(f)]
        h, l = b.h, b.l
        hi = (h[2:-2] > h[1:-3]) & (h[2:-2] > h[:-4]) & (h[2:-2] > h[3:-1]) & (h[2:-2] > h[4:])
        lo = (l[2:-2] < l[1:-3]) & (l[2:-2] < l[:-4]) & (l[2:-2] < l[3:-1]) & (l[2:-2] < l[4:])
        ph, pl = h[2:-2][hi], l[2:-2][lo]
        oh, ol = (ph % S) / S, (pl % S) / S
        w = 0.04
        dh = ((oh > 1 - w).mean() - (oh < w).mean()) * 100
        dl = ((ol < w).mean() - (ol > 1 - w).mean()) * 100
        tot += 1
        pos_h += dh > 0
        pos_l += dl > 0
        print(f"    {f:16s} step {S:3d}  highs {dh:+5.2f} pp | lows {dl:+5.2f} pp  (n_hi={len(ph)}, n_lo={len(pl)})")
    print(f"    series with highs>0: {pos_h}/{tot} ; lows>0: {pos_l}/{tot}")


if __name__ == "__main__":
    print("(a) contraction -> expansion?   values are relative to the all-bar mean (1.0 = no change)")
    c3.table(compare(stats), fmt="{:.3f}")
    roundnum()
