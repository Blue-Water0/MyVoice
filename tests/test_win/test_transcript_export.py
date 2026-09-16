"""Tests for the Windows transcript-export service.

Covers filename generation, UTF-8 file writing (English / Hebrew / Arabic),
directory placement, and the ``os.startfile``-backed launch path --
mirroring ``tests/test_transcript_export.py``'s Linux/Gio coverage, minus
the desktop-id / editor-discovery concepts that don't exist on Windows.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

from myvoice_win.services import transcript_export as tx


# ---------------------------------------------------------------------------
# make_transcript_filename / save_transcript_to_file (same logic as Linux)
# ---------------------------------------------------------------------------


def test_filename_is_timestamped_and_safe():
    now = _dt.datetime(2026, 7, 13, 2, 43, 15)
    name = tx.make_transcript_filename(now=now)
    assert name == "MyVoice Transcript 2026-07-13 02-43-15.txt"
    assert ":" not in name
    assert "/" not in name
    assert "\\" not in name
    assert name.endswith(".txt")


def test_filename_scrubs_unsafe_prefix():
    now = _dt.datetime(2026, 7, 13, 2, 43, 15)
    name = tx.make_transcript_filename(now=now, prefix="../../evil/prefix")
    assert "/" not in name
    assert ".." not in name.split(" ")[0]
    assert name.endswith(".txt")


def test_save_writes_utf8_english(tmp_path: Path):
    path = tx.save_transcript_to_file(
        "Hello, world!",
        directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )
    assert path.exists()
    assert path.parent == tmp_path
    assert path.read_text(encoding="utf-8") == "Hello, world!"


def test_save_writes_utf8_hebrew_and_arabic(tmp_path: Path):
    content = "English line\nשלום עולם\nمرحبا بالعالم\n"
    path = tx.save_transcript_to_file(
        content, directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )
    on_disk = path.read_text(encoding="utf-8")
    assert on_disk == content
    raw = path.read_bytes()
    assert raw == content.encode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_save_does_not_overwrite_existing_file_same_timestamp(tmp_path: Path):
    ts = _dt.datetime(2026, 7, 13, 12, 0, 0)
    first = tx.save_transcript_to_file("first", directory=tmp_path, now=ts)
    second = tx.save_transcript_to_file("second", directory=tmp_path, now=ts)
    third = tx.save_transcript_to_file("third", directory=tmp_path, now=ts)

    assert first != second != third
    assert second.name == "MyVoice Transcript 2026-07-13 12-00-00 (2).txt"
    assert third.name == "MyVoice Transcript 2026-07-13 12-00-00 (3).txt"


def test_save_uses_configured_transcripts_dir_by_default(monkeypatch):
    # test_win/conftest.py sets HOME to tmp_path -- default location must
    # be under tmp_path/Documents/MyVoice Transcripts/.
    path = tx.save_transcript_to_file(
        "hello", now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )
    expected_dir = Path.home() / "Documents" / "MyVoice Transcripts"
    assert path.parent == expected_dir
    assert path.exists()


# ---------------------------------------------------------------------------
# open_transcript_in_app -- os.startfile-backed launch
# ---------------------------------------------------------------------------


def test_open_writes_file_and_calls_os_startfile(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p),
                         raising=False)

    result = tx.open_transcript_in_app(
        text="Hello",
        desktop_id="ignored.desktop",
        directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )

    assert result.launched is True
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == "Hello"
    assert calls == [str(result.path)]
    assert result.error is None
    assert result.used_fallback is False
    assert result.fallback_reason is None


def test_open_ignores_desktop_id_completely(tmp_path, monkeypatch):
    """desktop_id has no meaning on Windows -- must not change behavior."""
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p),
                         raising=False)

    r1 = tx.open_transcript_in_app(
        text="A", desktop_id="",
        directory=tmp_path, now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )
    r2 = tx.open_transcript_in_app(
        text="B", desktop_id="some.random.desktop.id.that.does.not.exist",
        directory=tmp_path, now=_dt.datetime(2026, 7, 13, 12, 0, 1),
    )
    assert r1.launched is True
    assert r2.launched is True
    assert r1.used_fallback is False
    assert r2.used_fallback is False


def test_open_reports_launched_false_when_startfile_raises(tmp_path, monkeypatch):
    def boom(p):
        raise OSError("no association for .txt")

    monkeypatch.setattr(os, "startfile", boom, raising=False)

    result = tx.open_transcript_in_app(
        text="content",
        desktop_id="",
        directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )

    assert result.launched is False
    assert result.error is not None
    assert "no association" in result.error
    # File is still written and preserved even when the launch fails.
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == "content"


def test_open_defaults_to_temp_dir_not_permanent_transcripts_dir(monkeypatch):
    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)

    result = tx.open_transcript_in_app(
        text="scratch content",
        desktop_id="",
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )

    permanent_dir = Path.home() / "Documents" / "MyVoice Transcripts"
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == "scratch content"
    assert result.path.parent != permanent_dir
    assert permanent_dir not in result.path.parents
    assert not permanent_dir.exists()


def test_open_accepts_custom_launcher_override(tmp_path):
    calls = []

    def fake_launcher(path):
        calls.append(path)

    result = tx.open_transcript_in_app(
        text="x", desktop_id="",
        directory=tmp_path, now=_dt.datetime(2026, 7, 13, 12, 0, 0),
        launcher=fake_launcher,
    )
    assert result.launched is True
    assert calls == [result.path]


# ---------------------------------------------------------------------------
# reveal_transcript_folder -- last-resort fallback
# ---------------------------------------------------------------------------


def test_reveal_transcript_folder_calls_os_startfile_on_parent_dir(
    tmp_path, monkeypatch,
):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(p),
                         raising=False)

    target = tmp_path / "MyVoice Transcript foo.txt"
    target.write_text("x", encoding="utf-8")

    tx.reveal_transcript_folder(target)

    assert calls == [str(tmp_path)]


def test_reveal_transcript_folder_never_raises(tmp_path, monkeypatch):
    def boom(p):
        raise OSError("no shell")

    monkeypatch.setattr(os, "startfile", boom, raising=False)

    # Must not raise -- best-effort fallback UX, never crashes the caller.
    tx.reveal_transcript_folder(tmp_path / "whatever.txt")
