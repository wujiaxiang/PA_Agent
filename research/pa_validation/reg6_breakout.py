"""reg6: behaviour after a close beyond the previous N-bar extreme (descriptive, up + down pooled)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from common2 import invert
from common3 import compare, table

HZ = 10


def one_side(b, N, tag, acc):
    c, h, l, atr = b.c, b.h, b.l, b.atr14
    n = len(c)
    hh = pd.Series(h).rolling(N).max().shift(1).to_numpy()
    ev = (c > hh) & ~np.isnan(hh) & ~np.isnan(atr)
    ev[1:] &= ~ev[:-1]                       # only the first bar of a streak
    for t in np.flatnonzero(ev):
        if t + HZ >= n:
            break
        L = hh[t]
        a = atr[t]
        if a <= 0:
            continue
        lows = l[t + 1:t + HZ + 1]
        cls = c[t + 1:t + HZ + 1]
        hs = h[t + 1:t + HZ + 1]
        acc["retest"].append(float((lows <= L).any()))
        acc["close_back"].append(float((cls < L).any()))
        acc["up10"].append(float(c[t + HZ] > c[t]))
        acc["ret10"].append(float((c[t + HZ] - c[t]) / a))
        acc["gap_above"].append(float((c[t] - L) / a))
        res = np.nan
        for k in range(HZ):
            if cls[k] < L:
                res = 0.0
                break
            if hs[k] >= c[t] + a:
                res = 1.0
                break
        acc["ext_before_back"].append(res)


def stats(b):
    out = {}
    for N in (20, 50):
        acc = {k: [] for k in ("retest", "close_back", "up10", "ret10", "gap_above", "ext_before_back")}
        one_side(b, N, "up", acc)
        one_side(invert(b), N, "dn", acc)
        for k, v in acc.items():
            v = np.array(v, dtype=float)
            v = v[~np.isnan(v)]
            if len(v) > 30:
                out[f"N{N}_{k}"] = (float(v.mean()), len(v))
    return out


if __name__ == "__main__":
    print(f"reg6  close beyond previous N-bar extreme; horizon {HZ} bars (up+down pooled)")
    table(compare(stats))
