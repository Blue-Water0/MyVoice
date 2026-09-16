"""Concurrency safety for AudioService start/stop.

Regression coverage for the freeze on 2026-07-19: a fast double hotkey
toggle raced start_listening() against the previous session's async
teardown, and AudioService.start()'s check-then-act on self._active let
two threads both open a real PortAudio stream. The second stream silently
replaced self._stream, orphaning the first (never closed) and wedging the
microphone device.
"""
from __future__ import annotations

import sys
import threading
import time
from unittest.mock import MagicMock

from myvoice.services.audio_service import AudioService


class _FakeStream:
    """Stands in for sd.RawInputStream. Sleeps during open to widen the
    race window, the same way a real PortAudio device-open does."""

    def __init__(self, *args, **kwargs):
        time.sleep(0.05)
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True


def _install_fake_sounddevice(monkeypatch):
    created: list[_FakeStream] = []

    def _factory(*args, **kwargs):
        s = _FakeStream(*args, **kwargs)
        created.append(s)
        return s

    fake_sd = MagicMock()
    fake_sd.RawInputStream = _factory
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    return created


def test_concurrent_start_calls_do_not_open_two_streams(monkeypatch):
    created = _install_fake_sounddevice(monkeypatch)
    svc = AudioService()
    barrier = threading.Barrier(2)

    def _start():
        barrier.wait()
        svc.start(None, on_frame=lambda f: None)

    t1 = threading.Thread(target=_start)
    t2 = threading.Thread(target=_start)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(created) == 1, (
        f"expected exactly one PortAudio stream to be opened, got "
        f"{len(created)} -- a losing start() call opened a real stream "
        f"that self._stream then silently overwrote, orphaning it"
    )
