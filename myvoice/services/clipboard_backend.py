"""Clipboard + XTEST paste fallback for X11.

Behavior:
- Save clipboard once at session start.
- Each injection: take ownership of CLIPBOARD, verify focus is still
  the intended external window, then send paste shortcut via XTEST
  (xdotool WITHOUT --window). This is substantially more reliable than
  XSendEvent (--window) because many programs reject synthetic XSendEvent.
- Shortcut selected by AppClass: NORMAL -> ctrl+v, TERMINAL -> ctrl+shift+v.
- On end_session(), restore the original clipboard (unless user overrode it).

Clipboard ownership during dictation is delegated to a separate ``xclip``
process rather than held by our own GTK clipboard. This is not an
optimization — it is load-bearing:

X11 CLIPBOARD transfer is *lazy*. The owner only advertises ownership;
when a target app pastes, it sends a SelectionRequest that the **owner
must answer from its event loop**. A GTK owner answers from the GTK main
loop. But this module runs on the GTK main thread and sleeps on it
(settle windows, focus re-checks), and the injector above it may block it
further. While the main thread is blocked, we cannot answer
SelectionRequests at all — so the target asks for the text, gets no
reply, and pastes *nothing*, while ``xdotool`` still reports the paste
keystroke as successfully sent. That produced silent, intermittent loss
of dictated chunks (worst on fast consecutive chunks from long unbroken
speech), and every added settle/verify delay made it strictly worse.

``xclip`` forks and serves the selection from its own process, so
delivery no longer depends on our main loop being responsive.

Must be called from the GTK main thread.
"""
from __future__ import annotations

import ctypes
import logging
import shutil
import signal
import subprocess
import time
from typing import Optional

from .app_classifier import AppClass

log = logging.getLogger(__name__)

_PR_SET_PDEATHSIG = 1


def _pdeathsig_preexec() -> None:
    """preexec_fn for xclip's Popen: ask the kernel to SIGTERM this child
    the moment its parent process dies, for any reason -- including a
    SIGKILL that gives the parent no chance to run its own cleanup.
    Without this, a crashed/force-quit MyVoice leaves xclip processes
    running forever, still holding the X11 clipboard selection.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


def _has_xdotool() -> bool:
    return shutil.which("xdotool") is not None


def _has_xclip() -> bool:
    return shutil.which("xclip") is not None


def _xdotool_active_window() -> Optional[int]:
    """Query the currently active X11 window via xdotool. Returns XID or None."""
    if not _has_xdotool():
        return None
    try:
        out = subprocess.check_output(
            ["xdotool", "getactivewindow"],
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
        return int(out.decode().strip())
    except Exception as e:
        log.debug("_xdotool_active_window failed: %s", e)
        return None


def _get_gtk_clipboards():
    """Return (clipboard, primary) Gtk.Clipboard objects, or (None, None)."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk  # type: ignore  # noqa: F401
    except Exception as e:
        log.debug("GTK not available: %s", e)
        return (None, None)
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return (None, None)
        clipboard = Gtk.Clipboard.get_default(display)
        primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
        return (clipboard, primary)
    except Exception as e:
        log.debug("Gtk.Clipboard get failed: %s", e)
        return (None, None)


