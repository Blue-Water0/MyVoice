"""Segmenter logic tests using a fake VAD (webrtcvad not required)."""
from __future__ import annotations

from myvoice.services.vad_service import (
    FRAME_BYTES,
    FRAME_MS,
    Segment,
    Segmenter,
)


class FakeVad:
    """Speech = frame[0] byte is 1."""

    def is_speech(self, frame: bytes, sr: int) -> bool:
        return frame[0] == 1


def _frame(speech: bool) -> bytes:
    b = bytearray(FRAME_BYTES)
    b[0] = 1 if speech else 0
    return bytes(b)


def _run(seg: Segmenter, pattern: list[bool]) -> list[Segment]:
    out = []
    for s in pattern:
        r = seg.push_frame(_frame(s))
        if r is not None:
            out.append(r)
    return out


def test_short_speech_below_min_is_discarded():
    seg = Segmenter(FakeVad(), min_speech_ms=300, silence_ms=200, max_segment_ms=5000, pre_roll_ms=0)
    # 5 speech frames = 150ms then silence
    pattern = [True] * 5 + [False] * 20
    out = _run(seg, pattern)
    assert out == []


def test_normal_utterance_finalized_by_silence():
    seg = Segmenter(FakeVad(), min_speech_ms=200, silence_ms=210, max_segment_ms=5000, pre_roll_ms=0)
    # 30 speech frames = 900ms then 8 silence = 240ms
    pattern = [True] * 30 + [False] * 8
    out = _run(seg, pattern)
    assert len(out) == 1
    assert out[0].reason == "silence"
    assert out[0].duration_ms >= 900


def test_max_segment_cuts_long_speech():
    seg = Segmenter(FakeVad(), min_speech_ms=100, silence_ms=500, max_segment_ms=300, pre_roll_ms=0)
    # 20 speech frames = 600ms, should cut at 300ms
    out = _run(seg, [True] * 20)
    assert len(out) >= 1
    assert out[0].reason == "max_length"
    assert out[0].duration_ms >= 300


def test_flush_emits_pending_speech():
    seg = Segmenter(FakeVad(), min_speech_ms=100, silence_ms=1000, max_segment_ms=5000, pre_roll_ms=0)
    _run(seg, [True] * 20)  # 600ms speech, no silence => nothing yet
    flushed = seg.flush()
    assert flushed is not None
    assert flushed.reason == "flush"


def test_pre_roll_prepends_frames():
    seg = Segmenter(FakeVad(), min_speech_ms=100, silence_ms=200, max_segment_ms=5000,
                    pre_roll_ms=90)  # 3 frames pre-roll
    # 5 silence (fills pre-roll buffer), 20 speech, 8 silence
    pattern = [False] * 5 + [True] * 20 + [False] * 8
    out = _run(seg, pattern)
    assert len(out) == 1
    # 20 speech + 3 pre-roll + up to 8 trailing silence frames = >= 23 frames
    n_frames = out[0].duration_ms // FRAME_MS
    assert n_frames >= 23


def test_multiple_segments_in_sequence():
    seg = Segmenter(FakeVad(), min_speech_ms=100, silence_ms=200, max_segment_ms=5000, pre_roll_ms=0)
    pattern = ([True] * 20 + [False] * 10) * 2
    out = _run(seg, pattern)
    assert len(out) == 2
    assert all(s.reason == "silence" for s in out)


def test_wrong_frame_size_is_ignored():
    seg = Segmenter(FakeVad())
    assert seg.push_frame(b"\x01\x02\x03") is None
