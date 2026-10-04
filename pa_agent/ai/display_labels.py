"""Bilingual (中文 + English) labels for enum-valued fields.

Experience entries persist enums as English snake_case (``trending_tr``,
``up``, ``win``). The UI is Chinese, but showing only a translated label hides
the raw value that appears in prompts, records and file paths — and showing only
the raw value is unreadable to the actual operator. So: ``中文 (raw)``.

Qt-free on purpose (AGENTS.md layering rule): reused by the web layer.
"""
from __future__ import annotations

from typing import Any

#: 市场周期 / cycle position
CYCLE_POSITION_LABELS: dict[str, str] = {
    "trending_tr": "趋势型交易区间",
    "trading_range": "普通交易区间",
    "broad_channel": "宽通道",
    "normal_channel": "正常通道",
    "tight_channel": "窄通道",
    "micro_channel": "微型通道",
    "spike": "尖峰",
    "extreme_tr": "极端交易区间",
    "unknown": "未知周期",
}

#: 方向。经验条目实际写入的是 up/down/neutral；AI 决策侧另有
#: bullish/bearish/long/short/bull/bear，一并覆盖以防来源不同。
DIRECTION_LABELS: dict[str, str] = {
    "up": "上涨", "long": "上涨", "bullish": "上涨", "bull": "上涨",
    "down": "下跌", "short": "下跌", "bearish": "下跌", "bear": "下跌",
    "neutral": "中性", "none": "中性",
}

#: 经验结果
RESULT_LABELS: dict[str, str] = {
    "win": "盈利", "loss": "亏损",
}

#: 案例类型（目录名 success_cases / failure_cases）
CASE_TYPE_LABELS: dict[str, str] = {
    "success": "盈利", "failure": "亏损",
}

#: 订单类型（与 pa_agent.ai.order_opportunity.ORDER_OPPORTUNITY_TYPES 对齐）
ORDER_TYPE_LABELS: dict[str, str] = {
    "限价单": "限价单", "市价单": "市价单", "突破单": "突破单",
    "限价止损单": "限价止损单", "市价止损单": "市价止损单",
}

_ALL_TABLES: tuple[dict[str, str], ...] = (
    CYCLE_POSITION_LABELS,
    DIRECTION_LABELS,
    RESULT_LABELS,
    CASE_TYPE_LABELS,
)


def label_for(value: Any, table: dict[str, str] | None = None) -> str:
    """Return ``中文 (raw)`` for *value*, falling back to the raw string.

    An empty/unknown value yields ``""`` so templates can hide the field
    entirely rather than rendering a stray ``未知 ()``.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if table is not None:
        zh = table.get(raw) or table.get(raw.lower())
        return f"{zh} ({raw})" if zh else raw
    for tbl in _ALL_TABLES:
        zh = tbl.get(raw) or tbl.get(raw.lower())
        if zh:
            return f"{zh} ({raw})"
    return raw


def bilingual_cycle(value: Any) -> str:
    return label_for(value, CYCLE_POSITION_LABELS)


def bilingual_direction(value: Any) -> str:
    return label_for(value, DIRECTION_LABELS)


def bilingual_result(value: Any) -> str:
    return label_for(value, RESULT_LABELS)


def bilingual_case_type(value: Any) -> str:
    return label_for(value, CASE_TYPE_LABELS)


def bilingual_order_type(value: Any) -> str:
    """Order types are already Chinese in the pipeline; just pass them through."""
    raw = str(value or "").strip()
    return raw
