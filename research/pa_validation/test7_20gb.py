"""Validation Test 7: 20GB (20-bar gap bar) -> first EMA touch is with-trend.

Prompt claim (二元决策 §2.4 / 文件16 / 文件20):
  "连续约20根K线未触及EMA (20GB)，表示趋势极强。首次触及EMA是顺势刮头皮机会
   (非反转信号)。两次尝试失败则放弃。"

Method: find runs of >= 20 consecutive bars entirely on one side of EMA20
  (bull: low > EMA for 20+ bars). The first bar that touches EMA (low <= EMA <= high,
  or crosses) ends the run. Test whether, after first touch, the trend RESUMES
  (makes a new with-trend extreme beyond the pre-touch extreme) vs REVERSES
  (closes decisively through EMA to the other side and makes opposite extreme)
  within HORIZON bars.
"""
from __future__ import annotations

import numpy as np
from common import INSTRUMENTS, load, pct

MIN_GAP = 20
HORIZON = 20


def run_symbol(name):
    b = load(name)
    n = len(b)
    resumed = 0
    reversed_ = 0
    neither = 0
    total = 0

    i = 25
    while i < n - HORIZON - 1:
        ema = b.ema20[i]
        if np.isnan(ema):
            i += 1
            continue
        # bull gap: low > ema
        side = 0
        if b.l[i] > ema:
            side = 1
        elif b.h[i] < ema:
            side = -1
        if side == 0:
            i += 1
            continue
        # extend run while same side
        j = i
        while j + 1 < n:
            e = b.ema20[j + 1]
            if np.isnan(e):
                break
            if side == 1 and b.l[j + 1] > e:
                j += 1
            elif side == -1 and b.h[j + 1] < e:
                j += 1
            else:
                break
        run_len = j - i + 1
        if run_len >= MIN_GAP:
            # first touch = bar j+1
            touch = j + 1
            if touch >= n:
                break
            total += 1
            atr = b.atr14[touch] if not np.isnan(b.atr14[touch]) else None
            if atr is None or atr <= 0:
                i = touch + 1
                total -= 1
                continue
            # Actionable test: a with-trend bounce scalp off EMA.
            # Entry at touch bar's with-trend extreme + 1 tick (resume trigger);
            # target = +1*ATR with-trend; stop = 1*ATR counter-trend (reversal).
            # "resumed" = target hit first; "reversed" = stop hit first.
            if side == 1:
                entry = b.h[touch]
                target = entry + atr
                stop = b.l[touch] - 0.25 * atr
            else:
                entry = b.l[touch]
                target = entry - atr
                stop = b.h[touch] + 0.25 * atr
            outcome = "neither"
            for k in range(touch + 1, min(touch + 1 + HORIZON, n)):
                if side == 1:
                    hit_t = b.h[k] >= target
                    hit_s = b.l[k] <= stop
                else:
                    hit_t = b.l[k] <= target
                    hit_s = b.h[k] >= stop
                if hit_s and hit_t:
                    outcome = "reversed"; break
                if hit_t:
                    outcome = "resumed"; break
                if hit_s:
                    outcome = "reversed"; break
            if outcome == "resumed":
                resumed += 1
            elif outcome == "reversed":
                reversed_ += 1
            else:
                neither += 1
            i = touch + 1
        else:
            i = j + 1
    return name, total, resumed, reversed_, neither


def main():
    print("=" * 90)
    print(f"TEST 7: 20GB first EMA touch outcome (claim: with-trend resume, not reversal)")
    print(f"  gap run >= {MIN_GAP} bars one side of EMA20; outcome horizon {HORIZON}")
    print("=" * 90)
    T = R = V = X = 0
    for inst, files in INSTRUMENTS.items():
        print(f"\n### {inst}")
        for f in files:
            try:
                name, t, r, v, x = run_symbol(f)
            except FileNotFoundError:
                continue
            if t == 0:
                print(f"  {f:18s} (no 20GB runs)")
                continue
            print(f"  {f:18s} 20GB touches={t} | resume {pct(r,t)} | reverse {pct(v,t)}")
            T += t; R += r; V += v; X += x
    print("\n" + "=" * 90)
    print("AGGREGATE 20GB first-touch outcome:")
    print(f"  resumed (with-trend): {pct(R, T)}")
    print(f"  reversed:             {pct(V, T)}")
    print(f"  neither (chop):       {pct(X, T)}")
    print("=" * 90)


if __name__ == "__main__":
    main()
