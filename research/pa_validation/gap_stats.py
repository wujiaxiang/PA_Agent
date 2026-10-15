"""Pre-registered gap tests H6a, H6b, H7 -- implements GAP_PREREG.md EXACTLY.

usage:
    python gap_stats.py              # real run (decision on M15, robustness M5/H1, sensitivity)
    python gap_stats.py placebo      # calibration: real and control replaced by independent null series
"""
from __future__ import annotations

import sys

import numpy as np

from common3 import surrogate_vol
from cond_stats import Z_IS, Z_OOS, halves, unit_stats, verdict
from gap_defs import (BODY_MIN, CLOSEPOS_MIN, CONTIG_FACTOR, STRONG_ATR, detect, load_file, outcomes,
                      tf_seconds)

INSTS = ["XAUUSD", "US100", "US500"]
FILE_OF = {"XAUUSD": "XAUUSD_{tf}", "US100": "US100.cash_{tf}", "US500": "US500.cash_{tf}"}
SIZE_EDGES = [0.1, 0.25, 0.5, 1.0]
DIST_EDGES = [0.5, 1.5]
RNG_EDGES = [1.5, 2.5]
NS, ND = len(SIZE_EDGES) + 1, len(DIST_EDGES) + 1
NSTRATA = NS * ND
MIN_N = 30
VOL_SEED = 4
PRIMARY_TF = "M15"
TFS = ["M15", "M5", "H1"]

_CACHE: dict = {}


def get(inst: str, tf: str):
    key = (inst, tf)
    if key not in _CACHE:
        name = FILE_OF[inst].format(tf=tf)
        b, times = load_file(name)
        _CACHE[key] = (b, times, tf_seconds(name))
    return _CACHE[key]


# --------------------------------------------------------------------------- H6a / H6b

def dist_atr(b, ev):
    c = b.c[ev.t]
    return np.where(ev.d == 1, (c - ev.z_hi) / ev.atr, (ev.z_lo - c) / ev.atr)


def strata_ids(b, ev):
    s = np.digitize(ev.size_atr, SIZE_EDGES)             # 0..4
    d = np.digitize(dist_atr(b, ev), DIST_EDGES)         # 0..2
    return s * ND + d


def h6_table(b, ev, tier, N):
    test, mid, fill, ok = outcomes(b, ev, N)
    sel = ev.tier(tier, True) & ok
    sid = strata_ids(b, ev)
    return dict(t=ev.t[sel], sid=sid[sel], test=test[sel] > 0, fill=fill[sel] > 0)


def _counts(sid, flag=None):
    n = np.bincount(sid, minlength=NSTRATA).astype(float)
    k = np.bincount(sid, weights=None if flag is None else flag.astype(float), minlength=NSTRATA) if flag is not None else n
    return k, n


def strat_excess(kR, nR, kV, nV):
    ok = (nR >= MIN_N) & (nV >= MIN_N)
    if not ok.any():
        return np.nan, np.nan
    pR, pV = kR[ok] / nR[ok], kV[ok] / nV[ok]
    seR2, seV2 = pR * (1 - pR) / nR[ok], pV * (1 - pV) / nV[ok]
    w = nR[ok] / nR[ok].sum()
    return float((w * (pR - pV)).sum()), float(np.sqrt((w ** 2 * (seR2 + seV2)).sum()))


