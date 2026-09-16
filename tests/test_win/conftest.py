"""Shared test setup: isolate Windows env vars so tests don't touch the
real user profile.

Mirrors ``tests/conftest.py``'s XDG isolation, but for the Windows-specific
env vars (``APPDATA`` / ``LOCALAPPDATA``) that ``myvoice_win.paths`` reads.
"""
from __future__ import annotations

import pathlib

import pytest


@pytest.fixture(autouse=True)
def _isolate_windows_env(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Setting HOME alone does NOT redirect Path.home() on Windows: CPython's
    # ntpath.expanduser() (what Path.home() calls under the hood on
    # Windows) checks USERPROFILE, never HOME -- confirmed by a real CI
    # run, where tests relying on HOME silently fell through to the
    # runner's actual C:\Users\<name> profile instead of tmp_path. Patching
    # Path.home() directly works identically on both platforms and is what
    # every test in this suite that needs an isolated "home" should rely
    # on -- HOME above is still set for any code that reads the env var
    # directly, but Path.home() itself needs this.
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    yield


@pytest.fixture(scope="session")
def qapp():
    """A single shared, headless QApplication for the Qt-based UI tests.

    PySide6 is imported lazily inside this fixture (not at module top
    level) so that test files which never request ``qapp`` -- i.e. every
    non-UI test in this directory -- never pay the PySide6 import cost
    and never require a display.

    ``QT_QPA_PLATFORM`` defaults to ``offscreen`` here (via ``setdefault``)
    so the fixture is safe whether invoked as
    ``QT_QPA_PLATFORM=offscreen python -m pytest tests/test_win`` or as
    plain ``python -m pytest tests`` (which also collects this directory,
    since ``testpaths = ["tests"]``) on a sandbox with no real display.
    An already-set ``QT_QPA_PLATFORM`` (e.g. a real display in CI) is left
    untouched.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(["myvoice-tests"])
    yield app