class ClipboardBackend:
    def __init__(self, paste_settle_s: float = 0.15) -> None:
        self._clip = None
        self._primary = None
        self._saved_clipboard: Optional[str] = None
        self._saved_primary: Optional[str] = None
        self._session_active = False
        self._user_overridden = False
        # Live `xclip` processes currently serving a selection for us.
        # Each stays alive until another client takes ownership, then
        # exits; we reap the finished ones so they don't linger.
        self._owner_procs: list[subprocess.Popen] = []
        # Held after a successful paste, before returning control to the
        # caller. Reading CLIPBOARD is asynchronous on X11 — the target
        # app has to request it from us and we have to still be the
        # owner when it does. Without this settle window, a fast-arriving
        # next dictation chunk can overwrite the clipboard before the
        # target has actually read the previous one, silently dropping
        # or garbling it even though the paste keystroke "succeeded".
        self._paste_settle_s = paste_settle_s

    def begin_session(self) -> None:
        """Snapshot the user's current clipboard. Idempotent."""
        if self._session_active:
            return
        self._clip, self._primary = _get_gtk_clipboards()
        self._saved_clipboard = self._read(self._clip)
        self._saved_primary = self._read(self._primary)
        self._session_active = True
        self._user_overridden = False
        log.debug(
            "Clipboard session began (saved %d/%d chars)",
            len(self._saved_clipboard or ""),
            len(self._saved_primary or ""),
        )

    def end_session(self) -> None:
        """Restore saved clipboard. Safe to call even if never began."""
        if not self._session_active:
            return
        if not self._user_overridden:
            try:
                if self._saved_clipboard is not None:
                    self._write(self._clip, self._saved_clipboard)
                if self._saved_primary is not None:
                    self._write(self._primary, self._saved_primary)
            except Exception:
                log.exception("clipboard restore failed")
        # Release any selection we were serving via xclip. Restoring
        # through GTK above already takes ownership away (which makes
        # xclip exit on its own), but terminate explicitly so nothing
        # lingers when there was nothing to restore.
        for proc in self._owner_procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._owner_procs.clear()
        self._saved_clipboard = None
        self._saved_primary = None
        self._session_active = False
        self._user_overridden = False
        log.debug("Clipboard session ended and restored")

    def paste_into(
        self,
        xid: Optional[int],
        text: str,
        app_class: AppClass = AppClass.NORMAL,
        own_xids: frozenset[int] = frozenset(),
    ) -> bool:
        """Write ``text`` to CLIPBOARD and send paste shortcut via XTEST.

        Focus verification (immediately before paste):
        - If both xid and active_xid are None: no target — return False.
        - If active_xid is in own_xids (MyVoice window): abort, return False.
        - If xid is not None and active_xid != xid: focus changed — return False.

        Shortcut:
        - AppClass.TERMINAL -> ctrl+shift+v
        - AppClass.NORMAL   -> ctrl+v

        XTEST path: xdotool key --clearmodifiers <shortcut>  (NO --window).
        """
        if not text:
            return True
        if not self._session_active:
            self.begin_session()

        if not _has_xdotool():
            log.warning("xdotool not installed; cannot send paste shortcut")
            return False

        # Take ownership before querying the active window. Prefer xclip:
        # it serves the selection from its own process, so the target can
        # still fetch the text while our GTK main thread is blocked (see
        # module docstring — holding ownership ourselves silently loses
        # chunks). Fall back to GTK ownership only if xclip is missing.
        if not self._own_selection(text, "clipboard"):
            log.debug("paste_into: xclip unavailable — falling back to GTK clipboard")
            if not self._write(self._clip, text):
                log.warning("paste_into: clipboard write failed")
                return False
        if not self._own_selection(text, "primary"):
            self._write(self._primary, text)  # best-effort

        # Give the clipboard manager a brief moment to propagate ownership.
        time.sleep(0.03)

        # Verify focus immediately before sending the shortcut.
        active_xid = _xdotool_active_window()
        if active_xid is None and xid is not None:
            # A None result is indistinguishable from "focus changed" below,
            # but it's also what a transient xdotool query failure looks
            # like (e.g. system busy transcribing). Retry once before
            # concluding focus actually moved and silently dropping the
            # chunk.
            time.sleep(0.03)
            active_xid = _xdotool_active_window()

        if active_xid is None and xid is None:
            log.info(
                "paste_into: no active window and no intended XID — skipping paste"
            )
            return False

        if active_xid is not None and active_xid in own_xids:
            log.info(
                "paste_into: active window xid=%d is a MyVoice window — "
                "aborting paste to avoid self-injection",
                active_xid,
            )
            return False

        if xid is not None and active_xid != xid:
            log.info(
                "paste_into: focus changed — intended xid=%d but active xid=%s"
                " — skipping paste",
                xid, active_xid,
            )
            return False

        # Select paste shortcut.
        shortcut = "ctrl+shift+v" if app_class == AppClass.TERMINAL else "ctrl+v"

        # Send via XTEST (no --window) for maximum compatibility.
        args = ["xdotool", "key", "--clearmodifiers", shortcut]
        log.debug(
            "paste_into: sending %s to active_xid=%s intended=%s app_class=%s",
            shortcut, active_xid, xid, app_class.value,
        )
        try:
            result = subprocess.run(
                args,
                check=False,
                capture_output=True,
                timeout=2.0,
            )
            if result.returncode != 0:
                log.warning(
                    "paste_into: xdotool %s returned rc=%d: %s",
                    shortcut, result.returncode,
                    result.stderr.decode(errors="replace").strip(),
                )
                return False
            log.debug("paste_into: xdotool %s OK (rc=0)", shortcut)
            if self._paste_settle_s > 0:
                time.sleep(self._paste_settle_s)
            return True
        except subprocess.TimeoutExpired:
            log.warning("paste_into: xdotool timed out")
            return False
        except Exception as e:
            log.warning("paste_into: xdotool exec failed: %s", e)
            return False

    # ------- helpers -------

    def _reap_owner_procs(self) -> None:
        """Drop references to xclip processes that have already exited."""
        self._owner_procs = [p for p in self._owner_procs if p.poll() is None]

    def _own_selection(self, text: str, selection: str) -> bool:
        """Take ownership of ``selection`` via a separate xclip process.

        Returns False if xclip is unavailable or the spawn failed, so the
        caller can fall back to GTK ownership.
        """
        if not _has_xclip():
            return False
        try:
            proc = subprocess.Popen(
                ["xclip", "-selection", selection],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=_pdeathsig_preexec,
            )
            assert proc.stdin is not None
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except Exception as e:
            log.debug("xclip ownership failed for %s: %s", selection, e)
            return False
        self._owner_procs.append(proc)
        self._reap_owner_procs()
        return True

    def _read(self, cb) -> Optional[str]:
        if cb is None:
            return None
        try:
            return cb.wait_for_text() or ""
        except Exception as e:
            log.debug("clipboard read failed: %s", e)
            return None

    def _write(self, cb, text: str) -> bool:
        if cb is None:
            return False
        try:
            cb.set_text(text, -1)
            cb.store()
            return True
        except Exception as e:
            log.debug("clipboard write failed: %s", e)
            return False

    def write_text(self, text: str) -> bool:
        """Write text to CLIPBOARD. Used by end-of-session fallback."""
        if self._clip is None:
            self._clip, self._primary = _get_gtk_clipboards()
        return self._write(self._clip, text)

    def mark_user_override(self) -> None:
        """Signal user performed explicit clipboard action; suppress auto-restore."""
        self._saved_clipboard = None
        self._saved_primary = None
        self._user_overridden = True
        log.debug("Clipboard user-override recorded; auto-restore suppressed")

    def write_user_text(self, text: str) -> bool:
        """Explicit user copy — writes text and suppresses auto-restore."""
        if self._clip is None or self._primary is None:
            self._clip, self._primary = _get_gtk_clipboards()
        ok = self._write(self._clip, text)
        try:
            self._write(self._primary, text)
        except Exception:
            pass
        if ok:
            self.mark_user_override()
        return ok
