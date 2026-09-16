"""Behavior tests for the follow-current-focus TextInjectionService.

Covers the v0.6 contract (post-removal of the target-lock feature):

  * Each transcribed chunk is routed to whatever editable field is
    focused *at the moment of that chunk*.
  * If focus moves to a new editable field mid-session, subsequent
    chunks follow the new focus.
  * If no editable field is focused, the chunk is safely buffered;
    at Stop the joined transcript is copied to the clipboard.
  * Password fields are refused.
  * MyVoice's own accessibles (own PID) are never used as targets.
  * Character offsets are code-point offsets (Hebrew/Arabic ordering
    behaves correctly).

These tests deliberately avoid pyatspi, xdotool, and GTK clipboards by
injecting fake collaborators via the TextInjectionService DI hooks.
"""
from __future__ import annotations

from typing import Optional

from myvoice.services.focus_tracker import TargetRef
from myvoice.services.text_injection_service import TextInjectionService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAccessible:
    """Minimal Accessible-shaped object that records insertions."""

    def __init__(self, name: str = "field", initial_text: str = "") -> None:
        self.name = name
        self.text = initial_text
        self.caret_offset: Optional[int] = len(initial_text)
        # (offset, text, len_chars) per call.
        self.insertions: list[tuple[int, str, int]] = []
        self.dead = False


def fake_atspi_inserter(acc, text, at_char_offset):
    """Test stand-in for insert_via_atspi_at. Character-offset semantics."""
    if not isinstance(acc, FakeAccessible) or acc.dead:
        return None
    offset = at_char_offset
    if offset is None:
        offset = acc.caret_offset if acc.caret_offset is not None else len(acc.text)
    if offset < 0:
        offset = 0
    n_chars = len(text)
    acc.text = acc.text[:offset] + text + acc.text[offset:]
    new_offset = offset + n_chars
    acc.caret_offset = new_offset
    acc.insertions.append((offset, text, n_chars))
    return new_offset


class ScriptedFocus:
    """Focus source whose result the test changes over time."""

    def __init__(self, initial: TargetRef) -> None:
        self._target = initial

    def set(self, target: TargetRef) -> None:
        self._target = target

    def __call__(self) -> TargetRef:
        return self._target


class FakeClipboard:
    def __init__(self, paste_results: Optional[list[bool]] = None) -> None:
        self.begin_calls = 0
        self.end_calls = 0
        self.paste_calls: list[tuple[Optional[int], str]] = []
        self.written: list[str] = []
        # If given, popped one per paste_into() call (repeats last value
        # once exhausted). Defaults to always-True for existing tests.
        self._paste_results = list(paste_results) if paste_results else None

    def begin_session(self) -> None:
        self.begin_calls += 1

    def end_session(self) -> None:
        self.end_calls += 1

    def paste_into(self, xid, text, app_class=None, own_xids=None) -> bool:
        self.paste_calls.append((xid, text))
        if self._paste_results is None:
            return True
        if len(self._paste_results) > 1:
            return self._paste_results.pop(0)
        return self._paste_results[0]

    def write_text(self, text: str) -> bool:
        self.written.append(text)
        return True


class ScriptedTextReader:
    """Fake AT-SPI Text readback. Returns successive values from
    ``responses`` (one per call, repeating the last), or None if the
    accessible has no Text interface — mirrors ``read_text_tail``."""

    def __init__(self, responses: list[Optional[str]]) -> None:
        self._responses = list(responses)
        self.calls: list[Any] = []

    def __call__(self, accessible: Any) -> Optional[str]:
        self.calls.append(accessible)
        if not self._responses:
            return None
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _target(acc: Optional[FakeAccessible], *, xid: Optional[int] = 100,
            password: bool = False, pid: Optional[int] = 222,
            app: Optional[str] = "test-app",
            role: Optional[str] = "entry") -> TargetRef:
    return TargetRef(
        accessible=acc,
        xid=xid,
        app_name=app,
        is_password=password,
        supports_editable_text=(acc is not None) and not password,
        role=role,
        pid=pid,
    )


def _svc(focus, clipboard: Optional[FakeClipboard] = None,
         own_pid: int = 1, text_reader=None) -> TextInjectionService:
    kwargs: dict = dict(
        focus_capturer=focus,
        atspi_inserter=fake_atspi_inserter,
        clipboard=clipboard or FakeClipboard(),
        own_pid=own_pid,
    )
    if text_reader is not None:
        kwargs["text_reader"] = text_reader
        kwargs["verify_poll_interval_s"] = 0.0
    return TextInjectionService(**kwargs)


# ---------------------------------------------------------------------------
# Follow-current-focus behavior
# ---------------------------------------------------------------------------


