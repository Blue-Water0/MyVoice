"""Tests for myvoice_win.services.accessibility_backend.

``target_integrity_higher``/``current_process_integrity_level`` walk
``ctypes.windll.user32``/``kernel32``/``advapi32`` (``GetWindowThreadProcessId``,
``OpenProcess``, ``GetCurrentProcess``, ``CloseHandle``, ``OpenProcessToken``,
``GetTokenInformation``, ``GetSidSubAuthorityCount``, ``GetSidSubAuthority``),
none of which exist on this Linux sandbox, so tests inject a fake ``windll``
via ``monkeypatch.setattr(ctypes, "windll", FakeWindll(...), raising=False)``
-- same pattern as test_hotkey_service.py / test_focus_tracker.py /
test_clipboard_backend.py.

``FakeAdvapi32.GetTokenInformation`` writes a real
``TOKEN_MANDATORY_LABEL`` into the real ``ctypes`` buffer the module under
test allocates and passes in (genuine ``ctypes`` memory access, not
mocked -- same "back it with real memory" discipline
``test_clipboard_backend.py``'s ``FakeKernel32`` uses for
``GlobalAlloc``/``GlobalLock``), storing a synthetic non-zero "PSID"
value in it. ``GetSidSubAuthorityCount``/``GetSidSubAuthority`` are then
looked up against that synthetic PSID via a plain dict -- there's no real
SID byte layout to parse since those two calls are themselves mocked.
"""
from __future__ import annotations

import ctypes

import pytest

from myvoice_win.services.accessibility_backend import (
    TOKEN_MANDATORY_LABEL,
    TokenIntegrityLevel,
    current_process_integrity_level,
    is_own_window,
    target_integrity_higher,
)

HWND = 0xABCD
PID = 4321
CURRENT_PROCESS_HANDLE = -1  # mirrors the real GetCurrentProcess() pseudo-handle

SECURITY_MANDATORY_LOW_RID = 0x1000
SECURITY_MANDATORY_MEDIUM_RID = 0x2000
SECURITY_MANDATORY_HIGH_RID = 0x3000
SECURITY_MANDATORY_SYSTEM_RID = 0x4000


# ---- fake ctypes.windll.user32 / kernel32 / advapi32 ------------------------


class FakeUser32:
    def __init__(self, pid_by_hwnd=None):
        self.pid_by_hwnd = pid_by_hwnd or {}

    def GetWindowThreadProcessId(self, hwnd, pid_ref):
        pid = self.pid_by_hwnd.get(hwnd, 0)
        pid_ref._obj.value = pid
        return 111 if pid else 0  # thread id, 0 signals failure


class FakeKernel32:
    """OpenProcess returns the pid itself as the fake "handle" (deterministic
    and known to tests ahead of time -- avoids needing to predict an
    internal counter). GetCurrentProcess returns a fixed sentinel mirroring
    the real pseudo-handle value."""

    def __init__(self, open_process_ok=True, current_process_handle=CURRENT_PROCESS_HANDLE):
        self.open_process_ok = open_process_ok
        self.current_process_handle = current_process_handle
        self.closed_handles = []

    def OpenProcess(self, desired_access, inherit, pid):
        if not self.open_process_ok:
            return 0
        return pid

    def CloseHandle(self, handle):
        self.closed_handles.append(handle)
        return 1

    def GetCurrentProcess(self):
        return self.current_process_handle


