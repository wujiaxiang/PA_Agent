"""后台结算调度器：单飞守卫、不阻塞分析、按当前范围。

没有调度器，待验证记录会一直停在 pending —— 两阶段设计就白做了。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from pa_agent.records.experience_writer import STATUS_WIN, ExperienceWriter
from web.api import experience_scheduler as sched


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    """每个用例一份干净的库。

    ``run_once()`` 自己构造 ``ExperienceWriter``（生产上就该如此），而经验库
    现在完全落库 —— 不隔离就会读到别的用例写进去的 pending。
    """
    from pa_agent.storage.db import reset_hub_for_tests

    h = reset_hub_for_tests(tmp_path / "iso.db")
    yield h
    h.close_all()


@pytest.fixture()
def lib():
    return ExperienceWriter()


def _pending(lib, symbol="BTCUSDT", timeframe="1h", anchor=1000, **over):
    kw = dict(
        cycle_position="trending_tr", direction="up", detected_patterns=["x"],
        confidence=70, summary="s", symbol=symbol, timeframe=timeframe,
        exchange="GATEIO", entry_price=100.0, take_profit_price=120.0,
        stop_loss_price=90.0, is_long=True, entry_ts_open_ms=anchor,
    )
    kw.update(over)
    return lib.save_pending(**kw)


def _last_id(lib) -> str:
    """最近写的那条（库已无目录可看）。"""
    from pa_agent.storage.experience_repo import list_entries

    rows = list_entries(user_id="admin", limit=1)
    return str(rows[0]["entry_id"]) if rows else ""


class _DS:
    """Mirrors TradingViewSource: the shared source is subscription-bound, and
    stage 2 refuses to use it unless all three axes match the record."""

    calls = 0

    def __init__(self, symbol="BTCUSDT", timeframe="1h", exchange="GATEIO"):
        self._symbol = symbol
        self._timeframe = timeframe
        self._exchange = exchange

    def latest_snapshot(self, n):
        type(self).calls += 1
        return [{"ts_open": 2000, "high": 125.0, "low": 118.0,
                 "close": 124.0}]          # 触及 TP=120


class _Prompt:
    experience_verify_bars = 20
    experience_verify_batch = 5
    experience_verify_interval_s = 180.0
    experience_verify_mode = "auto"


class _General:
    def __init__(self, symbol="BTCUSDT", timeframe="1h"):
        self.last_symbol = symbol
        self.last_timeframe = timeframe


class _Settings:
    def __init__(self, symbol="BTCUSDT", timeframe="1h"):
        self.general = _General(symbol, timeframe)
        self.prompt = _Prompt()


class _Ctx:
    def __init__(self, symbol="BTCUSDT", timeframe="1h"):
        self.settings = _Settings(symbol, timeframe)
        # 必须是**实例**：调度器会调 shared.latest_snapshot(n)，
        # 传类会让 self 变成 int。
        self.data_source = _DS(symbol, timeframe)


# ── 结算正确性 ──────────────────────────────────────────────────────────────

def test_run_once_settles_matching_record(lib, monkeypatch):
    _pending(lib)
    ctx = _Ctx()
    monkeypatch.setattr(sched, "_guard", sched._Guard())

    summary = sched.run_once(ctx)

    from pa_agent.storage.experience_repo import get_entry

    assert summary["win"] == 1
    assert len(lib.list_pending()) == 0, "结算后不得残留 pending"
    eid = _last_id(lib)
    assert get_entry(eid, user_id="admin")["status"] == STATUS_WIN


def test_run_once_ignores_other_instruments(lib, monkeypatch):
    _pending(lib, symbol="ETHUSDT")
    ctx = _Ctx()
    monkeypatch.setattr(sched, "_guard", sched._Guard())

    summary = sched.run_once(ctx)

    assert summary["win"] == 0
    assert len(lib.list_pending()) == 1, "别的品种不该被当前范围结算"


def test_scope_follows_settings_not_snapshot(lib, monkeypatch):
    """范围必须每轮重读 settings —— 用户随时会切品种。"""
    _pending(lib, symbol="ETHUSDT")
    ctx = _Ctx()
    ctx = _Ctx(symbol="ETHUSDT")   # 用户切到 ETH
    monkeypatch.setattr(sched, "_guard", sched._Guard())

    assert sched.run_once(ctx)["win"] == 1


# ── 单飞守卫 ────────────────────────────────────────────────────────────────

def test_concurrent_passes_do_not_overlap(lib, monkeypatch):
    guard = sched._Guard()
    monkeypatch.setattr(sched, "_guard", guard)
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def slow_verify(**kw):
        entered.set()
        release.wait(2.0)
        seen.append(1)
        return {"win": 0}

    import web.api.experience_verifier as v
    monkeypatch.setattr(v, "verify_pending", slow_verify)

    t = threading.Thread(target=sched.run_once, args=(_Ctx(),), daemon=True)
    t.start()
    assert entered.wait(2.0), "第一次 pass 未进入"
    # 第二次必须被守卫挡下
    assert sched.run_once(_Ctx()) is None
    release.set()
    t.join(2.0)
    assert len(seen) == 1, "两轮 pass 并发执行了"


def test_guard_releases_after_error(lib, monkeypatch):
    """一次异常不能让调度器永久卡死。"""
    guard = sched._Guard()
    monkeypatch.setattr(sched, "_guard", guard)

    import web.api.experience_verifier as v

    def boom(**kw):
        raise RuntimeError("模拟取数失败")

    monkeypatch.setattr(v, "verify_pending", boom)
    assert sched.run_once(_Ctx()) is None          # 吞掉异常，不冒泡
    # 守卫必须已释放
    assert guard.try_acquire() is True


# ── 定时器生命周期 ──────────────────────────────────────────────────────────

def test_interval_is_clamped_to_minimum(monkeypatch):
    ctx = _Ctx()
    ctx.settings.prompt.experience_verify_interval_s = 1.0   # 太密
    t = sched.start(ctx)
    try:
        assert t is not None and t.daemon
        assert t.name == "experience-scheduler"
    finally:
        sched.stop()
    assert sched._thread is None


def test_start_is_idempotent(monkeypatch):
    ctx = _Ctx()
    a = sched.start(ctx)
    b = sched.start(ctx)
    try:
        assert a is b, "重复 start 不应再起一个线程"
    finally:
        sched.stop()


def test_stop_is_safe_when_not_started():
    sched.stop()          # 不应抛异常


def test_run_once_with_no_pending_is_noop(lib, monkeypatch):
    monkeypatch.setattr(sched, "_guard", sched._Guard())
    summary = sched.run_once(_Ctx())
    assert summary["checked"] == 0
    assert summary["win"] == 0


# ── 定时 / 手工 模式 ────────────────────────────────────────────────────────

def test_manual_mode_skips_timer_pass(lib, monkeypatch):
    """选了手工验证，定时轮询必须空操作。"""
    _pending(lib)
    ctx = _Ctx()
    ctx.settings.prompt.experience_verify_mode = "manual"
    monkeypatch.setattr(sched, "_guard", sched._Guard())

    assert sched.run_once(ctx) is None
    assert len(lib.list_pending()) == 1, "manual 模式下定时器不应结算"


def test_manual_mode_still_allows_explicit_button(lib, monkeypatch):
    """manual 只关定时器，不关「验证」按钮（force=True）。"""
    _pending(lib)
    ctx = _Ctx()
    ctx.settings.prompt.experience_verify_mode = "manual"
    monkeypatch.setattr(sched, "_guard", sched._Guard())

    assert sched.run_once(ctx, force=True)["win"] == 1
    assert len(lib.list_pending()) == 0


def test_auto_mode_is_the_default(lib, monkeypatch):
    _pending(lib)
    monkeypatch.setattr(sched, "_guard", sched._Guard())
    ctx = _Ctx()
    assert sched.mode_of(ctx) == "auto"
    assert sched.run_once(ctx)["win"] == 1


def test_unknown_mode_falls_back_to_auto(lib, monkeypatch):
    ctx = _Ctx()
    ctx.settings.prompt.experience_verify_mode = "别乱写"
    assert sched.mode_of(ctx) == "auto"


def test_missing_mode_setting_defaults_to_auto():
    class Bare:
        class settings:  # noqa: N801
            class general:  # noqa: N801
                last_symbol = "BTCUSDT"

    assert sched.mode_of(Bare()) == "auto"


def test_timer_loop_stays_idle_in_manual_mode(lib, monkeypatch):
    """manual 模式下定时循环反复醒来，但每轮都是空操作。

    必须真的让循环跑起来（用 _stop 收尾），只测 run_once 覆盖不到
    「定时器会不会偷偷结算」这个场景。
    """
    ctx = _Ctx()
    ctx.settings.prompt.experience_verify_mode = "manual"
    _pending(lib)
    guard = sched._Guard()
    monkeypatch.setattr(sched, "_guard", guard)

    # 让循环跑几轮后自行退出（否则 _stop 一直清着会死循环）
    rounds = {"n": 0}
    real_wait = sched._stop.wait

    def counting_wait(timeout=None):
        rounds["n"] += 1
        if rounds["n"] >= 4:
            sched._stop.set()
            return True
        return real_wait(0.01)

    monkeypatch.setattr(sched._stop, "wait", counting_wait)
    sched._stop.clear()
    try:
        sched._loop(ctx, 0.01)
    finally:
        sched._stop.clear()

    assert len(lib.list_pending()) == 1, "manual 模式下定时器结算了记录"
    assert rounds["n"] >= 4, "循环没真正跑起来（测试本身失效）"
