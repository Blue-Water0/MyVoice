"""Capture the currently-focused window/control via Win32 + UIA.

Discovery-only: this module never inserts, pastes, or otherwise mutates
anything. It exists purely to answer "what is focused right now, and is it
safe to treat as a password field", so callers (the text-injection service)
can decide how/whether to deliver dictated text. It has no state — every
call to :func:`capture_focus` re-queries the current foreground window.

UI Automation (UIA) is used narrowly and defensively here: it is the only
reliable cross-application way on Windows to learn whether the focused
control is a password field (``IUIAutomationElement.CurrentIsPassword``).
UIA can legitimately fail to produce an element for a given control (no UIA
provider registered, COM hiccup, etc.) — that must degrade to "not a
password field", never raise out of :func:`capture_focus`.

Cross-process safety rule: a UIA element's reported PID
(``CurrentProcessId``) can differ from the foreground window's owning PID
in edge cases (e.g. a UIA provider serving a different process than the one
that currently owns the visible window). When that happens, the UIA
element is untrustworthy for password detection and ``is_password`` is
forced to ``False`` regardless of what UIA reported.

Import-safety: neither ``ctypes.windll`` nor ``comtypes`` exist/import on
non-Windows platforms (``ctypes.windll`` raises ``AttributeError`` if
referenced at import time; ``import comtypes`` raises ``ImportError``
immediately, even before any Windows-specific attribute is touched). Every
reference to either lives inside a function body, never at module level, so
this module stays importable on Linux for pytest collection.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ._win32util import set_signature as _set_signature

log = logging.getLogger(__name__)


@dataclass
class TargetRef:
    """Snapshot of the currently-focused window/control.

    ``uia_pid`` is kept separate from ``foreground_pid`` so callers can
    apply the mismatch rule themselves if needed: "if the UIA element's PID
    differs from the foreground window's PID, treat the UIA element as
    unavailable for password detection." ``capture_focus`` already applies
    this rule to ``is_password`` before returning, but ``uia_pid`` still
    reflects whatever UIA reported so callers/logs can see the raw value.
    """

    hwnd: Optional[int]
    foreground_pid: Optional[int]
    uia_pid: Optional[int]
    is_password: bool
    hwnd_class: Optional[str]


# ---- Win32 helpers ----------------------------------------------------------


def _get_foreground_hwnd() -> Optional[int]:
    import ctypes  # local: ctypes.windll doesn't exist on Linux
    from ctypes import wintypes

    fn = ctypes.windll.user32.GetForegroundWindow
    _set_signature(fn, restype=wintypes.HWND, argtypes=[])
    hwnd = fn()
    return hwnd or None


def _get_foreground_pid(hwnd: int) -> Optional[int]:
    import ctypes  # local: ctypes.windll doesn't exist on Linux
    from ctypes import wintypes

    fn = ctypes.windll.user32.GetWindowThreadProcessId
    _set_signature(
        fn, restype=wintypes.DWORD,
        argtypes=[wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
    )
    pid = wintypes.DWORD(0)
    fn(hwnd, ctypes.byref(pid))
    return pid.value or None


def _get_hwnd_class(hwnd: int) -> Optional[str]:
    import ctypes  # local: ctypes.windll doesn't exist on Linux
    from ctypes import wintypes

    fn = ctypes.windll.user32.GetClassNameW
    _set_signature(
        fn, restype=ctypes.c_int,
        argtypes=[wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
    )
    buf = ctypes.create_unicode_buffer(256)
    n = fn(hwnd, buf, 256)
    return buf.value if n else None


# ---- UIA discovery (discovery-only, never used for text insertion) --------


def _discover_uia(foreground_pid: Optional[int]) -> tuple[bool, Optional[int]]:
    """Best-effort UIA discovery of the focused element's password state.

    Returns ``(is_password, uia_pid)``. Any failure anywhere in this
    function (COM not available, no focused element, no UIA provider for
    the control, property read errors) degrades to ``(False, None)`` — it
    must never raise out of here.
    """
    try:
        import comtypes.client as comtypes_client  # local: comtypes doesn't import on Linux

        # CUIAutomation is the standard UIA COM automation object
        # (documented ProgID for Microsoft's UI Automation API).
        uia = comtypes_client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}")
        element = uia.GetFocusedElement()
        if element is None:
            return False, None

        uia_pid = getattr(element, "CurrentProcessId", None)
        raw_is_password = bool(getattr(element, "CurrentIsPassword", False))
    except Exception:
        log.debug("UIA discovery failed; degrading to is_password=False", exc_info=True)
        return False, None

    # Whitelist rule: only trust the UIA element's password flag when both
    # PIDs are known AND they match. Any other case — either PID missing
    # (e.g. GetForegroundWindow legitimately returned 0/no focused window,
    # or UIA didn't report a PID) or a genuine mismatch — must default to
    # is_password=False; a UIA element we can't confirm belongs to the
    # foreground window is untrustworthy for password detection.
    if foreground_pid is not None and uia_pid is not None and uia_pid == foreground_pid:
        return raw_is_password, uia_pid

    return False, uia_pid


# ---- Public API --------------------------------------------------------------


def capture_focus() -> TargetRef:
    """Snapshot the currently-focused window/control.

    Discovery-only — the returned :class:`TargetRef` is meant to inform a
    caller's decision about whether/how to deliver text; this function
    itself never inserts, pastes, or otherwise touches the target.
    """
    hwnd = _get_foreground_hwnd()

    foreground_pid: Optional[int] = None
    hwnd_class: Optional[str] = None
    if hwnd is not None:
        foreground_pid = _get_foreground_pid(hwnd)
        hwnd_class = _get_hwnd_class(hwnd)

    is_password, uia_pid = _discover_uia(foreground_pid)

    ref = TargetRef(
        hwnd=hwnd,
        foreground_pid=foreground_pid,
        uia_pid=uia_pid,
        is_password=is_password,
        hwnd_class=hwnd_class,
    )
    log.debug(
        "capture_focus: hwnd=%s foreground_pid=%s hwnd_class=%s uia_pid=%s "
        "is_password=%s",
        ref.hwnd, ref.foreground_pid, ref.hwnd_class, ref.uia_pid, ref.is_password,
    )
    return ref
