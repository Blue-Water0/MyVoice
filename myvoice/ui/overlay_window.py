"""Listening overlay: always-on-top, click-through, animated waveform.

- GTK 3 window, POPUP_MENU type hint, no decorations, transparent background.
- Positioned near bottom-center of the primary monitor.
- Click-through via X11 SHAPE extension (empty input region). If SHAPE is
  unavailable (rare) we log and continue non-clickthrough.
- Waveform bars driven by RMS updates from the audio thread (safe via a lock).
  Bars are white while listening (mic-reactive) and amber while processing
  (synthetic calm drift, not tied to mic input).
- Fade in/out over ~200 ms by animating window opacity.
"""
from __future__ import annotations

import logging
import math
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # type: ignore

log = logging.getLogger(__name__)


OVERLAY_WIDTH = 280

# --- Waveform geometry (v0.9.4, mic circle/pulse removed) --------------------
# The red microphone disc and its pulsing rings were removed. The waveform
# bars are now the sole recording-status indicator, horizontally centered
# in the upper region of the pill.
WAVEFORM_CY = 39                # waveform bars vertical center
WAVEFORM_MAX_BAR_H = 50         # bar height at maximum amplitude
WAVEFORM_TOTAL_WIDTH = 200      # horizontal span reserved for bars
WAVEFORM_BAR_COUNT = 18
WAVEFORM_BAR_GAP = 3
WAVEFORM_BASE_X = (OVERLAY_WIDTH - WAVEFORM_TOTAL_WIDTH) // 2  # = 40
# Max Y any bar reaches at full amplitude:
#   WAVEFORM_CY + WAVEFORM_MAX_BAR_H / 2 = 39 + 25 = 64
MAX_ANIMATED_BOTTOM = WAVEFORM_CY + WAVEFORM_MAX_BAR_H // 2  # = 64

# --- State colors (RGBA) -----------------------------------------------------
# Waveform bars only. There is no mic disc, no pulse ring, no red anywhere.
WAVEFORM_LISTENING_COLOR = (1.0, 1.0, 1.0, 0.85)    # white, mic-reactive
WAVEFORM_PROCESSING_COLOR = (0.98, 0.65, 0.20, 0.90)  # amber, synthetic drift

# Processing-state synthetic level: bars sit at a calm mid-low value with a
# slow ~1.5 Hz drift so the overlay does not look frozen but is clearly
# distinguishable from active-mic bars.
PROCESSING_LEVEL_BASE = 0.25
PROCESSING_LEVEL_AMPLITUDE = 0.05
PROCESSING_LEVEL_HZ = 1.5

# --- Preview text layout (Pango) --------------------------------------------
PREVIEW_FONT_PX = 13             # absolute pixel size passed to Pango
# Target actual visible gap between the waveform's max-amplitude bottom edge
# and the top of the rendered glyph ink. Because we use Pango's real ink
# extents (not font metrics) at draw time, this value is honoured directly
# regardless of script — Arabic diacritics and English "H" both hit ~this gap.
PREVIEW_INK_GAP_FROM_WAVEFORM = 4
PREVIEW_BOTTOM_PADDING = 8       # min px between text ink bottom and pill edge

# Sans 13 px (absolute) Pango metrics on Linux fontconfig default:
#   ascent  = 14.0, descent = 4.0
# Worst-case ink heights measured on this system with the pixel-accurate
# render harness:
#   Arabic with diacritics: ink height 20 px (baseline + ~9 above, +9 below)
#   English descenders "jumpy pygmy quill": 13 px
# We reserve 20 px for ink height + 2 px descent safety.
PREVIEW_INK_MAX_HEIGHT = 20
PREVIEW_DESCENT_SAFETY = 2

# Pill outer inset — the dark background is drawn at (4, 4, w-8, h-8),
# so its visible bottom edge is at y = h - _PILL_INSET.
_PILL_INSET = 4

# Compact layout math (v0.9.6):
#   MAX_ANIMATED_BOTTOM  = 64  (waveform bottom @ max amp)
#   ink-top (pinned)     = 64 + 4                       = 68
#   ink-bottom (max)     = 68 + 20 + 2                  = 90
#   pill-inner-bottom    = 90 + 8                       = 98
#   OVERLAY_HEIGHT       = 98 + 4                       = 102
# Top margin (pill inner top -> waveform max top) = WAVEFORM_CY - WAVEFORM_MAX_BAR_H/2 - _PILL_INSET
#                                                 = 39 - 25 - 4 = 10
# => 10 top / 4 gap / 8 bottom — even more compact than v0.9.5.
_TEXT_INK_TOP = MAX_ANIMATED_BOTTOM + PREVIEW_INK_GAP_FROM_WAVEFORM  # = 68
_INK_BOTTOM_MAX = _TEXT_INK_TOP + PREVIEW_INK_MAX_HEIGHT + PREVIEW_DESCENT_SAFETY
OVERLAY_HEIGHT = _INK_BOTTOM_MAX + PREVIEW_BOTTOM_PADDING + _PILL_INSET

