"""v3 utilities for DESCRIPTIVE market-structure regularities.

Two nulls are used for every statistic:
  IID : bars shuffled as units (kills ALL serial dependence incl. volatility clustering)
  VOL : every bar mirrored around its own open with a random sign (kills DIRECTIONAL dependence,
        keeps the ORDER of bar sizes, i.e. keeps volatility clustering)

A statistic is called a regularity only if REAL differs from VOL (direction-specific structure)
or, for pure volatility facts, from IID, in the same direction in (almost) every one of the
14 series (3 instruments x M5/M15/H1/H4/D1; US500 has no H4).
"""
from __future__ import annotations

import sys

import numpy as np

from common2 import datasets, load, make_bars, surrogate  # noqa: F401

TFS = ["M5", "M15", "H1", "H4", "D1"]
VARIANTS = ["REAL", "IID", "VOL"]


def tf_of(f: str) -> str:
    return f.split("_")[-1]


def inst_of(f: str) -> str:
    if f.startswith("XAUUSD"):
        return "XAUUSD"
    if f.startswith("US100"):
        return "US100"
    return "US500"


def surrogate_vol(b, seed: int = 0):
    rng = np.random.default_rng(seed)
    n = len(b)
    prev_c = np.r_[b.c[0], b.c[:-1]]
    lg = np.log(b.o / prev_c)
    lh = np.log(b.h / b.o)
    ll = np.log(b.l / b.o)
    lc = np.log(b.c / b.o)
    s = rng.choice([-1.0, 1.0], size=n)
    lg2 = s * lg
    lh2 = np.where(s > 0, lh, -ll)
    ll2 = np.where(s > 0, ll, -lh)
    lc2 = s * lc
    c_new = b.c[0] * np.exp(np.cumsum(lg2 + lc2))
    prev_new = np.r_[b.c[0], c_new[:-1]]
    o_new = prev_new * np.exp(lg2)
    return make_bars(b.name + "~vol", o_new, o_new * np.exp(lh2), o_new * np.exp(ll2), c_new)


def compare(stats_fn, seed_iid: int = 3, seed_vol: int = 4, only=None):
    """stats_fn(bars) -> {name: (value, n)}.  Returns {(name, tf): {variant: [(value, n, inst)]}}."""
    res: dict = {}
    for inst, f in datasets():
        if only and not only(f):
            continue
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        tf = tf_of(f)
        variants = {"REAL": b, "IID": surrogate(b, seed_iid), "VOL": surrogate_vol(b, seed_vol)}
        for v, bb in variants.items():
            for k, (val, n) in stats_fn(bb).items():
                res.setdefault((k, tf), {}).setdefault(v, []).append((val, n, inst_of(f)))
        print("done", f, file=sys.stderr)
    return res


def _wmean(items):
    items = [(v, n) for (v, n, _) in items if v == v and n > 0]
    if not items:
        return float("nan")
    tot = sum(n for _, n in items)
    return sum(v * n for v, n in items) / tot


def table(res, names=None, fmt="{:+.3f}"):
    """One block per statistic: pooled REAL/IID/VOL, consistency counts, per-TF REAL-VOL deltas."""
    if names is None:
        names = []
        for (k, _tf) in res:
            if k not in names:
                names.append(k)
    for k in names:
        pooled = {}
        for v in VARIANTS:
            allitems = []
            for tf in TFS:
                allitems += res.get((k, tf), {}).get(v, [])
            pooled[v] = _wmean(allitems)
        rv_up = rv_dn = ri_up = ri_dn = 0
        for tf in TFS:
            d = res.get((k, tf), {})
            for r, vv, ii in zip(d.get("REAL", []), d.get("VOL", []), d.get("IID", [])):
                if r[0] != r[0]:
                    continue
                if vv[0] == vv[0]:
                    rv_up += r[0] > vv[0]
                    rv_dn += r[0] < vv[0]
                if ii[0] == ii[0]:
                    ri_up += r[0] > ii[0]
                    ri_dn += r[0] < ii[0]
        deltas = []
        for tf in TFS:
            d = res.get((k, tf), {})
            a, b_ = _wmean(d.get("REAL", [])), _wmean(d.get("VOL", []))
            deltas.append(f"{tf} {fmt.format(a - b_) if a == a and b_ == b_ else 'n/a'}")
        print(
            f"{k:26s} REAL {fmt.format(pooled['REAL'])} | IID {fmt.format(pooled['IID'])} | "
            f"VOL {fmt.format(pooled['VOL'])} || R>V {rv_up}/{rv_up + rv_dn}  R>I {ri_up}/{ri_up + ri_dn}"
        )
        print(f"{'':26s}   (REAL-VOL by TF)  " + " | ".join(deltas))
