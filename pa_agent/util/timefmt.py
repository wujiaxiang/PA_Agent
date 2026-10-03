"""Time formatting utilities."""
from __future__ import annotations
from datetime import datetime, timezone


def now_local_ms() -> int:
    """Return current local time as milliseconds since epoch."""
    return int(datetime.now(timezone.utc).astimezone().timestamp() * 1000)
