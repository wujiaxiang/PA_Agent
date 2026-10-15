"""reg2: persistence of trend/range 'states' using the efficiency ratio ER of non-overlapping blocks.

ER = |net move| / sum |bar-to-bar moves| over a block of L bars (1 = straight line, ~0 = pure chop).
Trendy = top 30% of ER within the series, Range = bottom 30% (so the base rate is 0.30 by construction
and the NULLs are comparable).
"""
from __future__ import annotations

import numpy as np

from common3 import compare, table


def stats(b):
    out = {}
    c = b.c
    for L in (20, 60):
        nb = (len(c) - 1) // L
        if nb < 30:
            continue
        steps = np.abs(np.diff(c))[: nb * L].reshape(nb, L)
        net = c[L: nb * L + 1: L] - c[0: nb * L: L]
        er = np.abs(net) / steps.sum(1)
        sg = np.sign(net)
        out[f"L{L}_er_mean"] = (float(er.mean()), nb)
        out[f"L{L}_er_p90"] = (float(np.percentile(er, 90)), nb)
        out[f"L{L}_er_ac1"] = (float(np.corrcoef(er[:-1], er[1:])[0, 1]), nb - 1)
        hi, lo = np.percentile(er, 70), np.percentile(er, 30)
        tr, rg = er >= hi, er <= lo
        a, bb = tr[:-1], tr[1:]
        out[f"L{L}_P(trendy|trendy)"] = (float(bb[a].mean()) if a.sum() else float("nan"), int(a.sum()))
        r0 = rg[:-1]
        out[f"L{L}_P(trendy|range)"] = (float(bb[r0].mean()) if r0.sum() else float("nan"), int(r0.sum()))
        out[f"L{L}_P(range|range)"] = (float(rg[1:][r0].mean()) if r0.sum() else float("nan"), int(r0.sum()))
        same = sg[:-1] == sg[1:]
        out[f"L{L}_P(same dir)"] = (float(same.mean()), nb - 1)
        out[f"L{L}_P(same dir|trendy)"] = (float(same[a].mean()) if a.sum() else float("nan"), int(a.sum()))
    return out


if __name__ == "__main__":
    print("reg2  trend/range state persistence  (blocks of L bars; base rate of 'trendy' = 0.30)")
    table(compare(stats))
