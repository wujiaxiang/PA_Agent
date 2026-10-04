"""枚举中英标签：经验库 / 决策面板展示层。"""
from __future__ import annotations

import pytest

from pa_agent.ai.display_labels import (
    bilingual_case_type,
    bilingual_cycle,
    bilingual_direction,
    bilingual_result,
    label_for,
)


@pytest.mark.parametrize("raw,zh,expected", [
    ("trending_tr", "趋势型交易区间", "趋势型交易区间 (trending_tr)"),
    ("broad_channel", "宽通道", "宽通道 (broad_channel)"),
    ("spike", "尖峰", "尖峰 (spike)"),
])
def test_cycle_labels_are_bilingual(raw, zh, expected):
    assert bilingual_cycle(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("up", "上涨 (up)"),
    ("down", "下跌 (down)"),
    ("neutral", "中性 (neutral)"),
    # 决策侧可能出现的别名也必须能翻译
    ("long", "上涨 (long)"),
    ("bearish", "下跌 (bearish)"),
])
def test_direction_labels_are_bilingual(raw, expected):
    assert bilingual_direction(raw) == expected


def test_result_and_case_type_labels():
    assert bilingual_result("win") == "盈利 (win)"
    assert bilingual_result("loss") == "亏损 (loss)"
    assert bilingual_case_type("success") == "盈利 (success)"
    assert bilingual_case_type("failure") == "亏损 (failure)"


def test_empty_value_returns_empty_string():
    """空值必须返回 '' —— 模板要能整块隐藏，而不是渲染出「未知 ()」。"""
    for fn in (bilingual_cycle, bilingual_direction, bilingual_result, bilingual_case_type):
        assert fn("") == ""
        assert fn(None) == ""


def test_unknown_enum_falls_back_to_raw():
    assert bilingual_cycle("some_new_cycle") == "some_new_cycle"


def test_label_for_searches_all_tables():
    assert label_for("trending_tr") == "趋势型交易区间 (trending_tr)"
    assert label_for("up") == "上涨 (up)"


def test_labels_are_case_insensitive():
    assert bilingual_cycle("TRENDING_TR") == "趋势型交易区间 (TRENDING_TR)"
