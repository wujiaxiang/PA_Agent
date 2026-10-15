"""Unit / orchestrator-wiring tests for SenseNova-only free-model fallback."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import openai
import pytest

from pa_agent.ai.json_validator import Ok
from pa_agent.ai.sensenova_fallback import (
    FreeCandidate,
    FreeStageFailure,
    FreeStageSuccess,
    build_candidates,
    call_free_candidate,
    is_sensenova_fallback_eligible,
    mark_cooldown,
    normalize_base_url,
    reset_fallback_state_for_tests,
    run_free_stage_fallback,
)
from pa_agent.config.settings import Settings
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken
from tests.fixtures.validators import schema_test_validator
from tests.integration.conftest import (
    VALID_STAGE1,
    VALID_STAGE2,
    make_frame,
    make_reply,
)


@pytest.fixture(autouse=True)
def _clear_fallback_state() -> None:
    reset_fallback_state_for_tests()
    yield
    reset_fallback_state_for_tests()


def _sensenova_settings() -> Settings:
    settings = Settings()
    settings.provider.base_url = "https://token.sensenova.cn/v1"
    settings.provider.api_key = "sk-PRIMARY-SECRET"
    return settings


def _assembler() -> MagicMock:
    mock = MagicMock()
    mock.build_stage1.return_value = [{"role": "user", "content": "stage1"}]
    mock.build_stage2.return_value = [{"role": "user", "content": "stage2"}]
    mock.build_stage2_continuation.return_value = [
        {"role": "user", "content": "stage2"},
        {"role": "user", "content": "cont"},
    ]
    return mock


def _orchestrator(
    settings: Settings,
    client: Any | None = None,
    *,
    validator: Any | None = None,
    pending_writer: Any | None = None,
) -> TwoStageOrchestrator:
    from pa_agent.ai.router import route_strategy_files

    return TwoStageOrchestrator(
        client=client or MagicMock(),
        assembler=_assembler(),
        router=route_strategy_files,
        validator=validator or schema_test_validator(),
        pending_writer=pending_writer or MagicMock(),
        exp_reader=MagicMock(read_top5=MagicMock(return_value=[]), read_for_stage2=MagicMock(return_value=[])),
        settings=settings,
    )


def _free_ok_payload(content_obj: dict[str, Any], *, model: str = "inclusionai/ling-3.1-flash") -> dict:
    return {
        "id": "free-ok",
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content_obj, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "cost": 0,
        },
    }


def _http_post_stage(content_obj: dict[str, Any]):
    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        assert "authorization" not in {k.lower() for k in headers}
        assert headers.get("user-agent", "").startswith("PA-Agent/")
        if body['model'] in {'inclusionai/ling-3.1-flash', 'nvidia/nemotron-3.5-lightning:free'}:
            assert body['reasoning'] == {'enabled': False}
        else:
            assert 'reasoning' not in body  # leave forced-thinking models alone
        return 200, _free_ok_payload(content_obj, model=body["model"])

    return _post


def test_normalize_base_url_sensenova_variants() -> None:
    assert normalize_base_url("https://token.sensenova.cn/v1") == (
        "https://token.sensenova.cn/v1"
    )
    assert normalize_base_url("https://token.sensenova.cn/v1/") == (
        "https://token.sensenova.cn/v1"
    )
    assert normalize_base_url("HTTPS://TOKEN.SENSENOVA.CN/v1/") == (
        "https://token.sensenova.cn/v1"
    )
    assert is_sensenova_fallback_eligible("https://token.sensenova.cn/v1/")
    assert not is_sensenova_fallback_eligible("https://api.deepseek.com")
    assert not is_sensenova_fallback_eligible("https://token.sensenova.cn/v1/extra")


def test_submit_primary_success_does_not_call_free_fallback() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = [
        make_reply(VALID_STAGE1),
        make_reply(VALID_STAGE2),
    ]
    orch = _orchestrator(settings, client)
    with patch(
        "pa_agent.ai.sensenova_fallback.run_free_stage_fallback"
    ) as run_fb:
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    run_fb.assert_not_called()
    assert record.exception is None
    assert record.stage1_diagnosis is not None
    assert record.stage2_decision is not None


def test_submit_non_sensenova_url_does_not_call_free_fallback() -> None:
    settings = Settings()
    settings.provider.base_url = "https://api.deepseek.com"
    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="primary down"
    )
    orch = _orchestrator(settings, client)
    with patch(
        "pa_agent.ai.sensenova_fallback.run_free_stage_fallback"
    ) as run_fb:
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    run_fb.assert_not_called()
    assert record.exception is not None
    assert record.exception.get("type") == "network_error"


def test_submit_stage1_network_error_recovers_via_free_and_validator() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = [
        openai.APIConnectionError(request=MagicMock(), message="stage1 network"),
        make_reply(VALID_STAGE2),
    ]
    pending = MagicMock()
    orch = _orchestrator(settings, client, pending_writer=pending)
    validate_spy = MagicMock(wraps=orch._validator.validate)
    orch._validator.validate = validate_spy  # type: ignore[method-assign]

    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate(
                "kilo",
                "inclusionai/ling-3.1-flash",
                "https://api.kilo.ai/api/gateway",
            )
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)

    assert record.exception is None
    assert record.stage1_diagnosis is not None
    assert record.stage2_decision is not None
    assert (record.stage1_response or {}).get("fallback_model") == (
        "inclusionai/ling-3.1-flash"
    )
    assert any(c.args and c.args[0] == "stage1" for c in validate_spy.call_args_list)
    pending.save_full.assert_called_once()


def test_submit_stage1_value_error_recovers_via_free() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = [
        ValueError("ordinary primary failure"),
        make_reply(VALID_STAGE2),
    ]
    orch = _orchestrator(settings, client)
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert record.stage1_diagnosis is not None


def test_submit_stage1_illegal_json_recovers_via_free() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    bad = make_reply({})
    bad.content = "NOT_JSON{{"
    client.stream_chat.side_effect = [bad, make_reply(VALID_STAGE2)]
    # Disable validation retries so illegal JSON becomes ValidationError immediately.
    orch = _orchestrator(settings, client)
    orch._settings.validation.retry_enabled = False  # type: ignore[union-attr]
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate(
                "kilo",
                "nvidia/nemotron-3.5-lightning:free",
                "https://api.kilo.ai/api/gateway",
            )
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert (record.stage1_response or {}).get("fallback_model") == (
        "nvidia/nemotron-3.5-lightning:free"
    )


def test_submit_stage1_validate_retry_exception_recovers_via_free() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    bad = make_reply({})
    bad.content = "NOT_JSON{{"
    client.stream_chat.side_effect = [
        bad,
        openai.APIConnectionError(
            request=MagicMock(), message="validate retry network fail"
        ),
        make_reply(VALID_STAGE2),
    ]
    orch = _orchestrator(settings, client)
    orch._settings.validation.retry_enabled = True  # type: ignore[union-attr]
    orch._settings.validation.retry_max = 2  # type: ignore[union-attr]
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE1),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert record.stage1_diagnosis is not None


def test_submit_stage2_network_and_value_error_and_json_and_retry() -> None:
    """Stage2: network / ValueError / illegal JSON / validate-retry each recover."""
    cases: list[tuple[str, Any]] = [
        ("network", openai.APIConnectionError(request=MagicMock(), message="s2 net")),
        ("value", ValueError("s2 value")),
    ]
    for label, exc in cases:
        reset_fallback_state_for_tests()
        settings = _sensenova_settings()
        client = MagicMock()
        client.stream_chat.side_effect = [make_reply(VALID_STAGE1), exc]
        orch = _orchestrator(settings, client)
        with patch(
            "pa_agent.ai.sensenova_fallback.build_candidates",
            return_value=[
                FreeCandidate(
                    "kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway"
                )
            ],
        ), patch(
            "pa_agent.ai.sensenova_fallback._default_http_post",
            side_effect=_http_post_stage(VALID_STAGE2),
        ):
            record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
        assert record.exception is None, label
        assert record.stage2_decision is not None, label
        assert (record.stage2_response or {}).get("fallback_provider") == "kilo", label

    # illegal JSON
    reset_fallback_state_for_tests()
    settings = _sensenova_settings()
    client = MagicMock()
    bad = make_reply({})
    bad.content = "%%%"
    client.stream_chat.side_effect = [make_reply(VALID_STAGE1), bad]
    orch = _orchestrator(settings, client)
    orch._settings.validation.retry_enabled = False  # type: ignore[union-attr]
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE2),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert record.stage2_decision is not None

    # validate_with_retry raises
    reset_fallback_state_for_tests()
    settings = _sensenova_settings()
    client = MagicMock()
    bad = make_reply({})
    bad.content = "%%%"
    client.stream_chat.side_effect = [
        make_reply(VALID_STAGE1),
        bad,
        RuntimeError("s2 retry boom"),
    ]
    orch = _orchestrator(settings, client)
    orch._settings.validation.retry_enabled = True  # type: ignore[union-attr]
    orch._settings.validation.retry_max = 2  # type: ignore[union-attr]
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_http_post_stage(VALID_STAGE2),
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    assert record.exception is None
    assert record.stage2_decision is not None


def test_submit_cancel_not_swallowed() -> None:
    from pa_agent.ai.deepseek_client import CancelledError

    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="primary"
    )
    orch = _orchestrator(settings, client)
    token = CancelToken()

    def _build(*_a, **_k):  # noqa: ANN001
        token.set()
        return [
            FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway")
        ]

    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        side_effect=_build,
    ):
        with pytest.raises(CancelledError):
            orch.submit(make_frame(), token, lambda _e: None)


def test_submit_all_free_candidates_fail_closed_with_trace() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="primary"
    )
    pending = MagicMock()
    orch = _orchestrator(settings, client, pending_writer=pending)

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        return 503, {"error": {"message": "down"}}

    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[
            FreeCandidate("kilo", "a", "https://api.kilo.ai/api/gateway"),
            FreeCandidate("kilo", "b", "https://api.kilo.ai/api/gateway"),
        ],
    ), patch(
        "pa_agent.ai.sensenova_fallback._default_http_post",
        side_effect=_post,
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)

    assert record.exception is not None
    fb = record.exception.get("sensenova_free_fallback") or {}
    assert fb.get("triggered") is True
    assert fb.get("success") is False
    assert len(fb.get("attempts") or []) == 2
    pending.save_partial.assert_called()


def test_submit_clears_stale_fallback_failure_across_runs() -> None:
    settings = _sensenova_settings()
    client = MagicMock()
    client.stream_chat.side_effect = openai.APIConnectionError(
        request=MagicMock(), message="primary"
    )
    orch = _orchestrator(settings, client)
    orch._last_sensenova_fallback_failure = FreeStageFailure(  # type: ignore[attr-defined]
        attempts=[],
        message="stale-from-previous-submit",
    )
    with patch(
        "pa_agent.ai.sensenova_fallback.build_candidates",
        return_value=[],
    ):
        record = orch.submit(make_frame(), CancelToken(), lambda _e: None)
    fb = (record.exception or {}).get("sensenova_free_fallback") or {}
    assert fb.get("summary") != "stale-from-previous-submit"


def test_non_sensenova_value_error_still_raises() -> None:
    settings = Settings()
    settings.provider.base_url = "https://api.deepseek.com"
    client = MagicMock()
    client.stream_chat.side_effect = ValueError("not network")
    orch = _orchestrator(settings, client)
    with pytest.raises(ValueError, match="not network"):
        orch.submit(make_frame(), CancelToken(), lambda _e: None)


def test_network_error_types_are_ordinary() -> None:
    settings = Settings()
    orch = _orchestrator(settings)
    assert orch._is_network_error(
        openai.APIConnectionError(request=MagicMock(), message="Connection error.")
    )
    assert orch._is_network_error(TimeoutError("timed out"))
    from pa_agent.ai.deepseek_client import CancelledError

    assert not orch._is_network_error(CancelledError("stop"))


def test_build_candidates_filters_cooldown_before_cap() -> None:
    """Cooldown filter must run before the max-6 cut so usable models are kept."""

    def _get(url, *, headers, timeout):  # noqa: ANN001
        assert headers.get("user-agent", "").startswith("PA-Agent/")
        ids = [
            "cool-1",
            "cool-2",
            "cool-3",
            "cool-4",
            "cool-5",
            "cool-6",
            "inclusionai/ling-3.1-flash",
            "nvidia/nemotron-3.5-lightning:free",
            "nvidia/nemotron-3.5-content-safety:free",
        ]
        return {"data": [{"id": i, "isFree": True} for i in ids]}

    # Put the first six preferred/sorted cool-* into cooldown; preferred verified
    # models must still appear because cooldown is filtered before capping.
    for mid in ("cool-1", "cool-2", "cool-3", "cool-4", "cool-5", "cool-6"):
        mark_cooldown(FreeCandidate("kilo", mid, "https://api.kilo.ai/api/gateway"))

    cands = build_candidates(http_get=_get, max_candidates=6)
    models = [c.model for c in cands]
    assert "inclusionai/ling-3.1-flash" in models
    assert "nvidia/nemotron-3.5-lightning:free" in models
    assert "nvidia/nemotron-3.5-content-safety:free" not in models
    assert models[0] == "inclusionai/ling-3.1-flash"
    assert "kilo-auto/free" not in models or models.index(
        "inclusionai/ling-3.1-flash"
    ) < models.index("kilo-auto/free") if "kilo-auto/free" in models else True
    assert all(not m.startswith("cool-") for m in models)
    assert len(models) <= 6


def test_build_candidates_kilo_requires_is_free() -> None:
    def _get(url, *, headers, timeout):  # noqa: ANN001
        return {
            "data": [
                {"id": "paid/model", "isFree": False},
                {"id": "free/model", "isFree": True},
                {"id": "inclusionai/ling-3.1-flash", "isFree": True},
            ]
        }

    cands = build_candidates(http_get=_get, max_candidates=6)
    ids = [c.model for c in cands]
    assert "paid/model" not in ids
    assert "inclusionai/ling-3.1-flash" in ids
    assert ids[0] == "inclusionai/ling-3.1-flash"


def test_call_free_rejects_empty_reasoning_only_and_truncated() -> None:
    cand = FreeCandidate("kilo", "m", "https://api.kilo.ai/api/gateway")

    def _empty(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        }

    with pytest.raises(RuntimeError, match="empty response"):
        call_free_candidate(cand, [{"role": "user", "content": "x"}], http_post=_empty)

    def _reason_only(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [
                {
                    "message": {"content": "", "reasoning": "thinking..."},
                    "finish_reason": "stop",
                }
            ],
        }

    with pytest.raises(RuntimeError, match="reasoning-only"):
        call_free_candidate(cand, [{"role": "user", "content": "x"}], http_post=_reason_only)

    def _trunc(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [
                {
                    "message": {"content": '{"gate_result":"wait"'},
                    "finish_reason": "length",
                }
            ],
        }

    with pytest.raises(RuntimeError, match="truncated"):
        call_free_candidate(cand, [{"role": "user", "content": "x"}], http_post=_trunc)


def test_keyboard_interrupt_propagates_from_post() -> None:
    c1 = FreeCandidate("kilo", "a", "https://api.kilo.ai/api/gateway")

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_free_stage_fallback(
            messages=[{"role": "user", "content": "hi"}],
            validate=lambda _r: Ok(obj={}),
            primary_api_key="sk-PRIMARY",
            candidates=[c1],
            http_post=_post,
        )


def test_primary_api_key_never_sent_to_free_upstream() -> None:
    secret = "sk-MUST-NOT-LEAK-TO-FREE"
    seen: dict[str, Any] = {}

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        seen["headers"] = dict(headers)
        seen["body"] = body
        blob = str(headers) + str(body)
        assert secret not in blob
        return 200, {
            "id": "x",
            "model": body["model"],
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {},
        }

    reply = call_free_candidate(
        FreeCandidate("kilo", "inclusionai/ling-3.1-flash", "https://api.kilo.ai/api/gateway"),
        [{"role": "user", "content": "ping"}],
        primary_api_key=secret,
        http_post=_post,
    )
    assert reply.content == "ok"
    assert "authorization" not in {k.lower() for k in seen["headers"]}
    assert seen["headers"].get("user-agent", "").startswith("PA-Agent/")
    assert seen['body']['reasoning'] == {'enabled': False}


def test_wait_ok_from_validator_is_not_treated_as_error() -> None:
    c1 = FreeCandidate("kilo", "good", "https://api.kilo.ai/api/gateway")

    def _post(url, *, headers, body, timeout):  # noqa: ANN001
        return 200, {
            "choices": [
                {
                    "message": {"content": '{"gate_result":"wait"}'},
                    "finish_reason": "stop",
                }
            ],
        }

    outcome = run_free_stage_fallback(
        messages=[{"role": "user", "content": "hi"}],
        validate=lambda _r: Ok(obj={"gate_result": "wait"}),
        primary_api_key="sk-PRIMARY",
        candidates=[c1],
        http_post=_post,
    )
    assert isinstance(outcome, FreeStageSuccess)
    assert outcome.result.obj["gate_result"] == "wait"
