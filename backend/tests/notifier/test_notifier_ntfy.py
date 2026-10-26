"""ntfy channel, disabled mode, POST /api/notify/test, scheduler registration, state table."""

import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from notifier_harness import NTFY_TOPIC, NtfyServer, decode_title, make_settings

from app.services import notifier
from app.services.notifier import Notification, NtfyChannel

TEST_API_KEY = "test-key-0123456789abcdef"


def _channel(server, **kw) -> NtfyChannel:
    return NtfyChannel("https://ntfy.example.test/", NTFY_TOPIC, transport=httpx.MockTransport(server), **kw)


async def test_notifier_ntfy_request_format():
    server = NtfyServer()
    ok = await _channel(server, token="tk_secret").send(Notification(
        title="Lidoire : risque ROUGE prévu", message="Pluie 72 h : 48 mm\nÉtiage", priority=5,
        tags=("rotating_light", "warning"), click="http://lidoire.test",
    ))
    assert ok
    request = server.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://ntfy.example.test/{NTFY_TOPIC}"
    assert request.content.decode("utf-8") == "Pluie 72 h : 48 mm\nÉtiage"
    assert request.headers["Title"].startswith("=?UTF-8?B?")        # RFC 2047 (accents)
    assert decode_title(request.headers["Title"]) == "Lidoire : risque ROUGE prévu"
    assert request.headers["Priority"] == "5"
    assert request.headers["Tags"] == "rotating_light,warning"
    assert request.headers["Click"] == "http://lidoire.test"
    assert request.headers["Authorization"] == "Bearer tk_secret"


async def test_notifier_ntfy_ascii_title_and_no_token():
    server = NtfyServer()
    assert await _channel(server).send(Notification(title="Lidoire : test", message="ok"))
    assert server.requests[0].headers["Title"] == "Lidoire : test"
    assert "Authorization" not in server.requests[0].headers


async def test_notifier_ntfy_failures_return_false_without_leaking_topic(caplog):
    caplog.set_level(logging.ERROR, logger="app.services.notifier")
    assert not await _channel(NtfyServer(status=403)).send(Notification(title="t", message="m"))

    def down(request):
        raise httpx.ConnectError("unreachable", request=request)

    assert not await _channel(down).send(Notification(title="t", message="m"))
    assert "ntfy.example.test" in caplog.text
    assert NTFY_TOPIC not in caplog.text


# --- disabled mode --------------------------------------------------------------

@pytest.mark.parametrize("overrides", [{"ntfy_url": ""}, {"ntfy_topic": ""}, {"ntfy_url": " ", "ntfy_topic": " "}])
def test_notifier_disabled_when_url_or_topic_missing(overrides):
    settings = make_settings(**overrides)
    assert not settings.notifications_enabled
    assert notifier.build_channel(settings) is None


def test_notifier_disabled_mode_logs_once_and_registers_no_job(monkeypatch, caplog):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    monkeypatch.setattr(notifier, "get_settings", lambda: make_settings(ntfy_url=""))
    caplog.set_level(logging.INFO, logger="app.services.notifier")
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    assert notifier.schedule_notifier(scheduler, first_run=datetime.now(timezone.utc)) is False
    assert scheduler.get_jobs() == []
    records = [r for r in caplog.records if r.name == "app.services.notifier"]
    assert len(records) == 1 and records[0].levelno == logging.INFO
    assert "disabled" in records[0].getMessage()


async def test_notifier_disabled_job_and_test_message_do_nothing(monkeypatch):
    monkeypatch.setattr(notifier, "get_settings", lambda: make_settings(ntfy_topic=""))
    await notifier.notifier_job()                        # no channel: returns at once
    assert await notifier.send_test_notification() is False


def test_notifier_enabled_registers_job_after_risk_scorer(monkeypatch, caplog):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    monkeypatch.setattr(notifier, "get_settings", lambda: make_settings())
    caplog.set_level(logging.INFO, logger="app.services.notifier")
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    first = datetime(2026, 10, 26, 12, 2, tzinfo=timezone.utc)
    assert notifier.schedule_notifier(scheduler, first_run=first) is True
    job = scheduler.get_job(notifier.JOB_ID)
    assert job.trigger.interval == timedelta(hours=1)
    assert job.next_run_time == first + notifier.JOB_OFFSET
    assert NTFY_TOPIC not in caplog.text


# --- POST /api/notify/test --------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    """App client with a mocked ntfy server; returns (client, server, use_settings)."""
    from app.main import create_app

    server = NtfyServer()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        notifier.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(server), **kw),
    )

    def use(**overrides):
        settings = make_settings(api_key=TEST_API_KEY, **overrides)
        monkeypatch.setattr("app.api.deps.get_settings", lambda: settings)
        monkeypatch.setattr("app.api.notify.get_settings", lambda: settings)

    use()
    return TestClient(create_app(make_settings())), server, use


def test_notifier_test_route_requires_api_key(api):
    client, server, _ = api
    assert client.post("/api/notify/test").status_code == 401
    assert client.post("/api/notify/test", headers={"X-API-Key": "wrong-key-0123456789"}).status_code == 401
    assert server.requests == []


def test_notifier_test_route_sends(api):
    client, server, _ = api
    response = client.post("/api/notify/test", headers={"X-API-Key": TEST_API_KEY})
    assert response.status_code == 200
    assert response.json() == {"sent": True, "channel": "ntfy"}
    assert decode_title(server.requests[0].headers["Title"]) == "Lidoire : test de notification"
    assert "Tableau de bord : http://lidoire.test" in server.requests[0].content.decode("utf-8")


def test_notifier_test_route_disabled_and_failure(api):
    client, server, use = api
    use(ntfy_url="")
    assert client.post("/api/notify/test", headers={"X-API-Key": TEST_API_KEY}).status_code == 503
    use()
    server.status = 500
    assert client.post("/api/notify/test", headers={"X-API-Key": TEST_API_KEY}).status_code == 502


# --- state table and secrets ------------------------------------------------------

def test_notifier_state_table_created_by_metadata():
    from app.db import Base
    from app.models.orm import NotifierStateRow

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(NotifierStateRow(key="default", value='{"level": "orange"}'))
        session.commit()
        assert session.get(NotifierStateRow, "default").value == '{"level": "orange"}'
    engine.dispose()


def test_notifier_placeholder_token_refuses_to_start():
    from app.api.deps import InsecureConfigurationError, check_secrets_not_placeholders

    with pytest.raises(InsecureConfigurationError, match="NTFY_TOKEN"):
        check_secrets_not_placeholders(make_settings(ntfy_token="change-me"))
