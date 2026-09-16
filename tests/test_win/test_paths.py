from pathlib import Path

from myvoice_win import paths


def test_dirs_are_created():
    cfg = paths.config_dir()
    logs = paths.log_dir()
    models = paths.models_cache_dir()
    for p in (cfg, logs, models):
        assert isinstance(p, Path)
        assert p.exists() and p.is_dir()


def test_config_dir_uses_appdata_env(monkeypatch, tmp_path):
    appdata = tmp_path / "custom-appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    cfg = paths.config_dir()
    assert cfg == appdata / "MyVoice"
    assert cfg.exists()


def test_config_dir_falls_back_when_appdata_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = paths.config_dir()
    assert cfg == tmp_path / "AppData" / "Roaming" / "MyVoice"


def test_cache_dir_uses_localappdata_env(monkeypatch, tmp_path):
    localappdata = tmp_path / "custom-localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    cache = paths.cache_dir()
    assert cache == localappdata / "MyVoice" / "cache"
    assert cache.exists()


def test_cache_dir_falls_back_when_localappdata_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = paths.cache_dir()
    assert cache == tmp_path / "AppData" / "Local" / "MyVoice" / "cache"


def test_log_dir_under_config():
    logs = paths.log_dir()
    assert logs == paths.config_dir() / "logs"


def test_settings_file_under_config():
    sf = paths.settings_file()
    assert sf.parent == paths.config_dir()
    assert sf.name == "settings.json"


def test_models_cache_dir_under_cache():
    models = paths.models_cache_dir()
    assert models == paths.cache_dir() / "models"


def test_transcripts_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    td = paths.transcripts_dir()
    assert td == tmp_path / "Documents" / "MyVoice Transcripts"
    assert td.exists() and td.is_dir()


def test_no_autostart_dir():
    assert not hasattr(paths, "autostart_dir")
