"""Behavioral tests for the "Live Transcript" action buttons.

We test the *contract* the main-window buttons rely on, without booting
GTK:

* Copying uses the full session transcript, not the truncated preview.
* Actions are disabled when the transcript is empty.
* The persisted text-app desktop-id round-trips through settings.
"""
from __future__ import annotations

from myvoice.services.settings_service import (
    Settings,
    SettingsService,
)
from myvoice.services.transcript_buffer import TranscriptBuffer


# ---------------------------------------------------------------------------
# Full-transcript-vs-preview
# ---------------------------------------------------------------------------


def test_full_session_transcript_is_used_not_preview_slice():
    """The Copy button and Open button must operate on the full session
    transcript, not on the small preview slice shown in the UI.

    The main window uses ``self._buffer.text[-60:]`` for the overlay
    preview. This test locks in that ``TranscriptBuffer.text`` still
    contains everything so the transcript-action handlers see the full
    string.
    """
    buf = TranscriptBuffer()
    # 300 characters of dictated content, much larger than the 60-char preview.
    for i in range(30):
        buf.append(f"chunk{i:02d} more words here")

    preview = buf.text[-60:]
    full = buf.text
    assert len(full) > len(preview)
    assert full.startswith("chunk00")
    assert full.endswith(preview)  # preview is truly a tail slice


def test_full_transcript_preserves_english_hebrew_arabic():
    buf = TranscriptBuffer()
    buf.append("Hello world.")
    buf.append("שלום עולם")
    buf.append("مرحبا بالعالم")
    full = buf.text
    assert "Hello world." in full
    assert "שלום עולם" in full
    assert "مرحبا بالعالم" in full


def test_empty_transcript_is_empty_string():
    """When there has been no dictation, buffer.text is empty — the
    button handlers use that to disable / show 'No transcript to copy'."""
    buf = TranscriptBuffer()
    assert buf.text == ""
    # An empty append (whitespace only) must not create content either.
    buf.append("   ")
    assert buf.text == ""


# ---------------------------------------------------------------------------
# Settings persistence for text_app_desktop_id
# ---------------------------------------------------------------------------


def test_text_app_desktop_id_defaults_to_system_default():
    s = Settings()
    assert s.text_app_desktop_id == ""  # SYSTEM_DEFAULT_ID


def test_text_app_desktop_id_persists_across_reload(tmp_path):
    svc = SettingsService(path=tmp_path / "settings.json")
    s = svc.settings
    s.text_app_desktop_id = "org.x.editor.desktop"
    svc.save(s)

    svc2 = SettingsService(path=tmp_path / "settings.json")
    assert svc2.settings.text_app_desktop_id == "org.x.editor.desktop"


def test_text_app_desktop_id_survives_unknown_extra_keys(tmp_path):
    """Older config files that lack the key must still load with a
    sane default, and unknown keys must not break loading."""
    import json
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "language_mode": "en",
        "obsolete_key": "who cares",
    }), encoding="utf-8")
    svc = SettingsService(path=p)
    assert svc.settings.language_mode == "en"
    assert svc.settings.text_app_desktop_id == ""


# ---------------------------------------------------------------------------
# Fallback behavior: uninstalled configured app
# ---------------------------------------------------------------------------


def test_uninstalled_configured_app_falls_back_via_open_transcript_in_app(
    tmp_path, monkeypatch,
):
    """If the persisted desktop_id no longer resolves, opening the
    transcript must still succeed by falling back to the system
    default. The saved file is preserved either way.
    """
    import datetime as _dt
    from myvoice.services import transcript_export as tx

    class FakeApp:
        def get_id(self):
            return "system_default.desktop"

        def get_name(self):
            return "System Default"

        def get_display_name(self):
            return "System Default"

        def get_icon(self):
            return None

    fake_default = FakeApp()

    # Force _resolve_app to simulate "configured id missing -> fallback".
    def fake_resolve(desktop_id, default_getter=None):
        if desktop_id == "":
            return (fake_default, False, None)
        return (fake_default, True,
                f"Configured text app '{desktop_id}' is not installed")

    monkeypatch.setattr(tx, "_resolve_app", fake_resolve)

    launched: list = []

    def fake_launcher(app, path):
        launched.append((app, path))

    result = tx.open_transcript_in_app(
        text="fallback text",
        desktop_id="i-am-not-installed.desktop",
        directory=tmp_path,
        now=_dt.datetime(2026, 7, 13, 12, 0, 0),
        launcher=fake_launcher,
    )

    assert result.launched is True
    assert result.used_fallback is True
    assert "not installed" in (result.fallback_reason or "")
    assert result.path.exists()
    assert len(launched) == 1
