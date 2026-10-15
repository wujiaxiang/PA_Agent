"""reg5: relationship with EMA20 -- side runs, gap-bar runs, stretch distribution, return-to-EMA."""
from __future__ import annotations

import numpy as np

from common3 import compare, table


def _runs(sign):
    s = sign[sign != 0]
    if len(s) < 5:
        return np.array([1])
    chg = np.flatnonzero(np.diff(s) != 0)
    return np.diff(np.r_[-1, chg, len(s) - 1])


def stats(b):
    out = {}
    c, h, l, e, atr = b.c, b.h, b.l, b.ema20, b.atr14
    n = len(c)
    ok = (~np.isnan(e)) & (~np.isnan(atr)) & (atr > 0)
    if ok.sum() < 200:
        return out
    side = np.where(ok, np.sign(c - e), 0.0)
    runs = _runs(side)
    out["mean_side_run"] = (float(runs.mean()), len(runs))
    out["P(side run>=20)"] = (float((runs >= 20).mean()), len(runs))

    gap = np.where(ok & (l > e), 1.0, np.where(ok & (h < e), -1.0, 0.0))
    # gap-bar runs: consecutive bars fully on the same side of the EMA
    g_runs = []
    cur, cs = 0, 0.0
    for v in gap:
        if v != 0 and v == cs:
            cur += 1
        else:
            if cur > 0:
                g_runs.append(cur)
            cur, cs = (1, v) if v != 0 else (0, 0.0)
    if cur > 0:
        g_runs.append(cur)
    g_runs = np.array(g_runs) if g_runs else np.array([1])
    per1000 = 1000.0 / n
    for m in (10, 20, 30):
        out[f"gapruns>={m} per1000bars"] = (float((g_runs >= m).sum() * per1000), n)
    out["P(gap run>=10 | gap run>=5)"] = (
        float((g_runs >= 10).sum() / max(1, (g_runs >= 5).sum())), int((g_runs >= 5).sum()))
    out["P(gap run>=20 | gap run>=10)"] = (
        float((g_runs >= 20).sum() / max(1, (g_runs >= 10).sum())), int((g_runs >= 10).sum()))

    st = np.where(ok, np.abs(c - e) / np.where(ok, atr, 1.0), np.nan)
    for q in (50, 90, 99):
        out[f"stretch_q{q}_atr"] = (float(np.nanpercentile(st, q)), int(ok.sum()))

    touch = ok & (l <= e) & (h >= e)
    cum = np.cumsum(touch.astype(int))
    for hz in (3, 10):
        fut = np.full(n, np.nan)
        fut[: n - hz] = (cum[hz:] - cum[: n - hz] > 0).astype(float)
        for lo_, hi_ in ((0.5, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 99.0)):
            m = ok & (st >= lo_) & (st < hi_) & ~touch & ~np.isnan(fut)
            if m.sum() > 30:
                out[f"P(touch EMA<={hz}b | stretch {lo_}-{hi_ if hi_ < 99 else 'inf'})"] = (float(fut[m].mean()), int(m.sum()))
    return out


if __name__ == "__main__":
    print("reg5  EMA20 relationship")
    table(compare(stats))
