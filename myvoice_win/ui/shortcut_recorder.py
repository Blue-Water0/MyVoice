"""Record-a-shortcut dialog (PySide6 / Qt).

Mirrors the *capture behavior* of the Linux ``myvoice/ui/shortcut_recorder.py``
+ its ``RecordShortcutDialog`` (defined in ``myvoice/ui/settings_dialog.py``)
combined into a single Qt widget -- not their GTK/Gdk-specific code. On
Linux the pure state machine and the GTK modal dialog that drives it are
two separate classes; here there is no ``Gdk.keyval``/state-bitmask
translation layer to keep UI-independent, so a single small ``QDialog``
that overrides ``keyPressEvent`` plays both roles.

Accelerator format matches what ``myvoice_win.services.hotkey_service.
parse_accel`` expects: ``+``-joined, lowercase tokens such as
``"win+shift+space"`` or ``"ctrl+alt+d"`` -- modifier tokens ``ctrl``,
``alt``, ``shift``, ``win`` (Qt maps the physical Windows/Super key to
``Qt.KeyboardModifier.MetaModifier`` on Windows) followed by exactly one
non-modifier key token.

A key press that is *only* a modifier (e.g. holding Ctrl with nothing else
pressed yet) never produces a token -- see ``build_accel`` -- so the
recorder simply keeps waiting for a real key, matching the Linux
recorder's "modifier-only press is a no-op" behaviour.
"""
from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

# Non-modifier keys we can express as an accel token, matching the token
# spellings ``myvoice_win.services.hotkey_service.parse_accel`` accepts via
# its ``_VK_MAP``. Any key not in this table (arrows, function keys, bare
# modifiers, etc.) makes ``build_accel`` return None.
_KEY_TOKENS: dict[int, str] = {
    int(QtCore.Qt.Key.Key_Space): "space",
    int(QtCore.Qt.Key.Key_Apostrophe): "'",
    int(QtCore.Qt.Key.Key_Period): ".",
}
for _i in range(10):
    _KEY_TOKENS[int(QtCore.Qt.Key.Key_0) + _i] = str(_i)
for _i in range(26):
    _KEY_TOKENS[int(QtCore.Qt.Key.Key_A) + _i] = chr(ord("a") + _i)
del _i


def build_accel(event: QtGui.QKeyEvent) -> Optional[str]:
    """Build a ``parse_accel``-compatible accel string from a key event.

    Returns ``None`` when ``event.key()`` isn't a representable
    non-modifier key -- this is what makes a modifier-only press (or an
    unsupported key like an arrow/function key) produce no accelerator at
    all, rather than an incomplete string like ``"ctrl"``.
    """
    token = _KEY_TOKENS.get(int(event.key()))
    if token is None:
        return None

    # Canonical modifier order: win, ctrl, alt, shift -- matches the
    # convention used elsewhere in this codebase (e.g. the default hotkey
    # "win+shift+space", "ctrl+alt+d") so the built string is stable and
    # human-readable, though ``parse_accel`` itself accepts any order.
    mods = event.modifiers()
    parts: list[str] = []
    if mods & QtCore.Qt.KeyboardModifier.MetaModifier:
        parts.append("win")
    if mods & QtCore.Qt.KeyboardModifier.ControlModifier:
        parts.append("ctrl")
    if mods & QtCore.Qt.KeyboardModifier.AltModifier:
        parts.append("alt")
    if mods & QtCore.Qt.KeyboardModifier.ShiftModifier:
        parts.append("shift")
    parts.append(token)
    return "+".join(parts)


class ShortcutRecorder(QtWidgets.QDialog):
    """Modal dialog that captures the next key combination the user presses.

    ``captured_accel`` holds the resulting accel string once the dialog is
    accepted (``exec()`` returns ``QDialog.DialogCode.Accepted``); it stays
    ``None`` if the user cancels (Escape, or the Cancel button).
    """

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Record Shortcut")
        self.setModal(True)
        self.captured_accel: Optional[str] = None

        layout = QtWidgets.QVBoxLayout(self)
        self._prompt = QtWidgets.QLabel("Press your shortcut now…\n(Esc to cancel)")
        self._prompt.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._prompt.setWordWrap(True)
        layout.addWidget(self._prompt)

        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(cancel_btn)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802 (Qt override)
        if event.key() == int(QtCore.Qt.Key.Key_Escape):
            self.captured_accel = None
            self.reject()
            return

        accel = build_accel(event)
        if accel is None:
            # Modifier-only or unsupported key -- swallow it and keep
            # waiting for a real key, rather than falling through to the
            # base class (which could e.g. treat Tab as focus-navigation).
            event.accept()
            return

        self.captured_accel = accel
        self.accept()
