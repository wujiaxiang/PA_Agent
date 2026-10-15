"""Test 4 v2: H1 / H2 / H3 inside a with-trend pullback, expectancy in R.

What was wrong in v1:
  * "H2" was just the second up-break after H1, not "H1 failed (new lower leg) then second try".
  * entry was on a break of the TRIGGER bar (one bar late); win/loss on a fixed 1R target with
    unresolved trades counted as losses, and no control group, so ~44% for everything only said
    "a 1R race with a tick-sized friction is below 50%".

v2 (all causal):
  swing high   : fractal (2 bars each side), known 2 bars later. A pullback state starts there.
  H trigger    : in the pullback (before the swing high is exceeded, <= 30 bars), a bar k whose high
                 exceeds high[k-1] (signal bar = k-1).  Entry = high[k-1] + 1 tick (must be touched
                 by bar k).  Stop = low[k-1] - 1 tick.  Risk must be in [0.2, 3] ATR.
  counting     : after a trigger the counter is "disarmed"; it re-arms only when a later bar trades
                 below the previous signal bar's low (= that H would have been stopped out, i.e. a NEW
                 down-leg).  The n-th trigger is H_n.
  context      : EMA20 rising over the last 10 bars (known at k-1).  Bucketed by slope in ATR units.
  outcome      : entry bar is conservative (if its low <= stop -> loss).  Afterwards stop is checked
                 before target within each bar.  Target T x risk with T in {1, 2}; unresolved after 30
                 bars -> marked to market at close.  Metric = mean R (expectancy), not win%.
  controls     : CTRL_any = every 5th bar in the same EMA-rising context with the same entry/stop rule.
                 NULL      = the same code on a bar-shuffled surrogate.
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from common2 import datasets, invert, load, mean_ci, surrogate, tick_of

HZ = 30


def sim(b, k, entry, stop, risk, T):
    n = len(b)
    if b.l[k] <= stop:
        return -1.0
    tgt = entry + T * risk
    end = min(k + HZ, n - 1)
    for j in range(k + 1, end + 1):
        if b.l[j] <= stop:
            return -1.0
        if b.h[j] >= tgt:
            return float(T)
    return float((b.c[end] - entry) / risk)


def bucket(slope_atr: float) -> str:
    if slope_atr <= 0.25:
        return "weak(<=0.25)"
    if slope_atr <= 1.0:
        return "mid(0.25-1)"
    return "strong(>1)"


def scan(b, tick, out, src):
    n = len(b)
    state = None
    for k in range(35, n - HZ - 2):
        s = k - 2
        if state is None and b.h[s] > b.h[s - 1] and b.h[s] > b.h[s - 2] and b.h[s] > b.h[s + 1] and b.h[s] > b.h[k]:
            state = {"s": s, "sh": b.h[s], "count": 0, "armed": True, "sig_low": None}
        atr = b.atr14[k - 1]
        e_now, e_old = b.ema20[k - 1], b.ema20[k - 11]
        if np.isnan(atr) or atr <= 0 or np.isnan(e_now) or np.isnan(e_old):
            continue
        slope = (e_now - e_old) / atr
        rising = slope > 0

        # control: any bar, same entry/stop rule, every 5th bar
        if rising and k % 5 == 0 and b.h[k] >= b.h[k - 1] + tick:
            entry = b.h[k - 1] + tick
            stop = b.l[k - 1] - tick
            risk = entry - stop
            if 0.2 * atr <= risk <= 3 * atr:
                for T in (1, 2):
                    out[f"{src}|CTRL_any|{bucket(slope)}|T{T}"].append(sim(b, k, entry, stop, risk, T))
                    out[f"{src}|CTRL_any|ALL|T{T}"].append(out[f"{src}|CTRL_any|{bucket(slope)}|T{T}"][-1])

        if state is None:
            continue
        if k > state["s"] + 2 and b.h[k] > state["sh"]:
            state = None
            continue
        if k - state["s"] > 30:
            state = None
            continue
        if not state["armed"] and state["sig_low"] is not None and b.l[k] < state["sig_low"]:
            state["armed"] = True
        if state["armed"] and k > state["s"] + 2 and b.h[k] > b.h[k - 1] and rising:
            entry = b.h[k - 1] + tick
            if b.h[k] >= entry:
                stop = b.l[k - 1] - tick
                risk = entry - stop
                if 0.2 * atr <= risk <= 3 * atr:
                    state["count"] += 1
                    lab = f"H{min(state['count'], 3)}"
                    for T in (1, 2):
                        r = sim(b, k, entry, stop, risk, T)
                        out[f"{src}|{lab}|{bucket(slope)}|T{T}"].append(r)
                        out[f"{src}|{lab}|ALL|T{T}"].append(r)
                    state["armed"] = False
                    state["sig_low"] = b.l[k - 1]


def main():
    out = defaultdict(list)
    for inst, f in datasets():
        try:
            b = load(f)
        except FileNotFoundError:
            continue
        tk = tick_of(b)
        s = surrogate(b, 21)
        for series, src in ((b, "REAL"), (s, "NULL")):
            scan(series, tk, out, src)
            scan(invert(series), tk, out, src)
        print("done", f, file=sys.stderr)

    def row(src, lab, bk, T):
        x = np.array(out.get(f"{src}|{lab}|{bk}|T{T}", []))
        if len(x) == 0:
            return "n=0"
        m, ci = mean_ci(x)
        wr = float((x > 0).mean())
        return f"n={len(x):6d}  E[R]={m:+.3f} ±{ci:.3f}  win={100*wr:4.1f}%"

    print("=" * 110)
    print("TEST 4 v2  with-trend pullback: H1 / H2 / H3+ expectancy (R multiples, stop = signal bar low - 1 tick)")
    print("=" * 110)
    for T in (1, 2):
        print(f"\n######## target = {T}R ########")
        for bk in ("ALL", "weak(<=0.25)", "mid(0.25-1)", "strong(>1)"):
            print(f"\n  EMA20 slope bucket: {bk}")
            for lab in ("H1", "H2", "H3", "CTRL_any"):
                print(f"    {lab:9s} REAL {row('REAL', lab, bk, T):62s} | NULL {row('NULL', lab, bk, T)}")
    print("\nREAD: compare REAL H2 vs REAL H1 (is the 2nd try better?), and each vs CTRL_any / NULL.")


if __name__ == "__main__":
    main()
