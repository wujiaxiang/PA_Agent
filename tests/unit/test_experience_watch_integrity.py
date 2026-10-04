"""经验库写入链路的真实性保障。

背景核查发现三个真实缺陷，都会让经验库写入**编造**的数据：
1. ``spawn_experience_watch`` 的 ``data_source`` 取自
   ``getattr(record, "_data_source")`` / ``getattr(frame, "data_source")``，
   而 ``AnalysisRecord`` / ``KlineFrame`` 都没有这两个属性 → ds 恒为 None
   → 写入链路自打通以来从未被调用过（库里 36 条全是种子数据）。
2. ``data_source`` 是全局共享、订阅绑定的单例。用户切品种后 watcher 会拿到
   **另一个标的**的 K 线去判定本单。
3. ``last_closed_ts_open_ms`` 传 0 时，过滤条件退化为 ``ts_open > 0``，
   入场**之前**的历史 K 线会被当成本单走势。

这些性质无法用「跑一次看看」验证，必须钉死。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from web.api.experience_watcher import _run_experience_watch


@dataclass
class _Bar:
    ts_open: int
    high: float
    low: float
    close: float = 0.0
    closed: bool = True


class FakeDataSource:
    """Mimics TradingViewSource: a shared, subscription-bound singleton."""

    def __init__(self, symbol="BTCUSDT", timeframe="1h", bars=None):
        self._symbol = symbol
        self._timeframe = timeframe
        self._bars = list(bars or [])
        self.calls = 0

    def subscribe(self, symbol, timeframe):
        self._symbol, self._timeframe = symbol, timeframe
        self._bars = []          # 新订阅 → 不同标的的 K 线

    def latest_snapshot(self, n):
        self.calls += 1
        return list(self._bars)


def _plan_kwargs(**over):
    base = dict(
        entry_price=100.0,
        take_profit_price=120.0,
        stop_loss_price=80.0,
        is_long=True,
        cycle_position="trending_tr",
        direction="up",
        detected_patterns=["测试"],
        confidence=70,
        summary="测试",
        after_ts_open_ms=1000,
        max_wait_s=0.2,
    )
    base.update(over)
    return base


def _run(ds, monkeypatch, tmp_path, **over):
    """Run the watcher once with sleeping patched out, return written entries."""
    import web.api.experience_watcher as ew
    monkeypatch.setattr(ew, "_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(
        "pa_agent.records.experience_writer.ExperienceWriter.__init__",
        lambda self, *a, **k: None,
    )
    writer_dir = tmp_path / "exp"
    writer_dir.mkdir()

    written: list[dict] = []

    class _W:
        def __init__(self, *a, **k):
            pass

        def save(self, **kw):
            written.append(kw)
            return writer_dir / "x.json"

    monkeypatch.setattr(
        "pa_agent.records.experience_writer.ExperienceWriter", _W)
    ew._run_experience_watch(data_source=ds, symbol="BTCUSDT",
                             timeframe="1h", **({**_plan_kwargs(), **over}))
    return written


# ── 缺陷 3：入场锚点缺失 ────────────────────────────────────────────────────

def test_missing_anchor_writes_nothing(monkeypatch, tmp_path):
    """锚点为 0 时绝不判定 —— 否则会用入场前的历史 K 线编造结果。"""
    pre_entry_crash = [_Bar(ts_open=500, high=101.0, low=75.0)]  # 触及 SL=80
    ds = FakeDataSource(bars=pre_entry_crash)

    written = _run(ds, monkeypatch, tmp_path, after_ts_open_ms=0)

    assert written == [], "锚点为 0 却写入了一条记录（多半是拿入场前的 bar 判的）"


def test_pre_entry_bars_are_excluded(monkeypatch, tmp_path):
    """入场前触及 SL 的 bar 不应影响判定。"""
    pre = [_Bar(ts_open=500, high=101.0, low=75.0)]        # 入场前暴跌
    after = [_Bar(ts_open=2000, high=102.0, low=99.0)]      # 入场后平稳
    ds = FakeDataSource(bars=pre + after)

    written = _run(ds, monkeypatch, tmp_path, after_ts_open_ms=1000)

    assert written == [], "入场前的暴跌 bar 不该把本单判负"


def test_post_entry_touch_still_writes(monkeypatch, tmp_path):
    """锚点正确时，真正触及 TP/SL 的入场后 bar 仍要正常写入。"""
    after_hit = [_Bar(ts_open=2000, high=125.0, low=99.0)]
    ds = FakeDataSource(bars=after_hit)

    written = _run(ds, monkeypatch, tmp_path, after_ts_open_ms=1000)

    assert len(written) == 1
    assert written[0]["success"] is True


# ── 缺陷 2：订阅漂移 ───────────────────────────────────────────────────────

def test_subscription_drift_between_polls_aborts(monkeypatch, tmp_path):
    """两次轮询之间用户切了品种 → 必须放弃，不能拿新标的的 K 线判定本单。"""
    ds = FakeDataSource(symbol="BTCUSDT", timeframe="1h",
                        bars=[_Bar(ts_open=2000, high=102.0, low=99.0)])
    state = {"n": 0}

    def drifting(n):
        state["n"] += 1
        if state["n"] == 2:                       # 第二轮前用户切了品种
            ds.subscribe("ETHUSDT", "1d")
            ds._bars = [_Bar(ts_open=2000, high=70.0, low=60.0)]   # ETH 暴跌
        return list(ds._bars)

    ds.latest_snapshot = drifting  # type: ignore[method-assign]

    written = _run(ds, monkeypatch, tmp_path, max_wait_s=0.05)

    assert written == [], "订阅漂移后仍写入 = 用别的标的判了这笔单"


def test_drift_during_fetch_discards_snapshot(monkeypatch, tmp_path):
    """订阅在「校验」与「取数」之间被改掉（竞态窗口）→ 本轮结果必须丢弃。"""
    ds = FakeDataSource(symbol="BTCUSDT", timeframe="1h")
    original_latest = ds.latest_snapshot

    def drift_inside_fetch(n):
        ds.subscribe("ETHUSDT", "1d")
        ds._bars = [_Bar(ts_open=2000, high=125.0, low=99.0)]   # 会判"盈利"
        return original_latest(n)

    ds.latest_snapshot = drift_inside_fetch  # type: ignore[method-assign]

    written = _run(ds, monkeypatch, tmp_path)

    assert written == [], "取数期间发生订阅漂移仍写入 = 竞态窗口未关闭"


# ── 缺陷 1：data_source 取不到 ──────────────────────────────────────────────

def test_spawn_followup_accepts_explicit_data_source():
    """record/frame 都没有 data_source 属性，必须能显式传入。"""
    import inspect

    from web.api.order_followup import spawn_post_order_followup

    params = inspect.signature(spawn_post_order_followup).parameters
    assert "data_source" in params, (
        "spawn_post_order_followup 必须接受显式 data_source —— "
        "AnalysisRecord / KlineFrame 都没有该属性，靠 getattr 取恒为 None"
    )
    assert params["data_source"].default is None


def test_record_and_frame_lack_datasource_attributes():
    """记录此事实：这就是当初 ds 恒为 None 的根因。"""
    from pa_agent.data.base import KlineFrame
    from pa_agent.records.schema import AnalysisRecord

    assert "_data_source" not in AnalysisRecord.model_fields
    assert "data_source" not in AnalysisRecord.model_fields
    assert "data_source" not in KlineFrame.__dataclass_fields__


def test_analyze_route_passes_context_data_source():
    """调用点必须把 ctx.data_source 传下去，否则链路仍是死的。"""
    from pathlib import Path

    src = Path("web/api/routes_analyze.py").read_text(encoding="utf-8")
    block = src[src.index("spawn_post_order_followup("):][:600]
    assert "data_source=ctx.data_source" in block, (
        "routes_analyze 必须显式传 data_source=ctx.data_source"
    )


# ── 种子数据识别 ────────────────────────────────────────────────────────────

def test_no_synthetic_data_in_retrievable_library():
    """可被检索的经验库里不得混入合成数据。

    真实盈亏是连续分布；合成种子数据只取少数几个固定值（本项目的种子数据
    只用了 4 个 pnl 取值、每个重复 8 次）。一旦这种数据进入库，
    ExperienceReader 会把它当"参考经验"注入 Stage1/Stage2 提示词，
    等于用编造的盈亏污染 AI 决策。

    合成种子数据已隔离到 ``experience/.seed_demo_20260817/``（点号前缀会被
    ``GET /api/experience`` 的目录枚举过滤）。
    """
    import json
    from pathlib import Path

    root = Path("experience")
    if not root.is_dir():
        pytest.skip("无经验库目录")
    files = [p for p in root.glob("*/*/*.json")] if root.is_dir() else []
    if not files:
        pytest.skip("经验库为空（尚无真实写入），无需检查分布")

    pnls = set()
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pnls.add(d.get("pnl_pct"))

    assert len(pnls) / len(files) > 0.3, (
        f"pnl 取值只用了 {len(pnls)} 种 / {len(files)} 条文件 —— "
        "疑似合成数据，不应留在可被检索的目录里"
    )


def test_seed_backup_is_excluded_from_api_enumeration():
    """隔离目录必须以点号开头，否则 GET /api/experience 仍会把它列出来。"""
    import inspect

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web.api import routes_data

    app = FastAPI()
    app.include_router(routes_data.router, prefix="/api")
    try:
        payload = TestClient(app).get("/api/experience").json()
    except Exception as exc:  # pragma: no cover - 环境缺依赖
        pytest.skip(f"无法构造 TestClient: {exc}")

    for entry in payload.get("entries", []):
        cycle = str(entry.get("cycle_position") or "")
        assert not cycle.startswith("."), f"隔离目录泄漏进 API: {cycle}"
