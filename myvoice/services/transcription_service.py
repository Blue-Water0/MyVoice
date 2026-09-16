"""Ordered transcription worker.

- Single background thread ensures FIFO ordering (Whisper isn't streaming; we
  transcribe finalized VAD segments in order).
- Segments come in as raw int16 mono 16kHz bytes; converted to numpy here.
- Emits transcribed text via a callback invoked on the worker thread; callers
  are expected to marshal onto the GTK main thread via GLib.idle_add.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from ..engines.base import SpeechEngine
from .vad_service import Segment

log = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    text: str
    duration_ms: int
    reason: str  # segment finalize reason
    ok: bool
    error: Optional[str] = None


TextCallback = Callable[[TranscriptionResult], None]


class TranscriptionService:
    def __init__(
        self,
        engine: SpeechEngine,
        on_result: TextCallback,
        ready_event: Optional[threading.Event] = None,
    ) -> None:
        self._engine = engine
        self._on_result = on_result
        self._queue: "queue.Queue[Optional[Segment]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._language = "en"
        self._prompt: Optional[str] = None
        # If provided, the worker will not call engine.transcribe() until the
        # event is set. Segments submitted meanwhile remain queued in FIFO
        # order and are transcribed as soon as the engine is ready.
        self._ready = ready_event

    def set_language(self, code: str) -> None:
        self._language = code

    def set_initial_prompt(self, prompt: Optional[str]) -> None:
        self._prompt = prompt

    def set_ready_event(self, event: Optional[threading.Event]) -> None:
        self._ready = event

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="myvoice-transcriber", daemon=True
        )
        self._thread.start()
        log.info("Transcription worker started")

    def stop(self, join_timeout: float = 5.0) -> None:
        """Ask the worker to stop after processing the current queue.

        The worker will *not* wait indefinitely for a not-yet-set ready event;
        stop() unblocks it via the internal stop flag.
        """
        self._stop.set()
        self._queue.put(None)
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=join_timeout)
        self._thread = None
        log.info("Transcription worker stopped")

    def request_drain_and_stop(
        self, ready_wait: float = 5.0, join_timeout: float = 60.0
    ) -> None:
        """Cooperative stop that gives the engine a chance to load first.

        If a ready_event was configured and is not yet set, wait up to
        ``ready_wait`` seconds for it. Then submit the sentinel and join for
        up to ``join_timeout`` seconds so already-queued segments have time
        to transcribe. Never blocks longer than ``ready_wait + join_timeout``.
        """
        if self._ready is not None and not self._ready.is_set():
            log.info("stop: waiting up to %.1fs for engine ready", ready_wait)
            got = self._ready.wait(timeout=ready_wait)
            if not got:
                log.warning("stop: engine not ready within %.1fs — dropping "
                            "%d queued segment(s)", ready_wait, self._queue.qsize())
        self._stop.set()
        self._queue.put(None)
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=join_timeout)
        self._thread = None
        log.info("Transcription worker stopped (drain)")

    def submit(self, seg: Segment) -> None:
        self._queue.put(seg)

    def pending_count(self) -> int:
        return self._queue.qsize()

    def _wait_ready(self) -> bool:
        """Block (with periodic stop-check) until the ready event is set.

        Returns False if we were asked to stop before becoming ready.
        """
        if self._ready is None:
            return True
        while not self._ready.is_set():
            if self._stop.is_set():
                return False
            # Short poll so stop() can interrupt in reasonable time.
            self._ready.wait(timeout=0.2)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                seg = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if seg is None:
                break
            if not self._wait_ready():
                break
            self._process(seg)

    def _process(self, seg: Segment) -> None:
        try:
            audio = np.frombuffer(seg.pcm16, dtype=np.int16)
            text = self._engine.transcribe(audio, language=self._language, initial_prompt=self._prompt)
            text = text.strip()
            self._on_result(TranscriptionResult(
                text=text, duration_ms=seg.duration_ms, reason=seg.reason, ok=True,
            ))
        except Exception as e:  # engine failure isolated to this segment
            log.exception("Transcription failed: %s", e)
            self._on_result(TranscriptionResult(
                text="", duration_ms=seg.duration_ms, reason=seg.reason,
                ok=False, error=str(e),
            ))
