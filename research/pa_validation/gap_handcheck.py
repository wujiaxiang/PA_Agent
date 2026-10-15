"""Print raw OHLC for a few detected (and rejected) events so they can be verified by hand."""
from __future__ import annotations

import datetime as dt

import numpy as np

from gap_defs import detect, load_file, tf_seconds

NAME = "XAUUSD_M15"


def ts(x):
    return dt.datetime.fromtimestamp(int(x), dt.timezone.utc).strftime("%m-%d %H:%M")


def show(b, times, t, ev=None, i=None):
    for k, lab in ((t - 2, "t-2"), (t - 1, "t-1"), (t, "t  ")):
        print(f"    {lab}  {ts(times[k])}  O {b.o[k]:9.2f}  H {b.h[k]:9.2f}  L {b.l[k]:9.2f}  C {b.c[k]:9.2f}   range {b.h[k]-b.l[k]:7.2f}")
    print(f"    ATR14[t-2] = {b.atr14[t-2]:.3f}")


def main():
    b, times = load_file(NAME)
    ev = detect(b, times, tf_seconds(NAME))
    rng = np.random.default_rng(2024)
    picks = [
        ("BULL strong (G3S)", ev.strong & ev.first_g3 & (ev.d == 1)),
        ("BEAR strong (G3S)", ev.strong & ev.first_g3 & (ev.d == -1)),
        ("G3 but middle bar NOT a trend bar", (~ev.trend) & ev.first_g3),
        ("big gap >= 1 ATR", ev.first_g3 & (ev.size_atr >= 1.0)),
    ]
    for label, m in picks:
        idx = np.flatnonzero(m)
        i = int(rng.choice(idx))
        t = int(ev.t[i])
        print(f"\n[{label}]  index t={t}")
        show(b, times, t)
        if ev.d[i] == 1:
            print(f"    BULL check: l[t] - h[t-2] = {b.l[t]:.2f} - {b.h[t-2]:.2f} = {b.l[t]-b.h[t-2]:.2f}  (>0 -> gap)   zone [{ev.z_lo[i]:.2f}, {ev.z_hi[i]:.2f}]  size/ATR = {ev.size_atr[i]:.2f}")
            m_ = t - 1
            rg = b.h[m_] - b.l[m_]
            print(f"    middle bar: body ratio {(b.c[m_]-b.o[m_])/rg:+.2f} (need > 0.25 for bull trend),  close position {(b.c[m_]-b.l[m_])/rg:.2f} (need >= 0.65),  range/ATR {rg/b.atr14[t-2]:.2f} (strong needs >= 1.0)")
        else:
            print(f"    BEAR check: l[t-2] - h[t] = {b.l[t-2]:.2f} - {b.h[t]:.2f} = {b.l[t-2]-b.h[t]:.2f}  (>0 -> gap)   zone [{ev.z_lo[i]:.2f}, {ev.z_hi[i]:.2f}]  size/ATR = {ev.size_atr[i]:.2f}")
            m_ = t - 1
            rg = b.h[m_] - b.l[m_]
            print(f"    middle bar: body ratio {(b.o[m_]-b.c[m_])/rg:+.2f} (need > 0.25 for bear trend),  close position from top {(b.h[m_]-b.c[m_])/rg:.2f} (need >= 0.65),  range/ATR {rg/b.atr14[t-2]:.2f}")
        print(f"    flags: trend={bool(ev.trend[i])} strong={bool(ev.strong[i])} covers={bool(ev.covers[i])} first_in_run={bool(ev.first_g3[i])}")

    # rejected: price gap exists but crosses a time break
    step = tf_seconds(NAME)
    t_all = np.arange(2, len(b))
    gap = b.l[t_all] - b.h[t_all - 2]
    cont = (times[t_all] - times[t_all - 1] <= 1.5 * step) & (times[t_all - 1] - times[t_all - 2] <= 1.5 * step)
    rej = t_all[(gap > 0) & ~cont]
    for t in rng.choice(rej, size=2, replace=False):
        t = int(t)
        print(f"\n[REJECTED: price gap across a time break]  index t={t}")
        show(b, times, t)
        print(f"    l[t]-h[t-2] = {b.l[t]-b.h[t-2]:.2f} > 0, but time gaps are {int(times[t-1]-times[t-2])//60} min and {int(times[t]-times[t-1])//60} min (nominal 15)")


if __name__ == "__main__":
    main()
