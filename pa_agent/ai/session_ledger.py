"""Session-level token usage ledger (no pricing)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pa_agent.ai.deepseek_client import AIUsage

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _QT_AVAILABLE = True
except ImportError:
    _QT_AVAILABLE = False


class _SessionLedgerMixin:
    """Shared logic for both Qt and stub implementations."""

    def __init__(
        self,
        context_window: int = 1_000_000,
        warn_pct: float = 80.0,
        **__kwargs,  # accept 'parent' kwarg silently in headless mode
    ) -> None:
        self._context_window = context_window
        self._warn_pct = warn_pct
        self._yellow_fired = False
        self._red_fired = False
        self.total_input: int = 0
        self.total_cached_input: int = 0
        self.total_output: int = 0

    @property
    def context_used(self) -> int:
        return self.total_input + self.total_output

    def add(self, usage: AIUsage) -> None:
        """Accumulate usage from one API call and emit signals (if Qt available)."""
        self.total_input += usage.prompt_tokens
        self.total_cached_input += usage.cached_prompt_tokens
        self.total_output += usage.completion_tokens

        pct = self.context_used / self._context_window * 100.0

        totals = self.breakdown()
        self._emit_updated(totals)

        if pct >= 95.0 and not self._red_fired:
            self._red_fired = True
            logger.warning("Context usage >= 95%% (%.1f%%)", pct)
            self._emit_threshold("red", totals)
        elif pct >= self._warn_pct and not self._yellow_fired:
            self._yellow_fired = True
            logger.warning("Context usage >= %.0f%% (%.1f%%)", self._warn_pct, pct)
            self._emit_threshold("yellow", totals)

    def reset(self) -> None:
        """Reset all counters (e.g. on symbol/timeframe switch)."""
        self.total_input = 0
        self.total_cached_input = 0
        self.total_output = 0
        self._yellow_fired = False
        self._red_fired = False

    def breakdown(self) -> dict:
        """Return current totals as a dict for UI display."""
        pct = self.context_used / self._context_window * 100.0
        return {
            "total_input": self.total_input,
            "total_cached_input": self.total_cached_input,
            "total_output": self.total_output,
            "context_used": self.context_used,
            "context_window": self._context_window,
            "context_pct": round(pct, 2),
        }

    def _emit_updated(self, totals: dict) -> None:
        """Override in Qt subclass."""
        pass

    def _emit_threshold(self, level: str, totals: dict) -> None:
        """Override in Qt subclass."""
        pass


if _QT_AVAILABLE:

    class SessionTokenLedger(_SessionLedgerMixin, QObject):
        """Accumulates token usage across API calls in a session.

        Signals
        -------
        threshold_crossed(str, dict)
            Emitted when context usage crosses warn_pct or 95%.
        updated(dict)
            Emitted after every add() with the current totals dict.
        """

        threshold_crossed = pyqtSignal(str, dict)
        updated = pyqtSignal(dict)

        def __init__(self, context_window=1_000_000, warn_pct=80.0, parent=None):
            QObject.__init__(self, parent)
            _SessionLedgerMixin.__init__(self, context_window=context_window, warn_pct=warn_pct)

        def _emit_updated(self, totals):
            self.updated.emit(totals)

        def _emit_threshold(self, level, totals):
            self.threshold_crossed.emit(level, totals)

else:

    class SessionTokenLedger(_SessionLedgerMixin):
        """Headless stub — no Qt signals, just accumulates counters."""
        pass
