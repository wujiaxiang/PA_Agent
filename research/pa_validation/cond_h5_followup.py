"""EXPLORATORY follow-up for H5 (added AFTER seeing the pre-registered result; not part of the verdict).

Question: is the first-decile deficit of session extremes just the intraday volatility profile
(quiet Asian hours) restated?  The pre-registered null shuffles bars inside each day, which destroys
that profile.  Here we compare REAL against
   (a) day-shuffle NULL  (pre-registered)
   (b) VOL null          (random bar direction, bar SIZES stay at their time of day)
If VOL reproduces the deficit, H5 is explained by volatility seasonality and is not an independent fact.
"""
from __future__ import annotations

import numpy as np

import reg7_time as r7
from common2 import make_bars
from common3 import surrogate_vol
from cond_stats import PREFIX, INSTS


def first_decile_share(o, h, l, c, groups, bph):
    st = r7.session_stats(o, h, l, c, groups, bph)
    pos = np.r_[st["pos_h"], st["pos_l"]]
    return float((pos < 0.1).mean()), float((pos >= 0.9).mean()), len(pos)


def main():
    print("EXPLORATORY (post-hoc) -- shares of session highs/lows in the first and last decile of the server day (H1 bars)")
    print(f"{'inst':8s} {'half':4s} | {'REAL first':>10s} {'REAL last':>9s} | {'day-shuffle first':>17s} {'last':>6s} | {'VOL-null first':>14s} {'last':>6s}")
    for inst in INSTS:
        df = r7.load_df(PREFIX[inst], "H1")
        t = df.time.to_numpy()
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        groups = r7.day_groups(t, r7.session_start_hour(t))
        G = len(groups)
        b = make_bars("x", o, h, l, c)
        vols = [surrogate_vol(b, s) for s in (11, 12, 13)]
        for half, gs in (("IS", groups[: G // 2]), ("OOS", groups[G // 2:])):
            rf, rl, _ = first_decile_share(o, h, l, c, gs, 1)
            dsf = dsl = 0.0
            for s in (1, 2):
                f, la, _ = first_decile_share(*r7.shuffle_days(o, h, l, c, gs, s), gs, 1)
                dsf += f / 2
                dsl += la / 2
            vf = vl = 0.0
            for v in vols:
                f, la, _ = first_decile_share(v.o, v.h, v.l, v.c, gs, 1)
                vf += f / len(vols)
                vl += la / len(vols)
            print(f"{inst:8s} {half:4s} | {100*rf:9.1f}% {100*rl:8.1f}% | {100*dsf:16.1f}% {100*dsl:5.1f}% | {100*vf:13.1f}% {100*vl:5.1f}%")
    print("\nIf the VOL-null column is close to REAL (and far below day-shuffle) the deficit is explained by the intraday")
    print("volatility profile alone.")


if __name__ == "__main__":
    main()
