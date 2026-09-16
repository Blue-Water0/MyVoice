"""State machine behind the Record Shortcut UI."""
from __future__ import annotations

import gi
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk

from myvoice.ui.shortcut_recorder import (
    KeyEvent,
    RecorderState,
    ShortcutRecorder,
)


def _e(name, mods_from_gdk=0):
    return KeyEvent(keyval_name=name, state=int(mods_from_gdk))


CTRL = Gdk.ModifierType.CONTROL_MASK
ALT = Gdk.ModifierType.MOD1_MASK
SHIFT = Gdk.ModifierType.SHIFT_MASK
SUPER = getattr(Gdk.ModifierType, "SUPER_MASK", Gdk.ModifierType.MOD4_MASK)


def test_starts_idle():
    r = ShortcutRecorder()
    assert r.state is RecorderState.IDLE
    # Feeding a key while idle does nothing.
    r.feed_key(_e("d", CTRL))
    assert r.state is RecorderState.IDLE
    assert r.captured is None


def test_valid_press_captures():
    r = ShortcutRecorder()
    r.start_recording()
    assert r.state is RecorderState.RECORDING
    new_state = r.feed_key(_e("space", SUPER | SHIFT))
    assert new_state is RecorderState.CAPTURED
    assert r.captured_label() == "Super+Shift+Space"


def test_modifier_only_press_stays_recording():
    r = ShortcutRecorder()
    r.start_recording()
    for name in ("Shift_L", "Shift_R", "Control_L", "Alt_R", "Super_L", "Meta_L"):
        assert r.feed_key(_e(name)) is RecorderState.RECORDING
    # Also lock keys and AltGr.
    for name in ("Caps_Lock", "Num_Lock", "ISO_Level3_Shift"):
        assert r.feed_key(_e(name)) is RecorderState.RECORDING
    assert r.captured is None


def test_escape_cancels():
    r = ShortcutRecorder()
    r.start_recording()
    assert r.feed_key(_e("Escape")) is RecorderState.CANCELLED
    assert r.captured is None


def test_escape_before_recording_is_noop():
    r = ShortcutRecorder()
    # Feeding while idle does nothing.
    assert r.feed_key(_e("Escape")) is RecorderState.IDLE


def test_reset_returns_to_idle():
    r = ShortcutRecorder()
    r.start_recording()
    r.feed_key(_e("d", CTRL | ALT))
    assert r.state is RecorderState.CAPTURED
    r.reset()
    assert r.state is RecorderState.IDLE
    assert r.captured is None


def test_no_modifier_still_captures_when_key_is_non_modifier():
    # e.g. F5 with no mods is still a valid (if unwise) shortcut.
    r = ShortcutRecorder()
    r.start_recording()
    assert r.feed_key(_e("F5")) is RecorderState.CAPTURED
    assert r.captured_label() == "F5"


def test_upper_letter_normalised_in_capture():
    r = ShortcutRecorder()
    r.start_recording()
    r.feed_key(_e("A", CTRL))
    assert r.captured_label() == "Ctrl+A"


def test_multiple_start_recording_resets_capture():
    r = ShortcutRecorder()
    r.start_recording()
    r.feed_key(_e("d", CTRL | ALT))
    assert r.state is RecorderState.CAPTURED
    r.start_recording()
    assert r.state is RecorderState.RECORDING
    assert r.captured is None
