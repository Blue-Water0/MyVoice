"""Tests for the transcript-export service.

Covers filename generation, UTF-8 file writing (English / Hebrew / Arabic),
directory placement, discovery filtering, system-default resolution, and
fallback behavior when a configured app is uninstalled.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from myvoice.services import transcript_export as tx


# ---------------------------------------------------------------------------
# Fake GIO doubles
# ---------------------------------------------------------------------------


class FakeAppInfo:
    """Duck-typed stand-in for a Gio.AppInfo / DesktopAppInfo."""

    def __init__(
        self,
        did: str,
        name: str,
        icon: str | None = None,
        terminal: bool = False,
    ) -> None:
        self._did = did
        self._name = name
        self._icon = icon
        self._terminal = terminal

    def get_id(self) -> str:
        return self._did

    def get_name(self) -> str:
        return self._name

    def get_display_name(self) -> str:
        return self._name

    def get_icon(self):
        return None if self._icon is None else _FakeIcon(self._icon)

    # Present only when we want the terminal filter to trigger the DesktopAppInfo path.
    # The real filter checks isinstance(app, Gio.DesktopAppInfo); here we short-circuit
    # by exposing a `_terminal` attribute the tests use via a patched _appinfo_is_terminal.


class _FakeIcon:
    def __init__(self, s: str) -> None:
        self._s = s

    def to_string(self) -> str:
        return self._s


# ---------------------------------------------------------------------------
# make_transcript_filename
# ---------------------------------------------------------------------------


def test_filename_is_timestamped_and_safe():
    now = _dt.datetime(2026, 7, 13, 2, 43, 15)
    name = tx.make_transcript_filename(now=now)
    assert name == "MyVoice Transcript 2026-07-13 02-43-15.txt"
    # No characters that misbehave on ext4 / Samba / exFAT.
    assert ":" not in name
    assert "/" not in name
    assert "\\" not in name
    assert name.endswith(".txt")


def test_filename_scrubs_unsafe_prefix():
    now = _dt.datetime(2026, 7, 13, 2, 43, 15)
    name = tx.make_transcript_filename(now=now, prefix="../../evil/prefix")
    # slashes removed, path traversal impossible
    assert "/" not in name
    assert ".." not in name.split(" ")[0]
    assert name.endswith(".txt")


def test_filename_empty_prefix_uses_default():
    now = _dt.datetime(2026, 7, 13, 2, 43, 15)
    name = tx.make_transcript_filename(now=now, prefix="!!!")
    assert name.startswith("MyVoice Transcript")


# ---------------------------------------------------------------------------
# save_transcript_to_file
# ---------------------------------------------------------------------------


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
    # Bytes must be UTF-8 encoded of the same characters, no BOM.
    raw = path.read_bytes()
    assert raw == content.encode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_save_does_not_overwrite_existing_file_same_timestamp(tmp_path: Path):
    ts = _dt.datetime(2026, 7, 13, 12, 0, 0)
    first = tx.save_transcript_to_file("first", directory=tmp_path, now=ts)
    second = tx.save_transcript_to_file("second", directory=tmp_path, now=ts)
    third = tx.save_transcript_to_file("third", directory=tmp_path, now=ts)

    assert first != second != third
    assert first.read_text(encoding="utf-8") == "first"
    assert second.read_text(encoding="utf-8") == "second"
    assert third.read_text(encoding="utf-8") == "third"
    assert second.name == "MyVoice Transcript 2026-07-13 12-00-00 (2).txt"
    assert third.name == "MyVoice Transcript 2026-07-13 12-00-00 (3).txt"


def test_save_creates_directory_if_missing(tmp_path: Path):
    target = tmp_path / "nested" / "dir"
    assert not target.exists()
    path = tx.save_transcript_to_file(
        "hi", directory=target,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )
    assert target.exists()
    assert path.parent == target


def test_save_uses_configured_transcripts_dir_by_default(tmp_path: Path, monkeypatch):
    # conftest sets HOME to tmp_path. The default location must therefore
    # be under tmp_path/Documents/MyVoice Transcripts/.
    path = tx.save_transcript_to_file(
        "hello", now=_dt.datetime(2026, 7, 13, 12, 0, 0),
    )
    expected_dir = Path.home() / "Documents" / "MyVoice Transcripts"
    assert path.parent == expected_dir
    assert path.exists()


# ---------------------------------------------------------------------------
# discover_text_apps
# ---------------------------------------------------------------------------


def test_discover_includes_installed_apps_and_dedupes(monkeypatch):
    fake_apps = [
        FakeAppInfo("org.x.editor.desktop", "Text Editor", icon="accessories-text-editor"),
        FakeAppInfo("code.desktop", "VS Code"),
        # duplicate id — should be ignored
        FakeAppInfo("org.x.editor.desktop", "Text Editor DUP"),
    ]

    def lister():
        return fake_apps

    # No terminal filter unless we mark the FakeAppInfo.
    monkeypatch.setattr(tx, "_appinfo_is_terminal", lambda a: False)

    entries = tx.discover_text_apps(lister=lister)
    ids = [e.desktop_id for e in entries]
    assert ids == ["org.x.editor.desktop", "code.desktop"]
    assert entries[0].name == "Text Editor"
    assert entries[0].icon == "accessories-text-editor"


def test_discover_excludes_terminal_apps(monkeypatch):
    fake_apps = [
        FakeAppInfo("gedit.desktop", "Gedit", terminal=False),
        FakeAppInfo("vim.desktop", "Vim", terminal=True),
        FakeAppInfo("nvim.desktop", "Neovim", terminal=True),
        FakeAppInfo("code.desktop", "VS Code", terminal=False),
    ]

    # Route the terminal check through the fake's attribute.
    monkeypatch.setattr(
        tx, "_appinfo_is_terminal", lambda a: bool(getattr(a, "_terminal", False)),
    )

    entries = tx.discover_text_apps(lister=lambda: fake_apps)
    ids = [e.desktop_id for e in entries]
    assert "vim.desktop" not in ids
    assert "nvim.desktop" not in ids
    assert set(ids) == {"gedit.desktop", "code.desktop"}


def test_discover_returns_empty_when_gio_unavailable(monkeypatch):
    # Simulate a system with no apps registered for text/plain.
    entries = tx.discover_text_apps(lister=lambda: [])
    assert entries == []


# ---------------------------------------------------------------------------
# get_default_text_app
# ---------------------------------------------------------------------------


def test_default_text_app_returned_when_present():
    fake = FakeAppInfo("gedit.desktop", "Gedit")
    entry = tx.get_default_text_app(default_getter=lambda: fake)
    assert entry is not None
    assert entry.desktop_id == "gedit.desktop"
    assert entry.name == "Gedit"


def test_default_text_app_none_when_absent():
    entry = tx.get_default_text_app(default_getter=lambda: None)
    assert entry is None


# ---------------------------------------------------------------------------
# open_transcript_in_app  (integration-y, still no real GIO)
# ---------------------------------------------------------------------------


class _RecordingLauncher:
    def __init__(self, should_raise: bool = False):
        self.calls: list[tuple[object, Path]] = []
        self._raise = should_raise

    def __call__(self, app, path: Path) -> None:
        self.calls.append((app, path))
        if self._raise:
            raise RuntimeError("boom")


def test_open_saves_file_and_launches_system_default(tmp_path, monkeypatch):
    launcher = _RecordingLauncher()
    fake_default = FakeAppInfo("org.x.editor.desktop", "Text Editor")

    def default_getter():
        return fake_default

    result = tx.open_transcript_in_app(
        text="Hello",
        desktop_id=tx.SYSTEM_DEFAULT_ID,  # ""
        directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
        default_getter=default_getter,
        launcher=launcher,
    )
    assert result.launched is True
    assert result.used_fallback is False
    assert result.app_name == "Text Editor"
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == "Hello"
    assert len(launcher.calls) == 1


def test_open_falls_back_when_configured_app_uninstalled(tmp_path, monkeypatch):
    """A specific desktop_id that isn't installed must silently fall back
    to the system default and mark used_fallback=True."""
    launcher = _RecordingLauncher()
    fake_default = FakeAppInfo("gedit.desktop", "Gedit")

    # Simulate Gio.DesktopAppInfo.new(<unknown>) returning None. We
    # patch _resolve_app's Gio module by patching the whole function
    # to exercise the intended path.
    def fake_resolve(desktop_id: str, default_getter=None):
        if desktop_id == tx.SYSTEM_DEFAULT_ID:
            return (fake_default, False, None)
        # unknown id -> None from DesktopAppInfo.new -> fallback
        return (fake_default, True,
                f"Configured text app '{desktop_id}' is not installed")

    monkeypatch.setattr(tx, "_resolve_app", fake_resolve)

    result = tx.open_transcript_in_app(
        text="שלום",
        desktop_id="ghost.desktop",
        directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
        launcher=launcher,
    )
    assert result.launched is True
    assert result.used_fallback is True
    assert result.fallback_reason is not None
    assert "ghost.desktop" in result.fallback_reason
    assert result.path.read_text(encoding="utf-8") == "שלום"


def test_open_saves_file_even_when_no_app_available(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tx, "_resolve_app",
        lambda desktop_id, default_getter=None: (None, False, None),
    )
    launcher = _RecordingLauncher()
    result = tx.open_transcript_in_app(
        text="content",
        desktop_id=tx.SYSTEM_DEFAULT_ID,
        directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
        launcher=launcher,
    )
    assert result.launched is False
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == "content"
    assert launcher.calls == []
    assert result.error is not None


def test_open_saves_file_even_when_launch_raises(tmp_path, monkeypatch):
    fake = FakeAppInfo("gedit.desktop", "Gedit")
    monkeypatch.setattr(
        tx, "_resolve_app",
        lambda desktop_id, default_getter=None: (fake, False, None),
    )
    launcher = _RecordingLauncher(should_raise=True)
    result = tx.open_transcript_in_app(
        text="ok",
        desktop_id=tx.SYSTEM_DEFAULT_ID,
        directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
        launcher=launcher,
    )
    assert result.launched is False
    assert result.error is not None and "boom" in result.error
    assert result.path.exists()


def test_open_defaults_to_temp_dir_not_permanent_transcripts_dir(monkeypatch):
    """Opening a transcript to view/edit it must not, by itself, create a
    permanent file under ~/Documents/MyVoice Transcripts/ -- only an
    explicit directory= override (or calling save_transcript_to_file
    directly) should land there."""
    launcher = _RecordingLauncher()
    fake_default = FakeAppInfo("org.x.editor.desktop", "Text Editor")

    result = tx.open_transcript_in_app(
        text="scratch content",
        desktop_id=tx.SYSTEM_DEFAULT_ID,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
        default_getter=lambda: fake_default,
        launcher=launcher,
    )

    permanent_dir = Path.home() / "Documents" / "MyVoice Transcripts"
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == "scratch content"
    assert result.path.parent != permanent_dir
    assert permanent_dir not in result.path.parents
    assert not permanent_dir.exists()
