"""SenseNova-only free-model fallback (public Kilo isFree pool).

Trigger gate: normalized ``provider.base_url`` equals
``https://token.sensenova.cn/v1``. On ordinary analysis failures (transport,
HTTP/429, empty body, JSON parse / schema validation, etc.) the orchestrator
may rotate a bounded set of public free candidates.

Rules:
- Never send the primary SenseNova API key to free upstreams.
- Kilo ``isFree: true`` discovery only; no OpenCode / EAC / login channels.
- User cancel / KeyboardInterrupt / SystemExit must propagate.
- Finite candidates + cooldowns; no infinite retry.
- Success requires a real upstream reply that also passes stage validation.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

SENSENOVA_CANONICAL_BASE = "https://token.sensenova.cn/v1"

KILO_BASE = "https://api.kilo.ai/api/gateway"
_KILO_UA = "PA-Agent/sensenova-fallback"

_LISTING_TTL_S = 30 * 60
_COOLDOWN_S = 5 * 60
_MAX_CANDIDATES = 6
_DEFAULT_TIMEOUT_S = 120.0
_DEFAULT_MAX_TOKENS = 8192

# Main-agent verified free chat models (HTTP 200, JSON, cost 0).
_PREFERRED_KILO = (
    "inclusionai/ling-3.1-flash",
    "nvidia/nemotron-3.5-lightning:free",
    "kilo-auto/free",
    "openrouter/free",
    "poolside/laguna-xs-2.1:free",
)

ValidateFn = Callable[[Any], Any]


def normalize_base_url(url: str | None) -> str:
    """Normalize an OpenAI-compatible base_url for exact SenseNova matching."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    if path and not path.startswith("/"):
        path = "/" + path
    return urlunsplit((scheme, netloc, path, "", ""))


def is_sensenova_fallback_eligible(base_url: str | None) -> bool:
    """True only when *base_url* normalizes to the SenseNova token gateway."""
    return normalize_base_url(base_url) == SENSENOVA_CANONICAL_BASE


