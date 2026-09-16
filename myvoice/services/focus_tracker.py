"""Capture the currently-focused editable target via AT-SPI + X11.

Returns a ``TargetRef`` describing what the caller can safely write
into. This module has no state — every call walks the AT-SPI desktop
tree to find whatever is focused *right now*.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass
class TargetRef:
    """Snapshot of what is currently focused.

    ``writable()`` is True when either the AT-SPI ``EditableText``
    interface is exposed, or we at least have an X11 window id we can
    paste into. Password fields are never considered writable.
    """
    accessible: Any = None            # pyatspi Accessible or None
    xid: Optional[int] = None         # X11 window id (from xdotool getactivewindow)
    app_name: Optional[str] = None    # human-readable, best-effort
    is_password: bool = False
    supports_editable_text: bool = False
    role: Optional[str] = None        # pyatspi role name string, best-effort
    pid: Optional[int] = None         # owning process id of the focused accessible
    # (instance_lower, class_lower) from X11 WM_CLASS, both already lowercased.
    # None if the XID is unknown or the property cannot be read.
    wm_class: Optional[tuple[str, str]] = None

    def writable(self) -> bool:
        return not self.is_password and (self.supports_editable_text or self.xid is not None)


# --- AT-SPI helpers ---------------------------------------------------------


def _get_pyatspi():
    try:
        import pyatspi  # type: ignore
        return pyatspi
    except Exception as e:
        log.debug("pyatspi unavailable: %s", e)
        return None


def _walk_focus(root, pyatspi) -> Optional[Any]:
    """DFS through the accessibility tree until we find the focused accessible."""
    if root is None:
        return None
    try:
        state = root.getState()
        if state.contains(pyatspi.STATE_FOCUSED):
            return root
    except Exception:
        pass
    try:
        n = root.childCount
    except Exception:
        n = 0
    for i in range(n):
        try:
            child = root.getChildAtIndex(i)
        except Exception:
            child = None
        if child is None:
            continue
        try:
            state = child.getState()
            if not state.contains(pyatspi.STATE_SHOWING):
                # Skip hidden subtrees to keep this fast.
                continue
        except Exception:
            pass
        found = _walk_focus(child, pyatspi)
        if found is not None:
            return found
    return None


def _find_focused_accessible():
    pyatspi = _get_pyatspi()
    if pyatspi is None:
        return None
    try:
        desktop = pyatspi.Registry.getDesktop(0)
    except Exception as e:
        log.debug("getDesktop failed: %s", e)
        return None
    try:
        for i in range(desktop.childCount):
            try:
                app = desktop.getChildAtIndex(i)
            except Exception:
                continue
            found = _walk_focus(app, pyatspi)
            if found is not None:
                return found
    except Exception as e:
        log.debug("desktop walk failed: %s", e)
    return None


def _is_password_role(acc) -> bool:
    pyatspi = _get_pyatspi()
    if pyatspi is None or acc is None:
        return False
    try:
        role = acc.getRole()
        return role == pyatspi.ROLE_PASSWORD_TEXT
    except Exception:
        return False


def _supports_editable_text(acc) -> bool:
    if acc is None:
        return False
    try:
        et = acc.queryEditableText()  # type: ignore[attr-defined]
        return et is not None
    except Exception:
        return False


def _get_application(acc):
    """Walk up to the AT-SPI Application accessible (root of the a11y
    tree for that process)."""
    if acc is None:
        return None
    try:
        cur = acc
        while cur is not None:
            try:
                parent = cur.getParent()
            except Exception:
                parent = None
            if parent is None:
                return cur  # cur is the Application accessible
            cur = parent
    except Exception:
        return None
    return None


def _get_application_name(app_acc) -> Optional[str]:
    if app_acc is None:
        return None
    try:
        return app_acc.name
    except Exception:
        return None


def _get_role_name(acc) -> Optional[str]:
    if acc is None:
        return None
    try:
        return acc.getRoleName()
    except Exception:
        try:
            return str(acc.getRole())
        except Exception:
            return None


def _get_pid(acc) -> Optional[int]:
    """Return the PID that owns this Accessible, or None if unknown."""
    if acc is None:
        return None
    getter = getattr(acc, "get_process_id", None)
    if getter is not None:
        try:
            pid = int(getter())
            if pid > 0:
                return pid
        except Exception:
            pass
    app = _get_application(acc)
    if app is None:
        return None
    getter = getattr(app, "get_process_id", None)
    if getter is None:
        return None
    try:
        pid = int(getter())
        return pid if pid > 0 else None
    except Exception:
        return None


# --- X11 helpers ------------------------------------------------------------


def _xlib_wm_class(xid: int) -> Optional[tuple[str, str]]:
    """Query the X11 WM_CLASS property for ``xid`` via python-xlib.

    Returns (instance_lower, class_lower) or None on any failure.
    WM_CLASS contains two null-terminated strings; Xlib returns them
    as a (instance, class) tuple from get_wm_class().
    """
    try:
        from Xlib import display as _xdisplay  # type: ignore
        d = _xdisplay.Display()
        window = d.create_resource_object("window", xid)
        wm = window.get_wm_class()
        d.close()
        if wm and len(wm) >= 2:
            instance = (wm[0] or "").lower()
            klass = (wm[1] or "").lower()
            return (instance, klass)
    except Exception as e:
        log.debug("_xlib_wm_class(%s) failed: %s", xid, e)
    return None


def _xdotool_active_window() -> Optional[int]:
    if not shutil.which("xdotool"):
        return None
    try:
        out = subprocess.check_output(
            ["xdotool", "getactivewindow"], stderr=subprocess.DEVNULL, timeout=1.0,
        )
        return int(out.decode().strip())
    except Exception as e:
        log.debug("xdotool getactivewindow failed: %s", e)
        return None


# --- Public API -------------------------------------------------------------


def capture_focus() -> TargetRef:
    """Snapshot the currently-focused editable target.

    Called from the injector on every chunk in follow-current-focus
    mode. Cheap enough to do per-chunk (small AT-SPI walk + one
    xdotool call + one X11 property read).
    """
    xid = _xdotool_active_window()
    acc = _find_focused_accessible()
    is_pw = _is_password_role(acc)
    editable = _supports_editable_text(acc) and not is_pw
    wm_class = _xlib_wm_class(xid) if xid is not None else None
    ref = TargetRef(
        accessible=acc,
        xid=xid,
        app_name=_get_application_name(_get_application(acc)),
        is_password=is_pw,
        supports_editable_text=editable,
        role=_get_role_name(acc),
        pid=_get_pid(acc),
        wm_class=wm_class,
    )
    log.debug(
        "capture_focus: app=%s pid=%s role=%s xid=%s editable=%s "
        "password=%s wm_class=%s",
        ref.app_name, ref.pid, ref.role, ref.xid,
        ref.supports_editable_text, ref.is_password, ref.wm_class,
    )
    return ref
