"""TranscriptionService must queue segments while ready_event is not set,
and drain them (in order) once it is set."""
from __future__ import annotations

import threading
import time

from myvoice.engines.base import EngineInfo, SpeechEngine
from myvoice.services.transcription_service import TranscriptionService
from myvoice.services.vad_service import Segment


class RecordingEngine(SpeechEngine):
    info = EngineInfo(key="rec", label="rec", presets=(), default_preset="")

    def __init__(self) -> None:
        self._loaded = True
        self.calls: list[int] = []

    def load(self, k, ct="auto"): pass  # noqa: E704
    def unload(self): pass              # noqa: E704
    def is_loaded(self): return self._loaded  # noqa: E704

    def transcribe(self, pcm, language, initial_prompt=None):
        # Return a distinguishable text per call
        n = len(self.calls) + 1
        self.calls.append(n)
        return f"seg-{n}"


def _seg(i: int) -> Segment:
    return Segment(pcm16=(bytes([i, 0]) * 8), duration_ms=300, reason="silence")


def test_segments_are_queued_until_ready_event_set():
    engine = RecordingEngine()
    ready = threading.Event()
    results: list[str] = []
    done = threading.Event()

    def cb(r):
        results.append(r.text)
        if len(results) >= 3:
            done.set()

    svc = TranscriptionService(engine, on_result=cb, ready_event=ready)
    svc.start()
    # Submit before ready
    svc.submit(_seg(1))
    svc.submit(_seg(2))
    svc.submit(_seg(3))
    time.sleep(0.4)
    assert results == [], "no transcription should happen before ready"
    assert svc.pending_count() >= 1, "segments should be queued"

    # Fire ready
    ready.set()
    assert done.wait(timeout=2.0), "should transcribe after ready"
    assert results == ["seg-1", "seg-2", "seg-3"], "must preserve FIFO order"
    svc.stop()


def test_stop_before_ready_does_not_hang():
    engine = RecordingEngine()
    ready = threading.Event()   # never set
    results: list[str] = []
    svc = TranscriptionService(engine, on_result=results.append, ready_event=ready)
    svc.start()
    svc.submit(_seg(1))
    time.sleep(0.1)
    t0 = time.monotonic()
    svc.stop(join_timeout=2.0)  # must not hang
    assert time.monotonic() - t0 < 2.0
    assert results == []


def test_no_ready_event_behaves_normally():
    engine = RecordingEngine()
    results: list[str] = []
    done = threading.Event()

    def cb(r):
        results.append(r.text)
        done.set()

    svc = TranscriptionService(engine, on_result=cb)  # no ready_event
    svc.start()
    svc.submit(_seg(1))
    assert done.wait(timeout=2.0)
    assert results == ["seg-1"]
    svc.stop()
