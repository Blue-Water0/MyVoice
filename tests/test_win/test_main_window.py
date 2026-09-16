"""Tests for the Windows main window (PySide6).

Mirrors the *callback surface* of ``myvoice/ui/main_window.py`` (GTK) --
status states, Start/Stop toggle, language/mic dropdowns, transcript
preview with Copy/Open actions, Settings button -- against a real,
headless (``QT_QPA_PLATFORM=offscreen``) ``QApplication`` and real Qt
widgets. Nothing here mocks Qt itself.
"""
from __future__ import annotations

from PySide6 import QtTest, QtCore, QtWidgets

from myvoice_win.ui.main_window import MainWindow


def _make_window(qapp, **overrides):
    calls = {
        "toggle": [],
        "settings": [],
        "quit": [],
        "language": [],
        "mic": [],
        "copy": [],
        "open_text_app": [],
    }
    devices = overrides.pop("devices", [
        {"name": "USB Mic", "default": True},
        {"name": "Built-in Mic", "default": False},
    ])
    kwargs = dict(
        on_toggle=lambda: calls["toggle"].append(True),
        on_open_settings=lambda: calls["settings"].append(True),
        on_quit=lambda: calls["quit"].append(True),
        on_language_changed=lambda code: calls["language"].append(code),
        on_mic_changed=lambda name: calls["mic"].append(name),
        get_input_devices=lambda: devices,
        initial_language_mode="auto",
        initial_mic=None,
        on_copy_transcript=lambda: calls["copy"].append(True),
        on_open_transcript_in_text_app=lambda: calls["open_text_app"].append(True),
    )
    kwargs.update(overrides)
    window = MainWindow(**kwargs)
    return window, calls


def test_initial_state_is_idle_with_transcript_actions_disabled(qapp):
    window, _calls = _make_window(qapp)
    assert window._toggle_btn.text() == "Start Listening"
    assert window._transcript_view.toPlainText() == ""
    assert window._copy_transcript_btn.isEnabled() is False
    assert window._open_transcript_btn.isEnabled() is False


def test_toggle_button_click_fires_on_toggle(qapp):
    window, calls = _make_window(qapp)
    QtTest.QTest.mouseClick(window._toggle_btn, QtCore.Qt.MouseButton.LeftButton)
    assert calls["toggle"] == [True]


def test_settings_button_click_fires_on_open_settings(qapp):
    window, calls = _make_window(qapp)
    QtTest.QTest.mouseClick(window._settings_btn, QtCore.Qt.MouseButton.LeftButton)
    assert calls["settings"] == [True]


def test_quit_button_click_fires_on_quit(qapp):
    window, calls = _make_window(qapp)
    QtTest.QTest.mouseClick(window._quit_btn, QtCore.Qt.MouseButton.LeftButton)
    assert calls["quit"] == [True]


def test_language_combo_populated_and_change_fires_callback(qapp):
    window, calls = _make_window(qapp)
    labels = [window._lang_combo.itemText(i) for i in range(window._lang_combo.count())]
    assert labels == [
        "Automatic (OS language)", "English", "Hebrew (עברית)", "Arabic (العربية)",
    ]
    idx = window._lang_combo.findData("he")
    window._lang_combo.setCurrentIndex(idx)
    assert calls["language"] == ["he"]


def test_mic_combo_populated_with_system_default_and_devices(qapp):
    window, _calls = _make_window(qapp)
    labels = [window._mic_combo.itemText(i) for i in range(window._mic_combo.count())]
    assert labels == [
        "System default", "USB Mic  (default)", "Built-in Mic",
    ]


def test_mic_combo_change_fires_on_mic_changed_with_device_name(qapp):
    window, calls = _make_window(qapp)
    idx = window._mic_combo.findData("Built-in Mic")
    window._mic_combo.setCurrentIndex(idx)
    assert calls["mic"] == ["Built-in Mic"]


def test_mic_combo_change_to_system_default_fires_none(qapp):
    window, calls = _make_window(qapp)
    idx = window._mic_combo.findData("Built-in Mic")
    window._mic_combo.setCurrentIndex(idx)
    window._mic_combo.setCurrentIndex(0)
    assert calls["mic"] == ["Built-in Mic", None]


def test_append_transcript_updates_textedit_content(qapp):
    window, _calls = _make_window(qapp)
    window.append_transcript("hello")
    window.append_transcript("world")
    assert window._transcript_view.toPlainText() == "hello world"


def test_append_transcript_enables_actions_when_controller_calls_it(qapp):
    window, _calls = _make_window(qapp)
    assert window._copy_transcript_btn.isEnabled() is False
    window.append_transcript("hi")
    window.set_transcript_actions_enabled(True)
    assert window._copy_transcript_btn.isEnabled() is True
    assert window._open_transcript_btn.isEnabled() is True


