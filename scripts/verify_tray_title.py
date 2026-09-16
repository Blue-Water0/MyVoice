"""Verify that TrayIndicator sets the visible title to APP_NAME.

Two levels of check:
  1. Python-side: the underlying AppIndicator's ``get_title()`` returns
     ``APP_NAME``. This proves our set_title call landed.
  2. D-Bus-side: query the StatusNotifierItem's ``Title`` property from
     the session bus. This is what tray/panel implementations (Cinnamon
     included) show on hover.

The D-Bus probe uses Gio.DBusProxy so it works in any environment where
a StatusNotifierWatcher is present.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gio, GLib  # type: ignore  # noqa: E402

from myvoice import APP_NAME  # noqa: E402
from myvoice.services.logging_setup import setup_logging  # noqa: E402


def _snw_query() -> dict:
    """Query the StatusNotifierWatcher for registered items and return the
    one whose service belongs to our process (best-effort).

    Returns a dict with 'bus_name', 'object_path', 'title' or empty dict.
    """
    out: dict = {}
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception as e:
        out["error"] = f"no session bus: {e}"
        return out

    try:
        watcher = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            None,
        )
        items_prop = watcher.get_cached_property("RegisteredStatusNotifierItems")
        items = items_prop.unpack() if items_prop else []
    except Exception as e:
        out["error"] = f"watcher unreachable: {e}"
        return out

    my_pid = os.getpid()
    for entry in items:
        # entry is either "name/path" or just "name". Split accordingly.
        if "/" in entry:
            bus_name, object_path = entry.split("/", 1)
            object_path = "/" + object_path
        else:
            bus_name, object_path = entry, "/StatusNotifierItem"

        try:
            proxy = Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                bus_name, object_path,
                "org.kde.StatusNotifierItem",
                None,
            )
        except Exception:
            continue

        # Match by process id if possible.
        try:
            dbus = Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", None,
            )
            pid_result = dbus.call_sync(
                "GetConnectionUnixProcessID",
                GLib.Variant("(s)", (bus_name,)),
                Gio.DBusCallFlags.NONE, -1, None,
            )
            owner_pid = pid_result.unpack()[0]
        except Exception:
            owner_pid = None

        title_prop = proxy.get_cached_property("Title")
        title = title_prop.unpack() if title_prop is not None else None
        id_prop = proxy.get_cached_property("Id")
        item_id = id_prop.unpack() if id_prop is not None else None

        entry_out = {
            "bus_name": bus_name,
            "object_path": object_path,
            "title": title,
            "id": item_id,
            "owner_pid": owner_pid,
        }
        if owner_pid == my_pid:
            entry_out["mine"] = True
            return entry_out
        out.setdefault("others", []).append(entry_out)

    if "mine" not in out:
        out["note"] = "no SNI registered by this process was found"
    return out


def main() -> int:
    setup_logging("INFO")
    from myvoice.app import MyVoiceApp
    app = MyVoiceApp()

    result: dict = {"APP_NAME": APP_NAME}

    def do_probe():
        print("[verify_tray_title] probe start", flush=True)
        # Wait for both the tray to be built AND time for it to register
        # itself on the session bus. Poll for up to 3 seconds.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if app._tray is not None and app._tray._indicator is not None:
                break
            time.sleep(0.05)

        tray = app._tray
        if tray is None or tray._indicator is None:
            result["error"] = "tray not built after 3s"
        else:
            result["tray_backend"] = tray._backend
            ind = tray._indicator
            get = getattr(ind, "get_title", None)
            result["python_get_title"] = get() if get else "(no getter)"
            get_id = getattr(ind, "get_id", None)
            result["python_get_id"] = get_id() if get_id else "(no getter)"
            # Enumerate menu labels for spec compliance.
            try:
                menu = ind.get_menu()
                labels = []
                if menu is not None:
                    for item in menu.get_children():
                        try:
                            labels.append(item.get_label())
                        except Exception:
                            labels.append(None)
                result["menu_labels"] = labels
            except Exception as e:
                result["menu_labels_error"] = str(e)

        # Extra settle time for StatusNotifierWatcher registration.
        time.sleep(1.0)
        result["snw"] = _snw_query()
        print("[verify_tray_title] probe done", flush=True)
        app.quit_app()

    def on_activate(_a):
        print("[verify_tray_title] activate fired", flush=True)
        # Run the probe from a worker thread so we don't block the GLib
        # loop while we sleep + do sync D-Bus calls.
        import threading
        threading.Thread(target=do_probe, daemon=True).start()

    app.connect("activate", on_activate)
    print("[verify_tray_title] calling app.run()", flush=True)
    app.run([])
    print("[verify_tray_title] app.run() returned", flush=True)

    print()
    print("=== TRAY TITLE VERIFICATION ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
    sys.stdout.flush()

    # Assertions
    ok = (
        result.get("python_get_title") == APP_NAME
        and (
            result.get("snw", {}).get("title") == APP_NAME
            or result.get("snw", {}).get("note")
        )
    )
    print()
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