class FakeAdvapi32:
    def __init__(
        self,
        open_process_token_ok=True,
        get_token_information_sizing_ok=True,
        get_token_information_data_ok=True,
        sub_authority_count_ok=True,
        sub_authority_ok=True,
    ):
        self.open_process_token_ok = open_process_token_ok
        self.get_token_information_sizing_ok = get_token_information_sizing_ok
        self.get_token_information_data_ok = get_token_information_data_ok
        self.sub_authority_count_ok = sub_authority_count_ok
        self.sub_authority_ok = sub_authority_ok

        self.integrity_by_handle: dict[int, int] = {}  # process handle -> level
        self._token_for_handle: dict[int, int] = {}  # process handle -> token
        self._level_for_token: dict[int, int] = {}  # token -> level
        self._sid_levels: dict[int, int] = {}  # synthetic psid -> level
        self._next_token = 0x5000

    def set_integrity_level(self, process_handle: int, level: int) -> None:
        token = self._next_token
        self._next_token += 1
        self._token_for_handle[process_handle] = token
        self._level_for_token[token] = level

    def OpenProcessToken(self, process_handle, desired_access, token_ref):
        if not self.open_process_token_ok:
            return 0
        token = self._token_for_handle.get(process_handle)
        if token is None:
            return 0
        token_ref._obj.value = token
        return 1

    def GetTokenInformation(self, token_handle, info_class, buf, buf_len, needed_ref):
        assert info_class == TokenIntegrityLevel
        if buf is None or buf_len == 0:
            if not self.get_token_information_sizing_ok:
                needed_ref._obj.value = 0
                return 0
            needed_ref._obj.value = ctypes.sizeof(TOKEN_MANDATORY_LABEL)
            return 0  # real GetTokenInformation returns FALSE on the sizing call too

        if not self.get_token_information_data_ok:
            return 0

        level = self._level_for_token.get(token_handle)
        if level is None:
            return 0

        sid_addr = 0xF00D0000 + token_handle  # synthetic non-zero "PSID"
        self._sid_levels[sid_addr] = level

        label = TOKEN_MANDATORY_LABEL.from_buffer(buf)
        label.Label.Sid = sid_addr
        label.Label.Attributes = 0
        needed_ref._obj.value = ctypes.sizeof(TOKEN_MANDATORY_LABEL)
        return 1

    def GetSidSubAuthorityCount(self, psid):
        if not self.sub_authority_count_ok:
            return None
        if psid not in self._sid_levels:
            return None
        return [1]  # one sub-authority, matching a mandatory-label SID

    def GetSidSubAuthority(self, psid, index):
        if not self.sub_authority_ok:
            return None
        level = self._sid_levels.get(psid)
        if level is None:
            return None
        assert index == 0  # sub_authority_count - 1 == 1 - 1 == 0
        return [level]


class FakeWindll:
    def __init__(self, user32=None, kernel32=None, advapi32=None):
        self.user32 = user32 or FakeUser32()
        self.kernel32 = kernel32 or FakeKernel32()
        self.advapi32 = advapi32 or FakeAdvapi32()


def _install(monkeypatch, user32=None, kernel32=None, advapi32=None) -> FakeWindll:
    fake = FakeWindll(user32, kernel32, advapi32)
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    return fake


# ---- target_integrity_higher: success cases ---------------------------------


def test_target_integrity_higher_returns_false_for_same_integrity(monkeypatch):
    fake = _install(monkeypatch, user32=FakeUser32(pid_by_hwnd={HWND: PID}))
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_MEDIUM_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is False


def test_target_integrity_higher_returns_true_when_target_is_higher(monkeypatch):
    fake = _install(monkeypatch, user32=FakeUser32(pid_by_hwnd={HWND: PID}))
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_HIGH_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


def test_target_integrity_higher_returns_false_when_target_is_lower(monkeypatch):
    # Elevated MyVoice (SYSTEM) targeting a plain medium-integrity window
    # (higher -> lower) must be permitted -- design doc explicitly calls
    # this out as required for test E3.
    fake = _install(monkeypatch, user32=FakeUser32(pid_by_hwnd={HWND: PID}))
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_MEDIUM_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_SYSTEM_RID)

    assert result is False


# ---- target_integrity_higher: fail-closed cases (each query step) ----------
#
# Each of these is its own dedicated test per the brief: "query-failure ->
# True ... this is the one worth a dedicated test since it's a deliberate
# non-obvious choice" -- and specifically every distinct step that can
# fail gets its own test, not just one generic "failure" case, since it
# would be easy to accidentally fail-open on some steps but not others.


def test_target_integrity_higher_fail_closed_when_window_pid_unresolvable(monkeypatch):
    # hwnd not in the fake's map -> GetWindowThreadProcessId "fails" (pid=0).
    _install(monkeypatch, user32=FakeUser32(pid_by_hwnd={}))

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


