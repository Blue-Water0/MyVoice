"""Tests for myvoice_win.services.focus_tracker.

``capture_focus`` calls three ``ctypes.windll.user32`` functions
(``GetForegroundWindow``, ``GetWindowThreadProcessId``, ``GetClassNameW``)
plus a lazily-imported ``comtypes.client`` UIA discovery step. Neither
``ctypes.windll`` nor ``comtypes`` exist/import on this Linux sandbox, so:

- the Win32 calls are driven via ``monkeypatch.setattr(ctypes, "windll",
  FakeWindll(), raising=False)`` (same pattern as test_hotkey_service.py).
- the UIA discovery path is driven by injecting fake ``comtypes`` /
  ``comtypes.client`` modules directly into ``sys.modules`` *before*
  ``capture_focus`` runs its lazy ``import comtypes.client`` — there is no
  real ``comtypes`` module on Linux to monkeypatch an attribute onto.
"""
from __future__ import annotations

import ctypes
import sys
import types

import pytest

from myvoice_win.services.focus_tracker import TargetRef, capture_focus

FOREGROUND_PID = 1234
HWND = 0xABCD
HWND_CLASS = "Chrome_WidgetWin_1"


# ---- fake ctypes.windll.user32 ---------------------------------------------


class FakeUser32:
    def __init__(self, hwnd=HWND, pid=FOREGROUND_PID, hwnd_class=HWND_CLASS):
        self.hwnd = hwnd
        self.pid = pid
        self.hwnd_class = hwnd_class
        self.get_class_name_calls = []

    def GetForegroundWindow(self):
        return self.hwnd

    def GetWindowThreadProcessId(self, hwnd, pid_ref):
        # pid_ref is a ctypes.byref(wintypes.DWORD) — write through it like
        # the real Win32 API does.
        pid_ref._obj.value = self.pid
        return 111  # thread id, unused by capture_focus

    def GetClassNameW(self, hwnd, buf, size):
        self.get_class_name_calls.append((hwnd, size))
        name = self.hwnd_class or ""
        buf.value = name
        return len(name)


class FakeWindll:
    def __init__(self, user32=None):
        self.user32 = user32 or FakeUser32()


@pytest.fixture
def fake_windll(monkeypatch):
    fake = FakeWindll()
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    return fake


# ---- fake comtypes.client / UIA --------------------------------------------


class FakeUIAElement:
    def __init__(self, is_password: bool, process_id):
        self.CurrentIsPassword = is_password
        self.CurrentProcessId = process_id

    def GetFocusedElement(self):
        return self


class _RaisingUIA:
    def GetFocusedElement(self):
        raise RuntimeError("UIA provider unavailable for this control")


