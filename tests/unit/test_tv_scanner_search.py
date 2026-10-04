"""TradingView scanner 品种搜索：清洗/排序逻辑 + 映射表。

网络相关用例默认跳过（CI 常无外网），重点覆盖纯逻辑：
衍生品识别、相关性排序、exchange→market 映射、港股代码不补零。
"""
from __future__ import annotations

import pytest

from pa_agent.data.tradingview import (
    TV_SEARCH_EXCHANGE_ALIASES,
    TV_SEARCH_MARKET_BY_EXCHANGE,
    _is_derivative,
    _relevance,
)


@pytest.mark.parametrize("code,expected", [
    ("BTCUSDT", False), ("ETHUSDT", False), ("AAPL", False),
    ("EURUSD", False), ("600519", False), ("700", False),
    ("BTCUSD.P", True),      # 永续合约
    ("ETHUSD.F", True),      # 交割合约
    ("BTCUSDT.3L", True),    # 杠杆代币（.NLS 后缀）
    ("BTCUSDT.5S", True),
    ("ETHUSDT.2L", True),
    ("EURUSD.ONE", True),    # 外汇券商变体
    ("EURUSD.PRO.OTMS", True),
    ("GBPUSD.SML.ONE", True),
    ("EURUSD.ECN", True),
    ("WBTCUSDT", True),      # wrapped
    ("STETHUSDT", True),     # staked
])
def test_is_derivative(code, expected):
    assert _is_derivative(code) is expected


def test_relevance_ranks_exact_before_prefix_before_substring():
    ranked = sorted(
        ["BTCUSDT", "WBTCUSDT", "ETHBTC", "BTCUSD"],
        key=lambda c: _relevance(c, "BTC"),
    )
    # 前缀匹配（BTCUSD / BTCUSDT）必须排在纯子串匹配（WBTCUSDT / ETHBTC）之前
    assert ranked.index("BTCUSD") < ranked.index("WBTCUSDT")
    assert ranked.index("BTCUSDT") < ranked.index("ETHBTC")
    # 前缀匹配中，查询词之后的尾巴越短越贴合（BTCUSD 优于 BTCUSDT）
    assert ranked[0] == "BTCUSD"


def test_relevance_is_stable_for_equal_codes():
    assert _relevance("BTCUSDT", "BTC") == _relevance("BTCUSDT", "BTC")


def test_relevance_exact_match_wins():
    assert _relevance("BTC", "BTC")[0] < _relevance("BTCUSDT", "BTC")[0]


def test_every_mapped_exchange_has_a_known_market():
    valid = {"crypto", "america", "forex", "china", "hongkong", "futures"}
    for ex, market in TV_SEARCH_MARKET_BY_EXCHANGE.items():
        assert market in valid, f"{ex} 映射到未知市场 {market}"


def test_gateio_alias_is_present():
    """Gate.io 在 scanner 里叫 GATE，不叫 GATEIO；缺了这个别名会搜不到任何东西。"""
    assert TV_SEARCH_EXCHANGE_ALIASES.get("GATEIO") == "GATE"


def test_hkex_codes_are_not_zero_padded():
    """TV 的港股代码不补前导零：0700/0388 在 TV 上不存在，订阅必然失败。"""
    from pa_agent.data.tradingview import TV_SYMBOL_PRESETS

    hkex = TV_SYMBOL_PRESETS["HKEX"]
    assert "700" in hkex and "0700" not in hkex
    assert "388" in hkex and "0388" not in hkex
    for code in hkex:
        assert code == code.lstrip("0") or code.isdigit() is False, \
            f"港股代码 {code} 不应补前导零"


@pytest.mark.network
def test_live_scan_returns_clean_spot_pairs():
    import pytest as _p
    if not _p.importorskip("requests", reason="需要外网"):
        _p.skip("需要外网")
    from pa_agent.data.tradingview import search_tv_symbols

    rows = search_tv_symbols("BTC", "GATEIO", limit=5)
    if not rows:
        _p.skip("scanner 不可达（离线环境）")
    assert rows[0]["code"] == "BTCUSDT"
    assert not any(_is_derivative(r["code"]) for r in rows[:1])
