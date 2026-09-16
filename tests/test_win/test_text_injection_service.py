"""Tests for myvoice_win.services.text_injection_service.

This is the core per-chunk text-insertion safety lifecycle (design doc §2).
Every test drives the service through fully-injected fakes -- a fake
``focus_capturer`` and a fake ``ClipboardBackend`` test double -- so none of
the real Win32/ctypes/comtypes machinery is exercised here (that lives in
the Task 4/5/6 modules and is tested there). The point of these tests is the
*orchestration*: capture -> reject-in-order -> classify -> paste ->
buffer-on-failure -> end-of-session branching.

Each row of design doc §2's "Insertion Summary Table" is covered:

    | Password field                         | Buffer only. Never paste.   |
    | MyVoice own window focused             | Buffer only. Never paste.   |
    | Focus changed between capture & paste  | Buffer. Next chunk fresh.   |
    | Target integrity higher than MyVoice   | Buffer only. Warn on stop.  |
    | Target is a known terminal             | SendInput Ctrl+Shift+V.     |
    | Target is a known non-terminal         | SendInput Ctrl+V.           |
    | On stop with missed_chunks non-empty   | Copy full transcript; no    |
    |                                        | restore.                    |
"""
from __future__ import annotations

from typing import Optional

import pytest

from myvoice.services.app_classifier import AppClass
from myvoice_win.services.focus_tracker import TargetRef
from myvoice_win.services.text_injection_service import (
    InjectionResult,
    TextInjectionService,
)

# Integrity level constants (winnt.h RIDs) used to make the intent obvious.
MEDIUM = 0x2000
HIGH = 0x3000

# A known Windows terminal HWND class and a known non-terminal one.
TERMINAL_CLASS = "ConsoleWindowClass"       # cmd.exe / PowerShell conhost
NORMAL_CLASS = "Chrome_WidgetWin_1"         # Chrome / Electron / etc.

TARGET_HWND = 0x1000
OWN_HWND = 0x2000
TARGET_PID = 4321
OWN_PID = 9999


# --------------------------------------------------------------------------- fakes


def make_ref(
    hwnd: Optional[int] = TARGET_HWND,
    foreground_pid: Optional[int] = TARGET_PID,
    is_password: bool = False,
    hwnd_class: Optional[str] = NORMAL_CLASS,
) -> TargetRef:
    return TargetRef(
        hwnd=hwnd,
        foreground_pid=foreground_pid,
        uia_pid=foreground_pid,
        is_password=is_password,
        hwnd_class=hwnd_class,
    )


class FakeFocusCapturer:
    """Returns a queued sequence of TargetRefs, one per ``capture_focus``
    call. Records how many times it was called so tests can assert that each
    chunk got its own *fresh* capture (design doc §2 step 7)."""

    def __init__(self, refs: list[TargetRef]) -> None:
        self._refs = list(refs)
        self.calls = 0

    def __call__(self) -> TargetRef:
        self.calls += 1
        # Repeat the last ref if called more times than queued, so a test
        # that only cares about a single chunk doesn't have to over-specify.
        idx = min(self.calls - 1, len(self._refs) - 1)
        return self._refs[idx]


class FakeClipboard:
    """Test double implementing the ClipboardBackend surface the injection
    service uses: begin_session / write_text / paste_into / end_session.

    ``paste_results`` is a per-call queue of bools returned by
    ``paste_into`` (default True). Every call is recorded for assertions.
    """

    def __init__(self, paste_results: Optional[list[bool]] = None) -> None:
        self.begin_called = 0
        self.write_text_calls: list[str] = []
        self.paste_calls: list[tuple[int, str, AppClass]] = []
        self.end_calls: list[bool] = []
        self._paste_results = list(paste_results or [])

    def begin_session(self) -> None:
        self.begin_called += 1

    def write_text(self, text: str) -> None:
        self.write_text_calls.append(text)

    def paste_into(self, hwnd: int, text: str, app_class: AppClass) -> bool:
        self.paste_calls.append((hwnd, text, app_class))
        if self._paste_results:
            return self._paste_results.pop(0)
        return True

    def end_session(self, missed_chunks_present: bool) -> None:
        self.end_calls.append(missed_chunks_present)


