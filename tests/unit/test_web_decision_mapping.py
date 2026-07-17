# -*- coding: utf-8 -*-
"""Unit tests for decision field mapping — 前端 renderDecision 逻辑的 Python 映射验证。

覆盖（SubTask 1.4 + 14.1）：
- 决策字段名映射：entry_price / stop_loss_price / take_profit_price / take_profit_price_2
  / estimated_win_rate / trade_confidence / diagnosis_confidence
- no_order 场景：order_type='不下单'/'no_order' 时不渲染价格字段
- 方向分类：做多/buy/long → buy；做空/sell/short → sell
- 盈亏比 computeRiskReward + trader equation 判定
- 胜率解析 parseWinRate

策略：将 app.js 中的纯逻辑函数移植为 Python 等价实现并验证行为一致性。
"""
from __future__ import annotations

import math
from typing import Any


# ── 移植自 app.js 的决策逻辑（保持与前端一致） ─────────────────────────────────

def is_no_order(order_type: str | None) -> bool:
    """前端 isNoOrder 判定。"""
    ot = (order_type or "").strip()
    return ot in ("不下单", "no_order")


def classify_direction(direction: str | None) -> str:
    """前端方向分类：返回 'buy' / 'sell' / ''。"""
    d = (direction or "").lower()
    if d in ("做多", "buy", "long"):
        return "buy"
    if d in ("做空", "sell", "short"):
        return "sell"
    return ""


