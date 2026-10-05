"""经验库「自然产生案例」的全链路测试。

这条链路此前从未被任何测试覆盖过，而它恰恰是经验库存在的理由：

    下单信号 → 阶段一写 pending → 结算（win/loss/unresolved）
            → 程序化复盘 → 注入 Stage 2 提示词

真机上之所以没跑到，是因为真实分析给出的是「不下单」，门控正确拦下 ——
**门控对了，但链路也就没人验证了**。这里用 mock 把门控打开，走的是
**生产入口**（``spawn_post_order_followup`` / ``settle_record`` /
``_render_experience``），不是内部函数。

两条铁律（踩过才知道）：

1. **必须走生产入口**。曾因测试直接调 ``attach_review(..., verdict=...)``
   ——那是一条只有测试会走的分支，于是「复盘从未进过提示词」这个 bug 在
   全绿的测试里活了很多轮。
2. **K 线快照是最新在前**（``bars[0]`` = newest）。锚点取 ``bars[0]`` 就等于
   锚在最新 bar 上，其后一根都没有，记录永远停在 pending。
"""

from __future__ import annotations

import json
from typing import Any

import pytest


# ── 假数据源（基类签名：subscribe(symbol, timeframe)，交易所在 set_exchange）──

def _bars(*, n: int = 40, base: float = 100.0, tp_at: int | None = None,
          sl_at: int | None = None, tp_level: float = 120.0,
          sl_level: float = 90.0, step: float = 1.0,
          trend: int = 1) -> list[Any]:
    """造一段 K 线。``tp_at`` / ``sl_at`` 指定第几根触到 ``tp_level`` /
    ``sl_level``。

    触价用的是**绝对价位**，不是相对 base 的比例 —— 写成 ``base * 1.2`` 时，
    趋势里第 40 根的实际价格早已是 base+40，两者对不上，触价判断全部落空。

    ``trend`` 必须随场景给方向：只造上涨行情时，TP 永远先于 SL 被穿到，
    「触止损」那个用例会稳定判成 win（实测过）。

    快照是**最新在前**（对齐生产契约）。

    **直接用真实的 ``KlineBar`` 数据类**，不造自己的 stub：先前用替身时因为
    少了 ``open`` 字段，结算阶段直接 AttributeError —— 替身掩盖的正是
    「生产代码到底读 bar 的哪些字段」这个问题。真实类型才能回答它。
    """
    from pa_agent.data.base import KlineBar

    out: list[Any] = []
    for i in range(n):
        price = base + i * step * (1 if trend >= 0 else -1)
        high, low = price + 0.2, price - 0.2
        if i == tp_at:
            high = tp_level
        if i == sl_at:
            low = sl_level
        # 生产契约：bars[0] 是**未收盘**的 forming bar（closed=False），
        # 入场锚点取「最后一根已收盘 bar」。全部标 closed=True 会让锚点落到
        # 最新 bar 上，其后一根都没有，记录永远停在 pending。
        out.append(KlineBar(seq=n - i, ts_open=1000 + i * 60, open=price,
                            high=high, low=low, close=price,
                            volume=1.0 + i, closed=(i != n - 1)))
    return list(reversed(out))          # newest-first


class _FakeSource:
    """共享源：三轴匹配才复用，否则生产代码会建专用源（这里不给）。"""

    def __init__(self, symbol: str, timeframe: str, exchange: str,
                 bars: list[Any]):
        self._symbol, self._timeframe, self._exchange = symbol, timeframe, exchange
        self._bars = bars

    def _match(self) -> bool:
        return (self._symbol, self._timeframe, self._exchange) == ("BTCUSDT", "1h", "GATEIO")

    def latest_snapshot(self, n: int, **kw: Any):
        return self._bars if self._match() else []


class _Frame:
    def __init__(self, bars: list[Any]):
        self.bars = bars


class _Settings:
    class general:                       # noqa: N801
        alert_on_order_opportunity = True
        decision_confidence_threshold = None
        # **故意给一个错的交易所**：symbol/timeframe 走本次分析，交易所若从
        # 这里读就会写出结算不了的记录（真机见过 GATEIO/NVDA）。
        last_tradingview_exchange = "WRONG_EXCHANGE"
        last_symbol = "WRONG_SYMBOL"
        last_timeframe = "99m"

    class prompt:                        # noqa: N801
        experience_auto_write = True
        experience_max_entries = 3
        experience_max_chars_per_entry = 400
        experience_verify_bars = 10


