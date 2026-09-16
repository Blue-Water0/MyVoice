"""Tests for myvoice_win.services.clipboard_backend.

``ClipboardBackend`` calls several ``ctypes.windll.user32``/``kernel32``
functions (clipboard open/read/write, ``GetClipboardSequenceNumber``,
``GetForegroundWindow``, ``SendInput``), none of which exist on this Linux
sandbox, so tests inject a fake ``windll`` via
``monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)`` --
same pattern as test_hotkey_service.py / test_focus_tracker.py.

One thing this fake does differently from the others: ``GlobalAlloc``/
``GlobalLock`` in the module under test feed real ``ctypes.memmove``/raw
memory reads (not mocked -- those are genuine ctypes calls, unrelated to
``ctypes.windll``). If ``FakeKernel32.GlobalAlloc`` returned a bare made-up
integer as a stand-in "pointer", the code under test's ``memmove`` would
try to write through a bogus address and segfault the test process. So
``FakeKernel32`` backs every allocation with a real ``ctypes`` buffer
(kept alive in a dict keyed by its address) and returns that buffer's
*real* address -- memory-safe, and it also means these tests exercise the
actual encode/memmove/decode byte-level round trip instead of mocking it
away.

Relatedly: test assertions that need to inspect what ended up in one of
those buffers use ``_read_utf16le_cstring`` (imported from the module
under test) rather than ``ctypes.wstring_at``. ``wstring_at`` reads "wide
chars" sized by the *current platform's* ``wchar_t`` -- 2 bytes (UTF-16)
on real Windows, matching CF_UNICODETEXT exactly, but 4 bytes (UCS-4) on
this Linux sandbox -- so it would misdecode the genuinely-2-byte-per-unit
buffers these fakes use. See that function's docstring in the module for
the full explanation.
"""
from __future__ import annotations

import ctypes

import pytest

from myvoice.services.app_classifier import AppClass
from myvoice_win.services.clipboard_backend import (
    KEYEVENTF_KEYUP,
    VK_CONTROL,
    VK_SHIFT,
    VK_V,
    ClipboardBackend,
    _read_utf16le_cstring,
)

CF_UNICODETEXT = 13
DEFAULT_HWND = 0x1000


# ---- fake ctypes.windll.kernel32 / user32 -----------------------------------


class FakeKernel32:
    """Fakes GlobalAlloc/GlobalLock/GlobalUnlock/GlobalFree using real
    ctypes buffers -- see module docstring for why a bare fabricated int
    "pointer" would be unsafe here.
    """

    def __init__(self):
        self._buffers: dict[int, ctypes.Array] = {}
        self.global_alloc_calls: list[tuple[int, int]] = []
        self.freed: list[int] = []
        self.fail_global_alloc = False
        self.fail_global_lock = False

    def GlobalAlloc(self, flags, size):
        self.global_alloc_calls.append((flags, size))
        if self.fail_global_alloc:
            return 0
        buf = ctypes.create_string_buffer(size)
        addr = ctypes.cast(buf, ctypes.c_void_p).value
        self._buffers[addr] = buf  # keep alive so the address stays valid
        return addr

    def GlobalLock(self, handle):
        if self.fail_global_lock:
            return None
        return handle if handle in self._buffers else None

    def GlobalUnlock(self, handle):
        return 1

    def GlobalFree(self, handle):
        self.freed.append(handle)
        self._buffers.pop(handle, None)
        return None  # Win32 returns NULL on success


class FakeUser32:
    def __init__(self, kernel32: FakeKernel32, hwnd=DEFAULT_HWND, sequence_number=100):
        self._kernel32 = kernel32
        self.hwnd = hwnd
        self.sequence_number = sequence_number
        self._clipboard_handle = None
        self._has_format = False
        self.set_clipboard_data_calls: list[tuple[int, int]] = []
        self.send_input_calls: list[list[tuple[int, int, int]]] = []
        self.fail_open_clipboard = False
        self.fail_set_clipboard_data = False

    # clipboard read/write
    def OpenClipboard(self, hwnd):
        return 0 if self.fail_open_clipboard else 1

    def CloseClipboard(self):
        return 1

    def EmptyClipboard(self):
        return 1

    def IsClipboardFormatAvailable(self, fmt):
        return 1 if (self._has_format and fmt == CF_UNICODETEXT) else 0

    def GetClipboardData(self, fmt):
        return self._clipboard_handle or 0

    def SetClipboardData(self, fmt, handle):
        self.set_clipboard_data_calls.append((fmt, handle))
        if self.fail_set_clipboard_data:
            return 0
        self._clipboard_handle = handle
        self._has_format = True
        self.sequence_number += 1
        return handle

    def GetClipboardSequenceNumber(self):
        return self.sequence_number

    # focus
    def GetForegroundWindow(self):
        return self.hwnd

    # paste
    def SendInput(self, n, inputs_array, cb_size):
        call = [
            (inputs_array[i].type, inputs_array[i].union.ki.wVk, inputs_array[i].union.ki.dwFlags)
            for i in range(n)
        ]
        self.send_input_calls.append(call)
        return n


