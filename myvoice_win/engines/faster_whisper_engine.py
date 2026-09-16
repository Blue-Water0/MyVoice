"""faster-whisper concrete implementation of SpeechEngine (Windows build).

Uses the HuggingFace CTranslate2 models (Systran/faster-whisper-*). CPU by
default, multilingual only. Language is passed explicitly; task is always
'transcribe' (never 'translate').

Windows equivalent of ``myvoice.engines.faster_whisper_engine``: identical
behavior, the only difference is ``models_cache_dir`` comes from
``myvoice_win.paths`` (Windows %LOCALAPPDATA%-based) instead of
``myvoice.paths`` (Linux XDG-based). ``SpeechEngine``/``EngineInfo``/
``ModelPreset`` are imported directly from ``myvoice.engines.base`` since
that module has no Linux-only or ``myvoice.paths`` dependency.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

from myvoice.engines.base import EngineInfo, ModelPreset, SpeechEngine

from ..paths import models_cache_dir

log = logging.getLogger(__name__)


PRESETS = (
    ModelPreset(
        key="small",
        label="Fast / lower accuracy — small (multilingual)",
        approx_download_mb=470,
        approx_ram_mb=1200,
        notes="Faster, lower Arabic/Hebrew accuracy. Good on modest CPUs.",
    ),
    ModelPreset(
        key="medium",
        label="Recommended — medium (multilingual)",
        approx_download_mb=1500,
        approx_ram_mb=2600,
        notes="Best balance for Arabic and Hebrew. Needs ~4 GB RAM headroom.",
    ),
    ModelPreset(
        key="large-v3-turbo",
        label="Best/fast — large-v3-turbo (multilingual)",
        approx_download_mb=1620,
        approx_ram_mb=4200,
        notes="Best accuracy/speed tradeoff. Recommended if RAM allows.",
    ),
)

INFO = EngineInfo(
    key="faster-whisper",
    label="faster-whisper (local, CTranslate2)",
    presets=PRESETS,
    default_preset="medium",
)


def _cache_env() -> None:
    """Point HF/CTranslate2 caches at our controlled dir so 'clear cache' works."""
    cache = str(models_cache_dir())
    os.environ.setdefault("HF_HOME", cache)
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", cache)
    os.environ.setdefault("XDG_CACHE_HOME", str(models_cache_dir().parent.parent))


class FasterWhisperEngine(SpeechEngine):
    info = INFO

    def __init__(self) -> None:
        self._model = None
        self._model_key: Optional[str] = None
        self._compute_type: Optional[str] = None

    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self, model_key: str, compute_type: str = "auto") -> None:
        _cache_env()
        # Import lazily so unit tests that only touch the interface don't need faster-whisper.
        try:
            from faster_whisper import WhisperModel  # type: ignore
            from huggingface_hub.utils import LocalEntryNotFoundError  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper is not installed. Run install.sh or "
                "pip install faster-whisper."
            ) from e

        if self.is_loaded() and self._model_key == model_key and self._compute_type == compute_type:
            return

        # 'auto' -> int8 on CPU is a solid default; user can override.
        ct = compute_type
        if ct == "auto":
            ct = "int8"

        log.info("Loading faster-whisper model=%s compute_type=%s", model_key, ct)
        try:
            self._model = WhisperModel(
                model_key,
                device="cpu",         # v1: CPU only; GPU can be added later
                compute_type=ct,
                download_root=str(models_cache_dir()),
                local_files_only=True,  # never touch the network here; see model_manager.install_model()
            )
        except LocalEntryNotFoundError as e:
            raise RuntimeError(
                f"Model '{model_key}' is not downloaded. Open Settings to download it."
            ) from e
        self._model_key = model_key
        self._compute_type = ct
        log.info("Model loaded.")

    def unload(self) -> None:
        self._model = None
        self._model_key = None
        self._compute_type = None

    def transcribe(
        self,
        pcm16_mono_16k: np.ndarray,
        language: str,
        initial_prompt: Optional[str] = None,
    ) -> str:
        if not self.is_loaded():
            raise RuntimeError("Engine not loaded")
        if pcm16_mono_16k.size == 0:
            return ""

        # faster-whisper wants float32 in [-1, 1].
        if pcm16_mono_16k.dtype != np.float32:
            audio = (pcm16_mono_16k.astype(np.float32)) / 32768.0
        else:
            audio = pcm16_mono_16k

        try:
            segments, _info = self._model.transcribe(  # type: ignore[attr-defined]
                audio,
                language=language,
                task="transcribe",
                beam_size=1,
                vad_filter=False,           # we already did VAD
                condition_on_previous_text=False,
                initial_prompt=initial_prompt,
                no_speech_threshold=0.6,
                temperature=0.0,
            )
            parts: list[str] = []
            for seg in segments:
                t = (seg.text or "").strip()
                if t:
                    parts.append(t)
            text = " ".join(parts).strip()
            return text
        except Exception as e:
            log.exception("faster-whisper transcribe failed: %s", e)
            return ""
