"""Validation Test 1: Range breakout failure rate + surviving-breakout MM hit rate.

Prompt claims (提示词大纲 / 市场诊断框架 / 文件23):
  A) Range breakout attempts: "影线试探/未站稳收盘 多数失败 (约70-80%量级)"
  B) Surviving breakout (收盘站稳且未快速收回): "约60%可达区间高度 Measured Move"

Method (sliding window, mirrors market_features range-envelope + breakout ladder):
  - At each bar t, define range over trailing LOOKBACK bars [t-LOOKBACK, t-1].
  - Require the window to be a genuine "range" (not already trending):
      range_width within [2, 12] * ATR, and overlap-ish (swing structure mixed).
  - A breakout EVENT at bar t = close_t beyond envelope +/- tick.
  - Classify forward:
      failed    = price closes back inside the range within RECLAIM bars
      surviving = stays beyond for >= SURVIVE bars without closing back inside
  - For surviving up-breakouts: did price reach range_high + range_height
    (the range-height measured move) within MM_HORIZON bars before closing
    back below range_high? (symmetric for down).
"""
from __future__ import annotations

import numpy as np
from common import INSTRUMENTS, load, pct

LOOKBACK = 20
RECLAIM = 5            # matches _BREAKOUT_RECLAIM_BARS
SURVIVE = 5            # matches _SURVIVING_MIN_BARS
MM_HORIZON = 40
MIN_WIDTH_ATR = 2.0
MAX_WIDTH_ATR = 12.0

# Strict "genuine trading range" context filter (Brooks 70-80% claim applies
# ONLY to real ranges, not to breakouts of already-trending structure):
#   - EMA20 roughly flat over the window (|slope| small relative to ATR)
#   - high mean bar-to-bar overlap (choppy / overlapping bars)
STRICT_RANGE = True
EMA_FLAT_ATR = 0.6        # |ema[t-1]-ema[t-LOOKBACK]| / ATR  <=  this
OVERLAP_MIN = 0.35        # mean adjacent-bar overlap ratio over window >= this


def _is_range_context(b, t, atr) -> bool:
    if not STRICT_RANGE:
        return True
    e0 = b.ema20[t - LOOKBACK]
    e1 = b.ema20[t - 1]
    if np.isnan(e0) or np.isnan(e1):
        return False
    if abs(e1 - e0) / atr > EMA_FLAT_ATR:
        return False
    # mean adjacent overlap
    ov = []
    for k in range(t - LOOKBACK + 1, t):
        hi = min(b.h[k], b.h[k - 1])
        lo = max(b.l[k], b.l[k - 1])
        denom = max(b.h[k], b.h[k - 1]) - min(b.l[k], b.l[k - 1])
        if denom > 0:
            ov.append(max(0.0, hi - lo) / denom)
    if not ov or (sum(ov) / len(ov)) < OVERLAP_MIN:
        return False
    return True


def run_symbol(name):
    b = load(name)
    n = len(b)
    tick = _infer_tick(b.c)

    total_close_breakouts = 0
    failed = 0
    surviving = 0
    wick_probes = 0
    wick_failed = 0            # wick probe that never became a close breakout "success"
    mm_hits = 0
    mm_total = 0

    t = LOOKBACK
    while t < n - SURVIVE - 1:
        atr = b.atr14[t]
        if np.isnan(atr) or atr <= 0:
            t += 1
            continue
        win_h = b.h[t - LOOKBACK:t]
        win_l = b.l[t - LOOKBACK:t]
        rh = win_h.max()
        rl = win_l.min()
        width = rh - rl
        if width <= 0:
            t += 1
            continue
        w_atr = width / atr
        if not (MIN_WIDTH_ATR <= w_atr <= MAX_WIDTH_ATR):
            t += 1
            continue
        if not _is_range_context(b, t, atr):
            t += 1
            continue

        slack = atr * 0.05

        # ---- UP side ----
        up_wick = b.h[t] > rh + slack and b.c[t] <= rh
        up_close = b.c[t] > rh + tick
        down_wick = b.l[t] < rl - slack and b.c[t] >= rl
        down_close = b.c[t] < rl - tick

        advanced = False
        if up_close:
            total_close_breakouts += 1
            res, mm = _forward_up(b, t, rh, width)
            if res == "failed":
                failed += 1
            else:
                surviving += 1
                mm_total += 1
                mm_hits += int(mm)
            advanced = True
            t += SURVIVE  # skip ahead to avoid double counting the same event
        elif down_close:
            total_close_breakouts += 1
            res, mm = _forward_down(b, t, rl, width)
            if res == "failed":
                failed += 1
            else:
                surviving += 1
                mm_total += 1
                mm_hits += int(mm)
            advanced = True
            t += SURVIVE
        elif up_wick or down_wick:
            wick_probes += 1
            # wick probe "fails" unless the NEXT few bars close beyond
            side_level = rh if up_wick else rl
            got_close = False
            for k in range(t + 1, min(t + RECLAIM + 1, n)):
                if up_wick and b.c[k] > side_level + tick:
                    got_close = True
                    break
                if down_wick and b.c[k] < side_level - tick:
                    got_close = True
                    break
            if not got_close:
                wick_failed += 1

        if not advanced:
            t += 1

    return {
        "name": name,
        "close_breakouts": total_close_breakouts,
        "failed": failed,
        "surviving": surviving,
        "wick_probes": wick_probes,
        "wick_failed": wick_failed,
        "mm_hits": mm_hits,
        "mm_total": mm_total,
    }


