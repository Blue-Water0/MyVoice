"""Speech engine interface. Concrete engines live alongside this file."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class ModelPreset:
    """UI-facing model preset info."""
    key: str
    label: str
    approx_download_mb: int
    approx_ram_mb: int
    notes: str  # human note about quality/perf


@dataclass(frozen=True)
class EngineInfo:
    key: str
    label: str
    presets: tuple[ModelPreset, ...]
    default_preset: str


class SpeechEngine(ABC):
    """
    Concrete engines must be safe to call from a single worker thread.

    load()/unload() may be expensive. transcribe() gets int16 mono PCM at
    16 kHz; engines that require float32/other rates convert internally.
    """

    info: EngineInfo  # class attribute on subclasses

    @abstractmethod
    def load(self, model_key: str, compute_type: str = "auto") -> None:
        ...

    @abstractmethod
    def unload(self) -> None:
        ...

    @abstractmethod
    def is_loaded(self) -> bool:
        ...

    @abstractmethod
    def transcribe(
        self,
        pcm16_mono_16k: np.ndarray,
        language: str,
        initial_prompt: Optional[str] = None,
    ) -> str:
        """Return transcription text (may be empty). Must not raise on empty/no-speech."""
