"""Filesystem paths used across the Windows app.

Windows-native equivalent of ``myvoice.paths`` (which is XDG-based, for
Linux). Same function surface, different backing environment variables:
``%APPDATA%`` for roaming config, ``%LOCALAPPDATA%`` for the cache.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "MyVoice"


def config_dir() -> Path:
    v = os.environ.get("APPDATA")
    base = Path(v) if v else Path.home() / "AppData" / "Roaming"
    p = base / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    v = os.environ.get("LOCALAPPDATA")
    base = Path(v) if v else Path.home() / "AppData" / "Local"
    p = base / APP_DIR_NAME / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = config_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return config_dir() / "settings.json"


def models_cache_dir() -> Path:
    p = cache_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def transcripts_dir() -> Path:
    """Location for user-visible saved transcript .txt files.

    Placed under the user's Documents directory so it is discoverable in
    File Explorer and survives across MyVoice restarts. Created lazily on
    first use.
    """
    p = Path.home() / "Documents" / "MyVoice Transcripts"
    p.mkdir(parents=True, exist_ok=True)
    return p
