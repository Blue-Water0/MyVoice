"""Tests for the explicit-user-copy override on ClipboardBackend.

An explicit "Copy Transcript" click is a deliberate user clipboard
action. It must not be silently overwritten later by the injector's
end-of-session automatic clipboard restoration.
"""
from __future__ import annotations

from myvoice.services.clipboard_backend import ClipboardBackend


class _FakeCb:
    """Duck-typed replacement for a Gtk.Clipboard.

    Records writes and returns whatever text() was last stored.
    """

    def __init__(self, initial: str = "") -> None:
        self._text = initial
        self.writes: list[str] = []

    def wait_for_text(self) -> str:
        return self._text

    def set_text(self, text: str, _len: int) -> None:
        self._text = text
        self.writes.append(text)

    def store(self) -> None:
        pass


def _bench_backend(monkeypatch) -> tuple[ClipboardBackend, _FakeCb, _FakeCb]:
    """Return a ClipboardBackend wired to two _FakeCb instances."""
    clip = _FakeCb(initial="ORIGINAL_CLIP")
    primary = _FakeCb(initial="ORIGINAL_PRIMARY")

    import myvoice.services.clipboard_backend as cbmod
    monkeypatch.setattr(cbmod, "_get_gtk_clipboards", lambda: (clip, primary))

    return ClipboardBackend(), clip, primary


def test_end_session_restores_original_by_default(monkeypatch):
    """Baseline: without a user override, end_session restores the
    pre-session clipboard."""
    b, clip, primary = _bench_backend(monkeypatch)
    b.begin_session()
    # Injector wrote something during the session.
    b.write_text("injected chunk")
    assert clip._text == "injected chunk"

    b.end_session()
    assert clip._text == "ORIGINAL_CLIP"
    assert primary._text == "ORIGINAL_PRIMARY"


def test_write_user_text_suppresses_end_session_restore(monkeypatch):
    """A deliberate 'Copy Transcript' write must survive end_session."""
    b, clip, primary = _bench_backend(monkeypatch)
    b.begin_session()
    # Simulate some injected text landing on the clipboard first.
    b.write_text("injected chunk")

    # User clicks Copy Transcript.
    ok = b.write_user_text("USER TRANSCRIPT")
    assert ok
    assert clip._text == "USER TRANSCRIPT"

    # Now dictation stops normally.
    b.end_session()

    # Because the user made an explicit clipboard action, the pre-session
    # clipboard should NOT be restored on top of the user's transcript.
    assert clip._text == "USER TRANSCRIPT"


def test_mark_user_override_alone_suppresses_restore(monkeypatch):
    """The low-level ``mark_user_override`` also drops the saved
    contents even if the caller did the write directly."""
    b, clip, primary = _bench_backend(monkeypatch)
    b.begin_session()
    b.write_text("chunk")
    b.mark_user_override()
    b.end_session()
    # Whatever was on the clipboard last is what remains.
    assert clip._text == "chunk"


def test_write_user_text_also_sets_primary(monkeypatch):
    b, clip, primary = _bench_backend(monkeypatch)
    b.begin_session()
    ok = b.write_user_text("HELLO")
    assert ok
    assert clip._text == "HELLO"
    assert primary._text == "HELLO"


def test_mark_user_override_outside_session_is_safe(monkeypatch):
    b, clip, primary = _bench_backend(monkeypatch)
    # No begin_session called. Nothing to override, must not raise.
    b.mark_user_override()
    b.end_session()  # also must not raise
