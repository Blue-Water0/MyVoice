"""Hotkey persistence through the settings service."""
from myvoice.services.hotkey_service import (
    DEFAULT_HOTKEY,
    canonical_label_from_string,
    format_accel,
    parse_accel,
)
from myvoice.services.settings_service import SettingsService


def test_default_hotkey_roundtrips(tmp_path):
    svc = SettingsService(path=tmp_path / "s.json")
    svc.save()
    svc2 = SettingsService(path=tmp_path / "s.json")
    assert svc2.settings.hotkey == DEFAULT_HOTKEY


def test_custom_hotkey_persists(tmp_path):
    p = tmp_path / "s.json"
    svc = SettingsService(path=p)
    s = svc.settings
    s.hotkey = "<Control><Alt>d"
    svc.save(s)

    svc2 = SettingsService(path=p)
    assert svc2.settings.hotkey == "<Control><Alt>d"
    assert canonical_label_from_string(svc2.settings.hotkey) == "Ctrl+Alt+D"


def test_format_accel_produces_reparseable(tmp_path):
    # Whatever the recorder captured, format_accel -> parse_accel must round-trip.
    p = parse_accel("<Super><Shift>space")
    f = format_accel(p.mods, p.key)
    p2 = parse_accel(f)
    assert p2.mods == p.mods and p2.key == p.key


def test_canonical_form_also_accepted_from_settings_file(tmp_path):
    """If a user hand-edits settings.json with 'Super+Shift+Space', it works."""
    p = tmp_path / "s.json"
    p.write_text('{"hotkey": "Super+Shift+Space"}', encoding="utf-8")
    svc = SettingsService(path=p)
    parsed = parse_accel(svc.settings.hotkey)
    assert parsed.key == "Space"
    assert "super" in parsed.mods and "shift" in parsed.mods
