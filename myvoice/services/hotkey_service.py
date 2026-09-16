"""Global hotkey service for X11.

Uses python-xlib XGrabKey on the root window with all lock-mask variants so
NumLock/CapsLock/ScrollLock don't disable the shortcut. Listens on a
background thread; posts the toggle callback via GLib.idle_add when GTK is
present, else directly.

Accelerator format: GTK-style, e.g. "<Super><Shift>space", "<Control><Alt>d".
Modifier tokens (case-insensitive): Control/Ctrl, Shift, Alt/Mod1, Super/Mod4.
Key: any single X keysym name (space, a, F5, ...).

Wayland: this backend will refuse to start; a future service module can add
portal/compositor support.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


# ---- accelerator parsing ---------------------------------------------------


_MODS = {
    "control": "control",
    "ctrl":    "control",
    "shift":   "shift",
    "alt":     "alt",
    "mod1":    "alt",
    "super":   "super",
    "mod4":    "super",
}


@dataclass(frozen=True)
class ParsedAccel:
    mods: frozenset[str]  # subset of {'control','shift','alt','super'}
    key: str              # keysym name, lower or as-typed


def _is_gtk_form(s: str) -> bool:
    return "<" in s and ">" in s


def parse_accel(accel: str) -> ParsedAccel:
    """Parse an accelerator into a canonical ParsedAccel.

    Accepts both the GTK-style form ``<Super><Shift>space`` (used in the
    settings file for backward compatibility) *and* the human-readable form
    ``Super+Shift+Space`` (produced by the new Record Shortcut UI).
    Raises ValueError on failure.
    """
    if not accel or not accel.strip():
        raise ValueError("empty accelerator")

    if _is_gtk_form(accel):
        return _parse_gtk_form(accel.strip())
    return _parse_label_form(accel.strip())


def _parse_gtk_form(accel: str) -> ParsedAccel:
    mods: set[str] = set()
    key: Optional[str] = None
    tokens = re.findall(r"<([^>]+)>|([^<>\s]+)", accel)
    for m, k in tokens:
        if m:
            name = _MODS.get(m.lower())
            if name is None:
                raise ValueError(f"unknown modifier: {m}")
            mods.add(name)
        elif k:
            if key is not None:
                raise ValueError("multiple key names in accelerator")
            key = k
    if key is None:
        raise ValueError("no key in accelerator")
    return ParsedAccel(mods=frozenset(mods), key=key)


def _parse_label_form(label: str) -> ParsedAccel:
    """Parse ``Super+Shift+Space`` style human labels."""
    parts = [p.strip() for p in label.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty accelerator")
    mods: set[str] = set()
    key: Optional[str] = None
    for p in parts:
        norm = _MODS.get(p.lower())
        if norm is not None:
            mods.add(norm)
        else:
            if key is not None:
                raise ValueError(
                    f"multiple non-modifier keys in accelerator: {label!r}"
                )
            key = p
    if key is None:
        raise ValueError(f"no non-modifier key in accelerator: {label!r}")
    return ParsedAccel(mods=frozenset(mods), key=key)


def format_accel(mods: frozenset[str], key: str) -> str:
    """Return GTK-style accelerator (backward-compatible with the settings file)."""
    order = ["control", "shift", "alt", "super"]
    label = {"control": "Control", "shift": "Shift", "alt": "Alt", "super": "Super"}
    parts = [f"<{label[m]}>" for m in order if m in mods]
    return "".join(parts) + key


# ---- canonical, human-readable form (used in UI + settings.json) ----------


# Nice display names for common non-alphanumeric keys.
_KEY_DISPLAY = {
    "space": "Space",
    "return": "Return",
    "enter": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    "minus": "Minus",
    "plus": "Plus",
    "equal": "Equal",
    "comma": "Comma",
    "period": "Period",
    "slash": "Slash",
    "semicolon": "Semicolon",
}

# Order of modifiers in the canonical label. Follows the user-facing example
# "Super+Shift+Space" / "Ctrl+Alt+D": Ctrl, Alt, Super, Shift, plain key.
# We keep Ctrl before Alt (matches the "Ctrl+Alt+D" example) and Super before
# Shift (matches "Super+Shift+Space"). "Ctrl+Shift" would produce "Ctrl+Shift+X"
# since Super is absent.
_MOD_LABEL_ORDER = ("control", "alt", "super", "shift")
_MOD_LABEL = {
    "control": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super",
}


def display_key(key: str) -> str:
    """Return the display-cased form of a key name.

    Single letters become uppercase (``a`` -> ``A``). Function keys keep their
    ``F5`` casing. Named keys use the map above; anything else is title-cased.
    """
    low = key.lower()
    if low in _KEY_DISPLAY:
        return _KEY_DISPLAY[low]
    if re.fullmatch(r"f\d{1,2}", low):
        return low.upper()
    if len(key) == 1:
        return key.upper()
    return key[:1].upper() + key[1:]


def canonical_label(parsed: ParsedAccel) -> str:
    """Return the canonical human label, e.g. ``Super+Shift+Space``.

    Modifier order is fixed: Ctrl, Alt, Shift, Super. Only recognised keys/mods
    are emitted. Callers that constructed ``parsed`` from user input must have
    validated non-modifier keys already (see ``ensure_valid``).
    """
    mods_out = [_MOD_LABEL[m] for m in _MOD_LABEL_ORDER if m in parsed.mods]
    return "+".join(mods_out + [display_key(parsed.key)])


def canonical_label_from_string(accel: str) -> str:
    """Canonicalise any accepted accel string into the display form."""
    return canonical_label(parse_accel(accel))


def ensure_valid(parsed: ParsedAccel) -> None:
    """Validate an accelerator captured from the UI.

    Raises ValueError if it is modifier-only or missing a key.
    """
    if not parsed.key:
        raise ValueError("accelerator has no non-modifier key")
    if parsed.key.lower() in _MODS:
        raise ValueError(
            "accelerator must include at least one non-modifier key "
            "(pressing only Ctrl/Alt/Shift/Super is not valid)"
        )


DEFAULT_HOTKEY = "<Super><Shift>space"


def default_hotkey_label() -> str:
    return canonical_label_from_string(DEFAULT_HOTKEY)


# ---- backend ---------------------------------------------------------------


def is_wayland_session() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


# ---- GTK/Gdk key event -> ParsedAccel -------------------------------------


# Modifiers we look for in a Gdk.ModifierType mask. Lock modifiers are
# deliberately excluded so CapsLock / NumLock never appear in the label.
_GDK_MOD_BITS = (
    # (mask_bit_index, mod_name)  — resolved at call time against Gdk enums.
    ("control_mask", "control"),
    ("mod1_mask", "alt"),         # Alt
    ("shift_mask", "shift"),
    ("super_mask", "super"),      # Super/Windows key on modern Gdk
    ("mod4_mask", "super"),       # Older Gdk uses Mod4 for Super
    ("meta_mask", "super"),       # Some layouts map Meta to Super
)

# Gdk keyvals that represent modifier keys themselves; we must reject these
# so a modifier-only press is not accepted as a valid shortcut.
_MODIFIER_KEYVALS = {
    # These names are the same as XK_ names, reachable via Gdk.keyval_name().
    "Shift_L", "Shift_R",
    "Control_L", "Control_R",
    "Alt_L", "Alt_R",
    "Super_L", "Super_R",
    "Meta_L", "Meta_R",
    "Hyper_L", "Hyper_R",
    "ISO_Level3_Shift",  # AltGr
    "Caps_Lock", "Num_Lock", "Scroll_Lock", "Shift_Lock",
}


def is_modifier_keyval_name(keyval_name: Optional[str]) -> bool:
    return bool(keyval_name) and keyval_name in _MODIFIER_KEYVALS


def parsed_from_gdk(keyval_name: Optional[str], gdk_state) -> Optional[ParsedAccel]:
    """Convert a Gdk key-press (keyval name + modifier state) to ParsedAccel.

    Returns None if the press is a modifier-only key (Ctrl_L, Shift_L, …).
    Rejects lock modifiers (CapsLock, NumLock) from the resulting mod set.
    Raises ValueError if the keyval is unusable.
    """
    if keyval_name is None:
        raise ValueError("no key name for Gdk event")
    if is_modifier_keyval_name(keyval_name):
        return None

    mods: set[str] = set()
    try:
        import gi
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk  # type: ignore
        mod_type = Gdk.ModifierType
        for attr, name in _GDK_MOD_BITS:
            bit = getattr(mod_type, attr.upper(), None)
            if bit is None:
                continue
            if gdk_state & bit:
                mods.add(name)
    except Exception:
        # If Gdk is unavailable (unit tests without display), treat gdk_state as
        # an integer bitfield keyed to a fixed mapping used only by tests.
        # Test bits: 1=shift 2=ctrl 4=alt 8=super
        if gdk_state & 1: mods.add("shift")
        if gdk_state & 2: mods.add("control")
        if gdk_state & 4: mods.add("alt")
        if gdk_state & 8: mods.add("super")

    # Normalise the keyval name into what XK.string_to_keysym understands
    # later (parse_accel + register both use _keysym_from_name).
    key = _normalize_gdk_keyval_name(keyval_name)
    return ParsedAccel(mods=frozenset(mods), key=key)


def _normalize_gdk_keyval_name(name: str) -> str:
    """Reduce Gdk key names to canonical low/keysym form.

    Examples:
        "space"   -> "space"
        "Return"  -> "Return"
        "A"       -> "a"       (single letters lowered so display_key upperCases)
        "F5"      -> "F5"
        "period"  -> "period"
    """
    if re.fullmatch(r"[A-Z]", name):
        return name.lower()
    return name


class HotkeyError(RuntimeError):
    pass


class HotkeyService:
    """X11 XGrabKey global hotkey. Toggle on each press."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._callback: Optional[Callable[[], None]] = None
        self._accel_str: Optional[str] = None
        self._display = None
        self._grabbed: list[tuple[int, int]] = []
        self._backend_available: Optional[bool] = None

    # --- utilities ------------------------------------------------------

    def _check_backend(self) -> None:
        if is_wayland_session():
            raise HotkeyError(
                "Global hotkeys are not supported on Wayland in this build. "
                "Log out and choose a Cinnamon (X11) session, or use the "
                "Start button in the app."
            )
        try:
            from Xlib import display as _display  # noqa: F401 # type: ignore
            from Xlib import X  # noqa: F401 # type: ignore
        except Exception as e:
            raise HotkeyError(
                "python-xlib is not installed; install it in the venv."
            ) from e

    # --- public API -----------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def register(self, accel: str, on_toggle: Callable[[], None]) -> None:
        """Grab the accelerator; raises HotkeyError on failure."""
        self._check_backend()
        parsed = parse_accel(accel)

        # Import here so unit tests without X11 don't fail on module import
        from Xlib import X, display  # type: ignore
        from Xlib.error import BadAccess  # type: ignore

        d = display.Display()
        root = d.screen().root

        keysym = _keysym_from_name(parsed.key)
        if keysym == 0:
            d.close()
            raise HotkeyError(f"Unknown key name: {parsed.key!r}")
        keycode = d.keysym_to_keycode(keysym)
        if keycode == 0:
            d.close()
            raise HotkeyError(f"Key {parsed.key!r} not mappable on this keyboard")

        base_mod = _x_modifier_mask(parsed.mods)

        # Combinations of ignored lock modifiers so the grab works with
        # NumLock/CapsLock etc. active.
        lock_masks = [0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask]

        # Install error handler to catch grab conflicts
        errors: list[Exception] = []

        def _err(err, req):  # noqa: ANN001
            errors.append(err)

        d.set_error_handler(_err)

        grabbed: list[tuple[int, int]] = []
        for lm in lock_masks:
            try:
                root.grab_key(
                    keycode,
                    base_mod | lm,
                    True,
                    X.GrabModeAsync,
                    X.GrabModeAsync,
                )
                grabbed.append((keycode, base_mod | lm))
            except BadAccess:
                errors.append(BadAccess("grab conflict"))
                break
        d.sync()
        d.set_error_handler(None)

        if errors:
            # Undo any partial grabs
            for kc, mm in grabbed:
                try:
                    root.ungrab_key(kc, mm)
                except Exception:
                    pass
            d.close()
            raise HotkeyError(
                f"Failed to grab hotkey {accel!r}. It may be reserved by "
                f"Cinnamon or another app. Choose a different combination."
            )

        self._display = d
        self._grabbed = grabbed
        self._callback = on_toggle
        self._accel_str = accel
        self._stop.clear()

        self._thread = threading.Thread(
            target=self._run, name="myvoice-hotkey", daemon=True
        )
        self._thread.start()
        log.info("Hotkey registered: %s (keycode=%d)", accel, keycode)

    def unregister(self) -> None:
        self._stop.set()
        d = self._display
        try:
            if d is not None and self._grabbed:
                root = d.screen().root
                for kc, mm in self._grabbed:
                    try:
                        root.ungrab_key(kc, mm)
                    except Exception:
                        pass
                d.sync()
        except Exception:
            log.exception("Error ungrabbing hotkey")
        try:
            if d is not None:
                d.close()
        except Exception:
            pass
        self._display = None
        self._grabbed = []
        self._callback = None
        self._accel_str = None
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None
        log.info("Hotkey unregistered")

    def rebind(self, new_accel: str, on_toggle: Callable[[], None]) -> None:
        """Atomically replace the current hotkey with ``new_accel``.

        If the new grab fails, the old hotkey is re-registered so the app
        still has a working shortcut. Raises HotkeyError on any failure —
        callers can display the message and know the previous binding is
        still active.
        """
        # Validate the new accel *before* touching X so we don't leave a
        # window where no hotkey is active due to bad user input.
        parsed = parse_accel(new_accel)
        ensure_valid(parsed)

        prev_accel = self._accel_str
        prev_cb = self._callback

        # Fast path: if the accelerator is identical and callback matches,
        # keep the current grab in place.
        if prev_accel == new_accel and prev_cb is on_toggle:
            return

        # Unregister current binding (if any).
        self.unregister()

        try:
            self.register(new_accel, on_toggle)
        except HotkeyError as e:
            # Roll back to the previous binding, if we had one.
            if prev_accel is not None and prev_cb is not None:
                try:
                    self.register(prev_accel, prev_cb)
                    log.warning(
                        "New hotkey %r rejected (%s); restored previous %r",
                        new_accel, e, prev_accel,
                    )
                except HotkeyError:
                    log.exception(
                        "Failed to restore previous hotkey %r after rebind failure",
                        prev_accel,
                    )
            raise

    # --- thread ---------------------------------------------------------

    def _run(self) -> None:
        d = self._display
        if d is None:
            return
        from Xlib import X  # type: ignore
        try:
            while not self._stop.is_set():
                # pending_events lets us poll without blocking forever
                n = d.pending_events()
                if n == 0:
                    # sleep briefly, then check stop flag
                    self._stop.wait(0.05)
                    continue
                for _ in range(n):
                    evt = d.next_event()
                    if evt.type == X.KeyPress:
                        cb = self._callback
                        if cb is not None:
                            self._dispatch(cb)
        except Exception as e:
            log.exception("Hotkey thread crashed: %s", e)

    def _dispatch(self, cb: Callable[[], None]) -> None:
        try:
            from gi.repository import GLib  # type: ignore
            GLib.idle_add(_safe_call, cb)
        except Exception:
            try:
                cb()
            except Exception:
                log.exception("Hotkey callback raised")


def _safe_call(cb: Callable[[], None]) -> bool:
    try:
        cb()
    except Exception:
        log.exception("Hotkey callback raised (main-thread)")
    return False  # remove idle handler


# ---- X modifier + keysym helpers ------------------------------------------


def _x_modifier_mask(mods: frozenset[str]) -> int:
    from Xlib import X  # type: ignore
    mask = 0
    if "control" in mods:
        mask |= X.ControlMask
    if "shift" in mods:
        mask |= X.ShiftMask
    if "alt" in mods:
        mask |= X.Mod1Mask
    if "super" in mods:
        mask |= X.Mod4Mask
    return mask


def _keysym_from_name(name: str) -> int:
    """Resolve a key name to an X keysym integer."""
    from Xlib import XK  # type: ignore
    # Try the exact name, then Capitalized, then upper for single letters
    candidates = [name, name.capitalize(), name.upper(), name.lower()]
    for c in candidates:
        ks = XK.string_to_keysym(c)
        if ks:
            return ks
    # Special common aliases
    aliases = {"space": "space", "return": "Return", "enter": "Return", "tab": "Tab", "esc": "Escape"}
    if name.lower() in aliases:
        return XK.string_to_keysym(aliases[name.lower()])
    return 0