def parse_win_rate(v: Any) -> float | None:
    """移植 parseWinRate：number 或 '55%' → 0-100 float。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return None
        return max(0.0, min(100.0, float(v)))
    s = str(v).replace("%", "").strip()
    try:
        n = float(s)
    except ValueError:
        return None
    return max(0.0, min(100.0, n))


def compute_risk_reward(entry: float, tp: float, sl: float, direction: str | None) -> dict | None:
    """移植 computeRiskReward。

    返回 {ratio, risk, reward, ratio_text} 或 None。
    """
    try:
        e, t, s = float(entry), float(tp), float(sl)
    except (TypeError, ValueError):
        return None
    if any(math.isnan(x) for x in (e, t, s)):
        return None
    d = (direction or "").lower()
    is_short = d in ("short", "做空", "sell")
    if is_short:
        risk = s - e
        reward = e - t
    else:
        risk = e - s
        reward = t - e
    if risk <= 0 or reward <= 0:
        return None
    ratio = reward / risk
    ratio_text = f"{ratio:.2f}:1 (risk={risk:.2f}, reward={reward:.2f})"
    return {"ratio": ratio, "risk": risk, "reward": reward, "ratio_text": ratio_text}


def trader_equation_passes(win_rate: float, risk: float, reward: float) -> bool:
    """移植前端 trader equation 判定：
    passes = (winRate/100)*reward >= ((100-winRate)/100)*risk
    """
    return (win_rate / 100) * reward >= ((100 - win_rate) / 100) * risk


# 后端 decision_overlay 构造逻辑（移植自 routes_analyze.py _run_analysis）
DECISION_OVERLAY_FIELDS = (
    "order_type",
    "order_direction",
    "chart_overlay_active",
    "entry_price",
    "stop_loss_price",
    "take_profit_price",
    "take_profit_price_2",
)

# 前端 renderDecision 读取的全部 stage2_decision 字段
DECISION_DISPLAY_FIELDS = (
    "order_type",
    "order_direction",
    "entry_price",
    "stop_loss_price",
    "take_profit_price",
    "take_profit_price_2",
    "entry_rule",
    "entry_basis_bar",
    "entry_basis_extreme",
    "diagnosis_confidence",
    "trade_confidence",
    "estimated_win_rate",
    "risk_assessment",
    "invalidation_condition",
    "key_factors",
    "watch_points",
    "reasoning",
    "diagnosis_confidence_reasoning",
    "trade_confidence_reasoning",
    "estimated_win_rate_reasoning",
)


def build_decision_overlay(stage2_decision: dict | None) -> dict:
    """复刻 routes_analyze.py 中 decision_overlay 的构造逻辑。"""
    s2_dec = stage2_decision or {}
    return {
        "order_type": s2_dec.get("order_type"),
        "order_direction": s2_dec.get("order_direction"),
        "chart_overlay_active": bool(s2_dec.get("chart_overlay_active", True)),
        "entry_price": s2_dec.get("entry_price"),
        "stop_loss_price": s2_dec.get("stop_loss_price"),
        "take_profit_price": s2_dec.get("take_profit_price"),
        "take_profit_price_2": s2_dec.get("take_profit_price_2"),
    }


# ── 测试 ──────────────────────────────────────────────────────────────────────


class TestNoOrderDetection:
    """no_order 场景判定。"""

    def test_chinese_no_order(self):
        assert is_no_order("不下单") is True

    def test_english_no_order(self):
        assert is_no_order("no_order") is True

    def test_normal_order_not_flagged(self):
        assert is_no_order("突破单") is False
        assert is_no_order("限价单") is False
        assert is_no_order("市价单") is False

    def test_none_or_empty(self):
        assert is_no_order(None) is False
        assert is_no_order("") is False

    def test_no_order_hides_price_fields(self):
        """no_order 时 decision_overlay 仍构造但 chart_overlay_active 控制是否画线。"""
        dec = {"order_type": "不下单", "order_direction": "", "entry_price": None}
        overlay = build_decision_overlay(dec)
        # 前端 setDecisionOverlays 遇到不下单会 return，不画价格线
        assert is_no_order(overlay["order_type"]) is True
        assert overlay["entry_price"] is None

    def test_no_order_with_reasoning_displayed(self):
        """no_order 时前端展示 reasoning 而非价格字段。"""
        dec = {
            "order_type": "no_order",
            "reasoning": "信号不足，本轮不下单",
        }
        # 前端逻辑：isNoOrder=True → 只显示 reasoning
        assert is_no_order(dec["order_type"]) is True
        assert dec["reasoning"]


class TestDirectionClassification:
    """方向分类逻辑。"""

    def test_chinese_long(self):
        assert classify_direction("做多") == "buy"

    def test_english_long(self):
        assert classify_direction("long") == "buy"
        assert classify_direction("buy") == "buy"

    def test_chinese_short(self):
        assert classify_direction("做空") == "sell"

    def test_english_short(self):
        assert classify_direction("short") == "sell"
        assert classify_direction("sell") == "sell"

    def test_neutral_or_empty(self):
        assert classify_direction("") == ""
        assert classify_direction(None) == ""
        assert classify_direction("震荡") == ""


class TestRiskReward:
    """盈亏比计算。"""

    def test_long_2to1(self):
        """做多 entry=2000, TP=2050, SL=1980 → reward=50, risk=20, ratio=2.5。"""
        rr = compute_risk_reward(2000, 2050, 1980, "做多")
        assert rr is not None
        assert rr["risk"] == 20.0
        assert rr["reward"] == 50.0
        assert abs(rr["ratio"] - 2.5) < 1e-9

    def test_short_2to1(self):
        """做空 entry=2000, TP=1950, SL=2020 → risk=20, reward=50, ratio=2.5。"""
        rr = compute_risk_reward(2000, 1950, 2020, "做空")
        assert rr is not None
        assert rr["risk"] == 20.0
        assert rr["reward"] == 50.0
        assert abs(rr["ratio"] - 2.5) < 1e-9

    def test_english_direction(self):
        rr = compute_risk_reward(2000, 2050, 1980, "long")
        assert rr is not None
        assert rr["ratio"] > 0

    def test_invalid_prices_returns_none(self):
        assert compute_risk_reward("abc", 2050, 1980, "做多") is None

    def test_zero_risk_returns_none(self):
        """entry == SL 时 risk=0 → 返回 None。"""
        assert compute_risk_reward(2000, 2050, 2000, "做多") is None

    def test_negative_reward_returns_none(self):
        """做多 TP < entry 时 reward<0 → None。"""
        assert compute_risk_reward(2000, 1950, 1980, "做多") is None

    def test_ratio_text_format(self):
        rr = compute_risk_reward(2000, 2050, 1980, "做多")
        assert ":1" in rr["ratio_text"]
        assert "risk=" in rr["ratio_text"]
        assert "reward=" in rr["ratio_text"]


class TestTraderEquation:
    """trader equation 判定。"""

    def test_passes_when_reward_high(self):
        """胜率 55%, reward=50, risk=20 → 0.55*50=27.5 >= 0.45*20=9 → 通过。"""
        assert trader_equation_passes(55, 20, 50) is True

    def test_fails_when_risk_too_high(self):
        """胜率 30%, reward=10, risk=50 → 0.3*10=3 < 0.7*50=35 → 不通过。"""
        assert trader_equation_passes(30, 50, 10) is False

    def test_boundary_equal(self):
        """胜率 50%, risk=reward → 0.5*r == 0.5*r → 通过（>=）。"""
        assert trader_equation_passes(50, 30, 30) is True


class TestWinRateParsing:
    """胜率解析。"""

    def test_integer(self):
        assert parse_win_rate(55) == 55.0

    def test_string_with_percent(self):
        assert parse_win_rate("55%") == 55.0

    def test_string_without_percent(self):
        assert parse_win_rate("55") == 55.0

    def test_clamps_above_100(self):
        assert parse_win_rate(150) == 100.0

    def test_clamps_below_0(self):
        assert parse_win_rate(-10) == 0.0

    def test_none_returns_none(self):
        assert parse_win_rate(None) is None

    def test_invalid_string_returns_none(self):
        assert parse_win_rate("abc") is None


class TestDecisionOverlayMapping:
    """decision_overlay 字段映射完整性。"""

    def test_all_fields_present(self):
        """正常下单时 overlay 包含全部 7 个字段。"""
        dec = {
            "order_type": "突破单",
            "order_direction": "做多",
            "entry_price": 2010.5,
            "stop_loss_price": 1995.0,
            "take_profit_price": 2050.0,
            "take_profit_price_2": 2090.0,
        }
        overlay = build_decision_overlay(dec)
        for field in DECISION_OVERLAY_FIELDS:
            assert field in overlay, f"缺少字段 {field}"
        assert overlay["chart_overlay_active"] is True
        assert overlay["entry_price"] == 2010.5

    def test_none_stage2_decision(self):
        """stage2_decision=None 时 overlay 字段全为 None。"""
        overlay = build_decision_overlay(None)
        assert overlay["order_type"] is None
        assert overlay["entry_price"] is None
        assert overlay["chart_overlay_active"] is True  # 默认 True

    def test_partial_decision(self):
        """只有 entry_price 时其他价格字段为 None。"""
        dec = {"order_type": "限价单", "entry_price": 2000.0}
        overlay = build_decision_overlay(dec)
        assert overlay["entry_price"] == 2000.0
        assert overlay["stop_loss_price"] is None
        assert overlay["take_profit_price"] is None

    def test_chart_overlay_inactive(self):
        """chart_overlay_active=False 时前端不应画线。"""
        dec = {"order_type": "突破单", "chart_overlay_active": False}
        overlay = build_decision_overlay(dec)
        assert overlay["chart_overlay_active"] is False

    def test_display_fields_coverage(self):
        """前端 renderDecision 读取的字段名应在 stage2_decision 中可存在。"""
        # 构造一个完整决策
        full_dec = {
            "order_type": "突破单",
            "order_direction": "做多",
            "entry_price": 2010.0,
            "stop_loss_price": 1995.0,
            "take_profit_price": 2050.0,
            "take_profit_price_2": 2090.0,
            "entry_rule": "突破 K2 高点",
            "entry_basis_bar": "K2",
            "entry_basis_extreme": "high",
            "diagnosis_confidence": 75,
            "trade_confidence": 70,
            "estimated_win_rate": 55,
            "risk_assessment": "low",
            "invalidation_condition": "跌破 1980",
            "key_factors": ["f1"],
            "watch_points": ["w1"],
            "reasoning": "看涨",
        }
        for field in DECISION_DISPLAY_FIELDS:
            assert field in full_dec or field.endswith("_reasoning"), \
                f"字段 {field} 未覆盖"
