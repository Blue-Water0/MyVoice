"""Microphone capture at 16 kHz mono int16. Uses sounddevice (PortAudio).

Emits frames of exactly FRAME_BYTES from a background stream. Also emits
RMS level for the UI level meter.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import numpy as np

from .vad_service import FRAME_BYTES, FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)

FrameCallback = Callable[[bytes], None]
RmsCallback = Callable[[float], None]


class AudioServiceError(RuntimeError):
    pass


def list_input_devices() -> list[dict]:
    """Return [{index, name, default}] for available input devices."""
    try:
        import sounddevice as sd  # type: ignore
    except Exception as e:
        log.warning("sounddevice unavailable: %s", e)
        return []
    devices = []
    try:
        default_input = sd.default.device[0] if sd.default.device else None
    except Exception:
        default_input = None
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                devices.append({
                    "index": i,
                    "name": d.get("name", f"Device {i}"),
                    "default": (i == default_input),
                })
    except Exception as e:
        log.warning("query_devices failed: %s", e)
    return devices


class AudioService:
    """
    Continuous mic capture into fixed-size frames.

    Usage:
        svc = AudioService()
        svc.start(device_name_or_None, on_frame=..., on_rms=...)
        ...
        svc.stop()
    """

    def __init__(self) -> None:
        self._stream = None
        self._on_frame: Optional[FrameCallback] = None
        self._on_rms: Optional[RmsCallback] = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        # Serializes start()/stop() so a racing pair of calls (e.g. a fast
        # double hotkey toggle) can't both pass the not-active check and
        # each open a real device stream -- the second would silently
        # replace self._stream, orphaning the first (never closed).
        self._transition_lock = threading.Lock()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(
        self,
        device: str | int | None,
        on_frame: FrameCallback,
        on_rms: Optional[RmsCallback] = None,
    ) -> None:
        with self._transition_lock:
            if self._active:
                return
            try:
                import sounddevice as sd  # type: ignore
            except Exception as e:
                raise AudioServiceError(f"sounddevice unavailable: {e}") from e

            self._on_frame = on_frame
            self._on_rms = on_rms
            self._buf.clear()

            def _callback(indata, frames, time_info, status):  # noqa: ANN001
                if status:
                    # xrun / overflow etc — logged but not fatal
                    log.debug("Audio status: %s", status)
                # indata is float32 by default; we requested int16 -> ndarray of int16
                data_bytes = bytes(indata)
                with self._lock:
                    self._buf.extend(data_bytes)
                    # Emit exact 30ms int16 frames
                    while len(self._buf) >= FRAME_BYTES:
                        frame = bytes(self._buf[:FRAME_BYTES])
                        del self._buf[:FRAME_BYTES]
                        if self._on_frame is not None:
                            try:
                                self._on_frame(frame)
                            except Exception:
                                log.exception("on_frame callback raised")
                if self._on_rms is not None:
                    try:
                        arr = np.frombuffer(data_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                        if arr.size:
                            rms = float(np.sqrt(np.mean(arr * arr)))
                            self._on_rms(rms)
                    except Exception:
                        log.exception("on_rms callback raised")

            try:
                stream = sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=FRAME_SAMPLES,   # request 30ms
                    dtype="int16",
                    channels=1,
                    device=device,
                    callback=_callback,
                )
                stream.start()
                self._stream = stream
                self._active = True
                log.info("Audio capture started (device=%s)", device)
            except Exception as e:
                self._stream = None
                self._active = False
                raise AudioServiceError(f"Failed to open microphone: {e}") from e

    def stop(self) -> None:
        with self._transition_lock:
            if not self._active:
                return
            s = self._stream
            self._stream = None
            self._active = False
            try:
                if s is not None:
                    s.stop()
                    s.close()
            except Exception:
                log.exception("Error closing audio stream")
        with self._lock:
            self._buf.clear()
        log.info("Audio capture stopped")
