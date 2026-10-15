"""reg7: intraday time structure (server time as stored by MT5).

(1) hour-of-day relative range; (2) when the session high/low occurs (deciles) vs a within-day
bar-shuffled null; (3) direction of the first 1h/2h of the session vs the rest of the session.
"""
from __future__ import annotations

import os
from collections import Counter

import numpy as np
import pandas as pd

from common import DATA_DIR, pct

PREFIX = {"XAUUSD": "XAUUSD", "US100": "US100.cash", "US500": "US500.cash"}
TF_BARS_PER_HOUR = {"M15": 4, "H1": 1}


def load_df(prefix, tf):
    df = pd.read_parquet(os.path.join(DATA_DIR, f"{prefix}_{tf}.parquet")).sort_values("time").reset_index(drop=True)
    return df


def session_start_hour(t):
    dt = np.diff(t)
    step = np.median(dt)
    big = np.flatnonzero((dt > 1.5 * step) & (dt <= 12 * 3600))
    if len(big) < 20:
        return 0
    hrs = Counter(((t[big + 1] // 3600) % 24).astype(int).tolist())
    return hrs.most_common(1)[0][0]


def day_groups(t, start_h):
    day = (t - start_h * 3600) // 86400
    order = np.arange(len(t))
    groups = []
    cur = day[0]
    s = 0
    for i in range(1, len(t)):
        if day[i] != cur:
            groups.append(order[s:i])
            s, cur = i, day[i]
    groups.append(order[s:])
    return groups


def shuffle_days(o, h, l, c, groups, seed):
    rng = np.random.default_rng(seed)
    o2, h2, l2, c2 = o.copy(), h.copy(), l.copy(), c.copy()
    for ix in groups:
        m = len(ix)
        if m < 4:
            continue
        prev_c = np.r_[o[ix[0]], c[ix[:-1]]]
        gap = o[ix] / prev_c
        hr, lr, cr = h[ix] / o[ix], l[ix] / o[ix], c[ix] / o[ix]
        price = o[ix[0]]
        for j, p in enumerate(rng.permutation(m)):
            oo = price * gap[p]
            o2[ix[j]], h2[ix[j]], l2[ix[j]], c2[ix[j]] = oo, oo * hr[p], oo * lr[p], oo * cr[p]
            price = c2[ix[j]]
    return o2, h2, l2, c2


def session_stats(o, h, l, c, groups, bph):
    sizes = np.array([len(g) for g in groups])
    med = np.median(sizes)
    pos_h, pos_l, same1, same2, dayfirst1, dayfirst2 = [], [], [], [], [], []
    first_hour_extreme = 0
    nd = 0
    for g in groups:
        m = len(g)
        if m < 0.8 * med or m < 3 * bph + 2:
            continue
        nd += 1
        hh, ll = h[g], l[g]
        ih, il = int(np.argmax(hh)), int(np.argmin(ll))
        pos_h.append(ih / (m - 1))
        pos_l.append(il / (m - 1))
        if ih < bph or il < bph:
            first_hour_extreme += 1
        for k, lst, lst2 in ((bph, same1, dayfirst1), (2 * bph, same2, dayfirst2)):
            first = c[g[k - 1]] - o[g[0]]
            rest = c[g[-1]] - c[g[k - 1]]
            whole = c[g[-1]] - o[g[0]]
            if first != 0 and rest != 0:
                lst.append(np.sign(first) == np.sign(rest))
            if first != 0 and whole != 0:
                lst2.append(np.sign(first) == np.sign(whole))
    return dict(pos_h=np.array(pos_h), pos_l=np.array(pos_l), nd=nd, fhe=first_hour_extreme,
                same1=same1, same2=same2, d1=dayfirst1, d2=dayfirst2)


def decile_share(pos):
    cnt, _ = np.histogram(pos, bins=10, range=(0, 1.0000001))
    return cnt / cnt.sum()


def report(inst, tf, show_hours=True):
    df = load_df(PREFIX[inst], tf)
    t = df.time.to_numpy()
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    sh = session_start_hour(t)
    groups = day_groups(t, sh)
    bph = TF_BARS_PER_HOUR[tf]
    print(f"\n=== {inst} {tf}: session start hour (server) {sh}:00, sessions {len(groups)} ===")
    if show_hours:
        hour = ((t // 3600) % 24).astype(int)
        rel = (h - l) / c
        prof = pd.Series(rel).groupby(hour).mean()
        idx = prof / prof.mean()
        cnt = pd.Series(1, index=range(len(t))).groupby(hour).sum()
        act = idx[cnt >= 0.5 * cnt.median()]
        top = act.sort_values(ascending=False).head(4)
        bot = act.sort_values().head(3)
        print("  hourly range index (1.0 = average of active hours): top " +
              ", ".join(f"{int(k):02d}h {v:.2f}" for k, v in top.items()) +
              " | bottom " + ", ".join(f"{int(k):02d}h {v:.2f}" for k, v in bot.items()))
    real = session_stats(o, h, l, c, groups, bph)
    nul = [session_stats(*shuffle_days(o, h, l, c, groups, s), groups, bph) for s in (1, 2)]
    ph = np.r_[real["pos_h"], real["pos_l"]]
    dr = decile_share(ph)
    dn = decile_share(np.r_[np.concatenate([x["pos_h"] for x in nul]), np.concatenate([x["pos_l"] for x in nul])])
    print(f"  session high/low position decile shares (first ... last):")
    print("    REAL " + " ".join(f"{100*x:4.1f}" for x in dr))
    print("    NULL " + " ".join(f"{100*x:4.1f}" for x in dn))
    nd = real["nd"]
    nulfhe = np.mean([x["fhe"] / x["nd"] for x in nul])
    print(f"  P(session high or low inside first hour): REAL {pct(real['fhe'], nd)} | NULL {100*nulfhe:.1f}%")
    for name, key, k in (("first 1h vs rest of session", "same1", 0), ("first 2h vs rest of session", "same2", 0),
                         ("first 1h vs whole-session direction", "d1", 0), ("first 2h vs whole-session direction", "d2", 0)):
        r = real[key]
        nn = np.mean([np.mean(x[key]) for x in nul])
        print(f"  P(same sign) {name:38s}: REAL {pct(int(np.sum(r)), len(r))} | NULL {100*nn:.1f}%")


def main():
    for inst in PREFIX:
        for tf in ("H1", "M15"):
            report(inst, tf, show_hours=True)


if __name__ == "__main__":
    main()
