"""Non-blocking desktop notification helper (Windows toast via plyer).

Windows-native equivalent of ``myvoice.services.notifications`` (which
uses libnotify/GObject). Same call shape --
``notify(title, msg, urgency="normal")`` -- so callers written against
the Linux signature work unchanged on Windows without an ``if platform``
branch at the call site.

Falls back silently if plyer's notification backend is unavailable or
raises -- the main window still shows inline status. Never raises to the
caller, matching the Linux wrapper's contract.
"""
from __future__ import annotations

import logging

import plyer

log = logging.getLogger(__name__)


def notify(title: str, msg: str, urgency: str = "normal") -> None:
    """Show a Windows toast notification for ``title``/``msg``.

    ``urgency`` has no equivalent in plyer's Windows toast backend --
    it is accepted (so call sites shared with the Linux code path don't
    need to branch on platform) and silently ignored, never forwarded to
    plyer. Never raises: any failure from the underlying notification
    backend is logged at debug level and swallowed.
    """
    try:
        plyer.notification.notify(title=title, message=msg)
    except Exception as e:
        log.debug("notify failed: %s", e)
