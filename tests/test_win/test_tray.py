"""Tests for the Windows tray indicator (PySide6 QSystemTrayIcon).

Mirrors the callback surface of ``myvoice/ui/tray.py``'s ``TrayIndicator``:
``on_toggle_show``, ``on_start``, ``on_stop``, ``on_quit``,
``set_listening(bool)``, ``destroy()``. Real ``QMenu``/``QAction`` objects,
headless via the shared ``qapp`` fixture -- no mocking of Qt itself.

Note: ``QSystemTrayIcon.isSystemTrayAvailable()`` is False under the
offscreen platform plugin used in this sandbox, so we don't assert
``isVisible()`` after construction -- we assert against the real QAction
objects in the real QMenu instead, which is backend-independent.
"""
from __future__ import annotations

from myvoice_win.ui.tray import TrayIndicator


def _make_tray(**overrides):
    calls = {"toggle": [], "start": [], "stop": [], "quit": []}
    kwargs = dict(
        on_toggle_show=lambda: calls["toggle"].append(True),
        on_start=lambda: calls["start"].append(True),
        on_stop=lambda: calls["stop"].append(True),
        on_quit=lambda: calls["quit"].append(True),
    )
    kwargs.update(overrides)
    tray = TrayIndicator(**kwargs)
    return tray, calls


def test_menu_has_show_start_stop_quit_actions(qapp):
    tray, _calls = _make_tray()
    labels = [a.text() for a in tray._menu.actions() if not a.isSeparator()]
    assert labels == [
        "Show MyVoice", "Start Listening", "Stop Listening", "Quit MyVoice",
    ]


def test_show_action_triggers_on_toggle_show(qapp):
    tray, calls = _make_tray()
    tray._show_action.trigger()
    assert calls["toggle"] == [True]


def test_start_action_triggers_on_start(qapp):
    tray, calls = _make_tray()
    tray._start_action.trigger()
    assert calls["start"] == [True]


def test_stop_action_triggers_on_stop(qapp):
    tray, calls = _make_tray()
    # Stop starts disabled (not listening yet); a disabled QAction's
    # .trigger() is a no-op, matching real menu-click behavior -- so
    # enable it the same way set_listening(True) would first.
    tray.set_listening(True)
    tray._stop_action.trigger()
    assert calls["stop"] == [True]


def test_quit_action_triggers_on_quit(qapp):
    tray, calls = _make_tray()
    tray._quit_action.trigger()
    assert calls["quit"] == [True]


def test_initial_state_start_enabled_stop_disabled(qapp):
    tray, _calls = _make_tray()
    assert tray._start_action.isEnabled() is True
    assert tray._stop_action.isEnabled() is False


def test_set_listening_true_disables_start_enables_stop(qapp):
    tray, _calls = _make_tray()
    tray.set_listening(True)
    assert tray._start_action.isEnabled() is False
    assert tray._stop_action.isEnabled() is True


def test_set_listening_false_reenables_start_disables_stop(qapp):
    tray, _calls = _make_tray()
    tray.set_listening(True)
    tray.set_listening(False)
    assert tray._start_action.isEnabled() is True
    assert tray._stop_action.isEnabled() is False


def test_destroy_hides_the_tray_icon(qapp, monkeypatch):
    tray, _calls = _make_tray()
    calls = []
    monkeypatch.setattr(tray._tray_icon, "hide", lambda: calls.append(True))
    tray.destroy()
    assert calls == [True]
