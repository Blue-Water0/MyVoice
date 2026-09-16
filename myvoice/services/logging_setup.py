"""Rotating file logger. No audio, no transcripts written to disk."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from ..paths import log_dir

_INITIALIZED = False


def setup_logging(level: str | int = "INFO") -> Path:
    """Configure root logger. Safe to call once. Returns log file path."""
    global _INITIALIZED
    log_path = log_dir() / "myvoice.log"
    if _INITIALIZED:
        return log_path

    lvl = logging._nameToLevel.get(level.upper(), logging.INFO) if isinstance(level, str) else level

    root = logging.getLogger()
    root.setLevel(lvl)
    # Wipe existing handlers (pytest, other libs)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if os.environ.get("MYVOICE_LOG_STDERR") == "1" or sys.stderr.isatty():
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _INITIALIZED = True
    logging.getLogger(__name__).info("Logging initialized -> %s", log_path)
    return log_path