# Horizontal text safe area (inside the pill, 12 px in from either rounded end).
# We add 4 px of layout slack so Pango's soft width limit produces ink that
# still fits (Pango sizes to logical width, but glyph ink can extend 1-2 px
# past that on scripts with wide sidebearings).
PREVIEW_HORIZONTAL_PADDING = 12
PREVIEW_LAYOUT_SLACK = 4
_TEXT_SAFE_X = _PILL_INSET + PREVIEW_HORIZONTAL_PADDING
_TEXT_SAFE_WIDTH = OVERLAY_WIDTH - 2 * _TEXT_SAFE_X
_LAYOUT_WIDTH = _TEXT_SAFE_WIDTH - PREVIEW_LAYOUT_SLACK  # feed to Pango

# Fully-rounded (stadium) pill corner radius.
PILL_CORNER_RADIUS = (OVERLAY_HEIGHT - 2 * _PILL_INSET) // 2


class OverlayWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_title("MyVoice Listening")
        self.set_default_size(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        self.set_resizable(False)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)

        # RGBA visual for transparency
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        self.set_app_paintable(True)

        self._level_lock = threading.Lock()
        self._level = 0.0
        self._level_smoothed = 0.0
        self._state = "idle"   # 'listening' | 'processing' | 'idle'
        self._preview_text = ""
        self._opacity = 0.0
        self._target_opacity = 0.0
        self._fade_start = 0.0
        self._fade_from = 0.0
        self._fade_duration = 0.22

        self._area = Gtk.DrawingArea()
        self._area.set_size_request(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        self._area.connect("draw", self._on_draw)
        self.add(self._area)

        # Pango font description used for the preview text. Absolute pixel
        # size (13 px) makes the rendering match what we measured on the
        # design system, regardless of DPI / text-scale settings.
        self._preview_font_desc = Pango.FontDescription.from_string("Sans")
        self._preview_font_desc.set_absolute_size(PREVIEW_FONT_PX * Pango.SCALE)

        self.connect("realize", self._on_realize)
        self.connect("screen-changed", lambda w, prev: self._reposition())

        self._tick_id = GLib.timeout_add(33, self._tick)  # ~30fps
        self.set_opacity(0.0)

    # -- public API ----------------------------------------------------------

    def show_listening(self) -> None:
        self._state = "listening"
        self._target_opacity = 1.0
        self._fade_from = self._opacity
        self._fade_start = time.monotonic()
        self._reposition()
        self.show_all()
        self.present()  # ensure stacking above; we still don't get focus

    def show_processing(self) -> None:
        self._state = "processing"
        self._target_opacity = 1.0
        self._fade_from = self._opacity
        self._fade_start = time.monotonic()

    def hide_overlay(self) -> None:
        self._state = "idle"
        self._target_opacity = 0.0
        self._fade_from = self._opacity
        self._fade_start = time.monotonic()
        self._preview_text = ""

    def set_level(self, rms: float) -> None:
        with self._level_lock:
            self._level = max(0.0, min(1.0, rms * 4.0))  # amplify a bit

    def set_preview(self, text: str) -> None:
        # Pango handles ellipsis at draw time (PANGO_ELLIPSIZE_START) so the
        # newest words stay visible and RTL / BiDi / combining marks are
        # split correctly. We keep an outer 500-codepoint cap only to avoid
        # pathological layout costs when the transcript grows very long.
        text = (text or "").strip()
        if len(text) > 500:
            text = text[-500:]
        self._preview_text = text
        self._area.queue_draw()

    # -- internals -----------------------------------------------------------

    def _on_realize(self, _w: Gtk.Widget) -> None:
        self._reposition()
        self._make_click_through()

    def _reposition(self) -> None:
        try:
            display = self.get_display()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            if monitor is None:
                return
            geom = monitor.get_geometry()
            x = geom.x + (geom.width - OVERLAY_WIDTH) // 2
            y = geom.y + geom.height - int(geom.height * 0.15) - OVERLAY_HEIGHT // 2
            self.move(x, y)
        except Exception as e:
            log.debug("overlay reposition failed: %s", e)

    def _make_click_through(self) -> None:
        """Set X11 input shape to empty so clicks pass through."""
        try:
            gdk_window = self.get_window()
            if gdk_window is None:
                return
            # Cairo region approach (works on Wayland too; on X11 sets input shape)
            empty = Gdk.Rectangle()
            empty.x = 0; empty.y = 0; empty.width = 0; empty.height = 0
            region = Gdk.Region.new() if hasattr(Gdk, "Region") else None
            if region is None:
                # Modern GTK 3: use cairo.Region via cairo module
                import cairo
                cregion = cairo.Region(cairo.RectangleInt(0, 0, 0, 0))
                gdk_window.input_shape_combine_region(cregion, 0, 0)
            else:
                gdk_window.input_shape_combine_region(region, 0, 0)
            log.debug("Overlay set to click-through")
        except Exception as e:
            log.warning("Could not set overlay click-through: %s", e)

    def _tick(self) -> bool:
        # Animate opacity
        now = time.monotonic()
        if abs(self._opacity - self._target_opacity) > 0.001:
            elapsed = now - self._fade_start
            t = min(1.0, elapsed / self._fade_duration)
            # easeInOut
            t = t * t * (3 - 2 * t)
            self._opacity = self._fade_from + (self._target_opacity - self._fade_from) * t
            self.set_opacity(self._opacity)
            if self._opacity <= 0.01 and self._target_opacity == 0.0:
                self.hide()

        # Smooth level for animation
        with self._level_lock:
            lv = self._level
        self._level_smoothed += (lv - self._level_smoothed) * 0.25
        self._area.queue_draw()
        return True

    # -- drawing -------------------------------------------------------------

    def _on_draw(self, _w, cr) -> bool:  # noqa: ANN001
        w = self.get_allocated_width()
        h = self.get_allocated_height()

        # Clear (transparent)
        cr.set_operator(1)  # CAIRO_OPERATOR_SOURCE
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(2)  # CAIRO_OPERATOR_OVER

        # Rounded (stadium) pill background.
        self._rounded_rect(cr, 4, 4, w - 8, h - 8, PILL_CORNER_RADIUS - 4)
        cr.set_source_rgba(0.10, 0.10, 0.12, 0.88)
        cr.fill()

        # Waveform bars — sole recording-status indicator. No mic circle,
        # no pulsing rings, no red anywhere in this drawing path.
        t = time.monotonic()
        if self._state == "processing":
            # Synthetic calm drift so the overlay does not look frozen but is
            # visibly not tied to microphone input.
            level = PROCESSING_LEVEL_BASE + PROCESSING_LEVEL_AMPLITUDE * math.sin(
                t * PROCESSING_LEVEL_HZ
            )
            bar_color = WAVEFORM_PROCESSING_COLOR
        else:
            # Listening (or fading out after listening): mic-reactive white.
            level = self._level_smoothed
            bar_color = WAVEFORM_LISTENING_COLOR

        bars = WAVEFORM_BAR_COUNT
        gap = WAVEFORM_BAR_GAP
        base_x = WAVEFORM_BASE_X
        total_w = WAVEFORM_TOTAL_WIDTH
        bar_w = max(2.0, (total_w - gap * (bars - 1)) / bars)
        max_bar_h = WAVEFORM_MAX_BAR_H
        for i in range(bars):
            phase = (i / bars) * 2 * math.pi + t * 6
            amp = level * (0.5 + 0.5 * math.sin(phase))
            bh = max(3, amp * max_bar_h)
            x = base_x + i * (bar_w + gap)
            y = WAVEFORM_CY - bh / 2
            self._rounded_rect(cr, x, y, bar_w, bh, min(bar_w / 2, 3))
            cr.set_source_rgba(*bar_color)
            cr.fill()

        # Preview text — rendered via Pango so RTL / BiDi / combining marks
        # / ellipsization are handled correctly.
        #
        # Layout width is _LAYOUT_WIDTH (= _TEXT_SAFE_WIDTH - PREVIEW_LAYOUT_SLACK)
        # so that even with Pango's soft width limit, no glyph ink extends
        # past the visible safe area. Ellipsize=START keeps the newest words.
        #
        # Vertical placement uses Pango's ink_rect (not font metrics) so the
        # actual visible gap between the waveform bottom and the top of the
        # rendered glyph ink is exactly PREVIEW_INK_GAP_FROM_WAVEFORM,
        # regardless of script (Arabic diacritics, English caps, etc.).
        if self._preview_text:
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(self._preview_font_desc)
            layout.set_text(self._preview_text, -1)
            layout.set_width(_LAYOUT_WIDTH * Pango.SCALE)
            layout.set_ellipsize(Pango.EllipsizeMode.START)
            layout.set_alignment(Pango.Alignment.CENTER)
            layout.set_single_paragraph_mode(True)

            ink_rect, _logical_rect = layout.get_pixel_extents()

            # Pin the ink top: layout_top + ink_rect.y = target ink top.
            target_ink_top = MAX_ANIMATED_BOTTOM + PREVIEW_INK_GAP_FROM_WAVEFORM
            layout_top = target_ink_top - ink_rect.y

            # Clamp so ink bottom never crosses the pill inner bottom edge.
            ink_bottom = layout_top + ink_rect.y + ink_rect.height
            max_ink_bottom = h - _PILL_INSET - PREVIEW_DESCENT_SAFETY
            if ink_bottom > max_ink_bottom:
                layout_top -= (ink_bottom - max_ink_bottom)

            # Pango draws from the top-left of the layout box; center it
            # horizontally in the safe area.
            layout_x = _TEXT_SAFE_X + (_TEXT_SAFE_WIDTH - _LAYOUT_WIDTH) / 2
            cr.save()
            cr.set_source_rgba(1, 1, 1, 0.72)
            cr.move_to(layout_x, layout_top)
            PangoCairo.show_layout(cr, layout)
            cr.restore()

        return False

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):  # noqa: ANN001
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
