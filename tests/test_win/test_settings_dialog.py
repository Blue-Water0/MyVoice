"""Tests for the Windows settings dialog (PySide6).

Mirrors the *callback surface* and field set of
``myvoice/ui/settings_dialog.py`` (GTK) against a real, headless
(``QT_QPA_PLATFORM=offscreen``) ``QApplication`` and real Qt widgets.
Modal confirmation/error popups (``QMessageBox``) are monkeypatched at the
dialog's own thin wrapper methods (``_confirm_yes_no`` / ``_show_error``)
so tests never block on a real modal loop -- Qt itself is never mocked.
"""
from __future__ import annotations

import threading

from PySide6 import QtCore, QtTest, QtWidgets

from myvoice_win.models import model_manager
from myvoice_win.services.settings_service import Settings, VadSettings
from myvoice_win.ui.settings_dialog import SettingsDialog


def _make_settings(**overrides) -> Settings:
    s = Settings(
        language_mode="en",
        hotkey="win+shift+space",
        microphone="Built-in Mic",
        start_minimized=False,
        autostart=False,
        engine="faster-whisper",
        model="medium",
        compute_type="auto",
        text_app_desktop_id="",
        vad=VadSettings(aggressiveness=2, min_speech_ms=300, silence_ms=700, max_segment_ms=12000),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_dialog(qapp, settings=None, **overrides):
    calls = {"apply": [], "clear_cache": [], "try_hotkey": []}
    kwargs = dict(
        parent=None,
        settings=settings or _make_settings(),
        on_apply=lambda s: calls["apply"].append(s),
        on_clear_cache=lambda: calls["clear_cache"].append(True),
        on_try_hotkey=lambda accel: (calls["try_hotkey"].append(accel), None)[1],
    )
    kwargs.update(overrides)
    dlg = SettingsDialog(**kwargs)
    return dlg, calls


def test_dialog_constructs_with_all_expected_widgets(qapp):
    dlg, _calls = _make_dialog(qapp)
    assert isinstance(dlg._lang_combo, QtWidgets.QComboBox)
    assert isinstance(dlg._model_combo, QtWidgets.QComboBox)
    assert isinstance(dlg._compute_combo, QtWidgets.QComboBox)
    assert isinstance(dlg._agg_spin, QtWidgets.QSpinBox)
    assert isinstance(dlg._min_speech_spin, QtWidgets.QSpinBox)
    assert isinstance(dlg._silence_spin, QtWidgets.QSpinBox)
    assert isinstance(dlg._max_seg_spin, QtWidgets.QSpinBox)
    assert isinstance(dlg._text_app_edit, QtWidgets.QLineEdit)
    assert isinstance(dlg._start_min_check, QtWidgets.QCheckBox)
    assert isinstance(dlg._autostart_check, QtWidgets.QCheckBox)
    assert isinstance(dlg._record_btn, QtWidgets.QPushButton)
    assert isinstance(dlg._reset_btn, QtWidgets.QPushButton)


def test_model_combo_populated_with_preset_labels(qapp):
    dlg, _calls = _make_dialog(qapp)
    labels = [dlg._model_combo.itemText(i) for i in range(dlg._model_combo.count())]
    assert any("medium" in l.lower() for l in labels)
    assert dlg._model_combo.count() == 3  # small/medium/large-v3-turbo
    idx = dlg._model_combo.findData("medium")
    assert dlg._model_combo.currentIndex() == idx


# ---- collect() round-trip ------------------------------------------------


def test_collect_round_trips_dropdown_field(qapp):
    dlg, _calls = _make_dialog(qapp)
    idx = dlg._lang_combo.findData("ar")
    dlg._lang_combo.setCurrentIndex(idx)
    result = dlg.collect()
    assert result is not None
    assert result.language_mode == "ar"


def test_collect_round_trips_checkbox_field(qapp):
    dlg, _calls = _make_dialog(qapp)
    assert dlg._autostart_check.isChecked() is False
    dlg._autostart_check.setChecked(True)
    result = dlg.collect()
    assert result.autostart is True


def test_collect_round_trips_text_field(qapp):
    dlg, _calls = _make_dialog(qapp)
    dlg._text_app_edit.setText("org.example.editor.desktop")
    result = dlg.collect()
    assert result.text_app_desktop_id == "org.example.editor.desktop"


def test_collect_round_trips_start_minimized_checkbox(qapp):
    dlg, _calls = _make_dialog(qapp, _make_settings(start_minimized=True))
    assert dlg._start_min_check.isChecked() is True
    dlg._start_min_check.setChecked(False)
    result = dlg.collect()
    assert result.start_minimized is False


def test_collect_round_trips_vad_spinboxes(qapp):
    dlg, _calls = _make_dialog(qapp)
    dlg._agg_spin.setValue(3)
    dlg._min_speech_spin.setValue(400)
    dlg._silence_spin.setValue(800)
    dlg._max_seg_spin.setValue(15000)
    result = dlg.collect()
    assert result.vad.aggressiveness == 3
    assert result.vad.min_speech_ms == 400
    assert result.vad.silence_ms == 800
    assert result.vad.max_segment_ms == 15000


def test_collect_preserves_fields_not_editable_in_dialog(qapp):
    settings = _make_settings(microphone="Special Mic", engine="faster-whisper")
    dlg, _calls = _make_dialog(qapp, settings)
    result = dlg.collect()
    assert result.microphone == "Special Mic"
    assert result.engine == "faster-whisper"
    assert result.schema_version == settings.schema_version


def test_collect_invalid_hotkey_shows_error_and_returns_none(qapp, monkeypatch):
    dlg, _calls = _make_dialog(qapp)
    errors = []
    monkeypatch.setattr(dlg, "_show_error", lambda msg: errors.append(msg))
    dlg._current_hotkey_accel = "not+a+real+hotkey"
    result = dlg.collect()
    assert result is None
    assert errors  # an error was surfaced


# ---- hotkey rebind row ----------------------------------------------------


def test_try_assign_hotkey_updates_label_and_current_accel(qapp):
    dlg, _calls = _make_dialog(qapp)
    dlg._try_assign_hotkey("ctrl+alt+d")
    assert dlg._current_hotkey_accel == "ctrl+alt+d"
    assert "ctrl+alt+d" in dlg._hotkey_label.text()


def test_try_assign_hotkey_calls_on_try_hotkey_callback(qapp):
    dlg, calls = _make_dialog(qapp)
    dlg._try_assign_hotkey("ctrl+alt+d")
    assert calls["try_hotkey"] == ["ctrl+alt+d"]


def test_try_assign_hotkey_rolls_back_on_conflict(qapp):
    dlg, _calls = _make_dialog(qapp, on_try_hotkey=lambda accel: "already in use")
    original = dlg._current_hotkey_accel
    dlg._try_assign_hotkey("ctrl+alt+d")
    assert dlg._current_hotkey_accel == original
    assert "already in use" in dlg._hotkey_feedback.text()


def test_try_assign_hotkey_rejects_invalid_accel_string(qapp):
    dlg, calls = _make_dialog(qapp)
    original = dlg._current_hotkey_accel
    dlg._try_assign_hotkey("not+a+real+hotkey")
    assert dlg._current_hotkey_accel == original
    assert calls["try_hotkey"] == []  # never even asked the app to try it


def test_reset_hotkey_to_default_uses_settings_default(qapp):
    dlg, _calls = _make_dialog(qapp, _make_settings(hotkey="ctrl+alt+d"))
    dlg._reset_hotkey_to_default()
    assert dlg._current_hotkey_accel == Settings().hotkey


def test_no_on_try_hotkey_callback_treats_assignment_as_successful(qapp):
    dlg, _calls = _make_dialog(qapp, on_try_hotkey=None)
    dlg._try_assign_hotkey("ctrl+alt+d")
    assert dlg._current_hotkey_accel == "ctrl+alt+d"


# ---- model install/download/delete ----------------------------------------


def test_model_rows_created_for_every_preset(qapp):
    dlg, _calls = _make_dialog(qapp)
    assert set(dlg._model_row_widgets.keys()) == {
        "small", "medium", "large-v3-turbo",
    }


def test_uninstalled_model_row_shows_download_button(qapp):
    dlg, _calls = _make_dialog(qapp)
    row = dlg._model_row_widgets["small"]
    assert row["button"].text() == "Download"


def test_installed_model_row_shows_delete_button(qapp, monkeypatch):
    monkeypatch.setattr(model_manager, "is_installed", lambda key: key == "small")
    monkeypatch.setattr(model_manager, "installed_size_bytes", lambda key: 123456)
    dlg, _calls = _make_dialog(qapp)
    row = dlg._model_row_widgets["small"]
    assert row["button"].text() == "Delete"


def test_delete_button_click_calls_model_manager_delete(qapp, monkeypatch):
    monkeypatch.setattr(model_manager, "is_installed", lambda key: key == "small")
    monkeypatch.setattr(model_manager, "installed_size_bytes", lambda key: 123456)
    dlg, _calls = _make_dialog(qapp, _make_settings(model="medium"))
    monkeypatch.setattr(dlg, "_confirm_yes_no", lambda *a: True)
    deleted = []
    monkeypatch.setattr(model_manager, "delete_model", lambda key: deleted.append(key))
    row = dlg._model_row_widgets["small"]
    QtTest.QTest.mouseClick(row["button"], QtCore.Qt.MouseButton.LeftButton)
    assert deleted == ["small"]


def test_delete_button_disabled_for_the_currently_active_model(qapp, monkeypatch):
    """Matches the Linux dialog: the active model's Delete action is
    disabled outright (rather than clickable-then-rejected) so a real
    mouse click on it is a no-op -- this is the same "menu item
    insensitive" behaviour the GTK dialog uses."""
    monkeypatch.setattr(model_manager, "is_installed", lambda key: key == "medium")
    monkeypatch.setattr(model_manager, "installed_size_bytes", lambda key: 123456)
    dlg, _calls = _make_dialog(qapp, _make_settings(model="medium"))
    row = dlg._model_row_widgets["medium"]
    assert row["button"].isEnabled() is False
    deleted = []
    monkeypatch.setattr(model_manager, "delete_model", lambda key: deleted.append(key))
    QtTest.QTest.mouseClick(row["button"], QtCore.Qt.MouseButton.LeftButton)
    assert deleted == []


def test_delete_model_guard_rejects_deleting_the_active_model_directly(qapp, monkeypatch):
    """Belt-and-braces: even if ``_on_delete_model_clicked`` were reached
    for the active model (e.g. a future caller bypassing the row button),
    it must still refuse rather than delete the in-use model."""
    monkeypatch.setattr(model_manager, "is_installed", lambda key: key == "medium")
    monkeypatch.setattr(model_manager, "installed_size_bytes", lambda key: 123456)
    dlg, _calls = _make_dialog(qapp, _make_settings(model="medium"))
    errors = []
    monkeypatch.setattr(dlg, "_show_error", lambda msg: errors.append(msg))
    deleted = []
    monkeypatch.setattr(model_manager, "delete_model", lambda key: deleted.append(key))
    dlg._on_delete_model_clicked("medium")
    assert deleted == []
    assert errors


def test_download_button_click_declined_confirmation_does_not_install(qapp, monkeypatch):
    dlg, _calls = _make_dialog(qapp)
    monkeypatch.setattr(dlg, "_confirm_yes_no", lambda *a: False)
    installed = []
    monkeypatch.setattr(model_manager, "install_model", lambda key, on_progress=None: installed.append(key))
    row = dlg._model_row_widgets["small"]
    QtTest.QTest.mouseClick(row["button"], QtCore.Qt.MouseButton.LeftButton)
    assert installed == []


def test_download_button_click_confirmed_installs_in_background_and_updates_row(qapp, monkeypatch):
    dlg, _calls = _make_dialog(qapp)
    monkeypatch.setattr(dlg, "_confirm_yes_no", lambda *a: True)

    installed_keys = []
    done_event = threading.Event()

    def fake_install(key, on_progress=None):
        installed_keys.append(key)
        if on_progress is not None:
            on_progress(1, 1)

    monkeypatch.setattr(model_manager, "install_model", fake_install)

    def fake_is_installed(key):
        return key in installed_keys

    monkeypatch.setattr(model_manager, "is_installed", fake_is_installed)
    monkeypatch.setattr(model_manager, "installed_size_bytes", lambda key: 999)

    original_finished = dlg._on_install_finished

    def wrapped_finished(key, error):
        original_finished(key, error)
        done_event.set()

    monkeypatch.setattr(dlg, "_on_install_finished", wrapped_finished)

    row = dlg._model_row_widgets["small"]
    QtTest.QTest.mouseClick(row["button"], QtCore.Qt.MouseButton.LeftButton)

    # Background thread + queued signal: pump the Qt event loop until done.
    for _ in range(200):
        if done_event.is_set():
            break
        QtTest.QTest.qWait(10)

    assert done_event.is_set()
    assert installed_keys == ["small"]
    assert dlg._active_model_key == "small"
    assert row["button"].text() == "Delete"


def test_model_combo_selecting_installed_model_switches_active_immediately(qapp, monkeypatch):
    monkeypatch.setattr(model_manager, "is_installed", lambda key: key == "large-v3-turbo")
    monkeypatch.setattr(model_manager, "installed_size_bytes", lambda key: 999)
    dlg, _calls = _make_dialog(qapp)
    idx = dlg._model_combo.findData("large-v3-turbo")
    dlg._model_combo.setCurrentIndex(idx)
    assert dlg._active_model_key == "large-v3-turbo"


def test_model_combo_selecting_uninstalled_model_prompts_and_reverts_on_decline(qapp, monkeypatch):
    monkeypatch.setattr(model_manager, "is_installed", lambda key: key == "medium")
    dlg, _calls = _make_dialog(qapp, _make_settings(model="medium"))
    monkeypatch.setattr(dlg, "_confirm_yes_no", lambda *a: False)
    idx = dlg._model_combo.findData("small")
    dlg._model_combo.setCurrentIndex(idx)
    assert dlg._active_model_key == "medium"
    assert dlg._model_combo.currentData() == "medium"
