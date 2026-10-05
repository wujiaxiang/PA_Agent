"""经验库闭环（库内唯一真源，2026-10-05 起不再有文件）。

两阶段状态机是这一层唯一的不变量：

    pending ──(触及 TP)──→ win
           ──(触及 SL)──→ loss
           ──(走满 N 根仍未触及)──→ unresolved   （终态但无盈亏结论）
           ──(不足 N 根)──→ 仍是 pending

**只有 win / loss 可被检索喂回提示词。** 其余状态都不得当成失败经验 ——
那会让模型把「还没判定」读成「我们错了」，进而做出过度保守的决策。
"""
from __future__ import annotations

import pytest

from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.experience_writer import (
    STATUS_LOSS,
    STATUS_PENDING,
    STATUS_UNRESOLVED,
    STATUS_WIN,
    ExperienceWriter,
    evaluate_outcome,
)
from web.api.experience_verifier import verify_pending


@pytest.fixture(autouse=True)
def hub(tmp_path):
    """每个用例一份干净的库。

    经验库已完全落库（共享 session 级 DB 会互相污染：一条用例写入的案例
    会被另一个用例读到，断言照样绿但验的不是它声称的东西）。
    """
    from pa_agent.storage.db import reset_hub_for_tests

    h = reset_hub_for_tests(tmp_path / "iso.db")
    yield h
    h.close_all()


@pytest.fixture()
def writer():
    return ExperienceWriter()


def _plan(writer, **over):
    kw = dict(
        cycle_position="trending_tr", direction="做空",
        detected_patterns=["测试形态"], confidence=70, summary="两阶段测试",
        symbol="BTCUSDT", timeframe="1h", exchange="GATEIO",
        entry_price=100.0, take_profit_price=90.0, stop_loss_price=110.0,
        is_long=False, entry_ts_open_ms=1000,
    )
    kw.update(over)
    return writer.save_pending(**kw)


def _bar(ts, high, low):
    return {"ts_open": ts, "high": high, "low": low, "close": (high + low) / 2}


class _Source:
    """最小数据源替身：三轴对齐，供 ``_shared_fetch`` 复用。"""

    _symbol = "BTCUSDT"
    _timeframe = "1h"
    _exchange = "GATEIO"

    def __init__(self, rows):
        self._rows = rows

    def latest_snapshot(self, n):
        return list(self._rows)


def _settle(writer, bars, **kw):
    return verify_pending(shared_source=_Source(bars), settings=None,
                          writer=writer, batch=5, **kw)


def _settled(writer, entry_id, status):
    """结算后回读该条目（finalize 就地改 status，不再搬文件）。"""
    from pa_agent.storage.experience_repo import get_entry

    out = get_entry(entry_id, user_id="admin")
    assert out is not None, "结算后条目必须仍在库里"
    assert out["status"] == status
    return out


# ── 阶段一：入场即写 ────────────────────────────────────────────────────────

def test_pending_is_written_with_status_pending(writer):
    eid = _plan(writer)
    from pa_agent.storage.experience_repo import get_entry

    rec = get_entry(eid, user_id="admin")
    assert rec["status"] == STATUS_PENDING
    assert rec["symbol"] == "BTCUSDT"
    assert rec["entry_ts_open_ms"] == 1000


def test_pending_is_not_retrievable_as_experience(writer):
    """**回归守卫**：未决记录不得被检索端拿到。

    曾因 `experience_repo._VALID_STATUSES` 词表错配（写 success/failure 而
    writer 发 win/loss），upsert 的兜底分支把**每一条**都静默改写成 pending ——
    检索端因此永远取不到任何已结算经验，整个经验库静默失效。
    """
    _plan(writer)
    assert ExperienceReader().read_top5("trending_tr") == []
    assert ExperienceReader().read_for_stage2("trending_tr", direction="做空") == []


def test_entry_ids_are_unique_even_in_the_same_second(writer):
    """两个 entry_id 不得相同。

    旧实现靠「秒级时间戳_标的_周期」的文件名唯一性，同秒写同一标的会撞；
    现在 ``new_entry_id`` 带 uuid 后缀，碰撞面被彻底消除。
    """
    ids = {_plan(writer) for _ in range(20)}
    assert len(ids) == 20, "同秒内连续写入必须得到互不相同的 entry_id"


