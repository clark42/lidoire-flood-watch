"""NOTIFY_MIN_LEVEL: red-only policy (owner budget), orange default unchanged."""

import pytest
from pydantic import ValidationError

from notifier_harness import make_settings

ORANGE, RED, YELLOW, GREEN = 40.0, 60.0, 20.0, 5.0
RED_POLICY = {"notify_min_level": "red", "notify_downgrade_hold_hours": 12.0}


def test_min_level_setting_default_and_env(monkeypatch):
    assert make_settings().notify_min_level == "orange"
    monkeypatch.setenv("NOTIFY_MIN_LEVEL", "Red")
    monkeypatch.setenv("NOTIFY_DOWNGRADE_HOLD_HOURS", "12")
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.notify_min_level == "red" and s.notify_downgrade_hold_hours == 12.0
    with pytest.raises(ValidationError):
        make_settings(notify_min_level="yellow")


# --- observed level -------------------------------------------------------------

async def test_red_policy_orange_is_silent_red_notifies(harness):
    h = harness(**RED_POLICY)
    assert await h.run(ORANGE) == []                     # green -> orange: silent
    assert h.state["level"] == "orange"                  # still tracked
    assert await h.run(RED) == ["Lidoire : risque ROUGE (60/100)"]
    assert h.state["level"] == "red"


async def test_red_policy_first_run_in_red_notifies(harness):
    h = harness(**RED_POLICY)
    assert await h.run(70.0) == ["Lidoire : risque ROUGE (70/100)"]


async def test_red_policy_downgrade_hold_is_12h(harness):
    h = harness(**RED_POLICY)
    await h.run(RED)
    for _ in range(12):                                  # t+0 .. t+11: pending
        assert await h.run(ORANGE) == []
    assert h.state["pending_level"] == "orange"
    assert await h.run(ORANGE) == ["Lidoire : retour en ORANGE (40/100)"]   # held 12 h
    assert h.state["level"] == "orange"
    for _ in range(14):                                  # orange -> green: not an alert level
        assert await h.run(GREEN) == []
    assert h.state["level"] == "green"


async def test_red_policy_tide_flapping_around_red_sends_once(harness):
    h = harness(**RED_POLICY)
    sent = []
    for score in [56, 50, 58, 45, 57, 40, 60, 52, 41, 38, 59]:   # red <-> orange with the tide
        sent += await h.run(float(score))
    assert sent == ["Lidoire : risque ROUGE (56/100)"]


async def test_red_policy_ignores_notify_yellow(harness):
    h = harness(notify_yellow=True, **RED_POLICY)
    assert await h.run(YELLOW) == []
    assert await h.run(ORANGE) == []
    for _ in range(14):
        assert await h.run(GREEN) == []


# --- forecast heads-up --------------------------------------------------------

async def test_red_policy_forecast_headsup_fires_at_red_only(harness):
    h = harness(**RED_POLICY)
    h.world.forecast = lambda hour: 45.0 if 20 <= hour <= 30 else 10.0
    assert await h.run(GREEN) == []                      # orange forecast: silent
    assert h.state["forecast_level"] is None
    h.world.forecast = lambda hour: {18: 40.0, 22: 56.0, 24: 62.0}.get(hour, 10.0)
    assert await h.run(GREEN) == ["Lidoire : risque ROUGE prévu"]
    assert h.state["forecast_level"] == "red"
    for _ in range(3):
        assert await h.run(GREEN) == []                  # no repeat


async def test_red_policy_forecast_body_and_clear(harness):
    h = harness(**RED_POLICY)
    h.world.forecast = lambda hour: {18: 40.0, 22: 56.0, 24: 62.0}.get(hour, 10.0)
    h.world.score = GREEN
    await h.notifier().run_once()
    body = h.server.requests[-1].content.decode("utf-8")
    assert body.startswith(
        "Prévision : score max 62/100 (ROUGE) dans 24 h vers 11/02 13:00, "
        "seuil rouge atteint dans 22 h (horizon 48 h)."
    )
    h.server.take()
    h.world.forecast = lambda hour: 45.0                 # orange only: below the minimum
    for _ in range(12):
        assert await h.run(GREEN) == []
    assert await h.run(GREEN) == ["Lidoire : prévision revenue sous le rouge"]
    assert h.state["forecast_level"] is None


async def test_red_policy_forecast_silent_when_red_already_observed(harness):
    h = harness(**RED_POLICY)
    h.world.forecast = lambda hour: 70.0
    assert await h.run(RED) == ["Lidoire : risque ROUGE (60/100)"]
    assert h.state["forecast_level"] == "red"


# --- switching policy on a persisted state ------------------------------------

async def test_switch_to_red_keeps_persisted_state_compatible(harness):
    h = harness()                                        # orange (old default)
    h.world.forecast = lambda hour: 45.0 if 20 <= hour <= 30 else 10.0
    assert await h.run(ORANGE) == ["Lidoire : risque ORANGE (40/100)"]
    assert h.state["level"] == "orange" and h.state["forecast_level"] == "orange"
    # Redeploy with the red policy: same notifier_state row
    h.settings = make_settings(**RED_POLICY)
    assert await h.run(ORANGE) == []
    for _ in range(14):                                  # stored orange heads-up ends silently
        assert await h.run(GREEN) == []
    assert h.state["level"] == "green" and h.state["forecast_level"] is None
    assert await h.run(RED) == ["Lidoire : risque ROUGE (60/100)"]


# --- default (orange) unchanged ------------------------------------------------

async def test_orange_default_unchanged(harness):
    h = harness()
    assert h.settings.notify_min_level == "orange" and h.settings.notify_downgrade_hold_hours == 3.0
    assert await h.run(YELLOW) == []
    assert await h.run(ORANGE) == ["Lidoire : risque ORANGE (40/100)"]
    assert await h.run(RED) == ["Lidoire : risque ROUGE (60/100)"]
    for _ in range(3):
        assert await h.run(YELLOW) == []
    assert await h.run(YELLOW) == ["Lidoire : retour en JAUNE (20/100)"]


async def test_orange_default_forecast_texts_unchanged(harness):
    h = harness()
    h.world.forecast = lambda hour: 42.0 if 20 <= hour <= 30 else 10.0
    assert await h.run(GREEN) == ["Lidoire : risque ORANGE prévu"]
    h.world.forecast = lambda hour: 20.0
    for _ in range(3):
        assert await h.run(GREEN) == []
    assert await h.run(GREEN) == ["Lidoire : prévision revenue sous l'orange"]


async def test_test_notification_lists_the_alert_levels():
    import httpx

    from app.services.notifier import NtfyChannel, send_test_notification
    from notifier_harness import NTFY_TOPIC, NTFY_URL, NtfyServer

    for policy, text in (({}, "passage en orange/rouge, prévision orange/rouge"),
                         (RED_POLICY, "passage en rouge, prévision rouge")):
        server = NtfyServer()
        channel = NtfyChannel(NTFY_URL, NTFY_TOPIC, transport=httpx.MockTransport(server))
        assert await send_test_notification(make_settings(**policy), channel)
        assert text in server.requests[-1].content.decode("utf-8")
