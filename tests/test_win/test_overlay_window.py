"""Tests for the Windows listening overlay window (PySide6 QWidget).

Mirrors the *feature set* of ``myvoice/ui/overlay_window.py`` (frameless,
always-on-top waveform + transcript-preview pill) -- not its Cairo/GTK
drawing code. Real ``QWidget``/``QPainter``/``QFontMetrics`` objects,
headless via the shared ``qapp`` fixture -- no mocking of Qt itself.
"""
from __future__ import annotations

from myvoice_win.ui.overlay_window import OverlayWindow


def test_set_level_does_not_raise_across_rms_range(qapp):
    win = OverlayWindow()
    for rms in (0.0, 0.05, 1.0, 5.0):  # silence, mid, clipped-high
        win.set_level(rms)
    win.close()


def test_set_level_clamps_into_0_1_range(qapp):
    win = OverlayWindow()
    win.set_level(5.0)
    assert 0.0 <= win._level <= 1.0
    win.set_level(-3.0)
    assert 0.0 <= win._level <= 1.0
    win.close()


def test_show_listening_then_hide_toggles_visibility(qapp):
    win = OverlayWindow()
    win.show_listening()
    assert win.isVisible() is True
    win.hide()
    assert win.isVisible() is False
    win.close()


def test_show_processing_makes_window_visible(qapp):
    win = OverlayWindow()
    win.show_processing()
    assert win.isVisible() is True
    win.close()


def test_set_preview_with_long_hebrew_string_does_not_raise(qapp):
    win = OverlayWindow()
    hebrew = "זהו טקסט ניסיוני ארוך שצריך להופיע בשורה התחתונה " * 20
    win.set_preview(hebrew)
    win.close()


def test_set_preview_with_long_arabic_string_does_not_raise(qapp):
    win = OverlayWindow()
    arabic = "هذا هو النص التجريبي الطويل الذي يجب أن يظهر في الشريط السفلي " * 20
    win.set_preview(arabic)
    win.close()


def test_elided_preview_keeps_tail_of_rtl_arabic_text_not_head(qapp):
    win = OverlayWindow()
    arabic = (
        "هذا هو النص التجريبي الطويل الذي يجب أن يظهر في الشريط السفلي "
        "وهو يحتوي على العديد من الكلمات القديمة ثم الكلمات الجديدة في النهاية"
    )
    win.set_preview(arabic)
    # A deliberately narrow pixel width -- exact per-glyph advance widths
    # for Arabic/Hebrew vary by platform font backend (this genuinely
    # differs between Linux's fontconfig-based Qt offscreen platform and
    # Windows' native font stack -- confirmed by a real CI run, where a
    # width/suffix-length combination tuned against the Linux sandbox
    # retained only 2 trailing characters on Windows, not 5), so a width
    # close to the "does this fit" boundary can tip either way and the
    # exact truncation point can land several characters differently. A
    # width this narrow leaves no doubt truncation must happen regardless
    # of platform, so the assertions below test the *mechanism* (elides
    # from the head, keeps the tail), not a pixel-exact boundary.
    elided = win._elided_preview_text(80)

    assert len(elided) < len(arabic)
    # RTL-safety: a live transcript preview must keep the *newest* words
    # (the logical tail of the string, appended most recently) and elide
    # the *oldest* words (the logical head) -- regardless of script. Naive
    # eliding that instead preserved the head and dropped the tail would
    # silently hide the newest dictated words for RTL languages. Checking
    # only the final couple of characters (rather than a longer fixed
    # suffix) keeps this robust against the platform-specific exact
    # truncation boundary discussed above.
    assert elided.endswith(arabic[-2:])
    assert not elided.startswith(arabic[:20])


def test_elided_preview_keeps_tail_of_rtl_hebrew_text_not_head(qapp):
    win = OverlayWindow()
    hebrew = (
        "זהו טקסט ניסיוני ארוך שצריך להופיע בשורה התחתונה "
        "והוא מכיל מילים ישנות רבות ולאחר מכן מילים חדשות בסוף"
    )
    win.set_preview(hebrew)
    # See the Arabic test above for why the width is narrow and the
    # suffix check short -- same platform-font-metric rationale.
    elided = win._elided_preview_text(80)

    assert len(elided) < len(hebrew)
    assert elided.endswith(hebrew[-2:])
    assert not elided.startswith(hebrew[:20])


def test_elided_preview_bounded_length_for_narrow_width(qapp):
    win = OverlayWindow()
    long_text = "word " * 200
    win.set_preview(long_text)
    elided = win._elided_preview_text(150)
    assert len(elided) < len(long_text)


def test_set_preview_empty_and_none_are_safe(qapp):
    win = OverlayWindow()
    win.set_preview("")
    win.set_preview(None)  # defensive, matches Linux's `text or ""` guard
    win.close()


def test_hide_resets_state_and_preview_text(qapp):
    win = OverlayWindow()
    win.show_listening()
    win.set_preview("hello")
    win.hide()
    assert win._preview_text == ""
    assert win._state == "idle"


def test_paint_event_runs_without_raising_with_level_and_preview_set(qapp):
    win = OverlayWindow()
    win.set_level(0.8)
    win.set_preview("hello world, this is the live preview " * 5)
    win.show_listening()
    win.repaint()  # forces a synchronous paintEvent
    win.close()


def test_paint_event_runs_without_raising_while_processing(qapp):
    win = OverlayWindow()
    win.show_processing()
    win.repaint()
    win.close()


def test_window_is_frameless_and_stays_on_top(qapp):
    from PySide6 import QtCore

    win = OverlayWindow()
    flags = win.windowFlags()
    assert bool(flags & QtCore.Qt.WindowType.FramelessWindowHint)
    assert bool(flags & QtCore.Qt.WindowType.WindowStaysOnTopHint)
    win.close()
