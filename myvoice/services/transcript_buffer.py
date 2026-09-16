"""In-memory transcript buffer. Handles whitespace normalization and joining.

Kept as pure logic (no GTK) so it can be unit-tested. RTL-safe: we operate on
logical Unicode order; combining spaces are ASCII-safe.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List


_SENT_END = ".!?\u061f\u06d4"   # . ! ? Arabic ? and Arabic full stop


def _normalize_chunk(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    # Collapse internal whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def join_chunks(previous: str, incoming: str) -> str:
    """Return the concatenation with a sensible single-space glue."""
    inc = _normalize_chunk(incoming)
    if not inc:
        return previous
    if not previous:
        return inc
    # If previous ends with sentence terminator, capitalize? We don't force
    # capitalization for RTL languages; Whisper output handles capitalization.
    if previous.endswith((" ", "\n", "\t")):
        return previous + inc
    # Punctuation shouldn't be prefixed with a space
    if inc[0] in ",.;:!?)]\u061f\u06d4":
        return previous + inc
    return previous + " " + inc


class TranscriptBuffer:
    """Session-lifetime transcript."""

    def __init__(self) -> None:
        self._parts: List[str] = []
        self._text: str = ""

    @property
    def text(self) -> str:
        return self._text

    def append(self, chunk: str) -> str:
        """Append chunk, return the *added* text (with any glue space)."""
        inc = _normalize_chunk(chunk)
        if not inc:
            return ""
        prev = self._text
        new_full = join_chunks(prev, inc)
        added = new_full[len(prev):]
        self._parts.append(inc)
        self._text = new_full
        return added

    def clear(self) -> None:
        self._parts.clear()
        self._text = ""
