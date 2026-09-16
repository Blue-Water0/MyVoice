"""System tray indicator (PySide6 / Qt).

Wraps ``QtWidgets.QSystemTrayIcon`` with a ``QMenu`` (Show, Start, Stop,
Quit), matching the callback surface of the Linux
``myvoice/ui/tray.py``'s ``TrayIndicator``: ``on_toggle_show``,
``on_start``, ``on_stop``, ``on_quit``, ``set_listening(bool)``,
``destroy()``.

Unlike the Linux version -- which tries three different GTK tray
backends (Ayatana AppIndicator3 / AppIndicator3 / Gtk.StatusIcon) --
Qt has exactly one cross-platform tray API, so there's no backend
fallback ladder here.
"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .. import APP_NAME

log = logging.getLogger(__name__)

TRAY_ID = "myvoice"


class TrayIndicator:
    def __init__(
        self,
        on_toggle_show: Callable[[], None],
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._callbacks = {
            "toggle": on_toggle_show,
            "start": on_start,
            "stop": on_stop,
            "quit": on_quit,
        }
        self._build()

    # ---- construction ---------------------------------------------------

    @staticmethod
    def _make_icon(theme_name: str) -> QtGui.QIcon:
        icon = QtGui.QIcon.fromTheme(theme_name)
        if icon.isNull():
            # No icon theme available (e.g. Windows, or this headless
            # sandbox) -- fall back to a simple solid pixmap so
            # QSystemTrayIcon always has a valid, non-null icon.
            pixmap = QtGui.QPixmap(16, 16)
            pixmap.fill(QtCore.Qt.GlobalColor.darkGray)
            icon = QtGui.QIcon(pixmap)
        return icon

    def _build(self) -> None:
        self._tray_icon = QtWidgets.QSystemTrayIcon()
        self._tray_icon.setIcon(self._make_icon("audio-input-microphone"))
        self._tray_icon.setToolTip(APP_NAME)
        self._tray_icon.activated.connect(self._on_activated)

        menu = QtWidgets.QMenu()

        def add(label: str, key: str) -> QtGui.QAction:
            action = menu.addAction(label)
            action.triggered.connect(lambda *_a, k=key: self._callbacks[k]())
            return action

        self._show_action = add(f"Show {APP_NAME}", "toggle")
        menu.addSeparator()
        self._start_action = add("Start Listening", "start")
        self._stop_action = add("Stop Listening", "stop")
        menu.addSeparator()
        self._quit_action = add(f"Quit {APP_NAME}", "quit")

        self._menu = menu
        self._tray_icon.setContextMenu(menu)

        # Initial enabled state: not listening yet.
        self.set_listening(False)

        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon.show()
        else:
            log.info(
                "System tray not available in this session (offscreen/"
                "headless platform, or no tray on this desktop)."
            )

    def _on_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
            self._callbacks["toggle"]()

    # ---- public API used by controller ----------------------------------

    def set_listening(self, listening: bool) -> None:
        self._start_action.setEnabled(not listening)
        self._stop_action.setEnabled(listening)
        icon_name = "media-record" if listening else "audio-input-microphone"
        try:
            self._tray_icon.setIcon(self._make_icon(icon_name))
        except Exception:
            log.exception("Failed to update tray icon")

    def destroy(self) -> None:
        try:
            self._tray_icon.hide()
        except Exception:
            pass
