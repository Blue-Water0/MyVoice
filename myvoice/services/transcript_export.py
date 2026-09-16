"""Transcript export: write the full session transcript to a real UTF-8
``.txt`` file and open it in a user-chosen text application.

``open_transcript_in_app`` writes to a throwaway temp file by default —
opening a transcript to view/edit it is not itself a permanent save. Use
``save_transcript_to_file`` directly (with no ``directory`` override) for
an actual deliberate save to ``~/Documents/MyVoice Transcripts/``.

Design goals:

* Pure-logic parts (filename generation, target directory, UTF-8 write,
  installed-editor discovery, fallback selection) live in module-level
  functions and small dataclasses so they can be unit-tested without
  GTK / Gio.
* GIO / GObject calls live behind small wrappers that we can substitute
  in tests. The launch path uses ``Gio.AppInfo.launch`` with a
  ``Gio.File`` — never shell string concatenation, never ``shell=True``,
  never an arbitrary user-supplied command line.
* We only ever discover applications registered for the ``text/plain``
  MIME type via ``Gio.AppInfo.get_all_for_type`` and we exclude
  applications that expect to run in a terminal (``Terminal=true``).
  We never enumerate every installed app on the machine.
"""
from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..paths import transcripts_dir

log = logging.getLogger(__name__)

TEXT_PLAIN = "text/plain"

# Sentinel desktop-id used in Settings to mean "use whatever the system
# has configured as the default handler for text/plain".
SYSTEM_DEFAULT_ID = ""


# ---------------------------------------------------------------------------
# Filename + on-disk save
# ---------------------------------------------------------------------------


_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 _\-]+")


def make_transcript_filename(now: Optional[datetime] = None,
                             prefix: str = "MyVoice Transcript") -> str:
    """Return a safe timestamped ``.txt`` filename.

    Format::

        MyVoice Transcript YYYY-MM-DD HH-MM-SS.txt

    Colons and dots are avoided in the prefix so no path-traversal or
    filesystem-weird combinations can be constructed. The prefix is
    scrubbed of anything that isn't alphanumeric, space, dash, or
    underscore to guarantee the returned string is a safe filename on
    ext4, exFAT, NTFS, and Samba. The ``.txt`` extension is appended
    literally.
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

    Returns the absolute :class:`Path` of the saved file. Uses explicit
    ``utf-8`` encoding — non-ASCII (Hebrew, Arabic, emoji, etc.) is
    written correctly and can be re-opened by any UTF-8-aware editor.
    If a file with the same timestamped name already exists (e.g. two
    saves within the same second), a `` (2)``, `` (3)``, ... suffix is
    added so the earlier file is never overwritten.
    """
    target_dir = directory if directory is not None else transcripts_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(target_dir, make_transcript_filename(now=now))
    # Explicit UTF-8, LF newlines. We don't add a BOM — modern editors
    # on Linux Mint (Xed, Gedit, VS Code, Kate, etc.) all handle
    # BOM-less UTF-8 correctly.
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Editor discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextAppEntry:
    """A discovered application capable of opening ``text/plain``.

    ``desktop_id`` is the freedesktop desktop-file id (e.g.
    ``org.x.editor.desktop``). ``name`` is human-readable. ``icon``
    is the icon name string suitable for ``Gtk.Image.new_from_icon_name``
    or ``None`` if the app didn't expose one.
    """

    desktop_id: str
    name: str
    icon: Optional[str] = None