def run_h6(real, vol, times, step, tier="G3S", N=20):
    evR, evV = detect(real, times, step), detect(vol, times, step)
    TR, TV = h6_table(real, evR, tier, N), h6_table(vol, evV, tier, N)
    n = len(real)
    a, b_ = {}, {}
    for half, (lo, hi) in halves(n).items():
        mR = (TR["t"] >= lo) & (TR["t"] < hi)
        mV = (TV["t"] >= lo) & (TV["t"] < hi)
        nR = np.bincount(TR["sid"][mR], minlength=NSTRATA).astype(float)
        nV = np.bincount(TV["sid"][mV], minlength=NSTRATA).astype(float)
        kR = np.bincount(TR["sid"][mR], weights=TR["test"][mR].astype(float), minlength=NSTRATA)
        kV = np.bincount(TV["sid"][mV], weights=TV["test"][mV].astype(float), minlength=NSTRATA)
        a[half] = strat_excess(kR, nR, kV, nV)
        mR2, mV2 = mR & TR["test"], mV & TV["test"]
        nR2 = np.bincount(TR["sid"][mR2], minlength=NSTRATA).astype(float)
        nV2 = np.bincount(TV["sid"][mV2], minlength=NSTRATA).astype(float)
        kR2 = np.bincount(TR["sid"][mR2], weights=TR["fill"][mR2].astype(float), minlength=NSTRATA)
        kV2 = np.bincount(TV["sid"][mV2], weights=TV["fill"][mV2].astype(float), minlength=NSTRATA)
        b_[half] = strat_excess(kR2, nR2, kV2, nV2)
    # descriptive (whole sample): per size bucket, raw rates
    desc = {}
    for key, T in (("R", TR), ("V", TV)):
        size_b = T["sid"] // ND
        desc[key] = dict(
            n=np.bincount(size_b, minlength=NS).astype(float),
            test=np.bincount(size_b, weights=T["test"].astype(float), minlength=NS),
            fill_given_test=np.bincount(size_b[T["test"]], weights=T["fill"][T["test"]].astype(float), minlength=NS),
            tested=np.bincount(size_b[T["test"]], minlength=NS).astype(float),
        )
    return a, b_, desc


# --------------------------------------------------------------------------- H7

def h7_samples(b, times, step, N=10):
    n = len(b)
    groups = {"gap": [], "ctrl": []}
    if n < N + 5:
        return {k: (np.array([], int), np.array([]), np.array([])) for k in groups}
    t = np.arange(2, n - N)
    for d in (1, -1):
        if d == 1:
            H, L, O, C = b.h, b.l, b.o, b.c
        else:
            H, L, O, C = -b.l, -b.h, -b.o, -b.c
        gap = L[t] - H[t - 2]
        atr = b.atr14[t - 2]
        okatr = ~np.isnan(atr) & (atr > 0)
        contig = np.ones(len(t), bool) if times is None else (
            (times[t] - times[t - 1] <= CONTIG_FACTOR * step) & (times[t - 1] - times[t - 2] <= CONTIG_FACTOR * step))
        Om, Hm, Lm, Cm = O[t - 1], H[t - 1], L[t - 1], C[t - 1]
        rng = Hm - Lm
        with np.errstate(divide="ignore", invalid="ignore"):
            body = (Cm - Om) / rng
            cp = (Cm - Lm) / rng
        trend = (rng > 0) & (Cm > Om) & (body > BODY_MIN) & (cp >= CLOSEPOS_MIN)
        strong = trend & (rng >= STRONG_ATR * np.where(okatr, atr, np.inf))
        base = okatr & contig & strong
        masks = {"gap": base & (gap > 0), "ctrl": base & (gap < 0)}
        R = (C[t + N] - C[t]) / np.where(okatr, atr, np.nan)
        rng_atr = rng / np.where(okatr, atr, np.nan)
        for g, m in masks.items():
            full = np.zeros(n, bool)
            full[t] = m
            first = ~(full[t - 1] | full[t - 2])
            sel = m & first
            groups[g].append((t[sel], R[sel], rng_atr[sel]))
    out = {}
    for g, parts in groups.items():
        out[g] = tuple(np.concatenate([p[i] for p in parts]) for i in range(3))
    return out


