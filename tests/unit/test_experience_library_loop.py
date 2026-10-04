"""Tests for the experience-library closed loop (write side + outcome resolution)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.experience_writer import ExperienceWriter, evaluate_outcome


def _bar(ts, high, low):
    return {"ts_open": ts, "high": high, "low": low, "close": (high + low) / 2}


# ── ExperienceWriter ────────────────────────────────────────────────────────


def test_save_writes_success_case(tmp_path):
    w = ExperienceWriter(experience_dir=tmp_path)
    p = w.save(
        cycle_position="trending_tr", direction="做多",
        detected_patterns=["均线多头排列"], confidence=65,
        summary="强势上升趋势延续", symbol="BTCUSDT", timeframe="1d",
        entry_price=65000, success=True, pnl_pct=2.5,
    )
    assert p.exists()
    assert p.parent.name == "success_cases"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["result"] == "win"
    assert d["pnl_pct"] == 2.5
    assert d["symbol"] == "BTCUSDT"


def test_save_writes_failure_case(tmp_path):
    w = ExperienceWriter(experience_dir=tmp_path)
    p = w.save(
        cycle_position="trading_range", direction="做空",
        detected_patterns=[], confidence=40, summary="区间下沿失败",
        symbol="ETHUSDT", timeframe="4h", entry_price=3000,
        success=False, pnl_pct=-1.8,
    )
    assert p.parent.name == "failure_cases"
    assert json.loads(p.read_text(encoding="utf-8"))["result"] == "loss"


def test_writer_output_is_readable_by_reader(tmp_path):
    """The whole point: a written entry must be retrievable by the read side."""
    w = ExperienceWriter(experience_dir=tmp_path)
    w.save(cycle_position="broad_channel", direction="做多", detected_patterns=["x"],
           confidence=50, summary="闭环验证", symbol="XAUUSD", timeframe="1h",
           entry_price=2000, success=True, pnl_pct=1.0)
    entries = ExperienceReader(experience_dir=tmp_path).read_top5("broad_channel")
    assert len(entries) == 1
    assert entries[0].content["summary"] == "闭环验证"
    assert entries[0].case_type == "success"


def test_same_second_writes_do_not_overwrite(tmp_path):
    w = ExperienceWriter(experience_dir=tmp_path)
    paths = [
        w.save(cycle_position="spike", direction="做多", detected_patterns=[],
               confidence=50, summary="同秒碰撞", symbol="NVDA", timeframe="1h",
               entry_price=100, success=True, pnl_pct=1.0)
        for _ in range(3)
    ]
    assert len(set(paths)) == 3, "同秒多次写入必须产生不同文件"
    assert all(p.exists() for p in paths)


def test_path_traversal_in_symbol_is_contained(tmp_path):
    w = ExperienceWriter(experience_dir=tmp_path)
    p = w.save(cycle_position="trending_tr", direction="做多", detected_patterns=[],
               confidence=50, summary="x", symbol="../../etc/passwd",
               timeframe="1h", entry_price=1, success=True, pnl_pct=0.1)
    assert tmp_path in p.parents, "symbol 不得逃逸经验库目录"


def test_unknown_cycle_creates_its_directory(tmp_path):
    w = ExperienceWriter(experience_dir=tmp_path)
    p = w.save(cycle_position="brand_new_cycle", direction="做多", detected_patterns=[],
               confidence=10, summary="x", symbol="BTC", timeframe="1d",
               entry_price=1, success=False, pnl_pct=-1)
    assert "brand_new_cycle" in str(p)


def test_no_temp_file_left_behind(tmp_path):
    w = ExperienceWriter(experience_dir=tmp_path)
    w.save(cycle_position="spike", direction="做多", detected_patterns=[],
           confidence=50, summary="x", symbol="BTC", timeframe="1d",
           entry_price=1, success=True, pnl_pct=1)
    assert list(tmp_path.rglob("*.tmp")) == []


# ── evaluate_outcome ────────────────────────────────────────────────────────


def test_take_profit_hit_is_win():
    bars = [_bar(1, 101, 99), _bar(2, 106, 100)]   # 2nd bar crosses TP=105
    assert evaluate_outcome(bars, entry_price=100, take_profit_price=105,
                            stop_loss_price=97, is_long=True) == ("win", 5.0)


def test_stop_loss_hit_is_loss():
    bars = [_bar(1, 101, 99), _bar(2, 100, 96)]   # 2nd bar crosses SL=97
    assert evaluate_outcome(bars, entry_price=100, take_profit_price=105,
                            stop_loss_price=97, is_long=True) == ("loss", -3.0)


def test_untouched_bars_return_none():
    bars = [_bar(1, 101, 99), _bar(2, 102, 99.5)]
    assert evaluate_outcome(bars, entry_price=100, take_profit_price=105,
                            stop_loss_price=97, is_long=True) is None


def test_short_direction_is_mirrored():
    bars = [_bar(1, 100, 95)]
    # short: TP below (95) is a win, SL above (106) is a loss
    assert evaluate_outcome(bars, entry_price=100, take_profit_price=95,
                            stop_loss_price=105, is_long=False) == ("win", 5.0)


def test_bar_spanning_both_levels_is_a_loss():
    """无 intrabar 路径时必须保守：同时触及按止损先算。"""
    bars = [_bar(1, 106, 96)]   # spans TP=105 and SL=97
    result, pnl = evaluate_outcome(bars, entry_price=100, take_profit_price=105,
                                   stop_loss_price=97, is_long=True)
    assert result == "loss"
    assert pnl == -3.0


def test_bars_may_arrive_newest_first():
    fwd = [_bar(1, 101, 99), _bar(2, 106, 100)]
    rev = list(reversed(fwd))
    assert (evaluate_outcome(fwd, entry_price=100, take_profit_price=105,
                             stop_loss_price=97, is_long=True)
            == evaluate_outcome(rev, entry_price=100, take_profit_price=105,
                                stop_loss_price=97, is_long=True))


def test_empty_bars_return_none():
    assert evaluate_outcome([], entry_price=100, take_profit_price=105,
                            stop_loss_price=97, is_long=True) is None


# ── watcher gating ──────────────────────────────────────────────────────────


def test_watcher_skips_no_order():
    from web.api.experience_watcher import spawn_experience_watch
    from pa_agent.config.settings import Settings

    assert spawn_experience_watch(
        data_source=object(), settings=Settings(), symbol="BTC", timeframe="1d",
        stage1={"cycle_position": "trending_tr"},
        stage2_flat={"order_type": "不下单"}, last_closed_ts_open_ms=0) is None


def test_watcher_skips_when_levels_missing():
    from web.api.experience_watcher import spawn_experience_watch
    from pa_agent.config.settings import Settings

    assert spawn_experience_watch(
        data_source=object(), settings=Settings(), symbol="BTC", timeframe="1d",
        stage1={"cycle_position": "trending_tr"},
        stage2_flat={"order_type": "限价单", "order_direction": "做多",
                     "entry_price": 100.0, "take_profit_price": None,
                     "stop_loss_price": None},
        last_closed_ts_open_ms=0) is None


def test_watcher_disabled_by_setting():
    from web.api.experience_watcher import spawn_experience_watch
    from pa_agent.config.settings import Settings

    s = Settings()
    s.prompt.experience_auto_write = False
    assert spawn_experience_watch(
        data_source=object(), settings=s, symbol="BTC", timeframe="1d",
        stage1={"cycle_position": "trending_tr"},
        stage2_flat={"order_type": "限价单", "order_direction": "做多",
                     "entry_price": 100.0, "take_profit_price": 105.0,
                     "stop_loss_price": 97.0},
        last_closed_ts_open_ms=0) is None


def test_watcher_writes_when_tp_resolves(tmp_path):
    """End-to-end: watcher resolves an outcome and lands a file in the library."""
    from web.api.experience_watcher import _run_experience_watch
    import pa_agent.records.experience_writer as ew

    ew.EXPERIENCE_DIR = tmp_path          # ExperienceWriter reads this default

    class _DS:
        # 模拟订阅绑定的数据源：watcher 会校验 _symbol/_timeframe 是否漂移
        _symbol = "BTCUSDT"
        _timeframe = "1d"

        def latest_snapshot(self, n):
            return [
                type("B", (), {"ts_open": 1, "high": 101.0, "low": 99.0, "close": 100.0})(),
                type("B", (), {"ts_open": 2, "high": 106.0, "low": 100.0, "close": 105.0})(),
            ]

    # 锚点必须晚于最后一根已收盘 bar：传 0 会让 watcher 拿入场**之前**的
    # 历史 K 线去判定本单（已由 test_experience_watch_integrity 钉死）。
    _run_experience_watch(
        data_source=_DS(), symbol="BTCUSDT", timeframe="1d",
        entry_price=100.0, take_profit_price=105.0, stop_loss_price=97.0,
        is_long=True, cycle_position="trending_tr", direction="做多",
        detected_patterns=["test"], confidence=60, summary="闭环端到端",
        after_ts_open_ms=1, max_wait_s=5,
    )
    files = list((tmp_path / "trending_tr" / "success_cases").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["result"] == "win"


def test_reader_now_sees_written_entries():
    """Default settings must actually load experiences (was 0 → 空跑)."""
    from pa_agent.config.settings import PromptSettings

    assert PromptSettings().experience_max_entries > 0, \
        "经验库默认条目数必须 > 0，否则整条读取链路长期空跑"