def _forward_up(b, t, rh, width):
    """Return (outcome, mm_hit) for an up close-breakout at bar t."""
    n = len(b)
    tick = _infer_tick(b.c)
    target = rh + width
    # surviving check: within RECLAIM bars, did it close back below rh?
    reclaimed = False
    for k in range(t + 1, min(t + RECLAIM + 1, n)):
        if b.c[k] < rh - tick:
            reclaimed = True
            break
    if reclaimed:
        return "failed", False
    # survived -> MM check: reach target before closing back below rh
    mm = False
    for k in range(t + 1, min(t + MM_HORIZON + 1, n)):
        if b.h[k] >= target:
            mm = True
            break
        if b.c[k] < rh - tick:
            break
    return "surviving", mm


def _forward_down(b, t, rl, width):
    n = len(b)
    tick = _infer_tick(b.c)
    target = rl - width
    reclaimed = False
    for k in range(t + 1, min(t + RECLAIM + 1, n)):
        if b.c[k] > rl + tick:
            reclaimed = True
            break
    if reclaimed:
        return "failed", False
    mm = False
    for k in range(t + 1, min(t + MM_HORIZON + 1, n)):
        if b.l[k] <= target:
            mm = True
            break
        if b.c[k] > rl + tick:
            break
    return "surviving", mm


_tick_cache = {}
def _infer_tick(c):
    key = id(c)
    if key in _tick_cache:
        return _tick_cache[key]
    diffs = np.abs(np.diff(c))
    diffs = diffs[diffs > 0]
    tick = np.percentile(diffs, 1) if len(diffs) else 0.01
    tick = max(tick, 1e-6)
    _tick_cache[key] = tick
    return tick


def main():
    print("=" * 90)
    print("TEST 1: Range breakout failure rate + surviving-breakout range-height MM hit rate")
    print(f"  LOOKBACK={LOOKBACK}, RECLAIM={RECLAIM}, SURVIVE={SURVIVE}, MM_HORIZON={MM_HORIZON}")
    print(f"  range filter: width in [{MIN_WIDTH_ATR},{MAX_WIDTH_ATR}]xATR")
    print("=" * 90)

    agg = {}
    for inst, files in INSTRUMENTS.items():
        print(f"\n### {inst}")
        for f in files:
            try:
                r = run_symbol(f)
            except FileNotFoundError:
                continue
            cb = r["close_breakouts"]
            # Breakout attempt failure rate = failed close breakouts + failed wick probes
            # vs ALL attempts (wick probes + close breakouts).
            total_attempts = r["wick_probes"] + cb
            total_fail = r["wick_failed"] + r["failed"]
            print(f"  {f:18s}")
            print(f"      close-breakouts: {cb} | failed(reclaim<={RECLAIM}): {pct(r['failed'], cb)}")
            print(f"      surviving: {r['surviving']} | range-MM reached: {pct(r['mm_hits'], r['mm_total'])}")
            print(f"      ALL attempts (wick+close) failure: {pct(total_fail, total_attempts)}")
            for key in ("failed", "close_breakouts", "surviving", "mm_hits", "mm_total",
                        "wick_probes", "wick_failed"):
                agg[key] = agg.get(key, 0) + r[key]

    print("\n" + "=" * 90)
    print("AGGREGATE (all 3 instruments, all timeframes):")
    cb = agg["close_breakouts"]
    total_attempts = agg["wick_probes"] + cb
    total_fail = agg["wick_failed"] + agg["failed"]
    print(f"  Claim A (all attempts fail 70-80%):   {pct(total_fail, total_attempts)}")
    print(f"  Close-breakout reclaim-failure rate:  {pct(agg['failed'], cb)}")
    print(f"  Claim B (surviving reach range MM 60%): {pct(agg['mm_hits'], agg['mm_total'])}")
    print("=" * 90)


if __name__ == "__main__":
    main()
