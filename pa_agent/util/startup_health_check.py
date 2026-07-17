"""Runtime health checks for model API and data source (TODO P1.3).

Used by ``/api/health/check`` endpoint and the background heartbeat task in
``web/server.py`` to surface degraded state (e.g. model API 401, TV connect
failure) instead of silently failing on the next analysis request.

Design:
- ``check_model_api(ctx)`` — sends a minimal chat completion (1 token) to
  verify the provider accepts the configured key/base_url/model.  Avoids
  burning tokens by requesting max_tokens=1.
- ``check_data_source(ctx)`` — calls ``latest_snapshot(2)`` to verify the
  data source is connected and returning bars.  Uses n=2 (forming + 1
  closed) to keep the request cheap.
- ``run_full_check(ctx)`` — runs both, returns a ``HealthReport`` with
  overall status (``ok`` / ``degraded`` / ``error``) and per-component
  details.

The report is intentionally plain dict-friendly so it can be JSON-serialised
directly by FastAPI.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ComponentHealth:
    """Per-component health result."""

    status: str  # "ok" | "warning" | "error"
    detail: str = ""
    latency_ms: float = 0.0


@dataclass
class HealthReport:
    """Aggregate health report for the whole system."""

    status: str  # "ok" | "degraded" | "error"
    components: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


def _classify(*components: ComponentHealth) -> str:
    """Aggregate component statuses into overall status."""
    if any(c.status == "error" for c in components):
        return "degraded" if any(c.status == "ok" for c in components) else "error"
    if any(c.status == "warning" for c in components):
        return "degraded"
    return "ok"


def check_model_api(ctx: Any) -> ComponentHealth:
    """Ping the AI provider with a minimal chat completion.

    Returns ``ComponentHealth`` with status ``ok`` / ``error`` and latency.
    Does NOT raise — callers receive the result and decide how to react.

    Strategy: call the client's ``chat`` method (DeepSeekClient.chat returns
    an AIReply synchronously).  We don't pass max_tokens — the goal is just
    to verify connectivity/auth, not limit tokens.
    """
    start = time.perf_counter()
    try:
        client = getattr(ctx, "client", None)
        if client is None:
            return ComponentHealth("error", "AI client not initialized", 0.0)
        messages = [{"role": "user", "content": "ping"}]
        if hasattr(client, "chat"):
            client.chat(messages)
        else:
            return ComponentHealth(
                "error", "AI client has no chat method", 0.0
            )
        latency = (time.perf_counter() - start) * 1000.0
        return ComponentHealth("ok", "model API reachable", latency)
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000.0
        msg = f"{type(exc).__name__}: {exc}"
        # Truncate long error messages
        if len(msg) > 300:
            msg = msg[:300] + "..."
        return ComponentHealth("error", msg, latency)


def check_data_source(ctx: Any) -> ComponentHealth:
    """Verify the data source returns at least one bar.

    Uses ``latest_snapshot(2)`` (forming + 1 closed) to keep it cheap.
    """
    start = time.perf_counter()
    try:
        ds = getattr(ctx, "data_source", None)
        if ds is None:
            return ComponentHealth("error", "data_source not initialized", 0.0)
        # Ensure connected
        if hasattr(ds, "_tv") and getattr(ds, "_tv", None) is None:
            ds.connect()
        bars = ds.latest_snapshot(2)
        if not bars:
            return ComponentHealth("error", "latest_snapshot returned 0 bars", 0.0)
        latency = (time.perf_counter() - start) * 1000.0
        return ComponentHealth(
            "ok",
            f"data_source ok ({len(bars)} bars, type={type(ds).__name__})",
            latency,
        )
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000.0
        msg = f"{type(exc).__name__}: {exc}"
        if len(msg) > 300:
            msg = msg[:300] + "..."
        return ComponentHealth("error", msg, latency)


def run_full_check(ctx: Any) -> HealthReport:
    """Run all component checks and return an aggregate report."""
    model = check_model_api(ctx)
    data = check_data_source(ctx)
    return HealthReport(
        status=_classify(model, data),
        components={"model_api": asdict(model), "data_source": asdict(data)},
    )
