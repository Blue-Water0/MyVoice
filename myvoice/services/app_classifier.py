"""App classification by WM_CLASS for paste shortcut selection.

classify(wm_class, extra_terminals) -> AppClass.TERMINAL | AppClass.NORMAL

All matching is case-insensitive. Both the instance and class values of
WM_CLASS are checked. Unknown apps and missing WM_CLASS default to NORMAL.
"""
from __future__ import annotations

import enum
from typing import Optional

# Lowercase WM_CLASS instance/class names that belong to terminal emulators.
# Both the instance name (e.g. "gnome-terminal-server") and the class name
# (e.g. "gnome-terminal") are checked after lowercasing.
KNOWN_TERMINALS: frozenset[str] = frozenset({
    "gnome-terminal",
    "gnome-terminal-server",
    "xfce4-terminal",
    "xterm",
    "uxterm",
    "kitty",
    "alacritty",
    "konsole",
    "tilix",
    "terminator",
    "mate-terminal",
    "lxterminal",
    "wezterm",
    "org.wezfurlong.wezterm",
    "rxvt",
    "rxvt-unicode",
    "urxvt",
    "st",           # suckless terminal
    "foot",
    # Windows HWND class names (see the Windows port design doc §3). Matched
    # case-insensitively like the X11 values above, so stored lowercased.
    "cascadia_hosting_window_class",  # Windows Terminal
    "consolewindowclass",             # cmd.exe / PowerShell (conhost)
    "mintty",                         # Git Bash / mintty
})


class AppClass(enum.Enum):
    TERMINAL = "terminal"
    NORMAL = "normal"


def classify(
    wm_class: Optional[tuple[str, str]],
    extra_terminals: Optional[list[str]] = None,
) -> AppClass:
    """Return AppClass.TERMINAL if wm_class identifies a terminal emulator.

    Args:
        wm_class: (instance, class) from X11 WM_CLASS property, or None.
                  Values need not be pre-lowercased; classify normalises them.
        extra_terminals: additional WM_CLASS values to treat as terminals
                         (from user settings). Matched case-insensitively.

    Returns:
        AppClass.TERMINAL or AppClass.NORMAL.
    """
    if wm_class is None:
        return AppClass.NORMAL

    known = KNOWN_TERMINALS
    if extra_terminals:
        known = known | frozenset(v.lower() for v in extra_terminals)

    instance_lower = (wm_class[0] or "").lower()
    class_lower = (wm_class[1] or "").lower()

    if instance_lower in known or class_lower in known:
        return AppClass.TERMINAL
    return AppClass.NORMAL
