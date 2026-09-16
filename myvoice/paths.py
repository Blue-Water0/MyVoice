"""Filesystem paths used across the app. XDG-compliant."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "Myvoice"  # spec-mandated dir name (~/.config/Myvoice/)


def _xdg(base_env: str, default_sub: str) -> Path:
    v = os.environ.get(base_env)
    return Path(v) if v else Path.home() / default_sub


def config_dir() -> Path:
    p = _xdg("XDG_CONFIG_HOME", ".config") / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = _xdg("XDG_CACHE_HOME", ".cache") / APP_DIR_NAME
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


def autostart_dir() -> Path:
    p = _xdg("XDG_CONFIG_HOME", ".config") / "autostart"
    p.mkdir(parents=True, exist_ok=True)
    return p


def transcripts_dir() -> Path:
    """Location for user-visible saved transcript .txt files.

    Placed under the user's Documents directory so it is discoverable in
    the file manager and survives across MyVoice restarts. Follows XDG
    user-dirs indirectly by preferring ``$HOME/Documents``. Created lazily
    on first use.
    """
    p = Path.home() / "Documents" / "MyVoice Transcripts"
    p.mkdir(parents=True, exist_ok=True)
    return p