class _Record:
    """假装是 AnalysisRecord：只暴露生产代码真正会读的属性。

    ``order_type`` 用的是**中文词表**里的值（``ORDER_OPPORTUNITY_TYPES``）——
    写 ``"limit"`` 会被门控正确拦下，那不是 bug，是模型本来就输出中文。
    """

    def __init__(self):
        self.stage1_diagnosis = {
            "cycle_position": "trending_tr",
            "direction": "bullish",
            "detected_patterns": ["always_in"],
            "confidence": 78,
            "summary": "趋势中回踩不破结构，顺大周期做多",
        }
        self.stage2_decision = {
            "decision": {
                "order_type": "限价单",   # 词表是中文：{"限价单","突破单","市价单"}
                "order_direction": "做多",
                "trade_confidence": 82,
                "entry_price": 100.0,
                "take_profit_price": 120.0,
                "stop_loss_price": 90.0,
            }
        }


@pytest.fixture()
def loop(db_path_isolated):
    """隔离库。

    **复用 conftest 的 ``db_path_isolated``**，不自建 hub：早先这里
    ``reset_hub_for_tests(tmp)`` 之后还 ``close_all()``，等于把**全局** hub
    关掉了 —— 同一进程里后续测试拿到的是已关闭的连接，于是一串无关用例
    开始随机失败（实测 ``test_record_user_isolation`` 5 条变红）。
    """
    from pa_agent.storage.db import initialize_storage

    initialize_storage()
    yield


def _run_stage1(loop, *, bars, record=None, user_id="admin"):
    """走生产入口：``spawn_post_order_followup`` → 门控 → 写 pending。"""
    from web.api.order_followup import spawn_post_order_followup

    # 返回值是「是否起了通知线程」，与 pending 写入无关 —— 写入在同步段完成，
    # 在起线程之前。这里不关心返回值，只保证整条路没抛。
    spawn_post_order_followup(
        record=record or _Record(),
        frame=_Frame(bars),
        settings=_Settings(),
        symbol="BTCUSDT",
        timeframe="1h",
        exchange="GATEIO",
        data_source=_FakeSource("BTCUSDT", "1h", "GATEIO", bars),
        user_id=user_id,
    )


def _pending(loop, user_id: str | None = None):
    """列待验证记录。默认 ``None`` = 遍历所有用户（与结算同一条路径）。"""
    from pa_agent.records.experience_writer import ExperienceWriter

    return ExperienceWriter().list_pending(limit=10, user_id=user_id)


def _settle(loop, bars, shared_source):
    """用**更晚的** K 线快照结算。

    真实时序是：信号触发时写 pending（锚点 = 最后一根已收盘 bar），几小时后
    才有后续 K 线可供判定。用同一批 bar 结算等于「刚入场就结算」，
    锚点之后一根都没有 —— 那不是缺陷，是时序不对。
    """
    from web.api import experience_verifier as ev
    from pa_agent.records.experience_writer import ExperienceWriter

    return ev.verify_pending(
        shared_source=shared_source,
        source_factory=None,             # 不给专用源：范围外应当安静跳过
        settings=_Settings(),
        scope=None,
        writer=ExperienceWriter(),
    )


#: 入场时能看到的 K 线数（最后 40 根），其后才是「未来」
_AT_ENTRY = 40


def _later(*, extra: int, tp_at: int | None = None, sl_at: int | None = None,
           **kw: Any) -> list[Any]:
    """构造「未来」的 K 线：从入场那一刻再往后 ``extra`` 根。"""
    return _bars(n=_AT_ENTRY + extra, tp_at=tp_at, sl_at=sl_at, **kw)


