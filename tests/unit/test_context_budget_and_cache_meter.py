"""Tests for the context-window pre-flight guard and the token-cache meter."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.ai.deepseek_client import _extract_cached_prompt_tokens
from pa_agent.config.settings import Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator


def _orchestrator(context_window: int, max_output_tokens: int | None = None) -> TwoStageOrchestrator:
    s = Settings()
    s.provider.context_window = context_window
    if max_output_tokens is not None:
        s.provider.max_output_tokens = max_output_tokens
    obj = TwoStageOrchestrator.__new__(TwoStageOrchestrator)
    obj._settings = s
    return obj


_CN = "这是一段真实的中文行情分析提示词，包含K线数据与策略规则。" * 500


def test_small_prompt_passes():
    orch = _orchestrator(context_window=1_000_000)
    assert orch._check_context_budget([{"role": "user", "content": "hi"}]) is None


def test_oversized_prompt_is_rejected():
    orch = _orchestrator(context_window=5_000)
    result = orch._check_context_budget([{"role": "system", "content": _CN}])
    assert result is not None
    assert result["type"] == "context_overflow"
    assert result["failed_check"] == "context_window"
    assert "analysis_bar_count" in result["message"]
    assert result["prompt_tokens"] > result["budget_tokens"]


def test_zero_context_window_disables_guard():
    """A provider that declares no window must not be blocked."""
    orch = _orchestrator(context_window=0)
    assert orch._check_context_budget([{"role": "system", "content": _CN}]) is None


def test_missing_settings_disables_guard():
    obj = TwoStageOrchestrator.__new__(TwoStageOrchestrator)
    obj._settings = None
    assert obj._check_context_budget([{"role": "system", "content": _CN}]) is None


def test_budget_reserves_completion_headroom():
    """max_tokens must be reserved, otherwise prompt+completion overflows."""
    tight = _orchestrator(context_window=10_000, max_output_tokens=9_000)
    # budget = 10_000 - max(9_000, 1_000) = 1_000
    msgs = [{"role": "user", "content": _CN}]
    result = tight._check_context_budget(msgs)
    assert result is not None
    assert result["budget_tokens"] == 1_000


# ── cached-token meter ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (SimpleNamespace(prompt_cache_hit_tokens=1234), 1234),
        (SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=999)), 999),
        (SimpleNamespace(cache_read_input_tokens=777), 777),
        (SimpleNamespace(model_extra={"cached_tokens": 555}), 555),
        ({"cached_tokens": 444}, 444),
        ({"prompt_tokens_details": {"cached_tokens": 333}}, 333),
        ({"prompt_tokens_details": SimpleNamespace(cached_tokens=222)}, 222),
        (SimpleNamespace(prompt_tokens=100), 0),
        (None, 0),
    ],
)
def test_cached_token_extraction_shapes(usage, expected):
    assert _extract_cached_prompt_tokens(usage) == expected