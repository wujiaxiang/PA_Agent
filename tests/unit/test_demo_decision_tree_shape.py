"""Demo 决策树数据必须与真实流水线同构。

回归背景：demo 曾用纯字符串 gate_trace + 缺字段的 decision_trace，
而前端决策树/可视化读的是 node_id/question/answer/reason/bar_range/skipped/section，
导致节点渲染成 "→ — —" 的空壳（demo 看起来"坏了"）。
真实分析链路一直正常 —— 这次修的是 demo 数据的保真度。
"""
from __future__ import annotations


def _demo():
    from web.api.routes_demo import _build_demo_record
    return _build_demo_record()


def test_gate_trace_entries_are_dicts_with_full_fields():
    gate = _demo()["stage1_diagnosis"]["gate_trace"]
    assert gate and all(isinstance(g, dict) for g in gate), "gate_trace 必须是 dict 列表"
    required = {"node_id", "question", "answer", "reason"}
    for g in gate:
        assert required <= set(g), f"gate_trace 缺字段: {required - set(g)}"


def test_decision_trace_entries_have_question_and_reason():
    trace = _demo()["stage2_decision"]["decision_trace"]
    assert trace and all(isinstance(t, dict) for t in trace)
    for t in trace:
        assert t.get("question"), "decision_trace 必须带 question，否则卡片无标题"
        assert "reason" in t, "decision_trace 必须带 reason"
        assert t.get("node_id")


def test_decision_trace_declares_sections():
    """section 用于可视化里的分组标题，缺失会让所有节点挤在一组。"""
    trace = _demo()["stage2_decision"]["decision_trace"]
    assert any(t.get("section") for t in trace)


def test_trace_includes_a_skipped_branch():
    """真实 trace 一定含跳过的分支（未走的决策路径），demo 应如实体现。"""
    trace = _demo()["stage2_decision"]["decision_trace"]
    assert any(t.get("skipped") is True for t in trace)


def test_terminal_shape_matches_real_pipeline():
    term = _demo()["stage2_decision"]["terminal"]
    assert isinstance(term, dict)
    assert term.get("node_id") and term.get("outcome") and term.get("label")


def test_demo_trace_fields_match_a_real_record():
    """与真实记录逐字段对齐，防止 demo 再次漂移。"""
    from pa_agent.records.analysis_history import load_record, list_record_paths
    import json

    real = None
    for p in list_record_paths(None)[:40]:
        r = load_record(p)
        if r and r.stage2_decision and (r.stage2_decision.get("decision_trace") or []):
            real = r
            break
    if real is None:
        return  # 环境无历史记录则跳过

    real_keys = set(real.stage2_decision["decision_trace"][0].keys())
    demo_keys = set(_demo()["stage2_decision"]["decision_trace"][0].keys())
    missing = {k for k in ("question", "answer", "reason", "node_id") if k in real_keys} - demo_keys
    assert not missing, f"demo decision_trace 缺少真实记录已有的字段: {missing}"

    real_term = real.stage2_decision.get("terminal") or {}
    demo_term = _demo()["stage2_decision"]["terminal"]
    assert set(real_term) == set(demo_term), "terminal 字段集合应与真实记录一致"
