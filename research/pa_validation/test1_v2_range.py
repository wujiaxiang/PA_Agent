"""Test 1 v2: trading-range breakout, with strict range definitions + baselines.

What was wrong in v1:
  * "range" = trailing 20-bar envelope + flat EMA + overlap. No requirement that the
    boundaries were actually TESTED, so many "ranges" were just pullbacks.
  * wick-probe "failure" = no close beyond level within 5 bars (a 1-tick close beyond
    counted as success); same event counted repeatedly; ATR included the breakout bar.
  * only one failure definition (the project's 5-bar reclaim) was reported.

v2:
  Range strictness (window N bars before bar t, ATR taken at t-1 = causal):
    L0  loose  : width 2-12 ATR, EMA flat, mean overlap >= .35      (the v1 definition)
    L1  tested : L0-width + top AND bottom boundary each touched >= 2 times
                 (touch = extreme within 15% of height of the boundary, clusters >= 3 bars apart)
    L2  mature : L1 + >= 3 EMA20 crossings + |net displacement| < 0.4 * height
  Breakout at bar t (up; down is mirrored via invert):
    close breakout : close_t > top + 0.1 ATR
    wick probe     : high_t > top + 0.1 ATR but close_t <= top
  Outcome definitions (all reported):
    A  reclaim5  : some close in t+1..t+5 < top  (project definition of "failed")
    B  race(m)   : from t+1, first passage between  UP = top + m*H  and  DOWN = top - 0.25*H
                   (m = 0.5 and 1.0; DOWN first or same bar = failure; 80 bar horizon)
    C  surviving : no close < top in t+1..t+5, then race(1.0) from t+5 (project's "surviving -> MM")
  Baseline: the SAME detector on a bar-shuffled surrogate of the same series.
"""
from __future__ import annotations

import sys

import numpy as np

from common2 import Acc, datasets, invert, load, surrogate

HORIZON = 80
DOWN_FRAC = 0.25
LEVELS = ("L0", "L1", "L2")


def _clusters(idx: np.ndarray, gap: int = 3) -> int:
    if len(idx) == 0:
        return 0
    return 1 + int((np.diff(idx) >= gap).sum())


def _range_level(b, t: int, N: int):
    """Return (level_name or None, top, bot, H, atr) for window [t-N, t-1]."""
    atr = b.atr14[t - 1]
    if np.isnan(atr) or atr <= 0:
        return None
    wh = b.h[t - N:t]
    wl = b.l[t - N:t]
    top, bot = wh.max(), wl.min()
    H = top - bot
    if H <= 0:
        return None
    if not (2.0 <= H / atr <= 12.0):
        return None
    e0, e1 = b.ema20[t - N], b.ema20[t - 1]
    if np.isnan(e0) or np.isnan(e1) or abs(e1 - e0) / atr > 0.6:
        return None
    hi = np.minimum(b.h[t - N + 1:t], b.h[t - N:t - 1])
    lo = np.maximum(b.l[t - N + 1:t], b.l[t - N:t - 1])
    den = np.maximum(b.h[t - N + 1:t], b.h[t - N:t - 1]) - np.minimum(b.l[t - N + 1:t], b.l[t - N:t - 1])
    ok = den > 0
    if ok.sum() == 0 or np.mean(np.maximum(0, hi[ok] - lo[ok]) / den[ok]) < 0.35:
        return None
    level = "L0"
    tt = _clusters(np.where(wh >= top - 0.15 * H)[0])
    tb = _clusters(np.where(wl <= bot + 0.15 * H)[0])
    if tt >= 2 and tb >= 2:
        level = "L1"
        wc = b.c[t - N:t]
        we = b.ema20[t - N:t]
        if np.isnan(we).any():
            return level, top, bot, H, atr
        cross = int((np.diff(np.sign(wc - we)) != 0).sum())
        disp = abs(wc[-1] - wc[0])
        if cross >= 3 and disp < 0.4 * H:
            level = "L2"
    return level, top, bot, H, atr