def test_inject_goes_to_currently_focused_field():
    a = FakeAccessible("A")
    focus = ScriptedFocus(_target(a))
    svc = _svc(focus)

    svc.begin_session()
    r = svc.inject("hello")
    assert r.ok and r.backend == "atspi"
    assert a.text == "hello"
    svc.end_session()


def test_focus_change_mid_session_reroutes_to_new_field():
    a = FakeAccessible("A")
    b = FakeAccessible("B")
    focus = ScriptedFocus(_target(a))
    svc = _svc(focus)

    svc.begin_session()
    svc.inject("alpha")
    # User clicks into B.
    focus.set(_target(b))
    svc.inject("beta")
    svc.end_session()

    assert a.text == "alpha"
    assert b.text == "beta"


def test_ordering_preserved_across_english_hebrew_arabic():
    a = FakeAccessible("A")
    focus = ScriptedFocus(_target(a))
    svc = _svc(focus)

    svc.begin_session()
    svc.inject("Hello ")
    svc.inject("שלום ")
    svc.inject("مرحبا")
    svc.end_session()

    assert a.text == "Hello שלום مرحبا"


def test_char_offsets_for_hebrew():
    a = FakeAccessible("A", initial_text="")
    focus = ScriptedFocus(_target(a))
    svc = _svc(focus)

    svc.begin_session()
    svc.inject("שלום ")   # 5 chars incl. trailing space
    svc.inject("עולם")    # 4 chars
    svc.end_session()

    assert a.insertions[0] == (0, "שלום ", 5)
    assert a.insertions[1] == (5, "עולם", 4)
    assert a.text == "שלום עולם"


def test_char_offsets_for_arabic():
    a = FakeAccessible("A", initial_text="")
    focus = ScriptedFocus(_target(a))
    svc = _svc(focus)

    svc.begin_session()
    svc.inject("مرحبا")           # 5 chars
    svc.inject(" بالعالم")        # 8 chars incl. leading space
    svc.end_session()

    assert a.insertions[0] == (0, "مرحبا", 5)
    assert a.insertions[1] == (5, " بالعالم", 8)
    assert a.text == "مرحبا بالعالم"


# ---------------------------------------------------------------------------
# Refusals and safe fallbacks
# ---------------------------------------------------------------------------


def test_password_field_is_refused():
    a = FakeAccessible("A")
    focus = ScriptedFocus(_target(a, password=True))
    svc = _svc(focus)

    svc.begin_session()
    r = svc.inject("secret")
    assert r.ok is False and r.backend == "refused"
    assert a.text == ""
    svc.end_session()


def test_password_field_mid_session_is_refused_without_spillover():
    a = FakeAccessible("A")
    b_pw = FakeAccessible("B-pw")
    focus = ScriptedFocus(_target(a))
    svc = _svc(focus)

    svc.begin_session()
    svc.inject("public")
    focus.set(_target(b_pw, password=True))
    r = svc.inject("secret")
    assert r.ok is False and r.backend == "refused"
    # Neither the new password field nor the previous field received it.
    assert a.text == "public"
    assert b_pw.text == ""
    svc.end_session()


def test_own_pid_focus_is_refused_and_buffered():
    """MyVoice's own accessibles must never be a dictation destination."""
    my = FakeAccessible("my-own-widget")
    focus = ScriptedFocus(
        _target(my, pid=1, xid=None, app="MyVoice", role="text")
    )
    clip = FakeClipboard()
    svc = _svc(focus, clipboard=clip, own_pid=1)

    svc.begin_session()
    r = svc.inject("this must not go to us")
    assert r.ok is False and r.backend == "buffer"
    assert my.text == ""

    had, buffered = svc.end_session()
    assert had is False
    assert buffered == "this must not go to us"
    assert clip.written == ["this must not go to us"]


def test_no_writable_focus_buffers_and_copies_to_clipboard_on_stop():
    """No editable field, no writable X11 window -> buffer + drop on Stop."""
    focus = ScriptedFocus(TargetRef(
        accessible=None, xid=None, supports_editable_text=False,
    ))
    clip = FakeClipboard()
    svc = _svc(focus, clipboard=clip)

    svc.begin_session()
    r = svc.inject("orphan")
    assert r.ok is False and r.backend == "buffer"

    had, buffered = svc.end_session()
    assert had is False
    assert buffered == "orphan"
    assert clip.written == ["orphan"]


def test_clipboard_paste_used_when_only_xid_available():
    """AT-SPI insertion failed but the current focus has an X11 window
    id — safe to paste via Ctrl+V because that's where the user's own
    keyboard would type right now."""
    # accessible=None means no EditableText.
    focus = ScriptedFocus(TargetRef(
        accessible=None, xid=42, supports_editable_text=False,
        app_name="terminal", role="terminal", pid=222,
    ))
    clip = FakeClipboard()
    svc = _svc(focus, clipboard=clip)

    svc.begin_session()
    r = svc.inject("ls -la")
    assert r.ok is True and r.backend == "clipboard"
    assert clip.paste_calls == [(42, "ls -la")]

    had, _ = svc.end_session()
    assert had is True


