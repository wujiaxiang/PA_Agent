"""Verification of gap_defs.py.

1. hand-built synthetic cases (the examples in GAP_DEFINITIONS.md)
2. cross-check against an independently written naive implementation on REAL data
3. causality: truncating the series must not change earlier events
4. mirror symmetry: detect(invert(b)) == mirrored detect(b)
5. invariants of events and outcomes on REAL data
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from common import DATA_DIR
from common2 import invert, make_bars
from gap_defs import detect, load_file, outcomes, tf_seconds

FILLER = 20
HAVE_DATA = os.path.isdir(DATA_DIR)


def build(rows, times=None):
    """20 neutral filler bars (range 1.0 -> ATR14 == 1.0) followed by `rows` of (o,h,l,c)."""
    fill = [(100.5, 101.0, 100.0, 100.5)] * FILLER
    allr = fill + list(rows)
    o, h, l, c = (np.array([r[i] for r in allr], float) for i in range(4))
    return make_bars("syn", o, h, l, c)


def mirror(rows, pivot=200.0):
    return [(pivot - o, pivot - l, pivot - h, pivot - c) for (o, h, l, c) in rows]


# bar t-2, t-1, t
CASE_A = [
    (100.4, 101.0, 100.0, 100.9),     # t-2 : high 101.0
    (100.9, 103.0, 100.8, 102.9),     # t-1 : strong bull bar, range 2.2, body .91, close pos .95
    (102.7, 104.0, 102.0, 103.8),     # t   : low 102.0 > 101.0  -> gap [101, 102], size 1.0
]


def test_bull_gap_basic():
    ev = detect(build(CASE_A))
    assert len(ev) == 1
    assert ev.t[0] == FILLER + 2 and ev.d[0] == 1
    assert (ev.z_lo[0], ev.z_hi[0], ev.size[0]) == (101.0, 102.0, 1.0)
    assert ev.mid[0] == 101.5
    assert ev.size_atr[0] == pytest.approx(1.0)
    assert ev.trend[0] and ev.strong[0] and ev.covers[0]
    assert ev.first_g3[0] and ev.first_g3t[0] and ev.first_g3s[0]


def test_equality_is_a_touch_not_an_event():
    rows = [CASE_A[0], CASE_A[1], (102.7, 104.0, 101.0, 103.8)]      # l[t] == h[t-2]
    ev = detect(build(rows))
    assert len(ev) == 0 and ev.n_touch == 1


def test_doji_middle_bar_is_g3_but_not_g3t():
    rows = [CASE_A[0], (101.4, 103.0, 100.8, 101.5), CASE_A[2]]
    ev = detect(build(rows))
    assert len(ev) == 1
    assert not ev.trend[0] and not ev.strong[0]
    assert ev.tier("G3")[0] and not ev.tier("G3T")[0] and not ev.tier("G3S")[0]


def test_trend_but_not_strong_and_not_covering():
    rows = [
        (100.4, 101.0, 100.0, 100.9),
        (101.0, 101.8, 101.0, 101.7),     # range 0.8 (< 1.0 ATR), bull trend bar
        (101.9, 102.5, 101.9, 102.4),     # l[t] = 101.9 > 101.0
    ]
    ev = detect(build(rows))
    assert len(ev) == 1
    assert ev.trend[0] and not ev.strong[0]
    assert not ev.covers[0]               # h[t-1] = 101.8 < l[t] = 101.9 -> a real void remains


def test_bear_is_the_mirror():
    ev = detect(build(mirror(CASE_A)))
    # filler is NOT mirrored; only look at the pattern event
    bear = [i for i in range(len(ev)) if ev.t[i] == FILLER + 2 and ev.d[i] == -1]
    assert len(bear) == 1
    i = bear[0]
    assert (ev.z_hi[i], ev.z_lo[i]) == (99.0, 98.0)
    assert ev.trend[i] and ev.strong[i] and ev.covers[i]


def test_noncontiguous_bars_are_excluded_and_counted():
    b = build(CASE_A)
    times = (np.arange(len(b)) * 900).astype(np.int64)
    times[FILLER + 2:] += 3600                      # a session break between t-1 and t
    ev = detect(b, times, 900)
    assert len(ev) == 0 and ev.n_noncontig == 1
    ev2 = detect(b, np.arange(len(b), dtype=np.int64) * 900, 900)
    assert len(ev2) == 1


def test_run_dedup_flags():
    rows = [
        (100.4, 101.0, 100.0, 100.9),
        (101.2, 103.0, 101.0, 102.9),
        (102.0, 105.0, 101.8, 104.8),     # t=22: l 101.8 > h[20]=101.0
        (104.0, 107.0, 103.5, 106.8),     # t=23: l 103.5 > h[21]=103.0
        (106.0, 109.0, 105.5, 108.8),     # t=24: l 105.5 > h[22]=105.0
    ]
    ev = detect(build(rows))
    bull = [i for i in range(len(ev)) if ev.d[i] == 1]
    assert [int(ev.t[i]) for i in bull] == [FILLER + 2, FILLER + 3, FILLER + 4]
    assert [bool(ev.first_g3[i]) for i in bull] == [True, False, False]


def test_outcome_offsets_bull_and_bear():
    after = [
        (103.8, 104.0, 102.4, 103.5),     # t+1 : low 102.4  (> z_hi 102)   nothing
        (103.5, 103.6, 101.8, 102.0),     # t+2 : low 101.8  (<= z_hi, > mid 101.5)  TEST
        (102.0, 102.1, 101.3, 101.4),     # t+3 : low 101.3  (<= mid, > z_lo 101)    MID
        (101.4, 101.5, 100.9, 101.0),     # t+4 : low 100.9  (<= z_lo)               FILL
        (101.0, 101.5, 100.5, 101.2),     # t+5
    ]
    b = build(CASE_A + after)
    ev = detect(b)
    te, mi, fi, ok = outcomes(b, ev, 5)
    assert ok[0] and (te[0], mi[0], fi[0]) == (2, 3, 4)

    rows = mirror(CASE_A + after)
    bm = build(rows)
    evm = detect(bm)
    sel = [i for i in range(len(evm)) if evm.t[i] == FILLER + 2 and evm.d[i] == -1]
    te, mi, fi, ok = outcomes(bm, evm, 5)
    i = sel[0]
    assert ok[i] and (te[i], mi[i], fi[i]) == (2, 3, 4)


def test_censoring_at_series_end():
    b = build(CASE_A)
    ev = detect(b)
    _, _, _, ok = outcomes(b, ev, 5)
    assert not ok[0]


# --------------------------------------------------------------------------- real data

FILES = ["XAUUSD_M15", "US100.cash_M15", "US500.cash_H1", "XAUUSD_M5", "XAUUSD_H4"]


def naive_events(b, times, step):
    o, h, l, c, atr = b.o, b.h, b.l, b.c, b.atr14
    out = []
    n = len(h)
    for t in range(2, n):
        a = atr[t - 2]
        if not (a == a) or a <= 0:
            continue
        if times is not None and (times[t] - times[t - 1] > 1.5 * step or times[t - 1] - times[t - 2] > 1.5 * step):
            continue
        m = t - 1
        rng = h[m] - l[m]
        # bull
        if l[t] - h[t - 2] > 0:
            tr = rng > 0 and c[m] > o[m] and (c[m] - o[m]) / rng > 0.25 and (c[m] - l[m]) / rng >= 0.65
            st = tr and rng >= 1.0 * a
            cov = l[m] <= h[t - 2] and h[m] >= l[t]
            out.append((t, 1, h[t - 2], l[t], bool(tr), bool(st), bool(cov)))
        # bear
        if l[t - 2] - h[t] > 0:
            tr = rng > 0 and c[m] < o[m] and (o[m] - c[m]) / rng > 0.25 and (h[m] - c[m]) / rng >= 0.65
            st = tr and rng >= 1.0 * a
            cov = h[m] >= l[t - 2] and l[m] <= h[t]
            out.append((t, -1, h[t], l[t - 2], bool(tr), bool(st), bool(cov)))
    ts = {(e[0], e[1]) for e in out}
    tr_ts = {(e[0], e[1]) for e in out if e[4]}
    st_ts = {(e[0], e[1]) for e in out if e[5]}
    res = []
    for (t, d, zl, zh, tr, st, cov) in out:
        f3 = not (((t - 1, d) in ts) or ((t - 2, d) in ts))
        f3t = not (((t - 1, d) in tr_ts) or ((t - 2, d) in tr_ts))
        f3s = not (((t - 1, d) in st_ts) or ((t - 2, d) in st_ts))
        res.append((t, d, zl, zh, tr, st, cov, f3, f3t, f3s))
    return sorted(res)


def fast_events(ev):
    return sorted(
        (int(ev.t[i]), int(ev.d[i]), float(ev.z_lo[i]), float(ev.z_hi[i]), bool(ev.trend[i]), bool(ev.strong[i]),
         bool(ev.covers[i]), bool(ev.first_g3[i]), bool(ev.first_g3t[i]), bool(ev.first_g3s[i]))
        for i in range(len(ev))
    )


@pytest.mark.skipif(not HAVE_DATA, reason="market data not available")
@pytest.mark.parametrize("name", FILES)
def test_matches_independent_naive_implementation(name):
    b, times = load_file(name)
    n = min(len(b), 25000)
    b = make_bars(name, b.o[:n], b.h[:n], b.l[:n], b.c[:n])
    times = times[:n]
    step = tf_seconds(name)
    assert fast_events(detect(b, times, step)) == naive_events(b, times, step)


@pytest.mark.skipif(not HAVE_DATA, reason="market data not available")
def test_causality_truncation_does_not_change_earlier_events():
    name = "XAUUSD_M15"
    b, times = load_file(name)
    n = 9000
    b = make_bars(name, b.o[:n], b.h[:n], b.l[:n], b.c[:n])
    times = times[:n]
    step = tf_seconds(name)
    full = fast_events(detect(b, times, step))
    for m in (1500, 4000, 7777):
        bc = make_bars(name, b.o[: m + 1], b.h[: m + 1], b.l[: m + 1], b.c[: m + 1])
        cut = fast_events(detect(bc, times[: m + 1], step))
        assert cut == [e for e in full if e[0] <= m]


@pytest.mark.skipif(not HAVE_DATA, reason="market data not available")
def test_mirror_symmetry_on_real_data():
    name = "US100.cash_M15"
    b, times = load_file(name)
    n = 12000
    b = make_bars(name, b.o[:n], b.h[:n], b.l[:n], b.c[:n])
    times = times[:n]
    step = tf_seconds(name)
    ev = detect(b, times, step)
    evi = detect(invert(b), times, step)
    assert len(ev) == len(evi)
    a = sorted((int(ev.t[i]), -int(ev.d[i]), -float(ev.z_hi[i]), -float(ev.z_lo[i]), bool(ev.trend[i]), bool(ev.strong[i]), bool(ev.covers[i]))
               for i in range(len(ev)))
    bb = sorted((int(evi.t[i]), int(evi.d[i]), float(evi.z_lo[i]), float(evi.z_hi[i]), bool(evi.trend[i]), bool(evi.strong[i]), bool(evi.covers[i]))
                for i in range(len(evi)))
    assert a == bb


@pytest.mark.skipif(not HAVE_DATA, reason="market data not available")
@pytest.mark.parametrize("name", ["XAUUSD_M15", "US500.cash_M5"])
def test_event_and_outcome_invariants(name):
    b, times = load_file(name)
    ev = detect(b, times, tf_seconds(name))
    assert len(ev) > 100
    assert (ev.size > 0).all() and (ev.z_hi > ev.z_lo).all()
    assert np.allclose(ev.size, ev.z_hi - ev.z_lo)
    assert (ev.size_atr > 0).all() and np.isfinite(ev.size_atr).all()
    assert (~ev.strong | ev.trend).all()                     # G3S is a subset of G3T
    te, mi, fi, ok = outcomes(b, ev, 20)
    assert ok.sum() > 0.9 * len(ev)
    o = ok
    assert ((te[o] == 0) <= (mi[o] == 0)).all() and ((mi[o] == 0) <= (fi[o] == 0)).all()   # fill => mid => test
    both = o & (mi > 0)
    assert (te[both] <= mi[both]).all()
    both = o & (fi > 0)
    assert (mi[both] <= fi[both]).all() and (fi[both] >= 1).all() and (fi[both] <= 20).all()
