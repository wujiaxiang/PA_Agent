"""Demo mode route — provides sample data for UI体验 without real data source.

The payload is produced by building a real :class:`AnalysisRecord` and running it
through ``_serialize_record``, so the demo can never drift from the shape the
frontend renderers actually consume. The previous hand-written dict nested
``stage2_decision.decision``, carried ``market_structure.trend_direction`` and a
single ``probability`` for the cycle forecast, and set
``decision_tree.terminal = True`` — every one of which made the UI fall back to
"不下单" / "方向：中性" with empty probability chips.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

router = APIRouter(tags=["demo"])


def _generate_kline_data(count: int = 50) -> list[dict]:
    """生成模拟 K 线数据（newest-first），最近 count 天。"""
    bars = []
    now = datetime.now(timezone.utc)
    base_price = 65000.0  # BTC 基准价格

    for i in range(count):
        bar_time = now - timedelta(days=i)
        ts_open = int(bar_time.timestamp() * 1000)

        change_pct = random.uniform(-0.05, 0.05)
        open_price = base_price * (1 + change_pct)
        close_price = open_price * (1 + random.uniform(-0.03, 0.03))
        high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.02))
        low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.02))
        volume = random.uniform(1000, 5000)

        bars.append({
            "seq": count - i,
            "ts_open": ts_open,
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": round(volume, 2),
            "closed": i > 0,  # 最新的 bar 未收盘
        })

        base_price = close_price

    return bars


def _build_demo_record():
    """构建完整的 Demo 分析记录，并按真实序列化输出。"""
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    kline_data = _generate_kline_data(50)
    now = datetime.now(timezone.utc)
    last_bar_time = now - timedelta(days=1)
    entry = kline_data[1]["close"]

    meta = RecordMeta(
        timestamp_local_iso=now.isoformat(),
        timestamp_local_ms=int(now.timestamp() * 1000),
        symbol="BTCUSDT",
        timeframe="1d",
        exchange="GATEIO",
        bar_count=50,
        ai_provider={"model": "demo", "base_url": "(demo)"},
        decision_stance="balanced",
        last_close_bar_iso=last_bar_time.isoformat(),
    )

    stage1_diagnosis = {
        "cycle_position": "trending_tr",
        "alternative_cycle_position": "trading_range",
        "direction": "bullish",
        "diagnosis_confidence": 72,
        "spike_stage": None,
        "climax_risk": "none",
        "market_phase": "stable",
        "transition_risk": "low",
        "detected_patterns": ["breakout_failure", "pullback"],
        "key_signals": [
            "价格在 20 日均线上方运行，中期趋势偏多",
            "MACD 金叉后持续发散，动能增强",
            "RSI 处于 55-65 区间，未超买",
            "成交量温和放大，资金持续流入",
        ],
        "htf_context": "日线级别结构完整，回调不破前低，上升趋势延续概率较高。",
        "entry_setup": "等待回踩前高支撑位附近出现顺势信号棒再入场。",
        "support_levels": [round(entry * 0.985, 2), round(entry * 0.975, 2)],
        "resistance_levels": [round(entry * 1.015, 2), round(entry * 1.03, 2)],
        "strategy_files_needed": [],
        "risk_warning": "",
        "bar_analysis": {
            "always_in": "long",
            "last_closed_bar": "K1",
            "bar_type": "trend_bull",
            "signal_bar": {"bar": "K1", "quality": "strong", "pattern": "bull_flag", "reason": "顺势信号棒"},
            "entry_bar": {"bar": "K1", "strength": "strong", "follow_through": True,
                          "still_valid": True, "freshness": "fresh"},
            "second_entry": {"is_second_entry": False, "type": "none"},
        },
        "bar_by_bar_summary": [],
        "gate_trace": ["数据验证通过", "市场结构分析完成"],
        "gate_result": "proceed",
        "gate_shortcircuited": False,
    }

    stage2_decision = {
        "decision": {
            "order_type": "限价单",
            "order_direction": "做多",
            "entry_price": entry,
            "stop_loss_price": round(entry * 0.97, 2),
            "take_profit_price": round(entry * 1.06, 2),
            "take_profit_price_2": round(entry * 1.10, 2),
            "entry_basis_bar": "K1",
            "entry_basis_extreme": "high",
            "entry_rule": "回踩支撑位挂限价单",
            "reasoning": "中期上升趋势明确，建议逢低做多",
            "diagnosis_confidence": 72,
            "diagnosis_confidence_reasoning": "周期与方向均有结构确认",
            "trade_confidence": 68,
            "trade_confidence_reasoning": "趋势延续概率较高，但需等待回踩确认",
            "estimated_win_rate": 58,
            "estimated_win_rate_reasoning": "顺势环境下胜率略高于五成",
            "key_factors": ["趋势向上", "回踩支撑", "动能增强"],
            "watch_points": ["跌破支撑则结构失效"],
            "risk_assessment": "止损位于结构下方，风险可控",
            "invalidation_condition": "收盘跌破前低则计划失效",
        },
        "diagnosis_summary": {
            "cycle_position": "trending_tr",
            "direction": "bullish",
            "key_signals": stage1_diagnosis["key_signals"],
        },
        "bar_analysis": stage1_diagnosis["bar_analysis"],
        # 决策树终点：必须是对象（app.js 读 .outcome/.label/.node_id），
        # 之前这里写成 True，渲染出来是 "proceed" 的空串。
        "terminal": {
            "node_id": "10.3",
            "outcome": "trade",
            "label": "顺势限价做多，止损置于结构下方",
        },
        "decision_trace": [
            {"node_id": "9.0", "answer": "是", "reason": "K1 为顺势信号棒"},
            {"node_id": "10.1", "answer": "是", "reason": "止损置于结构失效位"},
            {"node_id": "10.3", "answer": "是", "reason": "盈亏比满足均衡档位"},
        ],
        "gate_shortcircuited": False,
        # 前端读 probabilities 对象，单个 probability 会让所有概率芯片显示 0%。
        "next_bar_prediction": {
            "direction": "bullish",
            "probabilities": {"bullish": 58, "bearish": 24, "neutral": 18},
            "reasoning": "多头动能持续，预计延续上涨",
        },
        "next_cycle_prediction": {
            "cycle": "broad_channel",
            "direction": "bullish",
            "probabilities": {
                "spike": 3, "micro_channel": 5, "tight_channel": 8,
                "normal_channel": 20, "broad_channel": 35,
                "trending_tr": 15, "trading_range": 10, "extreme_tr": 4,
            },
            "reasoning": "延续当前结构的概率最高，但需警惕转换期",
        },
    }

    usage = {
        "prompt_tokens": 3500,
        "completion_tokens": 1400,
        "total_tokens": 4900,
    }

    record = AnalysisRecord(
        meta=meta,
        kline_data=kline_data,
        htf_text="",
        stage1_messages=[],
        stage1_response={"content": "分析完成", "usage": usage},
        stage1_diagnosis=stage1_diagnosis,
        stage2_messages=[],
        stage2_response={"content": "决策生成完成", "usage": usage},
        stage2_decision=stage2_decision,
        strategy_files_used=["market_structure.txt", "trend_analysis.txt",
                             "risk_management.txt", "entry_strategy.txt"],
        experience_loaded=[],
        exception=None,
        usage_total=usage,
    )

    # 复用真实序列化，确保 demo 与 _serialize_record 的输出契约完全一致
    from web.api.routes_analyze import _serialize_record

    payload = _serialize_record(record)

    # 仅覆盖序列化之外的、由 demo 路由负责的调试面板字段
    payload["raw_debug_payload"] = {
        "stage1_system_prompt": "你是专业的加密货币交易分析师...",
        "stage1_user_prompt": "请分析 BTCUSDT 日线数据...",
        "stage1_raw_response": {"content": "分析完成", "usage": usage},
        "stage2_system_prompt": "基于阶段一诊断，生成交易决策...",
        "stage2_user_prompt": "请给出具体交易建议...",
        "stage2_raw_response": {"content": "决策生成完成", "usage": usage},
        "validation": {
            "stage1_valid": True,
            "stage2_valid": True,
            "stage1_missing_fields": [],
            "stage2_missing_fields": [],
            "stage1_invalid_fields": [],
            "stage2_invalid_fields": [],
        },
        "exception": None,
        "stage1_cache_hit_pct": 85.0,
        "stage2_cache_hit_pct": 92.0,
    }
    payload["debug_files_payload"] = {
        "stage1_files": ["market_structure.txt", "trend_analysis.txt"],
        "stage2_files": ["risk_management.txt", "entry_strategy.txt"],
        "experience_loaded": [],
        "experience_count": {"success": 0, "failure": 0},
    }
    return payload


@router.get("/demo/sample")
async def get_demo_sample():
    """返回 Demo 分析记录，用于 UI 体验模式。"""
    return _build_demo_record()