"""Environment variable loader for PA Agent (TODO P1.2).

Provides a thin layer that reads environment variables (optionally from a
``.env`` file) and applies them as overrides on top of a ``Settings``
object loaded from ``config/settings.json``.

Priority (highest → lowest):
1. Process environment variables (``os.environ``)
2. ``.env`` file at project root (loaded by python-dotenv if available)
3. ``config/settings.json``

Supported env vars (all prefixed ``PA_AGENT_``):
- ``PA_AGENT_PROVIDER_MODEL``             → provider.model
- ``PA_AGENT_PROVIDER_BASE_URL``          → provider.base_url
- ``PA_AGENT_PROVIDER_API_KEY``           → provider.api_key
- ``PA_AGENT_PROVIDER_THINKING``          → provider.thinking (true/false)
- ``PA_AGENT_PROVIDER_REASONING_EFFORT``  → provider.reasoning_effort
- ``PA_AGENT_PROVIDER_CONTEXT_WINDOW``    → provider.context_window (int)
- ``PA_AGENT_PROVIDER_MAX_OUTPUT_TOKENS`` → provider.max_output_tokens (int)
- ``PA_AGENT_PROVIDER_SEED``              → provider.seed (int, 随机性控制)
- ``PA_AGENT_PROVIDER_TOP_P``             → provider.top_p (float, 核采样阈值 0~1)
- ``PA_AGENT_GENERAL_LAST_DATA_SOURCE``          → general.last_data_source
- ``PA_AGENT_GENERAL_LAST_TRADINGVIEW_EXCHANGE`` → general.last_tradingview_exchange
- ``PA_AGENT_GENERAL_LAST_SYMBOL``               → general.last_symbol
- ``PA_AGENT_GENERAL_LAST_TIMEFRAME``            → general.last_timeframe
- ``PA_AGENT_GENERAL_ANALYSIS_BAR_COUNT``        → general.analysis_bar_count (int)
- ``PA_AGENT_TRADINGVIEW_USERNAME``       → (read by TradingViewSource factory)
- ``PA_AGENT_TRADINGVIEW_PASSWORD``       → (read by TradingViewSource factory)
- ``PA_AGENT_PUSHPLUS_TOKEN``             → pushplus.token

Usage in Web mode (``pa_agent/app_context.py`` ``bootstrap()``):
    settings = load_settings(SETTINGS_JSON_PATH)
    apply_env_overrides(settings)  # ← add this line

If no ``.env`` file exists and no env vars are set, this module is a no-op.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from pathlib import Path

logger = logging.getLogger(__name__)

# Project root (parent of pa_agent/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"

# Mapping: env var name → (settings_section_attr, field_name, cast_fn)
# cast_fn: None = str, "int" = int, "bool" = bool_from_str
_ENV_MAPPING: dict[str, tuple[str, str, str | None]] = {
    "PA_AGENT_PROVIDER_MODEL": ("provider", "model", None),
    "PA_AGENT_PROVIDER_BASE_URL": ("provider", "base_url", None),
    "PA_AGENT_PROVIDER_API_KEY": ("provider", "api_key", None),
    "PA_AGENT_PROVIDER_THINKING": ("provider", "thinking", "bool"),
    "PA_AGENT_PROVIDER_REASONING_EFFORT": ("provider", "reasoning_effort", None),
    "PA_AGENT_PROVIDER_CONTEXT_WINDOW": ("provider", "context_window", "int"),
    "PA_AGENT_PROVIDER_MAX_OUTPUT_TOKENS": ("provider", "max_output_tokens", "int"),
    "PA_AGENT_PROVIDER_SEED": ("provider", "seed", "int"),
    "PA_AGENT_PROVIDER_TOP_P": ("provider", "top_p", "float"),
    "PA_AGENT_GENERAL_LAST_DATA_SOURCE": ("general", "last_data_source", None),
    "PA_AGENT_GENERAL_LAST_TRADINGVIEW_EXCHANGE": ("general", "last_tradingview_exchange", None),
    "PA_AGENT_GENERAL_LAST_SYMBOL": ("general", "last_symbol", None),
    "PA_AGENT_GENERAL_LAST_TIMEFRAME": ("general", "last_timeframe", None),
    "PA_AGENT_GENERAL_ANALYSIS_BAR_COUNT": ("general", "analysis_bar_count", "int"),
    "PA_AGENT_PUSHPLUS_TOKEN": ("pushplus", "token", None),
}

# TradingView credentials are read directly by the factory, not stored in Settings.
# Listed here only for .env.example documentation completeness.
_TV_ENV_VARS = (
    "PA_AGENT_TRADINGVIEW_USERNAME",
    "PA_AGENT_TRADINGVIEW_PASSWORD",
    "PA_AGENT_TRADINGVIEW_SESSION_ID",
)


def _bool_from_str(s: str) -> bool:
    """Parse 'true'/'false'/'1'/'0' case-insensitively."""
    return s.strip().lower() in ("true", "1", "yes", "on")


def _load_dotenv() -> None:
    """Load .env file into os.environ if python-dotenv is available.

    No-op if dotenv isn't installed or .env doesn't exist.
    """
    if not _ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import]

        load_dotenv(dotenv_path=str(_ENV_FILE), override=False)
        logger.debug(".env loaded from %s", _ENV_FILE)
    except ImportError:
        # Manual fallback: parse simple KEY=VALUE lines
        _load_dotenv_manual(_ENV_FILE)


def _load_dotenv_manual(path: Path) -> None:
    """Minimal .env parser (fallback when python-dotenv is not installed).

    Supports only ``KEY=VALUE`` lines; ignores comments (#) and blank lines.
    Does NOT support quoting/escaping — keep .env simple.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip inline comments
                if " #" in value:
                    value = value.split(" #")[0].strip()
                # Don't override existing env vars
                if key not in os.environ:
                    os.environ[key] = value
        logger.debug(".env loaded (manual parser) from %s", path)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to parse .env manually", exc_info=True)


def get_env_str(key: str, default: str = "") -> str:
    """Get a string env var, loading .env first if not already loaded."""
    if not os.environ.get("_PA_ENV_LOADED"):
        _load_dotenv()
        os.environ["_PA_ENV_LOADED"] = "1"
    return os.environ.get(key, default)


def get_env_int(key: str, default: int = 0) -> int:
    """Get an int env var with fallback."""
    s = get_env_str(key)
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        logger.warning("env var %s=%r is not a valid int, using default %d", key, s, default)
        return default


def get_env_bool(key: str, default: bool = False) -> bool:
    """Get a bool env var with fallback."""
    s = get_env_str(key)
    if not s:
        return default
    return _bool_from_str(s)


def apply_env_overrides(settings: Any) -> None:
    """Apply env var overrides on top of a Settings object in-place.

    Called by ``AppContext.bootstrap()`` after ``load_settings()``.  Only
    overrides fields whose env var is actually set; leaves the rest alone.

    Emits a debug log per applied override so users can see which env vars
    took effect.
    """
    _load_dotenv()
    applied = 0
    for env_key, (section, field, cast) in _ENV_MAPPING.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            target = getattr(settings, section, None)
            if target is None:
                continue
            if cast == "int":
                value: Any = int(raw)
            elif cast == "bool":
                value = _bool_from_str(raw)
            else:
                value = raw
            setattr(target, field, value)
            applied += 1
            logger.debug("env override: %s → %s.%s = %r", env_key, section, field, value)
        except (AttributeError, ValueError, TypeError) as exc:
            logger.warning("env override failed for %s: %s", env_key, exc)
    if applied:
        logger.info("Applied %d env overrides from .env / process env", applied)


def get_tv_credentials(settings: "object | None" = None) -> tuple[str, str, str]:
    """Return (session_id, username, password) for TradingView.

    Resolution order: ``settings.tradingview`` (UI-managed) → env vars
    ``PA_AGENT_TRADINGVIEW_SESSION_ID`` / ``PA_AGENT_TRADINGVIEW_USERNAME``
    / ``PA_AGENT_TRADINGVIEW_PASSWORD`` (headless server / .env deployment).

    Three authentication modes are checked in order:
    1. session_id: Direct cookie-based auth (most stable, avoids recaptcha)
    2. username + password: Traditional login (may trigger recaptcha)
    3. Anonymous: Empty tuple (rate-limited for US equities)

    Returns ("", "", "") when no credentials are configured.
    """
    s_session_id = s_user = s_pass = ""
    if settings is not None:
        tv = getattr(settings, "tradingview", None)
        if tv is not None:
            s_session_id = (getattr(tv, "session_id", "") or "").strip()
            s_user = (getattr(tv, "username", "") or "").strip()
            s_pass = (getattr(tv, "password", "") or "").strip()
    if s_session_id:
        return (s_session_id, s_user, s_pass)
    if s_user and s_pass:
        return ("", s_user, s_pass)
    env_session_id = get_env_str("PA_AGENT_TRADINGVIEW_SESSION_ID")
    env_user = get_env_str("PA_AGENT_TRADINGVIEW_USERNAME")
    env_pass = get_env_str("PA_AGENT_TRADINGVIEW_PASSWORD")
    if env_session_id:
        return (env_session_id, env_user, env_pass)
    return ("", s_user or env_user, s_pass or env_pass)
