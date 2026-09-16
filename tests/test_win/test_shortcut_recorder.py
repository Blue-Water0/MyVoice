"""Tests for the Windows shortcut recorder (PySide6).

Real ``QApplication`` (via the shared ``qapp`` fixture), real
``QtGui.QKeyEvent`` objects fed directly to the widget's overridden
``keyPressEvent`` -- nothing about Qt itself is mocked here.
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui

from myvoice_win.services.hotkey_service import parse_accel
from myvoice_win.ui.shortcut_recorder import ShortcutRecorder, build_accel


def _key_event(key, modifiers=QtCore.Qt.KeyboardModifier.NoModifier):
    return QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, modifiers)


# ---- build_accel ------------------------------------------------------


def test_build_accel_simple_key_no_modifiers():
    ev = _key_event(QtCore.Qt.Key.Key_D)
    assert build_accel(ev) == "d"


def test_build_accel_win_shift_space_round_trips_through_parse_accel():
    ev = _key_event(
        QtCore.Qt.Key.Key_Space,
        QtCore.Qt.KeyboardModifier.MetaModifier | QtCore.Qt.KeyboardModifier.ShiftModifier,
    )
    accel = build_accel(ev)
    assert accel == "win+shift+space"
    # Must be understood by the hotkey service's own parser.
    modifiers, vk = parse_accel(accel)
    assert (modifiers, vk) == parse_accel("win+shift+space")


def test_build_accel_ctrl_alt_letter():
    ev = _key_event(
        QtCore.Qt.Key.Key_D,
        QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.AltModifier,
    )
    assert build_accel(ev) == "ctrl+alt+d"
    parse_accel(build_accel(ev))


def test_build_accel_modifier_only_press_yields_none():
    """Holding Ctrl alone (no other key yet) must not produce an accel."""
    ev = _key_event(
        QtCore.Qt.Key.Key_Control, QtCore.Qt.KeyboardModifier.ControlModifier,
    )
    assert build_accel(ev) is None


def test_build_accel_unsupported_key_yields_none():
    ev = _key_event(QtCore.Qt.Key.Key_F5)
    assert build_accel(ev) is None


# ---- ShortcutRecorder dialog -------------------------------------------


def test_recorder_captures_full_combo_and_accepts(qapp):
    recorder = ShortcutRecorder()
    ev = _key_event(
        QtCore.Qt.Key.Key_Space,
        QtCore.Qt.KeyboardModifier.MetaModifier | QtCore.Qt.KeyboardModifier.ShiftModifier,
    )
    result_holder = {}
    recorder.finished.connect(lambda code: result_holder.setdefault("code", code))
    recorder.keyPressEvent(ev)
    assert recorder.captured_accel == "win+shift+space"
    assert result_holder["code"] == recorder.DialogCode.Accepted


def test_recorder_modifier_only_press_keeps_waiting(qapp):
    recorder = ShortcutRecorder()
    finished_calls = []
    recorder.finished.connect(lambda code: finished_calls.append(code))
    ev = _key_event(
        QtCore.Qt.Key.Key_Control, QtCore.Qt.KeyboardModifier.ControlModifier,
    )
    recorder.keyPressEvent(ev)
    assert recorder.captured_accel is None
    assert finished_calls == []  # dialog neither accepted nor rejected yet


def test_recorder_escape_cancels(qapp):
    recorder = ShortcutRecorder()
    finished_calls = []
    recorder.finished.connect(lambda code: finished_calls.append(code))
    ev = _key_event(QtCore.Qt.Key.Key_Escape)
    recorder.keyPressEvent(ev)
    assert recorder.captured_accel is None
    assert finished_calls == [recorder.DialogCode.Rejected]


def test_recorder_can_still_capture_after_a_modifier_only_press(qapp):
    recorder = ShortcutRecorder()
    recorder.keyPressEvent(_key_event(
        QtCore.Qt.Key.Key_Shift, QtCore.Qt.KeyboardModifier.ShiftModifier,
    ))
    assert recorder.captured_accel is None
    recorder.keyPressEvent(_key_event(
        QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ShiftModifier,
    ))
    assert recorder.captured_accel == "shift+a"
