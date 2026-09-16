from myvoice.services.language_service import (
    DEFAULT,
    SUPPORTED,
    resolve,
)


def test_resolve_explicit_supported():
    assert resolve("en") == "en"
    assert resolve("he") == "he"
    assert resolve("ar") == "ar"


def test_resolve_unknown_returns_default():
    assert resolve("xx") == DEFAULT
    assert resolve("") == DEFAULT


def test_resolve_auto_uses_env(monkeypatch):
    monkeypatch.setenv("LC_ALL", "he_IL.UTF-8")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    assert resolve("auto") == "he"


def test_resolve_auto_arabic(monkeypatch):
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "ar_PS.UTF-8")
    assert resolve("auto") == "ar"


def test_resolve_auto_english_fallback(monkeypatch):
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    assert resolve("auto") == "en"


def test_supported_shape():
    assert set(SUPPORTED) == {"en", "he", "ar"}
    assert DEFAULT in SUPPORTED
