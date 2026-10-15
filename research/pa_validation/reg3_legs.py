"""reg3: swing-leg structure (zigzag with threshold k*ATR): leg size/duration, retracement ratios
(Fibonacci peaks), equal-leg (measured-move) peak, double tops/bottoms, level revisits and the
number of counter-legs inside a pullback.
"""
from __future__ import annotations

import numpy as np

from common3 import compare, table

K_MAIN = 1.5
K_PB = 2.0      # pullback-complexity analysis uses larger legs
K_SUB = 0.7


def zigzag(b, k):
    h, l, atr = b.h, b.l, b.atr14
    n = len(h)
    valid = ~np.isnan(atr)
    if not valid.any():
        return np.array([]), np.array([]), np.array([])
    start = int(np.argmax(valid))
    idx, pr, kd = [], [], []
    dirn = 0
    hi, hi_i, lo, lo_i = h[start], start, l[start], start
    for i in range(start + 1, n):
        a = atr[i]
        if dirn == 0:
            if h[i] > hi:
                hi, hi_i = h[i], i
            if l[i] < lo:
                lo, lo_i = l[i], i
            if hi - lo >= k * a:
                if hi_i > lo_i:
                    idx.append(lo_i); pr.append(lo); kd.append(-1); dirn = 1
                else:
                    idx.append(hi_i); pr.append(hi); kd.append(1); dirn = -1
        elif dirn == 1:
            if h[i] > hi:
                hi, hi_i = h[i], i
            elif hi - l[i] >= k * a:
                idx.append(hi_i); pr.append(hi); kd.append(1)
                dirn = -1; lo, lo_i = l[i], i
        else:
            if l[i] < lo:
                lo, lo_i = l[i], i
            elif h[i] - lo >= k * a:
                idx.append(lo_i); pr.append(lo); kd.append(-1)
                dirn = 1; hi, hi_i = h[i], i
    return np.array(idx), np.array(pr), np.array(kd)


def peak(x, c, w=0.015, f1=0.045, f2=0.075):
    inw = int(((x >= c - w) & (x <= c + w)).sum())
    lft = int(((x >= c - f2) & (x <= c - f1)).sum())
    rgt = int(((x >= c + f1) & (x <= c + f2)).sum())
    fl = (lft + rgt) / 2
    return (inw / fl if fl > 0 else float("nan"), inw + lft + rgt)


def count_counter_legs(b, s, e, direction, thr):
    """Number of rebounds >= thr against a leg (direction=-1 down leg, +1 up leg) between bars s..e."""
    cnt = 0
    if direction < 0:
        lo = b.l[s]
        armed = True
        for i in range(s + 1, e + 1):
            if b.l[i] < lo:
                lo = b.l[i]; armed = True
            elif armed and b.h[i] - lo >= thr:
                cnt += 1; armed = False
    else:
        hi = b.h[s]
        armed = True
        for i in range(s + 1, e + 1):
            if b.h[i] > hi:
                hi = b.h[i]; armed = True
            elif armed and hi - b.l[i] >= thr:
                cnt += 1; armed = False
    return cnt


def stats(b):
    out = {}
    idx, pr, kd = zigzag(b, K_MAIN)
    if len(idx) < 40:
        return out
    size = np.abs(np.diff(pr))
    a_piv = b.atr14[idx[:-1]]
    size_atr = size / a_piv
    dur = np.diff(idx)
    out["leg_size_med_atr"] = (float(np.median(size_atr)), len(size))
    out["leg_dur_med_bars"] = (float(np.median(dur)), len(size))
    out["leg_size_p90_atr"] = (float(np.percentile(size_atr, 90)), len(size))

    r = size[1:] / size[:-1]
    out["retr_med"] = (float(np.median(r)), len(r))
    out["P(retr<0.5)"] = (float((r < 0.5).mean()), len(r))
    out["P(retr>1.0)"] = (float((r > 1.0).mean()), len(r))
    for c in (0.382, 0.5, 0.618, 0.786):
        out[f"fib_peak_{c}"] = peak(r, c)
    for c in (0.44, 0.56, 0.70):             # control positions with no Fibonacci meaning
        out[f"ctrl_peak_{c}"] = peak(r, c)
    s2 = size[2:] / size[:-2]
    out["eqleg_peak_1.0"] = peak(s2, 1.0)
    out["eqleg_peak_2.0"] = peak(s2, 2.0, w=0.03, f1=0.09, f2=0.15)
    out["ctrl_peak_1.3"] = peak(s2, 1.3)

    # double tops / bottoms: same-kind pivots two apart
    for j in (0, 1):
        pass
    p_same = np.abs(pr[2:] - pr[:-2])
    a_same = b.atr14[idx[:-2]]
    d_atr = p_same / a_same
    out["P(|P_j+2 - P_j|<0.25atr)"] = (float((d_atr < 0.25).mean()), len(d_atr))
    out["P(|P_j+2 - P_j|<0.5atr)"] = (float((d_atr < 0.5).mean()), len(d_atr))
    out["P(|P_j+2 - P_j|<1.0atr)"] = (float((d_atr < 1.0).mean()), len(d_atr))

    # revisit of a pivot level within 60 bars after the NEXT pivot (the pullback extreme)
    n = len(b.h)
    rev = hold = tot = 0
    for j in range(len(idx) - 1):
        i0, nxt = idx[j], idx[j + 1]
        a = b.atr14[i0]
        if np.isnan(a) or a <= 0 or nxt + 60 >= n:
            continue
        tot += 1
        if kd[j] == 1:       # pivot high P; revisit = a later high within 0.25 atr below it
            P = pr[j]
            seg = b.h[nxt + 1: nxt + 61]
            touched = seg >= P - 0.25 * a
            if touched.any():
                rev += 1
                if seg.max() <= P + 0.5 * a:
                    hold += 1
        else:
            P = pr[j]
            seg = b.l[nxt + 1: nxt + 61]
            touched = seg <= P + 0.25 * a
            if touched.any():
                rev += 1
                if seg.min() >= P - 0.5 * a:
                    hold += 1
    if tot > 30:
        out["P(revisit level<=60 bars)"] = (rev / tot, tot)
        out["P(revisit holds | revisit)"] = (hold / rev if rev else float("nan"), rev)

    # counter-legs inside a leg (pullback complexity), legs 2.5-5 atr
    idx2, pr2, kd2 = zigzag(b, K_PB)
    if len(idx2) >= 30:
        cnts = []
        for j in range(len(idx2) - 1):
            a = b.atr14[idx2[j]]
            sz = abs(pr2[j + 1] - pr2[j]) / a
            if 2.5 <= sz <= 5.0:
                direction = -1 if kd2[j] == 1 else 1
                cnts.append(count_counter_legs(b, idx2[j], idx2[j + 1], direction, K_SUB * a))
        cnts = np.array(cnts)
        if len(cnts) > 30:
            out["P(0 counter-legs)"] = (float((cnts == 0).mean()), len(cnts))
            out["P(1 counter-leg)"] = (float((cnts == 1).mean()), len(cnts))
            out["P(>=2 counter-legs)"] = (float((cnts >= 2).mean()), len(cnts))
    return out


if __name__ == "__main__":
    print(f"reg3  swing legs (zigzag {K_MAIN} ATR)   peak = density in +-0.015 window / density in flanking windows")
    table(compare(stats))
