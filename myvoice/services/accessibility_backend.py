"""AT-SPI2 EditableText insertion backend.

Insert text at a specific character offset (or the current caret) of the
given Accessible without replacing existing content. Returns the new
character offset just after the inserted text on success, or None on any
failure so the router can fall through / buffer.

All offsets are **character offsets** (Python code-points). AT-SPI's
``insertText(position, string, length)`` uses UTF-8-independent character
positions, and Python ``len(str)`` returns code-point count, which matches
the character-position semantics AT-SPI expects. This matters for Hebrew,
Arabic, and any non-ASCII text.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


def _live_caret_offset(text_iface: Any) -> Optional[int]:
    try:
        v = int(getattr(text_iface, "caretOffset", -1))
    except Exception:
        return None
    return v if v >= 0 else None


def _character_count(text_iface: Any) -> Optional[int]:
    try:
        return int(text_iface.characterCount)
    except Exception:
        return None


def insert_via_atspi_at(
    accessible: Any,
    text: str,
    at_char_offset: Optional[int],
) -> Optional[int]:
    """Insert ``text`` into ``accessible`` at a specific character offset.

    Parameters:
        accessible: pyatspi Accessible implementing EditableText.
        text: Unicode string to insert (Python str; no byte conversion).
        at_char_offset: character offset to insert at, or ``None`` to use
            the accessible's current caret offset. If the caret cannot be
            read, we append at the end of the field's current contents.

    Returns:
        The character offset immediately after the inserted text on
        success, or ``None`` if AT-SPI insertion failed for any reason.
    """
    if not accessible or not text:
        return None
    try:
        editable = accessible.queryEditableText()  # type: ignore[attr-defined]
    except Exception as e:
        log.info("insert_via_atspi_at: queryEditableText failed: %s "
                 "(target has no EditableText interface at insert time)", e)
        return None
    if editable is None:
        log.info("insert_via_atspi_at: queryEditableText returned None")
        return None

    try:
        text_iface = accessible.queryText()  # type: ignore[attr-defined]
    except Exception:
        text_iface = editable  # some impls unify

    # Decide insertion offset in *characters*.
    offset = at_char_offset
    if offset is None:
        offset = _live_caret_offset(text_iface)
    if offset is None:
        # Fall back to end of field.
        cc = _character_count(text_iface)
        offset = cc if cc is not None else 0
    if offset < 0:
        offset = 0

    n_chars = len(text)  # Python len() = code-point count

    try:
        # AT-SPI insertText signature: (position, string, length)
        ok = editable.insertText(offset, text, n_chars)
        # Some bindings return None (void); assume success unless it raised
        # or explicitly returned False.
        if ok is False:
            log.info(
                "insert_via_atspi_at: editable.insertText returned False "
                "(target refused insertion; offset=%d chars=%d)",
                offset, n_chars,
            )
            return None
        # Best-effort caret move so subsequent user typing continues after
        # our inserted chunk. Not fatal if unsupported.
        try:
            text_iface.setCaretOffset(offset + n_chars)
        except Exception:
            pass
        log.debug(
            "insert_via_atspi_at: OK offset=%d chars=%d new_offset=%d",
            offset, n_chars, offset + n_chars,
        )
        return offset + n_chars
    except Exception as e:
        log.info(
            "insert_via_atspi_at: insertText raised: %s "
            "(offset=%d chars=%d)", e, offset, n_chars,
        )
        return None


# Legacy shim for any external caller that still imports the old name.
def insert_via_atspi(accessible: Any, text: str) -> bool:
    return insert_via_atspi_at(accessible, text, None) is not None


def read_text_tail(accessible: Any, max_chars: int = 600) -> Optional[str]:
    """Best-effort read of the last ``max_chars`` of ``accessible``'s
    exposed text via the read-only AT-SPI Text interface.

    Used to verify that a clipboard+XTEST paste actually landed in
    targets — like terminal emulators — that expose ``Text`` for
    screen-reader accessibility but not ``EditableText`` (so they never
    go through ``insert_via_atspi_at`` above). ``xdotool`` reporting the
    paste keystroke as sent does not mean the target actually processed
    it as a paste; reading the content back is the only real signal.

    Returns ``None`` if the accessible has no readable Text interface at
    all — callers must treat that as "unverifiable", not "failed", since
    plenty of writable targets legitimately expose neither interface.
    """
    if accessible is None:
        return None
    try:
        text_iface = accessible.queryText()  # type: ignore[attr-defined]
    except Exception:
        return None
    if text_iface is None:
        return None
    try:
        count = int(text_iface.characterCount)
    except Exception:
        return None
    start = max(0, count - max_chars)
    try:
        return text_iface.getText(start, count)
    except Exception:
        return None