def make_service(
    refs: list[TargetRef],
    clipboard: Optional[FakeClipboard] = None,
    own_hwnds: frozenset[int] = frozenset(),
    integrity_higher: bool = False,
    extra_terminal_classes: Optional[list[str]] = None,
) -> tuple[TextInjectionService, FakeFocusCapturer, FakeClipboard]:
    capturer = FakeFocusCapturer(refs)
    cb = clipboard if clipboard is not None else FakeClipboard()

    def fake_integrity_checker(hwnd: int, own_level: int) -> bool:
        return integrity_higher

    svc = TextInjectionService(
        focus_capturer=capturer,
        clipboard=cb,
        own_pid=OWN_PID,
        own_hwnds=own_hwnds,
        extra_terminal_classes=extra_terminal_classes,
        own_integrity_level=MEDIUM,
        integrity_checker=fake_integrity_checker,
    )
    return svc, capturer, cb


# --------------------------------------------------------------------------- tests


def test_begin_session_starts_clipboard_session():
    svc, _capturer, cb = make_service([make_ref()])
    svc.begin_session()
    assert cb.begin_called == 1


def test_password_field_buffers_only_never_pastes():
    """Insertion Summary Table: password field -> buffer only, never paste."""
    svc, _capturer, cb = make_service([make_ref(is_password=True)])
    svc.begin_session()
    result = svc.inject("secret text")

    assert result.ok is False
    assert cb.paste_calls == []  # never pasted
    had_success, buffered = svc.end_session()
    assert had_success is False
    assert buffered == "secret text"
    # Full transcript copied to clipboard; restore skipped.
    assert cb.write_text_calls == ["secret text"]
    assert cb.end_calls == [True]


def test_own_window_buffers_only_never_pastes():
    """Insertion Summary Table: MyVoice own window focused -> buffer only."""
    svc, _capturer, cb = make_service(
        [make_ref(hwnd=OWN_HWND)], own_hwnds=frozenset({OWN_HWND})
    )
    svc.begin_session()
    result = svc.inject("into myself")

    assert result.ok is False
    assert cb.paste_calls == []
    had_success, buffered = svc.end_session()
    assert had_success is False
    assert buffered == "into myself"


def test_own_pid_also_treated_as_own_window():
    """Foreground window owned by MyVoice's own PID is refused too."""
    svc, _capturer, cb = make_service([make_ref(foreground_pid=OWN_PID)])
    svc.begin_session()
    result = svc.inject("into myself by pid")

    assert result.ok is False
    assert cb.paste_calls == []


def test_integrity_higher_buffers_only_never_pastes():
    """Insertion Summary Table: target integrity higher -> buffer only."""
    svc, _capturer, cb = make_service([make_ref()], integrity_higher=True)
    svc.begin_session()
    result = svc.inject("privileged target")

    assert result.ok is False
    assert cb.paste_calls == []
    had_success, buffered = svc.end_session()
    assert had_success is False
    assert buffered == "privileged target"


def test_no_foreground_window_buffers():
    """No focused window at all (hwnd is None) -> buffer."""
    svc, _capturer, cb = make_service([make_ref(hwnd=None, foreground_pid=None)])
    svc.begin_session()
    result = svc.inject("nowhere to go")

    assert result.ok is False
    assert cb.paste_calls == []


def test_known_terminal_uses_ctrl_shift_v_path():
    """Insertion Summary Table: known terminal -> Ctrl+Shift+V (TERMINAL)."""
    svc, _capturer, cb = make_service([make_ref(hwnd_class=TERMINAL_CLASS)])
    svc.begin_session()
    result = svc.inject("ls -la")

    assert result.ok is True
    assert len(cb.paste_calls) == 1
    hwnd, text, app_class = cb.paste_calls[0]
    assert hwnd == TARGET_HWND
    assert text == "ls -la"
    assert app_class == AppClass.TERMINAL


def test_known_non_terminal_uses_ctrl_v_path():
    """Insertion Summary Table: known non-terminal -> Ctrl+V (NORMAL)."""
    svc, _capturer, cb = make_service([make_ref(hwnd_class=NORMAL_CLASS)])
    svc.begin_session()
    result = svc.inject("hello world")

    assert result.ok is True
    assert len(cb.paste_calls) == 1
    _hwnd, _text, app_class = cb.paste_calls[0]
    assert app_class == AppClass.NORMAL


def test_extra_terminal_classes_are_honored():
    """User-configured extra terminal classes feed classify()."""
    svc, _capturer, cb = make_service(
        [make_ref(hwnd_class="MyCustomTerm")],
        extra_terminal_classes=["mycustomterm"],
    )
    svc.begin_session()
    svc.inject("custom terminal")

    _hwnd, _text, app_class = cb.paste_calls[0]
    assert app_class == AppClass.TERMINAL


