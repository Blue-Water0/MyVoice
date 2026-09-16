"""Settings dialog window."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore

from ..engines.base import ModelPreset
from ..engines.registry import engine_info
from ..models import model_manager
from ..services.hotkey_service import (
    DEFAULT_HOTKEY,
    canonical_label_from_string,
    ensure_valid,
    format_accel,
    parse_accel,
)
from ..services.language_service import LANGUAGE_LABELS
from ..services.settings_service import Settings
from ..services.transcript_export import (
    SYSTEM_DEFAULT_ID,
    discover_text_apps,
    get_default_text_app,
)
from .shortcut_recorder import KeyEvent, RecorderState, ShortcutRecorder

log = logging.getLogger(__name__)


@dataclass
class _ModelRowWidgets:
    row: Gtk.ListBoxRow
    dot: Gtk.Label
    subtitle: Gtk.Label
    progress: Gtk.ProgressBar
    menu_btn: Gtk.MenuButton
    delete_item: Gtk.MenuItem


class SettingsDialog(Gtk.Dialog):
    def __init__(
        self,
        parent: Gtk.Window,
        settings: Settings,
        on_apply: Callable[[Settings], None],
        on_clear_model_cache: Callable[[], None],
        on_try_hotkey: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        """Settings dialog.

        Parameters:
            on_try_hotkey: If provided, called with a new accelerator string
                when the user assigns one via the Record Shortcut UI. Return
                None on success, or a human error message on failure — in the
                error case the settings dialog keeps the previous shortcut
                and shows the message. This lets the app immediately attempt
                the X11 grab and fall back cleanly on conflict.
        """
        super().__init__(title="MyVoice Settings", transient_for=parent, flags=0)
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Apply", Gtk.ResponseType.OK)
        self.set_default_size(520, 640)
        self.get_style_context().add_class("myvoice-window")
        self._on_apply = on_apply
        self._on_clear_model_cache = on_clear_model_cache
        self._on_try_hotkey = on_try_hotkey
        self._settings = settings
        # Live current-hotkey string (in the GTK accel form used by the
        # settings service). Updated when the user records a new shortcut.
        self._current_hotkey_accel = settings.hotkey

        content = self.get_content_area()
        content.set_spacing(14)
        content.set_margin_start(16); content.set_margin_end(16)
        content.set_margin_top(16); content.set_margin_bottom(16)
        content.get_style_context().add_class("myvoice-window")

        # App-lifecycle checkboxes sit above the cards — they are not
        # voice/language settings, so they don't belong in a titled card.
        lifecycle_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self._start_min = Gtk.CheckButton(label="Start minimized to tray")
        self._start_min.set_active(settings.start_minimized)
        lifecycle_row.pack_start(self._start_min, False, False, 0)
        self._autostart = Gtk.CheckButton(label="Start MyVoice automatically on login")
        self._autostart.set_active(settings.autostart)
        lifecycle_row.pack_start(self._autostart, False, False, 0)
        content.pack_start(lifecycle_row, False, False, 0)

        # ---- Card: Voice & Language ----
        voice_card, voice_grid = self._start_card("Voice & Language")
        row = 0
        self._lang = Gtk.ComboBoxText()
        self._lang.get_style_context().add_class("myvoice-settings-input")
        for code, label in LANGUAGE_LABELS.items():
            self._lang.append(code, label)
        self._lang.set_active_id(settings.language_mode)
        voice_grid.attach(Gtk.Label(label="Language:", xalign=0), 0, row, 1, 1)
        voice_grid.attach(self._lang, 1, row, 1, 1)
        row += 1

        voice_grid.attach(Gtk.Label(label="Model quality:", xalign=0), 0, row, 1, 1)
        row += 1
        model_list_frame = Gtk.Frame()
        model_list_frame.get_style_context().add_class("myvoice-model-list-frame")
        self._model_list = Gtk.ListBox()
        self._model_list.get_style_context().add_class("myvoice-model-list")
        self._model_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        model_list_frame.add(self._model_list)
        voice_grid.attach(model_list_frame, 0, row, 2, 1)
        row += 1

        self._preset_by_key: dict[str, ModelPreset] = {}
        self._model_rows: dict[str, _ModelRowWidgets] = {}
        self._active_model_key = settings.model
        self._suppress_model_selection = False
        info = engine_info(settings.engine)
        for preset in info.presets:
            self._preset_by_key[preset.key] = preset
            self._add_model_row(preset)
        self._model_list.connect("row-selected", self._on_model_row_selected)
        self._select_model_row(self._active_model_key)
        self._refresh_all_model_rows()

        self._compute = Gtk.ComboBoxText()
        self._compute.get_style_context().add_class("myvoice-settings-input")
        for ct, label in [
            ("auto", "Auto (int8, best CPU perf)"),
            ("int8", "int8 (lowest RAM)"),
            ("int8_float32", "int8_float32 (balanced)"),
            ("float32", "float32 (highest quality, slowest)"),
        ]:
            self._compute.append(ct, label)
        self._compute.set_active_id(settings.compute_type)
        voice_grid.attach(Gtk.Label(label="Compute precision:", xalign=0), 0, row, 1, 1)
        voice_grid.attach(self._compute, 1, row, 1, 1)
        row += 1

        clear_btn = Gtk.Button(label="Clear downloaded models cache")
        clear_btn.get_style_context().add_class("myvoice-settings-input")
        clear_btn.connect("clicked", lambda *_: self._on_clear_model_cache())
        voice_grid.attach(clear_btn, 0, row, 2, 1)
        content.pack_start(voice_card, False, False, 0)

        # ---- Card: Shortcut ----
        hotkey_card, hotkey_grid = self._start_card("Shortcut")
        hk_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        try:
            self._hotkey_label = Gtk.Label(
                label=canonical_label_from_string(settings.hotkey), xalign=0.5,
            )
        except Exception:
            # Bad accel in settings — display raw and let apply/reset fix it.
            self._hotkey_label = Gtk.Label(label=settings.hotkey, xalign=0.5)
        self._hotkey_label.set_selectable(True)
        self._hotkey_label.set_hexpand(True)
        self._hotkey_label.get_style_context().add_class("myvoice-hotkey-chip")
        hk_box.pack_start(self._hotkey_label, True, True, 0)

        self._record_btn = Gtk.Button(label="Record Shortcut")
        self._record_btn.get_style_context().add_class("myvoice-settings-input")
        self._record_btn.connect("clicked", lambda *_: self._open_recorder())
        hk_box.pack_end(self._record_btn, False, False, 0)

        self._reset_btn = Gtk.Button(label="Reset to Default")
        self._reset_btn.get_style_context().add_class("myvoice-settings-input")
        self._reset_btn.connect("clicked", lambda *_: self._reset_hotkey_to_default())
        hk_box.pack_end(self._reset_btn, False, False, 0)
        hotkey_grid.attach(hk_box, 0, 0, 2, 1)

        hint = Gtk.Label(
            label=(
                "Click <b>Record Shortcut</b>, then press the key combination you "
                "want to use. Press <b>Esc</b> to cancel."
            ),
            xalign=0,
            use_markup=True,
        )
        hint.get_style_context().add_class("myvoice-privacy-note")
        hint.set_line_wrap(True)
        hotkey_grid.attach(hint, 0, 1, 2, 1)

        self._hotkey_feedback = Gtk.Label(label="", xalign=0)
        self._hotkey_feedback.set_line_wrap(True)
        hotkey_grid.attach(self._hotkey_feedback, 0, 2, 2, 1)
        content.pack_start(hotkey_card, False, False, 0)

        # ---- Card: Transcript text app ----
        text_card, text_grid = self._start_card("Transcript text app")
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row_box.pack_start(Gtk.Label(label="Open transcripts in:", xalign=0), False, False, 0)
        self._text_app_combo = Gtk.ComboBoxText()
        self._text_app_combo.set_hexpand(True)
        self._text_app_combo.get_style_context().add_class("myvoice-settings-input")
        row_box.pack_start(self._text_app_combo, True, True, 0)

        refresh_btn = Gtk.Button.new_from_icon_name(
            "view-refresh", Gtk.IconSize.BUTTON,
        )
        refresh_btn.set_tooltip_text("Refresh list of installed text apps")
        refresh_btn.get_style_context().add_class("myvoice-icon-button")
        refresh_btn.connect(
            "clicked",
            lambda *_: self._refresh_text_app_combo(
                self._current_text_app_id() or settings.text_app_desktop_id,
            ),
        )
        row_box.pack_end(refresh_btn, False, False, 0)
        text_grid.attach(row_box, 0, 0, 2, 1)

        hint = Gtk.Label(
            label=(
                "The <b>Open in Text App</b> button in the main window opens the "
                "current transcript here as a temporary .txt file for viewing/"
                "editing — it isn't saved permanently unless you save it "
                "yourself from the editor."
            ),
            xalign=0,
            use_markup=True,
        )
        hint.get_style_context().add_class("myvoice-privacy-note")
        hint.set_line_wrap(True)
        text_grid.attach(hint, 0, 1, 2, 1)

        self._refresh_text_app_combo(settings.text_app_desktop_id)
        content.pack_start(text_card, False, False, 0)

        # ---- Card: Advanced (VAD) ----
        vad_card, vad_grid = self._start_card("Advanced (VAD)")
        self._agg = Gtk.SpinButton.new_with_range(0, 3, 1); self._agg.set_value(settings.vad.aggressiveness)
        self._min_sp = Gtk.SpinButton.new_with_range(50, 3000, 50); self._min_sp.set_value(settings.vad.min_speech_ms)
        self._silence = Gtk.SpinButton.new_with_range(100, 3000, 50); self._silence.set_value(settings.vad.silence_ms)
        self._max_seg = Gtk.SpinButton.new_with_range(1000, 30000, 500); self._max_seg.set_value(settings.vad.max_segment_ms)
        for w in (self._agg, self._min_sp, self._silence, self._max_seg):
            w.get_style_context().add_class("myvoice-settings-input")

        vad_grid.attach(Gtk.Label(label="Aggressiveness (0..3):", xalign=0), 0, 0, 1, 1); vad_grid.attach(self._agg, 1, 0, 1, 1)
        vad_grid.attach(Gtk.Label(label="Min speech (ms):", xalign=0), 0, 1, 1, 1); vad_grid.attach(self._min_sp, 1, 1, 1, 1)
        vad_grid.attach(Gtk.Label(label="Trailing silence (ms):", xalign=0), 0, 2, 1, 1); vad_grid.attach(self._silence, 1, 2, 1, 1)
        vad_grid.attach(Gtk.Label(label="Max segment (ms):", xalign=0), 0, 3, 1, 1); vad_grid.attach(self._max_seg, 1, 3, 1, 1)
        content.pack_start(vad_card, False, False, 0)

        self.show_all()

    def _start_card(self, title: str) -> tuple[Gtk.Box, Gtk.Grid]:
        """Build one titled card: a vertical box with a title label
        above a field grid. Returns (card_box, field_grid) so callers
        attach fields to the grid and pack the box into the dialog.
        """
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("myvoice-settings-card")
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.get_style_context().add_class("myvoice-settings-card-title")
        card.pack_start(title_label, False, False, 0)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        grid.set_hexpand(True)
        card.pack_start(grid, False, False, 0)
        return card, grid

    # ---- Model list (install status, install/delete) ----------------------

    def _add_model_row(self, preset: ModelPreset) -> None:
        row = Gtk.ListBoxRow()
        row.preset_key = preset.key  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(8); box.set_margin_bottom(8)
        box.set_margin_start(10); box.set_margin_end(10)

        dot = Gtk.Label(label="●")  # ●
        dot.get_style_context().add_class("myvoice-model-dot")
        box.pack_start(dot, False, False, 0)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        title = Gtk.Label(label=preset.label, xalign=0)
        subtitle = Gtk.Label(xalign=0)
        subtitle.get_style_context().add_class("myvoice-privacy-note")
        text_box.pack_start(title, False, False, 0)
        text_box.pack_start(subtitle, False, False, 0)
        box.pack_start(text_box, True, True, 0)

        progress = Gtk.ProgressBar()
        progress.set_hexpand(True)
        progress.set_no_show_all(True)
        progress.set_visible(False)
        box.pack_start(progress, True, True, 0)

        menu_btn = Gtk.MenuButton()
        menu_btn.get_style_context().add_class("myvoice-icon-button")
        menu_btn.set_image(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON))
        menu = Gtk.Menu()
        delete_item = Gtk.MenuItem(label="Delete")
        delete_item.connect("activate", lambda *_: self._on_delete_model_clicked(preset.key))
        menu.append(delete_item)
        menu.show_all()
        menu_btn.set_popup(menu)
        box.pack_start(menu_btn, False, False, 0)

        row.add(box)
        self._model_list.add(row)
        self._model_rows[preset.key] = _ModelRowWidgets(
            row=row, dot=dot, subtitle=subtitle, progress=progress,
            menu_btn=menu_btn, delete_item=delete_item,
        )

    def _select_model_row(self, key: str) -> None:
        row_w = self._model_rows.get(key)
        if row_w is None:
            return
        self._suppress_model_selection = True
        try:
            self._model_list.select_row(row_w.row)
        finally:
            self._suppress_model_selection = False

    def _on_model_row_selected(self, _listbox: Gtk.ListBox, row: Optional[Gtk.ListBoxRow]) -> None:
        if row is None or self._suppress_model_selection:
            return
        key: str = row.preset_key  # type: ignore[attr-defined]
        if key == self._active_model_key:
            return
        preset = self._preset_by_key[key]
        if model_manager.is_installed(key):
            self._active_model_key = key
            self._refresh_all_model_rows()
            return

        proceed = self._confirm_yes_no(
            "Model not installed",
            f"'{preset.label}' is not installed yet "
            f"(~{preset.approx_download_mb} MB download).\n\n"
            "Install it now?",
        )
        if not proceed:
            self._select_model_row(self._active_model_key)
            return
        self._start_model_install(key)

    def _start_model_install(self, key: str) -> None:
        row_w = self._model_rows[key]
        row_w.dot.hide()
        row_w.progress.set_fraction(0.0)
        row_w.progress.set_visible(True)
        row_w.subtitle.set_text("Downloading…")
        self._model_list.set_sensitive(False)

        def worker() -> None:
            def on_progress(done: int, total: int) -> None:
                GLib.idle_add(self._update_install_progress, key, done, total)
            try:
                model_manager.install_model(key, on_progress=on_progress)
            except Exception as e:  # noqa: BLE001 - surfaced to the user below
                GLib.idle_add(self._on_install_finished, key, str(e))
            else:
                GLib.idle_add(self._on_install_finished, key, None)

        threading.Thread(target=worker, daemon=True).start()

    def _update_install_progress(self, key: str, done: int, total: int) -> bool:
        row_w = self._model_rows.get(key)
        if row_w is not None:
            if total > 0:
                row_w.progress.set_fraction(min(1.0, done / total))
            else:
                row_w.progress.pulse()
        return False

    def _on_install_finished(self, key: str, error: Optional[str]) -> bool:
        self._model_list.set_sensitive(True)
        row_w = self._model_rows.get(key)
        if row_w is not None:
            row_w.progress.set_visible(False)
            row_w.dot.show()
        if error:
            self._show_error(f"Failed to install model: {error}")
            self._select_model_row(self._active_model_key)
        else:
            self._active_model_key = key
            self._select_model_row(key)
        self._refresh_all_model_rows()
        return False

    def _on_delete_model_clicked(self, key: str) -> None:
        preset = self._preset_by_key[key]
        if key == self._active_model_key:
            self._show_error(
                f"'{preset.label}' is currently selected — switch to a "
                "different model first, then delete this one."
            )
            return
        size = model_manager.installed_size_bytes(key)
        proceed = self._confirm_yes_no(
            "Delete model",
            f"Delete '{preset.label}'? This frees "
            f"{model_manager.human_size(size)}.\n\n"
            "You can re-download it later by selecting it again.",
        )
        if not proceed:
            return
        try:
            model_manager.delete_model(key)
        except Exception as e:
            self._show_error(f"Failed to delete model: {e}")
            return
        self._refresh_all_model_rows()

    def _refresh_all_model_rows(self) -> None:
        for key in self._model_rows:
            self._refresh_model_row(key)

    def _refresh_model_row(self, key: str) -> None:
        row_w = self._model_rows[key]
        preset = self._preset_by_key[key]
        installed = model_manager.is_installed(key)

        ctx = row_w.dot.get_style_context()
        ctx.remove_class("myvoice-model-dot-installed")
        ctx.remove_class("myvoice-model-dot-missing")
        ctx.add_class("myvoice-model-dot-installed" if installed else "myvoice-model-dot-missing")

        if installed:
            size = model_manager.installed_size_bytes(key)
            row_w.subtitle.set_text(f"{model_manager.human_size(size)} installed")
        else:
            row_w.subtitle.set_text(f"~{preset.approx_download_mb} MB download — not installed")

        is_active = key == self._active_model_key
        row_w.menu_btn.set_sensitive(installed)
        row_w.delete_item.set_sensitive(installed and not is_active)

    def _confirm_yes_no(self, title: str, message: str) -> bool:
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text=title,
        )
        dlg.format_secondary_text(message)
        response = dlg.run()
        dlg.destroy()
        return response == Gtk.ResponseType.YES

    # ---- Transcript text app dropdown -------------------------------------

    def _refresh_text_app_combo(self, selected_id: Optional[str]) -> None:
        """Repopulate the text-app dropdown from GIO discovery."""
        combo = self._text_app_combo
        combo.remove_all()
        # System default entry is always first, always present. The
        # combo entry id "" matches SYSTEM_DEFAULT_ID in settings.
        default_entry = None
        try:
            default_entry = get_default_text_app()
        except Exception:
            default_entry = None
        if default_entry is not None:
            combo.append(
                SYSTEM_DEFAULT_ID,
                f"System default text editor ({default_entry.name})",
            )
        else:
            combo.append(
                SYSTEM_DEFAULT_ID, "System default text editor",
            )

        try:
            apps = discover_text_apps()
        except Exception:
            apps = []
        for app in apps:
            combo.append(app.desktop_id, app.name)

        # If the previously-selected app is not installed any more,
        # fall back to the system default in the UI.
        target = selected_id or SYSTEM_DEFAULT_ID
        combo.set_active_id(target)
        if combo.get_active_id() != target:
            combo.set_active_id(SYSTEM_DEFAULT_ID)

    def _current_text_app_id(self) -> Optional[str]:
        aid = self._text_app_combo.get_active_id()
        return aid if aid is not None else SYSTEM_DEFAULT_ID

    def collect(self) -> Optional[Settings]:
        """Read UI back into a Settings. Hotkey is always canonicalised."""
        from ..services.settings_service import VadSettings

        hotkey = self._current_hotkey_accel
        try:
            parse_accel(hotkey)
        except Exception as e:
            self._show_error(f"Invalid hotkey: {e}")
            return None

        s = Settings(
            schema_version=self._settings.schema_version,
            language_mode=self._lang.get_active_id() or "auto",
            hotkey=hotkey,
            microphone=self._settings.microphone,  # unchanged here; handled in main window
            start_minimized=self._start_min.get_active(),
            autostart=self._autostart.get_active(),
            engine=self._settings.engine,
            model=self._active_model_key,
            compute_type=self._compute.get_active_id() or "auto",
            text_app_desktop_id=self._current_text_app_id() or SYSTEM_DEFAULT_ID,
            vad=VadSettings(
                aggressiveness=int(self._agg.get_value()),
                min_speech_ms=int(self._min_sp.get_value()),
                silence_ms=int(self._silence.get_value()),
                max_segment_ms=int(self._max_seg.get_value()),
            ),
        )
        return s

    def _show_error(self, msg: str) -> None:
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text=msg,
        )
        dlg.run(); dlg.destroy()

    # ---- Record / Reset shortcut ------------------------------------------

    def _open_recorder(self) -> None:
        dlg = RecordShortcutDialog(parent=self)
        response = dlg.run()
        parsed = dlg.captured
        dlg.destroy()
        if response != Gtk.ResponseType.OK or parsed is None:
            self._set_hotkey_feedback("Recording cancelled.", ok=False)
            return
        try:
            ensure_valid(parsed)
        except ValueError as e:
            self._set_hotkey_feedback(f"Invalid combination: {e}", ok=False)
            return
        new_accel = format_accel(parsed.mods, parsed.key)
        self._try_assign_hotkey(new_accel)

    def _reset_hotkey_to_default(self) -> None:
        self._try_assign_hotkey(DEFAULT_HOTKEY, is_reset=True)

    def _try_assign_hotkey(self, new_accel: str, is_reset: bool = False) -> None:
        """Attempt to make ``new_accel`` the active hotkey.

        Uses on_try_hotkey callback if provided so the app can perform the
        actual X11 grab and roll back on conflict. If no callback is provided
        (e.g. dialog is used standalone), the assignment is treated as
        successful and simply updates the label.
        """
        # First: run pure validation.
        try:
            parsed = parse_accel(new_accel)
            ensure_valid(parsed)
        except ValueError as e:
            self._set_hotkey_feedback(f"Invalid shortcut: {e}", ok=False)
            return

        # If the app callback is available, ask it to try the grab now so
        # conflicts are surfaced immediately and the previous binding is
        # preserved.
        if self._on_try_hotkey is not None:
            err = self._on_try_hotkey(new_accel)
            if err is not None:
                self._set_hotkey_feedback(
                    f"That shortcut is already in use ({err}). "
                    "Please choose another combination — your previous "
                    "shortcut is still active.",
                    ok=False,
                )
                return

        self._current_hotkey_accel = new_accel
        try:
            display = canonical_label_from_string(new_accel)
        except Exception:
            display = new_accel
        self._hotkey_label.set_text(display)
        if is_reset:
            self._set_hotkey_feedback(f"Reset to default: {display}.", ok=True)
        else:
            self._set_hotkey_feedback(f"Shortcut set to {display}.", ok=True)

    def _set_hotkey_feedback(self, msg: str, ok: bool) -> None:
        ctx = self._hotkey_feedback.get_style_context()
        ctx.remove_class("myvoice-status-ready")
        ctx.remove_class("myvoice-status-error")
        ctx.add_class("myvoice-status-ready" if ok else "myvoice-status-error")
        self._hotkey_feedback.set_text(msg)


# --------------------------------------------------------------------------- Record dialog


class RecordShortcutDialog(Gtk.Dialog):
    """Modal dialog that grabs keyboard focus and waits for a key combo."""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(
            title="Record Shortcut",
            transient_for=parent,
            modal=True,
            flags=0,
        )
        self.set_default_size(360, 140)
        self.get_style_context().add_class("myvoice-window")
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self._recorder = ShortcutRecorder()
        self.captured = None  # ParsedAccel or None on exit

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_margin_start(16); content.set_margin_end(16)
        content.set_margin_top(16); content.set_margin_bottom(16)

        self._prompt = Gtk.Label(
            label="Press your shortcut now…\n(Esc to cancel)",
            xalign=0.5, justify=Gtk.Justification.CENTER,
        )
        self._prompt.set_line_wrap(True)
        content.pack_start(self._prompt, True, True, 0)

        # We use event masks + key-press-event signal to catch keys.
        self.set_events(self.get_events() | Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("key-press-event", self._on_key_press)

        # Start in RECORDING mode.
        self._recorder.start_recording()

    def _on_key_press(self, _widget, event) -> bool:  # noqa: ANN001
        name = Gdk.keyval_name(event.keyval)
        ke = KeyEvent(keyval_name=name, state=int(event.state))
        state = self._recorder.feed_key(ke)
        if state is RecorderState.CAPTURED:
            self.captured = self._recorder.captured
            self.response(Gtk.ResponseType.OK)
            return True
        if state is RecorderState.CANCELLED:
            self.captured = None
            self.response(Gtk.ResponseType.CANCEL)
            return True
        # Still recording (e.g. modifier-only) — swallow the event so it
        # doesn't leak to underlying widgets.
        return True