def h7_effect(samples, n):
    tg, Rg, rg = samples["gap"]
    tc, Rc, rc = samples["ctrl"]
    sg, sc = np.digitize(rg, RNG_EDGES), np.digitize(rc, RNG_EDGES)
    out = {}
    for half, (lo, hi) in halves(n).items():
        mg, mc = (tg >= lo) & (tg < hi), (tc >= lo) & (tc < hi)
        diffs, ses, ws = [], [], []
        for s in range(len(RNG_EDGES) + 1):
            a, c = Rg[mg & (sg == s)], Rc[mc & (sc == s)]
            if len(a) < MIN_N or len(c) < MIN_N:
                continue
            diffs.append(a.mean() - c.mean())
            ses.append(np.sqrt(a.var(ddof=1) / len(a) + c.var(ddof=1) / len(c)))
            ws.append(len(a))
        if not diffs:
            out[half] = (np.nan, np.nan)
            continue
        w = np.array(ws, float) / sum(ws)
        out[half] = (float((w * np.array(diffs)).sum()), float(np.sqrt(((w * np.array(ses)) ** 2).sum())))
    return out


# --------------------------------------------------------------------------- driver

def collect(tf, placebo_seed=None, tier="G3S", N6=20, N7=10):
    res = {"H6a": {}, "H6b": {}, "H7": {}}
    desc = {"R": None, "V": None}
    for inst in INSTS:
        try:
            b, times, step = get(inst, tf)
        except FileNotFoundError:
            continue
        if placebo_seed is None:
            real, vol = b, surrogate_vol(b, VOL_SEED)
        else:
            real, vol = surrogate_vol(b, 1000 + placebo_seed), surrogate_vol(b, 2000 + placebo_seed)
        a, b_, d = run_h6(real, vol, times, step, tier, N6)
        res["H6a"][inst], res["H6b"][inst] = a, b_
        res["H7"][inst] = h7_effect(h7_samples(real, times, step, N7), len(real))
        for k in ("R", "V"):
            if desc[k] is None:
                desc[k] = {kk: v.copy() for kk, v in d[k].items()}
            else:
                for kk in desc[k]:
                    desc[k][kk] += d[k][kk]
    return res, desc


TITLES = {
    "H6a": ("magnet: P(zone tested within N) REAL - VOL, size x distance strata", 100, "pp"),
    "H6b": ("holds: P(filled | tested) REAL - VOL, size x distance strata", 100, "pp"),
    "H7": ("strength: mean fwd return after gap - after non-gap strong bar (ATR)", 1, "ATR"),
}


def show(h, results_by_tf):
    title, scale, unit = TITLES[h]
    print("=" * 112)
    print(f"{h}: {title}   (primary TF = {PRIMARY_TF})")
    print("=" * 112)
    for tf in TFS:
        if tf not in results_by_tf:
            continue
        res = results_by_tf[tf][h]
        print(f"\n  [{tf}] ({'PRIMARY' if tf == PRIMARY_TF else 'robustness'})")
        for half in ("IS", "OOS"):
            cells = []
            for inst in INSTS:
                if inst in res:
                    e, s = res[inst][half]
                    cells.append(f"{inst}: {scale*e:+7.3f}{unit} (se {scale*s:5.3f}, z={e/s:+5.1f})" if not np.isnan(e) else f"{inst}: n/a")
            u = unit_stats(res, half)
            tot = (f"| combined z={u['z']:+5.2f} (xau {u['zx']:+.1f}, idx {u['zi']:+.1f}), avg {scale*u['e']:+.3f}{unit} +- {scale*u['s']:.3f}"
                   if u else "| n/a")
            print(f"    {half:3s} " + " ; ".join(cells) + " " + tot)
    res = results_by_tf[PRIMARY_TF][h]
    ui, uo = unit_stats(res, "IS"), unit_stats(res, "OOS")
    v = verdict(ui["z"] if ui else None, uo["z"] if uo else None)
    extra = f"  (OOS se of avg effect {scale*uo['s']:.3f}{unit}; minimum detectable ~ {scale*2.8*uo['s']:.2f}{unit})" if uo else ""
    print(f"\n  >>> {h} VERDICT on primary TF: {v}{extra}")
    base = np.sign(ui["z"]) if ui else 0
    rob = []
    for tf in TFS:
        if tf == PRIMARY_TF or tf not in results_by_tf:
            continue
        a, c = unit_stats(results_by_tf[tf][h], "IS"), unit_stats(results_by_tf[tf][h], "OOS")
        ok = bool(a and c and np.sign(a["z"]) == base and np.sign(c["z"]) == base)
        rob.append(f"{tf}:{'same-sign' if ok else 'differs'}")
    print(f"      robustness: {', '.join(rob)}")
    return v


