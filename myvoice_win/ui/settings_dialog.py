"""Settings dialog window (PySide6 / Qt).

Mirrors the *feature set* and callback surface of the Linux
``myvoice/ui/settings_dialog.py`` -- not its GTK widget code: model preset
selection with per-model install status/download/delete (backed by
``myvoice_win.models.model_manager``), language mode, VAD tuning, a
hotkey rebind row that embeds ``shortcut_recorder.ShortcutRecorder``, a
transcript text-app field, and the two app-lifecycle checkboxes.

Constructor signature intentionally mirrors the Linux
``SettingsDialog(parent, settings, on_apply, on_clear_model_cache,
on_try_hotkey=...)`` shape (see ``myvoice/app.py``'s ``_open_settings``)
closely enough that Task 12's ``myvoice_win/app.py`` can wire this the
same way -- with ``dlg.exec()`` in place of GTK's ``dlg.run()``:
``on_apply`` is accepted and stored for the same reason it is on Linux
(constructor-shape parity for the caller) but, matching the Linux
dialog's own behaviour, is not invoked from inside this class -- the
caller is expected to call ``dlg.collect()`` after a
``QDialog.DialogCode.Accepted`` result and apply it itself.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from PySide6 import QtCore, QtWidgets

from myvoice.engines.base import ModelPreset
from myvoice.services.language_service import LANGUAGE_LABELS

from ..engines.registry import engine_info
from ..models import model_manager
from ..services.hotkey_service import parse_accel
from ..services.settings_service import Settings, VadSettings
from .shortcut_recorder import ShortcutRecorder

log = logging.getLogger(__name__)

_COMPUTE_TYPES = [
    ("auto", "Auto (int8, best CPU perf)"),
    ("int8", "int8 (lowest RAM)"),
    ("int8_float32", "int8_float32 (balanced)"),
    ("float32", "float32 (highest quality, slowest)"),
]


class _InstallSignals(QtCore.QObject):
    """Marshals background-download progress/completion onto the GUI thread.

    ``install_model`` runs on a worker thread (see ``_start_model_install``);
    Qt signals emitted from a different thread than their receiver are
    automatically delivered via a queued connection, so the slots below
    only ever touch widgets from the GUI thread.
    """

    progress = QtCore.Signal(str, int, int)
    finished = QtCore.Signal(str, object)


class SettingsDialog(QtWidgets.QDialog):
    def __init__(
        self,
        parent: Optional[QtWidgets.QWidget],
        settings: Settings,
        on_apply: Callable[[Settings], None],
        on_clear_cache: Callable[[], None],
        on_try_hotkey: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        """Settings dialog.

        Parameters:
            on_try_hotkey: If provided, called with a new accelerator string
                when the user assigns one via Record Shortcut/Reset. Return
                None on success, or a human error message on failure -- in
                the error case the dialog keeps the previous shortcut and
                shows the message. Mirrors the Linux dialog's contract so
                the app can attempt the real ``RegisterHotKey`` call and
                fall back cleanly on conflict.
        """
        super().__init__(parent)
        self.setWindowTitle("MyVoice Settings")
        self.resize(520, 640)

        self._on_apply = on_apply
        self._on_clear_cache = on_clear_cache
        self._on_try_hotkey = on_try_hotkey
        self._settings = settings
        self._current_hotkey_accel = settings.hotkey

        self._preset_by_key: dict[str, ModelPreset] = {}
        self._model_row_widgets: dict[str, dict] = {}
        self._active_model_key = settings.model
        self._suppress_model_combo_signal = False

        self._install_signals = _InstallSignals()
        self._install_signals.progress.connect(self._on_install_progress)
        self._install_signals.finished.connect(self._on_install_finished)

        layout = QtWidgets.QVBoxLayout(self)

        self._build_lifecycle_row(layout, settings)
        self._build_voice_group(layout, settings)
        self._build_hotkey_group(layout, settings)
        self._build_text_app_group(layout, settings)
        self._build_vad_group(layout, settings)
        self._build_buttons(layout)

    # ---- construction -----------------------------------------------------

    def _build_lifecycle_row(self, layout: QtWidgets.QVBoxLayout, settings: Settings) -> None:
        row = QtWidgets.QHBoxLayout()
        self._start_min_check = QtWidgets.QCheckBox("Start minimized to tray")
        self._start_min_check.setChecked(settings.start_minimized)
        row.addWidget(self._start_min_check)
        self._autostart_check = QtWidgets.QCheckBox("Start MyVoice automatically on login")
        self._autostart_check.setChecked(settings.autostart)
        row.addWidget(self._autostart_check)
        row.addStretch(1)
        layout.addLayout(row)

    def _build_voice_group(self, layout: QtWidgets.QVBoxLayout, settings: Settings) -> None:
        group = QtWidgets.QGroupBox("Voice && Language")
        form = QtWidgets.QFormLayout(group)

        self._lang_combo = QtWidgets.QComboBox()
        for code, label in LANGUAGE_LABELS.items():
            self._lang_combo.addItem(label, code)
        idx = self._lang_combo.findData(settings.language_mode)
        if idx != -1:
            self._lang_combo.setCurrentIndex(idx)
        form.addRow("Language:", self._lang_combo)

        self._model_combo = QtWidgets.QComboBox()
        info = engine_info(settings.engine)
        for preset in info.presets:
            self._preset_by_key[preset.key] = preset
            self._model_combo.addItem(preset.label, preset.key)
        idx = self._model_combo.findData(settings.model)
        if idx != -1:
            self._model_combo.setCurrentIndex(idx)
        self._model_combo.currentIndexChanged.connect(self._on_model_combo_changed)
        form.addRow("Model quality:", self._model_combo)

        self._model_list = QtWidgets.QListWidget()
        self._model_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        for preset in info.presets:
            self._add_model_row(preset)
        form.addRow(self._model_list)
        self._refresh_all_model_rows()

        self._compute_combo = QtWidgets.QComboBox()
        for ct, label in _COMPUTE_TYPES:
            self._compute_combo.addItem(label, ct)
        idx = self._compute_combo.findData(settings.compute_type)
        if idx != -1:
            self._compute_combo.setCurrentIndex(idx)
        form.addRow("Compute precision:", self._compute_combo)

        clear_btn = QtWidgets.QPushButton("Clear downloaded models cache")
        clear_btn.clicked.connect(lambda: self._on_clear_cache())
        form.addRow(clear_btn)

        layout.addWidget(group)

    def _build_hotkey_group(self, layout: QtWidgets.QVBoxLayout, settings: Settings) -> None:
        group = QtWidgets.QGroupBox("Shortcut")
        group_layout = QtWidgets.QVBoxLayout(group)

        row = QtWidgets.QHBoxLayout()
        self._hotkey_label = QtWidgets.QLabel(settings.hotkey)
        row.addWidget(self._hotkey_label, 1)
        self._record_btn = QtWidgets.QPushButton("Record Shortcut")
        self._record_btn.clicked.connect(self._open_recorder)
        row.addWidget(self._record_btn)
        self._reset_btn = QtWidgets.QPushButton("Reset to Default")
        self._reset_btn.clicked.connect(self._reset_hotkey_to_default)
        row.addWidget(self._reset_btn)
        group_layout.addLayout(row)

        hint = QtWidgets.QLabel(
            "Click Record Shortcut, then press the key combination you want "
            "to use. Press Esc to cancel."
        )
        hint.setWordWrap(True)
        group_layout.addWidget(hint)

        self._hotkey_feedback = QtWidgets.QLabel("")
        self._hotkey_feedback.setWordWrap(True)
        group_layout.addWidget(self._hotkey_feedback)

        layout.addWidget(group)

    def _build_text_app_group(self, layout: QtWidgets.QVBoxLayout, settings: Settings) -> None:
        group = QtWidgets.QGroupBox("Transcript text app")
        form = QtWidgets.QFormLayout(group)
        self._text_app_edit = QtWidgets.QLineEdit(settings.text_app_desktop_id)
        form.addRow("Text app id:", self._text_app_edit)
        hint = QtWidgets.QLabel(
            "The 'Open in Text App' button in the main window opens the "
            "current transcript with whatever application Windows "
            "associates with .txt files -- this field is kept only for "
            "settings-file parity with the Linux build and is not "
            "currently used to choose an app on Windows."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        layout.addWidget(group)

    def _build_vad_group(self, layout: QtWidgets.QVBoxLayout, settings: Settings) -> None:
        group = QtWidgets.QGroupBox("Advanced (VAD)")
        form = QtWidgets.QFormLayout(group)

        self._agg_spin = QtWidgets.QSpinBox()
        self._agg_spin.setRange(0, 3)
        self._agg_spin.setValue(settings.vad.aggressiveness)
        form.addRow("Aggressiveness (0..3):", self._agg_spin)

        self._min_speech_spin = QtWidgets.QSpinBox()
        self._min_speech_spin.setRange(50, 3000)
        self._min_speech_spin.setSingleStep(50)
        self._min_speech_spin.setValue(settings.vad.min_speech_ms)
        form.addRow("Min speech (ms):", self._min_speech_spin)

        self._silence_spin = QtWidgets.QSpinBox()
        self._silence_spin.setRange(100, 3000)
        self._silence_spin.setSingleStep(50)
        self._silence_spin.setValue(settings.vad.silence_ms)
        form.addRow("Trailing silence (ms):", self._silence_spin)

        self._max_seg_spin = QtWidgets.QSpinBox()
        self._max_seg_spin.setRange(1000, 30000)
        self._max_seg_spin.setSingleStep(500)
        self._max_seg_spin.setValue(settings.vad.max_segment_ms)
        form.addRow("Max segment (ms):", self._max_seg_spin)

        layout.addWidget(group)

    def _build_buttons(self, layout: QtWidgets.QVBoxLayout) -> None:
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.Ok
        )
        ok_btn = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---- model list (install status, install/delete) ----------------------

    def _add_model_row(self, preset: ModelPreset) -> None:
        item = QtWidgets.QListWidgetItem()
        row_widget = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(row_widget)
        row_layout.setContentsMargins(6, 4, 6, 4)

        title_label = QtWidgets.QLabel(preset.label)
        status_label = QtWidgets.QLabel("")
        status_label.setWordWrap(True)
        button = QtWidgets.QPushButton("")
        button.clicked.connect(
            lambda _checked=False, k=preset.key: self._on_model_row_button_clicked(k)
        )

        row_layout.addWidget(title_label, 2)
        row_layout.addWidget(status_label, 2)
        row_layout.addWidget(button, 0)

        item.setSizeHint(row_widget.sizeHint())
        self._model_list.addItem(item)
        self._model_list.setItemWidget(item, row_widget)
        self._model_row_widgets[preset.key] = {
            "status": status_label,
            "button": button,
        }

    def _refresh_all_model_rows(self) -> None:
        for key in self._model_row_widgets:
            self._refresh_model_row(key)

    def _refresh_model_row(self, key: str) -> None:
        row = self._model_row_widgets[key]
        preset = self._preset_by_key[key]
        installed = model_manager.is_installed(key)
        if installed:
            size = model_manager.installed_size_bytes(key)
            row["status"].setText(f"{model_manager.human_size(size)} installed")
            row["button"].setText("Delete")
            row["button"].setEnabled(key != self._active_model_key)
        else:
            row["status"].setText(
                f"~{preset.approx_download_mb} MB download -- not installed"
            )
            row["button"].setText("Download")
            row["button"].setEnabled(True)

    def _on_model_row_button_clicked(self, key: str) -> None:
        if model_manager.is_installed(key):
            self._on_delete_model_clicked(key)
        else:
            self._maybe_install_model(key)

    def _maybe_install_model(self, key: str) -> bool:
        """Confirm and kick off a download for ``key``. Returns whether it
        actually started (used by the combo-box handler to decide whether
        to revert its selection)."""
        preset = self._preset_by_key[key]
        proceed = self._confirm_yes_no(
            "Model not installed",
            f"'{preset.label}' is not installed yet "
            f"(~{preset.approx_download_mb} MB download).\n\n"
            "Install it now?",
        )
        if not proceed:
            return False
        self._start_model_install(key)
        return True

    def _on_delete_model_clicked(self, key: str) -> None:
        preset = self._preset_by_key[key]
        if key == self._active_model_key:
            self._show_error(
                f"'{preset.label}' is currently selected -- switch to a "
                "different model first, then delete this one."
            )
            return
        size = model_manager.installed_size_bytes(key)
        proceed = self._confirm_yes_no(
            "Delete model",
            f"Delete '{preset.label}'? This frees "
            f"{model_manager.human_size(size)}.\n\n"
            "You can re-download it later by selecting it again.",
        )
        if not proceed:
            return
        try:
            model_manager.delete_model(key)
        except Exception as e:  # noqa: BLE001 - surfaced to the user below
            self._show_error(f"Failed to delete model: {e}")
            return
        self._refresh_all_model_rows()

    def _start_model_install(self, key: str) -> None:
        row = self._model_row_widgets[key]
        row["button"].setEnabled(False)
        row["status"].setText("Downloading...")

        def worker() -> None:
            def on_progress(done: int, total: int) -> None:
                self._install_signals.progress.emit(key, done, total)

            try:
                model_manager.install_model(key, on_progress=on_progress)
            except Exception as e:  # noqa: BLE001 - surfaced to the user below
                self._install_signals.finished.emit(key, str(e))
            else:
                self._install_signals.finished.emit(key, None)

        threading.Thread(target=worker, daemon=True).start()

    def _on_install_progress(self, key: str, done: int, total: int) -> None:
        row = self._model_row_widgets.get(key)
        if row is None:
            return
        if total > 0:
            pct = min(100, int(done * 100 / total))
            row["status"].setText(f"Downloading... {pct}%")
        else:
            row["status"].setText("Downloading...")

    def _on_install_finished(self, key: str, error: Optional[str]) -> None:
        row = self._model_row_widgets.get(key)
        if row is not None:
            row["button"].setEnabled(True)
        if error:
            self._show_error(f"Failed to install model: {error}")
            self._select_model_in_combo(self._active_model_key)
        else:
            self._active_model_key = key
            self._select_model_in_combo(key)
        self._refresh_all_model_rows()

    # ---- model combo (active-model selection) ------------------------------

    def _select_model_in_combo(self, key: str) -> None:
        idx = self._model_combo.findData(key)
        if idx == -1:
            return
        self._suppress_model_combo_signal = True
        try:
            self._model_combo.setCurrentIndex(idx)
        finally:
            self._suppress_model_combo_signal = False

    def _on_model_combo_changed(self, index: int) -> None:
        if self._suppress_model_combo_signal:
            return
        key = self._model_combo.itemData(index)
        if key is None or key == self._active_model_key:
            return
        if model_manager.is_installed(key):
            self._active_model_key = key
            self._refresh_all_model_rows()
            return
        if not self._maybe_install_model(key):
            self._select_model_in_combo(self._active_model_key)

    # ---- confirm / error dialogs (thin wrappers so tests can stub them) ---

    def _confirm_yes_no(self, title: str, message: str) -> bool:
        result = QtWidgets.QMessageBox.question(
            self,
            title,
            message,
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
        )
        return result == QtWidgets.QMessageBox.StandardButton.Yes

    def _show_error(self, msg: str) -> None:
        QtWidgets.QMessageBox.critical(self, "MyVoice Settings", msg)

    # ---- hotkey record / reset ---------------------------------------------

    def _open_recorder(self) -> None:
        dlg = ShortcutRecorder(self)
        result = dlg.exec()
        if result != QtWidgets.QDialog.DialogCode.Accepted or dlg.captured_accel is None:
            self._set_hotkey_feedback("Recording cancelled.", ok=False)
            return
        self._try_assign_hotkey(dlg.captured_accel)

    def _reset_hotkey_to_default(self) -> None:
        self._try_assign_hotkey(Settings().hotkey, is_reset=True)

    def _try_assign_hotkey(self, new_accel: str, is_reset: bool = False) -> None:
        """Attempt to make ``new_accel`` the active hotkey.

        Uses the ``on_try_hotkey`` callback if provided so the app can
        perform the real ``RegisterHotKey`` call and roll back on conflict.
        If no callback is provided (e.g. the dialog is used standalone),
        the assignment is treated as successful and simply updates the
        label.
        """
        try:
            parse_accel(new_accel)
        except ValueError as e:
            self._set_hotkey_feedback(f"Invalid shortcut: {e}", ok=False)
            return

        if self._on_try_hotkey is not None:
            err = self._on_try_hotkey(new_accel)
            if err is not None:
                self._set_hotkey_feedback(
                    f"That shortcut is already in use ({err}). Please "
                    "choose another combination -- your previous shortcut "
                    "is still active.",
                    ok=False,
                )
                return

        self._current_hotkey_accel = new_accel
        self._hotkey_label.setText(new_accel)
        if is_reset:
            self._set_hotkey_feedback(f"Reset to default: {new_accel}.", ok=True)
        else:
            self._set_hotkey_feedback(f"Shortcut set to {new_accel}.", ok=True)

    def _set_hotkey_feedback(self, msg: str, ok: bool) -> None:
        color = "green" if ok else "#e74c3c"
        self._hotkey_feedback.setStyleSheet(f"color: {color};")
        self._hotkey_feedback.setText(msg)

    # ---- collect ------------------------------------------------------------

    def collect(self) -> Optional[Settings]:
        """Read the UI back into a ``Settings``. Hotkey is always validated
        against ``parse_accel`` first; returns ``None`` (after showing an
        error) if it somehow doesn't parse."""
        hotkey = self._current_hotkey_accel
        try:
            parse_accel(hotkey)
        except Exception as e:
            self._show_error(f"Invalid hotkey: {e}")
            return None

        return Settings(
            schema_version=self._settings.schema_version,
            language_mode=self._lang_combo.currentData() or "auto",
            hotkey=hotkey,
            microphone=self._settings.microphone,  # unchanged here; handled elsewhere
            start_minimized=self._start_min_check.isChecked(),
            autostart=self._autostart_check.isChecked(),
            engine=self._settings.engine,
            model=self._active_model_key,
            compute_type=self._compute_combo.currentData() or "auto",
            text_app_desktop_id=self._text_app_edit.text(),
            terminal_wm_classes=list(self._settings.terminal_wm_classes),
            vad=VadSettings(
                aggressiveness=self._agg_spin.value(),
                min_speech_ms=self._min_speech_spin.value(),
                silence_ms=self._silence_spin.value(),
                max_segment_ms=self._max_seg_spin.value(),
            ),
        )
