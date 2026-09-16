import json

from myvoice.services.settings_service import (
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
    import json
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"terminal_wm_classes": "not-a-list"}))
    svc = SettingsService(path=p)
    assert svc.settings.terminal_wm_classes == []
