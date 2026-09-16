"""Per-chunk text-insertion safety lifecycle for Windows (design doc §2).

This is the core safety module of the Windows port. It orchestrates the
pieces built in Tasks 4-6 into the exact per-chunk lifecycle described in
design doc §2 ("Text Insertion Safety"). It owns *no* new Win32/ctypes code
of its own -- every OS interaction is delegated to:

* ``focus_tracker.capture_focus`` (Task 4) -- discovery of the currently
  focused window/control, already PID-mismatch-corrected for password
  detection.
* ``clipboard_backend.ClipboardBackend`` (Task 5) -- the clipboard write +
  re-verify-focus + SendInput paste + session-end restore machinery.
* ``accessibility_backend`` (Task 6) -- the integrity-level guard and the
  own-window membership check.
* ``myvoice.services.app_classifier`` (safe-reuse) -- terminal vs. normal
  classification, which selects Ctrl+Shift+V vs. Ctrl+V.

Public lifecycle mirrors the Linux ``TextInjectionService`` so ``app.py``
(Task 12) can wire it identically:

    begin_session()                 -> None
    inject(text)                    -> InjectionResult
    end_session()                   -> tuple[bool, str]

The *internal* per-chunk algorithm, however, is the Win32 one from design
doc §2, not the Linux AT-SPI one. For each non-empty chunk, independently:

    1. Capture the current foreground target (fresh every chunk).
    2. Reject unsafe targets, in order: password field, MyVoice own window,
       target at a higher integrity level than MyVoice. A rejected chunk is
       buffered (added to ``missed_chunks``) and never pasted.
    3-5. Otherwise, classify the target and hand it to
       ``ClipboardBackend.paste_into`` -- which does its own clipboard
       write + immediate pre-paste focus re-verify + single SendInput paste.
       If ``paste_into`` returns False (focus changed between capture and
       paste, or the write failed), the chunk is buffered.
    6. Every chunk -- pasted or buffered -- is appended to
       ``full_session_transcript``; only failed chunks are appended to
       ``missed_chunks``.
    7. The next chunk starts again at step 1 with a completely fresh
       capture. Chunks are never redirected retroactively.

On ``end_session`` (design doc §2 "Mixed-Success Sessions"):

* If any chunk was missed, the *full* session transcript is written to the
  clipboard as the explicit fallback result and the clipboard's normal
  sequence-number restore is skipped (``end_session(missed_chunks_present=
  True)``). The user's pre-session clipboard is deliberately not restored --
  the recovered transcript takes priority.
* If nothing was missed, the clipboard runs its normal restore
  (``end_session(missed_chunks_present=False)``).

Import-safety: this module references no ``ctypes.windll``/``comtypes``
symbols itself. It imports the Task 4/5/6 modules and ``app_classifier`` at
top level -- all of which are import-safe on non-Windows (they keep their
Windows-only calls inside function bodies) -- so this module stays
importable on Linux for pytest collection.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

from myvoice.services.app_classifier import AppClass, classify

from .accessibility_backend import (
    current_process_integrity_level,
    is_own_window,
    target_integrity_higher,
)
from .clipboard_backend import ClipboardBackend
from .focus_tracker import TargetRef, capture_focus

log = logging.getLogger(__name__)

# InjectionResult.detail value for a password-field rejection. A shared
# constant (rather than a bare literal duplicated in app.py) so callers that
# need to distinguish this specific reject reason from the other "buffer"
# causes can't silently drift out of sync with a future reword here.
REJECT_PASSWORD = "password field"


@dataclass
class InjectionResult:
    """Outcome of routing a single chunk.

    ``backend``:
      * ``"clipboard"`` -- pasted successfully via clipboard + SendInput.
      * ``"buffer"``    -- safely buffered (a safety rejection or a
                           focus-race abort); recoverable from the full
                           transcript copied to the clipboard on stop.
    """

    ok: bool
    backend: str
    detail: str = ""


# DI seam types (all defaulted to the real Task 4/5/6 implementations).
FocusCapturer = Callable[[], TargetRef]
IntegrityChecker = Callable[[int, int], bool]
OwnWindowChecker = Callable[[int, frozenset[int]], bool]


class TextInjectionService:
    """Follow-current-focus text inserter for Windows.

    Every DI hook exists so tests can substitute for the real
    focus/clipboard/integrity machinery without touching the OS. The public
    lifecycle (``begin_session``/``inject``/``end_session``) matches the
    Linux service so the app wiring is identical across platforms.
    """

    def __init__(
        self,
        focus_capturer: FocusCapturer = capture_focus,
        clipboard: Optional[ClipboardBackend] = None,
        own_pid: Optional[int] = None,
        own_hwnds: frozenset[int] = frozenset(),
        extra_terminal_classes: Optional[list[str]] = None,
        own_integrity_level: Optional[int] = None,
        integrity_checker: IntegrityChecker = target_integrity_higher,
        own_window_checker: OwnWindowChecker = is_own_window,
    ) -> None:
        self._focus_capturer = focus_capturer
        self._clipboard = clipboard if clipboard is not None else ClipboardBackend()
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._own_hwnds = own_hwnds
        self._extra_terminal_classes: list[str] = list(extra_terminal_classes or [])
        self._integrity_checker = integrity_checker
        self._own_window_checker = own_window_checker
        # Captured once at construction (design doc §2 integrity guard). On
        # real Windows this queries the current process's integrity level;
        # tests pass it explicitly to avoid the ctypes call.
        if own_integrity_level is None:
            own_integrity_level = current_process_integrity_level()
        self._own_integrity_level = own_integrity_level

        # Per-session mutable state.
        self._session_active = False
        # Every chunk, pasted or not -- joined and copied to the clipboard as
        # the fallback result whenever any chunk was missed.
        self._full_transcript_parts: list[str] = []
        # Only the chunks that failed to reach a destination.
        self._missed_chunks: list[str] = []
        self._had_success = False
        self._chunk_seq = 0

    # ------------------------------------------------------------------ lifecycle

    def begin_session(self) -> None:
        """Reset per-session state and start the clipboard-save context."""
        self._full_transcript_parts.clear()
        self._missed_chunks.clear()
        self._had_success = False
        self._chunk_seq = 0
        self._clipboard.begin_session()
        self._session_active = True
        log.info("injector: session started (follow-current-focus, Win32)")

    def end_session(self) -> tuple[bool, str]:
        """Finalize the session (design doc §2 "Mixed-Success Sessions").

        Returns ``(had_any_success, buffered_text)``. ``buffered_text`` is
        the joined set of chunks that failed to reach a destination --
        empty when every chunk landed. It has already been surfaced for
        recovery as a side effect of this call: whenever it is non-empty the
        *full* session transcript (successful + missed) is written to the
        clipboard and the pre-session clipboard restore is skipped.
        """
        full_transcript = self._join(self._full_transcript_parts)
        buffered = self._join(self._missed_chunks)
        had_success = self._had_success
        missed_count = len(self._missed_chunks)

        if self._missed_chunks:
            # design doc §2: copy the FULL session transcript to the
            # clipboard as the explicit fallback and skip the restore. Note:
            # what is *written* is the full transcript; what is *returned* is
            # the missed text only, so the caller can tell the user which
            # part failed (mirrors the Linux (had_target, buffered) contract).
            try:
                self._clipboard.write_text(full_transcript)
            except Exception:
                log.exception("end_session: failed to write fallback transcript")
            try:
                self._clipboard.end_session(missed_chunks_present=True)
            except Exception:
                log.exception("end_session: clipboard.end_session failed")
            self._reset_session_state()
            log.info(
                "injector: end_session had_success=%s missed=%d chunks "
                "clipboard=full-transcript-written restore=skipped",
                had_success, missed_count,
            )
            return (had_success, buffered)

        try:
            self._clipboard.end_session(missed_chunks_present=False)
        except Exception:
            log.exception("end_session: clipboard.end_session failed")
        self._reset_session_state()
        log.info(
            "injector: end_session had_success=%s missed=0 chunks "
            "clipboard=restored",
            had_success,
        )
        return (had_success, buffered)

    def _reset_session_state(self) -> None:
        self._session_active = False
        self._full_transcript_parts.clear()
        self._missed_chunks.clear()

    @staticmethod
    def _join(parts: list[str]) -> str:
        return " ".join(p for p in (chunk.strip() for chunk in parts) if p).strip()

    # ------------------------------------------------------------------ inject

    def inject(self, text: str) -> InjectionResult:
        """Route ``text`` to whichever window is focused NOW (design doc §2)."""
        if not text:
            return InjectionResult(ok=True, backend="buffer", detail="empty")
        if not self._session_active:
            log.warning("inject() called with no active session")
            return InjectionResult(ok=False, backend="buffer", detail="no session")

        self._chunk_seq += 1
        seq = self._chunk_seq

        # Every chunk is recorded in the full transcript, regardless of the
        # outcome below -- this is what gets copied to the clipboard on stop
        # if anything was missed (design doc §2 step 6).
        self._full_transcript_parts.append(text)

        # Step 1: capture the current target, fresh for this chunk.
        ref = self._focus_capturer()
        app_class = classify(
            (ref.hwnd_class, ref.hwnd_class), self._extra_terminal_classes
        )
        log.debug(
            "inject #%d: chars=%d hwnd=%s hwnd_class=%s app_class=%s "
            "fg_pid=%s uia_pid=%s password=%s",
            seq, len(text), ref.hwnd, ref.hwnd_class, app_class.value,
            ref.foreground_pid, ref.uia_pid, ref.is_password,
        )

        # Step 2: reject unsafe targets, in the design doc's order.
        reason = self._rejection_reason(ref)
        if reason is not None:
            log.info("inject #%d: rejected (%s) -- buffering %d chars",
                     seq, reason, len(text))
            self._missed_chunks.append(text)
            return InjectionResult(ok=False, backend="buffer", detail=reason)

        # Steps 3-5: paste_into does the clipboard write, the immediate
        # pre-paste focus re-verify, and the single SendInput paste. It
        # returns False (sending nothing) if focus changed between our
        # capture and the paste, or the clipboard write failed.
        try:
            ok = self._clipboard.paste_into(ref.hwnd, text, app_class)
        except Exception:
            log.exception("inject #%d: clipboard.paste_into raised", seq)
            ok = False

        if ok:
            self._had_success = True
            shortcut = "ctrl+shift+v" if app_class == AppClass.TERMINAL else "ctrl+v"
            log.debug("inject #%d: OK backend=clipboard shortcut=%s hwnd=%s chars=%d",
                      seq, shortcut, ref.hwnd, len(text))
            return InjectionResult(ok=True, backend="clipboard", detail=shortcut)

        # Step 6: buffer on failure. The chunk that lost the focus race is
        # NOT redirected -- the next chunk gets its own fresh capture (step 7,
        # handled by the next inject() call re-invoking capture_focus).
        log.info(
            "inject #%d: paste aborted (focus changed or write failed) -- "
            "buffering %d chars. Click the destination field to continue.",
            seq, len(text),
        )
        self._missed_chunks.append(text)
        return InjectionResult(ok=False, backend="buffer", detail="focus changed")

    # ------------------------------------------------------------------ helpers

    def _rejection_reason(self, ref: TargetRef) -> Optional[str]:
        """Return why ``ref`` must be rejected, or ``None`` if it is a safe
        paste target. Checks run in design doc §2 step 2 order: password,
        own window, integrity-higher."""
        # Password field (already UIA-PID-mismatch-corrected by Task 4).
        if ref.is_password:
            return REJECT_PASSWORD

        # No focused window at all -- nothing to paste into.
        if ref.hwnd is None:
            return "no focused window"

        # MyVoice's own window -- by the explicit own-HWND set (design doc §2)
        # and, as a belt-and-suspenders guard mirroring the Linux service,
        # by the foreground window belonging to MyVoice's own process.
        if self._own_window_checker(ref.hwnd, self._own_hwnds):
            return "own window"
        if ref.foreground_pid is not None and ref.foreground_pid == self._own_pid:
            return "own window (pid)"

        # Target running at a higher integrity level than MyVoice: Windows
        # UIPI would silently block SendInput anyway. Fail-closed inside
        # target_integrity_higher (Task 6): any query failure returns True.
        if self._integrity_checker(ref.hwnd, self._own_integrity_level):
            return "integrity higher"

        return None
