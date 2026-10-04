"""交易对选择器：内置品种表与名称映射的一致性。

背景：选择器只有 5–15 个品种且无模糊搜索，���验很差。
扩充到 ~180 个品种后，必须保证「每个品种都有中文名」，
否则下拉里会出现一列没有名字的纯代码。
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def presets():
    from pa_agent.data.tradingview import TV_SYMBOL_PRESETS
    return TV_SYMBOL_PRESETS


@pytest.fixture(scope="module")
def names():
    from pa_agent.data.tradingview import TV_SYMBOL_NAMES
    return TV_SYMBOL_NAMES


def _all_symbols(presets):
    return sorted({s for v in presets.values() for s in v})


def test_presets_are_substantial(presets):
    """每个交易所的清单不应再是 5 个的玩具规模。"""
    for ex, syms in presets.items():
        if ex == "":
            continue
        # 阈值取 8：指数/期货类交易所天然品种就少（SP 只有 8 个），
        # 重点是不能再是此前 5 个的玩具规模。
        assert len(syms) >= 8, f"{ex} 只有 {len(syms)} 个品种，交互体验过差"
        assert len(set(syms)) == len(syms), f"{ex} 存在重复品种"


def test_every_symbol_has_a_chinese_name(presets, names):
    missing = [s for s in _all_symbols(presets) if s not in names]
    assert not missing, f"缺少中文名：{missing}"


def test_names_are_non_empty(presets, names):
    empty = [s for s in _all_symbols(presets) if not str(names.get(s, "")).strip()]
    assert not empty, f"中文名为空：{empty}"


def test_list_symbols_returns_populated_list():
    """数据源侧接口必须真的返回品种（前端默认清单依赖它）。"""
    from pa_agent.data.tradingview import TradingViewSource

    src = TradingViewSource()
    for ex in ("GATEIO", "NASDAQ", "OANDA", ""):
        syms = src.list_symbols(ex)
        assert len(syms) >= 10, f"list_symbols({ex!r}) 只返回 {len(syms)} 个"


def test_list_symbols_falls_back_for_unknown_exchange():
    from pa_agent.data.tradingview import TradingViewSource

    syms = TradingViewSource().list_symbols("NOT_A_REAL_EXCHANGE")
    assert len(syms) >= 5, "未知交易所应回退到通用清单而不是空列表"
