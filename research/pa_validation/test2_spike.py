"""Validation Test 2: Spike aftermath distribution.

Prompt claims (提示词大纲 / 市场诊断框架 状态1 / 文件23):
  - After a spike, 3 outcomes: channel/flag continuation (most common),
    enter trading range (common), sticky reversal (less common).
  - "粘性反转 (收盘打穿尖峰起点且仍站在错边) 约 15-20% 量级 (极强尖峰可更低)"
  - Old "60% channel / 30% range / 10% reversal" called a teaching approximation.
  - "深度回撤 != 反转"

Method: detect spikes using the SAME run definition as market_features._spike_aftermath_hint
  (body>=0.5*ATR to start a run, >=0.35*ATR to continue, run>=3 same-direction bars).
Then classify the aftermath over the next AFTER bars using the SAME hint logic
  (continued / pullback / origin_break_risk / sticky_reversal_risk), AND independently
  measure the "ground truth" outcome HORIZON bars later:
    reversal  = close crosses spike ORIGIN and stays on wrong side at horizon
    continued = price makes a new spike-direction extreme beyond spike end
    range     = neither (chops between origin and spike end)
"""
from __future__ import annotations

import numpy as np
from common import INSTRUMENTS, load, pct

MIN_RUN = 3
START_BODY = 0.5
CONT_BODY = 0.35
AFTER = 4           # bars examined for the "hint" label (matches code's after[:4])
HORIZON = 20        # ground-truth outcome horizon


def detect_spikes(b):
    """Yield (end_idx, direction, origin, spike_end_close, move) chronologically.
    Mirrors _spike_aftermath_hint run scan but across the whole series."""
    n = len(b)
    spikes = []
    i = 1  # need prev close for nothing here; use ATR at i
    while i < n:
        atr = b.atr14[i]
        if np.isnan(atr) or atr <= 0:
            i += 1
            continue
        o, c = b.o[i], b.c[i]
        body = abs(c - o)
        d = 1 if c > o else (-1 if c < o else 0)
        if d == 0 or body < START_BODY * atr:
            i += 1
            continue
        run = [i]
        j = i + 1
        while j < n:
            aj = b.atr14[j]
            if np.isnan(aj) or aj <= 0:
                break
            dj = 1 if b.c[j] > b.o[j] else (-1 if b.c[j] < b.o[j] else 0)
            if dj != d or abs(b.c[j] - b.o[j]) < CONT_BODY * aj:
                break
            run.append(j)
            j += 1
        if len(run) >= MIN_RUN:
            s0, s1 = run[0], run[-1]
            if d > 0:
                origin = b.l[s0]
                spike_end = b.c[s1]
                move = max(spike_end - b.o[s0], atr)
            else:
                origin = b.h[s0]
                spike_end = b.c[s1]
                move = max(b.o[s0] - spike_end, atr)
            spikes.append((s1, d, origin, spike_end, move))
            i = j if j > i + 1 else i + 1
        else:
            i = j if j > i + 1 else i + 1
    return spikes


def ground_truth(b, end_idx, d, origin, spike_end, move):
    """Outcome HORIZON bars after spike end."""
    n = len(b)
    hi = min(end_idx + HORIZON, n - 1)
    if hi <= end_idx:
        return None
    seg_c = b.c[end_idx + 1:hi + 1]
    seg_h = b.h[end_idx + 1:hi + 1]
    seg_l = b.l[end_idx + 1:hi + 1]
    if len(seg_c) == 0:
        return None
    last_c = seg_c[-1]
    if d > 0:
        new_extreme = seg_h.max() > spike_end + 0.25 * move
        origin_broken_close = (seg_c < origin).any()
        still_wrong = last_c < origin
    else:
        new_extreme = seg_l.min() < spike_end - 0.25 * move
        origin_broken_close = (seg_c > origin).any()
        still_wrong = last_c > origin
    if origin_broken_close and still_wrong:
        return "reversal"
    if new_extreme:
        return "continued"
    return "range"


def run_symbol(name):
    b = load(name)
    spikes = detect_spikes(b)
    counts = {"continued": 0, "range": 0, "reversal": 0}
    deep_pullback_total = 0
    deep_pullback_reversal = 0
    for (end_idx, d, origin, spike_end, move) in spikes:
        gt = ground_truth(b, end_idx, d, origin, spike_end, move)
        if gt is None:
            continue
        counts[gt] += 1
        # deep pullback test: did price retrace >= 50% of spike move but then?
        n = len(b)
        hi = min(end_idx + HORIZON, n - 1)
        if d > 0:
            mae = max(0.0, spike_end - b.l[end_idx + 1:hi + 1].min()) if hi > end_idx else 0
        else:
            mae = max(0.0, b.h[end_idx + 1:hi + 1].max() - spike_end) if hi > end_idx else 0
        if mae >= 0.5 * move:
            deep_pullback_total += 1
            if gt == "reversal":
                deep_pullback_reversal += 1
    total = sum(counts.values())
    return name, counts, total, deep_pullback_total, deep_pullback_reversal


def main():
    print("=" * 90)
    print("TEST 2: Spike aftermath distribution (continued / range / reversal)")
    print(f"  spike def: run>={MIN_RUN} same-dir, start body>={START_BODY}xATR, cont>={CONT_BODY}xATR")
    print(f"  outcome horizon = {HORIZON} bars; reversal = close through origin & wrong side at horizon")
    print("=" * 90)
    agg = {"continued": 0, "range": 0, "reversal": 0}
    agg_dp_total = 0
    agg_dp_rev = 0
    for inst, files in INSTRUMENTS.items():
        print(f"\n### {inst}")
        for f in files:
            try:
                name, counts, total, dpt, dpr = run_symbol(f)
            except FileNotFoundError:
                continue
            if total == 0:
                continue
            print(f"  {f:18s} spikes={total}")
            print(f"      continued: {pct(counts['continued'], total)}")
            print(f"      range:     {pct(counts['range'], total)}")
            print(f"      reversal:  {pct(counts['reversal'], total)}")
            print(f"      deep(>=50%) pullback -> reversal: {pct(dpr, dpt)}")
            for k in agg:
                agg[k] += counts[k]
            agg_dp_total += dpt
            agg_dp_rev += dpr
    total = sum(agg.values())
    print("\n" + "=" * 90)
    print("AGGREGATE (all 3 instruments, all timeframes):")
    print(f"  continued: {pct(agg['continued'], total)}")
    print(f"  range:     {pct(agg['range'], total)}")
    print(f"  reversal (claim 15-20%): {pct(agg['reversal'], total)}")
    print(f"  deep(>=50%) pullback -> actually reversal (claim: deep pullback != reversal): {pct(agg_dp_rev, agg_dp_total)}")
    print("=" * 90)


if __name__ == "__main__":
    main()