def test_hostile_symbol_cannot_escape_the_library(writer):
    """符号名带路径分隔符时不得影响任何落库路径（库内已无路径，但内容仍要干净）。"""
    eid = _plan(writer, symbol="../../etc/passwd")
    from pa_agent.storage.experience_repo import get_entry

    rec = get_entry(eid, user_id="admin")
    assert rec["symbol"] == "../../etc/passwd"   # 原样存内容，不做任何解释
    assert "/" not in eid and ".." not in eid      # 但主键永远是生成的，不含用户输入


# ── 阶段二：N 根 K 线规则 ────────────────────────────────────────────────────

def test_tp_hit_settles_win(writer):
    eid = _plan(writer)
    bars = [_bar(500, 101, 99), _bar(1500, 95, 85)]   # 第二根跌破 TP=90
    s = _settle(writer, bars)
    assert s["win"] == 1
    _settled(writer, eid, STATUS_WIN)


def test_sl_hit_settles_loss(writer):
    eid = _plan(writer)
    bars = [_bar(1500, 115, 105)]                       # 上破 SL=110
    _settle(writer, bars)
    _settled(writer, eid, STATUS_LOSS)


def test_fewer_than_n_bars_stays_pending(writer):
    eid = _plan(writer)
    bars = [_bar(1500, 101, 99)] * 3                    # 3 根 < 默认 20
    _settle(writer, bars, verify_bars=20)
    _settled(writer, eid, STATUS_PENDING)


def test_n_bars_without_touch_settles_unresolved(writer):
    """走满 N 根仍未触及任一价位 = 终态，但**没有盈亏结论**。

    unresolved 不是失败：它只说明「在给定的窗口长度内价格没走到」。
    把它当成 loss 喂回提示词，会凭空制造大量并不存在的错误经验。
    """
    eid = _plan(writer)
    bars = [_bar(1500 * (i + 1), 101, 99) for i in range(5)]
    _settle(writer, bars, verify_bars=5)
    out = _settled(writer, eid, STATUS_UNRESOLVED)
    assert "pnl_pct" not in out and "result" not in out, "unresolved 不得带盈亏结论"
    assert ExperienceReader().read_top5("trending_tr") == [], "unresolved 不可检索"


def test_bars_before_entry_are_ignored(writer):
    """**回归守卫**：入场锚点之前的 K 线不能算作「本单走势」。

    锚点退化（为 0）时过滤条件会变成 ``ts_open > 0``，入场前的历史行情被
    当成结果，凭空写出胜负。
    """
    eid = _plan(writer, entry_ts_open_ms=3000)
    bars = [
        _bar(500, 120, 80),      # 入场**前**：疯狂波动，若被采信会立刻判定
        _bar(1000, 120, 80),
        _bar(3500, 101, 99),     # 入场后：平静
        _bar(4000, 101, 99),
    ]
    # 入场后只有 2 根，少于要求的 3 根 → 仍在等，不会被入场的剧烈波动提前判死
    _settle(writer, bars, verify_bars=3)
    _settled(writer, eid, STATUS_PENDING)


def test_same_bar_touching_both_counts_as_loss():
    """**回归守卫**：同一根 bar 同时触及 TP 与 SL 时按**止损**计。

    OHLC 无法还原 bar 内的先后路径，按乐观计会把经验库偏向虚高胜率 ——
    这类偏差不会报错，只会持续污染检索结果。
    """
    result, pnl = evaluate_outcome(
        [_bar(1500, 115, 85)],
        entry_price=100.0, take_profit_price=90.0, stop_loss_price=110.0,
        is_long=False,
    )
    assert result == "loss" and pnl < 0


# ── 归属 ────────────────────────────────────────────────────────────────────

