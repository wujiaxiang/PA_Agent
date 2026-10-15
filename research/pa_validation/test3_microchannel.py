"""Validation Test 3: Micro-channel first-breakout failure rate.

Prompt claim (市场诊断框架 状态2 / 二元决策 §5.1 / 文件13):
  "微型通道中的第一个突破尝试失败率>80%" and "第一个突破失败后，顺势入场胜率更高".

Definition of micro-channel (文件13 / 状态2): 2-10 bars, almost no pullback,
each bar's high/low advances in one direction. We detect a bull micro-channel as
a run of >=4 bars where each bar's LOW >= prev low (no lower low) and makes higher
highs; symmetric for bear. The "first breakout attempt" against the channel =
first bar after the run that trades beyond the channel's counter-trend boundary
(a bull micro-channel breaks DOWN through the rising-low line). We test whether
that first counter-trend break FAILS (price resumes original direction).
"""
from __future__ import annotations

import numpy as np
from common import INSTRUMENTS, load, pct

MIN_LEN = 4
MAX_LEN = 10
RESUME_HORIZON = 10


# Genuine Brooks micro-channel: tight, nearly every bar closes with-trend, each bar's
# low holds above prior low (bull). Require majority bull closes and small overlap.
def _mostly_up(b, s, e):
    ups = sum(1 for k in range(s, e + 1) if b.c[k] > b.o[k])
    return ups >= 0.75 * (e - s + 1)


def _mostly_down(b, s, e):
    dns = sum(1 for k in range(s, e + 1) if b.c[k] < b.o[k])
    return dns >= 0.75 * (e - s + 1)


def detect_bull_micro(b):
    """Yield (start, end) of bull micro-channels (higher highs, no lower lows)."""
    n = len(b)
    out = []
    i = 1
    while i < n - 1:
        j = i
        while (j + 1 < n and b.h[j + 1] > b.h[j] and b.l[j + 1] >= b.l[j] - 1e-9):
            j += 1
        length = j - i + 1
        if MIN_LEN <= length <= MAX_LEN and _mostly_up(b, i, j):
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def detect_bear_micro(b):
    n = len(b)
    out = []
    i = 1
    while i < n - 1:
        j = i
        while (j + 1 < n and b.l[j + 1] < b.l[j] and b.h[j + 1] <= b.h[j] + 1e-9):
            j += 1
        length = j - i + 1
        if MIN_LEN <= length <= MAX_LEN and _mostly_down(b, i, j):
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def run_symbol(name):
    b = load(name)
    n = len(b)
    bull = detect_bull_micro(b)
    bear = detect_bear_micro(b)

    first_break = 0
    first_break_failed = 0

    # Bull micro-channel: first counter-trend attempt = first bar after channel end
    # that trades below the previous bar's low (breaks the rising-low line).
    for (s, e) in bull:
        chan_high_end = b.h[e]
        brk = None
        for k in range(e + 1, min(e + 1 + RESUME_HORIZON, n)):
            if b.l[k] < b.l[k - 1]:
                brk = k
                break
        if brk is None:
            continue
        first_break += 1
        # FAILED (Brooks sense) = the counter-trend break does NOT follow through:
        #   price trades back above the break bar's high within horizon (trend resumes),
        #   BEFORE extending the breakdown by one more bar's worth below break low.
        brk_low = b.l[brk]
        brk_high = b.h[brk]
        resumed = False
        extended = False
        for k in range(brk + 1, min(brk + 1 + RESUME_HORIZON, n)):
            if b.h[k] > brk_high:
                resumed = True
                break
            if b.l[k] < brk_low:
                extended = True
                break
        first_break_failed += int(resumed and not extended)

    for (s, e) in bear:
        chan_low_end = b.l[e]
        brk = None
        for k in range(e + 1, min(e + 1 + RESUME_HORIZON, n)):
            if b.h[k] > b.h[k - 1]:
                brk = k
                break
        if brk is None:
            continue
        first_break += 1
        brk_low = b.l[brk]
        brk_high = b.h[brk]
        resumed = False
        extended = False
        for k in range(brk + 1, min(brk + 1 + RESUME_HORIZON, n)):
            if b.l[k] < brk_low:
                resumed = True
                break
            if b.h[k] > brk_high:
                extended = True
                break
        first_break_failed += int(resumed and not extended)

    return name, first_break, first_break_failed, len(bull) + len(bear)


def main():
    print("=" * 90)
    print("TEST 3: Micro-channel FIRST counter-trend breakout failure rate (claim >80%)")
    print(f"  micro def: run of {MIN_LEN}-{MAX_LEN} bars, higher highs & no lower lows (bull) / symmetric")
    print(f"  break = close beyond channel origin boundary within {RESUME_HORIZON} bars")
    print(f"  failed = original direction resumes (new extreme beyond channel end) within {RESUME_HORIZON} bars")
    print("=" * 90)
    tot_break = 0
    tot_fail = 0
    for inst, files in INSTRUMENTS.items():
        print(f"\n### {inst}")
        for f in files:
            try:
                name, fb, fbf, nch = run_symbol(f)
            except FileNotFoundError:
                continue
            print(f"  {f:18s} channels={nch} first-breaks={fb}")
            print(f"      first-break failure: {pct(fbf, fb)}")
            tot_break += fb
            tot_fail += fbf
    print("\n" + "=" * 90)
    print(f"AGGREGATE first micro-channel breakout failure (claim >80%): {pct(tot_fail, tot_break)}")
    print("=" * 90)


if __name__ == "__main__":
    main()
