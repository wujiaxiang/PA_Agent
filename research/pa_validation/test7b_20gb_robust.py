"""Test 7b robustness: after 20GB first EMA touch, where is price N bars later?

Simplest possible, trigger-free check:
  At first EMA touch after a >=20-bar one-sided run, measure the close N bars later
  relative to EMA at touch. Did it stay/resume on the trend side (with-trend) or
  flip to the other side (mean-revert/reverse)?
Also measure max with-trend excursion vs max counter excursion (in ATR) over N bars.
"""
from __future__ import annotations

import numpy as np
from common import INSTRUMENTS, load, pct

MIN_GAP = 20
N = 10


def run_symbol(name):
    b = load(name)
    n = len(b)
    stay = 0      # close N bars later still on trend side of EMA(touch)
    flip = 0
    total = 0
    with_exc = []
    cnt_exc = []
    i = 25
    while i < n - N - 1:
        ema = b.ema20[i]
        if np.isnan(ema):
            i += 1; continue
        side = 1 if b.l[i] > ema else (-1 if b.h[i] < ema else 0)
        if side == 0:
            i += 1; continue
        j = i
        while j + 1 < n:
            e = b.ema20[j + 1]
            if np.isnan(e): break
            if side == 1 and b.l[j + 1] > e: j += 1
            elif side == -1 and b.h[j + 1] < e: j += 1
            else: break
        if j - i + 1 >= MIN_GAP:
            touch = j + 1
            if touch + N >= n: break
            total += 1
            e_touch = b.ema20[touch]
            atr = b.atr14[touch]
            future_c = b.c[touch + N]
            if side == 1:
                if future_c >= e_touch: stay += 1
                else: flip += 1
                if atr and atr > 0:
                    with_exc.append((b.h[touch:touch + N + 1].max() - b.c[touch]) / atr)
                    cnt_exc.append((b.c[touch] - b.l[touch:touch + N + 1].min()) / atr)
            else:
                if future_c <= e_touch: stay += 1
                else: flip += 1
                if atr and atr > 0:
                    with_exc.append((b.c[touch] - b.l[touch:touch + N + 1].min()) / atr)
                    cnt_exc.append((b.h[touch:touch + N + 1].max() - b.c[touch]) / atr)
            i = touch + 1
        else:
            i = j + 1
    return name, total, stay, flip, with_exc, cnt_exc


def main():
    print("=" * 90)
    print(f"TEST 7b: 20GB first-touch, close {N} bars later on trend side? + mean excursions")
    print("=" * 90)
    T = S = F = 0
    allw = []; allc = []
    for inst, files in INSTRUMENTS.items():
        for f in files:
            try:
                name, t, s, fl, w, c = run_symbol(f)
            except FileNotFoundError:
                continue
            T += t; S += s; F += fl; allw += w; allc += c
    print(f"  close on trend side {N} bars later: {pct(S, T)}")
    print(f"  flipped to other side:               {pct(F, T)}")
    print(f"  mean with-trend excursion:  {np.mean(allw):.2f} x ATR")
    print(f"  mean counter-trend excursion: {np.mean(allc):.2f} x ATR")
    print("=" * 90)


if __name__ == "__main__":
    main()
