"""Demo mode route — provides sample data for UI体验 without real data source."""
from __future__ import annotations

import random
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter

router = APIRouter(tags=["demo"])


def _generate_kline_data(count: int = 50) -> list[dict]:
    """生成模拟 K 线数据（newest-first），最近 count 天。"""
    bars = []
    now = datetime.now(timezone.utc)
    base_price = 65000.0  # BTC 基准价格

    for i in range(count):
        # 计算时间戳（每天一根，从当前往前推）
        bar_time = now - timedelta(days=i)
        ts_open = int(bar_time.timestamp() * 1000)

        # 生成随机价格波动
        change_pct = random.uniform(-0.05, 0.05)
        open_price = base_price * (1 + change_pct)
        close_price = open_price * (1 + random.uniform(-0.03, 0.03))
        high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.02))
        low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.02))
        volume = random.uniform(1000, 5000)

        bars.append({
            "ts_open": ts_open,
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": round(volume, 2),
            "closed": i > 0,  # 最新的 bar 未收盘
            "seq": count - i,
        })

        base_price = close_price  # 下一根基于当前收盘价

    return bars


def _build_demo_record() -> dict:
    """构建完整的 Demo 分析记录。"""
    kline_data = _generate_kline_data(50)
    now = datetime.now(timezone.utc)
    last_bar_time = now - timedelta(days=1)

    return {
        "symbol": "BTCUSDT",
        "exchange": "GATEIO",
        "timeframe": "1d",
        "stage1_diagnosis": {
            "bar_analysis": {
                "last_closed_bar": last_bar_time.isoformat(),
                "bar_count": 50,
            },
            "market_structure": {
                "trend_direction": "上升",
                "cycle_position": "trending_tr",
            },
            "key_observations": [
                "价格在 20 日均线上方运行，中期趋势偏多",
                "MACD 金叉后持续发散，动能增强",
                "RSI 处于 55-65 区间，未超买",
                "成交量温和放大，资金持续流入",
            ],
        },
        "stage2_decision": {
            "decision": {
                "order_type": "限价单",
                "order_direction": "long",
                "entry_price": kline_data[1]["close"],  # 前一根收盘价
                "stop_loss": round(kline_data[1]["close"] * 0.97, 2),
                "take_profit": round(kline_data[1]["close"] * 1.06, 2),
                "take_profit_price_2": round(kline_data[1]["close"] * 1.10, 2),
            },
            "diagnosis_summary": "中期上升趋势明确，建议逢低做多",
            "confidence_scores": {
                "trend": 75,
                "momentum": 70,
                "volume": 65,
                "structure": 80,
            },
            "trade_confidence": 72,
            "reasoning": "日线级别上升趋势延续，MACD 多头动能增强，RSI 健康区间。建议在前低支撑位附近入场，止损设在关键支撑下方。",
            "next_bar_prediction": {
                "direction": "up",
                "probability": 0.65,
                "reasoning": "多头动能持续，预计延续上涨",
            },
            "next_cycle_prediction": {
                "direction": "up",
                "probability": 0.70,
                "reasoning": "中期趋势向上，周期处于上升阶段",
            },
        },
        "meta": {
            "timestamp_local_iso": now.isoformat(),
            "symbol": "BTCUSDT",
            "timeframe": "1d",
            "exchange": "GATEIO",
            "bar_count": 50,
            "last_close_bar_iso": last_bar_time.isoformat(),
        },
        "kline_data": kline_data,
        "decision_tree": {
            "gate_trace": ["数据验证通过", "市场结构分析完成"],
            "decision_trace": ["趋势确认", "入场点位计算"],
            "terminal": True,
            "gate_result": "通过",
            "gate_shortcircuited": False,
        },
        "decision_overlay": {
            "order_type": "限价单",
            "order_direction": "long",
            "chart_overlay_active": True,
            "entry_price": kline_data[1]["close"],
            "stop_loss_price": round(kline_data[1]["close"] * 0.97, 2),
            "take_profit_price": round(kline_data[1]["close"] * 1.06, 2),
            "take_profit_price_2": round(kline_data[1]["close"] * 1.10, 2),
        },
        "raw_debug_payload": {
            "stage1_system_prompt": "你是专业的加密货币交易分析师...",
            "stage1_user_prompt": "请分析 BTCUSDT 日线数据...",
            "stage1_raw_response": {"content": "分析完成", "usage": {"prompt_tokens": 1500, "completion_tokens": 800}},
            "stage2_system_prompt": "基于阶段一诊断，生成交易决策...",
            "stage2_user_prompt": "请给出具体交易建议...",
            "stage2_raw_response": {"content": "决策生成完成", "usage": {"prompt_tokens": 2000, "completion_tokens": 600}},
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
        },
        "debug_files_payload": {
            "stage1_files": ["market_structure.txt", "trend_analysis.txt"],
            "stage2_files": ["risk_management.txt", "entry_strategy.txt"],
            "experience_loaded": ["success_case_001.json", "failure_case_002.json"],
            "experience_count": {"success": 1, "failure": 1},
        },
        "strategy_files_used": ["market_structure.txt", "trend_analysis.txt", "risk_management.txt"],
        "usage_total": {
            "prompt_tokens": 3500,
            "completion_tokens": 1400,
            "total_tokens": 4900,
        },
        "exception": None,
    }


@router.get("/demo/sample")
async def get_demo_sample():
    """返回 Demo 分析记录，用于 UI 体验模式。"""
    return _build_demo_record()
