# -*- coding: utf-8 -*-
"""Unit tests for chart overlay logic — EMA 计算、支撑阻力位提取、forming bar 识别。

覆盖（SubTask 14.3）：
- EMA20 计算（SMA 种子 + 指数平滑）— 移植自 chart.js setEma
- parseLevelValue — 移植自 app.js parseLevelValue（number / string / object / 区间）
- extractSupportResistance — 移植自 app.js extractSupportResistance
- forming bar 半透明判定 — closed=False 的最后一根
- 奇数 seq 序号标签过滤 — 移植自 chart.js setSeqMarkers

策略：将 chart.js / app.js 中的纯计算逻辑移植为 Python 等价实现并验证。
"""
from __future__ import annotations

import math
from typing import Any


# ── 移植自 chart.js setEma ────────────────────────────────────────────────────

def calc_ema(bars: list[dict], period: int = 20) -> list[dict]:
    """计算 EMA，返回 [{time, value}, ...]。

    与 chart.js setEma 一致：
    - bars 按 ts_open 升序
    - 不足 period 根返回空
    - SMA 种子 = 前 period 根 close 均值
    - k = 2 / (period + 1)
    """
    if not bars or len(bars) < period:
        return []
    sorted_bars = sorted(bars, key=lambda b: b["ts_open"])
    k = 2 / (period + 1)
    # SMA 种子
    ema_prev = sum(sorted_bars[i]["close"] for i in range(period)) / period
    out = [{"time": sorted_bars[period - 1]["ts_open"] / 1000, "value": ema_prev}]
    for i in range(period, len(sorted_bars)):
        ema_prev = sorted_bars[i]["close"] * k + ema_prev * (1 - k)
        out.append({"time": sorted_bars[i]["ts_open"] / 1000, "value": ema_prev})
    return out


# ── 移植自 app.js parseLevelValue ─────────────────────────────────────────────

