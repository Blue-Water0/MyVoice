"""Model cache utilities.

faster-whisper (via huggingface_hub) downloads models on first .transcribe()
call. We expose whole-cache helpers ("clear cache" / total size) and
per-preset helpers (is a given preset downloaded, its on-disk size, delete
just that one, or download just that one ahead of time) so the Settings
dialog can show install status per model and act on individual models.
"""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Callable, Optional

from ..paths import models_cache_dir

log = logging.getLogger(__name__)

# A bare/partial model dir (stray .locks leftovers, an interrupted download)
# can exist on disk without real weight data in it. Anything smaller than
# this is treated as "not installed" so the UI never shows a false green dot.
_INSTALLED_MIN_BYTES = 10 * 1024 * 1024


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def cache_size_bytes() -> int:
    return _dir_size_bytes(models_cache_dir())


def clear_cache() -> None:
    root = models_cache_dir()
    for child in root.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except Exception as e:
            log.warning("Failed to remove %s: %s", child, e)
    log.info("Model cache cleared: %s", root)


def cache_dir() -> Path:
    return models_cache_dir()


# ---- Per-preset helpers ----------------------------------------------------


def _resolve_repo_id(preset_key: str) -> str:
    """Map a faster-whisper preset key (e.g. 'large-v3-turbo') to its
    HuggingFace repo id (e.g. 'mobiuslabsgmbh/faster-whisper-large-v3-turbo').
    """
    from faster_whisper.utils import _MODELS  # type: ignore

    try:
        return _MODELS[preset_key]
    except KeyError as e:
        raise ValueError(f"Unknown model preset: {preset_key}") from e


def _model_dir(preset_key: str) -> Path:
    repo_id = _resolve_repo_id(preset_key)
    return models_cache_dir() / ("models--" + repo_id.replace("/", "--"))


def _dir_size_bytes(d: Path) -> int:
    """Sum real file bytes under ``d``, counting each blob once.

    HuggingFace's cache layout stores real weight data under blobs/<hash>
    and exposes it under snapshots/<rev>/<filename> as a symlink to that
    same blob. Path.is_file()/stat() follow symlinks, so without excluding
    them here every blob gets counted twice.
    """
    total = 0
    if not d.exists():
        return 0
    for p in d.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def installed_size_bytes(preset_key: str) -> int:
    """On-disk size of one downloaded preset, in bytes. 0 if not installed."""
    return _dir_size_bytes(_model_dir(preset_key))


def is_installed(preset_key: str) -> bool:
    return installed_size_bytes(preset_key) >= _INSTALLED_MIN_BYTES


def delete_model(preset_key: str) -> None:
    """Remove one downloaded preset (and its stray lock dir, if any)."""
    d = _model_dir(preset_key)
    if d.exists():
        shutil.rmtree(d)
    lock_dir = models_cache_dir() / ".locks" / d.name
    if lock_dir.exists():
        shutil.rmtree(lock_dir, ignore_errors=True)
    log.info("Deleted model preset: %s", preset_key)


def install_model(
    preset_key: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """Download one preset's weights into the app's cache dir (blocking).

    Intended to be called from a background thread. ``on_progress`` is
    called as ``(downloaded_bytes, total_bytes)`` while huggingface_hub's
    parallel file-download workers report progress; total_bytes aggregates
    across every file in the repo (one large weight blob plus a handful of
    small config/tokenizer files).
    """
    import huggingface_hub
    from tqdm.auto import tqdm as _tqdm_base

    repo_id = _resolve_repo_id(preset_key)
    lock = threading.Lock()
    totals: dict[int, int] = {}
    currents: dict[int, int] = {}

    def _report() -> None:
        if on_progress is None:
            return
        with lock:
            done = sum(currents.values())
            total = sum(totals.values())
        try:
            on_progress(done, total)
        except Exception:
            log.exception("install_model on_progress callback failed")

    class _TrackedTqdm(_tqdm_base):  # noqa: D401 - internal progress shim
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            with lock:
                totals[id(self)] = self.total or 0
                currents[id(self)] = 0
            _report()

        def update(self, n: int = 1) -> bool | None:
            result = super().update(n)
            with lock:
                currents[id(self)] = self.n
            _report()
            return result

    log.info("Installing model preset: %s (%s)", preset_key, repo_id)
    huggingface_hub.snapshot_download(
        repo_id=repo_id,
        cache_dir=str(models_cache_dir()),
        tqdm_class=_TrackedTqdm,
    )
    log.info("Model preset installed: %s", preset_key)
