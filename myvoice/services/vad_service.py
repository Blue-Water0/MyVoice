"""Voice-activity-driven segmenter.

Pure logic — no threads, no audio libs. Feed 30 ms int16 mono 16 kHz frames.
Emits a Segment when speech ends (silence_ms trailing silence) OR when the
current speech run exceeds max_segment_ms (forced cut).

Small overlap is added at the head of each segment via a rolling pre-buffer
so the first phoneme after silence isn't clipped.
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 480
FRAME_BYTES = FRAME_SAMPLES * 2                   # int16 = 2 bytes


@dataclass
class Segment:
    """A finalized run of speech. `pcm16` is raw bytes, 16 kHz mono int16."""
    pcm16: bytes
    duration_ms: int
    reason: str  # 'silence' | 'max_length' | 'flush'


@dataclass
class _State:
    speech_frames: list[bytes] = field(default_factory=list)
    silence_run_ms: int = 0
    speech_ms: int = 0            # total collected audio ms (incl. trailing silence)
    pure_speech_ms: int = 0       # only frames the VAD flagged as speech
    in_speech: bool = False


class VadDetector:
    """
    Tiny protocol wrapper. In production we use webrtcvad; tests can inject a
    fake by passing an object with `is_speech(frame_bytes, sample_rate) -> bool`.
    """

    def __init__(self, aggressiveness: int = 2):
        try:
            import webrtcvad  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "webrtcvad not installed. Install via install.sh."
            ) from e
        self._vad = webrtcvad.Vad(max(0, min(3, aggressiveness)))

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return self._vad.is_speech(frame, sample_rate)


class Segmenter:
    """
    Feed 30 ms int16 frames. Returns a Segment when one completes, else None.

    Parameters:
        vad: object with is_speech(frame, sr)
        min_speech_ms: minimum accumulated speech before we bother emitting
        silence_ms: trailing silence duration that finalizes a segment
        max_segment_ms: force emit if current speech run exceeds this
        pre_roll_ms: audio kept before speech onset to avoid clipping first syllable
    """

    def __init__(
        self,
        vad: object,
        *,
        min_speech_ms: int = 300,
        silence_ms: int = 700,
        max_segment_ms: int = 12000,
        pre_roll_ms: int = 240,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = FRAME_MS,
    ) -> None:
        self._vad = vad
        self._sr = sample_rate
        self._frame_ms = frame_ms
        self._min_speech_ms = min_speech_ms
        self._silence_ms = silence_ms
        self._max_segment_ms = max_segment_ms
        self._pre_roll_frames = max(0, pre_roll_ms // frame_ms)
        self._preroll: collections.deque[bytes] = collections.deque(maxlen=self._pre_roll_frames)
        self._state = _State()

    def reset(self) -> None:
        self._state = _State()
        self._preroll.clear()

    def _finalize(self, reason: str) -> Optional[Segment]:
        st = self._state
        pcm = b"".join(st.speech_frames)
        dur = st.speech_ms
        pure = st.pure_speech_ms
        self._state = _State()
        if pure < self._min_speech_ms:
            log.debug("Discarded short segment: pure_speech=%d ms (< %d)",
                      pure, self._min_speech_ms)
            return None
        return Segment(pcm16=pcm, duration_ms=dur, reason=reason)

    def push_frame(self, frame: bytes) -> Optional[Segment]:
        if len(frame) != FRAME_BYTES:
            # We tolerate but log — audio_service should already size correctly.
            log.debug("Unexpected frame size %d (want %d)", len(frame), FRAME_BYTES)
            return None

        is_speech = self._vad.is_speech(frame, self._sr)  # type: ignore[attr-defined]
        st = self._state

        if is_speech:
            if not st.in_speech:
                # Speech onset: prepend pre-roll.
                st.speech_frames.extend(self._preroll)
                st.speech_ms += len(self._preroll) * self._frame_ms
                st.in_speech = True
            st.speech_frames.append(frame)
            st.speech_ms += self._frame_ms
            st.pure_speech_ms += self._frame_ms
            st.silence_run_ms = 0

            if st.speech_ms >= self._max_segment_ms:
                return self._finalize("max_length")
            return None

        # Silence frame
        self._preroll.append(frame)
        if st.in_speech:
            st.silence_run_ms += self._frame_ms
            # Keep a little trailing silence in the segment for natural cutoff.
            st.speech_frames.append(frame)
            st.speech_ms += self._frame_ms
            if st.silence_run_ms >= self._silence_ms:
                return self._finalize("silence")
        return None

    def flush(self) -> Optional[Segment]:
        """Force-emit any pending speech (call on Stop)."""
        if self._state.speech_frames:
            return self._finalize("flush")
        return None