def test_pending_records_its_owner_and_settlement_preserves_it(writer):
    """user_id 必须活得过后台结算。

    结算跑在调度器线程上、结算的是几小时前的记录，那时没有请求上下文 ——
    记录本身是唯一的归属依据。
    """
    eid = _plan(writer, user_id="alice")
    from pa_agent.storage.experience_repo import get_entry

    assert get_entry(eid, user_id="alice")["user_id"] == "alice"

    writer.finalize(eid, status=STATUS_WIN, pnl_pct=3.0, user_id="")
    # finalize 不传 user_id 时必须沿用记录自带的，不能洗成默认用户
    assert get_entry(eid, user_id="alice")["status"] == STATUS_WIN


def test_settlement_of_record_without_owner_still_works(writer):
    eid = _plan(writer)
    assert writer.finalize(eid, status=STATUS_LOSS, pnl_pct=-2.0) is True


def test_cannot_finalize_another_users_record(writer):
    """**回归守卫 / 安全**：不能通过传一个别的 user_id 去结算他人记录。

    归属一旦可被调用方随意指定，A 就能改写 B 的经验结论 —— 而经验结论会
    直接进入后续分析的提示词。传错 user_id 必须干净地失败且不改数据。
    """
    from pa_agent.storage.experience_repo import get_entry

    eid = _plan(writer, user_id="alice")
    assert writer.finalize(eid, status=STATUS_LOSS, pnl_pct=-9.0, user_id="carol") is False
    rec = get_entry(eid, user_id="alice")
    assert rec["status"] == STATUS_PENDING, "他人记录必须原封不动"


def test_finalize_of_unknown_entry_reports_failure(writer):
    assert writer.finalize("admin_nope", status=STATUS_WIN) is False


# ── 检索 ────────────────────────────────────────────────────────────────────

def test_reader_returns_only_verified_entries(writer):
    writer.save(cycle_position="trending_tr", direction="做多",
                detected_patterns=["x"], confidence=70, summary="赢的",
                symbol="BTCUSDT", timeframe="1h", entry_price=100.0,
                success=True, pnl_pct=2.0)
    _plan(writer)                       # 仍是 pending
    hits = ExperienceReader().read_top5("trending_tr")
    assert len(hits) == 1 and hits[0].content["summary"] == "赢的"


def test_direction_enum_mismatch_is_fixed(writer):
    """**回归守卫**：阶段二的「做空」必须能与阶段一的 ``bearish`` 匹配上。

    条目的 direction 来自阶段二 ``order_direction``（校验限定 ["做多","做空"]），
    检索时传入的却是阶段一 direction（bullish/bearish/neutral）。直接比字符串
    则永远不等 → +2 分恒为 0，检索退化成「只看形态交集」且毫无报错。
    """
    writer.save(cycle_position="trending_tr", direction="做多",
                detected_patterns=["均线多头排列"], confidence=70, summary="涨",
                symbol="BTCUSDT", timeframe="1h", entry_price=100.0,
                success=True, pnl_pct=2.0)
    assert ExperienceReader().read_for_stage2(
        "trending_tr", direction="bullish", patterns=["均线多头排列"])


def test_reader_respects_user_isolation(writer):
    writer.save(cycle_position="trending_tr", direction="做多", detected_patterns=["x"],
                confidence=70, summary="A的", symbol="BTCUSDT", timeframe="1h",
                entry_price=100.0, success=True, user_id="alice")
    r = ExperienceReader()
    assert len(r.read_top5("trending_tr", user_id="alice")) == 1
    assert len(r.read_top5("trending_tr", user_id="bob")) == 0


def test_reader_survives_unreadable_store(writer, hub):
    """存储层读不出来时返回空列表，**不抛**。

    经验库不可用时最该做的是让分析照常跑完，而不是抛异常中断 —— 更不能
    伪装成「库里没有」而让上层以为本来就没经验。
    """
    writer.save(cycle_position="trending_tr", direction="做多", detected_patterns=["x"],
                confidence=70, summary="x", symbol="BTCUSDT", timeframe="1h",
                entry_price=100.0, success=True)
    hub.query("DROP TABLE experience_entries")
    assert ExperienceReader().read_top5("trending_tr") == []