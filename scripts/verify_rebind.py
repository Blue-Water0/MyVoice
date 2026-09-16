"""Verify that changing the hotkey via the settings-dialog code path
actually unregisters the old grab and installs the new one, and that
a conflict rolls back correctly.

We can't press the physical key from a script (X11 passive grabs ignore
XTest synthesized events), but we CAN verify:
  1. HotkeyService accels_str changes when rebind succeeds.
  2. HotkeyService accels_str preserved when rebind fails.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
from gi.repository import GLib  # type: ignore  # noqa: E402

from myvoice.services.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    setup_logging("INFO")
    from myvoice.app import MyVoiceApp
    app = MyVoiceApp()
    results: list[str] = []

    def bootstrap():
        try:
            # Success case: switch to Ctrl+Alt+D.
            err = app._try_rebind_hotkey("<Control><Alt>d")
            results.append(f"rebind_ctrl_alt_d_err={err!r}")
            results.append(f"active_accel_after_success={app._hotkey._accel_str!r}")

            # Failure case: try to grab an accel that requires the same
            # combination twice — we simulate a conflict by grabbing it in a
            # second Display object first.
            try:
                from Xlib import X, display  # type: ignore
                d = display.Display()
                root = d.screen().root
                from Xlib import XK  # type: ignore
                keysym = XK.string_to_keysym("q")
                keycode = d.keysym_to_keycode(keysym)
                base_mod = X.ControlMask | X.Mod1Mask
                root.grab_key(keycode, base_mod, True, X.GrabModeAsync, X.GrabModeAsync)
                d.sync()

                # Now MyVoice tries to grab the same combo -> should fail and roll back
                err2 = app._try_rebind_hotkey("<Control><Alt>q")
                results.append(f"rebind_conflict_err={err2!r}")
                results.append(f"active_accel_after_conflict={app._hotkey._accel_str!r}")

                # Release our conflicting grab.
                root.ungrab_key(keycode, base_mod)
                d.sync()
                d.close()
            except Exception as e:
                results.append(f"conflict_setup_failed={e!r}")

            # Reset to default.
            err3 = app._try_rebind_hotkey("<Super><Shift>space")
            results.append(f"reset_default_err={err3!r}")
            results.append(f"active_accel_after_reset={app._hotkey._accel_str!r}")
        finally:
            app.quit_app()
        return False

    GLib.timeout_add(200, bootstrap)
    app.run([])
    for r in results:
        print("R:", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