class FakeWindll:
    def __init__(self):
        self.kernel32 = FakeKernel32()
        self.user32 = FakeUser32(self.kernel32)


@pytest.fixture
def fake_windll(monkeypatch):
    fake = FakeWindll()
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    return fake


def _seed_clipboard(fake: FakeWindll, text: str) -> None:
    """Pre-populate the fake clipboard with ``text`` as CF_UNICODETEXT, as
    if some other application had already put it there -- used to seed
    ``begin_session``'s snapshot without going through our own write path.

    Encodes independently of the module under test (no reuse of its
    encode/decode helpers) so read-path tests built on this genuinely
    cross-check the module's own decode logic.
    """
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    buf = ctypes.create_string_buffer(len(encoded))
    ctypes.memmove(buf, encoded, len(encoded))
    addr = ctypes.cast(buf, ctypes.c_void_p).value
    fake.kernel32._buffers[addr] = buf
    fake.user32._clipboard_handle = addr
    fake.user32._has_format = True


def _decode_handle(fake: FakeWindll, handle: int) -> str:
    """Decode what actually ended up in the fake global-memory buffer at
    ``handle``, using the module's own ``_read_utf16le_cstring`` (correct
    cross-platform; see module docstring re: ``ctypes.wstring_at``).
    """
    return _read_utf16le_cstring(handle)


# ---- begin_session / end_session: sequence-number restore rule -------------


def test_end_session_restores_when_sequence_number_unchanged(fake_windll):
    _seed_clipboard(fake_windll, "original clipboard text")
    cb = ClipboardBackend()
    cb.begin_session()

    cb.end_session(missed_chunks_present=False)

    assert len(fake_windll.user32.set_clipboard_data_calls) == 1
    _, handle = fake_windll.user32.set_clipboard_data_calls[-1]
    assert _decode_handle(fake_windll, handle) == "original clipboard text"


def test_end_session_does_not_restore_when_sequence_number_changed(fake_windll):
    _seed_clipboard(fake_windll, "original clipboard text")
    cb = ClipboardBackend()
    cb.begin_session()

    # Simulate another application copying something during the session --
    # bumps the real Windows sequence number without going through our
    # write path.
    fake_windll.user32.sequence_number += 1

    cb.end_session(missed_chunks_present=False)

    assert fake_windll.user32.set_clipboard_data_calls == []


def test_end_session_restores_after_chunk_paste_when_nothing_else_wrote(fake_windll):
    _seed_clipboard(fake_windll, "original")
    cb = ClipboardBackend()
    cb.begin_session()

    ok = cb.paste_into(fake_windll.user32.hwnd, "dictated chunk", AppClass.NORMAL)
    assert ok is True

    cb.end_session(missed_chunks_present=False)

    assert len(fake_windll.user32.set_clipboard_data_calls) == 2  # chunk write + restore
    _, last_handle = fake_windll.user32.set_clipboard_data_calls[-1]
    assert _decode_handle(fake_windll, last_handle) == "original"


def test_end_session_does_not_restore_when_something_wrote_after_last_chunk(fake_windll):
    _seed_clipboard(fake_windll, "original")
    cb = ClipboardBackend()
    cb.begin_session()
    cb.paste_into(fake_windll.user32.hwnd, "dictated chunk", AppClass.NORMAL)

    fake_windll.user32.sequence_number += 1  # external write after our chunk

    cb.end_session(missed_chunks_present=False)

    assert len(fake_windll.user32.set_clipboard_data_calls) == 1  # only the chunk write


def test_end_session_does_not_restore_when_missed_chunks_present(fake_windll):
    _seed_clipboard(fake_windll, "original")
    cb = ClipboardBackend()
    cb.begin_session()

    cb.end_session(missed_chunks_present=True)

    assert fake_windll.user32.set_clipboard_data_calls == []


def test_end_session_is_noop_when_never_begun(fake_windll):
    cb = ClipboardBackend()
    cb.end_session(missed_chunks_present=False)  # must not raise
    assert fake_windll.user32.set_clipboard_data_calls == []


def test_begin_session_saves_none_when_clipboard_has_no_text(fake_windll):
    cb = ClipboardBackend()
    cb.begin_session()  # fake clipboard starts with no text format present

    cb.end_session(missed_chunks_present=False)

    assert fake_windll.user32.set_clipboard_data_calls == []


def test_begin_session_is_idempotent(fake_windll):
    _seed_clipboard(fake_windll, "first")
    cb = ClipboardBackend()
    cb.begin_session()
    _seed_clipboard(fake_windll, "second")
    cb.begin_session()  # must be a no-op; "first" stays the saved snapshot

    cb.end_session(missed_chunks_present=False)

    _, handle = fake_windll.user32.set_clipboard_data_calls[-1]
    assert _decode_handle(fake_windll, handle) == "first"


