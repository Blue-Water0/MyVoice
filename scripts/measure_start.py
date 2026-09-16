"""Measure start-listening timings against a running MyVoiceApp instance.

Runs the real GTK app but never presses the hardware hotkey — instead it
invokes app.start_listening() from GLib.idle_add on the main thread to
measure the same code path.

Prints one CSV line per phase to stdout so it can be diffed before/after.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Ensure project root importable when run as script
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib  # type: ignore  # noqa: E402

from myvoice.services.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-seconds", type=float, default=1.5,
                        help="How long to remain 'listening' before stop")
    parser.add_argument("--start-delay", type=float, default=0.6,
                        help="Delay after app.run before calling start_listening")
    parser.add_argument("--exit-after", type=float, default=8.0)
    parser.add_argument("--skip-preload", action="store_true",
                        help="If set, force model_loaded=False before start")
    args = parser.parse_args()

    setup_logging("DEBUG")
    from myvoice.app import MyVoiceApp

    app = MyVoiceApp()

    events: list[tuple[str, float]] = []
    t0 = time.monotonic()

    def mark(label: str):
        events.append((label, (time.monotonic() - t0) * 1000.0))

    def instrument(_app):
        # Wrap _set_status so we can see when 'listening', 'processing', 'ready' fire
        original = _app._set_status
        def wrapped(key, msg):
            mark(f"set_status:{key}")
            return original(key, msg)
        _app._set_status = wrapped

        # Wrap overlay
        overlay = _app._overlay
        orig_show = overlay.show_listening
        def wrap_show():
            mark("overlay:show_listening")
            return orig_show()
        overlay.show_listening = wrap_show

        # Wrap audio start
        audio = _app._audio
        orig_astart = audio.start
        def wrap_astart(*a, **k):
            mark("audio:start_call")
            r = orig_astart(*a, **k)
            mark("audio:start_return")
            return r
        audio.start = wrap_astart

    def kick():
        mark("run:start_listening")
        try:
            app.start_listening()
        except Exception as e:
            mark(f"error:{e}")
        return False

    def stop_it():
        mark("run:stop_listening")
        try:
            app.stop_listening()
        except Exception as e:
            mark(f"stop_error:{e}")
        return False

    def bootstrap():
        mark("run:bootstrap")
        instrument(app)
        if args.skip_preload and hasattr(app, "_model_ready"):
            app._model_ready = False
        GLib.timeout_add(int(args.start_delay * 1000), kick)
        GLib.timeout_add(int((args.start_delay + args.listen_seconds) * 1000), stop_it)
        GLib.timeout_add(int(args.exit_after * 1000), lambda: (app.quit_app(), False)[1])
        return False

    GLib.idle_add(bootstrap)
    mark("app:run_before")
    rc = app.run(sys.argv[:1])
    mark("app:run_after")

    print()
    print("=== MEASUREMENT ===")
    prev = 0.0
    for label, t in events:
        delta = t - prev
        print(f"{t:8.2f} ms   Δ+{delta:8.2f}   {label}")
        prev = t
    return rc


if __name__ == "__main__":
    sys.exit(main())
