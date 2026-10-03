"""Secret masking utility (standalone, no dependencies).

Besides :func:`mask_secret` this module owns a process-wide registry of secrets
that must never reach a log file or a records JSON.  Registering them here lets
the logging formatters scrub every sink with one call instead of each call site
remembering to mask its own credential.

Background: ``requests`` embeds the full URL in ``ConnectionError`` /
``TooManyRedirects`` strings, so a failed Feishu webhook POST writes
``.../bot/v2/hook/<TOKEN>`` straight into ``logs/pa_agent.log`` unless the token
is registered here.
"""
from __future__ import annotations

import threading

#: Minimum length for a value to be treated as a secret. Short/empty values are
#: ignored so placeholders (e.g. the literal "****") never blank out the log.
_MIN_SECRET_LEN = 6

_lock = threading.RLock()
_secrets: set[str] = set()


def mask_secret(s: str) -> str:
    """Return s with all but the last 4 characters replaced by '*'.

    If len(s) < 4, return s unchanged (including empty string).
    """
    if len(s) < 4:
        return s
    return "*" * (len(s) - 4) + s[-4:]


def register_secret(value: object) -> None:
    """Register *value* so log/record scrubbing will redact it.

    Values shorter than ``_MIN_SECRET_LEN`` are ignored: they are almost always
    placeholders, and masking a 4-char string would mangle unrelated log text.
    """
    if not isinstance(value, str):
        return
    v = value.strip()
    if len(v) < _MIN_SECRET_LEN:
        return
    with _lock:
        _secrets.add(v)


def register_secrets(*values: object) -> None:
    """Register several secrets at once."""
    for value in values:
        register_secret(value)


def unregister_secret(value: object) -> None:
    """Drop a previously registered secret (used when settings rotate a key)."""
    if not isinstance(value, str):
        return
    with _lock:
        _secrets.discard(value.strip())


def registered_secrets() -> frozenset[str]:
    """Snapshot of the registered secrets (for tests/diagnostics)."""
    with _lock:
        return frozenset(_secrets)


def scrub(text: object) -> str:
    """Replace every registered secret in *text* with its masked form.

    Longest-first so overlapping secrets are redacted deterministically.
    Non-string input is returned unchanged.
    """
    if not isinstance(text, str) or not text:
        return text  # type: ignore[return-value]
    secrets = registered_secrets()
    if not secrets:
        return text
    for secret in sorted(secrets, key=len, reverse=True):
        if secret and secret in text:
            text = text.replace(secret, mask_secret(secret))
    return text