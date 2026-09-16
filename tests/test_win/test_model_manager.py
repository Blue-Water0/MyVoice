"""Tests for myvoice_win.models.model_manager.

huggingface_hub.snapshot_download is mocked throughout: never a real
network call in unit tests. faster_whisper.utils._MODELS is the real,
installed mapping (small/no-network to read) so repo-id resolution is
exercised for real; unknown preset keys are tested for the ValueError path.
"""
from __future__ import annotations

import pytest

from myvoice_win.models import model_manager
from myvoice_win.paths import models_cache_dir


# ---- human_size --------------------------------------------------------


@pytest.mark.parametrize(
    "num_bytes, expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (1024 * 1024 * 1024, "1.0 GB"),
    ],
)
def test_human_size(num_bytes, expected):
    assert model_manager.human_size(num_bytes) == expected


# ---- whole-cache helpers -------------------------------------------------


def test_cache_dir_matches_models_cache_dir():
    assert model_manager.cache_dir() == models_cache_dir()


def test_cache_size_bytes_empty_cache_is_zero():
    assert model_manager.cache_size_bytes() == 0


def test_cache_size_bytes_sums_real_files_only():
    root = models_cache_dir()
    d = root / "models--Some--Repo" / "blobs"
    d.mkdir(parents=True)
    (d / "blob1").write_bytes(b"x" * 100)
    snap_dir = root / "models--Some--Repo" / "snapshots" / "rev"
    snap_dir.mkdir(parents=True)
    (snap_dir / "weights.bin").symlink_to(d / "blob1")

    # The symlink must not be double-counted.
    assert model_manager.cache_size_bytes() == 100


def test_clear_cache_removes_everything():
    root = models_cache_dir()
    (root / "some_dir").mkdir()
    (root / "some_dir" / "f").write_bytes(b"data")
    (root / "loose_file").write_bytes(b"data")

    model_manager.clear_cache()

    assert list(root.iterdir()) == []


def test_clear_cache_on_empty_dir_does_not_raise():
    model_manager.clear_cache()


# ---- per-preset helpers ---------------------------------------------------


def test_resolve_repo_id_known_preset():
    repo_id = model_manager._resolve_repo_id("small")
    assert repo_id  # real mapping from faster_whisper.utils._MODELS
    assert "/" in repo_id


def test_resolve_repo_id_unknown_preset_raises_value_error():
    with pytest.raises(ValueError, match="Unknown model preset"):
        model_manager._resolve_repo_id("not-a-real-preset")


def _make_installed(preset_key: str, num_bytes: int) -> None:
    d = model_manager._model_dir(preset_key)
    blobs = d / "blobs"
    blobs.mkdir(parents=True)
    blob = blobs / "abc123"
    blob.write_bytes(b"x" * num_bytes)
    snapshots = d / "snapshots" / "rev1"
    snapshots.mkdir(parents=True)
    (snapshots / "model.bin").symlink_to(blob)


def test_is_installed_false_when_absent():
    assert model_manager.is_installed("small") is False
    assert model_manager.installed_size_bytes("small") == 0


def test_is_installed_false_for_partial_download_below_threshold():
    _make_installed("small", 1024)  # well under _INSTALLED_MIN_BYTES
    assert model_manager.is_installed("small") is False
    assert model_manager.installed_size_bytes("small") == 1024


def test_is_installed_true_once_above_threshold():
    _make_installed("small", model_manager._INSTALLED_MIN_BYTES + 1)
    assert model_manager.is_installed("small") is True
    assert model_manager.installed_size_bytes("small") == model_manager._INSTALLED_MIN_BYTES + 1


def test_delete_model_removes_dir_and_lock_dir():
    _make_installed("small", model_manager._INSTALLED_MIN_BYTES + 1)
    d = model_manager._model_dir("small")
    lock_dir = models_cache_dir() / ".locks" / d.name
    lock_dir.mkdir(parents=True)
    (lock_dir / "lockfile").write_bytes(b"")

    model_manager.delete_model("small")

    assert not d.exists()
    assert not lock_dir.exists()


def test_delete_model_on_absent_model_does_not_raise():
    model_manager.delete_model("small")


# ---- install_model ----------------------------------------------------------


def test_install_model_calls_snapshot_download_with_expected_args(monkeypatch):
    calls = {}

    def fake_snapshot_download(*, repo_id, cache_dir, tqdm_class):
        calls["repo_id"] = repo_id
        calls["cache_dir"] = cache_dir
        calls["tqdm_class"] = tqdm_class

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    model_manager.install_model("small")

    assert calls["repo_id"] == model_manager._resolve_repo_id("small")
    assert calls["cache_dir"] == str(models_cache_dir())
    assert calls["tqdm_class"] is not None


def test_install_model_reports_progress_via_tqdm_shim(monkeypatch):
    import io

    progress_calls = []
    sink = io.StringIO()  # swallow tqdm's own terminal output in test logs

    def on_progress(done, total):
        progress_calls.append((done, total))

    def fake_snapshot_download(*, repo_id, cache_dir, tqdm_class):
        # Simulate huggingface_hub's real usage: one tqdm bar per file,
        # updated as bytes come in. (disable=True would short-circuit
        # tqdm.update() entirely, so redirect output instead of disabling.)
        bar1 = tqdm_class(total=100, file=sink)
        bar1.update(50)
        bar2 = tqdm_class(total=50, file=sink)
        bar1.update(50)
        bar2.update(50)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    model_manager.install_model("small", on_progress=on_progress)

    assert len(progress_calls) > 0
    # Final call reflects both bars fully downloaded.
    assert progress_calls[-1] == (150, 150)


def test_install_model_progress_callback_exception_does_not_propagate(monkeypatch):
    def on_progress(done, total):
        raise RuntimeError("callback exploded")

    def fake_snapshot_download(*, repo_id, cache_dir, tqdm_class):
        bar = tqdm_class(total=10, disable=True)
        bar.update(10)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    # Must not raise even though the callback always raises.
    model_manager.install_model("small", on_progress=on_progress)


def test_install_model_without_progress_callback_works(monkeypatch):
    def fake_snapshot_download(*, repo_id, cache_dir, tqdm_class):
        bar = tqdm_class(total=10, disable=True)
        bar.update(10)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    model_manager.install_model("small")  # no on_progress; should not raise
