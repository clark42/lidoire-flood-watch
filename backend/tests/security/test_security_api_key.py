"""Tests for the X-API-Key write protection dependency."""

import logging

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    API_KEY_HEADER_NAME,
    api_key_matches,
    require_api_key_for_writes,
    warn_if_api_key_missing,
)

TEST_API_KEY = "test-key-0123456789abcdef"
WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE"]


@pytest.fixture
def client() -> TestClient:
    """Minimal app wired like app.api: router-level dependency on all routes."""
    router = APIRouter(dependencies=[Depends(require_api_key_for_writes)])

    @router.api_route("/thing", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def thing():
        return {"ok": True}

    @router.options("/thing")
    async def thing_options():
        return {"ok": True}

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


# --- constant-time compare -------------------------------------------------


def test_security_api_key_matches():
    assert api_key_matches("abc", "abc")
    assert not api_key_matches("abd", "abc")
    assert not api_key_matches("ab", "abc")
    assert not api_key_matches(None, "abc")
    assert not api_key_matches("", "abc")
    # An empty expected key never matches (dev mode is handled separately)
    assert not api_key_matches("", "")


def test_security_api_key_matches_non_ascii():
    assert api_key_matches("clé-é", "clé-é")
    assert not api_key_matches("clé-e", "clé-é")


# --- key configured --------------------------------------------------------


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_security_write_without_key_is_rejected(client, use_settings, method):
    use_settings(api_key=TEST_API_KEY)
    response = client.request(method, "/api/thing")
    assert response.status_code == 401
    assert API_KEY_HEADER_NAME in response.json()["detail"]
    assert response.headers.get("www-authenticate") == API_KEY_HEADER_NAME


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_security_write_with_wrong_key_is_rejected(client, use_settings, method):
    use_settings(api_key=TEST_API_KEY)
    response = client.request(method, "/api/thing", headers={API_KEY_HEADER_NAME: "nope"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid API key"


def test_security_wrong_key_is_logged(client, use_settings, caplog):
    use_settings(api_key=TEST_API_KEY)
    with caplog.at_level(logging.WARNING, logger="app.api.deps"):
        client.post("/api/thing", headers={API_KEY_HEADER_NAME: "nope"})
    assert any("invalid API key" in r.getMessage() for r in caplog.records)
    # The submitted key must never be logged
    assert all("nope" not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_security_write_with_right_key_is_allowed(client, use_settings, method):
    use_settings(api_key=TEST_API_KEY)
    response = client.request(method, "/api/thing", headers={API_KEY_HEADER_NAME: TEST_API_KEY})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_security_header_name_is_case_insensitive(client, use_settings):
    use_settings(api_key=TEST_API_KEY)
    response = client.post("/api/thing", headers={"x-api-key": TEST_API_KEY})
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_security_safe_methods_need_no_key(client, use_settings, method):
    use_settings(api_key=TEST_API_KEY)
    response = client.request(method, "/api/thing")
    assert response.status_code == 200


def test_security_safe_method_with_wrong_key_still_allowed(client, use_settings):
    use_settings(api_key=TEST_API_KEY)
    response = client.get("/api/thing", headers={API_KEY_HEADER_NAME: "nope"})
    assert response.status_code == 200


# --- dev mode (no key configured) -----------------------------------------


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_security_dev_mode_allows_writes(client, use_settings, method):
    use_settings(api_key="")
    response = client.request(method, "/api/thing")
    assert response.status_code == 200


def test_security_warn_if_api_key_missing(make_settings, caplog):
    with caplog.at_level(logging.WARNING, logger="app.api.deps"):
        assert warn_if_api_key_missing(make_settings(api_key="")) is False
    assert any("API_KEY is not set" in r.getMessage() for r in caplog.records)


def test_security_no_warning_when_key_set(make_settings, caplog):
    with caplog.at_level(logging.WARNING, logger="app.api.deps"):
        assert warn_if_api_key_missing(make_settings(api_key=TEST_API_KEY)) is True
    assert not any("API_KEY is not set" in r.getMessage() for r in caplog.records)


def test_security_api_key_loaded_from_env(monkeypatch, make_settings):
    monkeypatch.setenv("API_KEY", "from-env")
    assert make_settings().api_key == "from-env"


# --- wiring on the real API router ----------------------------------------


@pytest.fixture
def real_client(use_settings) -> TestClient:
    """Real application (lifespan not started: no DB/scheduler)."""
    from app.main import create_app

    use_settings(api_key=TEST_API_KEY)
    return TestClient(create_app())


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/episodes"),
        ("PUT", "/api/episodes/1"),
        ("DELETE", "/api/episodes/1"),
        ("POST", "/api/episodes/backfill"),
        ("POST", "/api/episodes/detect"),
        ("POST", "/api/risk/simulate"),
    ],
)
def test_security_real_write_routes_require_key(real_client, method, path):
    assert real_client.request(method, path).status_code == 401
    wrong = real_client.request(method, path, headers={API_KEY_HEADER_NAME: "nope"})
    assert wrong.status_code == 401


def test_security_oauth_setup_get_is_public(real_client):
    response = real_client.get("/api/auth/netatmo/setup")
    assert response.status_code == 200
    assert "callback_url" in response.json()


def test_security_oauth_callback_get_is_not_blocked(real_client):
    # Netatmo redirects the browser here with a plain GET (no custom header):
    # the API key check must not interfere, the endpoint's own validation runs.
    response = real_client.get(
        "/api/auth/netatmo/callback",
        params={"error": "access_denied", "error_description": "user refused"},
    )
    assert response.status_code == 400
    assert "access_denied" in response.json()["detail"]


def test_security_every_api_operation_has_the_dependency(real_client):
    # Behavioural check (independent of FastAPI routing internals): the
    # Security() dependency marks every operation in the OpenAPI schema.
    schema = real_client.get("/openapi.json").json()
    operations = [
        (method.upper(), path, op)
        for path, item in schema["paths"].items()
        if path.startswith("/api/")
        for method, op in item.items()
        if method in {"get", "head", "post", "put", "patch", "delete"}
    ]
    assert len(operations) > 10
    missing = [
        f"{method} {path}"
        for method, path, op in operations
        if not any("APIKeyHeader" in req for req in op.get("security", []))
    ]
    assert missing == []


def test_security_openapi_declares_api_key_scheme(real_client):
    schema = real_client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert any(
        s.get("type") == "apiKey" and s.get("name") == API_KEY_HEADER_NAME and s.get("in") == "header"
        for s in schemes.values()
    )
