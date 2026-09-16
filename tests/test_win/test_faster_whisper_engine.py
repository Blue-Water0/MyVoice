"""Tests for myvoice_win.engines.faster_whisper_engine and .registry.

faster_whisper.WhisperModel is mocked throughout: we never want a real
network call / model load in unit tests. The real ``faster_whisper``
package is installed in this venv (system-site-packages), so we monkeypatch
its ``WhisperModel`` attribute directly rather than faking the whole
module.
"""
from __future__ import annotations

import sys

import numpy as np
import pytest

from myvoice.engines.base import EngineInfo, ModelPreset, SpeechEngine
from myvoice_win.engines import registry
from myvoice_win.engines.faster_whisper_engine import (
    INFO,
    PRESETS,
    FasterWhisperEngine,
    _cache_env,
)


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    """Stand-in for faster_whisper.WhisperModel."""

    last_init_kwargs: dict | None = None

    def __init__(self, model_key, **kwargs) -> None:
        self.model_key = model_key
        self.kwargs = kwargs
        _FakeWhisperModel.last_init_kwargs = {"model_key": model_key, **kwargs}
        self.transcribe_calls: list[dict] = []
        self._segments = [_FakeSegment("hello"), _FakeSegment(" world ")]
        self._raise = False

    def transcribe(self, audio, **kwargs):
        self.transcribe_calls.append({"audio": audio, **kwargs})
        if self._raise:
            raise RuntimeError("boom")
        return self._segments, object()


@pytest.fixture
def fake_whisper_model(monkeypatch):
    """Patch the real faster_whisper.WhisperModel with our fake."""
    import faster_whisper

    monkeypatch.setattr(faster_whisper, "WhisperModel", _FakeWhisperModel)
    return _FakeWhisperModel


# ---- SpeechEngine interface / base reuse -----------------------------------


def test_engine_implements_base_speechengine():
    assert issubclass(FasterWhisperEngine, SpeechEngine)


def test_info_and_presets_shape():
    assert isinstance(INFO, EngineInfo)
    assert INFO.key == "faster-whisper"
    assert INFO.default_preset == "medium"
    assert INFO.presets == PRESETS
    keys = {p.key for p in PRESETS}
    assert keys == {"small", "medium", "large-v3-turbo"}
    for p in PRESETS:
        assert isinstance(p, ModelPreset)
        assert p.approx_download_mb > 0
        assert p.approx_ram_mb > 0


def test_cache_env_sets_hf_env_vars(monkeypatch):
    for var in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    _cache_env()
    import os

    from myvoice_win.paths import models_cache_dir

    assert os.environ["HF_HOME"] == str(models_cache_dir())
    assert os.environ["HUGGINGFACE_HUB_CACHE"] == str(models_cache_dir())


def test_cache_env_does_not_override_existing(monkeypatch):
    monkeypatch.setenv("HF_HOME", "/already/set")
    _cache_env()
    import os

    assert os.environ["HF_HOME"] == "/already/set"


# ---- load()/unload()/is_loaded() -------------------------------------------


def test_not_loaded_initially():
    engine = FasterWhisperEngine()
    assert engine.is_loaded() is False


def test_load_raises_runtime_error_if_faster_whisper_missing(monkeypatch):
    # Simulate faster_whisper being uninstalled: forcing the module import
    # inside load() to fail.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    engine = FasterWhisperEngine()
    with pytest.raises(RuntimeError, match="faster-whisper is not installed"):
        engine.load("small")


def test_load_sets_state_and_uses_int8_for_auto(fake_whisper_model, tmp_path):
    engine = FasterWhisperEngine()
    engine.load("small", compute_type="auto")
    assert engine.is_loaded() is True
    assert engine._model_key == "small"
    assert engine._compute_type == "int8"
    kwargs = fake_whisper_model.last_init_kwargs
    assert kwargs["model_key"] == "small"
    assert kwargs["device"] == "cpu"
    assert kwargs["compute_type"] == "int8"
    from myvoice_win.paths import models_cache_dir

    assert kwargs["download_root"] == str(models_cache_dir())


def test_load_passes_through_explicit_compute_type(fake_whisper_model):
    engine = FasterWhisperEngine()
    engine.load("medium", compute_type="int16")
    assert engine._compute_type == "int16"


def test_load_is_a_noop_if_same_model_already_loaded(fake_whisper_model):
    engine = FasterWhisperEngine()
    engine.load("small", compute_type="int8")
    first_model = engine._model
    engine.load("small", compute_type="int8")
    assert engine._model is first_model  # no reload


def test_load_reloads_if_model_key_changes(fake_whisper_model):
    engine = FasterWhisperEngine()
    engine.load("small", compute_type="int8")
    first_model = engine._model
    engine.load("medium", compute_type="int8")
    assert engine._model is not first_model
    assert engine._model_key == "medium"


def test_unload_resets_state(fake_whisper_model):
    engine = FasterWhisperEngine()
    engine.load("small")
    engine.unload()
    assert engine.is_loaded() is False
    assert engine._model_key is None
    assert engine._compute_type is None


# ---- transcribe() -----------------------------------------------------------


def test_transcribe_raises_if_not_loaded():
    engine = FasterWhisperEngine()
    with pytest.raises(RuntimeError, match="not loaded"):
        engine.transcribe(np.zeros(10, dtype=np.int16), "ar")


def test_transcribe_empty_audio_returns_empty_string(fake_whisper_model):
    engine = FasterWhisperEngine()
    engine.load("small")
    result = engine.transcribe(np.array([], dtype=np.int16), "ar")
    assert result == ""


def test_transcribe_joins_segment_text_and_strips(fake_whisper_model):
    engine = FasterWhisperEngine()
    engine.load("small")
    pcm = np.array([0, 100, -100, 32767], dtype=np.int16)
    result = engine.transcribe(pcm, "ar", initial_prompt="context")
    assert result == "hello world"

    call = engine._model.transcribe_calls[-1]
    assert call["language"] == "ar"
    assert call["task"] == "transcribe"
    assert call["initial_prompt"] == "context"
    assert call["vad_filter"] is False
    assert call["condition_on_previous_text"] is False
    # audio must be float32 in [-1, 1], converted from int16 PCM.
    assert call["audio"].dtype == np.float32
    assert call["audio"].max() <= 1.0
    assert call["audio"].min() >= -1.0


def test_transcribe_accepts_float32_audio_unchanged(fake_whisper_model):
    engine = FasterWhisperEngine()
    engine.load("small")
    pcm = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    engine.transcribe(pcm, "ar")
    call = engine._model.transcribe_calls[-1]
    np.testing.assert_array_equal(call["audio"], pcm)


def test_transcribe_swallows_exceptions_and_returns_empty(fake_whisper_model):
    engine = FasterWhisperEngine()
    engine.load("small")
    engine._model._raise = True
    result = engine.transcribe(np.array([1, 2, 3], dtype=np.int16), "ar")
    assert result == ""


# ---- registry ---------------------------------------------------------------


def test_registry_creates_faster_whisper_engine():
    engine = registry.create_engine("faster-whisper")
    assert isinstance(engine, FasterWhisperEngine)


def test_registry_raises_keyerror_on_unknown_key():
    # Matches myvoice/engines/registry.py's exact behavior (KeyError, not
    # ValueError) per the Global Constraints' Module Reuse Map instruction
    # to match the Linux original's exact signature/behavior.
    with pytest.raises(KeyError):
        registry.create_engine("does-not-exist")


def test_registry_available_engines_and_info():
    infos = registry.available_engines()
    assert INFO in infos
    assert registry.engine_info("faster-whisper") is INFO