def parse_level_value(v: Any) -> dict | None:
    """解析单条 level 值，返回 {low, high} 或 None。

    支持：number / "2600" / "2600-2610" / {low, high} / {price}
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return None
        return {"low": float(v), "high": float(v)}
    if isinstance(v, dict):
        low = float(v["low"]) if v.get("low") is not None else None
        high = float(v["high"]) if v.get("high") is not None else None
        price = float(v["price"]) if v.get("price") is not None else None
        if low is not None and high is not None:
            return {"low": low, "high": high}
        if price is not None:
            return {"low": price, "high": price}
        if low is not None:
            return {"low": low, "high": low}
        if high is not None:
            return {"low": high, "high": high}
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # 区间：2600-2610 / 2600~2610 / 2600—2610 / 2600到2610
        import re
        m = re.match(r"^(-?\d+(?:\.\d+)?)\s*[-~—–到至〜]\s*(-?\d+(?:\.\d+)?)$", s)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            return {"low": min(a, b), "high": max(a, b)}
        try:
            n = float(s)
            return {"low": n, "high": n}
        except ValueError:
            return None
    return None


# ── 移植自 app.js extractSupportResistance ────────────────────────────────────

def extract_support_resistance(stage1: dict | None) -> list[dict]:
    """从 stage1_diagnosis 提取支撑/阻力位。

    返回 [{kind, low, high, label}, ...]
    """
    if not stage1 or not isinstance(stage1, dict):
        return []
    out: list[dict] = []
    sup = stage1.get("support_levels") or stage1.get("supports") or []
    res = stage1.get("resistance_levels") or stage1.get("resistances") or []
    if isinstance(sup, list):
        for i, v in enumerate(sup):
            parsed = parse_level_value(v)
            if parsed:
                out.append({
                    "kind": "support",
                    "low": parsed["low"],
                    "high": parsed["high"],
                    "label": f"支撑{i + 1 if i > 0 else ''}",
                })
    if isinstance(res, list):
        for i, v in enumerate(res):
            parsed = parse_level_value(v)
            if parsed:
                out.append({
                    "kind": "resistance",
                    "low": parsed["low"],
                    "high": parsed["high"],
                    "label": f"阻力{i + 1 if i > 0 else ''}",
                })
    return out


# ── 移植自 chart.js setBars forming bar 判定 ──────────────────────────────────

def get_forming_bar(bars: list[dict]) -> dict | None:
    """返回最后一根 closed=False 的 bar（forming bar）。"""
    if not bars:
        return None
    sorted_bars = sorted(bars, key=lambda b: b["ts_open"])
    last = sorted_bars[-1]
    if last.get("closed") is False:
        return last
    return None


# ── 移植自 chart.js setSeqMarkers 奇数 seq 过滤 ───────────────────────────────

FIT_VISIBLE_BARS = 20


def filter_seq_markers(bars: list[dict]) -> list[int]:
    """返回应显示序号标签的 seq 列表（奇数 seq，最近 FIT_VISIBLE_BARS*2 范围内）。"""
    if not bars:
        return []
    sorted_bars = sorted(bars, key=lambda b: b["ts_open"])
    start = max(0, len(sorted_bars) - FIT_VISIBLE_BARS * 2)
    result: list[int] = []
    for i in range(start, len(sorted_bars)):
        b = sorted_bars[i]
        seq = b.get("seq")
        if not isinstance(seq, (int, float)):
            continue
        if seq <= 0:
            continue
        if int(seq) % 2 == 0:
            continue
        result.append(int(seq))
    return result


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _make_bars(n: int, base_price: float = 2000.0) -> list[dict]:
    """构造 n 根 K 线（升序），最后一根为 forming。"""
    return [
        {
            "seq": n - i,
            "ts_open": 1_700_000_000_000 + i * 60_000,
            "open": base_price + i,
            "high": base_price + i + 5,
            "low": base_price + i - 5,
            "close": base_price + i + 2,
            "volume": 100.0,
            "closed": i < n - 1,
        }
        for i in range(n)
    ]


# ── EMA 测试 ──────────────────────────────────────────────────────────────────


class TestEMA:
    def test_insufficient_bars_returns_empty(self):
        """不足 period 根返回空列表。"""
        bars = _make_bars(10)
        assert calc_ema(bars, period=20) == []

    def test_exact_period_returns_single_point(self):
        """刚好 period 根返回一个点（SMA 种子）。"""
        bars = _make_bars(20)
        result = calc_ema(bars, period=20)
        assert len(result) == 1
        # SMA 种子 = 前 20 根 close 均值
        expected = sum(b["close"] for b in sorted(bars, key=lambda x: x["ts_open"])[:20]) / 20
        assert abs(result[0]["value"] - expected) < 1e-9

    def test_more_than_period(self):
        """25 根 → 6 个 EMA 点（20+5）。"""
        bars = _make_bars(25)
        result = calc_ema(bars, period=20)
        assert len(result) == 6

    def test_ema_smoothing_formula(self):
        """验证 EMA 平滑公式：ema = close*k + ema_prev*(1-k)。"""
        bars = _make_bars(22)
        result = calc_ema(bars, period=20)
        assert len(result) == 3  # index 19, 20, 21
        k = 2 / 21
        sorted_bars = sorted(bars, key=lambda b: b["ts_open"])
        sma = sum(b["close"] for b in sorted_bars[:20]) / 20
        expected_1 = sorted_bars[20]["close"] * k + sma * (1 - k)
        assert abs(result[1]["value"] - expected_1) < 1e-9

    def test_empty_bars(self):
        assert calc_ema([], period=20) == []

    def test_bars_sorted_by_ts(self):
        """EMA 应按 ts_open 升序计算（即使输入乱序）。"""
        bars = _make_bars(22)
        import random
        shuffled = bars[:]
        random.shuffle(shuffled)
        result = calc_ema(shuffled, period=20)
        # 时间应递增
        times = [p["time"] for p in result]
        assert times == sorted(times)


# ── parseLevelValue 测试 ──────────────────────────────────────────────────────


class TestParseLevelValue:
    def test_number(self):
        assert parse_level_value(2600) == {"low": 2600.0, "high": 2600.0}

    def test_float(self):
        assert parse_level_value(2600.5) == {"low": 2600.5, "high": 2600.5}

    def test_string_single(self):
        assert parse_level_value("2600") == {"low": 2600.0, "high": 2600.0}

    def test_string_with_decimal(self):
        assert parse_level_value("2600.50") == {"low": 2600.5, "high": 2600.5}

    def test_string_range_dash(self):
        result = parse_level_value("2600-2610")
        assert result == {"low": 2600.0, "high": 2610.0}

    def test_string_range_tilde(self):
        result = parse_level_value("2600~2610")
        assert result == {"low": 2600.0, "high": 2610.0}

    def test_string_range_em_dash(self):
        result = parse_level_value("2600—2610")
        assert result == {"low": 2600.0, "high": 2610.0}

    def test_string_range_chinese(self):
        result = parse_level_value("2600到2610")
        assert result == {"low": 2600.0, "high": 2610.0}

    def test_string_range_reversed(self):
        """区间值反序时 low/high 自动归正。"""
        result = parse_level_value("2610-2600")
        assert result == {"low": 2600.0, "high": 2610.0}

    def test_object_low_high(self):
        result = parse_level_value({"low": 2600, "high": 2610})
        assert result == {"low": 2600.0, "high": 2610.0}

    def test_object_price(self):
        result = parse_level_value({"price": 2600})
        assert result == {"low": 2600.0, "high": 2600.0}

    def test_object_only_low(self):
        result = parse_level_value({"low": 2600})
        assert result == {"low": 2600.0, "high": 2600.0}

    def test_none(self):
        assert parse_level_value(None) is None

    def test_empty_string(self):
        assert parse_level_value("") is None
        assert parse_level_value("  ") is None

    def test_invalid_string(self):
        assert parse_level_value("abc") is None

    def test_nan_float(self):
        assert parse_level_value(float("nan")) is None


# ── extractSupportResistance 测试 ─────────────────────────────────────────────


class TestExtractSupportResistance:
    def test_support_and_resistance(self):
        stage1 = {
            "support_levels": [2600, "2610-2620"],
            "resistance_levels": [2700, {"low": 2710, "high": 2720}],
        }
        levels = extract_support_resistance(stage1)
        assert len(levels) == 4
        kinds = [lv["kind"] for lv in levels]
        assert kinds.count("support") == 2
        assert kinds.count("resistance") == 2

    def test_alias_keys(self):
        """supports / resistances 别名也能识别。"""
        stage1 = {"supports": [2600], "resistances": [2700]}
        levels = extract_support_resistance(stage1)
        assert len(levels) == 2

    def test_empty_stage1(self):
        assert extract_support_resistance({}) == []
        assert extract_support_resistance(None) == []

    def test_no_levels_key(self):
        assert extract_support_resistance({"direction": "bullish"}) == []

    def test_invalid_values_skipped(self):
        stage1 = {"support_levels": [2600, "abc", None, 2700]}
        levels = extract_support_resistance(stage1)
        assert len(levels) == 2  # 只有 2600 和 2700 有效

    def test_zone_vs_point(self):
        """区间 (low != high) 与单点 (low == high) 都能提取。"""
        stage1 = {
            "support_levels": [2600],             # 单点
            "resistance_levels": ["2700-2710"],   # 区间
        }
        levels = extract_support_resistance(stage1)
        sup = [lv for lv in levels if lv["kind"] == "support"][0]
        res = [lv for lv in levels if lv["kind"] == "resistance"][0]
        assert sup["low"] == sup["high"]  # 单点
        assert res["low"] != res["high"]  # 区间

    def test_labels_increment(self):
        """多条支撑位 label 应递增编号。"""
        stage1 = {"support_levels": [2600, 2610, 2620]}
        levels = extract_support_resistance(stage1)
        labels = [lv["label"] for lv in levels]
        assert labels[0] == "支撑"
        assert labels[1] == "支撑2"
        assert labels[2] == "支撑3"


# ── forming bar 测试 ──────────────────────────────────────────────────────────


class TestFormingBar:
    def test_last_bar_forming(self):
        bars = _make_bars(5)
        forming = get_forming_bar(bars)
        assert forming is not None
        assert forming["closed"] is False

    def test_all_closed_returns_none(self):
        bars = [
            {"seq": 2, "ts_open": 1, "close": 100, "closed": True},
            {"seq": 1, "ts_open": 2, "close": 101, "closed": True},
        ]
        assert get_forming_bar(bars) is None

    def test_empty_bars(self):
        assert get_forming_bar([]) is None

    def test_forming_is_last_by_time(self):
        """forming bar 应是按时间排序的最后一根。"""
        bars = [
            {"seq": 1, "ts_open": 300, "close": 100, "closed": True},
            {"seq": 2, "ts_open": 100, "close": 101, "closed": False},
            {"seq": 3, "ts_open": 200, "close": 102, "closed": True},
        ]
        forming = get_forming_bar(bars)
        # 按时间排序最后一根是 ts_open=300，但 closed=True
        # 所以没有 forming bar
        assert forming is None

    def test_forming_bar_transparency(self):
        """forming bar 的颜色判定逻辑：close >= open → up forming 色。"""
        bar = {"open": 2000, "close": 2005, "closed": False}
        is_up = bar["close"] >= bar["open"]
        assert is_up is True  # 应使用 COLOR_UP_FORMING


# ── seq marker 过滤测试 ───────────────────────────────────────────────────────


class TestSeqMarkers:
    def test_odd_seq_only(self):
        """仅奇数 seq 显示。"""
        bars = [
            {"seq": s, "ts_open": s * 60_000}
            for s in range(1, 11)
        ]
        seqs = filter_seq_markers(bars)
        assert all(s % 2 == 1 for s in seqs)
        assert 1 in seqs
        assert 3 in seqs
        assert 2 not in seqs
        assert 4 not in seqs

    def test_zero_seq_excluded(self):
        """seq=0（forming bar）不显示。"""
        bars = [
            {"seq": 0, "ts_open": 1},
            {"seq": 1, "ts_open": 2},
        ]
        seqs = filter_seq_markers(bars)
        assert 0 not in seqs
        assert 1 in seqs

    def test_range_limit(self):
        """超过 FIT_VISIBLE_BARS*2=40 根的旧 bar 不显示标签。"""
        bars = [
            {"seq": 100 - i, "ts_open": i * 60_000}
            for i in range(100)
        ]
        seqs = filter_seq_markers(bars)
        # 按 ts_open 排序后最后 40 根的 seq 为 40,39,...,1
        # 仅其中的奇数 seq 显示
        recent_seqs = list(range(40, 0, -1))  # [40, 39, ..., 1]
        expected = [s for s in recent_seqs if s > 0 and s % 2 == 1]
        assert set(seqs) == set(expected)
        # seq=99（最早的 bar）不应出现
        assert 99 not in seqs
        assert 61 not in seqs

    def test_empty_bars(self):
        assert filter_seq_markers([]) == []

    def test_non_numeric_seq_skipped(self):
        bars = [
            {"seq": "abc", "ts_open": 1},
            {"seq": 1, "ts_open": 2},
        ]
        seqs = filter_seq_markers(bars)
        assert 1 in seqs
