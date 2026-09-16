"""Graceful shutdown unit tests."""
from __future__ import annotations

import threading
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
        # Stub GTK/GIO calls that need a display
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


def test_shutting_down_flag_starts_false():
    app = _make_app()
    assert app.shutting_down is False


def test_request_quit_sets_flag():
    app = _make_app()
    app.request_quit()
    assert app.shutting_down is True


def test_no_ui_callback_after_shutdown():
    """GLib.idle_add callbacks must not touch UI once shutting_down is set."""
    app = _make_app()
    app.shutting_down = True

    # _handle_transcribed_text should bail immediately without touching window
    app._window.append_transcript.reset_mock()
    app._handle_transcribed_text("hello")
    app._window.append_transcript.assert_not_called()

    # _set_status should bail immediately
    app._window.set_status.reset_mock()
    app._set_status("ready", "Ready.")
    app._window.set_status.assert_not_called()

    # _on_audio_rms guard: overlay.set_level must not be posted
    # We test the method directly — it should return without calling idle_add
    with patch("myvoice.app.GLib") as mock_glib:
        app._on_audio_rms(0.5)
        mock_glib.idle_add.assert_not_called()


def test_double_quit_is_idempotent():
    """Calling request_quit() twice must not crash or double-exit."""
    app = _make_app()
    with patch.object(app, '_begin_shutdown_ui', return_value=False):
        app.request_quit()
        app.request_quit()  # second call must be a no-op
    # quit() only called from _finish_quit, not from request_quit itself
    assert app.quit.call_count == 0


def test_toggle_listening_blocked_during_shutdown():
    """toggle_listening must be a no-op once shutting_down is set."""
    app = _make_app()
    app.shutting_down = True
    app._listening = False
    with patch.object(app, 'start_listening') as mock_start:
        app.toggle_listening()
        mock_start.assert_not_called()


def _run_shutdown_sync(app):
    """Drive the full shutdown sequence synchronously (no GTK main loop needed).

    GLib.idle_add is patched to call the posted function directly so tests
    don't need a running GLib main loop.
    """
    import myvoice.app as app_module

    idle_queue = []

    def _fake_idle_add(fn, *args):
        idle_queue.append((fn, args))

    with patch.object(app_module.GLib, 'idle_add', side_effect=_fake_idle_add):
        app._do_shutdown_work()

    # Drain the idle queue (simulates GLib dispatching callbacks).
    for fn, args in idle_queue:
        fn(*args)


def test_quit_while_idle_calls_finish_quit():
    """Quit while not listening must proceed through _finish_quit and call app.quit()."""
    app = _make_app()
    app._listening = False
    app._transcriber = None
    app._segmenter = None
    # Set flag and timestamp as request_quit() would
    app.shutting_down = True
    import time as _time
    app._shutdown_started_at = _time.monotonic()

    _run_shutdown_sync(app)

    app.quit.assert_called_once()


def test_quit_while_listening_stops_audio():
    """Quit while listening must call audio.stop()."""
    app = _make_app()
    app._listening = True
    app._transcriber = MagicMock()
    app._transcriber.request_drain_and_stop = MagicMock()
    app._segmenter = MagicMock()
    app._segmenter.flush.return_value = None
    app.shutting_down = True
    import time as _time
    app._shutdown_started_at = _time.monotonic()

    _run_shutdown_sync(app)

    app._audio.stop.assert_called_once()


def test_quit_while_processing_drains_transcriber():
    """Quit while transcriber is active must call request_drain_and_stop."""
    app = _make_app()
    app._listening = False
    app._transcriber = MagicMock()
    app._transcriber.request_drain_and_stop = MagicMock()
    app._segmenter = MagicMock()
    app._segmenter.flush.return_value = None
    app.shutting_down = True
    import time as _time
    app._shutdown_started_at = _time.monotonic()

    _run_shutdown_sync(app)

    app._transcriber.request_drain_and_stop.assert_called_once_with(
        ready_wait=3.0, join_timeout=8.0
    )


def test_audio_stop_exception_does_not_block_shutdown():
    """If audio.stop() raises, shutdown must still proceed to _finish_quit."""
    app = _make_app()
    app._audio.stop.side_effect = RuntimeError("device locked")
    app._transcriber = None
    app._segmenter = None
    app.shutting_down = True
    import time as _time
    app._shutdown_started_at = _time.monotonic()

    _run_shutdown_sync(app)

    # quit() must have been reached despite the exception
    app.quit.assert_called_once()


def test_hotkey_unregister_exception_does_not_block_quit():
    """If hotkey.unregister() raises, self.quit() must still be called."""
    app = _make_app()
    app._hotkey.unregister.side_effect = RuntimeError("X11 error")
    app._transcriber = None
    app._segmenter = None
    app.shutting_down = True
    import time as _time
    app._shutdown_started_at = _time.monotonic()

    _run_shutdown_sync(app)

    app.quit.assert_called_once()
