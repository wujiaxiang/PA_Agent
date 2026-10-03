"""GUI presentation for stage-2 order opportunities.

The detection gate and alert text now live in :mod:`pa_agent.ai.order_opportunity`
(Qt-free) so the Web backend can share them; this module keeps only the
desktop-specific popup and alert-sound helpers and re-exports the shared
symbols for backward compatibility with existing GUI imports.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

from pa_agent.ai.order_opportunity import (
    ORDER_OPPORTUNITY_TYPES,
    _fmt_price,
    _parse_trade_confidence,
    _windows_alert_wav_paths,
    format_order_alert_message,
    has_order_opportunity,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ORDER_OPPORTUNITY_TYPES",
    "ORDER_ALERT_AUTO_CLOSE_MS",
    "format_order_alert_message",
    "has_order_opportunity",
    "play_order_alert_sound",
    "show_order_opportunity_alert",
]

ORDER_ALERT_AUTO_CLOSE_MS = 120_000


def show_order_opportunity_alert(parent: Any, decision: dict[str, Any]) -> None:
    """Non-modal alert that auto-closes after :data:`ORDER_ALERT_AUTO_CLOSE_MS`."""
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("下单机会")
    box.setText(format_order_alert_message(decision))
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    timer = QTimer(box)
    timer.setSingleShot(True)
    timer.timeout.connect(box.accept)
    timer.start(ORDER_ALERT_AUTO_CLOSE_MS)
    # Non-modal: avoid blocking the main event loop after analysis completes.
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    box.show()


def play_order_alert_sound() -> bool:
    """Play a short alert sound (best-effort). Returns True if playback was attempted."""
    if sys.platform == "win32":
        import winsound

        for path in _windows_alert_wav_paths():
            if not os.path.isfile(path):
                continue
            try:
                winsound.PlaySound(
                    path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
                return True
            except Exception as exc:
                logger.debug("order alert PlaySound file %s failed: %s", path, exc)

        for alias in ("SystemExclamation", "SystemHand", "SystemAsterisk"):
            try:
                winsound.PlaySound(
                    alias,
                    winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
                return True
            except Exception as exc:
                logger.debug("order alert PlaySound alias %s failed: %s", alias, exc)

        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            return True
        except Exception as exc:
            logger.debug("order alert MessageBeep failed: %s", exc)

    try:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.beep()
            return True
    except Exception as exc:
        logger.debug("order alert QApplication.beep failed: %s", exc)

    return False