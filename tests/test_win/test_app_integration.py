"""Tests for the Windows app integration controller (``myvoice_win.app``).

Scope (per the Task 12 brief): this is the one task where a full
audio -> VAD -> transcription -> injection pipeline cannot be exercised
without a display and real audio hardware. These tests therefore cover the
integration seams that *can* run headless:

* the cross-thread ``_post`` marshaling reaching a real ``MainWindow`` on
  the Qt main thread (the Qt equivalent of ``GLib.idle_add``),
* ``_apply_settings``'s hotkey-rebind-on-change branch (with
  ``HotkeyService`` mocked), and
* the autostart registry read/write (with a fake ``winreg`` injected into
  ``sys.modules``).

The full recording pipeline is intentionally *not* faked here -- it needs
real-hardware verification on a Windows machine later.
"""
from __future__ import annotations

import threading
import types
from unittest import mock

import pytest

from myvoice_win.app import MyVoiceApp
from myvoice_win.services.settings_service import Settings


# --------------------------------------------------------------------------- helpers


def _bare_controller(qapp) -> MyVoiceApp:
    """A controller constructed but NOT activated (no window/tray/injector).

    Mirrors the Linux ``__init__`` state where most collaborators are still
    ``None``; individual tests attach real objects or mocks as needed.
    """
    return MyVoiceApp(qapp)


def _pump(qapp, predicate, timeout=5.0):
    """Process Qt events until ``predicate()`` is true or ``timeout`` elapses."""
    import time

    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    return predicate()


# --------------------------------------------------------------------------- _post

def test_post_marshals_call_onto_main_thread_reaching_main_window(qapp):
    """A ``_post`` from a background thread updates a real MainWindow.

    This exercises the QObject/Signal marshaling path end-to-end: the emit
    happens on a worker thread, and the slot (and thus the widget mutation)
    must run on the Qt main thread.
    """
    from myvoice_win.ui.main_window import MainWindow

    app = _bare_controller(qapp)
    window = MainWindow(
        on_toggle=lambda: None,
        on_open_settings=lambda: None,
        on_quit=lambda: None,
        on_language_changed=lambda _m: None,
        on_mic_changed=lambda _m: None,
        get_input_devices=lambda: [],
        initial_language_mode="auto",
        initial_mic=None,
    )
    app._window = window

    main_thread = threading.get_ident()
    ran_on = {}

    def worker():
        # Post a status update AND record which thread the slot runs on.
        app._post(app._set_status, "ready", "Marshaled OK")
        app._post(lambda: ran_on.setdefault("tid", threading.get_ident()))

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert _pump(qapp, lambda: "tid" in ran_on), "posted callable never ran"
    # The slot must have executed on the Qt main thread, not the worker.
    assert ran_on["tid"] == main_thread
    assert window._status_label.text() == "Marshaled OK"


# ------------------------------------------------------------------- _apply_settings

def _controller_for_apply(qapp, current: Settings) -> MyVoiceApp:
    app = _bare_controller(qapp)
    app._settings = current
    app._settings_service = mock.Mock()
    app._hotkey = mock.Mock()
    app._window = None
    app._injector = mock.Mock()
    # Neutralize autostart so these tests don't depend on winreg.
    app._apply_autostart = mock.Mock()
    return app


def test_apply_settings_rebinds_hotkey_when_changed(qapp):
    old = Settings(hotkey="ctrl+alt+d")
    app = _controller_for_apply(qapp, old)

    new = Settings(hotkey="ctrl+alt+j")
    app._apply_settings(new)

    app._hotkey.rebind.assert_called_once()
    called_accel = app._hotkey.rebind.call_args.args[0]
    assert called_accel == "ctrl+alt+j"
    app._settings_service.save.assert_called_once_with(new)
    assert app._settings is new


def test_apply_settings_does_not_rebind_when_hotkey_unchanged(qapp):
    old = Settings(hotkey="ctrl+alt+d", model="medium")
    app = _controller_for_apply(qapp, old)

    new = Settings(hotkey="ctrl+alt+d", model="medium")
    app._apply_settings(new)

    app._hotkey.rebind.assert_not_called()