def print_desc(desc):
    print("\nDESCRIPTIVE (M15, whole sample, G3S first-in-run, N=20; raw rates by gap size, all strata pooled)")
    labels = ["<0.1", "0.1-0.25", "0.25-0.5", "0.5-1", ">=1"]
    print(f"  {'size/ATR':9s} {'n REAL':>7s} {'P(test) REAL':>13s} {'P(test) VOL':>12s} | {'P(fill|test) REAL':>18s} {'P(fill|test) VOL':>17s}")
    for i, lb in enumerate(labels):
        nR, nV = desc["R"]["n"][i], desc["V"]["n"][i]
        tR, tV = desc["R"]["test"][i], desc["V"]["test"][i]
        fR = desc["R"]["fill_given_test"][i] / max(desc["R"]["tested"][i], 1)
        fV = desc["V"]["fill_given_test"][i] / max(desc["V"]["tested"][i], 1)
        print(f"  {lb:9s} {int(nR):7d} {100*tR/max(nR,1):12.1f}% {100*tV/max(nV,1):11.1f}% | {100*fR:17.1f}% {100*fV:16.1f}%")


def run_real():
    results, descs = {}, {}
    for tf in TFS:
        results[tf], descs[tf] = collect(tf)
    verdicts = {h: show(h, results) for h in ("H6a", "H6b", "H7")}
    print_desc(descs[PRIMARY_TF])
    print("\nSENSITIVITY (primary TF M15, not part of the verdict): combined z  IS / OOS")
    for label, kw in (("G3T N=20", dict(tier="G3T")), ("G3 N=20", dict(tier="G3")),
                      ("G3S N=60 (H6) / N7=20", dict(N6=60, N7=20)), ("G3S N7=5", dict(N7=5))):
        r, _ = collect(PRIMARY_TF, **kw)
        cells = []
        for h in ("H6a", "H6b", "H7"):
            a, c = unit_stats(r[h], "IS"), unit_stats(r[h], "OOS")
            cells.append(f"{h} {a['z']:+.2f}/{c['z']:+.2f}" if a and c else f"{h} n/a")
        print(f"  {label:24s} " + "   ".join(cells))
    print("\n" + "=" * 112)
    print("SUMMARY (pre-registered rule: IS |z|>=3 and OOS same sign |z|>=2 -> CONFIRMED)")
    for h, v in verdicts.items():
        print(f"  {h}: {v}")


def run_placebo(n_seeds=10):
    print("PLACEBO calibration (M15): REAL and control are independent random-direction null series.")
    print("combined z should be ~N(0,1).  std near 1 => standard errors honest; std >> 1 => z inflated.\n")
    acc = {h: {"IS": [], "OOS": []} for h in ("H6a", "H6b", "H7")}
    for s in range(n_seeds):
        r, _ = collect(PRIMARY_TF, placebo_seed=s)
        for h in acc:
            for half in ("IS", "OOS"):
                u = unit_stats(r[h], half)
                if u:
                    acc[h][half].append(u["z"])
        print(f"  seed {s} done", file=sys.stderr)
    for h, d in acc.items():
        zi, zo = np.array(d["IS"]), np.array(d["OOS"])
        both = int(((np.abs(zi) >= Z_IS) & (np.sign(zo) == np.sign(zi)) & (np.abs(zo) >= Z_OOS)).sum()) if len(zi) == len(zo) else -1
        print(f"  {h}: IS z mean {zi.mean():+.2f} std {zi.std(ddof=1):.2f}, |z|>=2: {(abs(zi)>=2).sum()}, |z|>=3: {(abs(zi)>=3).sum()} | "
              f"OOS z mean {zo.mean():+.2f} std {zo.std(ddof=1):.2f}, |z|>=2: {(abs(zo)>=2).sum()} | n={len(zi)} | would be CONFIRMED by chance: {both}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "placebo":
        run_placebo()
    else:
        run_real()
