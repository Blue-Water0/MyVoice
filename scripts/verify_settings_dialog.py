"""Open MyVoice, open the Settings dialog, verify Record Shortcut is present
and the hotkey label reflects the current binding. Non-interactive smoke."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gi  # noqa: E402
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from myvoice.services.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    setup_logging("INFO")
    from myvoice.app import MyVoiceApp
    from myvoice.ui.settings_dialog import RecordShortcutDialog

    app = MyVoiceApp()
    results: list[str] = []

    def inspect_dialog(dlg):
        # Walk the dialog widget tree looking for the hotkey UI.
        found = {
            "record_btn": False, "reset_btn": False, "label": False,
            "hint": False,
        }

        def visit(w):
            if isinstance(w, Gtk.Button):
                lbl = (w.get_label() or "")
                if "Record Shortcut" in lbl:
                    found["record_btn"] = True
                if "Reset to Default" in lbl:
                    found["reset_btn"] = True
            if isinstance(w, Gtk.Label):
                txt = w.get_text()
                if txt and ("Super+Shift+Space" in txt or "Ctrl+" in txt):
                    found["label"] = True
                if txt and "Record Shortcut" in txt and "Esc" in txt:
                    found["hint"] = True
            if isinstance(w, Gtk.Container):
                for c in w.get_children():
                    visit(c)

        visit(dlg)
        for k, v in found.items():
            results.append(f"{k}={v}")

        # Model list: 4 presets, each with a status dot + delete menu, and
        # the currently-active preset's Delete item disabled.
        results.append(f"model_row_count={len(dlg._model_rows)}")
        for key, row_w in dlg._model_rows.items():
            dot_classes = row_w.dot.get_style_context().list_classes()
            has_dot_class = (
                "myvoice-model-dot-installed" in dot_classes
                or "myvoice-model-dot-missing" in dot_classes
            )
            results.append(f"model[{key}].has_dot_class={has_dot_class}")
            results.append(f"model[{key}].subtitle_nonempty={bool(row_w.subtitle.get_text())}")
        active = dlg._active_model_key
        active_delete_sensitive = dlg._model_rows[active].delete_item.get_sensitive()
        results.append(f"active_model_delete_blocked={not active_delete_sensitive}")

    def bootstrap():
        from myvoice.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(
            app._window,
            app._settings,
            lambda *_: None,
            lambda: None,
            on_try_hotkey=lambda *_: None,
        )
        inspect_dialog(dlg)
        dlg.destroy()

        # Also instantiate the recorder dialog (without run) to make sure it constructs.
        rec = RecordShortcutDialog(parent=app._window)
        results.append(f"record_dialog_type={rec.get_title()!r}")
        rec.destroy()

        app.quit_app()
        return False

    GLib.timeout_add(200, bootstrap)
    app.run([])
    print("VERIFY:", " | ".join(results))
    # Everything expected must be present:
    all_ok = all(
        "=True" in r for r in results
        if r.startswith((
            "record_btn", "reset_btn", "label", "hint",
            "model[", "active_model_delete_blocked",
        ))
    )
    all_ok = all_ok and "model_row_count=4" in results
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
