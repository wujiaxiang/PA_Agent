"""两阶段经验库：pending → 终态的状态机与 N 根 K 线规则。

核心不变量：**判定必须用同一条记录自己的 (exchange, symbol, timeframe) 的
K 线**。共享数据源是订阅绑定的单例，用户随时会切品种 —— 曾经正是这一点让
BTCUSDT 的单被 ETHUSDT 的价格判成 -20% 亏损。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa_agent.records.experience_writer import (
    STATUS_DIRS,
    STATUS_LOSS,
    STATUS_PENDING,
    STATUS_UNRESOLVED,
    STATUS_WIN,
    ExperienceWriter,
)
from web.api.experience_verifier import (
    bars_belong_to_instrument,
    settle_record,
    verify_pending,
)


@pytest.fixture()
def writer(tmp_path):
    return ExperienceWriter(experience_dir=tmp_path, logger=None)


def _plan(writer, **over):
    kw = dict(
        cycle_position="trending_tr", direction="up",
        detected_patterns=["测试形态"], confidence=70, summary="两阶段测试",
        symbol="BTCUSDT", timeframe="1h", exchange="GATEIO",
        entry_price=100.0, take_profit_price=120.0, stop_loss_price=90.0,
        is_long=True, entry_ts_open_ms=1000,
    )
    kw.update(over)
    return writer.save_pending(**kw)


def _bar(ts, high, low):
    return {"ts_open": ts, "high": high, "low": low, "close": (high + low) / 2}


def _final(writer, tmp_path, status):
    """Read the settled record (finalize *moves* the file out of pending/)."""
    d = tmp_path / "trending_tr" / STATUS_DIRS[status]
    files = list(d.glob("*.json"))
    assert files, f"{status} 目录下没有文件（目录布局是否正确？）"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ── 落盘布局 ────────────────────────────────────────────────────────────────

def test_pending_record_lands_in_pending_dir(writer):
    path = _plan(writer)
    assert path.parent.name == STATUS_DIRS[STATUS_PENDING]
    content = json.loads(path.read_text(encoding="utf-8"))
    assert content["status"] == STATUS_PENDING
    assert content["entry_ts_open_ms"] == 1000
    assert content["bars_seen"] == 0
    # 三轴都必须记下来，阶段二要靠它们取数
    assert content["exchange"] == "GATEIO"
    assert content["symbol"] == "BTCUSDT"
    assert content["timeframe"] == "1h"


def test_pending_is_not_retrievable_as_experience(writer, tmp_path):
    """待验证记录不能被 ExperienceReader 当成失败经验喂回提示词。"""
    from pa_agent.records.experience_reader import ExperienceReader

    _plan(writer)
    assert ExperienceReader(experience_dir=tmp_path).read_top5("trending_tr") == []


# ── N 根 K 线规则 ───────────────────────────────────────────────────────────

def test_tp_hit_settles_win(writer):
    path = _plan(writer)
    bars = [_bar(2000 + i * 60000, 101.0, 100.0) for i in range(3)]
    bars.append(_bar(2000 + 3 * 60000, 125.0, 118.0))   # 触及 TP=120

    status, seen = settle_record(writer, path, json.loads(path.read_text(encoding="utf-8")),
                                 bars, verify_bars=20)

    assert status == STATUS_WIN and seen == 4
    final = _final(writer, writer._dir, STATUS_WIN)
    assert final["status"] == STATUS_WIN
    assert final["result"] == "win"
    assert final["pnl_pct"] > 0


def test_sl_hit_settles_loss(writer):
    path = _plan(writer)
    bars = [_bar(2000, 100.0, 85.0)]                    # 触及 SL=90

    status, _ = settle_record(writer, path, json.loads(path.read_text(encoding="utf-8")),
                              bars, verify_bars=20)

    assert status == STATUS_LOSS
    assert _final(writer, writer._dir, STATUS_LOSS)["result"] == "loss"


def test_fewer_than_n_bars_stays_pending(writer):
    """K 线不够 N 根 → 继续等，不结算。"""
    path = _plan(writer)
    bars = [_bar(2000 + i * 60000, 101.0, 100.0) for i in range(3)]

    status, seen = settle_record(writer, path, json.loads(path.read_text(encoding="utf-8")),
                                 bars, verify_bars=20)

    assert status == STATUS_PENDING
    assert seen == 3
    assert path.parent.name == STATUS_DIRS[STATUS_PENDING]
    assert json.loads(path.read_text(encoding="utf-8"))["bars_seen"] == 3


def test_n_bars_without_touch_settles_unresolved(writer):
    """走满 N 根仍未触及 → 终态 unresolved，但不给盈亏结论。"""
    path = _plan(writer)
    bars = [_bar(2000 + i * 60000, 101.0, 100.0) for i in range(20)]

    status, seen = settle_record(writer, path, json.loads(path.read_text(encoding="utf-8")),
                                 bars, verify_bars=20)

    assert status == STATUS_UNRESOLVED and seen == 20
    content = _final(writer, writer._dir, STATUS_UNRESOLVED)
    assert content["status"] == STATUS_UNRESOLVED
    assert "pnl_pct" not in content, "未触及不该有盈亏数字"


def test_bars_before_entry_are_ignored(writer):
    """入场之前的 K 线一律不算。"""
    path = _plan(writer)
    pre = [_bar(100, 100.0, 85.0)]                     # ts_open=100 < 锚点 1000，触及 SL
    after = [_bar(5000, 101.0, 100.0)]

    status, seen = settle_record(writer, path, json.loads(path.read_text(encoding="utf-8")),
                                 pre + after, verify_bars=20)

    assert status == STATUS_PENDING
    assert seen == 1, "入场前那根暴跌 bar 被算进去了"


# ── 币种 / 周期对齐 ─────────────────────────────────────────────────────────

def test_price_guard_rejects_other_instrument_bars():
    assert bars_belong_to_instrument([_bar(1, 101.0, 99.0)], 100.0) is True
    # 同量级内的正常波动不该被误杀（entry 100，bars 60~70 = 0.6 倍）
    assert bars_belong_to_instrument([_bar(1, 70.0, 60.0)], 100.0) is True
    # 真正跨量级：BTC 的单（entry≈100000）却读到 ETH 的 bars（≈3000）
    assert bars_belong_to_instrument([_bar(1, 3100.0, 2900.0)], 100_000.0) is False
    # 反向：拿便宜币的 bars 去判高价币
    assert bars_belong_to_instrument([_bar(1, 1.1, 0.9)], 100_000.0) is False


def test_shared_source_rejected_when_symbol_differs(writer):
    _plan(writer, symbol="BTCUSDT", timeframe="1h")

    class Other:
        _symbol = "ETHUSDT"
        _timeframe = "1h"
        _exchange = "GATEIO"

        def latest_snapshot(self, n):
            return [_bar(3000, 130.0, 120.0)]        # 会把 BTC 的单判成 win

    summary = verify_pending(shared_source=Other(), settings=None,
                             writer=writer, batch=5)

    assert summary["checked"] == 0
    assert summary["skipped_no_data"] == 1, "共享源品种不符却仍被用了"


def test_shared_source_rejected_when_timeframe_differs(writer):
    _plan(writer, symbol="BTCUSDT", timeframe="1h")

    class WrongTF:
        _symbol = "BTCUSDT"
        _timeframe = "1d"
        _exchange = "GATEIO"

        def latest_snapshot(self, n):
            return [_bar(3000, 130.0, 120.0)]

    summary = verify_pending(shared_source=WrongTF(), settings=None,
                             writer=writer, batch=5)
    assert summary["checked"] == 0


def test_shared_source_rejected_when_exchange_differs(writer):
    _plan(writer, symbol="BTCUSDT", timeframe="1h", exchange="GATEIO")

    class WrongEx:
        _symbol = "BTCUSDT"
        _timeframe = "1h"
        _exchange = "BINANCE"

        def latest_snapshot(self, n):
            return [_bar(3000, 130.0, 120.0)]

    summary = verify_pending(shared_source=WrongEx(), settings=None,
                             writer=writer, batch=5)
    assert summary["checked"] == 0, "同代码不同交易所不应混用"


def test_shared_source_accepted_on_full_match(writer):
    _plan(writer, symbol="BTCUSDT", timeframe="1h", exchange="GATEIO")

    class Match:
        _symbol = "BTCUSDT"
        _timeframe = "1h"
        _exchange = "GATEIO"

        def latest_snapshot(self, n):
            return [_bar(3000, 125.0, 118.0)]        # 触及 TP

    summary = verify_pending(shared_source=Match(), settings=None,
                             writer=writer, batch=5, verify_bars=20)
    assert summary["win"] == 1


def test_misaligned_bars_leave_record_pending(writer):
    """取到了 bars，但价格量级对不上 → 保持 pending，绝不结算。"""
    _plan(writer, entry_price=100.0)

    class Drifted:
        _symbol = "BTCUSDT"
        _timeframe = "1h"
        _exchange = "GATEIO"

        def latest_snapshot(self, n):
            # entry=100 的记录却读到 3000 量级的 bars —— 必定是另一个标的
            return [_bar(3000, 3100.0, 2900.0)]

    summary = verify_pending(shared_source=Drifted(), settings=None,
                             writer=writer, batch=5)
    assert summary["skipped_misaligned"] == 1
    assert summary["win"] == 0 and summary["loss"] == 0


def test_scope_limits_to_current_instrument(writer):
    _plan(writer, symbol="BTCUSDT", timeframe="1h")
    _plan(writer, symbol="ETHUSDT", timeframe="1h")

    class Match:
        _symbol = "BTCUSDT"
        _timeframe = "1h"
        _exchange = "GATEIO"

        def latest_snapshot(self, n):
            return [_bar(3000, 125.0, 118.0)]

    summary = verify_pending(shared_source=Match(), settings=None,
                             writer=writer, batch=5, scope=("BTCUSDT", "1h"))
    assert summary["checked"] == 1, "范围过滤应只处理当前品种"


def test_no_factory_and_no_match_leaves_pending(writer):
    _plan(writer)

    class Other:
        _symbol = "ETHUSDT"
        _timeframe = "4h"
        _exchange = "GATEIO"

        def latest_snapshot(self, n):
            return []

    summary = verify_pending(shared_source=Other(), settings=None,
                             writer=writer, batch=5)
    assert summary["checked"] == 0
    assert summary["skipped_no_data"] == 1
    assert len(writer.list_pending()) == 1, "记录必须仍是 pending"
