"""Event bus for inter-component communication via Qt signals.

In headless/container environments (no PyQt6), a stub fallback is used so the
core modules can import without a display server.  The stub supports .emit() and
.connect() APIs matching Qt signals closely enough for internal consumers.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

try:
    from PyQt6.QtCore import QObject, pyqtSignal

    _QT_AVAILABLE = True
except ImportError:
    _QT_AVAILABLE = False

from pa_agent.data.base import KlineFrame
from pa_agent.records.schema import AlarmPayload

if _QT_AVAILABLE:

    class EventBus(QObject):  # type: ignore[no-redef]
        """Central signal hub shared across GUI components and orchestrators."""

        data_frame = pyqtSignal(object)
        status = pyqtSignal(str)
        exception = pyqtSignal(object)
        token_update = pyqtSignal(dict)

        def emit_status(self, text: str) -> None:
            self.status.emit(text)

        def emit_exception(self, payload: AlarmPayload) -> None:
            self.exception.emit(payload)

        def emit_data_frame(self, frame: KlineFrame) -> None:
            self.data_frame.emit(frame)

        def emit_token_update(self, data: dict) -> None:
            self.token_update.emit(data)

else:
    # ── No-Qt stub ──────────────────────────────────────────────────────────────
    class _StubSignal:
        """Minimal Qt signal emulation for headless environments."""

        def __init__(self) -> None:
            self._slots: list[Callable[..., Any]] = []

        def connect(self, slot: Callable[..., Any]) -> None:
            self._slots.append(slot)

        def emit(self, *args: Any) -> None:
            for slot in self._slots:
                try:
                    slot(*args)
                except Exception:
                    logger.debug("EventBus stub slot raised", exc_info=True)

    class EventBus:  # type: ignore[no-redef]
        """Central signal hub — headless stub (no PyQt6 available)."""

        _SIGNAL_MAP: dict[str, str] = {
            "disk_error": "exception",
        }

        def __init__(self) -> None:
            self.data_frame = _StubSignal()
            self.status = _StubSignal()
            self.exception = _StubSignal()
            self.token_update = _StubSignal()

        def emit(self, signal_name: str, *args: Any) -> None:
            """Route a generic emit call to the matching signal."""
            attr = self._SIGNAL_MAP.get(signal_name, signal_name)
            signal = getattr(self, attr, None)
            if signal is not None and hasattr(signal, "emit"):
                signal.emit(*args)
            else:
                logger.debug("EventBus stub: unknown signal %r", signal_name)

        def emit_status(self, text: str) -> None:
            self.status.emit(text)

        def emit_exception(self, payload: AlarmPayload) -> None:
            self.exception.emit(payload)

        def emit_data_frame(self, frame: KlineFrame) -> None:
            self.data_frame.emit(frame)

        def emit_token_update(self, data: dict) -> None:
            self.token_update.emit(data)
