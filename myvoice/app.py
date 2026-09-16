"""MyVoice application entrypoint.

Wires all services and the UI together. Runs on the GTK main loop.

Threading contract:
- All Gtk / Gdk / AT-SPI / clipboard operations happen on the GTK main thread.
- Audio, VAD, transcription, hotkey listen loops run on background threads.
- Cross-thread signalling uses GLib.idle_add.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore

from . import APP_ID, APP_NAME
from .engines.registry import create_engine
from .services.audio_service import AudioService, list_input_devices
from .services.hotkey_service import HotkeyError, HotkeyService, is_wayland_session
from .services.language_service import resolve as resolve_language
from .services.logging_setup import setup_logging
from .services.notifications import notify
from .services.overlay_service import OverlayService
from .services.settings_service import Settings, SettingsService
from .services.text_injection_service import TextInjectionService
from .services.timing import Timer
from .services.transcript_buffer import TranscriptBuffer
from .services.transcript_export import (
    LaunchResult,
    open_transcript_in_app,
)
from .services.transcription_service import (
    TranscriptionResult,
    TranscriptionService,
)
from .services.vad_service import Segmenter, VadDetector
from .ui.main_window import MainWindow
from .ui.settings_dialog import SettingsDialog
from .ui.tray import TrayIndicator

log = logging.getLogger(__name__)


def _tilde_home(path) -> str:
    """Return ``path`` with ``$HOME`` replaced by ``~`` for display."""
    try:
        home = os.path.expanduser("~")
        s = str(path)
        if home and s.startswith(home):
            return "~" + s[len(home):]
        return s
    except Exception:
        return str(path)


class MyVoiceApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=0,  # Gio.ApplicationFlags.FLAGS_NONE
        )
        self._settings_service: Optional[SettingsService] = None
        self._settings: Optional[Settings] = None

        self._window: Optional[MainWindow] = None
        self._tray: Optional[TrayIndicator] = None
        self._overlay = OverlayService()

        self._engine = None
        self._audio = AudioService()
        self._segmenter: Optional[Segmenter] = None
        self._transcriber: Optional[TranscriptionService] = None
        self._injector: Optional[TextInjectionService] = None
        self._hotkey = HotkeyService()
        self._buffer = TranscriptBuffer()

        self._listening = False
        self._current_lang = "en"

        # Model preload / warm state.
        self._engine_ready = threading.Event()
        self._engine_load_lock = threading.Lock()
        self._engine_loading = False
        self._engine_load_error: Optional[str] = None

        # Per-dictation-session timer (created on Start; cleared on Stop).
        self._timer: Optional[Timer] = None

        # Guard against overlapping Start calls.
        self._starting = False

        # True from the moment stop_listening() begins until its
        # background teardown (audio close + transcriber drain) and
        # _after_finalize() complete. Blocks a racing start_listening()
        # from beginning a new session on top of one still tearing down.
        self._stopping = False

        # Shutdown coordination flag — set once, never cleared.
        self.shutting_down = False
        self._shutdown_started_at: Optional[float] = None

    # ------------------------------------------------------------------ lifecycle

    def do_activate(self) -> None:
        # Load stylesheet once
        try:
            css = Gtk.CssProvider()
            css_path = os.path.join(os.path.dirname(__file__), "ui", "styles.css")
            css.load_from_path(css_path)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except Exception as e:
            log.debug("CSS load failed: %s", e)

        if self._window is None:
            self._settings_service = SettingsService()
            self._settings = self._settings_service.settings
            self._current_lang = resolve_language(self._settings.language_mode)

            # Keep the autostart .desktop file in sync with settings on
            # every launch — not just when Settings is saved — so a fresh
            # install (autostart defaults to True) actually gets it written,
            # and it self-heals if the file is ever deleted externally.
            self._apply_autostart(self._settings.autostart)

            self._injector = TextInjectionService(
                own_pid=os.getpid(),
                extra_terminal_classes=list(self._settings.terminal_wm_classes),
            )

            self._window = MainWindow(
                application=self,
                on_toggle=self.toggle_listening,
                on_open_settings=self._open_settings,
                on_quit=self.quit_app,
                on_language_changed=self._on_language_changed,
                on_mic_changed=self._on_mic_changed,
                get_input_devices=list_input_devices,
                initial_language_mode=self._settings.language_mode,
                initial_mic=self._settings.microphone,
                on_copy_transcript=self._copy_transcript_to_clipboard,
                on_open_transcript_in_text_app=self._open_transcript_in_text_app,
            )
            self._window.connect("delete-event", self._on_close_hide_to_tray)

            self._tray = TrayIndicator(
                on_toggle_show=self._toggle_window,
                on_start=self.start_listening,
                on_stop=self.stop_listening,
                on_quit=self.quit_app,
            )

            self._register_hotkey_from_settings()

            # Wayland honesty banner
            if is_wayland_session():
                self._window.set_status(
                    "error",
                    "Wayland session detected. Global hotkey and cross-app "
                    "insertion may be limited. Use Cinnamon (X11) for full support."
                )
                notify(
                    "MyVoice on Wayland",
                    "Global hotkey and cross-app text insertion are limited on "
                    "Wayland. Log out and choose a Cinnamon (X11) session for "
                    "full functionality.",
                    urgency="normal",
                )

        if not self._settings.start_minimized:
            self._window.show_all()
            self._window.present()

        # Kick off background model preload once GTK is idle so we do not
        # block window paint / first frame. Never blocks the UI.
        GLib.idle_add(self._start_preload_after_paint)

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)

    # ------------------------------------------------------------------ own XIDs

    def _collect_own_xids(self) -> frozenset[int]:
        """Return the set of X11 XIDs belonging to our own windows.

        Called after windows are realized so GDK XIDs are available.
        """
        xids: set[int] = set()
        candidates = [self._window]
        # Also include the overlay window if it has been realized.
        if self._overlay is not None:
            try:
                ow = self._overlay._window  # type: ignore[attr-defined]
                if ow is not None:
                    candidates.append(ow)
            except Exception:
                pass
        for win in candidates:
            if win is None:
                continue
            try:
                gdk_win = win.get_window()
                if gdk_win is not None:
                    xid = gdk_win.get_xid()
                    if xid:
                        xids.add(int(xid))
            except Exception:
                pass
        return frozenset(xids)

    def _update_injector_own_xids(self) -> bool:
        """Refresh the injector's own-XIDs set after windows are shown."""
        if self._injector is not None:
            self._injector._own_xids = self._collect_own_xids()
            log.debug("injector own_xids updated: %s", self._injector._own_xids)
        return False  # remove from idle queue

    # ------------------------------------------------------------------ preload

    def _start_preload_after_paint(self) -> bool:
        """Schedule model preload slightly after activation to let GTK paint."""
        GLib.timeout_add(150, self._start_preload_now)
        # Collect own XIDs after paint when GDK windows are realized.
        GLib.idle_add(self._update_injector_own_xids)
        return False

    def _start_preload_now(self) -> bool:
        self._preload_model_async(reason="startup")
        return False

    def _preload_model_async(self, reason: str = "explicit") -> None:
        """Start (or restart) a background load of the currently-selected model.

        Never blocks the caller. Idempotent: if a load is already in flight,
        or the engine is already ready, this is a no-op.
        """
        if self._engine_ready.is_set():
            return
        with self._engine_load_lock:
            if self._engine_loading:
                return
            self._engine_loading = True

        assert self._settings is not None
        model = self._settings.model
        compute_type = self._settings.compute_type
        engine_key = self._settings.engine
        log.info("Preloading model: engine=%s model=%s compute=%s (reason=%s)",
                 engine_key, model, compute_type, reason)

        def _work():
            t0 = time.monotonic()
            err: Optional[str] = None
            try:
                if self._engine is None:
                    self._engine = create_engine(engine_key)
                self._engine.load(model, compute_type)
            except Exception as e:
                err = str(e)
                log.exception("Model preload failed: %s", e)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            with self._engine_load_lock:
                self._engine_loading = False
                self._engine_load_error = err
                if err is None:
                    self._engine_ready.set()
            GLib.idle_add(self._on_preload_done, err, elapsed_ms)

        threading.Thread(
            target=_work, name="myvoice-model-preload", daemon=True
        ).start()
        # Show a small non-blocking status. Only when idle to avoid clobbering
        # an active listening/processing state.
        if self._window is not None and not self._listening:
            self._set_status("processing", "Preparing local speech model…")

    def _on_preload_done(self, err: Optional[str], elapsed_ms: float) -> bool:
        if err:
            log.warning("Model preload done with error in %.0f ms: %s", elapsed_ms, err)
            if not self._listening:
                self._set_status("error", f"Model load failed: {err}")
        else:
            log.info("Model preload complete in %.0f ms", elapsed_ms)
            if not self._listening:
                self._set_status("ready", "Ready.")
        return False

    # ------------------------------------------------------------------ window/tray

    def _on_close_hide_to_tray(self, *_args) -> bool:
        # Hide instead of close; keep dictation + hotkey alive.
        if self._window is not None:
            self._window.hide()
        return True  # stop propagation

    def _toggle_window(self) -> None:
        if self._window is None:
            return
        if self._window.get_visible():
            self._window.hide()
        else:
            self._window.show_all()
            self._window.present()

    def request_quit(self) -> None:
        """Idempotent shutdown entry point. Safe to call from any thread."""
        if self.shutting_down:
            log.debug("request_quit: already shutting down, ignoring")
            return
        self.shutting_down = True
        self._shutdown_started_at = time.monotonic()
        log.info("Quit requested — state: listening=%s starting=%s",
                 self._listening, self._starting)
        # All UI updates must be on the GTK thread.
        GLib.idle_add(self._begin_shutdown_ui)

    def _begin_shutdown_ui(self) -> bool:
        """GTK thread: update UI then hand off to shutdown worker thread."""
        elapsed = time.monotonic() - self._shutdown_started_at
        log.info("Shutdown UI begin (%.3fs since request)", elapsed)
        if self._window is not None:
            try:
                self._window.set_status("processing", "Stopping MyVoice…")
            except Exception:
                pass
        if self._tray is not None:
            try:
                self._tray.set_listening(False)
            except Exception:
                pass
        threading.Thread(
            target=self._do_shutdown_work,
            name="myvoice-shutdown",
            daemon=True,
        ).start()
        return False  # remove from idle queue

    def _do_shutdown_work(self) -> None:
        """Worker thread: stop audio/transcription, then post finish to GTK."""
        # Phase 1: stop microphone
        try:
            self._audio.stop()
            elapsed = time.monotonic() - self._shutdown_started_at
            log.info("Shutdown: microphone stopped (%.3fs)", elapsed)
        except Exception:
            log.exception("Shutdown: microphone stop failed")

        # Phase 2: flush segmenter + drain transcriber
        try:
            if self._segmenter is not None and self._transcriber is not None:
                final = self._segmenter.flush()
                if final is not None:
                    self._transcriber.submit(final)
            if self._transcriber is not None:
                self._transcriber.request_drain_and_stop(
                    ready_wait=3.0, join_timeout=8.0,
                )
            elapsed = time.monotonic() - self._shutdown_started_at
            log.info("Shutdown: transcription drained (%.3fs)", elapsed)
        except Exception:
            log.exception("Shutdown: transcription drain failed")

        # Phase 3: finish injector session (may copy to clipboard)
        try:
            if self._injector is not None:
                had_target, buffered = self._injector.end_session()
                elapsed = time.monotonic() - self._shutdown_started_at
                log.info("Shutdown: injector session ended (%.3fs)", elapsed)
                if buffered:
                    GLib.idle_add(self._notify_transcript_on_clipboard, buffered)
        except Exception:
            log.exception("Shutdown: injector end_session failed")

        # Post GTK-thread finish
        GLib.idle_add(self._finish_quit)

    def _notify_transcript_on_clipboard(self, text: str) -> bool:
        """GTK thread: copy unsent transcript to clipboard and notify user."""
        try:
            if self._injector is not None:
                cb = self._injector._clipboard  # type: ignore[attr-defined]
                cb.write_user_text(text)
            notify("MyVoice", "Transcript copied to clipboard before closing.")
            log.info("Shutdown: unsent transcript copied to clipboard")
        except Exception:
            log.exception("Shutdown: clipboard copy on quit failed")
        return False

    def _finish_quit(self) -> bool:
        """GTK thread: release all UI/system resources, then call self.quit()."""
        elapsed = time.monotonic() - self._shutdown_started_at
        log.info("Shutdown: finish_quit begin (%.3fs)", elapsed)

        # Unregister hotkey
        try:
            self._hotkey.unregister()
            elapsed = time.monotonic() - self._shutdown_started_at
            log.info("Shutdown: hotkey unregistered (%.3fs)", elapsed)
        except Exception:
            log.exception("Shutdown: hotkey unregister failed")

        # Destroy overlay
        try:
            self._overlay.destroy()
            elapsed = time.monotonic() - self._shutdown_started_at
            log.info("Shutdown: overlay destroyed (%.3fs)", elapsed)
        except Exception:
            log.exception("Shutdown: overlay destroy failed")

        # Destroy tray
        try:
            if self._tray is not None:
                self._tray.destroy()
                elapsed = time.monotonic() - self._shutdown_started_at
                log.info("Shutdown: tray destroyed (%.3fs)", elapsed)
        except Exception:
            log.exception("Shutdown: tray destroy failed")

        # Quit GTK application
        elapsed = time.monotonic() - self._shutdown_started_at
        log.info("Shutdown: calling Gtk.Application.quit (%.3fs total)", elapsed)
        self.quit()
        return False

    # Keep old name as alias so any remaining internal callers still work.
    def quit_app(self) -> None:
        self.request_quit()

    # ------------------------------------------------------------------ settings

    def _open_settings(self) -> None:
        assert self._settings is not None
        dlg = SettingsDialog(
            self._window, self._settings, self._apply_settings, self._clear_model_cache,
            on_try_hotkey=self._try_rebind_hotkey,
        )
        response = dlg.run()
        if response == Gtk.ResponseType.OK:
            new = dlg.collect()
            if new is not None:
                # Preserve microphone setting from main window (not in dialog)
                new.microphone = self._settings.microphone
                self._apply_settings(new)
        dlg.destroy()

    def _try_rebind_hotkey(self, new_accel: str) -> Optional[str]:
        """Attempt to switch the global hotkey to ``new_accel`` immediately.

        Returns None on success, or a human error message. On failure the
        previous binding remains active (HotkeyService.rebind rolls back).
        """
        if is_wayland_session():
            return "Wayland session — global hotkey is not supported here."
        try:
            self._hotkey.rebind(new_accel, self._hotkey_toggle)
        except HotkeyError as e:
            return str(e)
        except ValueError as e:
            return f"Invalid: {e}"
        # Persist immediately so it survives a crash before Apply.
        if self._settings is not None and self._settings_service is not None:
            self._settings.hotkey = new_accel
            try:
                self._settings_service.save(self._settings)
            except Exception:
                log.exception("failed to persist new hotkey")
        log.info("Global hotkey rebound to %s", new_accel)
        return None

    def _apply_settings(self, new: Settings) -> None:
        assert self._settings_service is not None
        old = self._settings
        self._settings_service.save(new)
        self._settings = new
        self._current_lang = resolve_language(new.language_mode)

        # Update UI dropdowns silently
        if self._window is not None:
            self._window.set_language_mode(new.language_mode)

        # Hotkey rebind if changed.
        if not old or old.hotkey != new.hotkey:
            try:
                self._hotkey.rebind(new.hotkey, self._hotkey_toggle)
            except HotkeyError as e:
                log.warning("Applying new hotkey failed: %s", e)
                notify("MyVoice — hotkey", str(e), urgency="critical")

        # Autostart file
        self._apply_autostart(new.autostart)

        # If model or compute settings changed, invalidate current model and
        # start a fresh preload in the background. Do NOT block the UI.
        if old and (old.model != new.model or old.compute_type != new.compute_type):
            self._engine_ready.clear()
            if self._engine is not None:
                try:
                    self._engine.unload()
                except Exception:
                    log.exception("engine.unload() during settings apply failed")
            self._preload_model_async(reason="model_changed")

        # Refresh terminal class overrides on the injector.
        if self._injector is not None:
            self._injector._extra_terminal_classes = list(new.terminal_wm_classes)

        log.info("Settings applied.")

    def _apply_autostart(self, enable: bool) -> None:
        from .paths import autostart_dir
        target = autostart_dir() / "myvoice.desktop"
        if enable:
            content = self._autostart_desktop_contents()
            try:
                target.write_text(content, encoding="utf-8")
                target.chmod(0o644)
                log.info("Autostart enabled at %s", target)
            except Exception as e:
                log.warning("Failed to write autostart file: %s", e)
        else:
            try:
                if target.exists():
                    target.unlink()
                    log.info("Autostart disabled")
            except Exception as e:
                log.warning("Failed to remove autostart file: %s", e)

    def _autostart_desktop_contents(self) -> str:
        # There is no installed `myvoice` console script on PATH (install.sh
        # only installs dependencies into the project venv) — the only
        # reliably working launcher is the project's own run.sh, the same
        # one the applications-menu shortcut uses. Resolve it as an
        # absolute path from this module's location so it works regardless
        # of where the project is checked out.
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        run_sh = os.path.join(project_root, "run.sh")
        exec_line = run_sh if os.path.exists(run_sh) else "myvoice"
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=MyVoice\n"
            "Comment=Local voice dictation\n"
            f"Exec={exec_line}\n"
            "Icon=audio-input-microphone\n"
            "X-GNOME-Autostart-enabled=true\n"
            "Terminal=false\n"
        )

    def _clear_model_cache(self) -> None:
        from .models import model_manager
        try:
            model_manager.clear_cache()
            notify("MyVoice", "Model cache cleared.")
        except Exception as e:
            notify("MyVoice", f"Failed to clear model cache: {e}", urgency="critical")

    def _on_language_changed(self, mode: Optional[str]) -> None:
        if not mode:
            return
        assert self._settings is not None
        self._settings.language_mode = mode
        self._settings_service.save(self._settings)  # persist immediately
        self._current_lang = resolve_language(mode)
        if self._transcriber is not None:
            self._transcriber.set_language(self._current_lang)
        log.info("Language mode -> %s (resolved %s)", mode, self._current_lang)

    def _on_mic_changed(self, name: Optional[str]) -> None:
        assert self._settings is not None
        if self._settings.microphone == name:
            return
        self._settings.microphone = name
        self._settings_service.save(self._settings)
        # If we're recording, restart capture with new device
        if self._listening:
            log.info("Microphone changed mid-session; restarting capture")
            self._audio.stop()
            try:
                self._start_audio_capture()
            except Exception as e:
                self._error(f"Microphone change failed: {e}")

    # ------------------------------------------------------------------ hotkey

    def _register_hotkey_from_settings(self) -> None:
        assert self._settings is not None
        try:
            self._hotkey.register(self._settings.hotkey, self._hotkey_toggle)
            log.info("Hotkey active: %s", self._settings.hotkey)
        except HotkeyError as e:
            log.warning("Hotkey registration failed: %s", e)
            if self._window is not None:
                self._window.set_status("error", f"Hotkey error: {e}")
            notify("MyVoice — hotkey", str(e), urgency="critical")

    def _hotkey_toggle(self) -> None:
        # Runs on the GTK main thread (posted via GLib.idle_add).
        if self.shutting_down:
            return
        self.toggle_listening()

    # ------------------------------------------------------------------ dictation

    def toggle_listening(self) -> None:
        if self.shutting_down:
            return
        if self._listening:
            self.stop_listening()
        else:
            self.start_listening()

    def start_listening(self) -> None:
        """Begin dictation. Never blocks the GTK main thread.

        Start always succeeds — we do not require any field to be focused.
        Each transcribed chunk is routed at insertion time to whatever
        editable field is focused *at that moment*.
        """
        if self.shutting_down or self._listening or self._starting or self._stopping:
            return
        assert self._settings is not None and self._injector is not None

        self._starting = True
        try:
            self._timer = Timer("dictation")
            self._first_frame_logged = False
            self._first_segment_logged = False
            self._first_text_logged = False
            self._timer.mark("start_listening:received")

            # Begin injector session. Injector re-queries current focus per
            # chunk, so there is no hint/target to pass here.
            self._injector.begin_session()
            self._timer.mark("start_listening:session_began")

            # Reset transcript UI
            self._buffer.clear()
            if self._window is not None:
                self._window.clear_transcript()

            # Overlay + status IMMEDIATELY on the main thread.
            model_ready = self._engine_ready.is_set()
            if model_ready:
                self._set_status("listening", "Listening…")
            else:
                self._set_status("processing", "Preparing model… (recording)")
            self._overlay.show_listening()
            self._timer.mark("start_listening:overlay_shown",
                             f"model_ready={model_ready}")

            # Build segmenter + transcriber (transcriber queues while model
            # is loading).
            try:
                self._start_segmenter()
                self._start_transcriber()
                self._timer.mark("start_listening:pipeline_built")
            except Exception as e:
                self._error(f"Could not initialize dictation pipeline: {e}")
                return

            # Audio in a worker thread — PortAudio open is I/O.
            self._listening = True
            if self._window is not None:
                self._window.set_listening(True)
            if self._tray is not None:
                self._tray.set_listening(True)

            def _open_mic():
                if self._timer:
                    self._timer.mark("audio:open_call")
                try:
                    self._start_audio_capture()
                    if self._timer:
                        self._timer.mark("audio:open_return")
                except Exception as e:
                    log.exception("mic open failed")
                    GLib.idle_add(self._error, f"Could not start microphone: {e}")
                    return
                GLib.idle_add(self._on_audio_started)

            threading.Thread(target=_open_mic, name="myvoice-mic-open",
                             daemon=True).start()

            # If model is not yet ready, ensure a preload is in flight.
            if not model_ready:
                self._preload_model_async(reason="start_listening")
                threading.Thread(
                    target=self._wait_and_update_when_ready,
                    name="myvoice-ready-watcher", daemon=True,
                ).start()
        finally:
            self._starting = False

    def _on_audio_started(self) -> bool:
        if self.shutting_down:
            return False
        if self._timer:
            self._timer.mark("audio:started_ack")
        return False

    def _wait_and_update_when_ready(self) -> None:
        """Wait for engine ready and update status if still listening."""
        self._engine_ready.wait(timeout=120.0)
        if self.shutting_down:
            return
        if self._engine_ready.is_set() and self._listening:
            if self._timer:
                self._timer.mark("model:ready_during_session")
            GLib.idle_add(self._set_status, "listening", "Listening…")

    def stop_listening(self) -> None:
        """Stop dictation. Never blocks the GTK main thread."""
        if not self._listening:
            # Still clean up any partial injector session
            if self._injector is not None:
                try:
                    self._injector.end_session()
                except Exception:
                    pass
            return

        if self._timer:
            self._timer.mark("stop_listening:received")

        self._listening = False
        self._stopping = True
        if self._window is not None:
            self._window.set_listening(False)
        if self._tray is not None:
            self._tray.set_listening(False)
        self._set_status("processing", "Finalizing…")
        self._overlay.show_processing()
        if self._timer:
            self._timer.mark("stop_listening:ui_updated")

        transcriber = self._transcriber

        def _teardown():
            try:
                self._audio.stop()
                if self._timer:
                    self._timer.mark("audio:stopped")
            except Exception:
                log.exception("audio stop failed")
            try:
                if self._segmenter is not None:
                    final = self._segmenter.flush()
                    if final is not None and transcriber is not None:
                        transcriber.submit(final)
                        if self._timer:
                            self._timer.mark("segmenter:flushed")
                if transcriber is not None:
                    transcriber.request_drain_and_stop(
                        ready_wait=5.0, join_timeout=60.0,
                    )
                    if self._timer:
                        self._timer.mark("transcriber:drained")
            except Exception:
                log.exception("finalization failed")
            if not self.shutting_down:
                GLib.idle_add(self._after_finalize)

        threading.Thread(target=_teardown, name="myvoice-teardown",
                         daemon=True).start()

    def _after_finalize(self) -> bool:
        self._stopping = False
        if self.shutting_down:
            return False
        # Finalize injector session and pick the right end-of-session message.
        had_target = True
        buffered_text = ""
        if self._injector is not None:
            had_target, buffered_text = self._injector.end_session()

        if buffered_text:
            if had_target:
                msg = "Some dictated text missed its target; copied to clipboard."
            else:
                msg = "No editable field focused; transcript copied to clipboard."
            notify("MyVoice", msg)
            self._set_status("ready", msg)
        else:
            self._set_status("ready", "Ready.")

        self._overlay.hide()
        if self._timer:
            self._timer.mark("session:done")
            self._timer = None
        return False

    # ------------------------------------------------------------------ pipeline glue

    def _start_transcriber(self) -> None:
        """Create/start a transcription worker gated on the engine-ready event."""
        assert self._settings is not None
        if self._engine is None:
            self._engine = create_engine(self._settings.engine)
        self._transcriber = TranscriptionService(
            self._engine,
            on_result=self._on_transcript_result,
            ready_event=self._engine_ready,
        )
        self._transcriber.set_language(self._current_lang)
        self._transcriber.start()
        if self._timer and not self._engine_ready.is_set():
            self._timer.mark("transcriber:gated_on_model")

    def _start_segmenter(self) -> None:
        assert self._settings is not None
        vad = VadDetector(self._settings.vad.aggressiveness)
        self._segmenter = Segmenter(
            vad,
            min_speech_ms=self._settings.vad.min_speech_ms,
            silence_ms=self._settings.vad.silence_ms,
            max_segment_ms=self._settings.vad.max_segment_ms,
        )

    def _start_audio_capture(self) -> None:
        assert self._settings is not None
        device: Optional[int | str] = None
        if self._settings.microphone:
            device = self._settings.microphone
        self._audio.start(
            device=device,
            on_frame=self._on_audio_frame,
            on_rms=self._on_audio_rms,
        )

    def _on_audio_frame(self, frame: bytes) -> None:
        if self._timer is not None and not getattr(self, "_first_frame_logged", False):
            self._first_frame_logged = True
            self._timer.mark("audio:first_frame")
        seg = self._segmenter.push_frame(frame) if self._segmenter is not None else None
        if seg is not None and self._transcriber is not None:
            if self._timer is not None and not getattr(self, "_first_segment_logged", False):
                self._first_segment_logged = True
                self._timer.mark("segmenter:first_segment_submitted")
            self._transcriber.submit(seg)

    def _on_audio_rms(self, rms: float) -> None:
        if self.shutting_down:
            return
        GLib.idle_add(self._overlay.set_level, rms)

    def _on_transcript_result(self, res: TranscriptionResult) -> None:
        if self.shutting_down:
            return
        if not res.ok:
            GLib.idle_add(self._set_status, "error", f"Transcription error: {res.error}")
            return
        text = res.text
        if not text:
            return
        GLib.idle_add(self._handle_transcribed_text, text)

    def _handle_transcribed_text(self, text: str) -> bool:
        if self.shutting_down:
            return False
        # Main thread: buffer append, inject, and preview
        if self._timer is not None and not getattr(self, "_first_text_logged", False):
            self._first_text_logged = True
            self._timer.mark("inject:first_text_received")
        added = self._buffer.append(text)
        if not added:
            return False
        if self._window is not None:
            self._window.append_transcript(added)
            # As soon as we have any real transcript content, the
            # Copy / Open-in-text-app buttons become usable.
            self._window.set_transcript_actions_enabled(True)
        preview = self._buffer.text[-60:]
        self._overlay.set_preview(preview)

        if self._injector is not None:
            result = self._injector.inject(added)
            if not result.ok and result.backend == "refused":
                self._set_status("error", "Password field — text not inserted.")
            elif not result.ok and result.backend == "buffer":
                # Silent buffering; final status shown on stop.
                pass
        return False

    # ------------------------------------------------------------------ helpers

    def _set_status(self, key: str, msg: str) -> bool:
        if self.shutting_down:
            return False
        if self._window is not None:
            self._window.set_status(key, msg)
        return False  # for idle_add compatibility

    # ------------------------------------------------------------------ transcript actions

    def _copy_transcript_to_clipboard(self) -> None:
        """Handler for the Copy transcript button in the main window.

        Copies the *entire* current session transcript (not the truncated
        preview) to the system clipboard. Marks the clipboard as
        user-overridden so the injection pipeline's end-of-session
        restoration cannot silently overwrite it later.
        """
        text = self._buffer.text if self._buffer is not None else ""
        if not text.strip():
            self._set_status("idle", "No transcript to copy yet.")
            return

        assert self._injector is not None
        # Use the same safe clipboard abstraction the injector uses.
        cb = self._injector._clipboard  # type: ignore[attr-defined]
        try:
            ok = cb.write_user_text(text)
        except Exception:
            log.exception("Copy transcript to clipboard failed")
            ok = False

        if ok:
            self._set_status("ready", "Transcript copied to clipboard.")
        else:
            self._set_status("error", "Could not copy transcript to clipboard.")

    def _open_transcript_in_text_app(self) -> None:
        """Handler for the Open in Text App button in the main window.

        Writes a UTF-8 ``.txt`` snapshot of the current session transcript
        to a throwaway temp file and opens it in the user's configured
        text application (or the system default). This is a scratch copy
        for viewing/editing, not a permanent save — nothing is written to
        ``~/Documents/MyVoice Transcripts/`` unless the user explicitly
        saves it themselves from the editor.

        A snapshot is exactly that — a snapshot. If dictation continues
        after the file is opened, we do *not* append to that file. The
        next click writes a new timestamped file.
        """
        assert self._settings is not None
        text = self._buffer.text if self._buffer is not None else ""
        if not text.strip():
            self._set_status("idle", "No transcript to open yet.")
            return

        desktop_id = self._settings.text_app_desktop_id or ""
        try:
            result: LaunchResult = open_transcript_in_app(text, desktop_id)
        except Exception as e:
            log.exception("open_transcript_in_app failed")
            self._set_status("error", f"Could not save transcript: {e}")
            notify("MyVoice", f"Could not save transcript: {e}",
                   urgency="critical")
            return

        # Present the saved file path with a leading ~ where possible so
        # the status line is short and human-readable.
        display_path = _tilde_home(result.path)
        if result.launched:
            if result.used_fallback and result.fallback_reason:
                self._set_status(
                    "ready",
                    f"{result.fallback_reason}. Opened {display_path} "
                    f"with system default text editor.",
                )
                notify(
                    "MyVoice — transcript",
                    result.fallback_reason
                    + "; opened with the system default text editor.",
                )
            else:
                self._set_status(
                    "ready", f"Transcript opened: {display_path}",
                )
        else:
            # File was saved but nothing launched. Try to reveal the
            # folder in the user's file manager as a last resort.
            self._reveal_transcript_folder(result.path)
            reason = result.error or "No text editor available"
            self._set_status(
                "error",
                f"{reason}. Transcript saved to {display_path}",
            )
            notify(
                "MyVoice — transcript",
                f"{reason}. Saved to {display_path}",
                urgency="normal",
            )

    def _reveal_transcript_folder(self, path) -> None:
        """Try to open the transcripts folder in the file manager.

        Best-effort. Uses ``Gio.AppInfo.launch_default_for_uri`` on the
        containing directory. Never raises.
        """
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio  # type: ignore
            uri = Gio.File.new_for_path(str(path.parent)).get_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception:
            log.debug("Could not reveal transcripts folder", exc_info=True)

    def _error(self, msg: str) -> bool:
        log.error(msg)
        self._listening = False
        if self._window is not None:
            self._window.set_status("error", msg)
            self._window.set_listening(False)
        try:
            self._overlay.hide()
        except Exception:
            pass
        notify("MyVoice", msg, urgency="critical")
        return False


# ---------------------------------------------------------------------------- entry


def _set_process_title() -> None:
    """Give the process a recognizable name in ps/top/System Monitor
    instead of the bare interpreter name ("python3.x"), so MyVoice is
    identifiable and not mistaken for a stray Python process.
    """
    try:
        import setproctitle
        setproctitle.setproctitle("myvoice")
    except Exception:
        log.debug("setproctitle unavailable; process will show as python")


def main() -> int:
    _set_process_title()
    parser = argparse.ArgumentParser(prog="myvoice", description="Local voice dictation")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--start-listening", action="store_true",
                        help="Start dictation immediately on launch")
    args, extra = parser.parse_known_args()

    setup_logging(args.log_level)
    log.info("Starting %s", APP_NAME)

    app = MyVoiceApp()

    # Graceful signal handling
    def _sig(_signum, _frame):
        log.info("Signal received; quitting")
        GLib.idle_add(app.quit_app)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    if args.start_listening:
        GLib.idle_add(app.start_listening)

    return app.run(sys.argv[:1] + extra)


if __name__ == "__main__":
    sys.exit(main())
