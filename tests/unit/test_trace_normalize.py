"""Tests for gate/decision trace normalization."""
from __future__ import annotations

import json

from pa_agent.ai.json_validator import JsonValidator, Ok
from pa_agent.ai.stage2_normalizer import normalize_stage2
from pa_agent.ai.trace_normalize import (
    fix_bar_range_string,
    normalize_trace_item,
    normalize_trace_list,
)
from tests.integration.conftest import VALID_STAGE2


def test_fix_reversed_bar_range() -> None:
    assert fix_bar_range_string("K1-K4") == "K4-K1"
    assert fix_bar_range_string("K50-K1") == "K50-K1"


def test_global_bar_range_uses_trace_max() -> None:
    item = {"node_id": "14.1", "answer": "通过", "bar_range": "全局"}
    normalize_trace_item(
        item,
        default_max_seq=100,
    )
    assert item["answer"] == "是"
    assert item["bar_range"] == "K100-K1"


def test_node_63_composite_boundary_answer() -> None:
    item = {
        "node_id": "6.3",
        "question": "当前价格是否在区间边界？",
        "answer": "是，在下边界",
        "reason": "x",
        "bar_range": "K5-K1",
    }
    normalize_trace_item(item)
    assert item["answer"] == "是"
    assert item["branch"] == "lower"


def test_node_62_trending_tr_answer() -> None:
    item = {
        "node_id": "6.2",
        "question": "区间类型",
        "answer": "趋势型交易区间",
        "reason": "x",
        "bar_range": "K25-K1",
    }
    normalize_trace_item(item)
    assert item["answer"] == "是"
    assert item["branch"] == "trending_tr"


def test_node_42_directional_answer() -> None:
    item = {
        "node_id": "4.2",
        "question": "通道方向",
        "answer": "下跌",
        "reason": "x",
        "bar_range": "K100-K1",
    }
    normalize_trace_item(item)
    assert item["answer"] == "是"
    assert item["branch"] == "bearish"


def test_validator_accepts_user_trending_tr_trace() -> None:
    """Regression: AI wrote 是，在下边界 / 趋势型交易区间 as answer."""
    payload = normalize_stage2(
        {
            **VALID_STAGE2,
            "decision": {
                **VALID_STAGE2["decision"],
                "order_type": "不下单",
                "order_direction": None,
                "entry_price": None,
                "take_profit_price": None,
                "stop_loss_price": None,
            },
            "decision_trace": [
                {
                    "node_id": "6.2",
                    "question": "是普通交易区间还是趋势型交易区间？",
                    "answer": "趋势型交易区间",
                    "reason": "EMA下倾",
                    "bar_range": "K25-K1",
                },
                {
                    "node_id": "6.3",
                    "question": "当前价格是否在区间边界？",
                    "answer": "是，在下边界",
                    "reason": "近下边界",
                    "bar_range": "K5-K1",
                },
            ],
            "terminal": {
                "node_id": "9.1",
                "outcome": "wait",
                "label": "等待信号",
            },
        }
    )
    result = JsonValidator().validate("stage2", json.dumps(payload, ensure_ascii=False))
    assert isinstance(result, Ok)


def test_null_bar_range_on_skipped_nodes() -> None:
    trace = [
        {
            "node_id": "10.1",
            "question": "是否能明确止损？",
            "answer": "不适用",
            "reason": "无交易计划",
            "skipped": True,
            "bar_range": None,
        },
        {
            "node_id": "10.2",
            "question": "止损是否过大？",
            "answer": "不适用",
            "reason": "无交易计划",
            "skipped": True,
            "bar_range": None,
        },
    ]
    normalize_trace_list(trace, default_max_seq=48)
    assert trace[0]["bar_range"] == "不适用"
    assert trace[1]["bar_range"] == "不适用"


def test_null_bar_range_inherits_prior_range() -> None:
    trace = [
        {
            "node_id": "9.1",
            "question": "信号？",
            "answer": "否",
            "reason": "x",
            "bar_range": "K48-K1",
        },
        {
            "node_id": "5.1",
            "question": "微型通道？",
            "answer": "否",
            "reason": "回撤大",
            "bar_range": None,
        },
    ]
    normalize_trace_list(trace, default_max_seq=48)
    assert trace[1]["bar_range"] == "K48-K1"