def test_order_signal_creates_a_pending_entry(loop):
    """下单信号必须**自动**产生 pending 条目（阶段一）。"""
    _run_stage1(loop, bars=_bars(n=_AT_ENTRY))

    pending = _pending(loop)
    assert len(pending) == 1, f"下单信号没有自动写入经验条目：{pending}"
    eid, content = pending[0]
    assert content["symbol"] == "BTCUSDT"
    assert content["user_id"] == "admin"
    assert content["entry_price"] == 100.0
    assert content["take_profit_price"] == 120.0
    assert content["stop_loss_price"] == 90.0
    # 自描述：只有 pnl_pct 的记录会让模型事后诸葛亮
    assert content.get("analysis_context"), "必须写 analysis_context"
    assert content.get("bars_snapshot"), "必须写 bars_snapshot"


def test_entry_exchange_matches_the_analysis_not_the_frozen_setting(loop):
    """**回归守卫**：交易所必须与 symbol/timeframe **同源**。

    真机实测写出一条 ``GATEIO/NVDA`` 的记录（美股挂在加密交易所下）——
    因为 symbol/timeframe 取自本次分析，而交易所从
    ``settings.general.last_tradingview_exchange``（**冻结**的只读字段）读，
    两者取自不同真相源。那条记录 TradingView 永远无数据，永久停在 pending。
    """
    _run_stage1(loop, bars=_bars(n=_AT_ENTRY))

    _, content = _pending(loop)[0]
    assert content["exchange"] == "GATEIO", (
        f"交易所取自冻结字段（{content['exchange']}），与本次分析的品种不配对")


def test_no_order_signal_creates_nothing(loop):
    """门控必须真的拦得住 —— 不下单不该产生任何经验条目。"""
    class _NoTrade(_Record):
        def __init__(self):
            super().__init__()
            self.stage2_decision = {"decision": {"order_type": "不下单",
                                                  "trade_confidence": 20}}

    _run_stage1(loop, bars=_bars(n=_AT_ENTRY), record=_NoTrade())
    assert _pending(loop) == [], "「不下单」也写入了经验条目"


def test_win_produces_review_that_reaches_the_prompt(loop):
    """完整闭环：下单信号 → win → 程序化复盘 → 注入 Stage 2 提示词。"""
    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.records.experience_reader import ExperienceReader
    from pa_agent.storage.experience_repo import get_entry, program_review

    entry_bars = _bars(n=_AT_ENTRY)
    _run_stage1(loop, bars=entry_bars)
    eid = _pending(loop)[0][0]

    future = _later(extra=10, tp_at=_AT_ENTRY + 5)
    summary = _settle(loop, future,
                      _FakeSource("BTCUSDT", "1h", "GATEIO", future))
    assert summary["checked"] == 1, f"结算没检查到记录：{summary}"
    assert summary["win"] == 1, f"触价应为 win：{summary}"

    assert get_entry(eid)["status"] == "win"

    review = program_review(eid)
    assert review is not None, "结算后必须自动生成程序化复盘"
    assert review["source"] == "program"
    assert review["verdict"], "复盘必须有结论"
    assert review["payload"]["bars_to_exit"] >= 1

    hits = ExperienceReader().read_for_stage2(
        "trending_tr", direction="bullish", patterns=["always_in"], user_id="admin")
    assert hits, "win 的条目必须能被检索端读到"

    out = PromptAssembler._render_experience(hits, max_chars_per_entry=400,
                                            user_id="admin")
    assert "<experience_review" in out, "复盘没有进入提示词"
    assert review["verdict"] in out

    for block in out.split("```json")[1:]:
        json.loads(block.split("```")[0])      # 案例块必须是合法 JSON


def test_loss_produces_review_with_facts(loop):
    """loss 分支同样要产出带 MFE/MAE 的确定性复盘。"""
    from pa_agent.storage.experience_repo import get_entry, program_review

    entry_bars = _bars(n=_AT_ENTRY)
    _run_stage1(loop, bars=entry_bars)
    eid = _pending(loop)[0][0]

    # 必须是**下跌**：上涨行情里 TP 永远先穿到，止损根本轮不到
    future = _later(extra=10, sl_at=_AT_ENTRY + 3, trend=-1, step=0.4)
    summary = _settle(loop, future,
                      _FakeSource("BTCUSDT", "1h", "GATEIO", future))
    assert summary["loss"] == 1, f"触价应为 loss：{summary}"
    assert get_entry(eid)["status"] == "loss"

    payload = program_review(eid)["payload"]
    assert payload["mae_pct"] > 0, "亏损单的 MAE 必须算出来"
    assert payload["bars_to_exit"] >= 1


