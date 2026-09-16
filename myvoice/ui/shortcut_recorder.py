"""Reusable state machine for "record a shortcut" flows.

Kept UI-independent so we can unit-test the accept/cancel/validation logic
without spinning up GTK. The GTK dialog in settings_dialog.py drives this
machine and reflects its state into buttons/labels.

State transitions:

    IDLE      → RECORDING           (start_recording)
    RECORDING → CAPTURED             (feed_key with a valid non-modifier key)
    RECORDING → CANCELLED            (feed_key with Escape)
    RECORDING → RECORDING            (feed_key with a modifier-only press)

The machine never touches X11 itself. The caller decides what to do with the
captured accelerator string (persist to settings, try to grab, etc.).

Reset (Backspace/Delete) behaviour is intentionally NOT baked into the
machine — the UI treats reset as a distinct "Reset to Default" button rather
than a mid-recording gesture, to avoid ambiguity per the requirement's
"choose one clear behaviour and document it".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..services.hotkey_service import (
    ParsedAccel,
    canonical_label,
    ensure_valid,
    is_modifier_keyval_name,
    parsed_from_gdk,
)

log = logging.getLogger(__name__)


class RecorderState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    CAPTURED = "captured"
    CANCELLED = "cancelled"


@dataclass
class KeyEvent:
    """A minimal, GTK-free view of a key press event.

    ``keyval_name`` is Gdk.keyval_name(event.keyval); ``state`` is the
    ModifierType bitmask from the event.
    """
    keyval_name: Optional[str]
    state: int


class ShortcutRecorder:
    def __init__(self) -> None:
        self._state = RecorderState.IDLE
        self._captured: Optional[ParsedAccel] = None

    @property
    def state(self) -> RecorderState:
        return self._state

    @property
    def captured(self) -> Optional[ParsedAccel]:
        return self._captured

    def captured_label(self) -> Optional[str]:
        if self._captured is None:
            return None
        return canonical_label(self._captured)

    def start_recording(self) -> None:
        self._state = RecorderState.RECORDING
        self._captured = None

    def cancel(self) -> None:
        self._state = RecorderState.CANCELLED

    def feed_key(self, event: KeyEvent) -> RecorderState:
        """Consume a key press event. Returns the new state.

        Behaviour:
          * Escape while RECORDING → CANCELLED.
          * Modifier-only press → stay in RECORDING (waiting for a real key).
          * Anything else → parse; if it validates, CAPTURED; else stay in
            RECORDING (bad input silently ignored so the user can try again).
        """
        if self._state != RecorderState.RECORDING:
            return self._state

        name = event.keyval_name

        # Escape cancels.
        if name == "Escape":
            self._state = RecorderState.CANCELLED
            return self._state

        # Modifier-only presses are a no-op; stay recording.
        if is_modifier_keyval_name(name):
            return self._state

        # Try to parse into a ParsedAccel.
        try:
            parsed = parsed_from_gdk(name, event.state)
        except ValueError as e:
            log.debug("shortcut recorder: rejected keyval=%r: %s", name, e)
            return self._state

        if parsed is None:
            # Should not happen because we already filtered modifier-only above,
            # but stay safe.
            return self._state

        try:
            ensure_valid(parsed)
        except ValueError as e:
            log.debug("shortcut recorder: invalid combination: %s", e)
            return self._state

        self._captured = parsed
        self._state = RecorderState.CAPTURED
        return self._state

    def reset(self) -> None:
        self._state = RecorderState.IDLE
        self._captured = None
