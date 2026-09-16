"""Language detection from OS locale and mapping to supported codes."""
from __future__ import annotations

import locale
import logging
import os
from typing import Iterable

log = logging.getLogger(__name__)

SUPPORTED = ("en", "he", "ar")
DEFAULT = "en"


def _candidate_env_locales() -> Iterable[str]:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var)
        if v:
            for part in v.split(":"):
                if part:
                    yield part


def detect_os_language() -> str:
    """Return one of SUPPORTED. English default."""
    candidates: list[str] = list(_candidate_env_locales())
    try:
        loc, _ = locale.getdefaultlocale()
        if loc:
            candidates.append(loc)
    except (ValueError, TypeError) as e:  # pragma: no cover
        log.debug("getdefaultlocale failed: %s", e)

    for cand in candidates:
        if not cand:
            continue
        low = cand.lower().replace("-", "_")
        prefix = low.split("_")[0].split(".")[0]
        if prefix in SUPPORTED:
            return prefix
    return DEFAULT


def resolve(mode: str) -> str:
    """
    Map a settings language_mode value to a concrete whisper language code.

    mode: 'auto' | 'en' | 'he' | 'ar' | anything else -> default.
    """
    if mode == "auto":
        return detect_os_language()
    if mode in SUPPORTED:
        return mode
    return DEFAULT


LANGUAGE_LABELS = {
    "auto": "Automatic (OS language)",
    "en": "English",
    "he": "Hebrew (עברית)",
    "ar": "Arabic (العربية)",
}
