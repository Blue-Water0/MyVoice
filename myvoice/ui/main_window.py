"""Main application window.

Layout: header with status; big Start/Stop button; language dropdown; mic
dropdown; live transcript preview; Settings and Quit buttons; privacy note.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore

from .. import APP_NAME
from ..services.language_service import LANGUAGE_LABELS

log = logging.getLogger(__name__)


STATUS_CLASSES = {
    "idle": "myvoice-status-idle",
    "listening": "myvoice-status-listen",
    "processing": "myvoice-status-process",
    "ready": "myvoice-status-ready",
    "error": "myvoice-status-error",
}


class MainWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        application: Gtk.Application,
        on_toggle: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_quit: Callable[[], None],
        on_language_changed: Callable[[str], None],
        on_mic_changed: Callable[[Optional[str]], None],
        get_input_devices: Callable[[], list[dict]],
        initial_language_mode: str,
        initial_mic: Optional[str],
        on_copy_transcript: Optional[Callable[[], None]] = None,
        on_open_transcript_in_text_app: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(560, 460)
        self.set_icon_name("audio-input-microphone")
        self.get_style_context().add_class("myvoice-window")

        self._on_toggle = on_toggle
        self._on_open_settings = on_open_settings
        self._on_quit = on_quit
        self._on_language_changed = on_language_changed
        self._on_mic_changed = on_mic_changed
        self._get_input_devices = get_input_devices
        self._on_copy_transcript = on_copy_transcript
        self._on_open_transcript_in_text_app = on_open_transcript_in_text_app

        self._listening = False
        self._glow_class_is_a = True
        self._glow_timer_id: Optional[int] = None

        self._build_ui(initial_language_mode, initial_mic)
        self._apply_status("idle", "Idle")
        # Start disabled — no transcript yet.
        self.set_transcript_actions_enabled(False)
        self._start_glow()

    # ---- construction ---------------------------------------------------

    def _build_ui(self, lang_mode: str, mic_name: Optional[str]) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_start(16); outer.set_margin_end(16)
        outer.set_margin_top(16); outer.set_margin_bottom(16)
        self.add(outer)
        outer.get_style_context().add_class("myvoice-window")

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=f"<span size='xx-large' weight='bold'>{APP_NAME}</span>")
        title.set_use_markup(True)
        title.set_xalign(0)
        title.get_style_context().add_class("myvoice-title")
        header.pack_start(title, True, True, 0)

        settings_btn = Gtk.Button.new_from_icon_name("preferences-system", Gtk.IconSize.BUTTON)
        settings_btn.set_tooltip_text("Settings")
        settings_btn.get_style_context().add_class("myvoice-icon-button")
        settings_btn.connect("clicked", lambda *_: self._on_open_settings())
        header.pack_end(settings_btn, False, False, 0)
        outer.pack_start(header, False, False, 0)

        # Status
        self._status_label = Gtk.Label(label="Idle")
        self._status_label.get_style_context().add_class("myvoice-status")
        self._status_label.set_xalign(0)
        outer.pack_start(self._status_label, False, False, 0)

        # Big button. Marked *unfocusable* so clicking it does not steal
        # keyboard focus from whatever external text field the user was
        # in. Also prevents the Start button itself from ever being a
        # dictation destination.
        self._toggle_btn = Gtk.Button(label="Start Listening")
        self._toggle_btn.get_style_context().add_class("myvoice-big-button")
        self._toggle_btn.set_can_focus(False)
        self._toggle_btn.set_focus_on_click(False)
        self._toggle_btn.connect("clicked", lambda *_: self._on_toggle())
        outer.pack_start(self._toggle_btn, False, False, 8)

        # Language row
        lang_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lang_row.pack_start(Gtk.Label(label="Language:"), False, False, 0)
        self._lang_combo = Gtk.ComboBoxText()
        self._lang_combo.get_style_context().add_class("myvoice-combo")
        for code, label in LANGUAGE_LABELS.items():
            self._lang_combo.append(code, label)
        self._lang_combo.set_active_id(lang_mode)
        self._lang_combo.connect(
            "changed", lambda c: self._on_language_changed(c.get_active_id())
        )
        lang_row.pack_start(self._lang_combo, True, True, 0)
        outer.pack_start(lang_row, False, False, 0)

        # Mic row
        mic_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mic_row.pack_start(Gtk.Label(label="Microphone:"), False, False, 0)
        self._mic_combo = Gtk.ComboBoxText()
        self._mic_combo.get_style_context().add_class("myvoice-combo")
        self._refresh_mic_combo(mic_name)
        self._mic_combo.connect("changed", self._on_mic_combo_changed)
        mic_row.pack_start(self._mic_combo, True, True, 0)
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh", Gtk.IconSize.BUTTON)
        refresh_btn.set_tooltip_text("Rescan microphones")
        refresh_btn.get_style_context().add_class("myvoice-icon-button")
        refresh_btn.connect("clicked", lambda *_: self._refresh_mic_combo(self._current_mic_id()))
        mic_row.pack_end(refresh_btn, False, False, 0)
        outer.pack_start(mic_row, False, False, 0)

        # Transcript preview: header row (label + right-aligned actions)
        # followed by the scrolled TextView.
        transcript_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
        )
        transcript_header.get_style_context().add_class(
            "myvoice-transcript-header",
        )
        label = Gtk.Label(label="Live transcript preview:", xalign=0)
        transcript_header.pack_start(label, True, True, 0)

        # Two small icon buttons pinned to the right. Kept unfocusable
        # so clicking them never steals keyboard focus from the user's
        # target editor during dictation.
        self._copy_transcript_btn = Gtk.Button.new_from_icon_name(
            "edit-copy-symbolic", Gtk.IconSize.BUTTON,
        )
        self._copy_transcript_btn.set_tooltip_text(
            "Copy transcript to clipboard",
        )
        # Explicit accessible name for screen readers (icon-only button).
        try:
            self._copy_transcript_btn.get_accessible().set_name("Copy transcript")
        except Exception:
            pass
        self._copy_transcript_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._copy_transcript_btn.set_can_focus(False)
        self._copy_transcript_btn.set_focus_on_click(False)
        self._copy_transcript_btn.get_style_context().add_class(
            "myvoice-transcript-action",
        )
        self._copy_transcript_btn.connect(
            "clicked",
            lambda *_: (self._on_copy_transcript() if self._on_copy_transcript
                        else None),
        )

        self._open_transcript_btn = Gtk.Button.new_from_icon_name(
            "document-open-symbolic", Gtk.IconSize.BUTTON,
        )
        self._open_transcript_btn.set_tooltip_text(
            "Open full transcript in text app",
        )
        try:
            self._open_transcript_btn.get_accessible().set_name(
                "Open in Text App",
            )
        except Exception:
            pass
        self._open_transcript_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._open_transcript_btn.set_can_focus(False)
        self._open_transcript_btn.set_focus_on_click(False)
        self._open_transcript_btn.get_style_context().add_class(
            "myvoice-transcript-action",
        )
        self._open_transcript_btn.connect(
            "clicked",
            lambda *_: (self._on_open_transcript_in_text_app()
                        if self._on_open_transcript_in_text_app else None),
        )

        transcript_header.pack_end(self._open_transcript_btn, False, False, 0)
        transcript_header.pack_end(self._copy_transcript_btn, False, False, 0)

        outer.pack_start(transcript_header, False, False, 6)

        scroll = Gtk.ScrolledWindow()
        scroll.get_style_context().add_class("myvoice-transcript-frame")
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(140)
        self._transcript_view = Gtk.TextView()
        self._transcript_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._transcript_view.set_editable(False)
        self._transcript_view.set_cursor_visible(False)
        self._transcript_view.get_style_context().add_class("myvoice-transcript")
        scroll.add(self._transcript_view)
        outer.pack_start(scroll, True, True, 0)

        # Footer
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        privacy = Gtk.Label(label="Local processing — no audio leaves your computer.")
        privacy.get_style_context().add_class("myvoice-privacy-note")
        privacy.set_xalign(0)
        footer.pack_start(privacy, True, True, 0)
        quit_btn = Gtk.Button(label="Quit")
        quit_btn.get_style_context().add_class("myvoice-quit-button")
        quit_btn.connect("clicked", lambda *_: self._on_quit())
        footer.pack_end(quit_btn, False, False, 0)
        outer.pack_start(footer, False, False, 0)

    # ---- public API used by controller ---------------------------------

    def _toggle_glow_tick(self) -> bool:
        ctx = self._toggle_btn.get_style_context()
        if self._glow_class_is_a:
            ctx.remove_class("myvoice-cta-glow-a")
            ctx.add_class("myvoice-cta-glow-b")
        else:
            ctx.remove_class("myvoice-cta-glow-b")
            ctx.add_class("myvoice-cta-glow-a")
        self._glow_class_is_a = not self._glow_class_is_a
        return True  # keep repeating

    def _start_glow(self) -> None:
        if self._glow_timer_id is not None:
            return
        ctx = self._toggle_btn.get_style_context()
        ctx.add_class("myvoice-cta-glow-a")
        self._glow_class_is_a = False
        self._glow_timer_id = GLib.timeout_add(1300, self._toggle_glow_tick)

    def _stop_glow(self) -> None:
        if self._glow_timer_id is not None:
            GLib.source_remove(self._glow_timer_id)
            self._glow_timer_id = None
        ctx = self._toggle_btn.get_style_context()
        ctx.remove_class("myvoice-cta-glow-a")
        ctx.remove_class("myvoice-cta-glow-b")

    def set_listening(self, listening: bool) -> None:
        self._listening = listening
        self._toggle_btn.set_label("Stop Listening" if listening else "Start Listening")
        if listening:
            self._stop_glow()
        else:
            self._start_glow()

    def set_status(self, status: str, message: str) -> None:
        self._apply_status(status, message)

    def append_transcript(self, chunk: str) -> None:
        if not chunk:
            return
        buf = self._transcript_view.get_buffer()
        end = buf.get_end_iter()
        buf.insert(end, chunk if chunk.startswith((" ", "\n")) or buf.get_char_count() == 0
                   else " " + chunk)
        # scroll to end
        end = buf.get_end_iter()
        mark = buf.create_mark(None, end, False)
        self._transcript_view.scroll_to_mark(mark, 0, True, 0, 1)

    def clear_transcript(self) -> None:
        self._transcript_view.get_buffer().set_text("")
        self.set_transcript_actions_enabled(False)

    def set_transcript_actions_enabled(self, enabled: bool) -> None:
        """Enable or disable the Copy / Open transcript buttons.

        The controller calls this with ``True`` as soon as any real
        transcript text has been accumulated and ``False`` when the
        transcript is cleared for a new session.
        """
        try:
            self._copy_transcript_btn.set_sensitive(enabled)
            self._open_transcript_btn.set_sensitive(enabled)
        except AttributeError:
            # Buttons may not exist yet during early construction.
            pass

    def set_language_mode(self, mode: str) -> None:
        # Prevent recursive signal
        if self._lang_combo.get_active_id() != mode:
            self._lang_combo.set_active_id(mode)

    def set_microphone(self, name: Optional[str]) -> None:
        self._refresh_mic_combo(name)

    # ---- helpers -------------------------------------------------------

    def _apply_status(self, key: str, message: str) -> None:
        ctx = self._status_label.get_style_context()
        for cls in STATUS_CLASSES.values():
            ctx.remove_class(cls)
        ctx.add_class(STATUS_CLASSES.get(key, "myvoice-status-idle"))
        self._status_label.set_text(message)

    def _current_mic_id(self) -> Optional[str]:
        aid = self._mic_combo.get_active_id()
        return None if not aid or aid == "__default__" else aid

    def _refresh_mic_combo(self, selected_name: Optional[str]) -> None:
        try:
            self._mic_combo.handler_block_by_func(self._on_mic_combo_changed)
        except Exception:
            pass
        self._mic_combo.remove_all()
        self._mic_combo.append("__default__", "System default")
        try:
            devices = self._get_input_devices()
        except Exception:
            devices = []
        for d in devices:
            self._mic_combo.append(d["name"], d["name"] + ("  (default)" if d.get("default") else ""))
        if selected_name:
            self._mic_combo.set_active_id(selected_name)
            if self._mic_combo.get_active_id() != selected_name:
                self._mic_combo.set_active_id("__default__")
        else:
            self._mic_combo.set_active_id("__default__")
        try:
            self._mic_combo.handler_unblock_by_func(self._on_mic_combo_changed)
        except Exception:
            pass

    def _on_mic_combo_changed(self, combo: Gtk.ComboBoxText) -> None:
        self._on_mic_changed(self._current_mic_id())
