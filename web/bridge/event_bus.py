"""Asyncio pub/sub event bus — Web replacement for pa_agent.util.event_bus.

Used by FastAPI routes to bridge between the core orchestrator (which fires
events on the ``event_bus`` attribute of AppContext) and WebSocket / SSE
streams.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

Subscriber = Callable[..., Coroutine[Any, Any, None]]


class AsyncEventBus:
    """In-process pub/sub for async subscribers.

    Replaces PyQt6 ``EventBus`` (``pa_agent/util/event_bus.py``) in the web
    backend.  Consumed by ``web/server.py`` during startup: the server patches
    ``ctx.event_bus = AsyncEventBus()`` after ``AppContext.bootstrap()`` so
    the core modules see the same API surface.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}

    # -- Qt-signal-compatible API used by core modules ---------------------------

    def connect(self, signal_name: str, handler: Subscriber) -> None:
        """Register an async *handler* for *signal_name*."""
        self._subscribers.setdefault(signal_name, []).append(handler)

    async def emit(self, signal_name: str, *args: Any) -> None:
        """Fire all subscribers for *signal_name* with *args*."""
        for handler in self._subscribers.get(signal_name, ()):
            try:
                await handler(*args)
            except Exception:
                logger.debug(
                    "AsyncEventBus handler for %r raised", signal_name, exc_info=True
                )

    # -- Convenience wrappers that core also calls -------------------------------

    async def emit_status(self, text: str) -> None:
        await self.emit("status", text)

    async def emit_exception(self, payload: Any) -> None:
        await self.emit("exception", payload)

    async def emit_data_frame(self, frame: Any) -> None:
        await self.emit("data_frame", frame)

    async def emit_token_update(self, data: dict) -> None:
        await self.emit("token_update", data)
