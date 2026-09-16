"""Shared ctypes.windll signature-declaration helper.

Import-safety: this module only touches plain ``ctypes`` type objects
(``ctypes.Structure``, argument/return-type constants) -- never
``ctypes.windll`` itself -- so it stays importable on non-Windows platforms
for pytest collection, same as every other module under ``myvoice_win``.
"""
from __future__ import annotations


def set_signature(fn, restype=None, argtypes=None) -> None:
    """Best-effort ``fn.restype``/``fn.argtypes`` assignment.

    Real ctypes function-pointer objects (what ``ctypes.windll.*.SomeFunc``
    actually returns) support arbitrary attribute assignment for this,
    which matters on real 64-bit Windows in both directions:

    - ``restype``: several Win32 calls across this codebase return
      pointer/handle values (e.g. ``GlobalAlloc``, ``GetForegroundWindow``,
      ``OpenProcess``) and, without an explicit ``restype``, ctypes
      defaults to treating the return value as a 32-bit C ``int`` --
      silently truncating a genuine 64-bit pointer/handle on 64-bit
      Windows.
    - ``argtypes``: symmetrically, a bare Python ``int`` argument with no
      declared ``argtypes`` gets marshalled as the platform's default C
      ``int`` (32-bit, even on 64-bit Windows' LLP64 model) -- silently
      truncating an HWND/HANDLE/pointer value being passed back *into*
      another Win32 call. Declaring ``argtypes`` (e.g. ``wintypes.HWND``/
      ``ctypes.c_void_p`` for handle/pointer parameters) makes ctypes
      marshal the full-width value correctly instead.

    The plain bound-method test doubles this project's tests use to fake
    ``ctypes.windll`` do NOT support attribute assignment (Python bound
    methods have no writable ``__dict__``) and raise ``AttributeError`` --
    harmless to swallow, since the fakes' calls already pass/return
    correctly-sized Python ints/None with no truncation risk to protect
    against.
    """
    try:
        if restype is not None:
            fn.restype = restype
        if argtypes is not None:
            fn.argtypes = argtypes
    except AttributeError:
        pass
