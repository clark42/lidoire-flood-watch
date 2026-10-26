"""Startup refuses placeholder secrets; .env.example ships EMPTY secrets."""

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    InsecureConfigurationError,
    check_secrets_not_placeholders,
    is_placeholder_secret,
)
from app.config import Settings

BACKEND = pathlib.Path(__file__).resolve().parents[2]
ENV_EXAMPLE = BACKEND.parent / ".env.example"
REAL_KEY = "3f1c9e0a7b2d4c6e8f0a1b3c5d7e9f11"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("value", ["change-me", "CHANGE-ME", " changeme ", "your-api-key", "replace-me"])
def test_reviewfix_placeholder_api_key_refuses_to_start(value):
    with pytest.raises(InsecureConfigurationError) as exc:
        check_secrets_not_placeholders(_settings(api_key=value))
    message = str(exc.value)
    assert "API_KEY" in message and "openssl rand -hex 32" in message
    assert value.strip() not in message.replace("'change-me'", "")  # value never echoed


@pytest.mark.parametrize("field,value,name", [
    ("influxdb_token", "change-me", "INFLUXDB_TOKEN"),
    ("redis_url", "redis://:change-me@redis:6379/0", "REDIS_URL"),
    ("database_url", "postgresql+asyncpg://postgres:change-me@postgres:5432/lidoire_flood", "DATABASE_URL"),
])
def test_reviewfix_placeholder_backend_secrets_refuse_to_start(field, value, name):
    with pytest.raises(InsecureConfigurationError) as exc:
        check_secrets_not_placeholders(_settings(api_key=REAL_KEY, **{field: value}))
    assert name in str(exc.value)


@pytest.mark.parametrize("api_key", ["", REAL_KEY, "short-but-real"])
def test_reviewfix_real_or_empty_secrets_are_accepted(api_key):
    check_secrets_not_placeholders(_settings(api_key=api_key))  # dev defaults included
    assert not is_placeholder_secret("")
    assert not is_placeholder_secret(None)


def test_reviewfix_app_lifespan_refuses_placeholder_key(monkeypatch):
    from app import main

    called = []

    async def must_not_run():
        called.append(1)

    monkeypatch.setattr(main, "get_settings", lambda: _settings(api_key="change-me"))
    monkeypatch.setattr(main, "init_db", must_not_run)
    monkeypatch.setattr(main, "start_scheduler", must_not_run)
    app = main.create_app(_settings(api_key="change-me"))
    with pytest.raises(InsecureConfigurationError):
        with TestClient(app):
            pass
    assert called == []


def test_reviewfix_env_example_secrets_are_empty_with_generation_hint():
    text = ENV_EXAMPLE.read_text()
    values = {}
    for line in text.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    for key in ("API_KEY", "POSTGRES_PASSWORD", "INFLUXDB_ADMIN_PASSWORD", "INFLUXDB_ADMIN_TOKEN",
                "REDIS_PASSWORD", "INFLUXDB_TOKEN", "INFLUXDB_READ_TOKEN"):
        assert values[key] == "", key
    assert "change-me" not in values.values()
    assert "openssl rand -hex 32" in text