def is_cancel_or_interrupt(exc: BaseException) -> bool:
    """True for user cancel / KeyboardInterrupt (must not be swallowed)."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return True
    try:
        from pa_agent.ai.deepseek_client import CancelledError

        if isinstance(exc, CancelledError):
            return True
    except Exception:  # noqa: BLE001
        pass
    name = type(exc).__name__
    return name in {"CancelledError", "KeyboardInterrupt", "SystemExit"}


@dataclass(frozen=True)
class FreeCandidate:
    """One free upstream target (provider + model)."""

    provider: str  # "kilo"
    model: str
    base_url: str


@dataclass
class FreeAttemptRecord:
    provider: str
    model: str
    ok: bool
    error: str = ""
    latency_ms: float = 0.0
    http_status: int | None = None
    traceback: str = ""


@dataclass
class FreeStageSuccess:
    reply: Any
    candidate: FreeCandidate
    result: Any = None  # JsonValidator Ok
    attempts: list[FreeAttemptRecord] = field(default_factory=list)


@dataclass
class FreeStageFailure:
    attempts: list[FreeAttemptRecord] = field(default_factory=list)
    message: str = "all free candidates failed"


class _ListingCache:
    def __init__(self) -> None:
        self.kilo_rows: list[dict[str, Any]] | None = None
        self.kilo_expires = 0.0


_CACHE = _ListingCache()
_COOLDOWNS: dict[tuple[str, str], float] = {}


def reset_fallback_state_for_tests() -> None:
    """Clear listing cache and cooldowns (unit tests only)."""
    _CACHE.kilo_rows = None
    _CACHE.kilo_expires = 0.0
    _COOLDOWNS.clear()


def _cooldown_key(candidate: FreeCandidate) -> tuple[str, str]:
    return (candidate.provider, candidate.model)


def mark_cooldown(candidate: FreeCandidate, *, seconds: float = _COOLDOWN_S) -> None:
    _COOLDOWNS[_cooldown_key(candidate)] = time.monotonic() + max(0.0, seconds)


def _in_cooldown(candidate: FreeCandidate) -> bool:
    until = _COOLDOWNS.get(_cooldown_key(candidate), 0.0)
    return time.monotonic() < until


def _is_kilo_free_row(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and row.get("isFree") is True
        and isinstance(row.get("id"), str)
        and bool(row["id"].strip())
    )


def _is_safety_only_model(model_id: str) -> bool:
    """Exclude content-safety / guardrail models from chat fallback rotation."""
    mid = (model_id or "").lower()
    needles = (
        "content-safety",
        "llama-guard",
        "prompt-guard",
        "safety-guard",
        "moderation",
        "/guard-",
        "-guard:",
        "-guard/",
    )
    if any(n in mid for n in needles):
        return True
    # Common trailing /guard patterns: "...-guard", ".../guard"
    return mid.endswith("-guard") or mid.endswith("/guard") or ":guard" in mid


def fetch_kilo_free_models(
    *,
    http_get: Callable[..., Any] | None = None,
    now: Callable[[], float] | None = None,
    cancel_token: Any | None = None,
) -> list[str]:
    """Return Kilo free-pool model ids (``isFree: true`` only)."""
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled before free-model listing")

    clock = now or time.monotonic
    if _CACHE.kilo_rows is not None and clock() < _CACHE.kilo_expires:
        return [str(r["id"]) for r in _CACHE.kilo_rows if _is_kilo_free_row(r)]

    getter = http_get or _default_http_get
    payload = getter(
        f"{KILO_BASE}/models",
        headers={
            "accept": "application/json",
            "user-agent": _KILO_UA,
        },
        timeout=30.0,
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []
    free_rows = [r for r in rows if _is_kilo_free_row(r)]
    _CACHE.kilo_rows = free_rows
    _CACHE.kilo_expires = clock() + _LISTING_TTL_S
    return [str(r["id"]) for r in free_rows]


def _default_http_get(url: str, *, headers: dict[str, str], timeout: float) -> Any:
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


def build_candidates(
    *,
    http_get: Callable[..., Any] | None = None,
    max_candidates: int = _MAX_CANDIDATES,
    cancel_token: Any | None = None,
) -> list[FreeCandidate]:
    """Build a bounded rotation: filter cooldown first, then cap at *max_candidates*."""
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled before free candidate build")

    kilo_ids: list[str] = []
    try:
        kilo_ids = fetch_kilo_free_models(http_get=http_get, cancel_token=cancel_token)
    except Exception as exc:
        if is_cancel_or_interrupt(exc):
            raise
        logger.warning("Kilo free listing failed: %s", exc)

    kilo_set = {
        m for m in kilo_ids if isinstance(m, str) and m.strip() and not _is_safety_only_model(m)
    }
    ordered_kilo = [m for m in _PREFERRED_KILO if m in kilo_set]
    ordered_kilo.extend(sorted(m for m in kilo_set if m not in ordered_kilo))

    out: list[FreeCandidate] = []
    for mid in ordered_kilo:
        cand = FreeCandidate("kilo", mid, KILO_BASE)
        if _in_cooldown(cand):
            continue
        out.append(cand)
        if len(out) >= max_candidates:
            break
    return out


def _assert_key_isolation(
    *,
    primary_api_key: str | None,
    headers: dict[str, str],
    body: dict[str, Any],
) -> None:
    key = (primary_api_key or "").strip()
    if not key:
        return
    blob = json.dumps(headers, ensure_ascii=False) + json.dumps(body, ensure_ascii=False)
    if key in blob:
        raise RuntimeError("refusing to send primary API key to free upstream")


def _extract_message_text(payload: dict[str, Any]) -> tuple[str, str]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return "", ""
    content = msg.get("content") or ""
    reasoning = (
        msg.get("reasoning_content")
        or msg.get("reasoning")
        or ""
    )
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        content = "".join(parts)
    if not isinstance(content, str):
        content = str(content or "")
    if not isinstance(reasoning, str):
        reasoning = str(reasoning or "")
    return content, reasoning


def _choice_finish_reason(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason") or choices[0].get("native_finish_reason") or "")


def _to_ai_reply(
    payload: dict[str, Any],
    *,
    candidate: FreeCandidate,
    latency_ms: float,
) -> Any:
    from pa_agent.ai.deepseek_client import AIReply, AIUsage

    content, reasoning = _extract_message_text(payload)
    usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    usage = AIUsage(
        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
        cached_prompt_tokens=0,
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        total_tokens=int(usage_raw.get("total_tokens") or 0),
    )
    raw = {
        "id": payload.get("id") or "",
        "model": payload.get("model") or candidate.model,
        "content": content,
        "reasoning_content": reasoning,
        "finish_reason": _choice_finish_reason(payload),
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost": usage_raw.get("cost"),
        },
        "latency_ms": latency_ms,
        "fallback_provider": candidate.provider,
        "fallback_model": candidate.model,
        "fallback_base_url": candidate.base_url,
    }
    return AIReply(
        content=content,
        reasoning_content=reasoning,
        raw=raw,
        usage=usage,
        request_id=str(payload.get("id") or ""),
        latency_ms=latency_ms,
    )


def call_free_candidate(
    candidate: FreeCandidate,
    messages: list[dict[str, Any]],
    *,
    primary_api_key: str | None = None,
    cancel_token: Any | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    http_post: Callable[..., Any] | None = None,
) -> Any:
    """POST one non-stream chat completion to a free upstream.

    Raises CancelledError / KeyboardInterrupt / SystemExit unchanged. Other
    failures raise ``RuntimeError`` with a concise message (no secrets).
    """
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled before free-model call")

    if candidate.provider != "kilo":
        raise RuntimeError(f"unsupported free provider: {candidate.provider}")

    body: dict[str, Any] = {
        "model": candidate.model,
        "messages": messages,
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    # These exact routes were live-verified with reasoning disabled; Nemotron
    # also passed the complete PA Stage-1 prompt. Leave forced-thinking models alone.
    if candidate.model in {
        "inclusionai/ling-3.1-flash",
        "nvidia/nemotron-3.5-lightning:free",
    }:
        body["reasoning"] = {"enabled": False}
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": _KILO_UA,
    }
    url = f"{candidate.base_url.rstrip('/')}/chat/completions"

    _assert_key_isolation(
        primary_api_key=primary_api_key,
        headers=headers,
        body=body,
    )

    poster = http_post or _default_http_post
    t0 = time.monotonic()
    try:
        status, payload = poster(url, headers=headers, body=body, timeout=timeout_s)
    except Exception as exc:
        if is_cancel_or_interrupt(exc):
            raise
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: {exc}") from exc

    latency_ms = (time.monotonic() - t0) * 1000
    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled after free-model call")

    if status == 429:
        mark_cooldown(candidate)
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: HTTP 429 rate limited")
    if status >= 500:
        mark_cooldown(candidate, seconds=60.0)
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: HTTP {status} server error")
    if status >= 400:
        err_msg = ""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                err_msg = str(err.get("message") or "")
            elif isinstance(err, str):
                err_msg = err
        raise RuntimeError(
            f"{candidate.provider}/{candidate.model}: HTTP {status}"
            + (f" {err_msg}" if err_msg else "")
        )

    if not isinstance(payload, dict):
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: non-JSON response")
    if payload.get("error"):
        raise RuntimeError(
            f"{candidate.provider}/{candidate.model}: {payload.get('error')!r}"
        )

    finish_reason = _choice_finish_reason(payload)
    if finish_reason == "length":
        raise RuntimeError(
            f"{candidate.provider}/{candidate.model}: truncated (finish_reason=length)"
        )

    reply = _to_ai_reply(payload, candidate=candidate, latency_ms=latency_ms)
    content = (reply.content or "").strip()
    reasoning = (reply.reasoning_content or "").strip()
    if not content:
        if reasoning:
            raise RuntimeError(
                f"{candidate.provider}/{candidate.model}: reasoning-only / empty content"
            )
        raise RuntimeError(f"{candidate.provider}/{candidate.model}: empty response")
    return reply


def _default_http_post(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[int, Any]:
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        resp = client.post(url, headers=headers, json=body)
        try:
            payload: Any = resp.json()
        except Exception:  # noqa: BLE001
            payload = {"error": {"message": (resp.text or "")[:300]}}
        return resp.status_code, payload


def _short_error(exc: BaseException, limit: int = 120) -> str:
    """One-line, truncated error text suitable for a log line."""
    text = " ".join(str(exc or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run_free_stage_fallback(
    *,
    messages: list[dict[str, Any]],
    validate: ValidateFn,
    primary_api_key: str | None,
    cancel_token: Any | None = None,
    on_reasoning_token: Callable[[str], None] | None = None,
    on_content_token: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    candidates: Iterable[FreeCandidate] | None = None,
    http_get: Callable[..., Any] | None = None,
    http_post: Callable[..., Any] | None = None,
    max_candidates: int = _MAX_CANDIDATES,
) -> FreeStageSuccess | FreeStageFailure:
    """Try free candidates until one call + validation both succeed.

    ``on_progress`` receives short human-readable lines so the UI can show that
    the analysis is still alive during a multi-minute fallback rotation. It is
    best-effort: a failing callback never breaks the analysis.
    """
    from pa_agent.ai.json_validator import Ok, ValidationError

    def _progress(text: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(text)
        except Exception:  # noqa: BLE001
            logger.debug("free fallback progress callback failed", exc_info=True)

    if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
        from pa_agent.ai.deepseek_client import CancelledError

        raise CancelledError("Request cancelled before free candidate loop")

    if candidates is not None:
        cand_list = [c for c in candidates if not _in_cooldown(c)][:max_candidates]
    else:
        cand_list = build_candidates(
            http_get=http_get,
            max_candidates=max_candidates,
            cancel_token=cancel_token,
        )
    attempts: list[FreeAttemptRecord] = []
    if not cand_list:
        _progress("[提示] 备用通道当前不可用，本次分析将按原错误结束。\n")
        return FreeStageFailure(attempts=attempts, message="no free candidates available")

    total = len(cand_list)
    for index, candidate in enumerate(cand_list, start=1):
        if cancel_token is not None and getattr(cancel_token, "is_set", lambda: False)():
            from pa_agent.ai.deepseek_client import CancelledError

            raise CancelledError("Request cancelled before free candidate loop")

        _progress(
            f"[提示] 正在等待备用通道返回（第 {index}/{total} 次尝试，"
            f"单次最长约 {int(_DEFAULT_TIMEOUT_S)} 秒，请勿关闭窗口）…\n"
        )
        t0 = time.monotonic()
        reply: Any = None
        try:
            reply = call_free_candidate(
                candidate,
                messages,
                primary_api_key=primary_api_key,
                cancel_token=cancel_token,
                http_post=http_post,
            )
            _progress("[提示] 已收到返回，正在校验结果…\n")
            result = validate(reply)
        except Exception as exc:
            if is_cancel_or_interrupt(exc):
                raise
            latency_ms = (time.monotonic() - t0) * 1000
            attempts.append(
                FreeAttemptRecord(
                    provider=candidate.provider,
                    model=candidate.model,
                    ok=False,
                    error=str(exc),
                    latency_ms=latency_ms,
                    traceback=traceback.format_exc(),
                )
            )
            logger.info(
                "SenseNova free fallback miss: %s/%s (%s)",
                candidate.provider,
                candidate.model,
                exc,
            )
            continue

        latency_ms = (time.monotonic() - t0) * 1000
        if isinstance(result, Ok):
            # Legal wait/reject Ok results are success — never treat as errors.
            attempts.append(
                FreeAttemptRecord(
                    provider=candidate.provider,
                    model=candidate.model,
                    ok=True,
                    latency_ms=latency_ms,
                )
            )
            logger.warning(
                "SenseNova 主接口失败，已由免费模型接管: provider=%s model=%s (%.0f ms)",
                candidate.provider,
                candidate.model,
                latency_ms,
            )
            # Announce success *before* streaming the answer so the UI reads in order.
            _progress(
                f"[提示] 校验通过（用时 {latency_ms / 1000:.1f} 秒），"
                "以下是本次分析结果：\n"
            )
            if on_reasoning_token and getattr(reply, "reasoning_content", None):
                on_reasoning_token(reply.reasoning_content)
            if on_content_token and getattr(reply, "content", None):
                on_content_token(reply.content)
            return FreeStageSuccess(
                reply=reply,
                candidate=candidate,
                result=result,
                attempts=attempts,
            )

        if not isinstance(result, ValidationError):
            err_msg = f"unexpected validate result: {type(result).__name__}"
        else:
            err_msg = result.message or "validation failed"
        attempts.append(
            FreeAttemptRecord(
                provider=candidate.provider,
                model=candidate.model,
                ok=False,
                error=f"validation: {err_msg}",
                latency_ms=latency_ms,
            )
        )
        logger.info(
            "SenseNova free fallback validation failed: %s/%s (%s)",
            candidate.provider,
            candidate.model,
            err_msg,
        )

    _progress(
        f"[提示] 备用通道 {total} 次尝试均未成功，本次分析将按原错误结束。\n"
    )
    return FreeStageFailure(
        attempts=attempts,
        message="all free candidates failed call or validation",
    )


def format_fallback_exception_meta(
    *,
    primary_message: str,
    primary_type: str,
    failure: FreeStageFailure | None,
) -> dict[str, Any]:
    """Structured exception metadata including full free-attempt trace."""
    attempts = []
    if failure is not None:
        for a in failure.attempts:
            attempts.append(
                {
                    "provider": a.provider,
                    "model": a.model,
                    "ok": a.ok,
                    "error": a.error,
                    "latency_ms": round(a.latency_ms, 1),
                    "http_status": a.http_status,
                    "traceback": a.traceback,
                }
            )
    return {
        "type": primary_type,
        "message": primary_message,
        "sensenova_free_fallback": {
            "triggered": True,
            "success": False,
            "attempts": attempts,
            "summary": failure.message if failure else "fallback not attempted",
        },
    }
