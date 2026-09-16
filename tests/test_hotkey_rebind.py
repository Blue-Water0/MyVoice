"""Rebind semantics: on failure the old binding must be restored."""
from __future__ import annotations

import pytest

from myvoice.services.hotkey_service import HotkeyError, HotkeyService


class FakeHotkeyService(HotkeyService):
    """Overrides only register/unregister to avoid real X11 usage.

    Tracks (accel, cb) as "currently active" and lets tests script whether
    register() succeeds for a given accel via a class attribute.
    """

    def __init__(self, allowed: set[str]) -> None:
        super().__init__()
        self.allowed = allowed
        # Reflect the currently active binding via the base class attrs.

    def register(self, accel, on_toggle):  # type: ignore[override]
        if accel not in self.allowed:
            raise HotkeyError(f"grab conflict on {accel}")
        self._accel_str = accel
        self._callback = on_toggle
        # We don't touch _thread / _display in the fake.

    def unregister(self):  # type: ignore[override]
        self._accel_str = None
        self._callback = None


def _cb(): pass  # noqa: E704


def test_rebind_success_replaces_binding():
    svc = FakeHotkeyService(allowed={"<Super><Shift>space", "<Control><Alt>d"})
    svc.register("<Super><Shift>space", _cb)
    assert svc._accel_str == "<Super><Shift>space"

    svc.rebind("<Control><Alt>d", _cb)
    assert svc._accel_str == "<Control><Alt>d"


def test_rebind_failure_restores_previous():
    svc = FakeHotkeyService(allowed={"<Super><Shift>space"})
    svc.register("<Super><Shift>space", _cb)

    with pytest.raises(HotkeyError):
        svc.rebind("<Control><Alt>d", _cb)   # not in allowed => conflict

    # Old binding must still be active.
    assert svc._accel_str == "<Super><Shift>space"
    assert svc._callback is _cb


def test_rebind_validates_before_touching_x11():
    svc = FakeHotkeyService(allowed={"<Super><Shift>space"})
    svc.register("<Super><Shift>space", _cb)

    # Modifier-only should fail validation, not silently drop the old binding.
    with pytest.raises(ValueError):
        svc.rebind("<Control>", _cb)
    assert svc._accel_str == "<Super><Shift>space"


def test_rebind_from_empty_state_works():
    svc = FakeHotkeyService(allowed={"<Control><Alt>d"})
    # Nothing registered yet.
    assert svc._accel_str is None
    svc.rebind("<Control><Alt>d", _cb)
    assert svc._accel_str == "<Control><Alt>d"


def test_rebind_from_empty_state_failure_leaves_empty():
    svc = FakeHotkeyService(allowed=set())
    with pytest.raises(HotkeyError):
        svc.rebind("<Control><Alt>d", _cb)
    assert svc._accel_str is None
