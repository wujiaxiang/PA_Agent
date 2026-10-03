"""Tests for data source factory and settings."""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    create_data_source,
    default_symbol_for_kind,
    default_tradingview_exchange,
    normalize_data_source_kind,
)
from pa_agent.data.eastmoney_source import EastMoneySource
from pa_agent.data.mt5 import MT5Source
from pa_agent.data.tushare_source import TushareSource
from pa_agent.data.tradingview import TradingViewSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "mt5"
    assert normalize_data_source_kind(None) == "mt5"


def test_normalize_data_source_kind_hidden_sources():
    assert normalize_data_source_kind("akshare") == "akshare"
    assert normalize_data_source_kind("eastmoney") == "eastmoney"
    assert normalize_data_source_kind("tushare") == "tushare"
    assert normalize_data_source_kind("yfinance") == "yfinance"


def test_mt5_in_ui_choices():
    """本仓库 Web 后端为 TradingView-only。

    上游 1.31 曾把 MT5 设为默认且排到 UI 首位，但 MT5 仅 Windows 可用，
    Linux/Docker 部署下 bootstrap 会失败，故 UI 只暴露 tradingview。
    """
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "tradingview" in ui_kinds
    assert DATA_SOURCE_CHOICES[0][0] == "tradingview"
    # MT5 / eastmoney / AkShare 均不在 UI 列表中
    assert "mt5" not in ui_kinds
    assert "eastmoney" not in ui_kinds
    assert "akshare" not in ui_kinds


def test_tushare_not_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "tushare" not in ui_kinds


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("mt5"), MT5Source)
    assert isinstance(create_data_source("tradingview"), TradingViewSource)
    assert isinstance(create_data_source("eastmoney"), EastMoneySource)
    assert isinstance(create_data_source("tushare"), TushareSource)


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("mt5") == "XAUUSDm"
    assert default_symbol_for_kind("tradingview") == "XAUUSD"
    assert default_symbol_for_kind("eastmoney") == "000001"
    assert default_symbol_for_kind("tushare") == "000001"


def test_default_tradingview_exchange_is_auto():
    assert default_tradingview_exchange() == ""


def test_general_settings_last_data_source_default():
    """默认数据源必须是 tradingview（本仓库 Web/TradingView-only）。

    上游默认 "mt5" 在 Linux/Docker 下 create_data_source() 抛
    DataSourceTransientError 且被 AppContext.bootstrap() 的 except 吞掉，
    会导致全新部署静默启动但完全没有数据源。
    """
    g = GeneralSettings()
    assert g.last_data_source == "tradingview"