def test_clear_transcript_empties_view_and_disables_actions(qapp):
    window, _calls = _make_window(qapp)
    window.append_transcript("some text")
    window.set_transcript_actions_enabled(True)
    window.clear_transcript()
    assert window._transcript_view.toPlainText() == ""
    assert window._copy_transcript_btn.isEnabled() is False
    assert window._open_transcript_btn.isEnabled() is False


def test_set_transcript_actions_enabled_false_disables_buttons(qapp):
    window, _calls = _make_window(qapp)
    window.set_transcript_actions_enabled(True)
    assert window._copy_transcript_btn.isEnabled() is True
    window.set_transcript_actions_enabled(False)
    assert window._copy_transcript_btn.isEnabled() is False
    assert window._open_transcript_btn.isEnabled() is False


def test_copy_transcript_button_click_fires_callback_when_enabled(qapp):
    window, calls = _make_window(qapp)
    window.set_transcript_actions_enabled(True)
    QtTest.QTest.mouseClick(window._copy_transcript_btn, QtCore.Qt.MouseButton.LeftButton)
    assert calls["copy"] == [True]


def test_open_transcript_button_click_fires_callback_when_enabled(qapp):
    window, calls = _make_window(qapp)
    window.set_transcript_actions_enabled(True)
    QtTest.QTest.mouseClick(window._open_transcript_btn, QtCore.Qt.MouseButton.LeftButton)
    assert calls["open_text_app"] == [True]


def test_disabled_transcript_buttons_do_not_fire_callback(qapp):
    window, calls = _make_window(qapp)
    # Actions start disabled -- a real Qt click on a disabled QPushButton
    # must not deliver the click / fire the connected slot.
    QtTest.QTest.mouseClick(window._copy_transcript_btn, QtCore.Qt.MouseButton.LeftButton)
    QtTest.QTest.mouseClick(window._open_transcript_btn, QtCore.Qt.MouseButton.LeftButton)
    assert calls["copy"] == []
    assert calls["open_text_app"] == []


def test_copy_and_open_callbacks_are_optional(qapp):
    window, _calls = _make_window(
        qapp, on_copy_transcript=None, on_open_transcript_in_text_app=None,
    )
    window.set_transcript_actions_enabled(True)
    # Must not raise even though no callback was supplied.
    QtTest.QTest.mouseClick(window._copy_transcript_btn, QtCore.Qt.MouseButton.LeftButton)
    QtTest.QTest.mouseClick(window._open_transcript_btn, QtCore.Qt.MouseButton.LeftButton)


def test_set_listening_true_updates_button_label(qapp):
    window, _calls = _make_window(qapp)
    window.set_listening(True)
    assert window._toggle_btn.text() == "Stop Listening"
    window.set_listening(False)
    assert window._toggle_btn.text() == "Start Listening"


def test_set_status_updates_label_text(qapp):
    window, _calls = _make_window(qapp)
    window.set_status("listening", "Listening...")
    assert window._status_label.text() == "Listening..."
    window.set_status("error", "Microphone unavailable")
    assert window._status_label.text() == "Microphone unavailable"


def test_set_language_mode_updates_combo_without_recursive_callback(qapp):
    window, calls = _make_window(qapp)
    window.set_language_mode("ar")
    assert window._lang_combo.currentData() == "ar"
    # Programmatic updates must not re-trigger the language-changed callback.
    assert calls["language"] == []


def test_set_microphone_refreshes_combo_selection(qapp):
    window, _calls = _make_window(qapp)
    window.set_microphone("Built-in Mic")
    assert window._mic_combo.currentData() == "Built-in Mic"


def test_mic_refresh_button_rescans_devices(qapp):
    devices = [{"name": "USB Mic", "default": True}]
    window, _calls = _make_window(qapp, devices=devices)
    assert window._mic_combo.count() == 2  # default + USB Mic
    devices.append({"name": "New Headset", "default": False})
    QtTest.QTest.mouseClick(window._mic_refresh_btn, QtCore.Qt.MouseButton.LeftButton)
    assert window._mic_combo.count() == 3


def test_main_window_has_no_closeevent_override(qapp):
    """Close-to-tray is an app-level (Task 12) concern, not MainWindow's.

    Mirrors the Linux app: ``myvoice/ui/main_window.py`` does not override
    ``delete-event`` either -- ``myvoice/app.py`` connects to it
    externally. MainWindow must keep Qt's default ``closeEvent`` so a
    plain ``.close()`` really closes it (e.g. in tests); the future
    ``myvoice_win/app.py`` is responsible for installing an event filter
    (or otherwise intercepting the close) to hide-to-tray instead.
    """
    window, _calls = _make_window(qapp)
    assert MainWindow.closeEvent is QtWidgets.QMainWindow.closeEvent
