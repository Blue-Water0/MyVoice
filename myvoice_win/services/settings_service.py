"""User settings persistence (Windows build).

Atomic writes (tmpfile+rename), schema versioning, safe defaults. Never
raises on read errors — falls back to defaults and logs. Mirrors
``myvoice.services.settings_service`` field-for-field so a settings.json
produced by either platform is a drop-in for the other; only the backing
``settings_file()`` path differs (``%APPDATA%`` vs. XDG).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..paths import settings_file

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class VadSettings:
    aggressiveness: int = 2       # webrtcvad 0..3
    min_speech_ms: int = 300
    silence_ms: int = 700
    max_segment_ms: int = 12000


@dataclass
class Settings:
    schema_version: int = SCHEMA_VERSION
    language_mode: str = "auto"        # auto|en|he|ar
    # Placeholder format pending Task 3's hotkey parser definition.
    hotkey: str = "win+shift+space"
    microphone: str | None = None      # None = default device
    start_minimized: bool = False
    autostart: bool = False
    engine: str = "faster-whisper"
    model: str = "medium"              # small|medium|large-v3|large-v3-turbo
    compute_type: str = "auto"         # auto|int8|int8_float32|float32
    # Preferred application for the "Open in Text App" transcript action.
    # Empty string means "system default for text/plain". Otherwise a
    # freedesktop desktop-file id like "org.x.editor.desktop" (kept as-is
    # for schema parity with the Linux settings file; unused on Windows).
    text_app_desktop_id: str = ""
    # Extra WM_CLASS instance/class values to classify as terminal emulators.
    # Matched case-insensitively by app_classifier.classify(). Users can add
    # uncommon terminal WM_CLASS names here via the settings JSON.
    terminal_wm_classes: list[str] = field(default_factory=list)
    vad: VadSettings = field(default_factory=VadSettings)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        # Tolerate missing/extra keys. Any keys not on the dataclass —
        # including obsolete keys from removed features — are silently
        # ignored.
        base = cls()
        for k, v in d.items():
            if k == "vad" and isinstance(v, dict):
                vad = VadSettings()
                for vk, vv in v.items():
                    if hasattr(vad, vk):
                        setattr(vad, vk, vv)
                base.vad = vad
            elif hasattr(base, k):
                setattr(base, k, v)
        # Clamp/validate
        if base.language_mode not in ("auto", "en", "he", "ar"):
            base.language_mode = "auto"
        if base.model not in ("small", "medium", "large-v3-turbo"):
            base.model = "medium"
        if base.compute_type not in ("auto", "int8", "int8_float32", "float32"):
            base.compute_type = "auto"
        base.vad.aggressiveness = max(0, min(3, int(base.vad.aggressiveness)))
        base.vad.min_speech_ms = max(50, int(base.vad.min_speech_ms))
        base.vad.silence_ms = max(100, int(base.vad.silence_ms))
        base.vad.max_segment_ms = max(1000, int(base.vad.max_segment_ms))
        if not isinstance(base.terminal_wm_classes, list):
            base.terminal_wm_classes = []
        return base


class SettingsService:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings_file()
        self._settings: Settings = self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def settings(self) -> Settings:
        return self._settings

    def _load(self) -> Settings:
        if not self._path.exists():
            log.info("No settings file, using defaults (%s)", self._path)
            return Settings()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Failed to read settings (%s); using defaults", e)
            return Settings()
        if not isinstance(data, dict):
            log.warning("Settings file not a dict; using defaults")
            return Settings()
        return Settings.from_dict(data)

    def save(self, settings: Settings | None = None) -> None:
        if settings is not None:
            self._settings = settings
        payload = json.dumps(self._settings.to_dict(), indent=2, ensure_ascii=False)
        # Atomic write
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        log.debug("Settings saved to %s", self._path)

    def update(self, **kwargs: Any) -> Settings:
        new = copy.deepcopy(self._settings)
        for k, v in kwargs.items():
            if hasattr(new, k):
                setattr(new, k, v)
        self.save(new)
        return new
