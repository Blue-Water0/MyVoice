"""System tray indicator.

Tries AyatanaAppIndicator3 → AppIndicator3 → Gtk.StatusIcon (deprecated but
still works on Cinnamon). All three are commonly available on Linux Mint.

The visible tooltip / hover text is set explicitly to ``APP_NAME`` via each
backend's title API. Without an explicit ``set_title`` call, Ayatana /
AppIndicator3 report the running process's argv[0] as the D-Bus
StatusNotifierItem ``Title`` property, which on Cinnamon shows up as
``app.py`` when the app is launched with ``python -m myvoice.app``. That is
what we're fixing here.
"""
from __future__ import annotations

import logging
from typing import Callable

from .. import APP_NAME

log = logging.getLogger(__name__)


# Stable identifier used as the AppIndicator ID and as the process-side
# handle for the tray icon. This is NOT the visible tooltip — that is set
# via set_title(APP_NAME).
TRAY_ID = "myvoice"


class TrayIndicator:
    def __init__(
        self,
        on_toggle_show: Callable[[], None],
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._backend: str | None = None
        self._indicator = None
        self._status_icon = None
        self._menu = None
        self._callbacks = {
            "toggle": on_toggle_show,
            "start": on_start,
            "stop": on_stop,
            "quit": on_quit,
        }
        self._build()

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _apply_title(indicator, title: str) -> None:
        """Best-effort: set the indicator's title (visible tooltip) if the
        installed binding supports it. Ayatana >=0.5 and libappindicator
        >=0.4.90 both expose ``set_title``; older bindings won't.
        """
        setter = getattr(indicator, "set_title", None)
        if setter is None:
            log.debug("Indicator binding has no set_title(); tooltip may "
                      "fall back to the process argv[0].")
            return
        try:
            setter(title)
        except Exception:
            log.exception("indicator.set_title(%r) failed", title)

    def _build(self) -> None:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        menu = Gtk.Menu()

        def add(label, key):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda *_: self._callbacks[key]())
            menu.append(item)

        # User-visible menu labels. All reference APP_NAME so a future
        # display-name change happens in exactly one place.
        add(f"Show {APP_NAME}", "toggle")
        menu.append(Gtk.SeparatorMenuItem())
        add("Start Listening", "start")
        add("Stop Listening", "stop")
        menu.append(Gtk.SeparatorMenuItem())
        add(f"Quit {APP_NAME}", "quit")
        menu.show_all()
        self._menu = menu

        # 1) Ayatana
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # type: ignore
            ind = AppIndicator3.Indicator.new(
                TRAY_ID, "audio-input-microphone",
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            # Visible hover text — this is the actual fix for the "app.py"
            # tooltip. Must be called before or after set_status, both work.
            self._apply_title(ind, APP_NAME)
            ind.set_menu(menu)
            self._indicator = ind
            self._backend = "ayatana"
            log.info("Tray: Ayatana AppIndicator3 (title=%s)", APP_NAME)
            return
        except Exception as e:
            log.debug("Ayatana AppIndicator3 not available: %s", e)

        # 2) Legacy AppIndicator3
        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3  # type: ignore
            ind = AppIndicator3.Indicator.new(
                TRAY_ID, "audio-input-microphone",
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self._apply_title(ind, APP_NAME)
            ind.set_menu(menu)
            self._indicator = ind
            self._backend = "appindicator3"
            log.info("Tray: AppIndicator3 (title=%s)", APP_NAME)
            return
        except Exception as e:
            log.debug("AppIndicator3 not available: %s", e)

        # 3) StatusIcon fallback (Cinnamon still supports this).
        try:
            icon = Gtk.StatusIcon.new_from_icon_name("audio-input-microphone")
            icon.set_visible(True)
            # StatusIcon has both a title (accessibility / SNI) and a tooltip
            # (traditional hover text). Set both explicitly to APP_NAME.
            try:
                icon.set_title(APP_NAME)
            except Exception:
                log.exception("StatusIcon.set_title failed")
            try:
                icon.set_tooltip_text(APP_NAME)
            except Exception:
                log.exception("StatusIcon.set_tooltip_text failed")
            try:
                icon.set_name(TRAY_ID)  # stable process-side identifier
            except Exception:
                pass

            def on_activate(_i):
                self._callbacks["toggle"]()

            def on_popup(_i, button, time):
                menu.popup(None, None, None, None, button, time)

            icon.connect("activate", on_activate)
            icon.connect("popup-menu", on_popup)
            self._status_icon = icon
            self._backend = "statusicon"
            log.info("Tray: Gtk.StatusIcon fallback (title=%s)", APP_NAME)
        except Exception as e:
            log.warning("No tray backend available: %s", e)
            self._backend = None

    def set_listening(self, listening: bool) -> None:
        icon = "media-record" if listening else "audio-input-microphone"
        try:
            if self._indicator is not None:
                # set_icon_full(icon_name, accessible_desc). The accessible
                # description is user-visible on screen readers; keep it
                # aligned with APP_NAME. Note: this does NOT change the
                # hover tooltip — that stays whatever was set via
                # set_title(APP_NAME) in _build().
                self._indicator.set_icon_full(icon, APP_NAME)
            elif self._status_icon is not None:
                self._status_icon.set_from_icon_name(icon)
                # Keep the tooltip stable across state changes.
                try:
                    self._status_icon.set_tooltip_text(APP_NAME)
                except Exception:
                    pass
        except Exception:
            log.exception("Failed to update tray icon")

    def destroy(self) -> None:
        try:
            if self._status_icon is not None:
                self._status_icon.set_visible(False)
        except Exception:
            pass
        self._indicator = None
        self._status_icon = None
