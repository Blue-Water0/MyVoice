"""Route finalized transcript chunks into whatever text field is
currently focused.

Behavior contract:

* Every chunk is routed independently. There is no persistent
  destination — the user is free to click into a different editable
  field at any time and future chunks will follow.
* Insertion order of preference for each chunk:
    1. If the currently-focused accessible exposes ``EditableText`` and
       is not a password field, insert directly via AT-SPI ``insertText``
       at the caret.
    2. Otherwise, if we have a currently-focused X11 window (xid) that
       is not one of our own windows, write the chunk to the clipboard
       and simulate the appropriate paste shortcut via XTEST:
         - TERMINAL windows (by WM_CLASS): ctrl+shift+v
         - All other windows (NORMAL): ctrl+v
    3. Otherwise the chunk is kept in-session; on Stop, the joined
       transcript is written to the clipboard.
* Password fields are refused in all cases.
* MyVoice's own windows are refused by PID and XID.
* All string offsets are Python code-point offsets (characters), which
  matches what AT-SPI ``insertText`` expects. Correct for Hebrew/Arabic.

Threading contract: all methods must be called from the GTK main thread.
The pipeline calls ``inject()`` from the transcription worker via
``GLib.idle_add``.
"""
from __future__ import annotations

import logging
import os as _os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .accessibility_backend import insert_via_atspi_at, read_text_tail
from .app_classifier import AppClass, classify
from .clipboard_backend import ClipboardBackend
from .focus_tracker import TargetRef, capture_focus

log = logging.getLogger(__name__)

# Set MYVOICE_VERBOSE_DIAG=1 to log a sanitized 20-char sample of each chunk.
# WARNING: this can put dictated content in log files. Keep OFF by default.
_VERBOSE_DIAG: bool = _os.environ.get("MYVOICE_VERBOSE_DIAG", "").strip() == "1"


def _diag_sample(text: str, max_chars: int = 20) -> str:
    """Return a sanitized prefix of ``text`` for diagnostic logging only."""
    sample = text[:max_chars]
    sample = "".join(c if c.isprintable() and c != "\n" else "·" for c in sample)
    ellipsis = "…" if len(text) > max_chars else ""
    return f"{repr(sample)}{ellipsis}"


@dataclass
class InjectionResult:
    ok: bool
    # 'atspi'    — succeeded via AT-SPI EditableText.
    # 'clipboard'— succeeded via clipboard + XTEST paste.
    # 'buffer'   — safely buffered (no writable focus); copied at Stop.
    # 'refused'  — target was a password field.
    backend: str
    detail: str = ""


@dataclass
class _CaretState:
    """Per-accessible caret bookkeeping."""
    accessible: Any = None
    next_char_offset: Optional[int] = None


# Type of the AT-SPI helper: (accessible, text, at_offset) -> new_offset or None.
AtSpiInserter = Callable[[Any, str, Optional[int]], Optional[int]]

# Type of the AT-SPI text-readback helper: accessible -> tail text or None.
TextReader = Callable[[Any], Optional[str]]


