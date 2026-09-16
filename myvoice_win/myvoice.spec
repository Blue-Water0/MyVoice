# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for MyVoice -- Windows **one-folder** build.

    pyinstaller myvoice_win\\myvoice.spec

Run from the repository root on a Windows build machine with
``myvoice_win\\requirements-win.txt`` installed into the active venv (see
design doc "docs/superpowers/plans/2026-07-13-windows-port-plan.md" section
"## 3. Packaging" for the full build process and installer-contents spec
this file implements).

Why one-folder, never one-file
-------------------------------
Design doc section 3 rules out PyInstaller's one-file mode for this app:

* One-file builds re-extract the whole ~150 MB payload to a temp directory
  on *every* launch -- slow startup for an app meant to be summoned
  instantly by a global hotkey.
* The self-extracting stub triggers far more antivirus heuristic-unpacking
  false positives than a plain folder of files.
* Uninstall is incomplete -- extracted temp files can survive removal.

A one-folder build (this file) sidesteps all three; Inno Setup
(``myvoice_win/setup.iss``) wraps the resulting folder into a normal
installer.

Bootstrap entry script
----------------------
``myvoice_win/app.py`` uses a package-relative import at module level
(``from . import APP_NAME``), which only resolves when the module is
loaded *as part of* the ``myvoice_win`` package. PyInstaller's ``Analysis``
instead execs whatever script path it is given directly as ``__main__`` --
with no parent package -- so pointing it straight at ``app.py`` would fail
at freeze time with "attempted relative import with no known parent
package". Rather than adding a permanent extra source module to the
tree just to dodge that (this task's brief calls for exactly two new
files: this spec and ``setup.iss``), this spec generates a tiny two-line
bootstrap script itself, at spec-execution time, under the already-
gitignored ``build/`` directory, and points ``Analysis`` at the generated
file instead of ``app.py`` directly. The generated file is not part of the
source tree and is never committed.
"""

import pathlib

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# ``SPECPATH`` is a name PyInstaller injects into this file's globals when it
# execs the .spec (it is the absolute directory containing this file, i.e.
# ``<repo>/myvoice_win``). Not defined outside a real PyInstaller run --
# that is fine, this file is only ever *executed* by PyInstaller; a plain
# ``compile()`` syntax check (as used for this task's sandbox validation)
# never evaluates names, so their absence there is harmless.
_spec_dir = pathlib.Path(SPECPATH).resolve()
_repo_root = _spec_dir.parent

_build_dir = _repo_root / "build" / "pyi_bootstrap"
_build_dir.mkdir(parents=True, exist_ok=True)
_entry_script = _build_dir / "myvoice_entry.py"
_entry_script.write_text(
    "from myvoice_win.app import main\n"
    "import sys\n"
    "sys.exit(main())\n",
    encoding="utf-8",
)

# Optional app icon. No .ico ships in the repo yet (only PySide6's own
# placeholder icon exists on disk, which is not ours to use) -- if/when one
# is added, drop it at this path and the build will pick it up automatically;
# until then the frozen exe just gets PyInstaller's default icon.
_icon_path = _repo_root / "myvoice_win" / "myvoice.ico"
_icon = str(_icon_path) if _icon_path.exists() else None

# ---------------------------------------------------------------------------
# myvoice.* modules actually imported by myvoice_win code (derived via
#   grep -rn "from myvoice\." myvoice_win --include="*.py"
# -- do not add to this list without re-running that grep; these are the
# "safe to import directly" modules per the plan's Module Reuse Map, and
# every one of them is pure logic / cross-platform at *module import
# time* (lazy imports of sounddevice/webrtcvad happen inside functions).
# PyInstaller's own modulegraph walk should already find every one of these
# by following the import statements above -- they are listed again here
# explicitly per this task's brief, as defensive belt-and-braces in case a
# future refactor introduces a dynamic/conditional import PyInstaller's
# static analysis cannot see.
# ---------------------------------------------------------------------------
myvoice_hidden_imports = [
    "myvoice.engines.base",
    "myvoice.services.app_classifier",
    "myvoice.services.audio_service",
    "myvoice.services.language_service",
    "myvoice.services.timing",
    "myvoice.services.transcript_buffer",
    "myvoice.services.transcription_service",
    "myvoice.services.vad_service",
]

# ---------------------------------------------------------------------------
# Known PyInstaller/dependency packaging quirks (see task-13-report.md for
# the full write-up of which of these are confident vs. speculative):
# ---------------------------------------------------------------------------
quirk_hidden_imports = [
    # plyer resolves its notification backend via importlib.import_module()
    # at *runtime*, branching on sys.platform inside plyer/notification.py
    # -- a dynamic import PyInstaller's static graph walk cannot see. Left
    # out, the frozen app would silently fall back to plyer's no-op
    # DummyNotification backend on Windows (notifications would just never
    # appear, with no error -- notifications.notify() never raises by
    # design, so this failure mode is otherwise invisible).
    "plyer.platforms.win.notification",
    # comtypes.client generates wrapper modules into a `comtypes.gen`
    # cache package the first time an early-bound COM interface is used.
    # myvoice_win.services.focus_tracker only calls comtypes.client.
    # CreateObject() with a bare CLSID string (no `interface=` kwarg / no
    # GetModule() call), which is the late-bound code path and likely does
    # NOT require comtypes.gen -- but comtypes.client itself still probes
    # for a writable comtypes.gen package on import, and that probe is a
    # well-documented source of PyInstaller+comtypes friction (frozen
    # bundles are not always writable/importable the same way a normal
    # site-packages install is). Listing the package here is cheap
    # insurance; if COM errors mentioning "gen_dir" or read-only caches
    # surface on the real Windows build, the standard fix is setting
    # `comtypes.client.gen_dir = None` before first use so it generates
    # in-memory instead of trying to persist to disk.
    "comtypes.gen",
]

hiddenimports = myvoice_hidden_imports + quirk_hidden_imports

# ---------------------------------------------------------------------------
# Data files PyInstaller's static import analysis cannot discover on its
# own, because they are loaded as *non-Python package data* at runtime
# rather than imported.
# ---------------------------------------------------------------------------
datas = []

# faster-whisper ships assets/silero_vad_v6.onnx as package data (used for
# its own optional built-in VAD pre-filter). myvoice_win's engine always
# calls .transcribe(..., vad_filter=False) -- we do our own VAD upstream via
# webrtcvad -- so this asset is not expected to be touched at runtime today.
# Included anyway as a defensive measure: it is one small file, and it
# guards against a future config change (or a faster-whisper internal
# code path) that ends up needing it, which would otherwise be a silent
# runtime failure invisible until someone hits that exact path in the field.
datas += collect_data_files("faster_whisper")

# PySide6's Qt "platforms" plugin (qwindows.dll on Windows) is what lets
# QApplication initialize at all -- "This application failed to start
# because no Qt platform plugin could be initialized" is one of the most
# common PyInstaller+Qt failure reports on Windows. PyInstaller's bundled
# PySide6 hook is normally supposed to collect this automatically, but that
# hook has regressed across PyInstaller/hooks-contrib versions often enough
# that it is worth collecting explicitly rather than trusting it silently.
# Scoped to just the "platforms" and "styles" plugin subdirectories (not
# all of PySide6) to avoid bloating the installer with QML/docs/examples
# this app never touches.
datas += collect_data_files("PySide6", subdir=str(pathlib.Path("Qt", "plugins", "platforms")))
datas += collect_data_files("PySide6", subdir=str(pathlib.Path("Qt", "plugins", "styles")))

# sounddevice's Windows wheel bundles the PortAudio shared library inside a
# private `_sounddevice_data` package (a prebuilt portaudio DLL), loaded via
# ctypes at runtime rather than a normal Python import -- again invisible
# to PyInstaller's static analysis. Wrapped in try/except because this
# sandbox has no Windows sounddevice wheel to introspect (Linux installs
# use the system libportaudio.so instead and never install this package),
# so the exact package name/shape here is asserted from sounddevice's
# documented Windows packaging, not verified against a real install --
# confirm on the Windows build machine and adjust if the wheel layout has
# changed.
try:
    datas += collect_data_files("_sounddevice_data")
except (ImportError, ModuleNotFoundError):
    pass

# ---------------------------------------------------------------------------
# Native/compiled binary dependencies. PyInstaller's binary dependency walk
# (parsing each collected extension's PE import table) should already catch
# ctranslate2's and onnxruntime's directly-linked sibling DLLs automatically
# -- these two calls are a defensive duplicate (PyInstaller de-duplicates,
# so this is harmless) covering the case where either library loads a
# provider/runtime DLL dynamically (LoadLibrary/dlopen-style plugin
# loading) rather than through its import table, which the static PE walk
# would miss. Not verified on a real Windows build.
# ---------------------------------------------------------------------------
binaries = []
binaries += collect_dynamic_libs("ctranslate2")
binaries += collect_dynamic_libs("onnxruntime")

# numpy and webrtcvad are plain PyInstaller-discovered dependencies with
# mature, well-tested hooks (numpy) / a simple single compiled extension
# with no bundled data files (webrtcvad) -- no special handling needed.

a = Analysis(
    [str(_entry_script)],
    pathex=[str(_repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MyVoice",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed (no console) app -- MyVoice is a tray/dictation app with no
    # terminal UI; a console window popping up alongside it would be a bug,
    # not a feature.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MyVoice",
)
