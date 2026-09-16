"""Thin service wrapping the overlay window lifecycle.

All calls must be made on the GTK main thread (or via GLib.idle_add).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class OverlayService:
    def __init__(self) -> None:
        self._window = None

    def _ensure(self):
        if self._window is None:
            from ..ui.overlay_window import OverlayWindow
            self._window = OverlayWindow()
        return self._window

    def show_listening(self) -> None:
        self._ensure().show_listening()

    def show_processing(self) -> None:
        self._ensure().show_processing()

    def hide(self) -> None:
        if self._window is not None:
            self._window.hide_overlay()

    def set_level(self, rms: float) -> None:
        if self._window is not None:
            self._window.set_level(rms)

    def set_preview(self, text: str) -> None:
        if self._window is not None:
            self._window.set_preview(text)

    def destroy(self) -> None:
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None