def test_apply_settings_triggers_preload_when_model_changed(qapp):
    old = Settings(hotkey="ctrl+alt+d", model="medium")
    app = _controller_for_apply(qapp, old)
    app._preload_model_async = mock.Mock()

    new = Settings(hotkey="ctrl+alt+d", model="small")
    app._apply_settings(new)

    app._preload_model_async.assert_called_once()


# ------------------------------------------------------------------- autostart

class _FakeWinreg(types.ModuleType):
    """Minimal stand-in for the Windows-only ``winreg`` stdlib module."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self) -> None:
        super().__init__("winreg")
        self.set_values: list = []
        self.deleted_values: list = []
        self.created_keys: list = []
        self.opened_keys: list = []
        self.closed = 0
        self.delete_raises = None  # set to an exception instance to simulate

    # Each key object is just a sentinel string; we track calls, not handles.
    def CreateKey(self, root, path):  # noqa: N802 (winreg API name)
        self.created_keys.append((root, path))
        return f"key:{path}"

    def OpenKey(self, root, path, reserved, access):  # noqa: N802
        self.opened_keys.append((root, path, access))
        return f"key:{path}"

    def SetValueEx(self, key, name, reserved, type_, value):  # noqa: N802
        self.set_values.append((key, name, type_, value))

    def DeleteValue(self, key, name):  # noqa: N802
        if self.delete_raises is not None:
            raise self.delete_raises
        self.deleted_values.append((key, name))

    def CloseKey(self, key):  # noqa: N802
        self.closed += 1


@pytest.fixture()
def fake_winreg(monkeypatch):
    fake = _FakeWinreg()
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
    return fake


def test_apply_autostart_enable_writes_run_key(qapp, fake_winreg):
    app = _bare_controller(qapp)
    app._apply_autostart(True)

    assert fake_winreg.created_keys == [
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ]
    assert len(fake_winreg.set_values) == 1
    _key, name, type_, value = fake_winreg.set_values[0]
    assert name == "MyVoice"
    assert type_ == fake_winreg.REG_SZ
    # Value is the quoted MyVoice.exe launcher path.
    assert value.startswith('"') and value.endswith('"')
    assert "MyVoice.exe" in value
    assert fake_winreg.closed == 1


def test_apply_autostart_disable_deletes_run_value(qapp, fake_winreg):
    app = _bare_controller(qapp)
    app._apply_autostart(False)

    assert fake_winreg.opened_keys == [
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run",
         fake_winreg.KEY_SET_VALUE),
    ]
    assert fake_winreg.deleted_values == [("key:" + r"Software\Microsoft\Windows\CurrentVersion\Run", "MyVoice")]
    assert fake_winreg.closed == 1


def test_apply_autostart_disable_tolerates_missing_value(qapp, fake_winreg):
    fake_winreg.delete_raises = FileNotFoundError()
    app = _bare_controller(qapp)
    # Must not raise even when the value was never present.
    app._apply_autostart(False)
    assert fake_winreg.deleted_values == []


# ------------------------------------------------------------------- misc wiring

def test_toggle_listening_is_noop_while_shutting_down(qapp):
    app = _bare_controller(qapp)
    app.shutting_down = True
    app.start_listening = mock.Mock()
    app.stop_listening = mock.Mock()
    app.toggle_listening()
    app.start_listening.assert_not_called()
    app.stop_listening.assert_not_called()


def test_request_quit_is_idempotent(qapp):
    app = _bare_controller(qapp)
    app._begin_shutdown_ui = mock.Mock()
    app.request_quit()
    app.request_quit()  # second call must be ignored
    assert app.shutting_down is True
    # Only one shutdown UI hand-off should have been posted/queued.
    assert _pump(qapp, lambda: app._begin_shutdown_ui.call_count >= 1)
    qapp.processEvents()
    assert app._begin_shutdown_ui.call_count == 1