def test_validator_accepts_user_payload_with_null_bar_ranges() -> None:
    """Regression: skipped §10/§11 nodes had bar_range: null."""
    payload = normalize_stage2(
        {
            **VALID_STAGE2,
            "decision": {
                **VALID_STAGE2["decision"],
                "order_type": "不下单",
                "order_direction": None,
                "entry_price": None,
                "take_profit_price": None,
                "stop_loss_price": None,
            },
            "decision_trace": [
                {
                    "node_id": "4.2",
                    "question": "通道方向是上涨还是下跌？",
                    "answer": "下跌",
                    "reason": "LL+LH",
                    "bar_range": "K48-K1",
                },
                {
                    "node_id": "10.1",
                    "question": "是否能明确止损？",
                    "answer": "不适用",
                    "reason": "无交易计划",
                    "skipped": True,
                    "bar_range": None,
                },
                {
                    "node_id": "10.3",
                    "question": "交易者方程是否通过？",
                    "answer": "不适用",
                    "reason": "无交易计划",
                    "skipped": True,
                    "bar_range": None,
                },
            ],
            "terminal": {
                "node_id": "9.1",
                "outcome": "wait",
                "label": "等待有效信号",
            },
        }
    )
    result = JsonValidator().validate("stage2", json.dumps(payload, ensure_ascii=False))
    assert isinstance(result, Ok)
    assert result.obj["decision_trace"][1]["bar_range"] == "不适用"


def test_validator_accepts_normalized_user_stage2_snippet() -> None:
    base = normalize_stage2(
        {
            **VALID_STAGE2,
            "decision": {
                **VALID_STAGE2["decision"],
                "order_type": "不下单",
                "order_direction": None,
                "entry_price": None,
                "take_profit_price": None,
                "stop_loss_price": None,
            },
            "decision_trace": [
                {
                    "node_id": "4.2",
                    "question": "通道方向是上涨还是下跌？",
                    "answer": "下跌",
                    "reason": "LL+LH",
                    "bar_range": "K100-K1",
                },
                {
                    "node_id": "9.4",
                    "question": "是否是第一次入场？",
                    "answer": "是",
                    "reason": "Low1",
                    "bar_range": "K1-K4",
                },
                {
                    "node_id": "14.1",
                    "question": "禁止行为清单扫描",
                    "answer": "通过",
                    "reason": "ok",
                    "bar_range": "全局",
                },
            ],
            "terminal": {
                "node_id": "10.3",
                "outcome": "reject",
                "label": "交易者方程未通过",
            },
        }
    )
    result = JsonValidator().validate("stage2", json.dumps(base, ensure_ascii=False))
    assert isinstance(result, Ok)


def test_validator_accepts_stage2_with_null_bar_range_and_forbid_phrase() -> None:
    """Regression: null bar_range, missing reason, §14 answer typo, node_id '14'."""
    payload = normalize_stage2(
        {
            **VALID_STAGE2,
            "decision": {
                **VALID_STAGE2["decision"],
                "order_type": "不下单",
                "order_direction": None,
                "entry_price": None,
                "take_profit_price": None,
                "stop_loss_price": None,
            },
            "decision_trace": [
                {
                    "node_id": "9.3",
                    "section": "入场信号",
                    "question": "信号棒是否过长？",
                    "answer": "不适用",
                    "skipped": True,
                    "bar_range": None,
                },
                {
                    "node_id": "14",
                    "section": "禁止行为",
                    "question": "是否触犯禁止行为？",
                    "answer": "无交易计划，不存在触犯",
                    "reason": "未触发入场",
                    "skipped": True,
                    "bar_range": None,
                },
            ],
            "terminal": {
                "node_id": "9.1",
                "outcome": "wait",
                "label": "等待",
            },
        }
    )
    result = JsonValidator().validate("stage2", json.dumps(payload, ensure_ascii=False))
    assert isinstance(result, Ok)
    trace = result.obj["decision_trace"]
    assert trace[0]["bar_range"] == "不适用"
    assert trace[0]["reason"] == "—"
    assert trace[1]["node_id"] == "14.1"
    assert trace[1]["answer"] == "否"
    assert trace[1]["bar_range"] == "不适用"
