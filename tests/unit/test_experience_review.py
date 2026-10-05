"""复盘记录的状态机（P3）。

经验条目本身有一条状态机（pending → win/loss/unresolved，见
``test_experience_two_stage``）。复盘是**挂在条目上的一条独立记录**，
它自己的状态机是：

    (无复盘) ──点击复盘──→ running ──┬─→ saved     （模型正常返回，已落库）
                                     ├─→ failed    （模型报错 / 客户端不可用）
                                     ├─→ empty     （模型返回空内容）
                                     └─→ abandoned （流中途断开，用户关页面）

关键不变式：

1. **只有 saved 会落库**，其余三种都不写 —— 否则库里会堆满半截复盘，
   而半截复盘会被当成结论喂回提示词。
2. **saved 不改条目本身**的状态与时序。复盘是补充，不是结算。
3. **可以重跑并留历史**（独立表），取用时永远取最新一版。
4. **能进提示词**：这是复盘存在的唯一理由。落库却不渲染，等于白做。
5. **按用户隔离**：A 读不到 B 的复盘。
"""
from __future__ import annotations

import pytest

from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.experience_writer import ExperienceWriter
from pa_agent.storage.experience_repo import attach_review, latest_review, list_entries


@pytest.fixture(autouse=True)
def hub(tmp_path):
    from pa_agent.storage.db import reset_hub_for_tests

    h = reset_hub_for_tests(tmp_path / "iso.db")
    yield h
    h.close_all()


@pytest.fixture()
def writer():
    return ExperienceWriter()


@pytest.fixture()
def entry_id(writer):
    return writer.save(
        cycle_position="trending_tr", direction="做多",
        detected_patterns=["均线多头排列"], confidence=70,
        summary="强势突破", symbol="BTCUSDT", timeframe="1h",
        entry_price=100.0, success=True, pnl_pct=2.5,
    )


#: 词表已统一到 ``review_program.VERDICTS``（程序层那份）—— 两处各写一套时，
#: 同一个提示词字段会出现两种措辞，而 LLM 那套从未被闭词表校验过。
_REVIEW = {
    "verdict": "判断成立但运气不佳",
    "reusable_criteria": "顺大周期方向，等回踩不追高",
}


# ── 状态转移 ────────────────────────────────────────────────────────────────

def test_starts_with_no_review(entry_id):
    assert latest_review(entry_id) is None


def test_saved_review_is_retrievable(entry_id):
    attach_review({**_REVIEW, "content": "正文"}, entry_id=entry_id,
                  model="m1", **_REVIEW)
    got = latest_review(entry_id)
    assert got["verdict"] == _REVIEW["verdict"]
    assert got["reusable_criteria"] == _REVIEW["reusable_criteria"]
    assert got["model"] == "m1"
    assert got["payload"]["content"] == "正文"


def test_rerun_keeps_history_and_returns_latest(entry_id):
    """**回归守卫**：复盘可重跑，且**必须留得住历史**。

    独立表正是为此存在 —— 若把结论写进 ``content_json`` 的一个字段，重跑
    会把上一版覆盖掉，「当时用的是哪个模型 / 怎么判断的」就再也找不回来。
    """
    attach_review({**_REVIEW, "v": 1}, entry_id=entry_id, model="m1", **_REVIEW)
    attach_review({**_REVIEW, "v": 2, "verdict": "第二版结论",
                   "reusable_criteria": "第二版判据"},
                  entry_id=entry_id, model="m2", **_REVIEW)

    got = latest_review(entry_id)
    assert got["payload"]["v"] == 2, "取用时必须是最新一版"
    rows = list_entries(user_id="admin", status="win")
    assert len(rows) == 1, "重跑复盘不得给经验条目本身增加行数"


def test_review_does_not_mutate_the_entry(entry_id):
    """复盘是补充，**不改**条目的状态与时序。"""
    before = latest_review(entry_id)
    attach_review({**_REVIEW, "content": "x"}, entry_id=entry_id, **_REVIEW)
    from pa_agent.storage.experience_repo import get_entry

    rec = get_entry(entry_id, user_id="admin")
    assert rec["status"] == "win", "条目状态不得被复盘改动"
    assert rec["summary"] == "强势突破"
    assert before is None


def test_failed_run_writes_nothing(entry_id, monkeypatch):
    """模型抛异常时**不得**留下半截复盘。

    ``routes_experience_review._persist_review`` 在异常路径上根本不会被调用；
    这里从仓储侧再钉一道：空内容 / 未调用即不应有行。
    """
    from web.api import routes_experience_review as mod

    class _Boom:
        def stream_chat(self, *a, **k):
            raise RuntimeError("模型炸了")

    class _Ctx:
        settings = type("S", (), {"provider": type("P", (), {"model": "m"})()})()

    mod._persist_review(entry_id, "admin", _Boom(), _Ctx())
    assert latest_review(entry_id) is None, "失败不得落库"


def test_empty_content_is_not_persisted(entry_id):
    """模型返回空内容时不得落库 —— 空复盘会占掉「最新一版」的位置，
    把真正有内容的那版挤掉。"""
    from web.api import routes_experience_review as mod

    class _Empty:
        content = "   "
        reasoning_content = ""

    class _Ctx:
        settings = type("S", (), {"provider": type("P", (), {"model": "m"})()})()

    mod._persist_review(entry_id, "admin", _Empty(), _Ctx())
    assert latest_review(entry_id) is None


