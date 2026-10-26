"""Static checks on the deployment files (docker-compose, nginx, .env.example).

These guard against regressions such as re-publishing the databases or
committing real credentials (the repository is public).
"""

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.yml"
NGINX = REPO_ROOT / "frontend" / "nginx.conf"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

INTERNAL_SERVICES = ["postgres", "influxdb", "redis"]


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _env(service: dict) -> dict:
    env = service.get("environment", {})
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return env


@pytest.mark.parametrize("name", INTERNAL_SERVICES)
def test_security_internal_services_not_published(compose, name):
    assert "ports" not in compose["services"][name]


@pytest.mark.parametrize("name", ["backend", "frontend"])
def test_security_published_ports_bound_to_loopback_by_default(compose, name):
    ports = compose["services"][name]["ports"]
    assert ports
    for port in ports:
        assert str(port).startswith("${BIND_ADDRESS:-127.0.0.1}:"), port


def test_security_no_source_bind_mount(compose):
    for volume in compose["services"]["backend"].get("volumes", []):
        assert not str(volume).startswith("./"), volume


def test_security_no_hardcoded_credentials(compose):
    known_defaults = {"postgres:postgres@", "adminpassword", "lidoire-flood-watch-token"}
    text = COMPOSE.read_text()
    for value in known_defaults:
        assert value not in text


@pytest.mark.parametrize(
    "service,var",
    [
        ("postgres", "POSTGRES_PASSWORD"),
        ("influxdb", "DOCKER_INFLUXDB_INIT_PASSWORD"),
        ("influxdb", "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"),
        ("redis", "REDIS_PASSWORD"),
    ],
)
def test_security_secrets_are_required_variables(compose, service, var):
    value = _env(compose["services"][service])[var]
    assert re.fullmatch(r"\$\{[A-Z_]+:\?[^}]+\}", value), value


def test_security_redis_requires_password(compose):
    command = " ".join(compose["services"]["redis"]["command"])
    assert "--requirepass" in command
    assert "REDIS_PASSWORD" in _env(compose["services"]["backend"])["REDIS_URL"]


def test_security_backend_does_not_get_admin_secrets(compose):
    env = _env(compose["services"]["backend"])
    assert env["INFLUXDB_ADMIN_TOKEN"] == ""
    assert env["INFLUXDB_ADMIN_PASSWORD"] == ""


def test_security_env_example_has_only_placeholders():
    secret_keys = {
        "API_KEY",
        "POSTGRES_PASSWORD",
        "INFLUXDB_ADMIN_PASSWORD",
        "INFLUXDB_ADMIN_TOKEN",
        "INFLUXDB_TOKEN",
        "INFLUXDB_READ_TOKEN",
        "REDIS_PASSWORD",
        "NETATMO_CLIENT_SECRET",
        "NETATMO_REFRESH_TOKEN",
        "NETATMO_ACCESS_TOKEN",
        "INFOCLIMAT_API_KEY",
        "SHOM_API_KEY",
        "METEOFRANCE_API_KEY",
        "CAMERA_RTSP_URL",
        "NTFY_TOPIC",
        "NTFY_TOKEN",
    }
    seen = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in secret_keys:
            seen.add(key)
            # EMPTY (not "change-me"): compose ":?" fails fast and the backend
            # refuses known placeholders at startup
            assert value == "", f"{key} must be empty in .env.example"
    # Every variable required by docker-compose is documented
    assert {"API_KEY", "POSTGRES_PASSWORD", "INFLUXDB_ADMIN_TOKEN", "REDIS_PASSWORD"} <= seen


def test_security_nginx_headers_and_timeouts():
    conf = NGINX.read_text()
    for header in (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
    ):
        assert f"add_header {header}" in conf
    assert "frame-ancestors 'none'" in conf
    assert "tile.openstreetmap.org" in conf
    assert "proxy_read_timeout 180s" in conf
    assert "location ^~ /api/" in conf
    assert "server_tokens off" in conf