class TextInjectionService:
    """Simple follow-current-focus text inserter.

    All DI hooks exist so tests can substitute for pyatspi / clipboard /
    xdotool without touching the system.
    """

    def __init__(
        self,
        focus_capturer: Callable[[], TargetRef] = capture_focus,
        atspi_inserter: AtSpiInserter = insert_via_atspi_at,
        clipboard: Optional[ClipboardBackend] = None,
        own_pid: Optional[int] = None,
        own_xids: frozenset[int] = frozenset(),
        extra_terminal_classes: Optional[list[str]] = None,
        text_reader: TextReader = read_text_tail,
        verify_attempts: int = 3,
        verify_poll_interval_s: float = 0.1,
    ) -> None:
        self._focus_capturer = focus_capturer
        self._atspi_inserter = atspi_inserter
        self._clipboard = clipboard if clipboard is not None else ClipboardBackend()
        self._own_pid = own_pid if own_pid is not None else _os.getpid()
        self._own_xids = own_xids
        self._extra_terminal_classes: list[str] = extra_terminal_classes or []
        self._text_reader = text_reader
        self._verify_attempts = verify_attempts
        self._verify_poll_interval_s = verify_poll_interval_s

        # Per-session mutable state.
        self._session_active = False
        self._buffer_parts: list[str] = []
        self._states: dict[int, _CaretState] = {}
        self._had_successful_injection = False
        self._chunk_seq = 0  # monotonic counter for diagnostics

    # ------------------------------------------------------------------ lifecycle

    def begin_session(self) -> None:
        """Reset per-session state and start the clipboard-save context."""
        self._buffer_parts.clear()
        self._states.clear()
        self._had_successful_injection = False
        self._chunk_seq = 0
        self._clipboard.begin_session()
        self._session_active = True
        log.info("injector: session started (follow-current-focus)")

    def end_session(self) -> tuple[bool, str]:
        """Finalize the session.

        Returns ``(had_target, buffered_text)``. ``buffered_text`` is
        non-empty whenever any chunk failed to reach a destination —
        whether the whole session never had one, or just some chunks were
        orphaned along the way — and callers must treat it as needing to
        be surfaced to the user (it has already been written to the
        clipboard as a side effect of this call).
        """
        buffered = " ".join(
            p for p in (chunk.strip() for chunk in self._buffer_parts) if p
        ).strip()
        had_target = self._had_successful_injection

        # Any leftover buffered text — whether it's the whole session (no
        # writable focus was ever found) or just chunks orphaned partway
        # through (a focus hiccup dropped them while earlier/later chunks
        # landed fine) — must be recoverable. Only when nothing was ever
        # buffered is it safe to restore the user's pre-session clipboard.
        if buffered:
            try:
                self._clipboard.write_text(buffered)
            except Exception:
                log.exception("Failed to write orphaned transcript to clipboard")
            self._reset_session_state()
            log.info(
                "injector: end_session had_target=%s buffered=%d chars "
                "clipboard=written",
                had_target, len(buffered),
            )
            return (had_target, buffered)

        try:
            self._clipboard.end_session()
        except Exception:
            log.exception("clipboard.end_session failed")
        self._reset_session_state()
        log.info(
            "injector: end_session had_target=%s buffered=0 chars "
            "clipboard=restored",
            had_target,
        )
        return (had_target, buffered)

    def _reset_session_state(self) -> None:
        self._session_active = False
        self._buffer_parts.clear()
        self._states.clear()

    # ------------------------------------------------------------------ inject

    def inject(self, text: str) -> InjectionResult:
        """Route ``text`` to whichever editable field is focused NOW."""
        if not text:
            return InjectionResult(ok=True, backend="buffer", detail="empty")
        if not self._session_active:
            log.warning("inject() called with no active session")
            return InjectionResult(ok=False, backend="buffer", detail="no session")

        self._chunk_seq += 1
        seq = self._chunk_seq
        fresh = self._focus_capturer()

        app_class = classify(fresh.wm_class, self._extra_terminal_classes)

        log.debug(
            "inject #%d: chars=%d xid=%s wm_class=%s app_class=%s "
            "role=%s editable=%s password=%s pid=%s",
            seq, len(text), fresh.xid, fresh.wm_class, app_class.value,
            fresh.role, fresh.supports_editable_text,
            fresh.is_password, fresh.pid,
        )
        if _VERBOSE_DIAG:
            log.debug("[DIAG] inject #%d sample=%s", seq, _diag_sample(text))

        # Refuse password fields.
        if fresh.is_password:
            log.info("inject #%d: refused — password field", seq)
            return InjectionResult(ok=False, backend="refused", detail="password field")

        # Refuse own windows (by PID and XID).
        is_own_pid = fresh.pid is not None and fresh.pid == self._own_pid
        is_own_xid = fresh.xid is not None and fresh.xid in self._own_xids
        if is_own_pid or is_own_xid:
            log.info(
                "inject #%d: own window focused (pid=%s xid=%s) — buffering %d chars",
                seq, fresh.pid, fresh.xid, len(text),
            )
            self._buffer_parts.append(text)
            return InjectionResult(ok=False, backend="buffer", detail="own window focused")

        # First choice: AT-SPI direct insertion.
        if fresh.supports_editable_text and fresh.accessible is not None:
            state = self._state_for(fresh.accessible)
            try:
                new_offset = self._atspi_inserter(
                    fresh.accessible, text, state.next_char_offset,
                )
            except Exception as e:
                log.info(
                    "inject #%d: AT-SPI raised %s — falling through to clipboard/XTEST",
                    seq, type(e).__name__,
                )
                new_offset = None

            if new_offset is not None:
                state.next_char_offset = new_offset
                self._had_successful_injection = True
                log.debug(
                    "inject #%d: OK backend=atspi offset=%d chars=%d",
                    seq, new_offset, len(text),
                )
                return InjectionResult(ok=True, backend="atspi")
            else:
                log.info(
                    "inject #%d: AT-SPI insertion returned None — "
                    "falling through to clipboard/XTEST",
                    seq,
                )

        # Fallback: clipboard + XTEST paste.
        if fresh.xid is not None:
            try:
                ok = self._clipboard.paste_into(
                    fresh.xid,
                    text,
                    app_class=app_class,
                    own_xids=self._own_xids,
                )
                if ok:
                    # xdotool reporting the paste keystroke as sent is not
                    # proof the target actually processed it as a paste —
                    # confirmed by live testing: paste_into can report
                    # success while the text never lands. Where the target
                    # exposes a readable AT-SPI Text interface (terminals
                    # do, for screen readers, even without EditableText),
                    # verify the chunk actually arrived and retry once
                    # before trusting it.
                    verified = self._verify_landed(fresh.accessible, text)
                    if verified is False:
                        log.info(
                            "inject #%d: clipboard paste reported success but "
                            "AT-SPI readback shows it missing — retrying once",
                            seq,
                        )
                        ok = self._clipboard.paste_into(
                            fresh.xid,
                            text,
                            app_class=app_class,
                            own_xids=self._own_xids,
                        )
                        if ok:
                            verified = self._verify_landed(fresh.accessible, text)
                            if verified is False:
                                log.info(
                                    "inject #%d: retry also failed verification "
                                    "— treating as failed, buffering %d chars",
                                    seq, len(text),
                                )
                                ok = False
                if ok:
                    self._had_successful_injection = True
                    shortcut = (
                        "ctrl+shift+v" if app_class == AppClass.TERMINAL else "ctrl+v"
                    )
                    log.debug(
                        "inject #%d: OK backend=clipboard shortcut=%s xid=%d chars=%d",
                        seq, shortcut, fresh.xid, len(text),
                    )
                    return InjectionResult(ok=True, backend="clipboard")
                else:
                    # paste_into returns False when focus changed or own window;
                    # ok can also be forced False above by failed verification.
                    log.info(
                        "inject #%d: clipboard paste aborted (focus changed, "
                        "own window, or unverified) — buffering %d chars. "
                        "Click the destination field to continue dictation.",
                        seq, len(text),
                    )
            except Exception:
                log.exception("inject #%d: clipboard.paste_into raised", seq)

        # Buffer for end-of-session clipboard drop.
        log.info(
            "inject #%d: buffering %d chars (app=%r wm_class=%s role=%s xid=%s)",
            seq, len(text), fresh.app_name, fresh.wm_class, fresh.role, fresh.xid,
        )
        self._buffer_parts.append(text)
        return InjectionResult(ok=False, backend="buffer", detail="no writable focus")

    def _verify_landed(self, accessible: Any, expected_chunk: str) -> Optional[bool]:
        """Poll ``accessible``'s exposed text for ``expected_chunk``.

        Returns True/False when the target is actually verifiable, or
        None when it exposes no readable Text interface at all — callers
        must treat None as "can't verify, keep trusting the paste result"
        rather than as a failure, since retrying blindly risks pasting a
        chunk twice into a target we can't confirm anything about.
        """
        # Compare with all whitespace collapsed: a terminal's exposed text
        # is a *rendered screen*, so a chunk that landed may be wrapped
        # across lines. Matching raw would report a false miss and trigger
        # a retry that pastes the chunk a second time.
        needle = " ".join(expected_chunk.split())
        if not needle:
            return True
        for attempt in range(self._verify_attempts):
            tail = self._text_reader(accessible)
            if tail is None:
                return None
            if needle in " ".join(tail.split()):
                return True
            if attempt + 1 < self._verify_attempts and self._verify_poll_interval_s > 0:
                time.sleep(self._verify_poll_interval_s)
        return False

    # ------------------------------------------------------------------ helpers

    def _state_for(self, accessible: Any) -> _CaretState:
        key = id(accessible)
        st = self._states.get(key)
        if st is None:
            st = _CaretState(accessible=accessible, next_char_offset=None)
            self._states[key] = st
        return st
