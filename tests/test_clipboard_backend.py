"""Tests for ClipboardBackend.paste_into() — focus verification and shortcut selection."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from myvoice.services.app_classifier import AppClass


def _make_backend(paste_settle_s: float = 0.0):
    from myvoice.services.clipboard_backend import ClipboardBackend
    b = ClipboardBackend(paste_settle_s=paste_settle_s)
    b._clip = MagicMock()
    b._primary = MagicMock()
    b._clip.set_text = MagicMock()
    b._clip.store = MagicMock()
    b._primary.set_text = MagicMock()
    b._primary.store = MagicMock()
    b._session_active = True
    b._saved_clipboard = None
    b._saved_primary = None
    return b


def test_normal_app_uses_ctrl_v(monkeypatch):
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        r = MagicMock()
        r.returncode = 0
        r.stderr = b""
        return r

    with (
        patch("myvoice.services.clipboard_backend.subprocess.run", side_effect=fake_run),
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=99),
    ):
        result = b.paste_into(99, "hello", app_class=AppClass.NORMAL, own_xids=frozenset())

    assert result is True
    key_call = [c for c in calls if "key" in c]
    assert key_call, "xdotool key not called"
    assert "--window" not in key_call[0]
    assert "ctrl+v" in key_call[0]
    assert "ctrl+shift+v" not in " ".join(str(x) for x in key_call[0])


def test_terminal_app_uses_ctrl_shift_v(monkeypatch):
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        r = MagicMock()
        r.returncode = 0
        r.stderr = b""
        return r

    with (
        patch("myvoice.services.clipboard_backend.subprocess.run", side_effect=fake_run),
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=99),
    ):
        result = b.paste_into(99, "ls", app_class=AppClass.TERMINAL, own_xids=frozenset())

    assert result is True
    key_call = [c for c in calls if "key" in c]
    assert "ctrl+shift+v" in key_call[0]
    # Confirm it's NOT plain ctrl+v (check that ctrl+v alone isn't a separate entry)
    assert key_call[0].count("ctrl+v") == 0 or "ctrl+shift+v" in key_call[0]


def test_own_xid_focus_aborts_paste(monkeypatch):
    """If the active window at paste time is MyVoice, do not paste."""
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")

    with (
        patch("myvoice.services.clipboard_backend.subprocess.run") as mock_run,
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=555),
    ):
        result = b.paste_into(99, "secret", app_class=AppClass.NORMAL,
                               own_xids=frozenset({555}))

    assert result is False
    mock_run.assert_not_called()


def test_focus_changed_at_paste_time_aborts(monkeypatch):
    """If active window changed to a different external window, abort paste."""
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")

    with (
        patch("myvoice.services.clipboard_backend.subprocess.run") as mock_run,
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=888),
    ):
        # intended=99, but active is now 888
        result = b.paste_into(99, "hello", app_class=AppClass.NORMAL,
                               own_xids=frozenset())

    assert result is False
    mock_run.assert_not_called()


def test_xdotool_nonzero_exit_returns_false(monkeypatch):
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")

    def fake_run(args, **kwargs):
        r = MagicMock()
        r.returncode = 1
        r.stderr = b"error"
        return r

    with (
        patch("myvoice.services.clipboard_backend.subprocess.run", side_effect=fake_run),
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=99),
    ):
        result = b.paste_into(99, "hello", app_class=AppClass.NORMAL, own_xids=frozenset())

    assert result is False


def test_no_xdotool_returns_false(monkeypatch):
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: None)

    result = b.paste_into(99, "hello", app_class=AppClass.NORMAL, own_xids=frozenset())
    assert result is False


def test_no_window_at_all_skips_paste(monkeypatch):
    """xid=None and active=None means no known target — skip paste."""
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")

    with (
        patch("myvoice.services.clipboard_backend.subprocess.run") as mock_run,
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=None),
    ):
        result = b.paste_into(None, "hello", app_class=AppClass.NORMAL, own_xids=frozenset())

    assert result is False
    mock_run.assert_not_called()


def test_no_window_arg_in_xdotool_command(monkeypatch):
    """Ensure --window is never passed to xdotool key."""
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        r = MagicMock()
        r.returncode = 0
        r.stderr = b""
        return r

    with (
        patch("myvoice.services.clipboard_backend.subprocess.run", side_effect=fake_run),
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=42),
    ):
        b.paste_into(42, "test", app_class=AppClass.NORMAL, own_xids=frozenset())

    assert "--window" not in captured_args


def test_successful_paste_holds_settle_window(monkeypatch):
    """After a successful paste, hold paste_settle_s so the next chunk
    can't overwrite the clipboard before the target has read this one."""
    b = _make_backend(paste_settle_s=0.15)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")

    def fake_run(args, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stderr = b""
        return r

    sleeps = []
    with (
        patch("myvoice.services.clipboard_backend.subprocess.run", side_effect=fake_run),
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=99),
        patch("myvoice.services.clipboard_backend.time.sleep", side_effect=sleeps.append),
    ):
        result = b.paste_into(99, "hello", app_class=AppClass.NORMAL, own_xids=frozenset())

    assert result is True
    assert 0.15 in sleeps


def test_failed_paste_does_not_hold_settle_window(monkeypatch):
    """No point settling if the paste itself never happened."""
    b = _make_backend(paste_settle_s=0.15)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")

    def fake_run(args, **kwargs):
        r = MagicMock()
        r.returncode = 1
        r.stderr = b"error"
        return r

    sleeps = []
    with (
        patch("myvoice.services.clipboard_backend.subprocess.run", side_effect=fake_run),
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=99),
        patch("myvoice.services.clipboard_backend.time.sleep", side_effect=sleeps.append),
    ):
        result = b.paste_into(99, "hello", app_class=AppClass.NORMAL, own_xids=frozenset())

    assert result is False
    assert 0.15 not in sleeps


def test_transient_focus_query_failure_is_retried(monkeypatch):
    """A None active-window result can mean 'focus changed' or a transient
    xdotool query failure (e.g. system busy transcribing). It must be
    retried once before being treated as a real focus change, otherwise
    chunks get silently dropped even though focus never actually moved."""
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")

    def fake_run(args, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stderr = b""
        return r

    active_results = iter([None, 99])
    with (
        patch("myvoice.services.clipboard_backend.subprocess.run", side_effect=fake_run),
        patch(
            "myvoice.services.clipboard_backend._xdotool_active_window",
            side_effect=lambda: next(active_results),
        ),
        patch("myvoice.services.clipboard_backend.time.sleep"),
    ):
        result = b.paste_into(99, "hello", app_class=AppClass.NORMAL, own_xids=frozenset())

    assert result is True


def test_persistent_focus_query_failure_still_aborts(monkeypatch):
    """If the retry also comes back None, this really is 'no known focus'
    and the chunk must still be safely buffered, not pasted blind."""
    b = _make_backend()
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/xdotool")

    with (
        patch("myvoice.services.clipboard_backend.subprocess.run") as mock_run,
        patch("myvoice.services.clipboard_backend._xdotool_active_window", return_value=None),
        patch("myvoice.services.clipboard_backend.time.sleep"),
    ):
        result = b.paste_into(99, "hello", app_class=AppClass.NORMAL, own_xids=frozenset())

    assert result is False
    mock_run.assert_not_called()
