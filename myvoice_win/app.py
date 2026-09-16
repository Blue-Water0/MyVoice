"""MyVoice application entrypoint (Windows build / PySide6-Qt).

Windows-native equivalent of ``myvoice/app.py`` (the GTK original). Wires
all Task 1-11 services and the UI together and runs on the Qt event loop.

This module is *integration* work: it reproduces, method-for-method, the
lifecycle and threading contract of the Linux ``MyVoiceApp`` with the Qt /
Win32 pieces built in the earlier tasks. Where the Linux file uses GLib
primitives, this uses the Qt equivalents:

* ``GLib.idle_add(fn, *args)`` (marshal a call onto the UI thread from a
  background thread) -> ``self._post(fn, *args)``, which emits a Qt
  ``Signal(object)`` connected to a main-thread slot. Qt delivers a signal
  emitted from a thread other than the receiver's via a queued connection
  automatically, so ``_post`` defers the call onto the Qt main thread the
  same way ``idle_add`` defers onto the GTK main thread.
* ``GLib.timeout_add(ms, fn)`` / deferred main-thread scheduling ->
  ``QtCore.QTimer.singleShot(ms, fn)``.

Threading contract (identical shape to the Linux original):
* All Qt widget / clipboard / SendInput-adjacent UI operations happen on
  the Qt main thread.
* Audio capture, VAD, transcription, and model-preload run on background
  threads.
* Every background -> UI hand-off goes through ``self._post`` (or a
  ``QTimer`` for pure main-thread deferral).

Close-to-tray ownership: ``MainWindow`` deliberately does not override
``closeEvent`` (see its module docstring). This controller installs itself
as an event filter on the window and intercepts ``QEvent.Type.Close`` to
hide-instead-of-quit, mirroring the Linux app connecting to ``delete-event``
externally.

Deliberate omissions vs. the Linux original:
* ``_set_process_title`` (Linux ``setproctitle``) -- no worthwhile Windows
  equivalent; intentionally dropped per the Task 12 brief.
* The Wayland honesty banner / ``is_wayland_session`` checks -- a
  Linux-display-server concept with no Windows analogue; dropped.
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

from PySide6 import QtCore, QtWidgets

from . import APP_NAME
from .engines.registry import create_engine
from .services.hotkey_service import HotkeyError, HotkeyService
from .services.logging_setup import setup_logging
from .services.notifications import notify
from .services.overlay_service import OverlayService
from .services.settings_service import Settings, SettingsService
from .services.text_injection_service import REJECT_PASSWORD, TextInjectionService
from .services.transcript_export import (
    LaunchResult,
    open_transcript_in_app,
    reveal_transcript_folder,
)
from .ui.main_window import MainWindow
from .ui.settings_dialog import SettingsDialog
from .ui.tray import TrayIndicator

# Reused directly from the Linux app (safe-reuse list -- pure logic, no
# Linux-only imports at module load).
from myvoice.services.audio_service import AudioService, list_input_devices
from myvoice.services.language_service import resolve as resolve_language
from myvoice.services.timing import Timer
from myvoice.services.transcript_buffer import TranscriptBuffer
from myvoice.services.transcription_service import (
    TranscriptionResult,
    TranscriptionService,
)
from myvoice.services.vad_service import Segmenter, VadDetector

log = logging.getLogger(__name__)

# Registry location for the per-user autostart entry -- the Windows
# equivalent of the Linux ``~/.config/autostart/myvoice.desktop`` file.
_AUTOSTART_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_VALUE_NAME = "MyVoice"


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


class MyVoiceApp(QtCore.QObject):
    """Owns and wires every service + the UI. One instance per process.

    Implemented as a plain ``QObject`` controller that *uses* a
    ``QApplication`` (rather than subclassing it) so the object can be
    constructed and unit-tested against the shared session ``QApplication``
    without minting a second app instance, and so it can serve as the
    window's close-event filter.
    """

    # Cross-thread marshaling primitive: emit a callable, run it on the
    # thread this QObject lives on (the Qt main thread). This is the Qt
    # equivalent of ``GLib.idle_add``.
    _post_signal = QtCore.Signal(object)

    def __init__(self, app: Optional[QtWidgets.QApplication] = None) -> None:
        super().__init__()
        self._app = app or QtWidgets.QApplication.instance()

        self._post_signal.connect(self._run_posted)

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

        # True from the moment stop_listening() begins until its background
        # teardown and _after_finalize() complete. Blocks a racing
        # start_listening() from beginning a new session on top of one still
        # tearing down.
        self._stopping = False

        # Shutdown coordination flag -- set once, never cleared.
        self.shutting_down = False
        self._shutdown_started_at: Optional[float] = None

    # ------------------------------------------------------------- marshaling

    @QtCore.Slot(object)
    def _run_posted(self, fn) -> None:
        """Main-thread slot: run a callable posted from any thread."""
        try:
            fn()
        except Exception:
            log.exception("posted callable raised")

    def _post(self, fn, *args, **kwargs) -> None:
        """Marshal ``fn(*args, **kwargs)`` onto the Qt main thread.

        The Qt equivalent of ``GLib.idle_add`` -- safe to call from any
        thread. When called from the main thread the connection runs
        directly; from a background thread Qt queues it onto the main
        thread's event loop.
        """
        self._post_signal.emit(lambda: fn(*args, **kwargs))

    # -------------------------------------------------------------- lifecycle

    def activate(self) -> None:
        """Build the UI + services and show the window. (Linux ``do_activate``.)"""
        if self._window is not None:
            # Already activated -- just re-present the window.
            self._present_window()
            return

        self._settings_service = SettingsService()
        self._settings = self._settings_service.settings
        self._current_lang = resolve_language(self._settings.language_mode)

        # Keep the autostart registry entry in sync with settings on every
        # launch (not only when Settings is saved) so a fresh install with
        # autostart enabled actually gets registered, and it self-heals if
        # the entry is ever removed externally.
        self._apply_autostart(self._settings.autostart)

        self._injector = TextInjectionService(
            own_pid=os.getpid(),
            extra_terminal_classes=list(self._settings.terminal_wm_classes),
        )

        self._window = MainWindow(
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
        # Close-to-tray: intercept the window's Close event from outside the
        # widget (mirrors the Linux external ``delete-event`` connection).
        self._window.installEventFilter(self)

        self._tray = TrayIndicator(
            on_toggle_show=self._toggle_window,
            on_start=self.start_listening,
            on_stop=self.stop_listening,
            on_quit=self.quit_app,
        )

        # Deliver WM_HOTKEY messages to the HotkeyService via the app's
        # native event filter.
        if self._app is not None:
            self._app.installNativeEventFilter(self._hotkey.event_filter)

        self._register_hotkey_from_settings()

        if not self._settings.start_minimized:
            self._present_window()

        # Kick off background model preload once Qt is idle so we do not
        # block the first window paint. Never blocks the UI.
        QtCore.QTimer.singleShot(0, self._start_preload_after_paint)

    def _present_window(self) -> None:
        if self._window is None:
            return
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    # ------------------------------------------------------------- close filter

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        """Hide-to-tray on window close (Linux ``_on_close_hide_to_tray``)."""
        if obj is self._window and event.type() == QtCore.QEvent.Type.Close:
            if not self.shutting_down:
                self._window.hide()
                event.ignore()
                return True  # stop propagation -- keep dictation + hotkey alive
        return super().eventFilter(obj, event)

    # --------------------------------------------------------------- own HWNDs

    def _collect_own_hwnds(self) -> frozenset[int]:
        """Return the set of native window handles belonging to our windows.

        Windows equivalent of the Linux ``_collect_own_xids`` -- on Windows
        ``QWidget.winId()`` returns the ``HWND``. Called after windows are
        shown so the native handles exist.
        """
        hwnds: set[int] = set()
        candidates = [self._window]
        if self._overlay is not None:
            ow = getattr(self._overlay, "_window", None)
            if ow is not None:
                candidates.append(ow)
        for win in candidates:
            if win is None:
                continue
            try:
                wid = int(win.winId())
                if wid:
                    hwnds.add(wid)
            except Exception:
                pass
        return frozenset(hwnds)

    def _update_injector_own_hwnds(self) -> None:
        """Refresh the injector's own-HWNDs set after windows are shown."""
        if self._injector is not None:
            self._injector._own_hwnds = self._collect_own_hwnds()
            log.debug("injector own_hwnds updated: %s", self._injector._own_hwnds)

    # ----------------------------------------------------------------- preload

    def _start_preload_after_paint(self) -> None:
        """Schedule model preload slightly after activation to let Qt paint."""
        QtCore.QTimer.singleShot(150, self._start_preload_now)
        # Collect own HWNDs after paint when the native windows exist.
        QtCore.QTimer.singleShot(0, self._update_injector_own_hwnds)

    def _start_preload_now(self) -> None:
        self._preload_model_async(reason="startup")

    def _preload_model_async(self, reason: str = "explicit") -> None:
        """Start (or restart) a background load of the selected model.

        Never blocks the caller. Idempotent: a no-op if a load is already
        in flight or the engine is already ready.
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
            self._post(self._on_preload_done, err, elapsed_ms)

        threading.Thread(
            target=_work, name="myvoice-model-preload", daemon=True
        ).start()
        # Non-blocking status -- only when idle, so we do not clobber an
        # active listening/processing state.
        if self._window is not None and not self._listening:
            self._set_status("processing", "Preparing local speech model...")

    def _on_preload_done(self, err: Optional[str], elapsed_ms: float) -> None:
        if err:
            log.warning("Model preload done with error in %.0f ms: %s", elapsed_ms, err)
            if not self._listening:
                self._set_status("error", f"Model load failed: {err}")
        else:
            log.info("Model preload complete in %.0f ms", elapsed_ms)
            if not self._listening:
                self._set_status("ready", "Ready.")

    # ------------------------------------------------------------- window/tray

    def _toggle_window(self) -> None:
        if self._window is None:
            return
        if self._window.isVisible():
            self._window.hide()
        else:
            self._present_window()

    # ----------------------------------------------------------------- quit

    def request_quit(self) -> None:
        """Idempotent shutdown entry point. Safe to call from any thread."""
        if self.shutting_down:
            log.debug("request_quit: already shutting down, ignoring")
            return
        self.shutting_down = True
        self._shutdown_started_at = time.monotonic()
        log.info("Quit requested -- state: listening=%s starting=%s",
                 self._listening, self._starting)
        # All UI updates must be on the Qt main thread.
        self._post(self._begin_shutdown_ui)

    def _begin_shutdown_ui(self) -> None:
        """Main thread: update UI then hand off to shutdown worker thread."""
        elapsed = time.monotonic() - self._shutdown_started_at
        log.info("Shutdown UI begin (%.3fs since request)", elapsed)
        if self._window is not None:
            try:
                self._window.set_status("processing", "Stopping MyVoice...")
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

    def _do_shutdown_work(self) -> None:
        """Worker thread: stop audio/transcription, then post finish to UI."""
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
                    self._post(self._notify_transcript_on_clipboard, buffered)
        except Exception:
            log.exception("Shutdown: injector end_session failed")

        # Post UI-thread finish
        self._post(self._finish_quit)

    def _notify_transcript_on_clipboard(self, text: str) -> None:
        """Main thread: copy unsent transcript to clipboard and notify user."""
        try:
            if self._injector is not None:
                cb = self._injector._clipboard  # type: ignore[attr-defined]
                cb.write_user_text(text)
            notify("MyVoice", "Transcript copied to clipboard before closing.")
            log.info("Shutdown: unsent transcript copied to clipboard")
        except Exception:
            log.exception("Shutdown: clipboard copy on quit failed")

    def _finish_quit(self) -> None:
        """Main thread: release all UI/system resources, then quit the app."""
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

        # Quit the Qt application.
        elapsed = time.monotonic() - self._shutdown_started_at
        log.info("Shutdown: calling QApplication.quit (%.3fs total)", elapsed)
        if self._app is not None:
            self._app.quit()

    # Keep old name as alias so any remaining internal callers still work.
    def quit_app(self) -> None:
        self.request_quit()

    # ------------------------------------------------------------------ settings

    def _open_settings(self) -> None:
        assert self._settings is not None
        dlg = SettingsDialog(
            self._window, self._settings, self._apply_settings,
            self._clear_model_cache, on_try_hotkey=self._try_rebind_hotkey,
        )
        result = dlg.exec()
        if result == QtWidgets.QDialog.DialogCode.Accepted:
            new = dlg.collect()
            if new is not None:
                # Preserve microphone setting from main window (not in dialog)
                new.microphone = self._settings.microphone
                self._apply_settings(new)
        dlg.deleteLater()

    def _try_rebind_hotkey(self, new_accel: str) -> Optional[str]:
        """Attempt to switch the global hotkey to ``new_accel`` immediately.

        Returns None on success, or a human error message. On failure the
        previous binding remains active (HotkeyService.rebind rolls back).
        """
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
                notify("MyVoice -- hotkey", str(e), urgency="critical")

        # Autostart entry
        self._apply_autostart(new.autostart)

        # If model or compute settings changed, invalidate the current model
        # and start a fresh preload in the background. Never blocks the UI.
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
        """Enable/disable launch-on-login via the HKCU Run registry key.

        Windows equivalent of the Linux ``_apply_autostart`` (which writes/
        removes ``~/.config/autostart/myvoice.desktop``). Imported lazily:
        ``winreg`` is a Windows-only stdlib module and does not exist on the
        Linux dev/test sandbox.
        """
        try:
            import winreg  # Windows-only stdlib; absent on Linux.
        except ImportError:
            log.warning("winreg unavailable; cannot manage autostart entry")
            return

        if enable:
            command = self._autostart_command()
            try:
                key = winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY_PATH
                )
                try:
                    winreg.SetValueEx(
                        key, _AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, command
                    )
                finally:
                    winreg.CloseKey(key)
                log.info("Autostart enabled: %s -> %s",
                         _AUTOSTART_VALUE_NAME, command)
            except OSError as e:
                log.warning("Failed to write autostart registry entry: %s", e)
        else:
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY_PATH, 0,
                    winreg.KEY_SET_VALUE,
                )
                try:
                    winreg.DeleteValue(key, _AUTOSTART_VALUE_NAME)
                    log.info("Autostart disabled")
                finally:
                    winreg.CloseKey(key)
            except FileNotFoundError:
                # Key or value already absent -- nothing to remove.
                pass
            except OSError as e:
                log.warning("Failed to remove autostart registry entry: %s", e)

    def _autostart_command(self) -> str:
        """Return the command string to store under the Run key.

        When packaged with PyInstaller, ``sys.executable`` *is*
        ``MyVoice.exe`` -- the real installed launcher. In a source/dev
        checkout there is no built exe, so we derive the placeholder path
        where the installer would place it (``<project>/dist/MyVoice/
        MyVoice.exe``), mirroring the way the Linux version derives
        ``run.sh``'s absolute path from the project root. The stored value
        is quoted so a path containing spaces is handled by the shell.
        """
        if getattr(sys, "frozen", False):
            exe = sys.executable
        else:
            project_root = os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )
            exe = os.path.join(project_root, "dist", "MyVoice", "MyVoice.exe")
        return f'"{exe}"'

    def _clear_model_cache(self) -> None:
        from .models import model_manager
        try:
            model_manager.clear_cache()
            notify("MyVoice", "Model cache cleared.")
        except Exception as e:
            notify("MyVoice", f"Failed to clear model cache: {e}",
                   urgency="critical")

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
        # If we're recording, restart capture with the new device.
        if self._listening:
            log.info("Microphone changed mid-session; restarting capture")
            self._audio.stop()
            try:
                self._start_audio_capture()
            except Exception as e:
                self._error(f"Microphone change failed: {e}")

    # -------------------------------------------------------------------- hotkey

    def _register_hotkey_from_settings(self) -> None:
        assert self._settings is not None
        try:
            self._hotkey.register(self._settings.hotkey, self._hotkey_toggle)
            log.info("Hotkey active: %s", self._settings.hotkey)
        except HotkeyError as e:
            log.warning("Hotkey registration failed: %s", e)
            if self._window is not None:
                self._window.set_status("error", f"Hotkey error: {e}")
            notify("MyVoice -- hotkey", str(e), urgency="critical")

    def _hotkey_toggle(self) -> None:
        # Runs on the Qt main thread (delivered by the native event filter,
        # which runs inside the Qt event loop).
        if self.shutting_down:
            return
        self.toggle_listening()

    # ----------------------------------------------------------------- dictation

    def toggle_listening(self) -> None:
        if self.shutting_down:
            return
        if self._listening:
            self.stop_listening()
        else:
            self.start_listening()

    def start_listening(self) -> None:
        """Begin dictation. Never blocks the Qt main thread.

        Start always succeeds -- we do not require any field to be focused.
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

            # Begin injector session. The injector re-queries current focus
            # per chunk, so there is no hint/target to pass here.
            self._injector.begin_session()
            self._timer.mark("start_listening:session_began")

            # Reset transcript UI
            self._buffer.clear()
            if self._window is not None:
                self._window.clear_transcript()

            # Overlay + status IMMEDIATELY on the main thread.
            model_ready = self._engine_ready.is_set()
            if model_ready:
                self._set_status("listening", "Listening...")
            else:
                self._set_status("processing", "Preparing model... (recording)")
            self._overlay.show_listening()
            self._timer.mark("start_listening:overlay_shown",
                             f"model_ready={model_ready}")

            # Build segmenter + transcriber (transcriber queues while the
            # model is loading).
            try:
                self._start_segmenter()
                self._start_transcriber()
                self._timer.mark("start_listening:pipeline_built")
            except Exception as e:
                self._error(f"Could not initialize dictation pipeline: {e}")
                return

            # Audio in a worker thread -- opening PortAudio is I/O.
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
                    self._post(self._error, f"Could not start microphone: {e}")
                    return
                self._post(self._on_audio_started)

            threading.Thread(target=_open_mic, name="myvoice-mic-open",
                             daemon=True).start()

            # If the model is not yet ready, ensure a preload is in flight.
            if not model_ready:
                self._preload_model_async(reason="start_listening")
                threading.Thread(
                    target=self._wait_and_update_when_ready,
                    name="myvoice-ready-watcher", daemon=True,
                ).start()
        finally:
            self._starting = False

    def _on_audio_started(self) -> None:
        if self.shutting_down:
            return
        if self._timer:
            self._timer.mark("audio:started_ack")

    def _wait_and_update_when_ready(self) -> None:
        """Wait for engine ready and update status if still listening."""
        self._engine_ready.wait(timeout=120.0)
        if self.shutting_down:
            return
        if self._engine_ready.is_set() and self._listening:
            if self._timer:
                self._timer.mark("model:ready_during_session")
            self._post(self._set_status, "listening", "Listening...")

    def stop_listening(self) -> None:
        """Stop dictation. Never blocks the Qt main thread."""
        if not self._listening:
            # Still clean up any partial injector session.
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
        self._set_status("processing", "Finalizing...")
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
                self._post(self._after_finalize)

        threading.Thread(target=_teardown, name="myvoice-teardown",
                         daemon=True).start()

    def _after_finalize(self) -> None:
        self._stopping = False
        if self.shutting_down:
            return
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

    # ------------------------------------------------------------- pipeline glue

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
        # Runs on the audio callback thread. No UI touch here -- push to the
        # segmenter and submit finished segments to the transcriber queue.
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
        self._post(self._overlay.set_level, rms)

    def _on_transcript_result(self, res: TranscriptionResult) -> None:
        # Runs on the transcription worker thread.
        if self.shutting_down:
            return
        if not res.ok:
            self._post(self._set_status, "error",
                       f"Transcription error: {res.error}")
            return
        text = res.text
        if not text:
            return
        self._post(self._handle_transcribed_text, text)

    def _handle_transcribed_text(self, text: str) -> None:
        # Main thread: buffer append, inject, and preview.
        if self.shutting_down:
            return
        if self._timer is not None and not getattr(self, "_first_text_logged", False):
            self._first_text_logged = True
            self._timer.mark("inject:first_text_received")
        added = self._buffer.append(text)
        if not added:
            return
        if self._window is not None:
            self._window.append_transcript(added)
            # As soon as we have any real transcript content, the Copy /
            # Open-in-text-app buttons become usable.
            self._window.set_transcript_actions_enabled(True)
        preview = self._buffer.text[-60:]
        self._overlay.set_preview(preview)

        if self._injector is not None:
            result = self._injector.inject(added)
            # Interface drift vs. Linux: the Linux injector signalled a
            # password-field rejection with ``backend == "refused"``; the
            # Windows ``TextInjectionService`` instead *buffers* the chunk
            # (``backend == "buffer"``) and records the reason in
            # ``detail``. Key off the shared REJECT_PASSWORD constant (not
            # a bare literal) so the same user-facing status the Linux app
            # showed is preserved and can't silently drift out of sync with
            # a future reword of that reason string.
            if not result.ok and result.detail == REJECT_PASSWORD:
                self._set_status("error", "Password field -- text not inserted.")
            elif not result.ok and result.backend == "buffer":
                # Silent buffering; final status shown on stop.
                pass

    # --------------------------------------------------------------------- helpers

    def _set_status(self, key: str, msg: str) -> None:
        if self.shutting_down:
            return
        if self._window is not None:
            self._window.set_status(key, msg)

    # ------------------------------------------------------------ transcript actions

    def _copy_transcript_to_clipboard(self) -> None:
        """Handler for the Copy transcript button in the main window.

        Copies the *entire* current session transcript (not the truncated
        preview) to the system clipboard, marking it user-overridden so the
        injection pipeline's end-of-session restoration cannot silently
        overwrite it later.
        """
        text = self._buffer.text if self._buffer is not None else ""
        if not text.strip():
            self._set_status("idle", "No transcript to copy yet.")
            return

        assert self._injector is not None
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
        to a throwaway temp file and opens it in the OS default ``.txt``
        handler. This is a scratch copy for viewing/editing, not a
        permanent save.
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

        display_path = _tilde_home(result.path)
        if result.launched:
            if result.used_fallback and result.fallback_reason:
                self._set_status(
                    "ready",
                    f"{result.fallback_reason}. Opened {display_path} "
                    f"with the system default text editor.",
                )
                notify(
                    "MyVoice -- transcript",
                    result.fallback_reason
                    + "; opened with the system default text editor.",
                )
            else:
                self._set_status(
                    "ready", f"Transcript opened: {display_path}",
                )
        else:
            # File was saved but nothing launched. Reveal the folder in File
            # Explorer as a last resort.
            self._reveal_transcript_folder(result.path)
            reason = result.error or "No text editor available"
            self._set_status(
                "error",
                f"{reason}. Transcript saved to {display_path}",
            )
            notify(
                "MyVoice -- transcript",
                f"{reason}. Saved to {display_path}",
                urgency="normal",
            )

    def _reveal_transcript_folder(self, path) -> None:
        """Best-effort: open the transcripts folder in File Explorer.

        Delegates to ``transcript_export.reveal_transcript_folder`` (which
        never raises), mirroring the role of the Linux app's
        ``_reveal_transcript_folder``.
        """
        try:
            reveal_transcript_folder(path)
        except Exception:
            log.debug("Could not reveal transcripts folder", exc_info=True)

    def _error(self, msg: str) -> None:
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


# ---------------------------------------------------------------------------- entry


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="myvoice", description="Local voice dictation")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--start-listening", action="store_true",
                        help="Start dictation immediately on launch")
    args, extra = parser.parse_known_args()

    setup_logging(args.log_level)
    log.info("Starting %s", APP_NAME)

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv[:1] + extra)
    app.setApplicationName(APP_NAME)
    # Closing the main window hides it to tray -- do not quit when the last
    # visible window is hidden/closed. Quit only happens via request_quit().
    app.setQuitOnLastWindowClosed(False)

    controller = MyVoiceApp(app)

    # Graceful signal handling.
    def _sig(_signum, _frame):
        log.info("Signal received; quitting")
        controller._post(controller.quit_app)

    signal.signal(signal.SIGINT, _sig)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError, OSError):
        # SIGTERM may not be settable on every platform/thread.
        pass

    controller.activate()

    if args.start_listening:
        QtCore.QTimer.singleShot(0, controller.start_listening)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