def test_windows_terminal_class_is_terminal():
    """CASCADIA_HOSTING_WINDOW_CLASS (Windows Terminal) classifies TERMINAL,
    proving the app_classifier addition took effect end-to-end."""
    svc, _capturer, cb = make_service(
        [make_ref(hwnd_class="CASCADIA_HOSTING_WINDOW_CLASS")]
    )
    svc.begin_session()
    svc.inject("wt")

    _hwnd, _text, app_class = cb.paste_calls[0]
    assert app_class == AppClass.TERMINAL


def test_focus_changed_mid_chunk_buffers_and_next_chunk_gets_fresh_capture():
    """Insertion Summary Table: focus changed between capture and paste ->
    this chunk buffered; the *next* chunk gets a fresh capture and is NOT
    redirected to the window that stole focus (design doc §2 steps 5-7)."""
    first = make_ref(hwnd=0xAAAA, hwnd_class=NORMAL_CLASS)
    second = make_ref(hwnd=0xBBBB, hwnd_class=NORMAL_CLASS)
    # paste_into fails for chunk 1 (focus changed), succeeds for chunk 2.
    cb = FakeClipboard(paste_results=[False, True])
    svc, capturer, cb = make_service([first, second], clipboard=cb)
    svc.begin_session()

    r1 = svc.inject("first chunk")
    assert r1.ok is False  # buffered

    r2 = svc.inject("second chunk")
    assert r2.ok is True

    # Each chunk got its own fresh capture.
    assert capturer.calls == 2
    # Chunk 1 attempted paste into 0xAAAA (its captured hwnd), chunk 2 into
    # 0xBBBB -- chunk 1 was NOT retroactively redirected to 0xBBBB.
    assert cb.paste_calls[0][0] == 0xAAAA
    assert cb.paste_calls[1][0] == 0xBBBB

    had_success, buffered = svc.end_session()
    assert had_success is True
    # Only the missed chunk 1 surfaces as buffered text.
    assert buffered == "first chunk"


def test_mixed_session_end_session_branching():
    """Design doc §2 "Mixed-Success Sessions": one success + one buffered.

    - full_session_transcript (both chunks) is written to the clipboard.
    - clipboard.end_session is called with missed_chunks_present=True (skip
      the sequence-number restore).
    - end_session returns (had_any_success=True, <buffered/missed text only>).
    """
    good = make_ref(hwnd=0xAAAA, hwnd_class=NORMAL_CLASS)
    pw = make_ref(hwnd=0xBBBB, is_password=True)
    svc, _capturer, cb = make_service([good, pw])
    svc.begin_session()

    r1 = svc.inject("hello world")
    r2 = svc.inject("secret")
    assert r1.ok is True
    assert r2.ok is False

    had_success, buffered = svc.end_session()

    assert had_success is True
    # Return value is the buffered (missed) text only, NOT the successful chunk.
    assert buffered == "secret"
    # The clipboard fallback receives the FULL session transcript.
    assert cb.write_text_calls == ["hello world secret"]
    # Restore is skipped because there were missed chunks.
    assert cb.end_calls == [True]


def test_clean_session_restores_and_returns_empty():
    """No missed chunks -> normal restore path, empty buffered return."""
    svc, _capturer, cb = make_service(
        [make_ref(hwnd_class=NORMAL_CLASS), make_ref(hwnd_class=NORMAL_CLASS)]
    )
    svc.begin_session()
    svc.inject("first")
    svc.inject("second")

    had_success, buffered = svc.end_session()

    assert had_success is True
    assert buffered == ""
    # Normal restore: end_session called with missed_chunks_present=False and
    # no fallback-transcript write_text.
    assert cb.end_calls == [False]
    assert cb.write_text_calls == []


def test_empty_text_is_noop():
    svc, capturer, cb = make_service([make_ref()])
    svc.begin_session()
    result = svc.inject("")
    assert result.ok is True
    assert capturer.calls == 0  # never even captured focus
    assert cb.paste_calls == []


def test_inject_without_session_is_rejected():
    svc, _capturer, cb = make_service([make_ref()])
    # No begin_session() call.
    result = svc.inject("orphan")
    assert result.ok is False
    assert cb.paste_calls == []


def test_injection_result_is_the_expected_type():
    svc, _capturer, _cb = make_service([make_ref(hwnd_class=NORMAL_CLASS)])
    svc.begin_session()
    result = svc.inject("hi")
    assert isinstance(result, InjectionResult)
    assert result.backend == "clipboard"
