"""Unit tests for app_classifier — pure, no mocks needed."""
from myvoice.services.app_classifier import AppClass, classify, KNOWN_TERMINALS


def test_known_terminal_instance_name():
    assert classify(("gnome-terminal-server", "Gnome-terminal")) == AppClass.TERMINAL


def test_known_terminal_class_name():
    assert classify(("xterm", "XTerm")) == AppClass.TERMINAL


def test_kitty_is_terminal():
    assert classify(("kitty", "kitty")) == AppClass.TERMINAL


def test_alacritty_is_terminal():
    assert classify(("Alacritty", "Alacritty")) == AppClass.TERMINAL


def test_konsole_is_terminal():
    assert classify(("konsole", "konsole")) == AppClass.TERMINAL


def test_tilix_is_terminal():
    assert classify(("tilix", "Tilix")) == AppClass.TERMINAL


def test_wezterm_is_terminal():
    assert classify(("org.wezfurlong.wezterm", "org.wezfurlong.wezterm")) == AppClass.TERMINAL


def test_sublime_text_is_normal():
    assert classify(("sublime_text", "Sublime_text")) == AppClass.NORMAL


def test_xed_is_normal():
    assert classify(("xed", "Xed")) == AppClass.NORMAL


def test_gedit_is_normal():
    assert classify(("gedit", "Gedit")) == AppClass.NORMAL


def test_firefox_is_normal():
    assert classify(("Navigator", "Firefox")) == AppClass.NORMAL


def test_unknown_class_defaults_to_normal():
    assert classify(("unknown-app", "Unknown-App")) == AppClass.NORMAL


def test_none_wm_class_defaults_to_normal():
    assert classify(None) == AppClass.NORMAL


def test_case_insensitive_matching():
    assert classify(("XTERM", "XTERM")) == AppClass.TERMINAL
    assert classify(("Kitty", "Kitty")) == AppClass.TERMINAL


def test_extra_terminal_override():
    assert classify(
        ("myspecialterminal", "MySpecialTerminal"),
        extra_terminals=["myspecialterminal"],
    ) == AppClass.TERMINAL


def test_extra_terminal_override_case_insensitive():
    assert classify(
        ("MySpecialTerminal", "MySpecialTerminal"),
        extra_terminals=["myspecialterminal"],
    ) == AppClass.TERMINAL


def test_known_terminals_frozenset_not_empty():
    assert len(KNOWN_TERMINALS) > 5


# --- focus_tracker integration ---

from unittest.mock import patch


def test_focus_tracker_wm_class_field_exists():
    """TargetRef must have a wm_class attribute."""
    from myvoice.services.focus_tracker import TargetRef
    ref = TargetRef()
    assert hasattr(ref, "wm_class")
    assert ref.wm_class is None


def test_focus_tracker_capture_populates_wm_class():
    """capture_focus() must populate wm_class from the active XID."""
    from myvoice.services.focus_tracker import capture_focus
    with (
        patch("myvoice.services.focus_tracker._xdotool_active_window", return_value=12345),
        patch("myvoice.services.focus_tracker._find_focused_accessible", return_value=None),
        patch("myvoice.services.focus_tracker._xlib_wm_class", return_value=("xterm", "xterm")),
    ):
        ref = capture_focus()
    assert ref.wm_class == ("xterm", "xterm")
    assert ref.xid == 12345