def _race(b, start: int, top: float, H: float, m: float):
    """First passage from bar `start` on: UP=top+m*H (success) vs DOWN=top-DOWN_FRAC*H (failure)."""
    up = top + m * H
    dn = top - DOWN_FRAC * H
    n = len(b)
    for k in range(start, min(start + HORIZON, n)):
        hit_dn = b.l[k] <= dn
        hit_up = b.h[k] >= up
        if hit_dn:      # same-bar tie -> failure (conservative)
            return 0
        if hit_up:
            return 1
    return None         # unresolved -> excluded


def scan(b, N: int, acc: Acc, tag: str):
    n = len(b)
    t = N + 20
    while t < n - HORIZON - 12:
        r = _range_level(b, t, N)
        if r is None:
            t += 1
            continue
        level, top, bot, H, atr = r
        thr = 0.1 * atr
        close_bo = b.c[t] > top + thr
        wick = (not close_bo) and b.h[t] > top + thr
        if close_bo:
            fail5 = bool((b.c[t + 1:t + 6] < top).any())
            for lv in LEVELS[: LEVELS.index(level) + 1]:     # L2 events also count in L1, L0
                acc.add(f"{tag}|{lv}|close|A_reclaim5_fail", fail5)
                for m in (0.5, 1.0):
                    res = _race(b, t + 1, top, H, m)
                    if res is not None:
                        acc.add(f"{tag}|{lv}|close|B_race{m}_success", res == 1)
                if not fail5:
                    res = _race(b, t + 6, top, H, 1.0)
                    if res is not None:
                        acc.add(f"{tag}|{lv}|close|C_surviving_MM", res == 1)
            t += 10
        elif wick:
            for lv in LEVELS[: LEVELS.index(level) + 1]:
                for m in (0.5, 1.0):
                    res = _race(b, t + 1, top, H, m)
                    if res is not None:
                        acc.add(f"{tag}|{lv}|wick|B_race{m}_success", res == 1)
            t += 3
        else:
            t += 1


def run_series(b, N, acc, tag):
    scan(b, N, acc, tag + "|up")
    scan(invert(b), N, acc, tag + "|dn")


def main():
    N_list = [20, 40]
    real, surr = {N: Acc() for N in N_list}, {N: Acc() for N in N_list}
    per_file = {}
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        s = surrogate(b, 7)
        for N in N_list:
            a_r, a_s = Acc(), Acc()
            run_series(b, N, a_r, "x")
            run_series(s, N, a_s, "x")
            real[N].merge(a_r)
            surr[N].merge(a_s)
            per_file[(f, N)] = a_r
        print(f"done {f}", file=sys.stderr)

    def agg(acc: Acc, lv: str, kind: str, metric: str):
        k = n = 0
        for d in ("up", "dn"):
            a, c = acc.get(f"x|{d}|{lv}|{kind}|{metric}")
            k += a
            n += c
        return k, n

    from common import pct
    print("=" * 100)
    print("TEST 1 v2  range breakout   (race: success = reach top+m*H before returning to top-0.25H)")
    print("surrogate = same detector on bar-shuffled series (null).  Differences from the null are what matter.")
    print("=" * 100)
    for N in N_list:
        print(f"\n######## range window N = {N} bars ########")
        for lv, desc in (("L0", "loose (v1 def)"), ("L1", "boundaries each tested >=2x"), ("L2", "mature: +EMA crosses +low displacement")):
            print(f"\n--- {lv}: {desc} ---")
            for kind in ("close", "wick"):
                for metric in ("A_reclaim5_fail", "B_race0.5_success", "B_race1.0_success", "C_surviving_MM"):
                    kr, nr = agg(real[N], lv, kind, metric)
                    ks, ns = agg(surr[N], lv, kind, metric)
                    if nr == 0 and ns == 0:
                        continue
                    print(f"  {kind:5s} {metric:20s} REAL {pct(kr, nr):48s} | NULL {pct(ks, ns)}")
    print("\nREAD: 'A_reclaim5_fail' is the project's own 'failed' definition. 'B_race*_success' = follow-through.")
    print("Brooks-style '70-80% of breakout attempts fail' corresponds to  1 - success  in the race rows.")


if __name__ == "__main__":
    main()