def test_entry_outside_shared_scope_is_left_pending(loop):
    """范围外的记录**不得**拿共享源的 K 线判定 —— 宁可留在 pending。

    宁可保持 pending，也绝不用别的标的判定：凭空写出的胜负比没有胜负更糟。
    """
    entry_bars = _bars(n=_AT_ENTRY)
    _run_stage1(loop, bars=entry_bars)

    future = _later(extra=10, tp_at=_AT_ENTRY + 5)
    summary = _settle(loop, future,
                      _FakeSource("ETHUSDT", "1h", "GATEIO", future))  # 故意错标的
    assert summary["checked"] == 0, "范围外却检查了记录 —— 会拿错标的判定"
    assert summary["win"] == 0 and summary["loss"] == 0
    assert _pending(loop), "范围外的记录必须仍留在 pending"


def test_user_isolation_holds_across_the_whole_loop(loop):
    """整条链路都要按 user_id 隔离：bob 看不到 alice 的经验。"""
    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.records.experience_reader import ExperienceReader

    entry_bars = _bars(n=_AT_ENTRY)
    _run_stage1(loop, bars=entry_bars, user_id="alice")
    eid = _pending(loop)[0][0]
    future = _later(extra=10, tp_at=_AT_ENTRY + 5)
    _settle(loop, future, _FakeSource("BTCUSDT", "1h", "GATEIO", future))

    assert ExperienceReader().read_for_stage2(
        "trending_tr", direction="bullish", patterns=["always_in"],
        user_id="alice"), "alice 应当看得到自己那条"
    assert not ExperienceReader().read_for_stage2(
        "trending_tr", direction="bullish", patterns=["always_in"],
        user_id="bob"), "bob 不该看到 alice 的经验"
    assert not ExperienceReader().read_for_stage2(
        "trending_tr", direction="bullish", patterns=["always_in"],
        user_id=""), "未登录也不该看到 alice 的经验"

    # 且注入块本身也不能把 alice 的结论带给 bob
    out = PromptAssembler._render_experience([], max_chars_per_entry=400,
                                            user_id="bob")
    assert eid not in out

def test_success_is_not_logged_as_failure(loop, caplog):
    """写入成功时**不得**打出 "stage-1 failed"。

    回归守卫：成功路径上写着 ``staged.name``，而
    ``save_pending_if_resolvable`` 返回的是 entry_id（str）—— 于是**每次成功
    写入都在那一行抛 AttributeError**，掉进外层 except 被报成
    「experience stage-1 failed」。记录写进去了，日志却说失败，排查会被直接带偏。
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="web.api.order_followup"):
        _run_stage1(loop, bars=_bars(n=_AT_ENTRY))

    assert _pending(loop), "前提：记录确实写成功了"
    assert not [r for r in caplog.records
                if "experience stage-1 failed" in r.getMessage()], (
        "成功写入被报成了失败")


def test_analyze_passes_the_live_exchange_not_the_frozen_one():
    """**回归守卫**：`routes_analyze` 必须把**本次分析的**交易所传下去。

    上面的用例直接调 ``spawn_post_order_followup``，绕过了这一跳 —— 而真机
    那条 ``GATEIO/NVDA`` 恰恰是在**这一跳**丢的：调用方刻意用本次分析的
    symbol/timeframe（注释明写"不能用全局订阅"），交易所却在函数内部从
    冻结的 ``settings.general.last_tradingview_exchange`` 读，两者不同源。

    **断言必须落在那一个调用块上**：早期版本只查 ``"exchange=view_exchange"
    in inspect.getsource(module)``，而该串在文件里出现 3 次（别的调用点早就在
    传），删掉出问题的那一处照样绿 —— 典型的「看起来测到了、其实没测到」。
    """
    import inspect
    import re

    from web.api import routes_analyze

    src = inspect.getsource(routes_analyze)
    calls = re.findall(r"spawn_post_order_followup\((.*?)\n            \)",
                       src, re.S)
    assert calls, "找不到 spawn_post_order_followup 的调用块"
    assert any("exchange=" in block for block in calls), (
        "调用点没有把本次分析的交易所透传下去")
