"""Listening overlay: frameless, always-on-top waveform + preview pill (PySide6/Qt).

Mirrors the *feature set* of the Linux GTK ``myvoice/ui/overlay_window.py``
-- not its Cairo/GTK drawing code:

- Frameless, always-on-top window (``Qt.FramelessWindowHint |
  Qt.WindowStaysOnTopHint``) positioned near bottom-center of the primary
  screen.
- A waveform painted from RMS updates (``set_level``) via ``QPainter`` in
  ``paintEvent``. Bars are white while listening (mic-reactive) and amber
  while processing (synthetic calm drift, not tied to mic input) -- same
  two-state behaviour as the Linux version, redrawn on a Qt timer instead
  of a GLib one.
- A truncated transcript preview (``set_preview``).
- ``show_listening()`` / ``show_processing()`` / ``hide()``.

Preview-text eliding (RTL-safety)
----------------------------------
The Linux version uses Pango's ``PANGO_ELLIPSIZE_START`` so that as a
transcript grows, the newest words -- the *end* of the logical string --
stay visible, and the oldest words -- the logical *beginning* -- are
replaced by an ellipsis. This is direction-agnostic: it operates on
logical string order, not visual/screen position, so Pango's BiDi engine
renders whatever logical string comes out correctly for Hebrew, Arabic,
and English alike.

``QFontMetrics.elidedText(text, Qt.TextElideMode.ElideLeft, width)`` has
the equivalent semantics in Qt: it drops characters from the logical
*beginning* of the string and keeps the logical *tail*, prefixing an
ellipsis -- verified directly against real Arabic/Hebrew strings (not just
assumed from the name): eliding "...ذي يجب أن...القديمة ثم الكلمات
الجديدة في النهاية" with ``ElideLeft`` keeps "الكلمات الجديدة في النهاية"
("the new words at the end"), i.e. the newest words, exactly like the
Linux behaviour. Qt's own BiDi shaping (used by both ``elidedText`` and
``QPainter.drawText``) then displays that logical string correctly for
RTL scripts -- so ``ElideLeft`` (not ``ElideRight``) is the correct
choice here regardless of the preview text's script.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

OVERLAY_WIDTH = 280
OVERLAY_HEIGHT = 100

WAVEFORM_CY = 40                 # waveform bars vertical center
WAVEFORM_MAX_BAR_H = 50          # bar height at maximum amplitude
WAVEFORM_BAR_COUNT = 18
WAVEFORM_BAR_GAP = 3
WAVEFORM_HORIZONTAL_MARGIN = 40  # each side, mirrors Linux's centered layout

WAVEFORM_LISTENING_COLOR = QtGui.QColor(255, 255, 255, 217)   # white, mic-reactive
WAVEFORM_PROCESSING_COLOR = QtGui.QColor(250, 166, 51, 230)   # amber, synthetic drift

# Processing-state synthetic level: bars sit at a calm mid-low value with a
# slow drift so the overlay does not look frozen but is clearly
# distinguishable from active-mic bars. Mirrors the Linux constants.
PROCESSING_LEVEL_BASE = 0.25
PROCESSING_LEVEL_AMPLITUDE = 0.05
PROCESSING_LEVEL_HZ = 1.5

PREVIEW_MAX_CHARS = 500  # outer cap, mirrors Linux's pathological-length guard
PREVIEW_FONT_PT = 10
PREVIEW_TOP_MARGIN = 6
PREVIEW_SIDE_MARGIN = 12
PREVIEW_BOTTOM_MARGIN = 8

TICK_INTERVAL_MS = 33  # ~30fps, mirrors Linux's GLib.timeout_add(33, ...)


class OverlayWindow(QtWidgets.QWidget):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MyVoice Listening")
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(OVERLAY_WIDTH, OVERLAY_HEIGHT)

        self._state = "idle"  # 'listening' | 'processing' | 'idle'
        self._level = 0.0
        self._preview_text = ""
        self._start_time = time.monotonic()

        # "Segoe UI" (Windows' default UI font since Vista, on every real
        # Windows 10/11 install) is named explicitly rather than leaving
        # the ambient default QFont(), which resolves differently across
        # Qt platform backends -- under the headless "offscreen" platform
        # in particular, the ambient default has been observed to report
        # unreliable advance widths for non-Latin scripts. Qt falls back
        # gracefully if the named family isn't available (e.g. on Linux).
        self._preview_font = QtGui.QFont("Segoe UI")
        self._preview_font.setPointSize(PREVIEW_FONT_PT)

        self._tick_timer = QtCore.QTimer(self)
        self._tick_timer.setInterval(TICK_INTERVAL_MS)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

    # -- public API ----------------------------------------------------------

    def show_listening(self) -> None:
        self._state = "listening"
        self._reposition()
        self.show()

    def show_processing(self) -> None:
        self._state = "processing"
        self._reposition()
        self.show()

    def hide(self) -> None:  # noqa: A003 - intentionally overrides QWidget.hide
        self._state = "idle"
        self._preview_text = ""
        super().hide()

    def set_level(self, rms: float) -> None:
        # Amplify a bit and clamp, mirrors Linux's
        # `max(0.0, min(1.0, rms * 4.0))`.
        self._level = max(0.0, min(1.0, rms * 4.0))
        self.update()

    def set_preview(self, text: str) -> None:
        text = (text or "").strip()
        if len(text) > PREVIEW_MAX_CHARS:
            # Outer cap only, to avoid pathological layout costs when the
            # transcript grows very long -- keep the tail (newest text),
            # matching the eliding direction below.
            text = text[-PREVIEW_MAX_CHARS:]
        self._preview_text = text
        self.update()

    # -- internals -------------------------------------------------------

    def _reposition(self) -> None:
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        x = geom.x() + (geom.width() - self.width()) // 2
        y = geom.y() + geom.height() - int(geom.height() * 0.15) - self.height() // 2
        self.move(x, y)

    def _on_tick(self) -> None:
        if self.isVisible():
            self.update()

    def _elided_preview_text(self, width: int) -> str:
        """Elide ``self._preview_text`` to fit ``width`` px, tail-preserving.

        See the module docstring for why ``ElideLeft`` is direction-safe
        for RTL (Hebrew/Arabic) preview text.
        """
        if not self._preview_text:
            return ""
        metrics = QtGui.QFontMetrics(self._preview_font)
        return metrics.elidedText(
            self._preview_text, QtCore.Qt.TextElideMode.ElideLeft, max(0, width)
        )

    # -- drawing -----------------------------------------------------------

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        # Rounded (stadium) pill background.
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(26, 26, 31, 224))
        radius = (h - 8) / 2
        painter.drawRoundedRect(QtCore.QRectF(4, 4, w - 8, h - 8), radius, radius)

        # Waveform bars -- sole recording-status indicator.
        t = time.monotonic() - self._start_time
        if self._state == "processing":
            # Synthetic calm drift so the overlay does not look frozen but
            # is visibly not tied to microphone input.
            level = PROCESSING_LEVEL_BASE + PROCESSING_LEVEL_AMPLITUDE * math.sin(
                t * PROCESSING_LEVEL_HZ * 2 * math.pi
            )
            bar_color = WAVEFORM_PROCESSING_COLOR
        else:
            # Listening (or idle): mic-reactive white.
            level = self._level
            bar_color = WAVEFORM_LISTENING_COLOR

        bars = WAVEFORM_BAR_COUNT
        gap = WAVEFORM_BAR_GAP
        total_w = w - 2 * WAVEFORM_HORIZONTAL_MARGIN
        base_x = WAVEFORM_HORIZONTAL_MARGIN
        bar_w = max(2.0, (total_w - gap * (bars - 1)) / bars)
        painter.setBrush(bar_color)
        for i in range(bars):
            phase = (i / bars) * 2 * math.pi + t * 6
            amp = level * (0.5 + 0.5 * math.sin(phase))
            bar_h = max(3.0, amp * WAVEFORM_MAX_BAR_H)
            x = base_x + i * (bar_w + gap)
            y = WAVEFORM_CY - bar_h / 2
            corner = min(bar_w / 2, 3)
            painter.drawRoundedRect(QtCore.QRectF(x, y, bar_w, bar_h), corner, corner)

        # Preview text -- tail-preserving elision, see module docstring.
        if self._preview_text:
            text_area_x = PREVIEW_SIDE_MARGIN
            text_area_width = w - 2 * PREVIEW_SIDE_MARGIN
            text_area_y = WAVEFORM_CY + WAVEFORM_MAX_BAR_H // 2 + PREVIEW_TOP_MARGIN
            text_area_height = h - text_area_y - PREVIEW_BOTTOM_MARGIN
            elided = self._elided_preview_text(text_area_width)

            painter.setFont(self._preview_font)
            painter.setPen(QtGui.QColor(255, 255, 255, 184))
            text_rect = QtCore.QRectF(
                text_area_x, text_area_y, text_area_width, max(0, text_area_height)
            )
            painter.drawText(
                text_rect,
                int(
                    QtCore.Qt.AlignmentFlag.AlignHCenter
                    | QtCore.Qt.AlignmentFlag.AlignTop
                ),
                elided,
            )

        painter.end()
