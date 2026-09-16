"""Win32 clipboard write/restore + SendInput paste backend for Windows.

Implements design doc §2 ("Text Insertion Safety") steps 3-5 of the
per-chunk insertion lifecycle, plus the "Clipboard Ownership and Restore
Rules" subsection: write chunk text to the clipboard as CF_UNICODETEXT,
re-verify focus immediately before sending the paste shortcut via
SendInput, and (at session end) restore the pre-session clipboard only if
nothing else has touched it since MyVoice's last write.

Steps 1-2 of the per-chunk lifecycle (capture the target, reject unsafe
targets -- password fields, own windows, higher-integrity targets) are NOT
this module's job; they belong to ``text_injection_service.py`` (a later
task). This module receives an already-approved ``hwnd`` and only does the
clipboard write + paste-shortcut send + the immediate pre-paste focus
re-check.

Sequence-number bookkeeping (see "Clipboard Ownership and Restore Rules"):
Windows increments a process-wide clipboard sequence number
(``GetClipboardSequenceNumber``) every time the clipboard's content
changes, regardless of who changed it. ``begin_session`` records that
number as the starting baseline for "the last write MyVoice is
responsible for." Every *session-owned* write (``write_text``, used
per-chunk by ``paste_into``) advances that baseline to the number produced
by the write. ``end_session`` restores the session-start text only if the
clipboard's current sequence number still equals that baseline -- i.e.
nobody (not the user, not another app) has written to the clipboard since
MyVoice's last write. If it differs, something else now owns the
clipboard's contents and MyVoice must never clobber it.

``write_user_text`` is different: it is an *explicit, user-initiated*
clipboard write (e.g. a "Copy transcript" button) and deliberately does
NOT update the session-owned baseline above. The underlying Windows
sequence number still advances (the OS doesn't know or care who wrote to
it), so if ``end_session`` runs afterward, the current number no longer
matches MyVoice's last *session-owned* write, and the restore decision
correctly comes out "don't restore" -- the user's explicit write takes
priority. This mirrors the Linux backend's ``write_user_text``/override
semantics without needing a separate override flag.

Import-safety: ``ctypes.windll`` does not exist on non-Windows platforms
(referencing it raises ``AttributeError``). This module imports plain
``ctypes``/``ctypes.wintypes`` at the top level -- that's safe everywhere,
it's only the ``.windll`` *attribute* that is Windows-only -- and every
reference to ``ctypes.windll.*`` lives inside a method body, evaluated at
call time, never at module import time. The ``INPUT``/``KEYBDINPUT``/
``MOUSEINPUT``/``HARDWAREINPUT`` ctypes ``Structure``/``Union``
definitions below use only ``ctypes.Structure``/``ctypes.Union``/
``ctypes.wintypes.*`` types, which are plain, cross-platform type
definitions with no DLL loading involved, so defining them at module level
is safe and keeps this module importable on Linux for pytest collection.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional

from myvoice.services.app_classifier import AppClass

from ._win32util import set_signature as _set_signature

log = logging.getLogger(__name__)

# ---- Win32 constants --------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_V = 0x56  # same as ord("V"); VK_A..VK_Z == 'A'..'Z' per the Win32 VK convention

# Safety cap for reading a NUL-terminated clipboard string (see
# ``_read_utf16le_cstring``): generous for any realistic dictation
# transcript, but bounds the scan instead of looping forever against
# corrupted/non-terminated memory.
_MAX_CLIPBOARD_TEXT_UNITS = 1 << 20  # ~1M UTF-16 code units


# ---- SendInput structures ---------------------------------------------------
#
# Mirrors the documented Win32 INPUT/KEYBDINPUT/MOUSEINPUT/HARDWAREINPUT
# layout (MSDN "INPUT structure" / "KEYBDINPUT structure"). MOUSEINPUT and
# HARDWAREINPUT are never populated by this module (only keyboard events
# are sent) but they MUST still be declared as union members: ctypes
# computes a Union's size/alignment purely from its declared fields, so a
# union containing only KEYBDINPUT would be smaller (24 bytes on x64) than
# the real Win32 INPUT union (32 bytes on x64, sized by the larger
# MOUSEINPUT member, whose trailing ULONG_PTR needs 8-byte alignment).
# SendInput validates that the caller's ``cbSize`` argument equals the
# *real* ``sizeof(INPUT)`` and fails outright if it doesn't match -- so
# this union's member list is load-bearing, not cosmetic.
#
# ULONG_PTR (the true type of each struct's ``dwExtraInfo``) is a
# pointer-sized integer. ``wintypes.WPARAM`` is CPython's ready-made
# pointer-sized-integer alias (``c_ulong`` or ``c_ulonglong``, whichever
# matches ``sizeof(c_void_p)`` on the current platform/build), so it's the
# correct stand-in without hand-rolling one -- and, importantly, ctypes
# then automatically computes the correct alignment padding for the union
# and the enclosing INPUT struct (matching the real Win32 layout) purely
# from this field's declared size, on both 32- and 64-bit builds.

ULONG_PTR = wintypes.WPARAM


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _InputUnion(ctypes.Union):
    _fields_ = (
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    )


class INPUT(ctypes.Structure):
    _fields_ = (
        ("type", wintypes.DWORD),
        ("union", _InputUnion),
    )


def _make_key_input(vk: int, key_up: bool) -> INPUT:
    """Build one keyboard ``INPUT`` event for virtual-key code ``vk``."""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = vk
    inp.union.ki.wScan = 0
    inp.union.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
    inp.union.ki.time = 0
    inp.union.ki.dwExtraInfo = 0
    return inp


def _read_utf16le_cstring(ptr: int) -> str:
    """Read a NUL-terminated UTF-16LE string starting at raw address ``ptr``.

    Deliberately does NOT use ``ctypes.wstring_at``: that function reads
    "wide chars" sized by the *current platform's* ``wchar_t`` -- 2 bytes
    (UTF-16) on real Windows, which happens to match CF_UNICODETEXT's
    in-memory format exactly, but 4 bytes (UCS-4) on Linux, where this
    exact function also executes during this project's tests (against a
    fake clipboard, but through real, unmocked ctypes memory reads) -- on
    Linux, ``wstring_at`` would misinterpret a genuinely-2-byte-per-unit
    buffer. Scanning 2 raw bytes at a time sidesteps the platform's
    ``wchar_t`` size entirely, so the decode is identically correct on
    both platforms.
    """
    raw = bytearray()
    for _ in range(_MAX_CLIPBOARD_TEXT_UNITS):
        unit = ctypes.string_at(ptr, 2)
        if unit == b"\x00\x00":
            return bytes(raw).decode("utf-16-le")
        raw += unit
        ptr += 2
    raise OSError(
        "clipboard text exceeded the maximum supported length "
        "(missing NUL terminator or corrupted memory)"
    )


class ClipboardBackend:
    """Win32 clipboard write/restore + SendInput paste for Windows.

    Mirrors the *session-lifecycle contract* of
    ``myvoice.services.clipboard_backend.ClipboardBackend``
    (``begin_session``/``end_session``/``write_user_text``) -- the
    mechanism is completely different (Win32 clipboard API + SendInput,
    not xclip/XTEST).
    """

    def __init__(self) -> None:
        self._session_active = False
        self._saved_text: Optional[str] = None
        # Clipboard sequence number MyVoice is responsible for -- see the
        # module docstring. Set at begin_session to the number *before*
        # any of our writes, then advanced by every write_text call.
        self._last_written_seq: Optional[int] = None

    # ---- session lifecycle --------------------------------------------------

    def begin_session(self) -> None:
        """Snapshot the user's current clipboard text. Idempotent."""
        if self._session_active:
            return
        self._saved_text = self._read_clipboard_text()
        self._last_written_seq = self._get_sequence_number()
        self._session_active = True
        log.debug(
            "Clipboard session began (saved %d chars, seq=%s)",
            len(self._saved_text or ""), self._last_written_seq,
        )

    def end_session(self, missed_chunks_present: bool) -> None:
        """Restore the session-start clipboard, subject to the
        sequence-number rule. Safe to call even if never begun.

        Per design doc §2 "Mixed-Success Sessions": if
        ``missed_chunks_present`` is True, the caller has already written
        the fallback full-session transcript via ``write_text`` and that
        takes priority -- this method must NOT restore over it in that
        case. Otherwise, restore the session-start text only if the
        clipboard's sequence number is still exactly equal to the number
        recorded after MyVoice's last write (nobody else has written to
        the clipboard since); if it differs, leave the clipboard alone.
        """
        if not self._session_active:
            return

        if missed_chunks_present:
            log.debug("end_session: missed_chunks_present -- not restoring")
        elif self._saved_text is not None:
            current_seq = self._get_sequence_number()
            if current_seq == self._last_written_seq:
                try:
                    self._set_clipboard_text(self._saved_text)
                    log.debug("Clipboard session ended and restored")
                except OSError:
                    log.exception("end_session: clipboard restore failed")
            else:
                log.debug(
                    "end_session: clipboard sequence number changed "
                    "(%s -> %s) since our last write -- not restoring",
                    self._last_written_seq, current_seq,
                )

        self._session_active = False
        self._saved_text = None
        self._last_written_seq = None

    # ---- writes ---------------------------------------------------------

    def write_text(self, text: str) -> None:
        """Write ``text`` to the clipboard as a *session-owned* write.

        Advances the sequence-number baseline that ``end_session``
        compares against. Raises ``OSError`` if the underlying Win32
        calls fail.
        """
        self._set_clipboard_text(text)
        self._last_written_seq = self._get_sequence_number()

    def write_user_text(self, text: str) -> bool:
        """Explicit user-initiated clipboard write (e.g. "Copy transcript").

        Same underlying write as ``write_text`` but returns success/
        failure instead of raising, and deliberately does NOT advance the
        session-owned sequence-number baseline -- see the module
        docstring for why that omission is exactly what makes
        ``end_session`` correctly decline to restore over it afterward.
        """
        try:
            self._set_clipboard_text(text)
        except OSError:
            log.exception("write_user_text: clipboard write failed")
            return False
        return True

    # ---- paste ------------------------------------------------------------

    def paste_into(self, hwnd: int, text: str, app_class: AppClass) -> bool:
        """Design doc §2 per-chunk insertion lifecycle, steps 3-5 only.

        ``hwnd`` must already be an approved paste target (steps 1-2 --
        capture + safety rejection -- are the caller's job, not this
        method's). Writes ``text`` to the clipboard, re-verifies
        ``GetForegroundWindow() == hwnd`` immediately before pasting, and
        -- only if that still matches -- sends the paste shortcut once via
        SendInput (never Enter, never retried). Returns True once the
        paste has been sent; returns False, sending nothing, if the
        clipboard write failed or the immediate pre-paste focus re-check
        no longer matches ``hwnd``.
        """
        if not hwnd:
            log.debug("paste_into: no hwnd given -- nothing to paste into")
            return False

        try:
            self.write_text(text)
        except OSError:
            log.exception("paste_into: clipboard write failed; aborting paste")
            return False

        foreground_hwnd = self._get_foreground_window()
        if foreground_hwnd != hwnd:
            log.info(
                "paste_into: focus changed before paste (expected hwnd=%s, "
                "now %s) -- skipping paste",
                hwnd, foreground_hwnd,
            )
            return False

        self._send_paste_shortcut(app_class)
        return True

    # ---- Win32 helpers ----------------------------------------------------

    def _get_sequence_number(self) -> int:
        user32 = ctypes.windll.user32
        _set_signature(
            user32.GetClipboardSequenceNumber, restype=wintypes.DWORD, argtypes=[]
        )
        return user32.GetClipboardSequenceNumber()

    def _get_foreground_window(self) -> Optional[int]:
        user32 = ctypes.windll.user32
        _set_signature(user32.GetForegroundWindow, restype=wintypes.HWND, argtypes=[])
        hwnd = user32.GetForegroundWindow()
        return hwnd or None

    def _read_clipboard_text(self) -> Optional[str]:
        """Best-effort read of the current clipboard text.

        Returns ``None`` if the clipboard holds non-text data, is empty,
        or the read fails for any reason -- callers must treat that as
        "no saved text" rather than raising.
        """
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _set_signature(
            user32.OpenClipboard, restype=wintypes.BOOL, argtypes=[wintypes.HWND]
        )
        _set_signature(
            user32.IsClipboardFormatAvailable,
            restype=wintypes.BOOL,
            argtypes=[wintypes.UINT],
        )
        _set_signature(
            user32.GetClipboardData,
            restype=ctypes.c_void_p,
            argtypes=[wintypes.UINT],
        )
        _set_signature(
            kernel32.GlobalLock, restype=ctypes.c_void_p, argtypes=[ctypes.c_void_p]
        )
        _set_signature(
            kernel32.GlobalUnlock, restype=wintypes.BOOL, argtypes=[ctypes.c_void_p]
        )
        _set_signature(user32.CloseClipboard, restype=wintypes.BOOL, argtypes=[])

        if not user32.OpenClipboard(None):
            log.debug("_read_clipboard_text: OpenClipboard failed")
            return None
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return _read_utf16le_cstring(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        except Exception:
            log.debug("_read_clipboard_text: read failed", exc_info=True)
            return None
        finally:
            user32.CloseClipboard()

    def _set_clipboard_text(self, text: str) -> None:
        """Write ``text`` to the clipboard as CF_UNICODETEXT.

        Uses a UTF-16LE global memory handle
        (``GlobalAlloc(GMEM_MOVEABLE, ...)`` + ``GlobalLock``/
        ``GlobalUnlock``) per the documented ``SetClipboardData``
        contract:
        ``OpenClipboard`` -> ``EmptyClipboard`` ->
        ``SetClipboardData(CF_UNICODETEXT, handle)`` -> ``CloseClipboard``.
        Raises ``OSError`` if any step fails.

        Once ``SetClipboardData`` succeeds, the system takes ownership of
        the global memory handle -- this function must NOT free it
        afterward (that would free memory the clipboard/next reader still
        needs). If any earlier step fails, the handle is freed here since
        nothing else will ever take ownership of it.
        """
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _set_signature(
            kernel32.GlobalAlloc,
            restype=ctypes.c_void_p,
            argtypes=[wintypes.UINT, ctypes.c_size_t],
        )
        _set_signature(
            kernel32.GlobalLock, restype=ctypes.c_void_p, argtypes=[ctypes.c_void_p]
        )
        _set_signature(
            kernel32.GlobalUnlock, restype=wintypes.BOOL, argtypes=[ctypes.c_void_p]
        )
        _set_signature(
            kernel32.GlobalFree, restype=ctypes.c_void_p, argtypes=[ctypes.c_void_p]
        )
        _set_signature(
            user32.OpenClipboard, restype=wintypes.BOOL, argtypes=[wintypes.HWND]
        )
        _set_signature(user32.EmptyClipboard, restype=wintypes.BOOL, argtypes=[])
        _set_signature(
            user32.SetClipboardData,
            restype=ctypes.c_void_p,
            argtypes=[wintypes.UINT, ctypes.c_void_p],
        )
        _set_signature(user32.CloseClipboard, restype=wintypes.BOOL, argtypes=[])

        encoded = text.encode("utf-16-le") + b"\x00\x00"  # UTF-16 NUL terminator
        h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not h_global:
            raise OSError("GlobalAlloc failed")

        ptr = kernel32.GlobalLock(h_global)
        if not ptr:
            kernel32.GlobalFree(h_global)
            raise OSError("GlobalLock failed")
        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(h_global)

        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(h_global)
            raise OSError("OpenClipboard failed")
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, h_global):
                kernel32.GlobalFree(h_global)
                raise OSError("SetClipboardData failed")
            # Ownership of h_global has transferred to the system -- do
            # not free it.
        finally:
            user32.CloseClipboard()

    def _send_paste_shortcut(self, app_class: AppClass) -> None:
        """Send the paste shortcut once via SendInput.

        ``Ctrl+Shift+V`` for ``AppClass.TERMINAL``, ``Ctrl+V`` otherwise.
        The whole key-down/key-up sequence is sent as a single SendInput
        call so Windows never interleaves other input in the middle of it
        (see the SendInput docs). Never sends Enter; never retries.
        """
        vk_sequence = [VK_CONTROL]
        if app_class == AppClass.TERMINAL:
            vk_sequence.append(VK_SHIFT)
        vk_sequence.append(VK_V)

        events = [_make_key_input(vk, key_up=False) for vk in vk_sequence]
        events += [_make_key_input(vk, key_up=True) for vk in reversed(vk_sequence)]

        n = len(events)
        arr = (INPUT * n)(*events)

        user32 = ctypes.windll.user32
        _set_signature(
            user32.SendInput,
            restype=wintypes.UINT,
            argtypes=[wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int],
        )
        sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
        if sent != n:
            log.warning(
                "SendInput inserted %d/%d events -- paste shortcut may not "
                "have been fully delivered",
                sent, n,
            )


# _set_signature is now imported (aliased) from ._win32util at module top --
# see that module's docstring for the full handle/pointer-truncation
# rationale and the bound-method test-double AttributeError guard.
