from pathlib import Path

from myvoice import paths


def test_dirs_are_created(tmp_path):
    # conftest already isolates XDG paths under tmp_path
    cfg = paths.config_dir()
    cache = paths.cache_dir()
    logs = paths.log_dir()
    models = paths.models_cache_dir()
    for p in (cfg, cache, logs, models):
        assert isinstance(p, Path)
        assert p.exists() and p.is_dir()


def test_settings_file_under_config():
    sf = paths.settings_file()
    assert sf.parent == paths.config_dir()
    assert sf.name == "settings.json"
