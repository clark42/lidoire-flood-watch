"""GET /api/auth/netatmo (OAuth start) requires the API key when one is configured."""

import pytest
from fastapi.testclient import TestClient

from app.api import auth
from app.config import Settings

API_KEY = "followups-key-0123456789"


@pytest.fixture
def client_with(monkeypatch):
    def _make(**overrides) -> TestClient:
        settings = Settings(_env_file=None, netatmo_client_id="client-123", **overrides)
        monkeypatch.setattr("app.api.deps.get_settings", lambda: settings)
        monkeypatch.setattr("app.api.auth.get_settings", lambda: settings)
        monkeypatch.setattr(auth, "_oauth_states", {})
        from app.main import create_app

        return TestClient(create_app(), follow_redirects=False)

    return _make


def test_followups_oauth_start_without_key_is_rejected(client_with):
    client = client_with(api_key=API_KEY)
    response = client.get("/api/auth/netatmo")
    assert response.status_code == 401
    assert "api_key" in response.json()["detail"]
    assert auth._oauth_states == {}  # no state issued


def test_followups_oauth_start_with_wrong_key_is_rejected(client_with):
    client = client_with(api_key=API_KEY)
    assert client.get("/api/auth/netatmo", params={"api_key": "nope"}).status_code == 401
    assert client.get("/api/auth/netatmo", headers={"X-API-Key": "nope"}).status_code == 401
    assert auth._oauth_states == {}


def test_followups_oauth_start_with_key_redirects(client_with):
    client = client_with(api_key=API_KEY)
    response = client.get("/api/auth/netatmo", params={"api_key": API_KEY})
    assert response.status_code in (302, 307)
    location = response.headers["location"]
    assert location.startswith(auth.NETATMO_AUTH_URL)
    assert "client_id=client-123" in location
    assert API_KEY not in location  # the key is never forwarded to Netatmo
    assert len(auth._oauth_states) == 1
    # header form (curl) also accepted
    assert client.get("/api/auth/netatmo", headers={"X-API-Key": API_KEY}).status_code in (302, 307)


def test_followups_oauth_start_open_in_dev_mode(client_with):
    client = client_with(api_key="")
    assert client.get("/api/auth/netatmo").status_code in (302, 307)


def test_followups_oauth_start_uses_constant_time_compare(client_with, monkeypatch):
    calls = []

    def spy(provided, expected):
        calls.append((provided, expected))
        return provided == expected

    monkeypatch.setattr(auth, "api_key_matches", spy)
    client = client_with(api_key=API_KEY)
    client.get("/api/auth/netatmo", params={"api_key": "nope"})
    assert calls == [("nope", API_KEY)]


def test_followups_oauth_callback_and_status_stay_public(client_with):
    client = client_with(api_key=API_KEY)
    assert client.get("/api/auth/netatmo/setup").status_code == 200
    response = client.get("/api/auth/netatmo/callback", params={"code": "x", "state": "unknown"})
    assert response.status_code == 400  # own state validation, not the API key
