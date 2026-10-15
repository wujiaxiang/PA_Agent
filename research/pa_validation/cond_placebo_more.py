"""Larger placebo calibration: BOTH the 'REAL' and the 'VOL' series are independent null draws.

Reports the empirical distribution of the combined z under a true null, so that the thresholds
(IS |z|>=3, OOS |z|>=2) can be judged against the actual false-positive rate.  No real data is
evaluated here as an effect; it only supplies the bar sizes / timestamps.
"""
from __future__ import annotations

import sys

import numpy as np

import cond_stats as cs
from common3 import surrogate_vol

SEEDS = {"H1": 8, "H2": 30, "H3": 30, "H4": 30}


def main():
    for h, n_seeds in SEEDS.items():
        spec = cs.HYP[h]
        tf = spec["primary"]
        cache = {}
        for inst in cs.INSTS:
            cache[inst] = cs.load_t(inst, tf)
        zi, zo = [], []
        for s in range(n_seeds):
            per = {}
            for inst in cs.INSTS:
                b, times = cache[inst]
                real = surrogate_vol(b, 1000 + s)
                vol = surrogate_vol(b, 2000 + s)
                per[inst] = spec["fn"](inst, tf, real, vol, times)
            a, c = cs.unit_stats(per, "IS"), cs.unit_stats(per, "OOS")
            if a and c:
                zi.append(a["z"])
                zo.append(c["z"])
            print(f"  {h} seed {s}: IS z {zi[-1]:+.2f}  OOS z {zo[-1]:+.2f}", file=sys.stderr)
        zi, zo = np.array(zi), np.array(zo)
        both = int(((abs(zi) >= cs.Z_IS) & (np.sign(zo) == np.sign(zi)) & (abs(zo) >= cs.Z_OOS)).sum())
        print(f"{h} ({tf}, {len(zi)} null draws): IS z mean {zi.mean():+.2f} std {zi.std(ddof=1):.2f}, "
              f"|z|>=3: {(abs(zi) >= 3).sum()}, |z|>=2: {(abs(zi) >= 2).sum()} | "
              f"OOS z mean {zo.mean():+.2f} std {zo.std(ddof=1):.2f}, |z|>=2: {(abs(zo) >= 2).sum()} | "
              f"would be CONFIRMED by chance: {both}")


if __name__ == "__main__":
    main()
