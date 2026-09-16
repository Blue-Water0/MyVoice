import json

from myvoice_win.services.settings_service import (
    SCHEMA_VERSION,
    Settings,
    SettingsService,
)


def test_defaults_are_sane():
    s = Settings()
    assert s.schema_version == SCHEMA_VERSION
    assert s.language_mode == "auto"
    assert s.model == "medium"
    assert s.engine == "faster-whisper"
    assert s.hotkey == "win+shift+space"
    assert 0 <= s.vad.aggressiveness <= 3


def test_save_and_reload_roundtrip(tmp_path):
    svc = SettingsService(path=tmp_path / "settings.json")
    original = svc.settings
    original.language_mode = "he"
    original.vad.aggressiveness = 3
    original.model = "large-v3-turbo"
    svc.save(original)

    svc2 = SettingsService(path=tmp_path / "settings.json")
    assert svc2.settings.language_mode == "he"
    assert svc2.settings.vad.aggressiveness == 3
    assert svc2.settings.model == "large-v3-turbo"


def test_bad_values_are_normalized(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "language_mode": "klingon",
        "model": "not-a-model",
        "compute_type": "weird",
        "vad": {"aggressiveness": 99, "min_speech_ms": -5, "silence_ms": 0, "max_segment_ms": 10},
    }))
    svc = SettingsService(path=p)
    s = svc.settings
    assert s.language_mode == "auto"
    assert s.model == "medium"
    assert s.compute_type == "auto"
    assert 0 <= s.vad.aggressiveness <= 3
    assert s.vad.min_speech_ms >= 50
    assert s.vad.silence_ms >= 100
    assert s.vad.max_segment_ms >= 1000


def test_atomic_write_no_partial_file(tmp_path):
    path = tmp_path / "settings.json"
    svc = SettingsService(path=path)
    svc.save()
    assert path.exists()
    # No leftover tmp files
    remnants = [p for p in tmp_path.iterdir() if p.name.startswith(".settings-")]
    assert remnants == []


def test_missing_file_uses_defaults(tmp_path):
    svc = SettingsService(path=tmp_path / "does-not-exist.json")
    assert svc.settings.model == "medium"


def test_corrupt_file_falls_back(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("not-json{")
    svc = SettingsService(path=p)
    assert svc.settings.model == "medium"


def test_terminal_wm_classes_default_empty(tmp_path):
    svc = SettingsService(path=tmp_path / "settings.json")
    assert svc.settings.terminal_wm_classes == []


def test_terminal_wm_classes_roundtrip(tmp_path):
    svc = SettingsService(path=tmp_path / "settings.json")
    svc.settings.terminal_wm_classes = ["myterm", "specialterm"]
    svc.save()
    svc2 = SettingsService(path=tmp_path / "settings.json")
    assert svc2.settings.terminal_wm_classes == ["myterm", "specialterm"]


def test_terminal_wm_classes_non_list_in_json_falls_back(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"terminal_wm_classes": "not-a-list"}))
    svc = SettingsService(path=p)
    assert svc.settings.terminal_wm_classes == []


def test_update_writes_and_returns_new_settings(tmp_path):
    svc = SettingsService(path=tmp_path / "settings.json")
    updated = svc.update(language_mode="ar", model="small")
    assert updated.language_mode == "ar"
    assert updated.model == "small"
    svc2 = SettingsService(path=tmp_path / "settings.json")
    assert svc2.settings.language_mode == "ar"
    assert svc2.settings.model == "small"


def test_default_path_uses_myvoice_win_settings_file(monkeypatch, tmp_path):
    from myvoice_win import paths

    svc = SettingsService()
    assert svc.path == paths.settings_file()


def test_settings_file_shape_matches_linux_schema(tmp_path):
    # A settings.json produced by either platform's SettingsService must be
    # round-trippable by the other -- same field set, same types.
    from myvoice.services.settings_service import Settings as LinuxSettings

    win_fields = set(Settings().to_dict().keys())
    linux_fields = set(LinuxSettings().to_dict().keys())
    assert win_fields == linux_fields
