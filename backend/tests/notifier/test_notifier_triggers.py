"""Notifier triggers: level transitions, hysteresis, forecast dedup, staleness, restart."""

from datetime import timedelta

from notifier_harness import T0

ORANGE, RED, YELLOW, GREEN = 40.0, 60.0, 20.0, 5.0


# --- observed level transitions ---------------------------------------------

async def test_notifier_rising_to_orange_then_red_is_immediate(harness):
    h = harness()
    assert await h.run(YELLOW) == []                     # green -> yellow: silent by default
    assert await h.run(ORANGE) == ["Lidoire : risque ORANGE (40/100)"]
    assert await h.run(ORANGE) == []                     # same level: nothing
    assert await h.run(RED) == ["Lidoire : risque ROUGE (60/100)"]
    assert h.state["level"] == "red"


async def test_notifier_first_run_in_orange_notifies(harness):
    h = harness()
    assert await h.run(45.0) == ["Lidoire : risque ORANGE (45/100)"]


async def test_notifier_yellow_transitions_configurable(harness):
    h = harness(notify_yellow=True)
    assert await h.run(YELLOW) == ["Lidoire : risque JAUNE (20/100)"]
    for _ in range(3):
        assert await h.run(GREEN) == []
    assert await h.run(GREEN) == ["Lidoire : retour en VERT (5/100)"]


async def test_notifier_same_score_is_processed_once(harness):
    h = harness()
    h.world.score_age_h = 0.0
    assert await h.run(ORANGE) == ["Lidoire : risque ORANGE (40/100)"]
    # No new stored score (risk_scorer late): the same point is not reprocessed
    h.world.score_age_h = 1.0
    assert await h.run(advance=1.0) == []


async def test_notifier_downgrade_waits_for_hold_time(harness):
    h = harness()                                        # hold = 3 h
    await h.run(ORANGE)
    assert await h.run(YELLOW) == []                     # t+0: pending
    assert await h.run(YELLOW) == []                     # t+1
    assert await h.run(YELLOW) == []                     # t+2
    assert h.state["pending_level"] == "yellow"
    assert await h.run(YELLOW) == ["Lidoire : retour en JAUNE (20/100)"]   # held 3 h
    assert h.state["level"] == "yellow" and h.state["pending_since"] is None
    for _ in range(4):                                   # yellow -> green: silent by default
        assert await h.run(GREEN) == []
    assert h.state["level"] == "green"


async def test_notifier_tide_flapping_around_orange_sends_once(harness):
    h = harness()
    sent = []
    for score in [40, 33, 38, 34, 36, 33, 37, 34, 39]:   # oscillation with the tide
        sent += await h.run(float(score))
    assert sent == ["Lidoire : risque ORANGE (40/100)"]
    assert h.state["level"] == "orange" and h.state["pending_since"] is None


async def test_notifier_downgrade_target_is_highest_level_during_hold(harness):
    h = harness()
    await h.run(RED)
    sent = []
    for score in [ORANGE, YELLOW, ORANGE, YELLOW]:
        sent += await h.run(score)
    assert sent == ["Lidoire : retour en ORANGE (20/100)"]
    assert h.state["level"] == "orange"


async def test_notifier_message_content_in_french(harness):
    h = harness()
    h.clock.advance()                                    # 13:00 UTC, high tide at 15:30 UTC
    h.world.score = RED
    await h.notifier().run_once()
    request = h.server.requests[-1]
    assert str(request.url) == "https://ntfy.example.test/lidoire-test-topic"
    body = request.content.decode("utf-8")
    assert "Le risque au moulin passe de VERT à ROUGE (score 60/100)." in body
    assert "Pessac-sur-Dordogne : 3,92 m, en hausse (+2,1 cm/h)" in body
    assert "Pluie 72 h : 48 mm" in body
    assert "Prochaine pleine mer (confluence) : 10/02 16:30, coef. 95" in body   # Europe/Paris (UTC+1)
    assert "Tableau de bord : http://lidoire.test" in body
    assert request.headers["Priority"] == "5"
    assert request.headers["Tags"] == "rotating_light"
    assert request.headers["Click"] == "http://lidoire.test"


# --- delivery failures and restarts ------------------------------------------

async def test_notifier_failed_send_is_retried_next_run(harness):
    h = harness()
    h.server.status = 500
    assert await h.run(ORANGE) == ["Lidoire : risque ORANGE (40/100)"]   # attempted
    assert h.state.get("level", "green") == "green"                       # not acknowledged
    h.server.status = 200
    assert await h.run(ORANGE) == ["Lidoire : risque ORANGE (40/100)"]
    assert h.state["level"] == "orange"
    assert await h.run(ORANGE) == []


async def test_notifier_restart_does_not_resend(harness):
    h = harness()
    h.world.forecast = lambda hour: 60.0 if 10 <= hour <= 20 else 5.0
    first = await h.run(ORANGE)
    assert len(first) == 2                               # observed orange + forecast red
    saved = h.store.value
    # Each run builds a new Notifier on the same store: like a backend restart
    assert await h.run(ORANGE) == []
    assert await h.run(ORANGE) == []
    assert h.store.value is not None and saved is not None