def test_begin_session_reads_back_unicode_text_written_independently(fake_windll):
    text = "héllo wörld — 日本語 🎉"
    _seed_clipboard(fake_windll, text)

    cb = ClipboardBackend()
    cb.begin_session()

    assert cb._saved_text == text


# ---- write_user_text: must not disturb end_session's bookkeeping -----------


def test_write_user_text_does_not_update_bookkeeping_so_end_session_skips_restore(fake_windll):
    _seed_clipboard(fake_windll, "original")
    cb = ClipboardBackend()
    cb.begin_session()

    ok = cb.write_user_text("user copied this explicitly")
    assert ok is True

    cb.end_session(missed_chunks_present=False)

    # Only write_user_text's own write happened; end_session must not have
    # added a second (restore) SetClipboardData call on top of it.
    assert len(fake_windll.user32.set_clipboard_data_calls) == 1
    _, handle = fake_windll.user32.set_clipboard_data_calls[0]
    assert _decode_handle(fake_windll, handle) == "user copied this explicitly"


def test_write_user_text_returns_false_on_failure_without_raising(fake_windll):
    fake_windll.user32.fail_open_clipboard = True
    cb = ClipboardBackend()

    ok = cb.write_user_text("hello")

    assert ok is False


# ---- write_text -------------------------------------------------------------


def test_write_text_raises_oserror_when_open_clipboard_fails(fake_windll):
    fake_windll.user32.fail_open_clipboard = True
    cb = ClipboardBackend()

    with pytest.raises(OSError):
        cb.write_text("hello")


def test_write_text_round_trips_unicode_correctly(fake_windll):
    cb = ClipboardBackend()
    text = "héllo wörld — 日本語 🎉"

    cb.write_text(text)

    assert _decode_handle(fake_windll, fake_windll.user32._clipboard_handle) == text


def test_set_clipboard_text_frees_memory_when_set_clipboard_data_fails(fake_windll):
    fake_windll.user32.fail_set_clipboard_data = True
    cb = ClipboardBackend()

    with pytest.raises(OSError):
        cb.write_text("hello")

    assert len(fake_windll.kernel32.freed) == 1


def test_set_clipboard_text_does_not_free_memory_on_success(fake_windll):
    cb = ClipboardBackend()

    cb.write_text("hello")

    assert fake_windll.kernel32.freed == []


# ---- paste_into: shortcut selection -----------------------------------------


def test_paste_into_sends_ctrl_v_for_normal_app(fake_windll):
    cb = ClipboardBackend()

    ok = cb.paste_into(fake_windll.user32.hwnd, "hello", AppClass.NORMAL)

    assert ok is True
    assert len(fake_windll.user32.send_input_calls) == 1
    events = fake_windll.user32.send_input_calls[0]
    vks_down = [wvk for (_type, wvk, flags) in events if flags == 0]
    vks_up = [wvk for (_type, wvk, flags) in events if flags == KEYEVENTF_KEYUP]
    assert vks_down == [VK_CONTROL, VK_V]
    assert vks_up == [VK_V, VK_CONTROL]


def test_paste_into_sends_ctrl_shift_v_for_terminal_app(fake_windll):
    cb = ClipboardBackend()

    ok = cb.paste_into(fake_windll.user32.hwnd, "hello", AppClass.TERMINAL)

    assert ok is True
    events = fake_windll.user32.send_input_calls[0]
    vks_down = [wvk for (_type, wvk, flags) in events if flags == 0]
    vks_up = [wvk for (_type, wvk, flags) in events if flags == KEYEVENTF_KEYUP]
    assert vks_down == [VK_CONTROL, VK_SHIFT, VK_V]
    assert vks_up == [VK_V, VK_SHIFT, VK_CONTROL]


# ---- paste_into: pre-paste focus re-check -----------------------------------


def test_paste_into_returns_false_and_does_not_send_input_when_focus_changed(fake_windll):
    cb = ClipboardBackend()
    intended_hwnd = fake_windll.user32.hwnd
    fake_windll.user32.hwnd = intended_hwnd + 1  # focus moved before paste

    ok = cb.paste_into(intended_hwnd, "hello", AppClass.NORMAL)

    assert ok is False
    assert fake_windll.user32.send_input_calls == []
    # The clipboard write (step 3) still happens before the step-4 re-check.
    assert len(fake_windll.user32.set_clipboard_data_calls) == 1


def test_paste_into_returns_false_when_hwnd_is_falsy(fake_windll):
    cb = ClipboardBackend()

    ok = cb.paste_into(0, "hello", AppClass.NORMAL)

    assert ok is False
    assert fake_windll.user32.send_input_calls == []
    assert fake_windll.user32.set_clipboard_data_calls == []


def test_paste_into_returns_false_when_clipboard_write_fails(fake_windll):
    fake_windll.user32.fail_open_clipboard = True
    cb = ClipboardBackend()

    ok = cb.paste_into(fake_windll.user32.hwnd, "hello", AppClass.NORMAL)

    assert ok is False
    assert fake_windll.user32.send_input_calls == []
