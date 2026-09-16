"""Non-blocking desktop notification helper (libnotify via GObject).

Falls back silently if Notify is unavailable — the main window still shows
inline status.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_notify_inited = False
_notify_available = False


def _init() -> bool:
    global _notify_inited, _notify_available
    if _notify_inited:
        return _notify_available
    _notify_inited = True
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify  # type: ignore
        Notify.init("MyVoice")
        _notify_available = True
    except Exception as e:
        log.debug("libnotify unavailable: %s", e)
        _notify_available = False
    return _notify_available


def notify(title: str, body: str, urgency: str = "normal") -> None:
    if not _init():
        return
    try:
        from gi.repository import Notify  # type: ignore
        n = Notify.Notification.new(title, body, "audio-input-microphone")
        try:
            u = {"low": Notify.Urgency.LOW, "normal": Notify.Urgency.NORMAL,
                 "critical": Notify.Urgency.CRITICAL}[urgency]
            n.set_urgency(u)
        except Exception:
            pass
        n.show()
    except Exception as e:
        log.debug("notify failed: %s", e)
