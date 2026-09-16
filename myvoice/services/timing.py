"""Lightweight monotonic timing helper for start/stop latency diagnostics.

Usage:
    from myvoice.services.timing import Timer, tlog

    t = Timer("dictation")
    t.mark("start_listening")
    ...
    t.mark("overlay_shown")

All entries are logged at DEBUG level with elapsed ms since the timer began
and since the previous mark.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("myvoice.timing")


class Timer:
    """Named phase timer using monotonic clock. Thread-safe for concurrent marks."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._t0 = time.monotonic()
        self._last = self._t0
        self._lock = threading.Lock()

    def name(self) -> str:
        return self._name

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def mark(self, event: str, extra: Optional[str] = None) -> float:
        """Log a timing event. Returns elapsed ms since timer start."""
        now = time.monotonic()
        with self._lock:
            delta = (now - self._last) * 1000.0
            total = (now - self._t0) * 1000.0
            self._last = now
        suffix = f" {extra}" if extra else ""
        log.debug("[%s] +%7.1f ms (Δ%+7.1f ms) %s%s",
                  self._name, total, delta, event, suffix)
        return total

    def reset(self) -> None:
        with self._lock:
            self._t0 = time.monotonic()
            self._last = self._t0


def tlog(timer: Optional[Timer], event: str, extra: Optional[str] = None) -> None:
    """Convenience: mark on timer if provided, else no-op."""
    if timer is not None:
        timer.mark(event, extra)
