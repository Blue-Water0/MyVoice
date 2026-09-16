"""Engine registry (Windows build). New engines: add here."""
from __future__ import annotations

from typing import Callable

from myvoice.engines.base import EngineInfo, SpeechEngine

from .faster_whisper_engine import INFO as FW_INFO
from .faster_whisper_engine import FasterWhisperEngine

_FACTORIES: dict[str, Callable[[], SpeechEngine]] = {
    FW_INFO.key: FasterWhisperEngine,
}

_INFOS: dict[str, EngineInfo] = {
    FW_INFO.key: FW_INFO,
}


def available_engines() -> list[EngineInfo]:
    return list(_INFOS.values())


def create_engine(key: str) -> SpeechEngine:
    if key not in _FACTORIES:
        raise KeyError(f"Unknown speech engine: {key}")
    return _FACTORIES[key]()


def engine_info(key: str) -> EngineInfo:
    return _INFOS[key]
