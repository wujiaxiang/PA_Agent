"""Draw detected gap events for eyeballing.  Bars are drawn up to and INCLUDING the confirming bar t only.

Blue/orange/green frames = bars t-2 / t-1 / t.  Shaded band = the zone [z_lo, z_hi].
"""
from __future__ import annotations

import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle

from gap_defs import detect, load_file, tf_seconds

LOOKBACK = 12


def draw(ax, b, times, ev, i, title_extra=""):
    t = int(ev.t[i])
    lo_i = max(0, t - LOOKBACK)
    xs = np.arange(lo_i, t + 1)
    for k in xs:
        up = b.c[k] >= b.o[k]
        col = "#2e7d32" if up else "#c62828"
        ax.plot([k, k], [b.l[k], b.h[k]], color=col, lw=1)
        body_lo, body_hi = min(b.o[k], b.c[k]), max(b.o[k], b.c[k])
        ax.add_patch(Rectangle((k - 0.3, body_lo), 0.6, max(body_hi - body_lo, 1e-9), facecolor=col, edgecolor=col, alpha=0.85))
    for k, colr in ((t - 2, "#1565c0"), (t - 1, "#ef6c00"), (t, "#6a1b9a")):
        ax.add_patch(Rectangle((k - 0.45, b.l[k]), 0.9, b.h[k] - b.l[k], fill=False, edgecolor=colr, lw=1.6))
    ax.add_patch(Rectangle((t - 2 - 0.45, ev.z_lo[i]), 2.9, ev.z_hi[i] - ev.z_lo[i], facecolor="#ffeb3b", alpha=0.45, edgecolor="none"))
    ax.axhline(ev.z_lo[i], color="#9e9e9e", lw=0.6, ls=":")
    ax.axhline(ev.z_hi[i], color="#9e9e9e", lw=0.6, ls=":")
    lo = min(b.l[lo_i:t + 1].min(), ev.z_lo[i])
    hi = max(b.h[lo_i:t + 1].max(), ev.z_hi[i])
    pad = (hi - lo) * 0.08
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(lo_i - 1, t + 2)
    d = "BULL" if ev.d[i] == 1 else "BEAR"
    tier = "G3S" if ev.strong[i] else ("G3T" if ev.trend[i] else "G3")
    ax.set_title(f"{d} {tier}  size={ev.size_atr[i]:.2f}ATR  covers={bool(ev.covers[i])}{title_extra}", fontsize=8)
    ax.tick_params(labelsize=6)


def sample(ev, mask, k, rng):
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    return list(rng.choice(idx, size=min(k, len(idx)), replace=False))


def make_sheet(path, name, picks, b, times, ev, title):
    n = len(picks)
    cols = 4
    rows = int(np.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(4.4 * cols, 3.2 * rows))
    axes = np.atleast_1d(axes).ravel()
    for ax, (i, extra) in zip(axes, picks):
        draw(ax, b, times, ev, i, extra)
    for ax in axes[n:]:
        ax.axis("off")
    fig.suptitle(title, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=70)
    plt.close(fig)


def main():
    rng = np.random.default_rng(12345)
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    for name in ("XAUUSD_M15", "US100.cash_M15"):
        b, times = load_file(name)
        ev = detect(b, times, tf_seconds(name))
        first = ev.first_g3
        picks = []
        # (a) Brooks micro gap G3T/G3S, first in run, bull + bear, typical size
        typ = (ev.size_atr >= 0.15) & (ev.size_atr <= 0.8)
        for i in sample(ev, ev.strong & first & typ & (ev.d == 1), 3, rng):
            picks.append((i, ""))
        for i in sample(ev, ev.strong & first & typ & (ev.d == -1), 3, rng):
            picks.append((i, ""))
        # (b) G3 only: middle bar NOT a trend bar (FVG geometry without Brooks' bar-type requirement)
        for i in sample(ev, (~ev.trend) & first & typ, 3, rng):
            picks.append((i, "  [middle bar not a trend bar]"))
        # (c) big gaps
        for i in sample(ev, first & (ev.size_atr >= 1.0), 3, rng):
            picks.append((i, "  [big]"))
        make_sheet(f"{out_dir}/gap_check_{name}.png", name, picks, b, times, ev,
                   f"{name}: random detected 3-bar gaps (bars drawn up to confirming bar t only)")

    # edge cases: touch / session-break are NOT events -> show a couple of rejected ones for the user to judge
    name = "XAUUSD_M15"
    b, times = load_file(name)
    step = tf_seconds(name)
    t = np.arange(2, len(b))
    gap = b.l[t] - b.h[t - 2]
    cont = (times[t] - times[t - 1] <= 1.5 * step) & (times[t - 1] - times[t - 2] <= 1.5 * step)
    rej = t[(gap > 0) & ~cont]
    print("rejected (session-break) examples:", len(rej))


if __name__ == "__main__":
    main()
