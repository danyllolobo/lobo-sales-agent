"""Testes de `src/config.py`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import ENV_FILE, Settings, get_settings


def make(**overrides) -> Settings:
    """Settings isolado do `.env` real."""
    return Settings(_env_file=None, **overrides)


def test_defaults_are_sane():
    s = make()
    assert s.environment == "development"
    assert s.port == 8000
    assert s.debounce_silence_seconds == 4.0
    assert s.debounce_max_window_seconds == 10.0
    assert s.is_production is False


def test_async_database_url_conversion():
    s = make(database_url="postgresql://u:p@db:5432/lobo")
    assert s.async_database_url == "postgresql+asyncpg://u:p@db:5432/lobo"
    s2 = make(database_url="postgres://u:p@db/lobo")
    assert s2.async_database_url == "postgresql+asyncpg://u:p@db/lobo"
    s3 = make(database_url="postgresql+asyncpg://u:p@db/lobo")
    assert s3.async_database_url == "postgresql+asyncpg://u:p@db/lobo"


def test_invalid_database_url_rejected():
    with pytest.raises(ValidationError):
        make(database_url="mysql://u:p@db/lobo")


def test_urls_are_normalized_without_trailing_slash():
    s = make(waha_base_url="https://waha.example.com/ ", chatwoot_base_url="")
    assert s.waha_base_url == "https://waha.example.com"
    assert s.chatwoot_base_url is None


def test_invalid_http_url_rejected():
    with pytest.raises(ValidationError):
        make(waha_base_url="ftp://waha.example.com")


def test_placeholder_and_empty_secrets_become_none():
    s = make(google_api_key="your_google_api_key_here", astracalls_api_key="  ")
    assert s.google_api_key is None
    assert s.astracalls_api_key is None


def test_secrets_never_leak_in_repr():
    s = make(google_api_key="super-secret-key-123", waha_api_key="waha-secret")
    text = repr(s) + str(s) + str(s.model_dump())
    assert "super-secret-key-123" not in text
    assert "waha-secret" not in text
    assert s.google_api_key.get_secret_value() == "super-secret-key-123"


def test_safe_summary_has_no_secrets():
    s = make(
        google_api_key="g-secret",
        waha_base_url="https://waha.example.com",
        waha_api_key="w-secret",
        database_url="postgresql://admin:dbpass@db/lobo",
    )
    summary = str(s.safe_summary())
    for secret in ("g-secret", "w-secret", "dbpass"):
        assert secret not in summary
    assert s.safe_summary()["integrations"]["waha"] is True
    assert s.safe_summary()["integrations"]["chatwoot"] is False


def test_integration_flags():
    s = make(
        ghl_vizzu_api_token="tok",
        ghl_vizzu_location_id="",
        astracalls_base_url="https://astra.example.com",
        astracalls_api_key="k",
    )
    assert s.ghl_vizzu_enabled is False  # location vazio
    assert s.astracalls_enabled is True


@pytest.mark.parametrize(
    ("silence", "max_window"),
    [(8, 8), (8, 5), (0.5, 10), (31, 60), (10, 65)],
)
def test_invalid_debounce_windows(silence, max_window):
    with pytest.raises(ValidationError):
        make(debounce_silence_seconds=silence, debounce_max_window_seconds=max_window)


def test_production_requires_google_api_key():
    with pytest.raises(ValidationError, match="GOOGLE_API_KEY"):
        make(environment="production", google_api_key=None)
    assert make(environment="production", google_api_key="k").is_production


def test_reads_from_environment_variables(monkeypatch):
    monkeypatch.setenv("DEBOUNCE_SILENCE_SECONDS", "6")
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = make()
    assert s.debounce_silence_seconds == 6.0
    assert s.jev_enabled is True
    assert s.log_level == "DEBUG"


@pytest.mark.skipif(not ENV_FILE.exists(), reason=".env real ausente")
def test_real_env_file_loads():
    """Garante que o `.env` do projeto é válido (sem exibir valores)."""
    get_settings.cache_clear()
    s = get_settings()
    assert s.project_name
    assert s.async_database_url.startswith("postgresql+asyncpg://")
    assert get_settings() is s  # cache

