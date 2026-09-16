from __future__ import annotations

import logging
import logging.handlers

import pytest

from myvoice_win.services import logging_setup


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """setup_logging() is a call-once singleton guarded by a module-level
    flag; reset it (and the root logger's handlers) around every test so
    each test observes a clean initialization.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    logging_setup._INITIALIZED = False
    for h in list(root.handlers):
        root.removeHandler(h)
    yield
    logging_setup._INITIALIZED = False
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_setup_logging_creates_log_file():
    from myvoice_win import paths

    log_path = logging_setup.setup_logging("DEBUG")
    assert log_path == paths.log_dir() / "myvoice.log"
    assert log_path.exists()


def test_setup_logging_is_idempotent():
    first = logging_setup.setup_logging("INFO")
    handler_count_after_first = len(logging.getLogger().handlers)
    second = logging_setup.setup_logging("INFO")
    assert first == second
    assert len(logging.getLogger().handlers) == handler_count_after_first


def test_sets_root_logger_level():
    logging_setup.setup_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_stderr_handler_added_when_env_var_set(monkeypatch):
    monkeypatch.setenv("MYVOICE_LOG_STDERR", "1")
    logging_setup.setup_logging("INFO")
    root = logging.getLogger()
    assert any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
        for h in root.handlers
    )


def test_no_stderr_handler_when_not_tty_and_env_unset(monkeypatch):
    monkeypatch.delenv("MYVOICE_LOG_STDERR", raising=False)

    class _NotATty:
        def isatty(self):
            return False

    monkeypatch.setattr(logging_setup.sys, "stderr", _NotATty())
    logging_setup.setup_logging("INFO")
    root = logging.getLogger()
    stream_handlers = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert stream_handlers == []
