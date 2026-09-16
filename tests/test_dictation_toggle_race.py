"""Regression coverage for the 2026-07-19 freeze: stop_listening() flips
_listening to False synchronously but the real teardown (audio close +
transcriber drain) runs in a background thread. A fast double hotkey
press let start_listening() see _listening=False and begin a brand new
session while the old one was still tearing down, racing two sessions'
worth of audio/transcriber/injector state against each other.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_app():
    """Build a MyVoiceApp with all external I/O mocked out."""
    with (
        patch("myvoice.app.HotkeyService"),
        patch("myvoice.app.AudioService"),
        patch("myvoice.app.OverlayService"),
    ):
        from myvoice.app import MyVoiceApp
        app = MyVoiceApp()
        app.quit = MagicMock()
        app._tray = MagicMock()
        app._window = MagicMock()
        app._overlay = MagicMock()
        app._hotkey = MagicMock()
        app._injector = MagicMock()
        app._injector.end_session.return_value = (True, "")
        app._settings = MagicMock()
        app._settings_service = MagicMock()
        return app


def test_start_listening_blocked_while_previous_session_is_stopping():
    """A second hotkey press must not start a new session while the
    previous session's teardown is still running in the background."""
    app = _make_app()
    app._listening = False
    app._starting = False
    app._stopping = True

    app.start_listening()

    app._injector.begin_session.assert_not_called()
    assert app._listening is False


def test_stop_listening_sets_stopping_flag_before_backgrounding_teardown():
    """_stopping must flip True synchronously, in the same call that
    flips _listening False, so there is no window where a racing
    start_listening() sees both flags clear."""
    app = _make_app()
    app._listening = True
    app._transcriber = MagicMock()
    app._segmenter = MagicMock()
    app._segmenter.flush.return_value = None

    with patch("myvoice.app.threading.Thread") as mock_thread:
        app.stop_listening()

    assert app._stopping is True
    mock_thread.assert_called_once()


def test_after_finalize_clears_stopping_flag():
    """Once the previous session has fully finished tearing down,
    start_listening() must be allowed again."""
    app = _make_app()
    app._stopping = True

    app._after_finalize()

    assert app._stopping is False
