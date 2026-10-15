"""reg8: bar-type transition lifts, P(next=j | cur=i) / P(next=j), with the project's bar classification."""
from __future__ import annotations

import sys

import numpy as np

import common3 as c3
from common3 import compare

TYPES = ["TB", "TR", "DJ", "IN", "OB", "OR", "OT"]


def classify(b):
    o, h, l, c = b.o, b.h, b.l, b.c
    n = len(c)
    t = np.full(n, 6)                       # OT other
    rng = h - l
    with np.errstate(divide="ignore", invalid="ignore"):
        body = np.abs(c - o) / rng
        cp = (c - l) / rng
    ph, pl = np.r_[h[0], h[:-1]], np.r_[l[0], l[:-1]]
    inside = (h <= ph) & (l >= pl)
    outside = (h >= ph) & (l <= pl)
    doji = body <= 0.25
    tb = (c > o) & (cp >= 0.65)
    tr = (c < o) & (cp <= 0.35)
    t[tr] = 1
    t[tb] = 0
    t[doji] = 2
    t[outside & (c >= o)] = 4
    t[outside & (c < o)] = 5
    t[inside] = 3
    t[0] = 6
    return t


def stats(b):
    t = classify(b)
    cur, nxt = t[:-1], t[1:]
    m = np.zeros((7, 7))
    for i in range(7):
        for j in range(7):
            m[i, j] = ((cur == i) & (nxt == j)).sum()
    rowsum = m.sum(1)
    marg = m.sum(0) / m.sum()
    out = {}
    for i, a in enumerate(TYPES):
        out[f"freq_{a}"] = (float((t == i).mean()), len(t))
        for j, bn in enumerate(TYPES):
            if rowsum[i] > 30 and marg[j] > 0:
                out[f"lift {a}->{bn}"] = (float(m[i, j] / rowsum[i] / marg[j]), int(rowsum[i]))
    return out


if __name__ == "__main__":
    res = compare(stats)
    print("reg8  bar-type frequencies and transition lifts (TB/TR trend bull/bear, DJ doji, IN inside, OB/OR outside bull/bear, OT other)")
    c3.table(res, names=[f"freq_{a}" for a in TYPES], fmt="{:.3f}")
    # transition lifts: show only those where REAL deviates from 1 by >= 0.08 pooled AND from VOL consistently
    flagged = []
    for i, a in enumerate(TYPES):
        for j, bn in enumerate(TYPES):
            k = f"lift {a}->{bn}"
            pooled = {}
            for v in c3.VARIANTS:
                items = []
                for tf in c3.TFS:
                    items += res.get((k, tf), {}).get(v, [])
                pooled[v] = c3._wmean(items)
            up = dn = 0
            for tf in c3.TFS:
                d = res.get((k, tf), {})
                for r, vv in zip(d.get("REAL", []), d.get("VOL", [])):
                    if r[0] == r[0] and vv[0] == vv[0]:
                        up += r[0] > vv[0]
                        dn += r[0] < vv[0]
            flagged.append((abs(pooled["REAL"] - pooled["VOL"]), k, pooled, up, dn))
    flagged.sort(reverse=True)
    print("\nlargest REAL-vs-VOL differences in transition lift (top 14):")
    for diff, k, p, up, dn in flagged[:14]:
        print(f"  {k:12s} REAL {p['REAL']:.3f} | IID {p['IID']:.3f} | VOL {p['VOL']:.3f}  (R>V {up}/{up + dn})")
    print("\nREAL lifts that deviate most from 1 (top 12), with IID/VOL for reference:")
    flagged.sort(key=lambda x: -abs(x[2]["REAL"] - 1))
    for diff, k, p, up, dn in flagged[:12]:
        print(f"  {k:12s} REAL {p['REAL']:.3f} | IID {p['IID']:.3f} | VOL {p['VOL']:.3f}  (R>V {up}/{up + dn})")
