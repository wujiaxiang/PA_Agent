"""reg1: bar-level dependence and volatility structure (descriptive)."""
from __future__ import annotations

import numpy as np

from common3 import compare, table


def _ac(x, y):
    m = ~np.isnan(x) & ~np.isnan(y)
    if m.sum() < 50:
        return (float("nan"), 0)
    return (float(np.corrcoef(x[m], y[m])[0, 1]), int(m.sum()))


def stats(b):
    out = {}
    c, o, h, l = b.c, b.o, b.h, b.l
    atr = b.atr14
    n = len(c)
    a = atr[:-1]
    ok = (~np.isnan(a)) & (a > 0)
    z = np.full(n - 1, np.nan)
    z[ok] = np.clip((c[1:] - c[:-1])[ok] / a[ok], -5, 5)
    for lag in (1, 2, 3, 5):
        out[f"ac_ret_lag{lag}"] = _ac(z[:-lag], z[lag:])

    body = c - o
    rng = h - l
    d = np.where(np.abs(body) > 0.1 * np.maximum(rng, 1e-12), np.sign(body), 0.0)
    m = (d[:-1] != 0) & (d[1:] != 0)
    out["p_same_body_dir"] = (float((d[:-1][m] == d[1:][m]).mean()), int(m.sum()))
    dd = d[d != 0]
    chg = np.flatnonzero(np.diff(dd) != 0)
    runs = np.diff(np.r_[-1, chg, len(dd) - 1])
    out["mean_dir_run"] = (float(runs.mean()), len(runs))
    out["p_dir_run_ge6"] = (float((runs >= 6).mean()), len(runs))

    okk = (~np.isnan(atr)) & (atr > 0)
    rr = np.full(n, np.nan)
    rr[1:] = np.where(okk[:-1], rng[1:] / np.where(okk[:-1], atr[:-1], 1.0), np.nan)
    rr = np.clip(rr, 0, 6)
    for lag in (1, 5, 20):
        out[f"ac_range_lag{lag}"] = _ac(rr[:-lag], rr[lag:])

    base = np.nanmean(rr)
    nxt = np.full(n, np.nan)
    nxt[:-1] = rr[1:]
    inside = np.zeros(n, bool)
    inside[1:] = (h[1:] <= h[:-1]) & (l[1:] >= l[:-1])
    mm = inside & ~np.isnan(nxt)
    out["next_range_after_inside"] = (float(nxt[mm].mean() / base) if mm.sum() > 50 else float("nan"), int(mm.sum()))
    q25, q75 = np.nanpercentile(rr, 25), np.nanpercentile(rr, 75)
    nar = (rr < q25) & ~np.isnan(nxt)
    wid = (rr > q75) & ~np.isnan(nxt)
    out["next_range_after_narrow"] = (float(np.nanmean(nxt[nar]) / base), int(nar.sum()))
    out["next_range_after_wide"] = (float(np.nanmean(nxt[wid]) / base), int(wid.sum()))
    return out


if __name__ == "__main__":
    print("reg1  bar-level dependence & volatility   (range ratios are relative to the all-bar mean)")
    table(compare(stats))