def test_clipboard_paste_verified_via_readback_when_target_exposes_text():
    """Terminals expose a read-only AT-SPI Text interface (for screen
    readers) even though they refuse EditableText. When that's
    available, a paste that xdotool reports as sent but that verifiably
    never landed must be retried before being trusted."""
    focus = ScriptedFocus(TargetRef(
        accessible="terminal-accessible", xid=42, supports_editable_text=False,
        app_name="terminal", role="terminal", pid=222,
    ))
    clip = FakeClipboard()
    # All 3 poll attempts after the first paste show the chunk missing
    # (it didn't really land); after the retry paste, the very next
    # readback shows it present.
    reader = ScriptedTextReader(["$ ", "$ ", "$ ", "$ ls -la"])
    svc = _svc(focus, clipboard=clip, text_reader=reader)

    svc.begin_session()
    r = svc.inject("ls -la")
    assert r.ok is True and r.backend == "clipboard"
    assert clip.paste_calls == [(42, "ls -la"), (42, "ls -la")]  # retried once


def test_clipboard_paste_buffered_when_verification_fails_after_retry():
    """If the retry also fails verification, the chunk must not be
    reported as a silent success — it falls back to buffering, which is
    recoverable via clipboard at end_session() instead of being lost."""
    focus = ScriptedFocus(TargetRef(
        accessible="terminal-accessible", xid=42, supports_editable_text=False,
        app_name="terminal", role="terminal", pid=222,
    ))
    clip = FakeClipboard()
    reader = ScriptedTextReader(["$ "])  # never shows the pasted text
    svc = _svc(focus, clipboard=clip, text_reader=reader)

    svc.begin_session()
    r = svc.inject("ls -la")
    assert r.ok is False and r.backend == "buffer"
    assert clip.paste_calls == [(42, "ls -la"), (42, "ls -la")]  # retried once

    had, buffered = svc.end_session()
    assert had is False
    assert buffered == "ls -la"
    assert clip.written == ["ls -la"]  # recoverable, not lost


def test_clipboard_paste_unverifiable_target_keeps_single_attempt():
    """When the target exposes no readable Text interface at all
    (read_text_tail returns None), we have no way to verify — must not
    retry blindly (that would risk duplicate pastes) and must keep
    trusting the paste's own report, same as before verification existed."""
    focus = ScriptedFocus(TargetRef(
        accessible=None, xid=42, supports_editable_text=False,
        app_name="some-app", role="unknown", pid=222,
    ))
    clip = FakeClipboard()
    reader = ScriptedTextReader([None])
    svc = _svc(focus, clipboard=clip, text_reader=reader)

    svc.begin_session()
    r = svc.inject("hello")
    assert r.ok is True and r.backend == "clipboard"
    assert clip.paste_calls == [(42, "hello")]  # no retry


def test_focus_leaves_writable_field_mid_session_buffers_the_orphan_chunk():
    """A had focus at chunk 1. Then the user clicks a non-editable
    location before chunk 2 is ready. Chunk 2 must not go to A anymore."""
    a = FakeAccessible("A")
    focus = ScriptedFocus(_target(a))
    clip = FakeClipboard()
    svc = _svc(focus, clipboard=clip)

    svc.begin_session()
    svc.inject("hello ")
    assert a.text == "hello "

    # User clicks somewhere with no editable field and no window id.
    focus.set(TargetRef(accessible=None, xid=None,
                        supports_editable_text=False))
    r = svc.inject("world")
    assert r.ok is False and r.backend == "buffer"
    assert a.text == "hello "  # unchanged — no writing to old focus

    had, buffered = svc.end_session()
    assert had is True  # first chunk succeeded
    assert buffered == "world"
    # The orphaned chunk must not be silently lost just because an
    # earlier chunk succeeded elsewhere in the session.
    assert clip.written == ["world"]


def test_empty_text_is_a_noop():
    a = FakeAccessible("A")
    focus = ScriptedFocus(_target(a))
    svc = _svc(focus)

    svc.begin_session()
    r = svc.inject("")
    assert r.ok is True and r.backend == "buffer"
    assert a.text == ""
    svc.end_session()


def test_no_active_session_inject_returns_no_session():
    a = FakeAccessible("A")
    focus = ScriptedFocus(_target(a))
    svc = _svc(focus)

    r = svc.inject("something")
    assert r.ok is False and r.backend == "buffer"
    assert r.detail == "no session"


