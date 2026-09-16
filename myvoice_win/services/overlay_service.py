"""Thin service wrapping the Windows overlay window lifecycle (PySide6/Qt).

Matches ``myvoice/services/overlay_service.py``'s ``OverlayService`` public
surface (``show_listening``/``show_processing``/``set_level``/
``set_preview``/``hide``/``destroy``), delegating to
``myvoice_win/ui/overlay_window.py``'s ``OverlayWindow``, lazily
constructing the window on first use.

All calls must be made on the Qt GUI thread (or marshalled onto it, e.g.
via a Qt signal/slot connection), matching the Linux version's "GTK main
thread only" contract.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


class OverlayService:
    def __init__(self) -> None:
        self._window: Optional[object] = None

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
            self._window.hide()

    def set_level(self, rms: float) -> None:
        if self._window is not None:
            self._window.set_level(rms)

    def set_preview(self, text: str) -> None:
        if self._window is not None:
            self._window.set_preview(text)

    def destroy(self) -> None:
        if self._window is not None:
            try:
                self._window.close()
            except Exception:
                log.exception("Failed to close overlay window during destroy")
            self._window = None
