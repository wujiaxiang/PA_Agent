"""程序化复盘（结算即算，不调 LLM）。

这一层存在的意义是**确定性**：同输入必同输出，所以可以写断言 ——
LLM 复盘永远写不了这种断言。
"""
from __future__ import annotations

import pytest

from pa_agent.records.experience_writer import resolve_exit
from pa_agent.records.review_program import (
    MAX_CRITERIA_CHARS, VERDICT_CONFIRMED, VERDICT_LUCKY as VERDICT_LUCK, VERDICT_UNTOUCHED,
    VERDICT_WRONG, VERDICTS, build_program_review,
)

ENTRY = {"entry_price": 100.0, "take_profit_price": 120.0, "stop_loss_price": 90.0}


def bar(ts, hi, lo):
    return {"ts_open": ts, "high": hi, "low": lo}


def review(status, bars, content=None):
    info = resolve_exit(bars, entry_price=100.0, take_profit_price=120.0,
                        stop_loss_price=90.0, is_long=True)
    return build_program_review(status=status, content=content or ENTRY, exit_info=info)


# ── 结论映射 ────────────────────────────────────────────────────────────────

def test_win_is_confirmed():
    assert review("win", [bar(1, 121, 101)])["verdict"] == VERDICT_CONFIRMED


def test_loss_with_profit_first_is_luck_not_blind_wrongness():
    """**回归守卫**：先浮盈过再被打回 ≠ 判断与走势相悖。

    这两类混为一谈是事后复盘最常见的错误：亏损单里浮盈过的那些，方向与入场
    往往并没有错，问题在 TP/SL 间距。把它们算成「判断错误」会让检索端据此
    回避本来正确的形态。
    """
    r = review("loss", [bar(1, 112, 101), bar(2, 89, 89)])
    assert r["verdict"] == VERDICT_LUCK
    assert r["mfe_pct"] == pytest.approx(12.0)


def test_loss_without_any_profit_is_wrong():
    assert review("loss", [bar(1, 100, 99), bar(2, 95, 89)])["verdict"] == VERDICT_WRONG


def test_unresolved_is_not_a_failure():
    """未触及不等于判断错 —— 它没有结论。"""
    assert review("unresolved", [bar(i, 101, 99) for i in range(1, 5)])["verdict"] == VERDICT_UNTOUCHED


def test_verdict_is_always_in_closed_vocabulary():
    """**回归守卫 / 注入面**：结论必须取自闭词表。

    这个字段会被渲染进决策提示词。若能夹带任意文本，等于给「复盘文本操纵
    后续推理」开了口子 —— 而复盘文本有一部分是模型生成的。
    """
    for status, bars in [
        ("win", [bar(1, 121, 101)]),
        ("loss", [bar(1, 112, 101), bar(2, 89, 89)]),
        ("loss", [bar(1, 89, 89)]),
        ("unresolved", [bar(1, 101, 99)]),
        ("pending", []),
    ]:
        assert review(status, bars)["verdict"] in VERDICTS


# ── MFE/MAE ─────────────────────────────────────────────────────────────────

def test_excursions_stop_at_the_exit_bar():
    """**回归守卫**：MFE 只统计到出场那根为止。

    拿入场后全部 K 线算最大浮盈，会把出场之后的行情算到这笔单头上：
    「止盈后回落」的 win 会显示成曾经浮盈 30%，「止损后反弹」的 loss 会显示
    成曾经盈利 —— 运气成分完全失真，而这正是程序化复盘要提供的核心事实。
    """
    bars = [bar(1, 101, 100), bar(2, 85, 84)]           # 第 2 根触及止损
    after = bars + [bar(3, 300, 300)]                  # 出场后暴涨，不该算进来
    info = resolve_exit(bars + after, entry_price=100.0, take_profit_price=120.0,
                        stop_loss_price=90.0, is_long=True)
    assert info["touched"] and info["result"] == "loss"
    assert info["mfe_pct"] == pytest.approx(1.0), "出场后的 300 不该被计入浮盈"


def test_unresolved_covers_every_bar():
    info = resolve_exit([bar(1, 105, 100), bar(2, 108, 103)], entry_price=100.0,
                        take_profit_price=120.0, stop_loss_price=90.0, is_long=True)
    assert info["touched"] is False and info["exit_index"] is None
    assert info["mfe_pct"] == pytest.approx(8.0)


def test_same_bar_hits_both_counts_as_stop():
    """同根同时触及按止损 —— 原有的保守规则不得被重构破坏。"""
    info = resolve_exit([bar(1, 125, 85)], entry_price=100.0, take_profit_price=120.0,
                        stop_loss_price=90.0, is_long=True)
    assert info["result"] == "loss"


# ── 数值与文本边界 ───────────────────────────────────────────────────────────

def test_realised_rr_uses_actual_exit_not_planned_tp():
    """实际盈亏比按**实际达成**算，不是按设定的 TP/SL。"""
    r = review("loss", [bar(1, 89, 89)])
    assert r["risk_pct"] == pytest.approx(10.0)
    assert r["reward_pct"] == pytest.approx(20.0)
    assert r["realised_rr"] == pytest.approx(-1.0), "计划 RR 是 +2.0，实际是 -1.0"


def test_criteria_is_bounded():
    r = review("unresolved", [bar(i, 101, 99) for i in range(1, 60)])
    assert len(r["reusable_criteria"]) <= MAX_CRITERIA_CHARS


def test_missing_bars_degrades_without_raising():
    """数据缺失时给最小可用结论，不得抛异常打断结算。"""
    r = build_program_review(status="pending", content=ENTRY, exit_info=None)
    assert r["verdict"] and r["mfe_pct"] == 0.0


def test_build_is_deterministic():
    bars = [bar(1, 112, 101), bar(2, 89, 89)]
    assert review("loss", bars) == review("loss", bars)
