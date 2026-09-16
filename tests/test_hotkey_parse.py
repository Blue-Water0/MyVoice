"""Accelerator parser tests (no X11 required)."""
import pytest

from myvoice.services.hotkey_service import format_accel, parse_accel


def test_super_shift_space():
    p = parse_accel("<Super><Shift>space")
    assert p.mods == frozenset({"super", "shift"})
    assert p.key == "space"


def test_ctrl_alt_d():
    p = parse_accel("<Control><Alt>d")
    assert p.mods == frozenset({"control", "alt"})


def test_ctrl_alias():
    p = parse_accel("<Ctrl>a")
    assert "control" in p.mods


def test_mod4_alias():
    p = parse_accel("<Mod4><Shift>space")
    assert "super" in p.mods


def test_no_mods():
    p = parse_accel("F5")
    assert p.mods == frozenset()
    assert p.key == "F5"


def test_empty_raises():
    with pytest.raises(ValueError):
        parse_accel("")


def test_unknown_mod_raises():
    with pytest.raises(ValueError):
        parse_accel("<Weird>a")


def test_no_key_raises():
    with pytest.raises(ValueError):
        parse_accel("<Control>")


def test_multiple_keys_raises():
    with pytest.raises(ValueError):
        parse_accel("<Control>a b")


def test_format_roundtrip():
    original = "<Control><Shift>a"
    p = parse_accel(original)
    s = format_accel(p.mods, p.key)
    p2 = parse_accel(s)
    assert p2.mods == p.mods
    assert p2.key == p.key