async def test_notifier_state_unavailable_skips_run(harness):
    h = harness()

    async def broken_load():
        raise ConnectionError("postgres down")

    h.store.load = broken_load
    assert await h.run(RED) == []


# --- forecast heads-up --------------------------------------------------------

async def test_notifier_forecast_headsup_once_per_event(harness):
    h = harness()
    h.world.forecast = lambda hour: 42.0 if 20 <= hour <= 30 else 10.0
    assert await h.run(GREEN) == ["Lidoire : risque ORANGE prévu"]
    for _ in range(5):                                   # still forecast: no repeat
        assert await h.run(GREEN) == []
    h.world.forecast = lambda hour: 58.0 if 20 <= hour <= 30 else 10.0
    assert await h.run(GREEN) == ["Lidoire : risque ROUGE prévu"]           # escalation
    h.world.forecast = lambda hour: 42.0 if 20 <= hour <= 30 else 10.0
    assert await h.run(GREEN) == []                      # de-escalation: silent


async def test_notifier_forecast_beyond_horizon_ignored(harness):
    h = harness()
    h.world.forecast = lambda hour: 70.0 if hour > 48 else 10.0
    assert await h.run(GREEN) == []
    assert h.state["forecast_level"] is None


async def test_notifier_forecast_clears_after_hold_and_rearms(harness):
    h = harness()
    alert = lambda hour: 42.0 if 20 <= hour <= 30 else 10.0  # noqa: E731
    calm = lambda hour: 20.0                                  # noqa: E731
    h.world.forecast = alert
    assert await h.run(GREEN) == ["Lidoire : risque ORANGE prévu"]
    h.world.forecast = calm
    assert await h.run(GREEN) == []
    h.world.forecast = alert                             # flaps back: still the same event
    assert await h.run(GREEN) == []
    h.world.forecast = calm
    for _ in range(3):
        assert await h.run(GREEN) == []
    assert await h.run(GREEN) == ["Lidoire : prévision revenue sous l'orange"]
    assert h.state["forecast_level"] is None
    h.world.forecast = alert                             # new event
    assert await h.run(GREEN) == ["Lidoire : risque ORANGE prévu"]


async def test_notifier_forecast_silent_when_already_observed(harness):
    h = harness()
    h.world.forecast = lambda hour: 45.0
    assert await h.run(ORANGE) == ["Lidoire : risque ORANGE (40/100)"]
    assert h.state["forecast_level"] == "orange"


async def test_notifier_forecast_body(harness):
    h = harness()
    h.world.forecast = lambda hour: {18: 39.0, 24: 57.0}.get(hour, 10.0)
    h.world.score = GREEN
    await h.notifier().run_once()
    body = h.server.requests[-1].content.decode("utf-8")
    assert body.startswith(
        "Prévision : score max 57/100 (ROUGE) dans 24 h vers 11/02 13:00, "
        "seuil orange atteint dans 18 h (horizon 48 h)."
    )


# --- staleness ----------------------------------------------------------------

async def test_notifier_station_stale_alert_and_recovery(harness):
    h = harness()
    await h.run(GREEN)
    h.world.station_age_h = 3.5
    assert await h.run(GREEN) == ["Lidoire : données Pessac-sur-Dordogne périmées"]
    h.world.station_age_h = 5.5
    assert await h.run(GREEN) == []                      # one alert only
    h.world.station_age_h = None                         # no point at all: still stale
    assert await h.run(GREEN) == []
    h.world.station_age_h = 0.2
    assert await h.run(GREEN) == ["Lidoire : données Pessac-sur-Dordogne rétablies"]
    assert await h.run(GREEN) == []


async def test_notifier_inputs_failure_counts_as_stale_station(harness):
    h = harness()
    h.world.fail_inputs = True
    assert "Lidoire : données Pessac-sur-Dordogne périmées" in await h.run(GREEN)


async def test_notifier_score_stale_alert_and_recovery(harness):
    h = harness()
    assert await h.run() == ["Lidoire : score de risque non calculé"]     # nothing stored yet
    assert await h.run() == []
    h.world.score = GREEN
    h.world.score_age_h = 2.5                            # stored, but too old
    assert await h.run() == []
    h.world.score_age_h = 0.1
    assert await h.run() == ["Lidoire : score de risque rétabli"]
    h.world.score_age_h = 2.5
    assert await h.run() == ["Lidoire : score de risque non calculé"]


async def test_notifier_state_is_json_roundtrip(harness):
    from app.services.notifier import NotifierState

    h = harness()
    await h.run(ORANGE)
    await h.run(YELLOW)
    state = NotifierState.from_json(h.store.value)
    assert state.pending_level == "yellow"
    assert state.pending_since == T0 + timedelta(hours=2, minutes=-6)
    assert NotifierState.from_json(state.to_json()) == state
    assert NotifierState.from_json("not json") == NotifierState()