# ── 用户隔离 ────────────────────────────────────────────────────────────────

def test_review_is_user_scoped(entry_id):
    attach_review({**_REVIEW, "content": "A 的复盘"}, entry_id=entry_id,
                  user_id="alice", **_REVIEW)
    assert latest_review(entry_id, user_id="alice") is not None
    assert latest_review(entry_id, user_id="bob") is None, "B 不得读到 A 的复盘"


def test_entry_with_review_of_other_user_is_not_exposed(writer):
    eid = writer.save(cycle_position="trending_tr", direction="做多",
                      detected_patterns=["x"], confidence=70, summary="A的",
                      symbol="BTCUSDT", timeframe="1h", entry_price=100.0,
                      success=True, user_id="alice")
    attach_review({**_REVIEW}, entry_id=eid, user_id="alice", **_REVIEW)
    from pa_agent.storage.experience_repo import get_entry

    assert get_entry(eid, user_id="alice") is not None
    assert get_entry(eid, user_id="bob") is None


# ── 端到端：复盘必须真的进提示词 ─────────────────────────────────────────────

def test_review_reaches_the_prompt(entry_id):
    """**回归守卫**：落库却不渲染 = 复盘白做。

    原版本直接调 ``attach_review(..., verdict=...)`` —— 那条分支只有测试会走，
    而生产路径压根不传这两个参数，于是复盘**从未进过任何提示词**而测试全绿。
    现在一律走生产路径 ``_persist_review``。
    """
    from web.api import routes_experience_review as rv

    class _R:
        content = (
            "## 结论\n判断成立但运气不佳\n\n## 归因\n- 对的部分: 形态正确\n"
            "- 错的部分: 止损太紧\n\n## 当时能否预见\n- 动能衰减\n\n"
            "## 改进建议\n- 止损放到结构位\n\n## 下次同类 setup 的判据\n顺大周期方向，等回踩不追高"
        )
        reasoning_content = "r"

    class _C:
        class _P:
            model = "m"
        settings = type("S", (), {"provider": _P()})()

    rv._persist_review(entry_id, "admin", _R(), _C())

    from pa_agent.ai.prompt_assembler import PromptAssembler

    hits = ExperienceReader().read_for_stage2("trending_tr", direction="bullish",
                                              patterns=["均线多头排列"])
    out = PromptAssembler._render_experience(hits, max_chars_per_entry=400)

    assert "顺大周期方向" in out, "复盘判据必须出现在提示词里"
    assert "判断成立但运气不佳" in out
    assert len(out) < 900, "复盘不得把提示词撑爆"


def test_prompt_stays_bounded_when_context_is_huge(writer):
    """即便条目带着巨大的 analysis_context，注入块也必须受字符预算约束。"""
    writer.save(
        cycle_position="trending_tr", direction="做多",
        detected_patterns=["x"], confidence=70, summary="大 payload",
        symbol="BTCUSDT", timeframe="1h", entry_price=100.0, success=True,
        extra={"analysis_context": {"stage1": {"k": "y" * 8000}},
               "bars_snapshot": [{"ts_open": i} for i in range(200)]},
    )
    from pa_agent.ai.prompt_assembler import PromptAssembler

    hits = ExperienceReader().read_for_stage2("trending_tr", direction="bullish")
    out = PromptAssembler._render_experience(hits, max_chars_per_entry=400)
    assert "yyyy" not in out, "analysis_context 不得挤进注入块"
    assert "大 payload" in out, "摘要字段必须保留"


def test_entry_without_review_renders_cleanly(entry_id):
    from pa_agent.ai.prompt_assembler import PromptAssembler

    hits = ExperienceReader().read_for_stage2("trending_tr", direction="bullish")
    out = PromptAssembler._render_experience(hits)
    assert "复盘要点" not in out, "没有复盘时不得出现空标题"

def test_successful_review_is_persisted_by_the_route(entry_id):
    """**回归守卫**：正常返回的复盘**必须**落库。

    与上面两条「失败/空内容不写」互补：只测「不该写的没写」的话，把持久化
    整段删掉测试照样全绿 —— 而那意味着复盘功能整个消失且无人察觉。
    """
    from web.api import routes_experience_review as mod

    class _Reply:
        content = (
            "## 结论\n判断成立但运气不佳\n\n## 归因\n- 对的部分: a\n- 错的部分: b\n\n"
            "## 当时能否预见\n- c\n\n## 改进建议\n- d\n\n"
            "## 下次同类 setup 的判据\n顺大周期方向"
        )
        reasoning_content = "推理过程"

    class _Ctx:
        settings = type("S", (), {"provider": type("P", (), {"model": "m-x"})()})()

    mod._persist_review(entry_id, "admin", _Reply(), _Ctx())

    # 取用走 latest_llm_review：解析失败（verdict 为空）的版本刻意**不可取用**，
    # 它会把该有的确定性事实整个顶掉。
    from pa_agent.storage.experience_repo import latest_llm_review

    got = latest_llm_review(entry_id, user_id="admin")
    assert got is not None, "正常复盘必须落库且可取用"
    assert "顺大周期方向" in got["payload"]["content"]
    assert got["payload"]["reasoning"] == "推理过程"
    assert got["model"] == "m-x", "必须记下当时用的模型，否则复盘不可追溯"
