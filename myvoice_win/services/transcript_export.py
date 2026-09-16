"""Transcript export: write the full session transcript to a real UTF-8
``.txt`` file and open it with whatever application Windows has
associated with ``.txt`` files.

Windows-native equivalent of ``myvoice.services.transcript_export`` (which
uses GIO/Gio.AppInfo to enumerate and launch desktop-file editors). There
is no per-desktop-id / installed-app-enumeration concept on Windows --
Explorer's file-type associations are a single system-wide default per
extension, not a chooser of arbitrary registered apps -- so this module
always opens the file via the OS default handler
(``os.startfile``). ``desktop_id`` is still accepted by
``open_transcript_in_app`` for call-site compatibility with the Linux
signature (so shared UI code doesn't need an ``if platform`` branch) and
is otherwise ignored.

``os.startfile`` is a Windows-only stdlib addition -- it does not exist as
an attribute on Linux, so it is never referenced at module import time,
only inside function bodies via ``getattr(os, "startfile")``, evaluated
only when the function actually runs (i.e. only under a test that
monkeypatches it in, or on real Windows).
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..paths import transcripts_dir

log = logging.getLogger(__name__)

# Kept for call-site compatibility with the Linux module's constant of the
# same name -- Windows has no per-app desktop-id concept, so this is not
# otherwise meaningful here.
SYSTEM_DEFAULT_ID = ""


# ---------------------------------------------------------------------------
# Filename + on-disk save (identical logic/contract to the Linux module)
# ---------------------------------------------------------------------------


_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 _\-]+")


def make_transcript_filename(now: Optional[datetime] = None,
                             prefix: str = "MyVoice Transcript") -> str:
    """Return a safe timestamped ``.txt`` filename.

    Format::

        MyVoice Transcript YYYY-MM-DD HH-MM-SS.txt

    The prefix is scrubbed of anything that isn't alphanumeric, space,
    dash, or underscore so the returned string is a safe filename on
    NTFS/exFAT (and ext4/Samba, for consistency with the Linux module).
    """
    if now is None:
        now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H-%M-%S")
    safe_prefix = _UNSAFE_CHARS.sub("", prefix).strip() or "MyVoice Transcript"
    return f"{safe_prefix} {ts}.txt"


def _unique_path(target_dir: Path, filename: str) -> Path:
    """Return a path for ``filename`` under ``target_dir`` that does not
    already exist, appending `` (2)``, `` (3)``, ... before the extension
    if needed so a new transcript never silently overwrites an existing
    one saved within the same second.
    """
    candidate = target_dir / filename
    if not candidate.exists():
        return candidate
    stem, _, ext = filename.rpartition(".")
    stem, ext = (stem, f".{ext}") if stem else (filename, "")
    n = 2
    while True:
        candidate = target_dir / f"{stem} ({n}){ext}"
        if not candidate.exists():
            return candidate
        n += 1


def save_transcript_to_file(
    text: str,
    directory: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Write ``text`` (UTF-8) to a timestamped ``.txt`` file.

    Returns the absolute :class:`Path` of the saved file. Defaults to
    ``myvoice_win.paths.transcripts_dir()`` (``~/Documents/MyVoice
    Transcripts``) when ``directory`` isn't given. If a file with the same
    timestamped name already exists, a `` (2)``, `` (3)``, ... suffix is
    added so the earlier file is never overwritten.
    """
    target_dir = directory if directory is not None else transcripts_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(target_dir, make_transcript_filename(now=now))
    # newline="" disables Python's universal-newline translation, which
    # on Windows otherwise silently rewrites every "\n" in `text` to
    # "\r\n" on write. Without this, the file on disk would not match
    # `text` byte-for-byte -- surprising for anything that later compares
    # or hashes the saved content, and inconsistent with the Linux port's
    # explicit "no BOM, no surprises" contract for this function.
    path.write_text(text, encoding="utf-8", newline="")
    return path


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


@dataclass
class LaunchResult:
    """Result of an "open transcript in text app" attempt.

    Same field shape as the Linux ``LaunchResult`` for call-site
    compatibility. ``used_fallback``/``fallback_reason`` always report
    ``False``/``None`` on Windows -- there is no configured-app-vs-default
    distinction to fall back from, since ``open_transcript_in_app`` always
    goes through the single OS default handler.

    ``path`` is always set to the saved ``.txt`` file -- even when the
    launch failed -- so the caller can show its location to the user and
    they can open it manually from File Explorer.
    """

    path: Path
    launched: bool
    app_name: Optional[str] = None
    used_fallback: bool = False
    fallback_reason: Optional[str] = None
    error: Optional[str] = None


def _launch_via_startfile(path: Path) -> None:
    """Open ``path`` with whatever application Windows associates with its
    extension. Raises on error (e.g. ``OSError`` if there's no
    association).

    ``os.startfile`` doesn't exist on Linux -- referenced only via
    ``getattr`` here, inside the function body, so this module still
    imports cleanly on the Linux sandbox (the attribute is only looked up
    when this function actually runs, which happens under tests that
    monkeypatch it in via ``monkeypatch.setattr(os, "startfile", fake,
    raising=False)``, or for real on Windows).
    """
    getattr(os, "startfile")(str(path))


def open_transcript_in_app(
    text: str,
    desktop_id: str,
    directory: Optional[Path] = None,
    now: Optional[datetime] = None,
    launcher: Callable[[Path], None] = _launch_via_startfile,
) -> LaunchResult:
    """Write the transcript to a throwaway temp file and open it with the
    system's default ``.txt`` handler.

    ``desktop_id`` is accepted only for signature compatibility with the
    Linux ``open_transcript_in_app`` (so shared caller code doesn't need
    an ``if platform`` branch) -- it is completely ignored on Windows,
    which always launches via the single OS default handler.

    Behavior (mirrors the Linux contract):

    * Unless ``directory`` is given explicitly, the file is written under
      a fresh temp directory (not ``~/Documents/MyVoice Transcripts/``)
      so simply opening a transcript to view or edit it never creates a
      permanent artifact on its own.
    * The file is always written, even if launching the app fails.
    * If the launch fails (e.g. no application is associated with
      ``.txt``, which practically never happens on a real Windows install
      but is handled defensively), returns ``launched=False`` with
      ``error`` set -- the caller should fall back to revealing the
      folder (see :func:`reveal_transcript_folder`).
    """
    target_dir = (
        directory if directory is not None
        else Path(tempfile.mkdtemp(prefix="myvoice-transcript-"))
    )
    path = save_transcript_to_file(text, directory=target_dir, now=now)

    try:
        launcher(path)
        return LaunchResult(path=path, launched=True)
    except Exception as e:
        log.exception("Failed to launch default app for transcript")
        return LaunchResult(path=path, launched=False, error=str(e))


def reveal_transcript_folder(path: Path) -> None:
    """Best-effort: open the folder containing ``path`` in File Explorer.

    Intended as a last-resort fallback for the caller to invoke when
    ``open_transcript_in_app`` reports ``launched=False`` -- mirroring the
    role of the Linux app's ``_reveal_transcript_folder`` (which uses
    ``Gio.AppInfo.launch_default_for_uri`` on the containing directory).
    Uses ``os.startfile`` on the parent directory, the same underlying
    mechanism as the file-open path, so only one Windows API surface needs
    to be exercised/mocked in tests. Never raises.
    """
    try:
        getattr(os, "startfile")(str(path.parent))
    except Exception as e:
        log.debug("Could not reveal transcript folder: %s", e)
