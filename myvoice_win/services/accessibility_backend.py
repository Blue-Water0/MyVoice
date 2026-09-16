"""Integrity-level guard and "own window" check for Windows text insertion.

Design doc §2 step 2 ("capture the target, reject unsafe targets") lists
three first-class rejection reasons for a paste target: password fields
(handled by ``focus_tracker.TargetRef.is_password``, Task 4), the target
being one of MyVoice's own windows, and the target process running at a
*higher* Windows integrity level than MyVoice itself (Windows UIPI would
silently block ``SendInput`` into it anyway). This module owns the latter
two checks. UIA itself is used narrowly elsewhere (``focus_tracker.py``)
purely for password-field discovery -- this module never touches UIA.

Integrity-Level Guard (UIPI) mechanism -- see design doc §2 "Integrity-
Level Guard (UIPI)" for the ground truth this implements:

    GetWindowThreadProcessId(hwnd) -> pid
    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) -> hProcess
    OpenProcessToken(hProcess, TOKEN_QUERY, ...) -> hToken
    GetTokenInformation(hToken, TokenIntegrityLevel, ...) -> TOKEN_MANDATORY_LABEL
    -> extract the SID's last sub-authority (the RID) as the integrity level
    -> compare against MyVoice's own integrity level

``PROCESS_QUERY_LIMITED_INFORMATION`` (rather than the fuller
``PROCESS_QUERY_INFORMATION`` from the design doc's pseudocode comment) is
used deliberately: it is the documented minimal-privilege access right
that still permits ``OpenProcessToken``, and is specifically the flag
Microsoft recommends for querying processes that may be running at a
different (including higher) integrity level or as a protected process --
``PROCESS_QUERY_INFORMATION`` is more likely to be denied in exactly the
cross-integrity cases this module cares about most.

Fail-closed, non-obvious design choice: ``target_integrity_higher``
returns ``True`` (i.e. "treat as higher / unsafe to paste, buffer
instead") not only when the target is genuinely confirmed higher, but
also whenever *any* step of the above chain fails and the target's real
integrity level cannot be determined at all (PID unresolvable, OpenProcess
denied, OpenProcessToken denied, GetTokenInformation failing, or any
unexpected exception). The alternative -- defaulting to ``False`` ("safe
to paste") on query failure -- would silently risk sending SendInput into
an opaque, possibly higher-privileged process precisely in the cases
where we have the least information to justify it being safe. Buffering
one extra chunk is a much cheaper mistake than an uncontrolled paste.

Import-safety: neither ``ctypes.windll`` (raises ``AttributeError`` if
referenced at import time on non-Windows) is referenced at module level.
Only plain ``ctypes``/``ctypes.wintypes`` are imported at module level --
those exist and are safe to use cross-platform, it is only the
``.windll`` *attribute* that is Windows-only. The ``TOKEN_MANDATORY_LABEL``/
``SID_AND_ATTRIBUTES`` ``ctypes.Structure`` definitions below use only
plain ``ctypes``/``wintypes`` field types (no DLL loading), so defining
them at module level is safe and keeps this module importable on Linux
for pytest collection (mirrors ``clipboard_backend.py``'s ``INPUT``/
``KEYBDINPUT`` structures for the same reason).
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional

from ._win32util import set_signature as _set_signature

log = logging.getLogger(__name__)

# ---- Win32 constants ---------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008

# TOKEN_INFORMATION_CLASS enum value for TokenIntegrityLevel (winnt.h).
TokenIntegrityLevel = 25

# Well-known mandatory-label RID values (winnt.h SECURITY_MANDATORY_*_RID).
# Not directly referenced by this module's logic (which only ever compares
# two levels for strict-greater-than) but documented here since they're
# the values callers (app.py) will actually see out of
# current_process_integrity_level()/target_integrity_higher().
SECURITY_MANDATORY_UNTRUSTED_RID = 0x00000000
SECURITY_MANDATORY_LOW_RID = 0x00001000
SECURITY_MANDATORY_MEDIUM_RID = 0x00002000
SECURITY_MANDATORY_MEDIUM_PLUS_RID = 0x00002100
SECURITY_MANDATORY_HIGH_RID = 0x00003000
SECURITY_MANDATORY_SYSTEM_RID = 0x00004000
SECURITY_MANDATORY_PROTECTED_PROCESS_RID = 0x00005000


# ---- TOKEN_MANDATORY_LABEL structure -----------------------------------------
#
# GetTokenInformation(..., TokenIntegrityLevel, ...) fills a buffer shaped
# like this documented Win32 structure:
#
#   typedef struct _SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; } SID_AND_ATTRIBUTES;
#   typedef struct _TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES Label; } TOKEN_MANDATORY_LABEL;
#
# ``Label.Sid`` points at a SID whose *last* sub-authority (RID) is the
# integrity level (the well-known MSDN "Getting the Integrity Level of a
# Process" sample extracts it exactly this way, via
# GetSidSubAuthorityCount/GetSidSubAuthority rather than hand-parsing the
# SID's raw byte layout).


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = (
        ("Sid", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
    )


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = (("Label", SID_AND_ATTRIBUTES),)


# ---- Win32 helpers ------------------------------------------------------------


def _get_window_pid(hwnd: int) -> Optional[int]:
    """Return the owning process id of ``hwnd``, or ``None`` on failure."""
    import ctypes  # local: ctypes.windll doesn't exist on Linux
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    _set_signature(
        user32.GetWindowThreadProcessId,
        restype=wintypes.DWORD,
        argtypes=[wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
    )

    pid = wintypes.DWORD(0)
    thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not thread_id or not pid.value:
        log.debug("_get_window_pid: GetWindowThreadProcessId failed for hwnd=%s", hwnd)
        return None
    return pid.value


def _get_integrity_level_from_token(token_handle: int) -> Optional[int]:
    """Extract the integrity level (RID) from an open token handle.

    Returns ``None`` on any failure -- never raises -- so callers can
    apply their own (possibly fail-closed) handling uniformly.
    """
    import ctypes  # local: ctypes.windll doesn't exist on Linux
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32
    _set_signature(
        advapi32.GetTokenInformation,
        restype=wintypes.BOOL,
        argtypes=[
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ],
    )
    _set_signature(
        advapi32.GetSidSubAuthorityCount,
        restype=ctypes.POINTER(ctypes.c_ubyte),
        argtypes=[ctypes.c_void_p],
    )
    _set_signature(
        advapi32.GetSidSubAuthority,
        restype=ctypes.POINTER(wintypes.DWORD),
        argtypes=[ctypes.c_void_p, wintypes.DWORD],
    )

    # First call: NULL buffer / 0 length, purely to learn the required
    # buffer size. Per the documented GetTokenInformation contract this
    # call is expected to return FALSE -- only the out-param `needed`
    # matters here, the return value is intentionally not checked.
    needed = wintypes.DWORD(0)
    advapi32.GetTokenInformation(
        token_handle, TokenIntegrityLevel, None, 0, ctypes.byref(needed)
    )
    if not needed.value:
        log.debug("_get_integrity_level_from_token: sizing call reported 0 bytes needed")
        return None

    buf = ctypes.create_string_buffer(needed.value)
    ok = advapi32.GetTokenInformation(
        token_handle, TokenIntegrityLevel, buf, needed.value, ctypes.byref(needed)
    )
    if not ok:
        log.debug("_get_integrity_level_from_token: GetTokenInformation data call failed")
        return None

    label = TOKEN_MANDATORY_LABEL.from_buffer(buf)
    psid = label.Label.Sid
    if not psid:
        log.debug("_get_integrity_level_from_token: TOKEN_MANDATORY_LABEL.Label.Sid is NULL")
        return None

    sub_authority_count_ptr = advapi32.GetSidSubAuthorityCount(psid)
    if not sub_authority_count_ptr:
        log.debug("_get_integrity_level_from_token: GetSidSubAuthorityCount failed")
        return None
    sub_authority_count = sub_authority_count_ptr[0]
    if not sub_authority_count:
        return None

    rid_ptr = advapi32.GetSidSubAuthority(psid, sub_authority_count - 1)
    if not rid_ptr:
        log.debug("_get_integrity_level_from_token: GetSidSubAuthority failed")
        return None
    return rid_ptr[0]


def _integrity_level_for_pid(pid: int) -> Optional[int]:
    """Open ``pid``'s token and return its integrity level, or ``None``
    on any failure (OpenProcess denied, OpenProcessToken denied, or the
    GetTokenInformation extraction failing)."""
    import ctypes  # local: ctypes.windll doesn't exist on Linux
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    _set_signature(
        kernel32.OpenProcess,
        restype=wintypes.HANDLE,
        argtypes=[wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
    )
    _set_signature(
        kernel32.CloseHandle, restype=wintypes.BOOL, argtypes=[wintypes.HANDLE]
    )
    _set_signature(
        advapi32.OpenProcessToken,
        restype=wintypes.BOOL,
        argtypes=[wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
    )

    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_process:
        log.debug("_integrity_level_for_pid: OpenProcess failed for pid=%s", pid)
        return None
    try:
        h_token = wintypes.HANDLE(0)
        if not advapi32.OpenProcessToken(h_process, TOKEN_QUERY, ctypes.byref(h_token)):
            log.debug("_integrity_level_for_pid: OpenProcessToken failed for pid=%s", pid)
            return None
        try:
            return _get_integrity_level_from_token(h_token.value)
        finally:
            kernel32.CloseHandle(h_token.value)
    finally:
        kernel32.CloseHandle(h_process)


# ---- Public API ---------------------------------------------------------------


def target_integrity_higher(hwnd: int, own_integrity_level: int) -> bool:
    """Return ``True`` iff ``hwnd``'s owning process integrity level is
    strictly higher than ``own_integrity_level``.

    Fail-closed (see module docstring): any failure to determine the
    target's integrity level -- an unresolvable pid, ``OpenProcess``
    denied, ``OpenProcessToken`` denied, ``GetTokenInformation`` failing,
    or any unexpected exception along the way -- returns ``True``
    (treat as higher/unknown -- abort the paste and buffer instead)
    rather than ``False`` (safe to paste). This is deliberately
    asymmetric and non-obvious: do not "simplify" it to default to
    ``False`` on failure.
    """
    try:
        pid = _get_window_pid(hwnd)
        if pid is None:
            log.debug(
                "target_integrity_higher: could not resolve pid for hwnd=%s -- "
                "fail-closed (treating as higher)",
                hwnd,
            )
            return True

        level = _integrity_level_for_pid(pid)
        if level is None:
            log.debug(
                "target_integrity_higher: could not query integrity level for "
                "pid=%s -- fail-closed (treating as higher)",
                pid,
            )
            return True

        return level > own_integrity_level
    except Exception:
        log.exception(
            "target_integrity_higher: unexpected error querying hwnd=%s -- "
            "fail-closed (treating as higher)",
            hwnd,
        )
        return True


def current_process_integrity_level() -> int:
    """Query MyVoice's own process integrity level.

    Uses ``GetCurrentProcess()`` (a genuine exported pseudo-handle
    function, valid since Windows XP) followed by ``OpenProcessToken``,
    rather than the newer ``GetCurrentProcessToken()`` name mentioned
    alongside it in the design doc: that name is documented
    (``processthreadsapi.h``, Windows 8+) as a compile-time pseudo-handle
    *macro* (``(HANDLE)(LONG_PTR) -4``), not a real exported DLL
    function -- there is no symbol for ``ctypes.windll`` to bind to by
    name. ``GetCurrentProcess`` + ``OpenProcessToken`` reach the exact
    same token via the same real, exported functions
    ``target_integrity_higher``/``_integrity_level_for_pid`` already use
    for an external process, and work on every supported Windows version.

    Unlike ``target_integrity_higher``, there is no safe default to fall
    back on here: this runs once at startup (``app.py``) and is cached by
    the caller, so any failure raises ``OSError`` rather than silently
    guessing a value that could make the fail-closed guard above
    meaningless.
    """
    import ctypes  # local: ctypes.windll doesn't exist on Linux
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    _set_signature(kernel32.GetCurrentProcess, restype=wintypes.HANDLE, argtypes=[])
    _set_signature(
        kernel32.CloseHandle, restype=wintypes.BOOL, argtypes=[wintypes.HANDLE]
    )
    _set_signature(
        advapi32.OpenProcessToken,
        restype=wintypes.BOOL,
        argtypes=[wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
    )

    h_process = kernel32.GetCurrentProcess()
    h_token = wintypes.HANDLE(0)
    if not advapi32.OpenProcessToken(h_process, TOKEN_QUERY, ctypes.byref(h_token)):
        raise OSError("OpenProcessToken failed for the current process")
    try:
        level = _get_integrity_level_from_token(h_token.value)
    finally:
        kernel32.CloseHandle(h_token.value)

    if level is None:
        raise OSError("Failed to query the current process's integrity level")
    return level


def is_own_window(hwnd: int, own_hwnds: frozenset[int]) -> bool:
    """Return ``True`` iff ``hwnd`` is one of MyVoice's own windows.

    Trivial membership check, pulled out to its own testable function
    (rather than inlined at the call site) since design doc §2 step 2
    treats "own MyVoice window focused" as a first-class paste-rejection
    reason alongside password fields and the integrity-level guard.
    """
    return hwnd in own_hwnds
