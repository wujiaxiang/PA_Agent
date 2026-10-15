"""Three-bar gap detector (ICT "FVG" geometry / Brooks "micro gap").

Implements GAP_DEFINITIONS.md EXACTLY.  Bull case below; bear is the mirror image (prices negated).

    bars t-2, t-1, t  (t = newest bar, the event is confirmed at the close of t)
    gap  : l[t] > h[t-2]            (strict; equality is counted separately as a "touch")
    zone : z_lo = h[t-2],  z_hi = l[t],  size = z_hi - z_lo,  mid = (z_hi + z_lo) / 2
    G3   : the geometry only
    G3T  : G3 and bar t-1 is a with-direction trend bar (Brooks micro gap)
    G3S  : G3T and range of bar t-1 >= 1.0 * ATR14[t-2]
    covers: bar t-1 spans the whole zone  (l[t-1] <= h[t-2]  and  h[t-1] >= l[t])
    first: no event of the same tier and direction at t-1 or t-2 (de-duplication)

Only contiguous bars are used (each neighbouring pair closer than 1.5 x the nominal bar step) so that
session breaks / weekends do not masquerade as price-action gaps.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from common import DATA_DIR
from common2 import make_bars

CONTIG_FACTOR = 1.5
STRONG_ATR = 1.0
BODY_MIN = 0.25
CLOSEPOS_MIN = 0.65

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


def load_file(name: str):
    """name like 'XAUUSD_M15' or 'US100.cash_H1' -> (Bars, times[int64 seconds])."""
    df = pd.read_parquet(os.path.join(DATA_DIR, f"{name}.parquet")).sort_values("time").reset_index(drop=True)
    return make_bars(name, df["open"], df["high"], df["low"], df["close"]), df["time"].to_numpy(np.int64)


def tf_seconds(name: str) -> int:
    return TF_SECONDS[name.rsplit("_", 1)[-1]]


@dataclass
class GapEvents:
    t: np.ndarray            # index of the third (newest) bar
    d: np.ndarray            # +1 bull gap, -1 bear gap
    z_lo: np.ndarray         # zone lower edge (original price coordinates)
    z_hi: np.ndarray         # zone upper edge
    size: np.ndarray
    size_atr: np.ndarray
    atr: np.ndarray          # ATR14[t-2]
    trend: np.ndarray        # G3T: middle bar is a with-direction trend bar
    strong: np.ndarray       # G3S
    covers: np.ndarray
    first_g3: np.ndarray
    first_g3t: np.ndarray
    first_g3s: np.ndarray
    n_touch: int = 0         # equality cases (no space), not events
    n_noncontig: int = 0     # excluded: crosses a session break
    n_noatr: int = 0         # excluded: ATR not available

    def __len__(self) -> int:
        return len(self.t)

    @property
    def mid(self) -> np.ndarray:
        return (self.z_lo + self.z_hi) / 2.0

    def tier(self, name: str, first_only: bool = True) -> np.ndarray:
        """Boolean mask of events belonging to tier 'G3' | 'G3T' | 'G3S' (optionally only first-in-run)."""
        if name == "G3":
            m, f = np.ones(len(self), bool), self.first_g3
        elif name == "G3T":
            m, f = self.trend, self.first_g3t
        elif name == "G3S":
            m, f = self.strong, self.first_g3s
        else:
            raise ValueError(name)
        return m & f if first_only else m


def detect(b, times: np.ndarray | None = None, step: float | None = None) -> GapEvents:
    n = len(b)
    if times is not None and step is None:
        raise ValueError("pass the nominal bar step (seconds) together with times")
    cols = {k: [] for k in ("t", "d", "z_lo", "z_hi", "size", "size_atr", "atr", "trend", "strong", "covers",
                            "f3", "f3t", "f3s")}
    n_touch = n_noncontig = n_noatr = 0
    if n >= 3:
        t = np.arange(2, n)
        for d in (1, -1):
            if d == 1:
                H, L, O, C = b.h, b.l, b.o, b.c
            else:                                   # mirror: bear gap == bull gap on negated prices
                H, L, O, C = -b.l, -b.h, -b.o, -b.c
            gap = L[t] - H[t - 2]
            atr = b.atr14[t - 2]
            okatr = ~np.isnan(atr) & (atr > 0)
            if times is None:
                contig = np.ones(len(t), bool)
            else:
                contig = (times[t] - times[t - 1] <= CONTIG_FACTOR * step) & (times[t - 1] - times[t - 2] <= CONTIG_FACTOR * step)
            n_touch += int(((gap == 0) & okatr & contig).sum())
            n_noatr += int(((gap > 0) & ~okatr).sum())
            n_noncontig += int(((gap > 0) & okatr & ~contig).sum())
            valid = (gap > 0) & okatr & contig

            Om, Hm, Lm, Cm = O[t - 1], H[t - 1], L[t - 1], C[t - 1]
            rng = Hm - Lm
            with np.errstate(divide="ignore", invalid="ignore"):
                body = (Cm - Om) / rng
                cp = (Cm - Lm) / rng
            trend = (rng > 0) & (Cm > Om) & (body > BODY_MIN) & (cp >= CLOSEPOS_MIN)
            strong = trend & (rng >= STRONG_ATR * np.where(okatr, atr, np.inf))
            covers = (Lm <= H[t - 2]) & (Hm >= L[t])

            def first_of(mask):
                full = np.zeros(n, bool)
                full[2:] = mask
                return ~(full[t - 1] | full[t - 2])

            f3, f3t, f3s = first_of(valid), first_of(valid & trend), first_of(valid & strong)
            idx = np.flatnonzero(valid)
            tt = t[idx]
            if d == 1:
                z_lo, z_hi = H[t - 2][idx], L[t][idx]
            else:
                z_hi, z_lo = -H[t - 2][idx], -L[t][idx]
            cols["t"].append(tt)
            cols["d"].append(np.full(len(idx), d))
            cols["z_lo"].append(z_lo)
            cols["z_hi"].append(z_hi)
            cols["size"].append(gap[idx])
            cols["size_atr"].append(gap[idx] / atr[idx])
            cols["atr"].append(atr[idx])
            cols["trend"].append(trend[idx])
            cols["strong"].append(strong[idx])
            cols["covers"].append(covers[idx])
            cols["f3"].append(f3[idx])
            cols["f3t"].append(f3t[idx])
            cols["f3s"].append(f3s[idx])

    def cat(k, dtype):
        return np.concatenate(cols[k]).astype(dtype) if cols[k] else np.array([], dtype=dtype)

    ev = dict(
        t=cat("t", int), d=cat("d", int), z_lo=cat("z_lo", float), z_hi=cat("z_hi", float),
        size=cat("size", float), size_atr=cat("size_atr", float), atr=cat("atr", float),
        trend=cat("trend", bool), strong=cat("strong", bool), covers=cat("covers", bool),
        first_g3=cat("f3", bool), first_g3t=cat("f3t", bool), first_g3s=cat("f3s", bool),
    )
    order = np.lexsort((ev["d"], ev["t"]))
    ev = {k: v[order] for k, v in ev.items()}
    return GapEvents(**ev, n_touch=n_touch, n_noncontig=n_noncontig, n_noatr=n_noatr)


def outcomes(b, ev: GapEvents, N: int):
    """First offset (1..N) at which the zone is tested / its midpoint is reached / it is filled; 0 = never.

    bull: test l<=z_hi, mid l<=mid, fill l<=z_lo.   bear: test h>=z_lo, mid h>=mid, fill h>=z_hi.
    Events whose N-bar window runs past the end of the series are marked invalid (censored).
    """
    m, n = len(ev), len(b)
    test = np.zeros(m, int)
    mid = np.zeros(m, int)
    fill = np.zeros(m, int)
    valid = ev.t + N <= n - 1
    em = ev.mid
    for i in np.flatnonzero(valid):
        t0 = int(ev.t[i])
        if ev.d[i] == 1:
            seg = b.l[t0 + 1: t0 + N + 1]
            ths = (ev.z_hi[i], em[i], ev.z_lo[i])
            for arr, th in zip((test, mid, fill), ths):
                hit = seg <= th
                if hit.any():
                    arr[i] = int(np.argmax(hit)) + 1
        else:
            seg = b.h[t0 + 1: t0 + N + 1]
            ths = (ev.z_lo[i], em[i], ev.z_hi[i])
            for arr, th in zip((test, mid, fill), ths):
                hit = seg >= th
                if hit.any():
                    arr[i] = int(np.argmax(hit)) + 1
    return test, mid, fill, valid