def test_target_integrity_higher_fail_closed_when_open_process_denied(monkeypatch):
    fake = _install(
        monkeypatch,
        user32=FakeUser32(pid_by_hwnd={HWND: PID}),
        kernel32=FakeKernel32(open_process_ok=False),
    )
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_MEDIUM_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


def test_target_integrity_higher_fail_closed_when_open_process_token_denied(monkeypatch):
    fake = _install(
        monkeypatch,
        user32=FakeUser32(pid_by_hwnd={HWND: PID}),
        advapi32=FakeAdvapi32(open_process_token_ok=False),
    )
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_MEDIUM_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


def test_target_integrity_higher_fail_closed_when_get_token_information_sizing_fails(
    monkeypatch,
):
    fake = _install(
        monkeypatch,
        user32=FakeUser32(pid_by_hwnd={HWND: PID}),
        advapi32=FakeAdvapi32(get_token_information_sizing_ok=False),
    )
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_MEDIUM_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


def test_target_integrity_higher_fail_closed_when_get_token_information_data_fails(
    monkeypatch,
):
    fake = _install(
        monkeypatch,
        user32=FakeUser32(pid_by_hwnd={HWND: PID}),
        advapi32=FakeAdvapi32(get_token_information_data_ok=False),
    )
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_MEDIUM_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


def test_target_integrity_higher_fail_closed_when_sub_authority_count_fails(monkeypatch):
    fake = _install(
        monkeypatch,
        user32=FakeUser32(pid_by_hwnd={HWND: PID}),
        advapi32=FakeAdvapi32(sub_authority_count_ok=False),
    )
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_MEDIUM_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


def test_target_integrity_higher_fail_closed_when_sub_authority_fails(monkeypatch):
    fake = _install(
        monkeypatch,
        user32=FakeUser32(pid_by_hwnd={HWND: PID}),
        advapi32=FakeAdvapi32(sub_authority_ok=False),
    )
    fake.advapi32.set_integrity_level(PID, SECURITY_MANDATORY_MEDIUM_RID)

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


def test_target_integrity_higher_fail_closed_on_unexpected_exception(monkeypatch):
    class _ExplodingUser32(FakeUser32):
        def GetWindowThreadProcessId(self, hwnd, pid_ref):
            raise RuntimeError("boom")

    _install(monkeypatch, user32=_ExplodingUser32(pid_by_hwnd={HWND: PID}))

    result = target_integrity_higher(HWND, own_integrity_level=SECURITY_MANDATORY_MEDIUM_RID)

    assert result is True


# ---- current_process_integrity_level ---------------------------------------


def test_current_process_integrity_level_returns_queried_level(monkeypatch):
    fake = _install(monkeypatch)
    fake.advapi32.set_integrity_level(CURRENT_PROCESS_HANDLE, SECURITY_MANDATORY_MEDIUM_RID)

    assert current_process_integrity_level() == SECURITY_MANDATORY_MEDIUM_RID


def test_current_process_integrity_level_raises_when_open_process_token_fails(monkeypatch):
    _install(monkeypatch, advapi32=FakeAdvapi32(open_process_token_ok=False))

    with pytest.raises(OSError):
        current_process_integrity_level()


def test_current_process_integrity_level_raises_when_get_token_information_fails(monkeypatch):
    fake = _install(monkeypatch, advapi32=FakeAdvapi32(get_token_information_data_ok=False))
    fake.advapi32.set_integrity_level(CURRENT_PROCESS_HANDLE, SECURITY_MANDATORY_MEDIUM_RID)

    with pytest.raises(OSError):
        current_process_integrity_level()


# ---- is_own_window -----------------------------------------------------------


def test_is_own_window_true_when_hwnd_in_set():
    assert is_own_window(HWND, frozenset({HWND, 0x1})) is True


def test_is_own_window_false_when_hwnd_not_in_set():
    assert is_own_window(HWND, frozenset({0x1, 0x2})) is False


def test_is_own_window_false_for_empty_set():
    assert is_own_window(HWND, frozenset()) is False