def _install_fake_comtypes(monkeypatch, uia_object):
    """Inject fake comtypes/comtypes.client modules into sys.modules so
    ``capture_focus``'s lazy ``import comtypes.client`` picks these up
    instead of trying (and failing) to import the real package.
    """
    fake_comtypes = types.ModuleType("comtypes")
    fake_comtypes_client = types.ModuleType("comtypes.client")

    def CreateObject(prog_id, interface=None):
        return uia_object

    fake_comtypes_client.CreateObject = CreateObject
    fake_comtypes.client = fake_comtypes_client

    monkeypatch.setitem(sys.modules, "comtypes", fake_comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", fake_comtypes_client)


def _install_fake_comtypes_raising_create_object(monkeypatch):
    fake_comtypes = types.ModuleType("comtypes")
    fake_comtypes_client = types.ModuleType("comtypes.client")

    def CreateObject(prog_id, interface=None):
        raise OSError("CoCreateInstance failed")

    fake_comtypes_client.CreateObject = CreateObject
    fake_comtypes.client = fake_comtypes_client

    monkeypatch.setitem(sys.modules, "comtypes", fake_comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", fake_comtypes_client)


# ---- TargetRef dataclass shape ----------------------------------------------


def test_target_ref_fields_default_to_none_and_false():
    ref = TargetRef(
        hwnd=None, foreground_pid=None, uia_pid=None,
        is_password=False, hwnd_class=None,
    )
    assert ref.hwnd is None
    assert ref.foreground_pid is None
    assert ref.uia_pid is None
    assert ref.is_password is False
    assert ref.hwnd_class is None


# ---- capture_focus: win32 basics --------------------------------------------


def test_capture_focus_reads_hwnd_pid_and_class(fake_windll, monkeypatch):
    _install_fake_comtypes(monkeypatch, FakeUIAElement(False, FOREGROUND_PID))

    ref = capture_focus()

    assert ref.hwnd == HWND
    assert ref.foreground_pid == FOREGROUND_PID
    assert ref.hwnd_class == HWND_CLASS


# ---- capture_focus: UIA matching PID case -----------------------------------


def test_capture_focus_reports_password_when_uia_pid_matches(fake_windll, monkeypatch):
    _install_fake_comtypes(monkeypatch, FakeUIAElement(True, FOREGROUND_PID))

    ref = capture_focus()

    assert ref.uia_pid == FOREGROUND_PID
    assert ref.is_password is True


def test_capture_focus_reports_non_password_when_uia_pid_matches(fake_windll, monkeypatch):
    _install_fake_comtypes(monkeypatch, FakeUIAElement(False, FOREGROUND_PID))

    ref = capture_focus()

    assert ref.uia_pid == FOREGROUND_PID
    assert ref.is_password is False


# ---- capture_focus: UIA/foreground PID mismatch rule ------------------------


def test_capture_focus_forces_is_password_false_on_pid_mismatch(fake_windll, monkeypatch):
    other_pid = FOREGROUND_PID + 1
    _install_fake_comtypes(monkeypatch, FakeUIAElement(True, other_pid))

    ref = capture_focus()

    # UIA said is_password=True, but its PID (other_pid) does not match the
    # foreground window's PID -> must be forced to False per the design
    # doc's mismatch rule, though uia_pid still reflects what UIA reported.
    assert ref.uia_pid == other_pid
    assert ref.foreground_pid == FOREGROUND_PID
    assert ref.is_password is False


def test_capture_focus_forces_is_password_false_when_foreground_pid_unknown(monkeypatch):
    # GetForegroundWindow can legitimately return 0 (no window currently has
    # focus). With no foreground_pid to confirm a match against, is_password
    # must default to False -- whitelist semantics, not "pass through
    # whatever UIA reported" -- even though UIA still reports a focused
    # element claiming CurrentIsPassword=True.
    fake = FakeWindll(FakeUser32(hwnd=0))
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    _install_fake_comtypes(monkeypatch, FakeUIAElement(True, FOREGROUND_PID))

    ref = capture_focus()

    assert ref.hwnd is None
    assert ref.foreground_pid is None
    assert ref.uia_pid == FOREGROUND_PID
    assert ref.is_password is False


# ---- capture_focus: UIA raises / unavailable --------------------------------


def test_capture_focus_degrades_gracefully_when_uia_get_focused_element_raises(
    fake_windll, monkeypatch
):
    _install_fake_comtypes(monkeypatch, _RaisingUIA())

    ref = capture_focus()  # must not raise

    assert ref.uia_pid is None
    assert ref.is_password is False
    # Win32-sourced fields are unaffected by the UIA failure.
    assert ref.hwnd == HWND
    assert ref.foreground_pid == FOREGROUND_PID
    assert ref.hwnd_class == HWND_CLASS


def test_capture_focus_degrades_gracefully_when_create_object_raises(
    fake_windll, monkeypatch
):
    _install_fake_comtypes_raising_create_object(monkeypatch)

    ref = capture_focus()  # must not raise

    assert ref.uia_pid is None
    assert ref.is_password is False


def test_capture_focus_degrades_gracefully_when_get_focused_element_returns_none(
    fake_windll, monkeypatch
):
    class _NoneReturningUIA:
        def GetFocusedElement(self):
            return None

    _install_fake_comtypes(monkeypatch, _NoneReturningUIA())

    ref = capture_focus()  # must not raise

    assert ref.uia_pid is None
    assert ref.is_password is False