def _default_gio_lister() -> list:
    """Return the raw list of ``Gio.AppInfo`` for text/plain, or []."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # type: ignore
    except Exception as e:
        log.debug("Gio not available: %s", e)
        return []
    try:
        return list(Gio.AppInfo.get_all_for_type(TEXT_PLAIN))
    except Exception as e:
        log.debug("get_all_for_type failed: %s", e)
        return []


def _default_gio_default_for_type() -> Optional[object]:
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # type: ignore
    except Exception as e:
        log.debug("Gio not available: %s", e)
        return None
    try:
        return Gio.AppInfo.get_default_for_type(TEXT_PLAIN, False)
    except Exception as e:
        log.debug("get_default_for_type failed: %s", e)
        return None


def _appinfo_is_terminal(app: object) -> bool:
    """Return True if the AppInfo declares ``Terminal=true``.

    Terminal-embedded editors (vim, nvim, nano invoked from a terminal)
    would not open a windowed editor when launched — filter them out.
    """
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # type: ignore
        if isinstance(app, Gio.DesktopAppInfo):
            try:
                return bool(app.get_boolean("Terminal"))
            except Exception:
                return False
    except Exception:
        pass
    return False


def discover_text_apps(
    lister: Callable[[], list] = _default_gio_lister,
) -> list[TextAppEntry]:
    """Return installed applications registered for ``text/plain``.

    Terminal-embedded applications (``Terminal=true``) are excluded so
    the dropdown only offers editors that will actually open a window.
    Deduplicated by desktop-id. The order follows Gio's own preference
    order (which normally puts the user's default first).
    """
    seen: set[str] = set()
    out: list[TextAppEntry] = []
    for app in lister():
        try:
            did = app.get_id() or ""
        except Exception:
            continue
        if not did or did in seen:
            continue
        if _appinfo_is_terminal(app):
            continue
        try:
            name = app.get_display_name() or app.get_name() or did
        except Exception:
            name = did
        icon_name: Optional[str] = None
        try:
            icon = app.get_icon()
            if icon is not None:
                try:
                    icon_name = icon.to_string()
                except Exception:
                    icon_name = None
        except Exception:
            icon_name = None
        seen.add(did)
        out.append(TextAppEntry(desktop_id=did, name=name, icon=icon_name))
    return out


def get_default_text_app(
    default_getter: Callable[[], Optional[object]] = _default_gio_default_for_type,
) -> Optional[TextAppEntry]:
    """Return the system's configured default handler for text/plain."""
    app = default_getter()
    if app is None:
        return None
    try:
        did = app.get_id() or ""
        name = app.get_display_name() or app.get_name() or did
    except Exception:
        return None
    if not did:
        return None
    return TextAppEntry(desktop_id=did, name=name)


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


@dataclass
class LaunchResult:
    """Result of an "open transcript in text app" attempt.

    ``path`` is always set to the saved ``.txt`` file — even when the
    launch failed — so the caller can show its location to the user
    and they can open it manually from the file manager.
    """

    path: Path
    launched: bool
    app_name: Optional[str] = None
    used_fallback: bool = False
    fallback_reason: Optional[str] = None
    error: Optional[str] = None


def _resolve_app(desktop_id: str,
                 default_getter: Callable[[], Optional[object]]
                 = _default_gio_default_for_type) -> tuple[Optional[object], bool, Optional[str]]:
    """Resolve ``desktop_id`` to a ``Gio.AppInfo``.

    Returns ``(appinfo, used_fallback, reason)``:

    * If ``desktop_id`` is empty (``SYSTEM_DEFAULT_ID``), return the
      system default and ``used_fallback=False``.
    * If ``desktop_id`` names an installed desktop entry, return it
      and ``used_fallback=False``.
    * If ``desktop_id`` is set but not installed, fall back to the
      system default and set ``used_fallback=True`` with a reason.
    """
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # type: ignore
    except Exception as e:
        return (None, False, f"Gio unavailable: {e}")

    if desktop_id == SYSTEM_DEFAULT_ID:
        return (default_getter(), False, None)

    app = Gio.DesktopAppInfo.new(desktop_id)
    if app is not None:
        return (app, False, None)

    # Configured app not installed. Fall back to system default.
    fb = default_getter()
    reason = f"Configured text app '{desktop_id}' is not installed"
    return (fb, True, reason)


def _launch_via_gio(app: object, path: Path) -> None:
    """Launch ``app`` with the ``file://`` URI of ``path``. Raises on error.

    Uses ``Gio.AppInfo.launch()`` with a ``Gio.File`` list — safe. Never
    invokes a shell.
    """
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio  # type: ignore

    gfile = Gio.File.new_for_path(str(path))
    # launch() returns True on success, may raise GError.
    app.launch([gfile], None)


def open_transcript_in_app(
    text: str,
    desktop_id: str,
    directory: Optional[Path] = None,
    now: Optional[datetime] = None,
    default_getter: Callable[[], Optional[object]] = _default_gio_default_for_type,
    launcher: Callable[[object, Path], None] = _launch_via_gio,
) -> LaunchResult:
    """Write the transcript to a throwaway temp file and open it in the
    chosen app — a view/edit scratch copy, not a permanent save.

    Behavior:

    * Unless ``directory`` is given explicitly, the file is written under
      a fresh temp directory (not ``~/Documents/MyVoice Transcripts/``)
      so simply opening a transcript to view or edit it never creates a
      permanent artifact on its own. If the user wants to keep it, they
      save it themselves from the editor (e.g. Save As) — at that point
      it's a deliberate choice of theirs, not something MyVoice decided
      for them.
    * The file is always written, even if launching the app fails or no
      text application can be found. The caller is expected to show
      the path to the user so they can open it manually.
    * If the configured ``desktop_id`` refers to an uninstalled app,
      falls back to the system default text/plain handler.
    * If no application can be resolved, returns ``launched=False`` —
      the caller should present the folder in the file manager.
    """
    # 1. Write the file first — to a throwaway temp dir by default; see
    #    docstring above for why this is deliberately not the permanent
    #    transcripts folder.
    target_dir = (
        directory if directory is not None
        else Path(tempfile.mkdtemp(prefix="myvoice-transcript-"))
    )
    path = save_transcript_to_file(text, directory=target_dir, now=now)

    # 2. Resolve the target application.
    app, used_fallback, fallback_reason = _resolve_app(
        desktop_id, default_getter=default_getter,
    )
    if app is None:
        return LaunchResult(
            path=path,
            launched=False,
            app_name=None,
            used_fallback=used_fallback,
            fallback_reason=fallback_reason,
            error="No text application registered for text/plain",
        )

    # 3. Attempt to launch. Even a failed launch does not delete the file.
    app_name: Optional[str]
    try:
        app_name = app.get_display_name() or app.get_name()
    except Exception:
        app_name = None
    try:
        launcher(app, path)
        return LaunchResult(
            path=path,
            launched=True,
            app_name=app_name,
            used_fallback=used_fallback,
            fallback_reason=fallback_reason,
        )
    except Exception as e:
        log.exception("Failed to launch text app for transcript")
        return LaunchResult(
            path=path,
            launched=False,
            app_name=app_name,
            used_fallback=used_fallback,
            fallback_reason=fallback_reason,
            error=str(e),
        )
