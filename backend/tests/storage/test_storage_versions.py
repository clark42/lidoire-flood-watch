"""Versions: model version from MODEL_VERSION, app version from APP_VERSION."""

from pathlib import Path

import httpx
import pytest
import respx

from app.api import status
from app.config import Settings, get_settings
from app.services import influx_client
from app.services.risk_core import MODEL_VERSION

CHART = Path(__file__).resolve().parents[3] / "deploy" / "helm" / "lidoire-flood-watch"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    status._sources_cache.clear()

    async def last_time(measurement, tags=None, field=None, start=None, stop=None):
        return status._utcnow()

    async def ok():
        return None

    monkeypatch.setattr(influx_client, "query_last_time", last_time)
    monkeypatch.setattr(status, "_check_postgres", ok)
    monkeypatch.setattr(status, "_check_redis", ok)
    yield
    status._sources_cache.clear()


def test_storage_app_version_from_env(monkeypatch):
    assert Settings(_env_file=None).app_version == "dev"
    monkeypatch.setenv("APP_VERSION", "sha-1a2b3c4")
    assert Settings(_env_file=None).app_version == "sha-1a2b3c4"


@respx.mock
async def test_storage_status_reports_derived_versions(monkeypatch):
    respx.get(get_settings().influxdb_url.rstrip("/") + "/health").mock(
        return_value=httpx.Response(200, json={"status": "pass"}))
    monkeypatch.setattr(get_settings(), "app_version", "sha-1a2b3c4")
    body = await status.get_status()
    assert body["version"] == "sha-1a2b3c4"
    assert body["model_version"] == MODEL_VERSION == "3.4"


async def test_storage_sources_detail_model_version():
    body = await status.get_sources_detail()
    assert body["risk_model"]["version"] == f"v{MODEL_VERSION}"  # was a hardcoded "v3.2"


async def test_storage_root_and_openapi_version():
    from app.main import create_app

    app = create_app(Settings(_env_file=None, app_version="v1.2.3"))
    assert app.version == "v1.2.3"
    root = next(r for r in app.routes if getattr(r, "path", None) == "/")
    body = await root.endpoint()  # no lifespan (no DB, no scheduler)
    assert body["version"] == "v1.2.3" and body["model_version"] == MODEL_VERSION


def test_storage_helm_sets_app_version_from_image_tag():
    template = (CHART / "templates" / "backend.yaml").read_text()
    assert "name: APP_VERSION" in template
    assert "{{ .Values.image.tag | default .Chart.AppVersion | quote }}" in template
