"""Main application window (PySide6 / Qt).

Layout: header with Settings button; status label; big Start/Stop button;
language dropdown; microphone dropdown; live transcript preview with
Copy/"Open in Text App" actions; footer with privacy note and Quit button.

This mirrors the *feature set* and callback surface of the Linux GTK
``myvoice/ui/main_window.py`` -- not its widget code, which is GTK-specific.

Close-to-tray ownership
------------------------
This class deliberately does **not** override ``closeEvent``. On Linux,
``myvoice/ui/main_window.py`` doesn't override ``delete-event`` either --
``myvoice/app.py`` connects to it *externally*
(``self._window.connect("delete-event", self._on_close_hide_to_tray)``)
so the window class itself stays a plain, reusable widget and the
app-level controller owns the "hide instead of quit" policy.

The Qt equivalent of an external ``delete-event`` connection is an
``QObject`` event filter installed from outside the class (rather than a
subclass override baked into ``MainWindow``): the future
``myvoice_win/app.py`` (Task 12) should call
``main_window.installEventFilter(controller)`` and, in the controller's
``eventFilter(obj, event)``, intercept ``QEvent.Type.Close``, call
``main_window.hide()``, and return ``True`` to stop propagation --
functionally identical to the GTK ``return True`` that stops the
``delete-event`` signal from destroying the window. Keeping this out of
``MainWindow`` means plain ``.close()`` still works as expected in tests
and in any other embedding that doesn't want tray behavior.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6 import QtWidgets

from .. import APP_NAME
from myvoice.services.language_service import LANGUAGE_LABELS

# Mirrors the Linux STATUS_CLASSES keys (idle/listening/processing/ready/
# error) -- here as inline stylesheet colors applied to the status label
# instead of GTK CSS classes.
STATUS_STYLES = {
    "idle": "color: palette(mid);",
    "listening": "color: #2ecc71; font-weight: bold;",
    "processing": "color: #f39c12;",
    "ready": "color: #2ecc71;",
    "error": "color: #e74c3c; font-weight: bold;",
}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_quit: Callable[[], None],
        on_language_changed: Callable[[str], None],
        on_mic_changed: Callable[[Optional[str]], None],
        get_input_devices: Callable[[], list[dict]],
        initial_language_mode: str,
        initial_mic: Optional[str],
        on_copy_transcript: Optional[Callable[[], None]] = None,
        on_open_transcript_in_text_app: Optional[Callable[[], None]] = None,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.resize(560, 460)

        self._on_toggle = on_toggle
        self._on_open_settings = on_open_settings
        self._on_quit = on_quit
        self._on_language_changed = on_language_changed
        self._on_mic_changed = on_mic_changed
        self._get_input_devices = get_input_devices
        self._on_copy_transcript = on_copy_transcript
        self._on_open_transcript_in_text_app = on_open_transcript_in_text_app

        self._listening = False

        self._build_ui(initial_language_mode, initial_mic)
        self._apply_status("idle", "Idle")
        # Start disabled -- no transcript yet.
        self.set_transcript_actions_enabled(False)

    # ---- construction ---------------------------------------------------

    def _build_ui(self, lang_mode: str, mic_name: Optional[str]) -> None:
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        # Header
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(APP_NAME)
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 6)
        title_font.setBold(True)
        title.setFont(title_font)
        header.addWidget(title, 1)

        self._settings_btn = QtWidgets.QPushButton("Settings")
        self._settings_btn.setToolTip("Settings")
        self._settings_btn.clicked.connect(lambda: self._on_open_settings())
        header.addWidget(self._settings_btn, 0)
        outer.addLayout(header)

        # Status
        self._status_label = QtWidgets.QLabel("Idle")
        outer.addWidget(self._status_label)

        # Big toggle button.
        self._toggle_btn = QtWidgets.QPushButton("Start Listening")
        self._toggle_btn.setMinimumHeight(48)
        self._toggle_btn.clicked.connect(lambda: self._on_toggle())
        outer.addWidget(self._toggle_btn)

        # Language row
        lang_row = QtWidgets.QHBoxLayout()
        lang_row.addWidget(QtWidgets.QLabel("Language:"))
        self._lang_combo = QtWidgets.QComboBox()
        for code, label in LANGUAGE_LABELS.items():
            self._lang_combo.addItem(label, code)
        idx = self._lang_combo.findData(lang_mode)
        if idx != -1:
            self._lang_combo.setCurrentIndex(idx)
        self._lang_combo.currentIndexChanged.connect(self._on_lang_combo_changed)
        lang_row.addWidget(self._lang_combo, 1)
        outer.addLayout(lang_row)

        # Mic row
        mic_row = QtWidgets.QHBoxLayout()
        mic_row.addWidget(QtWidgets.QLabel("Microphone:"))
        self._mic_combo = QtWidgets.QComboBox()
        self._refresh_mic_combo(mic_name)
        self._mic_combo.currentIndexChanged.connect(self._on_mic_combo_changed)
        mic_row.addWidget(self._mic_combo, 1)
        self._mic_refresh_btn = QtWidgets.QPushButton("Rescan")
        self._mic_refresh_btn.setToolTip("Rescan microphones")
        self._mic_refresh_btn.clicked.connect(
            lambda: self._refresh_mic_combo(self._current_mic_id())
        )
        mic_row.addWidget(self._mic_refresh_btn, 0)
        outer.addLayout(mic_row)

        # Transcript header row: label + right-aligned Copy/Open actions.
        transcript_header = QtWidgets.QHBoxLayout()
        transcript_header.addWidget(QtWidgets.QLabel("Live transcript preview:"), 1)
        self._copy_transcript_btn = QtWidgets.QPushButton("Copy")
        self._copy_transcript_btn.setToolTip("Copy transcript to clipboard")
        self._copy_transcript_btn.clicked.connect(self._handle_copy_transcript)
        transcript_header.addWidget(self._copy_transcript_btn, 0)

        self._open_transcript_btn = QtWidgets.QPushButton("Open in Text App")
        self._open_transcript_btn.setToolTip("Open full transcript in text app")
        self._open_transcript_btn.clicked.connect(self._handle_open_transcript)
        transcript_header.addWidget(self._open_transcript_btn, 0)
        outer.addLayout(transcript_header)

        self._transcript_view = QtWidgets.QTextEdit()
        self._transcript_view.setReadOnly(True)
        self._transcript_view.setMinimumHeight(140)
        outer.addWidget(self._transcript_view, 1)

        # Footer
        footer = QtWidgets.QHBoxLayout()
        privacy = QtWidgets.QLabel("Local processing -- no audio leaves your computer.")
        footer.addWidget(privacy, 1)
        self._quit_btn = QtWidgets.QPushButton("Quit")
        self._quit_btn.clicked.connect(lambda: self._on_quit())
        footer.addWidget(self._quit_btn, 0)
        outer.addLayout(footer)

    # ---- public API used by controller ----------------------------------

    def set_listening(self, listening: bool) -> None:
        self._listening = listening
        self._toggle_btn.setText("Stop Listening" if listening else "Start Listening")

    def set_status(self, status: str, message: str) -> None:
        self._apply_status(status, message)

    def append_transcript(self, chunk: str) -> None:
        if not chunk:
            return
        is_empty = self._transcript_view.document().isEmpty()
        text = chunk if chunk.startswith((" ", "\n")) or is_empty else " " + chunk
        cursor = self._transcript_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self._transcript_view.setTextCursor(cursor)
        self._transcript_view.ensureCursorVisible()

    def clear_transcript(self) -> None:
        self._transcript_view.clear()
        self.set_transcript_actions_enabled(False)

    def set_transcript_actions_enabled(self, enabled: bool) -> None:
        """Enable or disable the Copy / Open transcript buttons.

        The controller calls this with ``True`` as soon as any real
        transcript text has been accumulated and ``False`` when the
        transcript is cleared for a new session.
        """
        self._copy_transcript_btn.setEnabled(enabled)
        self._open_transcript_btn.setEnabled(enabled)

    def set_language_mode(self, mode: str) -> None:
        idx = self._lang_combo.findData(mode)
        if idx != -1 and self._lang_combo.currentIndex() != idx:
            self._lang_combo.blockSignals(True)
            self._lang_combo.setCurrentIndex(idx)
            self._lang_combo.blockSignals(False)

    def set_microphone(self, name: Optional[str]) -> None:
        self._refresh_mic_combo(name)

    # ---- helpers ----------------------------------------------------------

    def _apply_status(self, key: str, message: str) -> None:
        self._status_label.setStyleSheet(STATUS_STYLES.get(key, STATUS_STYLES["idle"]))
        self._status_label.setText(message)

    def _handle_copy_transcript(self) -> None:
        if self._on_copy_transcript is not None:
            self._on_copy_transcript()

    def _handle_open_transcript(self) -> None:
        if self._on_open_transcript_in_text_app is not None:
            self._on_open_transcript_in_text_app()

    def _current_mic_id(self) -> Optional[str]:
        return self._mic_combo.currentData()

    def _refresh_mic_combo(self, selected_name: Optional[str]) -> None:
        self._mic_combo.blockSignals(True)
        try:
            self._mic_combo.clear()
            self._mic_combo.addItem("System default", None)
            try:
                devices = self._get_input_devices()
            except Exception:
                devices = []
            for d in devices:
                label = d["name"] + ("  (default)" if d.get("default") else "")
                self._mic_combo.addItem(label, d["name"])
            if selected_name:
                idx = self._mic_combo.findData(selected_name)
                self._mic_combo.setCurrentIndex(idx if idx != -1 else 0)
            else:
                self._mic_combo.setCurrentIndex(0)
        finally:
            self._mic_combo.blockSignals(False)

    def _on_mic_combo_changed(self, _index: int) -> None:
        self._on_mic_changed(self._current_mic_id())

    def _on_lang_combo_changed(self, _index: int) -> None:
        code = self._lang_combo.currentData()
        if code is not None:
            self._on_language_changed(code)
