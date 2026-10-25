"""Fixtures for security tests (no network, no database)."""

import pytest

from app.config import Settings



@pytest.fixture
def make_settings():
    """Build an isolated Settings instance (ignores any local .env file)."""

    def _make(**overrides) -> Settings:
        return Settings(_env_file=None, **overrides)

    return _make


@pytest.fixture
def use_settings(monkeypatch, make_settings):
    """Patch the settings seen by the API key dependency."""

    def _use(**overrides) -> Settings:
        settings = make_settings(**overrides)
        monkeypatch.setattr("app.api.deps.get_settings", lambda: settings)
        return settings

    return _use
