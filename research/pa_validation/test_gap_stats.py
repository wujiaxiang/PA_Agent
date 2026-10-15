"""Safeguards for gap_stats.py: the H7 'gap group' must be exactly the detector's G3S first-in-run events."""
from __future__ import annotations

import os

import numpy as np
import pytest

from common import DATA_DIR
from common2 import make_bars
from gap_defs import detect, load_file, tf_seconds
from gap_stats import (MIN_N, NSTRATA, dist_atr, h7_samples, strat_excess, strata_ids)

HAVE_DATA = os.path.isdir(DATA_DIR)


def _slice(name, n):
    b, times = load_file(name)
    b = make_bars(name, b.o[:n], b.h[:n], b.l[:n], b.c[:n])
    return b, times[:n], tf_seconds(name)


@pytest.mark.skipif(not HAVE_DATA, reason="market data not available")
@pytest.mark.parametrize("name", ["XAUUSD_M15", "US100.cash_M15", "US500.cash_H1"])
def test_h7_gap_group_equals_detector_g3s_first(name):
    b, times, step = _slice(name, 20000)
    N = 10
    ev = detect(b, times, step)
    m = ev.tier("G3S", True) & (ev.t <= len(b) - 1 - N)
    want = sorted(zip(ev.t[m].tolist(), ev.d[m].tolist()))
    s = h7_samples(b, times, step, N)
    got_t = s["gap"][0]
    assert sorted(got_t.tolist()) == sorted(t for t, _ in want)


@pytest.mark.skipif(not HAVE_DATA, reason="market data not available")
def test_h7_control_never_overlaps_gap_and_has_no_gap():
    b, times, step = _slice("XAUUSD_M15", 20000)
    s = h7_samples(b, times, step, 10)
    tg, tc = s["gap"][0], s["ctrl"][0]
    # control events are bars whose low did not clear the high two bars earlier (bull) / mirror
    for t in tc[:500]:
        t = int(t)
        bull_gap = b.l[t] > b.h[t - 2]
        bear_gap = b.h[t] < b.l[t - 2]
        assert not (bull_gap or bear_gap)
    assert len(tg) > 100 and len(tc) > 100


@pytest.mark.skipif(not HAVE_DATA, reason="market data not available")
def test_distance_nonnegative_and_strata_in_range():
    b, times, step = _slice("US100.cash_M15", 20000)
    ev = detect(b, times, step)
    d = dist_atr(b, ev)
    assert (d >= -1e-9).all()
    sid = strata_ids(b, ev)
    assert sid.min() >= 0 and sid.max() < NSTRATA


def test_strat_excess_hand_computed():
    kR = np.zeros(NSTRATA)
    nR = np.zeros(NSTRATA)
    kV = np.zeros(NSTRATA)
    nV = np.zeros(NSTRATA)
    # stratum 0: REAL 60/100, VOL 50/100 ; stratum 1: REAL 30/100, VOL 30/100 ; stratum 2 too small -> dropped
    kR[0], nR[0], kV[0], nV[0] = 60, 100, 50, 100
    kR[1], nR[1], kV[1], nV[1] = 30, 100, 30, 100
    kR[2], nR[2], kV[2], nV[2] = 5, MIN_N - 1, 1, MIN_N - 1
    e, se = strat_excess(kR, nR, kV, nV)
    assert e == pytest.approx(0.5 * 0.10 + 0.5 * 0.0)
    se0 = np.sqrt(0.6 * 0.4 / 100 + 0.5 * 0.5 / 100)
    se1 = np.sqrt(0.3 * 0.7 / 100 * 2)
    assert se == pytest.approx(np.sqrt((0.5 * se0) ** 2 + (0.5 * se1) ** 2))


def test_strat_excess_all_strata_too_small_gives_nan():
    z = np.zeros(NSTRATA)
    e, se = strat_excess(z, z, z, z)
    assert np.isnan(e) and np.isnan(se)
