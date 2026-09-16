"""Tests for myvoice_win.services.hotkey_service.

``parse_accel`` is pure string-parsing logic and is unit-tested directly
with no mocking. ``register``/``rebind``/``unregister`` call
``ctypes.windll.user32.RegisterHotKey``/``UnregisterHotKey``, which do not
exist on this Linux sandbox — those tests inject a fake ``windll`` object
via ``monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)``
so the module itself never needs `ctypes.windll` to exist at import time.
"""
from __future__ import annotations

import ctypes

import pytest

from myvoice_win.services.hotkey_service import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    HotkeyError,
    HotkeyService,
    parse_accel,
)


# ---- parse_accel (pure logic, no mocking) ---------------------------------


def test_parse_accel_win_shift_space():
    modifiers, vk = parse_accel("win+shift+space")
    assert modifiers == (MOD_WIN | MOD_SHIFT)
    assert vk == 0x20  # VK_SPACE


def test_parse_accel_win_shift_apostrophe():
    modifiers, vk = parse_accel("win+shift+'")
    assert modifiers == (MOD_WIN | MOD_SHIFT)
    assert vk == 0xDE  # VK_OEM_7


def test_parse_accel_win_shift_period():
    modifiers, vk = parse_accel("win+shift+.")
    assert modifiers == (MOD_WIN | MOD_SHIFT)
    assert vk == 0xBE  # VK_OEM_PERIOD


def test_parse_accel_letter_key():
    modifiers, vk = parse_accel("ctrl+alt+d")
    assert modifiers == (MOD_CONTROL | MOD_ALT)
    assert vk == ord("D")


def test_parse_accel_is_case_insensitive():
    modifiers, vk = parse_accel("WIN+Shift+SPACE")
    assert modifiers == (MOD_WIN | MOD_SHIFT)
    assert vk == 0x20


@pytest.mark.parametrize(
    "bad_accel",
    [
        "",
        "   ",
        "win+shift",           # modifier-only, no key
        "foo+space",           # unknown modifier
        "win+shift+doesnotexist",  # unknown key
        "win+shift+a+b",       # two non-modifier keys
    ],
)
def test_parse_accel_raises_value_error(bad_accel):
    with pytest.raises(ValueError):
        parse_accel(bad_accel)


# ---- fake windll.user32 for register/rebind/unregister --------------------


class FakeUser32:
    def __init__(self):
        self.registered: dict[int, tuple[int, int]] = {}
        self.register_calls: list[tuple[object, int, int, int]] = []
        self.unregister_calls: list[tuple[object, int]] = []
        # Set of hotkey ids for which RegisterHotKey should fail (return 0).
        self.fail_ids: set[int] = set()
        # If set, the *next* call to RegisterHotKey fails regardless of id.
        self.fail_next_register = False

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, vk):
        self.register_calls.append((hwnd, hotkey_id, modifiers, vk))
        if self.fail_next_register or hotkey_id in self.fail_ids:
            self.fail_next_register = False
            return 0
        self.registered[hotkey_id] = (modifiers, vk)
        return 1

    def UnregisterHotKey(self, hwnd, hotkey_id):
        self.unregister_calls.append((hwnd, hotkey_id))
        self.registered.pop(hotkey_id, None)
        return 1


class FakeWindll:
    def __init__(self, user32=None):
        self.user32 = user32 or FakeUser32()


@pytest.fixture
def fake_windll(monkeypatch):
    fake = FakeWindll()
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    return fake


# ---- register --------------------------------------------------------------


def test_register_calls_register_hotkey_with_correct_args(fake_windll):
    svc = HotkeyService()
    cb = lambda: None
    svc.register("win+shift+space", cb)

    assert len(fake_windll.user32.register_calls) == 1
    _hwnd, _hotkey_id, modifiers, vk = fake_windll.user32.register_calls[0]
    assert modifiers == (MOD_WIN | MOD_SHIFT)
    assert vk == 0x20


def test_register_raises_hotkey_error_on_failure(fake_windll):
    fake_windll.user32.fail_next_register = True
    svc = HotkeyService()
    with pytest.raises(HotkeyError):
        svc.register("win+shift+space", lambda: None)


def test_register_raises_value_error_before_touching_windll_on_bad_accel(fake_windll):
    svc = HotkeyService()
    with pytest.raises(ValueError):
        svc.register("nonsense", lambda: None)
    assert fake_windll.user32.register_calls == []


# ---- unregister --------------------------------------------------------------


def test_unregister_calls_unregister_hotkey(fake_windll):
    svc = HotkeyService()
    svc.register("win+shift+space", lambda: None)
    svc.unregister()
    assert len(fake_windll.user32.unregister_calls) == 1
    assert fake_windll.user32.registered == {}


def test_unregister_is_idempotent_when_nothing_registered(fake_windll):
    svc = HotkeyService()
    svc.unregister()  # must not raise
    assert fake_windll.user32.unregister_calls == []


# ---- rebind ------------------------------------------------------------------


def test_rebind_unregisters_old_and_registers_new(fake_windll):
    svc = HotkeyService()
    svc.register("win+shift+space", lambda: None)

    svc.rebind("ctrl+alt+d", lambda: None)

    assert len(fake_windll.user32.unregister_calls) == 1
    assert len(fake_windll.user32.register_calls) == 2
    _hwnd, _id, modifiers, vk = fake_windll.user32.register_calls[-1]
    assert modifiers == (MOD_CONTROL | MOD_ALT)
    assert vk == ord("D")


def test_rebind_rolls_back_to_old_binding_on_failure(fake_windll):
    svc = HotkeyService()
    cb1 = lambda: None
    svc.register("win+shift+space", cb1)

    # Make every subsequent RegisterHotKey call fail (the rebind attempt).
    fake_windll.user32.fail_next_register = True

    with pytest.raises(HotkeyError):
        svc.rebind("ctrl+alt+d", lambda: None)

    # Rollback should have re-registered the old accelerator successfully.
    assert len(fake_windll.user32.register_calls) == 3  # original + failed new + rollback
    last_hwnd, _id, modifiers, vk = fake_windll.user32.register_calls[-1]
    assert modifiers == (MOD_WIN | MOD_SHIFT)
    assert vk == 0x20


def test_rebind_reraises_hotkey_error_after_rollback(fake_windll):
    svc = HotkeyService()
    svc.register("win+shift+space", lambda: None)
    fake_windll.user32.fail_next_register = True
    with pytest.raises(HotkeyError):
        svc.rebind("ctrl+alt+d", lambda: None)
