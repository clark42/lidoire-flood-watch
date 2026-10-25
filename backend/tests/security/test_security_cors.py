"""Tests for the CORS configuration."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ALLOWED = "http://localhost:3100"
DENIED = "https://evil.example.com"


def test_security_cors_default_origins(make_settings):
    settings = make_settings(frontend_url="http://localhost:3100")
    assert settings.cors_origin_list == ["http://localhost:3100", "http://localhost:5173"]


def test_security_cors_origins_parsing(make_settings):
    settings = make_settings(
        cors_origins=" https://a.example.com/ , ,https://b.example.com,*,https://a.example.com",
        frontend_url="https://flood.example.com/",
    )
    assert settings.cors_origin_list == [
        "https://a.example.com",
        "https://b.example.com",
        "https://flood.example.com",
    ]


def test_security_cors_origins_from_env(monkeypatch, make_settings):
    monkeypatch.setenv("CORS_ORIGINS", "https://x.example.com,https://y.example.com")
    monkeypatch.setenv("FRONTEND_URL", "https://x.example.com")
    assert make_settings().cors_origin_list == ["https://x.example.com", "https://y.example.com"]


def test_security_cors_wildcard_is_ignored_and_warned(make_settings, caplog):
    with caplog.at_level(logging.WARNING, logger="app.main"):
        app = create_app(make_settings(cors_origins="*", frontend_url=""))
    assert any("'*'" in r.getMessage() for r in caplog.records)
    response = TestClient(app).get("/", headers={"Origin": DENIED})
    assert "access-control-allow-origin" not in response.headers


@pytest.fixture
def client(make_settings, use_settings) -> TestClient:
    use_settings(api_key="secret-key-for-cors-tests")
    return TestClient(create_app(make_settings(frontend_url="http://localhost:3100")))


def test_security_cors_preflight_allowed_origin(client):
    response = client.options(
        "/api/episodes",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-api-key",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "x-api-key" in allowed_headers
    assert "content-type" in allowed_headers
    assert "POST" in response.headers["access-control-allow-methods"]
    # No cookies/credentials: auth is header based
    assert "access-control-allow-credentials" not in response.headers


def test_security_cors_preflight_denied_origin(client):
    response = client.options(
        "/api/episodes",
        headers={"Origin": DENIED, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_security_cors_preflight_unknown_header_denied(client):
    response = client.options(
        "/api/episodes",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-something-else",
        },
    )
    assert response.status_code == 400


def test_security_cors_simple_request(client):
    allowed = client.get("/", headers={"Origin": ALLOWED})
    assert allowed.headers["access-control-allow-origin"] == ALLOWED
    denied = client.get("/", headers={"Origin": DENIED})
    assert "access-control-allow-origin" not in denied.headers
