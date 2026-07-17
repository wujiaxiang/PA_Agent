"""Centralised logging configuration for PA Agent.

Public API
----------
configure_logging(api_key: str = "") -> None
update_api_key(new_key: str) -> None
verify_logging_handlers() -> bool
set_trace_id(trace_id: str | None = None) -> str
get_trace_id() -> str
"""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import time
import uuid
from pathlib import Path
from typing import Any, List

from pa_agent.config.paths import LOG_FILE_PATH
from pa_agent.util.mask_secret import mask_secret

# ── Module-level state ────────────────────────────────────────────────────────

_active_formatters: List["MaskingFormatter"] = []
_configured: bool = False

# trace_id contextvar — per-request correlation id (TODO P2.4)
# Set by FastAPI middleware / orchestrator entry; emitted in JSON logs.
_trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "pa_agent_trace_id", default=""
)


def set_trace_id(trace_id: str | None = None) -> str:
    """Set the current request's trace_id; auto-generates if None.

    Returns the trace_id that was set (useful for callers that need to log it).
    """
    tid = trace_id or uuid.uuid4().hex[:12]
    _trace_id_ctx.set(tid)
    return tid


def get_trace_id() -> str:
    """Return the current trace_id (empty string if not in a request scope)."""
    return _trace_id_ctx.get()


# ── MaskingFormatter ──────────────────────────────────────────────────────────


class MaskingFormatter(logging.Formatter):
    """Logging formatter that replaces the plaintext API key with its masked form."""

    def __init__(self, fmt: str, api_key: str = "") -> None:
        super().__init__(fmt)
        self._api_key = api_key

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        message = super().format(record)
        if self._api_key:
            message = message.replace(self._api_key, mask_secret(self._api_key))
        return message

    def set_api_key(self, new_key: str) -> None:
        self._api_key = new_key


class JsonlFormatter(logging.Formatter):
    """Structured JSON-lines formatter (TODO P2.4).

    Emits one JSON object per log record with: ts, level, logger, msg,
    trace_id, and any extra fields attached via ``logger.info(..., extra=...)``.
    API key masking is applied to the message string.
    """

    def __init__(self, api_key: str = "") -> None:
        super().__init__()
        self._api_key = api_key

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        # Build base payload
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": get_trace_id(),
        }
        # Attach any caller-supplied extras (skip private LogRecord attrs)
        std_attrs = set(vars(record).keys()) | {
            "message", "asctime", "name", "msg", "args", "levelname", "levelno",
            "pathname", "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated", "thread",
            "threadName", "processName", "process", "taskName",
        }
        for k, v in vars(record).items():
            if k not in std_attrs and not k.startswith("_"):
                try:
                    json.dumps(v)  # ensure serializable
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = repr(v)
        # Exception info
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Mask api_key in message
        if self._api_key and self._api_key in payload["msg"]:
            payload["msg"] = payload["msg"].replace(
                self._api_key, mask_secret(self._api_key)
            )
        return json.dumps(payload, ensure_ascii=False)

    def set_api_key(self, new_key: str) -> None:
        self._api_key = new_key


# ── Public functions ──────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_THIRD_PARTY_LOGGERS = ("urllib3", "openai", "httpx")

# tvdatafeed opens a websocket every refresh tick and logs at DEBUG — keep quiet
_QUIET_LOGGER_NAMES = (
    "urllib3",
    "openai",
    "httpx",
    "tvDatafeed",
    "tvDatafeed.main",
    "root",  # tvdatafeed uses logging.getLogger("root") for websocket
    "websocket",
)


def _json_logging_enabled() -> bool:
    """True if PA_AGENT_LOG_JSON env var is set to a truthy value (TODO P2.4)."""
    return os.environ.get("PA_AGENT_LOG_JSON", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def verify_logging_handlers() -> bool:
    """Return True when the expected rotating file handler is attached to root."""
    expected = str(LOG_FILE_PATH.resolve())
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            base = getattr(handler, "baseFilename", "")
            if str(Path(base).resolve()) == expected:
                return True
    return False


def configure_logging(api_key: str = "") -> None:
    """Configure the root logger with rotating file handler and console handler.

    Both handlers use MaskingFormatter that replaces api_key with mask_secret(api_key).
    Third-party loggers (urllib3, openai, httpx) are also attached to the same handlers.

    If handlers were removed after a prior configure_logging call, re-attaches them.

    If env var ``PA_AGENT_LOG_JSON=true`` is set, an additional JSON-lines
    handler is attached to write structured logs to ``logs/pa_agent.jsonl``
    (TODO P2.4).  The text log is kept for backward compatibility.
    """
    global _configured  # noqa: PLW0603

    if _configured:
        if api_key:
            update_api_key(api_key)
        if verify_logging_handlers():
            return
        # Handlers missing (e.g. external code cleared root.handlers) — re-install.
        _configured = False

    # Build formatters
    file_formatter = MaskingFormatter(_LOG_FORMAT, api_key=api_key)
    console_formatter = MaskingFormatter(_LOG_FORMAT, api_key=api_key)

    # Track all active formatters so update_api_key can reach them
    _active_formatters.clear()
    _active_formatters.append(file_formatter)
    _active_formatters.append(console_formatter)

    # Rotating file handler
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    # Console (stream) handler — INFO+ only; file keeps DEBUG for troubleshooting
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)

    handlers: list[logging.Handler] = [file_handler, console_handler]

    # Optional JSON-lines handler (TODO P2.4)
    if _json_logging_enabled():
        jsonl_path = LOG_FILE_PATH.parent / "pa_agent.jsonl"
        jsonl_formatter = JsonlFormatter(api_key=api_key)
        _active_formatters.append(jsonl_formatter)
        jsonl_handler = logging.handlers.RotatingFileHandler(
            jsonl_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        jsonl_handler.setFormatter(jsonl_formatter)
        handlers.append(jsonl_handler)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Remove any previously installed handlers to avoid duplicates on re-call
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    for h in handlers:
        root_logger.addHandler(h)

    # Attach the same handlers to third-party loggers
    for name in _THIRD_PARTY_LOGGERS:
        tp_logger = logging.getLogger(name)
        for h in list(tp_logger.handlers):
            tp_logger.removeHandler(h)
        for h in handlers:
            tp_logger.addHandler(h)
        # Prevent double-logging via root propagation
        tp_logger.propagate = False

    _silence_noisy_libraries()

    _configured = True
    logging.getLogger("pa_agent.diagnostics").info(
        "configure_logging: handlers attached (log_file=%s, json=%s)",
        LOG_FILE_PATH,
        _json_logging_enabled(),
    )


def _silence_noisy_libraries() -> None:
    """Turn down chatty third-party DEBUG loggers (tvdatafeed websocket spam)."""
    for name in _QUIET_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.WARNING)


def update_api_key(new_key: str) -> None:
    """Update the masking key in all active MaskingFormatter instances."""
    for formatter in _active_formatters:
        formatter.set_api_key(new_key)
