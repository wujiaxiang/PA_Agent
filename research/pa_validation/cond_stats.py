"""Conditional statistics with in-sample / out-of-sample halves.

Implements PREREG.md EXACTLY (H1..H5).  Do not change definitions or thresholds after seeing results.

usage:
    python cond_stats.py            # real run (all hypotheses)
    python cond_stats.py placebo    # calibration: REAL replaced by an independent null series
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from common import DATA_DIR
from common2 import invert, make_bars
from common3 import surrogate_vol

import reg7_time as r7

PREFIX = {"XAUUSD": "XAUUSD", "US100": "US100.cash", "US500": "US500.cash"}
INSTS = ["XAUUSD", "US100", "US500"]
VOL_SEED = 4
Z_IS, Z_OOS = 3.0, 2.0


# ------------------------------------------------------------------ data

def load_t(inst: str, tf: str):
    path = os.path.join(DATA_DIR, f"{PREFIX[inst]}_{tf}.parquet")
    df = pd.read_parquet(path).sort_values("time").reset_index(drop=True)
    b = make_bars(f"{PREFIX[inst]}_{tf}", df["open"], df["high"], df["low"], df["close"])
    return b, df["time"].to_numpy(np.int64)


def halves(n):
    return {"IS": (0, n // 2), "OOS": (n // 2, n)}


def prop_se(k, n):
    p = k / n
    return p, float(np.sqrt(p * (1 - p) / n))


# ------------------------------------------------------------------ H1

def breakout_events(b, N=20, H=5):
    hh = pd.Series(b.h).rolling(N).max().shift(1).to_numpy()
    ev = (b.c > hh) & ~np.isnan(hh) & ~np.isnan(b.atr14)
    ev[1:] &= ~ev[:-1]
    ts = np.flatnonzero(ev)
    ts = ts[ts + H < len(b)]
    rec = np.array([bool((b.c[t + 1:t + H + 1] < hh[t]).any()) for t in ts], dtype=bool)
    return ts, rec


def h1_events(b):
    t1, r1 = breakout_events(b)
    t2, r2 = breakout_events(invert(b))
    return np.r_[t1, t2], np.r_[r1, r2]


def run_h1(inst, tf, real, vol, times):
    n = len(times)
    ev = {"R": h1_events(real), "V": h1_events(vol)}
    out = {}
    for half, (lo, hi) in halves(n).items():
        parts = []
        for key in ("R", "V"):
            t, r = ev[key]
            m = (t >= lo) & (t < hi)
            t, r = t[m], r[m]
            cond = np.isin((times[t] // 3600) % 24, (16, 17))
            nc, no = int(cond.sum()), int((~cond).sum())
            if nc < 30 or no < 30:
                parts = None
                break
            pc, sc = prop_se(r[cond].sum(), nc)
            po, so = prop_se(r[~cond].sum(), no)
            parts.append((pc - po, float(np.hypot(sc, so))))
        if parts is None:
            out[half] = (np.nan, np.nan)
        else:
            (dr, sr), (dv, sv) = parts
            out[half] = (dr - dv, float(np.hypot(sr, sv)))
    return out


# ------------------------------------------------------------------ H2

def h2_arrays(b, stride=5, lag=5):
    n = len(b)
    med = pd.Series(b.atr14).rolling(200).median().shift(1).to_numpy()
    ratio = b.atr14 / med
    idx = np.arange(214, n - lag, stride)
    idx = idx[~np.isnan(ratio[idx])]
    prev = b.c[idx] - b.c[idx - lag]
    nxt = b.c[idx + lag] - b.c[idx]
    ok = (prev != 0) & (nxt != 0)
    same = np.sign(prev) == np.sign(nxt)
    return idx, ratio[idx], same, ok


def run_h2(inst, tf, real, vol, times):
    n = len(times)
    arr = {"R": h2_arrays(real), "V": h2_arrays(vol)}
    out = {}
    for half, (lo, hi) in halves(n).items():
        parts = []
        for key in ("R", "V"):
            idx, ratio, same, ok = arr[key]
            m = (idx >= lo) & (idx < hi) & ok
            hi_m = m & (ratio >= 1.5)
            lo_m = m & (ratio <= 0.7)
            nh, nl = int(hi_m.sum()), int(lo_m.sum())
            if nh < 30 or nl < 30:
                parts = None
                break
            ph, sh = prop_se(same[hi_m].sum(), nh)
            pl, sl = prop_se(same[lo_m].sum(), nl)
            parts.append((ph - pl, float(np.hypot(sh, sl))))
        if parts is None:
            out[half] = (np.nan, np.nan)
        else:
            (dr, sr), (dv, sv) = parts
            out[half] = (dr - dv, float(np.hypot(sr, sv)))
    return out


# ------------------------------------------------------------------ H3

def h3_obs(times, b, tf):
    sec = {"H1": 3600, "M15": 900, "M5": 300}[tf]
    idx_of = dict(zip(times.tolist(), range(len(times))))
    pos, same = [], []
    for d in np.unique(times // 86400):
        base = int(d) * 86400
        if tf == "H1":
            t_open, t_e1, t_e2 = base + 16 * 3600, base + 17 * 3600, base + 19 * 3600
        else:
            t_open = base + 16 * 3600 + 30 * 60
            t_e1 = base + 17 * 3600 - sec
            t_e2 = base + 19 * 3600 - sec
        if t_open not in idx_of or t_e1 not in idx_of or t_e2 not in idx_of:
            continue
        i0, i1, i2 = idx_of[t_open], idx_of[t_e1], idx_of[t_e2]
        m1 = b.c[i1] - b.o[i0]
        m2 = b.c[i2] - b.c[i1]
        if m1 == 0 or m2 == 0:
            continue
        pos.append(i0)
        same.append(np.sign(m1) == np.sign(m2))
    return np.array(pos), np.array(same, dtype=bool)


def run_h3(inst, tf, real, vol, times):
    n = len(times)
    obs = {"R": h3_obs(times, real, tf), "V": h3_obs(times, vol, tf)}
    out = {}
    for half, (lo, hi) in halves(n).items():
        parts = []
        for key in ("R", "V"):
            pos, same = obs[key]
            m = (pos >= lo) & (pos < hi)
            k, nn = int(same[m].sum()), int(m.sum())
            if nn < 30:
                parts = None
                break
            p, s = prop_se(k, nn)
            parts.append((p, s))
        if parts is None:
            out[half] = (np.nan, np.nan)
        else:
            (pr, sr), (pv, sv) = parts
            out[half] = (pr - pv, float(np.hypot(sr, sv)))
    return out


# ------------------------------------------------------------------ H4

H4_STEP = {"XAUUSD": 50, "US100": 100, "US500": 50}
H4_W = 0.04


def run_h4(inst, tf, real, vol, times):
    h, l = real.h, real.l
    n = len(h)
    is_hi = (h[2:-2] > h[1:-3]) & (h[2:-2] > h[:-4]) & (h[2:-2] > h[3:-1]) & (h[2:-2] > h[4:])
    is_lo = (l[2:-2] < l[1:-3]) & (l[2:-2] < l[:-4]) & (l[2:-2] < l[3:-1]) & (l[2:-2] < l[4:])
    ph, ih = h[2:-2][is_hi], np.flatnonzero(is_hi) + 2
    pl, il = l[2:-2][is_lo], np.flatnonzero(is_lo) + 2
    S = H4_STEP[inst]
    out = {}
    for half, (lo, hi) in halves(n).items():
        mh = (ih >= lo) & (ih < hi)
        ml = (il >= lo) & (il < hi)
        oh = (ph[mh] % S) / S
        ol = (pl[ml] % S) / S
        hb, ha = int((oh > 1 - H4_W).sum()), int((oh < H4_W).sum())
        la, lb = int((ol < H4_W).sum()), int((ol > 1 - H4_W).sum())
        k = hb + la
        nn = hb + ha + la + lb
        if nn < 30:
            out[half] = (np.nan, np.nan)
        else:
            out[half] = (k / nn - 0.5, float(0.5 / np.sqrt(nn)))
    return out


# ------------------------------------------------------------------ H5

def run_h5(inst, tf):
    bph = {"H1": 1, "M15": 4}[tf]
    df = r7.load_df(PREFIX[inst], tf)
    t = df.time.to_numpy()
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    groups = r7.day_groups(t, r7.session_start_hour(t))
    G = len(groups)
    out = {}
    for half, gs in (("IS", groups[: G // 2]), ("OOS", groups[G // 2:])):
        real = r7.session_stats(o, h, l, c, gs, bph)
        nul = [r7.session_stats(*r7.shuffle_days(o, h, l, c, gs, s), gs, bph) for s in (1, 2)]
        pr = np.r_[real["pos_h"], real["pos_l"]]
        pn = np.concatenate([np.r_[x["pos_h"], x["pos_l"]] for x in nul])
        if len(pr) < 60:
            out[half] = (np.nan, np.nan)
            continue
        a, sa = prop_se((pr < 0.1).sum(), len(pr))
        b_, sb = prop_se((pn < 0.1).sum(), len(pn))
        out[half] = (a - b_, float(np.hypot(sa, sb)))
    return out


# ------------------------------------------------------------------ driver

HYP = {
    "H1": dict(fn=run_h1, primary="M15", tfs=["M15", "M5", "H1"], title="US-open breakout reclaim-rate (cond - other), excess over VOL"),
    "H2": dict(fn=run_h2, primary="H1", tfs=["H1", "M15", "M5"], title="momentum same-sign rate (high-vol - low-vol), excess over VOL"),
    "H3": dict(fn=run_h3, primary="H1", tfs=["H1", "M15", "M5"], title="US-open direction continuation, excess over VOL (0.5)"),
    "H4": dict(fn=run_h4, primary="H1", tfs=["H1", "M15"], title="round-number barrier share - 0.5"),
    "H5": dict(fn=None, primary="H1", tfs=["H1", "M15"], title="first-decile share of session extremes: REAL - NULL"),
}


def unit_stats(per_inst, half):
    if "XAUUSD" not in per_inst:
        return None
    xe, xs = per_inst["XAUUSD"][half]
    ie = [per_inst[i][half] for i in ("US100", "US500") if i in per_inst]
    vals = [(xe, xs)] + ie
    if any(np.isnan(e) or np.isnan(s) or s == 0 for e, s in vals):
        return None
    z_x = xe / xs
    z_i = float(np.mean([e / s for e, s in ie]))
    e_i = float(np.mean([e for e, _ in ie]))
    s_i = float(np.mean([s for _, s in ie]))
    return dict(
        z=(z_x + z_i) / np.sqrt(2), e=(xe + e_i) / 2, s=float(np.hypot(xs, s_i) / 2), zx=z_x, zi=z_i,
    )


def verdict(zi, zo):
    if zi is None or zo is None:
        return "NO-DATA"
    if abs(zi) >= Z_IS:
        if np.sign(zo) == np.sign(zi) and abs(zo) >= Z_OOS:
            return "CONFIRMED"
        return "OVERFIT/UNSTABLE"
    return "NOT-FOUND"


def collect(hname, placebo_seed=None, tfs=None):
    spec = HYP[hname]
    res = {}
    for tf in tfs or spec["tfs"]:
        res[tf] = {}
        for inst in INSTS:
            try:
                if hname == "H5":
                    res[tf][inst] = run_h5(inst, tf)
                    continue
                b, times = load_t(inst, tf)
            except FileNotFoundError:
                continue
            if placebo_seed is None:
                real = b
            else:
                real = surrogate_vol(b, 100 + placebo_seed)
            vol = surrogate_vol(b, VOL_SEED)
            res[tf][inst] = spec["fn"](inst, tf, real, vol, times)
    return res


def show(hname, res):
    spec = HYP[hname]
    print("=" * 108)
    print(f"{hname}: {spec['title']}   (primary TF = {spec['primary']})")
    print("=" * 108)
    for tf in spec["tfs"]:
        if tf not in res:
            continue
        tag = "PRIMARY" if tf == spec["primary"] else "robustness"
        print(f"\n  [{tf}] ({tag})")
        for half in ("IS", "OOS"):
            cells = []
            for inst in INSTS:
                if inst in res[tf]:
                    e, s = res[tf][inst][half]
                    cells.append(f"{inst}: {100*e:+6.2f}pp (se {100*s:4.2f}, z={e/s:+5.1f})" if not np.isnan(e) else f"{inst}: n/a")
            u = unit_stats(res[tf], half)
            tot = f"| combined z={u['z']:+5.2f} (xau {u['zx']:+.1f}, idx {u['zi']:+.1f}), avg effect {100*u['e']:+.2f}pp +- {100*u['s']:.2f}" if u else "| n/a"
            print(f"    {half:3s} " + " ; ".join(cells) + " " + tot)
    pr = spec["primary"]
    if pr in res:
        ui, uo = unit_stats(res[pr], "IS"), unit_stats(res[pr], "OOS")
        v = verdict(ui["z"] if ui else None, uo["z"] if uo else None)
        extra = ""
        if uo:
            extra = f"  (OOS se of avg effect {100*uo['s']:.2f}pp -> minimum detectable ~ {100*2.8*uo['s']:.1f}pp)"
        print(f"\n  >>> {hname} VERDICT on primary TF: {v}{extra}")
        # robustness: same sign in IS and OOS combined effect on the other TFs
        base = np.sign(ui["z"]) if ui else 0
        rob = []
        for tf in spec["tfs"]:
            if tf == pr or tf not in res:
                continue
            a, c = unit_stats(res[tf], "IS"), unit_stats(res[tf], "OOS")
            ok = bool(a and c and np.sign(a["z"]) == base and np.sign(c["z"]) == base)
            rob.append(f"{tf}:{'same-sign' if ok else 'differs'}")
        print(f"      robustness: {', '.join(rob) if rob else 'n/a'}")
        return v
    return "NO-DATA"


def run_real():
    verdicts = {}
    for h in ("H1", "H2", "H3", "H4", "H5"):
        verdicts[h] = show(h, collect(h))
    print("\n" + "=" * 108)
    print("SUMMARY (pre-registered rule: IS |z|>=3 and OOS same sign |z|>=2 -> CONFIRMED)")
    for h, v in verdicts.items():
        print(f"  {h}: {v}")


def run_placebo(n_seeds=6):
    print("PLACEBO calibration: REAL replaced by an independent VOL-null series (no true effect exists).")
    print("combined z should be ~N(0,1): std near 1 means the standard errors are honest;")
    print("std >> 1 means z is inflated (overlapping events etc.) and thresholds are too lenient.\n")
    for h in ("H1", "H2", "H3", "H4"):
        spec = HYP[h]
        zis, zos = [], []
        for s in range(n_seeds):
            res = collect(h, placebo_seed=s, tfs=[spec["primary"]])
            ui, uo = unit_stats(res[spec["primary"]], "IS"), unit_stats(res[spec["primary"]], "OOS")
            if ui and uo:
                zis.append(ui["z"])
                zos.append(uo["z"])
        zis, zos = np.array(zis), np.array(zos)
        print(f"  {h}: IS z mean {zis.mean():+.2f} std {zis.std(ddof=1):.2f} | OOS z mean {zos.mean():+.2f} std {zos.std(ddof=1):.2f}"
              f"   (n={len(zis)} seeds; |z|>=3 in IS: {(abs(zis) >= 3).sum()})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "placebo":
        run_placebo()
    else:
        run_real()
