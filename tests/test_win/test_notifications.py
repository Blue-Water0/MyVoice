"""Tests for the Windows notification helper (plyer-backed toast).

Mirrors ``tests/test_notifications.py``-style coverage for the Linux
libnotify wrapper (there isn't one today, but the contract is the same:
never raise, forward title/message, ignore ``urgency``).
"""
from __future__ import annotations

import plyer

from myvoice_win.services import notifications as nf


def test_notify_calls_plyer_with_title_and_message(monkeypatch):
    calls = []

    def fake_notify(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(plyer.notification, "notify", fake_notify)

    nf.notify("MyVoice", "Dictation started")

    assert len(calls) == 1
    assert calls[0]["title"] == "MyVoice"
    assert calls[0]["message"] == "Dictation started"


def test_notify_ignores_urgency_kwarg(monkeypatch):
    calls = []
    monkeypatch.setattr(plyer.notification, "notify",
                         lambda **kw: calls.append(kw))

    nf.notify("MyVoice", "boom", urgency="critical")

    assert len(calls) == 1
    # urgency has no Windows-toast equivalent -- never forwarded to plyer.
    assert "urgency" not in calls[0]


def test_notify_swallows_exceptions_from_plyer(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no notification backend")

    monkeypatch.setattr(plyer.notification, "notify", boom)

    # Must not raise -- "never raises" contract, matches the Linux wrapper.
    nf.notify("MyVoice", "this should not blow up")


def test_notify_default_urgency_does_not_require_kwarg(monkeypatch):
    calls = []
    monkeypatch.setattr(plyer.notification, "notify",
                         lambda **kw: calls.append(kw))

    nf.notify("Title only", "Message only")

    assert calls[0] == {"title": "Title only", "message": "Message only"}
