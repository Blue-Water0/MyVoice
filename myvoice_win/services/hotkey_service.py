"""Global hotkey service for Windows.

Uses the Win32 ``RegisterHotKey``/``UnregisterHotKey`` APIs (``user32.dll``)
via ``ctypes.windll``. Once registered, Windows delivers a ``WM_HOTKEY``
message to the thread's message queue whenever the combination is pressed;
Qt's event loop hands every native Windows message to any installed
``QAbstractNativeEventFilter``, so ``_NativeEventFilter`` below is how this
service actually learns the hotkey fired.

Accelerator format: ``+``-joined, case-insensitive tokens, e.g.
``"win+shift+space"``, ``"ctrl+alt+d"``. Modifier tokens: ``ctrl``, ``alt``,
``shift``, ``win``. Exactly one non-modifier key token is required.

Import-safety: ``ctypes.windll`` does not exist on non-Windows platforms
(referencing it at import time raises ``AttributeError``), so every
reference to it lives inside a method body, never at module level. This
module must remain importable on Linux so pytest can collect its tests.
"""
from __future__ import annotations

import ctypes.wintypes as wintypes
import itertools
import logging
from typing import Callable, Optional

from PySide6.QtCore import QAbstractNativeEventFilter

from ._win32util import set_signature as _set_signature

log = logging.getLogger(__name__)

# ---- Win32 constants (see Win32 API docs for RegisterHotKey/WM_HOTKEY) ----

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

VK_SPACE = 0x20
VK_OEM_7 = 0xDE        # apostrophe/quote key ( '"' on US layout )
VK_OEM_PERIOD = 0xBE   # '.' key

_MOD_MAP: dict[str, int] = {
    "ctrl": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}

# Virtual-key code lookup for the non-modifier token. A-Z and 0-9 map onto
# their ASCII codes per the Win32 VK code convention (VK_A..VK_Z == 'A'..'Z',
# VK_0..VK_9 == '0'..'9'); everything else needs an explicit VK_* entry.
_VK_MAP: dict[str, int] = {
    "space": VK_SPACE,
    "'": VK_OEM_7,
    ".": VK_OEM_PERIOD,
}
for _digit in "0123456789":
    _VK_MAP[_digit] = ord(_digit)
for _letter in "abcdefghijklmnopqrstuvwxyz":
    _VK_MAP[_letter] = ord(_letter.upper())
del _digit, _letter


def parse_accel(accel: str) -> tuple[int, int]:
    """Parse an accelerator string into ``(modifiers, vk)``.

    ``accel`` is a ``+``-joined, case-insensitive string such as
    ``"win+shift+space"``. Raises ``ValueError`` if the string is empty,
    contains an unknown modifier/key name, contains no non-modifier key, or
    contains more than one non-modifier key.
    """
    if not accel or not accel.strip():
        raise ValueError("empty accelerator")

    parts = [p.strip().lower() for p in accel.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty accelerator: {accel!r}")

    modifiers = 0
    vk: Optional[int] = None
    for part in parts:
        if part in _MOD_MAP:
            modifiers |= _MOD_MAP[part]
        elif part in _VK_MAP:
            if vk is not None:
                raise ValueError(
                    f"multiple non-modifier keys in accelerator: {accel!r}"
                )
            vk = _VK_MAP[part]
        else:
            raise ValueError(f"unknown key or modifier {part!r} in {accel!r}")

    if vk is None:
        raise ValueError(f"no non-modifier key in accelerator: {accel!r}")

    return modifiers, vk


class HotkeyError(Exception):
    pass


# Monotonically-increasing hotkey ids, unique for the lifetime of the
# process, so a stray WM_HOTKEY for a just-unregistered id can never be
# mistaken for the newly-registered one.
_id_counter = itertools.count(1)


class HotkeyService:
    """Windows global hotkey via ``RegisterHotKey``/``UnregisterHotKey``."""

    def __init__(self) -> None:
        self._hotkey_id: Optional[int] = None
        self._accel: Optional[str] = None
        self._callback: Optional[Callable[[], None]] = None
        # Exposed so the app can `installNativeEventFilter` it on the
        # QApplication/QAbstractEventDispatcher once the app is wired up.
        self.event_filter = _NativeEventFilter(self)

    # --- public API -----------------------------------------------------

    def register(self, accel: str, callback: Callable[[], None]) -> None:
        """Register ``accel`` globally; raises ``HotkeyError`` on failure."""
        modifiers, vk = parse_accel(accel)

        import ctypes  # local: ctypes.windll doesn't exist on Linux

        hotkey_id = next(_id_counter)
        register = ctypes.windll.user32.RegisterHotKey
        _set_signature(
            register,
            restype=wintypes.BOOL,
            argtypes=[wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT],
        )
        ok = register(None, hotkey_id, modifiers, vk)
        if not ok:
            raise HotkeyError(
                f"Failed to register hotkey {accel!r} "
                f"(RegisterHotKey returned 0; it may be reserved by "
                f"another application)."
            )

        self._hotkey_id = hotkey_id
        self._accel = accel
        self._callback = callback
        log.info("Hotkey registered: %s (id=%d)", accel, hotkey_id)

    def unregister(self) -> None:
        """Unregister the current binding. Idempotent."""
        if self._hotkey_id is None:
            return

        import ctypes  # local: ctypes.windll doesn't exist on Linux

        unregister = ctypes.windll.user32.UnregisterHotKey
        _set_signature(
            unregister, restype=wintypes.BOOL, argtypes=[wintypes.HWND, ctypes.c_int],
        )
        try:
            unregister(None, self._hotkey_id)
        except Exception:
            log.exception("Error unregistering hotkey id=%s", self._hotkey_id)

        self._hotkey_id = None
        self._accel = None
        self._callback = None
        log.info("Hotkey unregistered")

    def rebind(self, new_accel: str, callback: Callable[[], None]) -> None:
        """Atomically replace the current hotkey with ``new_accel``.

        If registering the new accelerator fails, the previous binding (if
        any) is re-registered so the app still has a working shortcut.
        Raises ``HotkeyError`` on any failure; callers can display the
        message and know the previous binding is still active.
        """
        # Validate before touching anything so a bad string never tears
        # down a working binding.
        parse_accel(new_accel)

        prev_accel = self._accel
        prev_callback = self._callback

        if prev_accel == new_accel and prev_callback is callback:
            return

        self.unregister()

        try:
            self.register(new_accel, callback)
        except HotkeyError as e:
            if prev_accel is not None and prev_callback is not None:
                try:
                    self.register(prev_accel, prev_callback)
                    log.warning(
                        "New hotkey %r rejected (%s); restored previous %r",
                        new_accel, e, prev_accel,
                    )
                except HotkeyError:
                    log.exception(
                        "Failed to restore previous hotkey %r after "
                        "rebind failure",
                        prev_accel,
                    )
            raise


class _NativeEventFilter(QAbstractNativeEventFilter):
    """Watches raw Windows messages for WM_HOTKEY and dispatches callbacks."""

    def __init__(self, service: HotkeyService) -> None:
        super().__init__()
        self._service = service

    def nativeEventFilter(self, eventType, message):  # noqa: N802 (Qt override)
        if eventType not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0

        msg = wintypes.MSG.from_address(int(message))
        if msg.message != WM_HOTKEY:
            return False, 0

        hotkey_id = msg.wParam
        callback = self._service._callback
        if hotkey_id == self._service._hotkey_id and callback is not None:
            try:
                callback()
            except Exception:
                log.exception("Hotkey callback raised")
            return True, 0

        return False, 0