def test_end_session_normal_path_restores_clipboard():
    """When at least one chunk succeeded, end_session restores the
    pre-session clipboard rather than writing the transcript."""
    a = FakeAccessible("A")
    focus = ScriptedFocus(_target(a))
    clip = FakeClipboard()
    svc = _svc(focus, clipboard=clip)

    svc.begin_session()
    svc.inject("hi")
    had, buffered = svc.end_session()

    assert had is True
    assert buffered == ""
    assert clip.written == []            # transcript NOT dropped on clipboard
    assert clip.begin_calls == 1
    assert clip.end_calls == 1           # pre-session clipboard restored


def test_end_session_no_target_writes_clipboard():
    """When no chunk was ever inserted anywhere, the joined buffer is
    written to the clipboard."""
    focus = ScriptedFocus(TargetRef(
        accessible=None, xid=None, supports_editable_text=False,
    ))
    clip = FakeClipboard()
    svc = _svc(focus, clipboard=clip)

    svc.begin_session()
    svc.inject("hello")
    svc.inject("world")
    had, buffered = svc.end_session()

    assert had is False
    assert buffered == "hello world"
    assert clip.written == ["hello world"]


# ---------------------------------------------------------------------------
# New: own_xids, app_class, wm_class wiring
# ---------------------------------------------------------------------------

def test_own_xid_focus_is_refused_not_pasted():
    """If the active X11 window XID is one of our own, do not paste."""
    from myvoice.services.text_injection_service import TextInjectionService

    clip = FakeClipboard()
    focus = ScriptedFocus(TargetRef(
        accessible=None, xid=555, supports_editable_text=False,
        pid=999, app_name="SomeApp",
    ))
    svc = TextInjectionService(
        focus_capturer=focus,
        atspi_inserter=fake_atspi_inserter,
        clipboard=clip,
        own_pid=1,
        own_xids=frozenset({555}),
    )
    svc.begin_session()
    r = svc.inject("hello")
    assert r.ok is False
    assert r.backend == "buffer"
    assert r.detail == "own window focused"
    assert clip.paste_calls == []


def test_terminal_wm_class_passes_correct_app_class_to_paste():
    """Terminal WM_CLASS must cause AppClass.TERMINAL passed to paste_into."""
    from myvoice.services.text_injection_service import TextInjectionService
    from myvoice.services.app_classifier import AppClass

    paste_app_classes = []

    class TrackingClipboard(FakeClipboard):
        def paste_into(self, xid, text, app_class=None, own_xids=None):
            paste_app_classes.append(app_class)
            self.paste_calls.append((xid, text))
            return True

    focus = ScriptedFocus(TargetRef(
        accessible=None, xid=77, supports_editable_text=False,
        pid=222, wm_class=("xterm", "xterm"),
    ))
    svc = TextInjectionService(
        focus_capturer=focus,
        atspi_inserter=fake_atspi_inserter,
        clipboard=TrackingClipboard(),
        own_pid=1,
    )
    svc.begin_session()
    svc.inject("ls -la")
    assert paste_app_classes == [AppClass.TERMINAL]


def test_verbose_diag_flag_exists():
    """_VERBOSE_DIAG must be a bool attribute on the module."""
    import myvoice.services.text_injection_service as m
    assert isinstance(m._VERBOSE_DIAG, bool)


def test_diag_sample_truncates_and_sanitizes():
    """_diag_sample must truncate to 20 chars and replace control chars."""
    from myvoice.services.text_injection_service import _diag_sample
    long_text = "a" * 30
    sample = _diag_sample(long_text)
    assert "…" in sample

    newline_text = "hello\nworld"
    sample2 = _diag_sample(newline_text)
    assert "\n" not in sample2


def test_normal_app_passes_normal_app_class():
    """Non-terminal WM_CLASS must cause AppClass.NORMAL passed to paste_into."""
    from myvoice.services.text_injection_service import TextInjectionService
    from myvoice.services.app_classifier import AppClass

    paste_app_classes = []

    class TrackingClipboard(FakeClipboard):
        def paste_into(self, xid, text, app_class=None, own_xids=None):
            paste_app_classes.append(app_class)
            self.paste_calls.append((xid, text))
            return True

    focus = ScriptedFocus(TargetRef(
        accessible=None, xid=88, supports_editable_text=False,
        pid=333, wm_class=("sublime_text", "sublime_text"),
    ))
    svc = TextInjectionService(
        focus_capturer=focus,
        atspi_inserter=fake_atspi_inserter,
        clipboard=TrackingClipboard(),
        own_pid=1,
    )
    svc.begin_session()
    svc.inject("hello world")
    assert paste_app_classes == [AppClass.NORMAL]
