"""v2 shared utilities: surrogate (i.i.d.) baselines, bull/bear mirroring, datasets.

Why surrogates: many "claims" are really first-passage races whose answer depends
mostly on geometry (distance to target vs stop). A claim is only supported if the
real data beats the SAME detector run on a series whose bars are shuffled
(destroys serial dependence, preserves bar-shape / volatility distribution).
"""
from __future__ import annotations

import numpy as np

from common import INSTRUMENTS, Bars, atr_full, ema_full, load, pct, wilson  # noqa: F401


def make_bars(name: str, o, h, l, c) -> Bars:
    o = np.asarray(o, dtype=float)
    h = np.asarray(h, dtype=float)
    l = np.asarray(l, dtype=float)
    c = np.asarray(c, dtype=float)
    return Bars(name=name, o=o, h=h, l=l, c=c, ema20=ema_full(c, 20), atr14=atr_full(h, l, c, 14))


def surrogate(b: Bars, seed: int = 0) -> Bars:
    """Shuffle bars as units (gap, body, wicks as price RATIOS), rebuild a price chain."""
    rng = np.random.default_rng(seed)
    n = len(b)
    prev_c = np.r_[b.c[0], b.c[:-1]]
    gap = b.o / prev_c
    hr = b.h / b.o
    lr = b.l / b.o
    cr = b.c / b.o
    idx = rng.permutation(n)
    log_step = np.log(gap[idx]) + np.log(cr[idx])
    c_new = b.c[0] * np.exp(np.cumsum(log_step))
    prev_new = np.r_[b.c[0], c_new[:-1]]
    o_new = prev_new * gap[idx]
    h_new = o_new * hr[idx]
    l_new = o_new * lr[idx]
    return make_bars(b.name + "~surr", o_new, h_new, l_new, c_new)


def invert(b: Bars) -> Bars:
    """Mirror price (bear -> bull). EMA of -c is -EMA(c); ATR unchanged."""
    return Bars(
        name=b.name + "~inv",
        o=-b.o, h=-b.l, l=-b.h, c=-b.c,
        ema20=-b.ema20, atr14=b.atr14.copy(),
    )


def tick_of(b: Bars) -> float:
    d = np.abs(np.diff(b.c))
    d = d[d > 0]
    return float(max(np.percentile(d, 1), 1e-6)) if len(d) else 0.01


def datasets(include_h4: bool = True):
    """Yield (instrument, file_name). Same universe as v1."""
    for inst, files in INSTRUMENTS.items():
        for f in files:
            if not include_h4 and "_H4" in f:
                continue
            yield inst, f


def mean_ci(x: np.ndarray) -> tuple[float, float]:
    """Mean and 1.96*SE half-width (events overlap a bit, so treat CI as optimistic)."""
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return (float(x.mean()) if len(x) else float("nan"), float("nan"))
    return float(x.mean()), float(1.96 * x.std(ddof=1) / np.sqrt(len(x)))


class Acc:
    """Hit/total accumulator keyed by string."""

    def __init__(self):
        self.d: dict[str, list[int]] = {}

    def add(self, key: str, hit: bool | int):
        v = self.d.setdefault(key, [0, 0])
        v[0] += int(bool(hit))
        v[1] += 1

    def merge(self, other: "Acc"):
        for k, (a, b) in other.d.items():
            v = self.d.setdefault(k, [0, 0])
            v[0] += a
            v[1] += b

    def get(self, key: str) -> tuple[int, int]:
        v = self.d.get(key, [0, 0])
        return v[0], v[1]

    def fmt(self, key: str) -> str:
        k, n = self.get(key)
        return pct(k, n)
