"""Shared test setup: isolate XDG paths so tests don't touch real config."""
from __future__ import annotations


import pytest


@pytest.fixture(autouse=True)
def _isolate_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path))
    yield
