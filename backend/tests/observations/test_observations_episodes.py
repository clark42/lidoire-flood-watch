"""Observations inside an episode window and the suggested (never applied) outcome."""

from datetime import datetime

from app.models.schemas import EpisodeOutcome, FloodEpisode, Observation, ObservationKind
from app.services import observations as store


def _episode(**fields) -> FloodEpisode:
    base = {"id": 7, "start_time": datetime(2026, 2, 12, 0), "peak_time": datetime(2026, 2, 13, 12),
            "end_time": datetime(2026, 2, 14, 6), "severity": 4}
    return FloodEpisode(**{**base, **fields})


def _obs(obs_id: int, at: datetime, kind: str, height=None) -> Observation:
    return Observation(id=obs_id, observed_at=at, kind=ObservationKind(kind), water_height_cm=height,
                       created_at=at, source="user")


def test_observations_episode_window_margin():
    assert store.episode_window(_episode(), margin_hours=12) == (datetime(2026, 2, 11, 12), datetime(2026, 2, 14, 18))
    # No end: the peak, then the start bound the window
    assert store.episode_window(_episode(end_time=None), margin_hours=0)[1] == datetime(2026, 2, 13, 12)
    assert store.episode_window(_episode(end_time=None, peak_time=None), margin_hours=1)[1] == datetime(2026, 2, 12, 1)


def test_observations_in_window_sorted():
    window = (datetime(2026, 2, 11, 12), datetime(2026, 2, 14, 18))
    obs = [
        _obs(1, datetime(2026, 2, 14, 18), "flooded"),
        _obs(2, datetime(2026, 2, 11, 11, 59), "normal"),
        _obs(3, datetime(2026, 2, 12, 8), "water_rising"),
    ]
    assert [o.id for o in store.observations_in_window(obs, window)] == [3, 1]


def test_observations_suggestion_prefers_flooded_and_max_height():
    obs = [
        _obs(1, datetime(2026, 2, 13, 8), "near_miss"),
        _obs(2, datetime(2026, 2, 13, 10), "flooded", 5),
        _obs(3, datetime(2026, 2, 13, 14), "flooded", 18),
    ]
    s = store.suggest_outcome(_episode(), obs)
    assert s.outcome == EpisodeOutcome.FLOODED and s.observation_id == 3
    assert s.water_height_cm == 18 and s.observed_at == datetime(2026, 2, 13, 14)


def test_observations_suggestion_near_miss_and_none():
    s = store.suggest_outcome(_episode(), [_obs(1, datetime(2026, 2, 13), "near_miss", 3)])
    assert s.outcome == EpisodeOutcome.NEAR_MISS and s.water_height_cm is None
    assert store.suggest_outcome(_episode(), [_obs(1, datetime(2026, 2, 13), "water_rising")]) is None
    # Episode already labelled: nothing offered
    assert store.suggest_outcome(_episode(outcome=EpisodeOutcome.FALSE_POSITIVE),
                                 [_obs(1, datetime(2026, 2, 13), "flooded")]) is None


def test_observations_episode_route_lists_and_suggests_without_applying(client, auth, fake_store):
    fake_store.episodes[7] = _episode()
    fake_store.add_row(observed_at=datetime(2026, 2, 13, 9), kind="flooded", water_height_cm=10, reference="plancher")
    fake_store.add_row(observed_at=datetime(2026, 2, 20), kind="flooded")  # outside the window
    body = client.get("/api/episodes/7/observations").json()
    assert body["episode_id"] == 7
    assert body["window_start"] == "2026-02-11T12:00:00Z" and body["window_end"] == "2026-02-14T18:00:00Z"
    assert [o["id"] for o in body["observations"]] == [1]
    assert body["suggested_outcome"]["outcome"] == "flooded"
    assert body["suggested_outcome"]["observed_at"] == "2026-02-13T09:00:00Z"
    # Read-only: the episode keeps no outcome
    assert fake_store.episodes[7].outcome is None

    fake_store.episodes[8] = _episode(id=8, outcome=EpisodeOutcome.FLOODED)
    assert client.get("/api/episodes/8/observations").json()["suggested_outcome"] is None
    assert client.get("/api/episodes/99/observations").status_code == 404


def test_observations_episode_route_does_not_shadow_episode_routes():
    from fastapi.testclient import TestClient

    from app.main import create_app

    paths = set(TestClient(create_app()).get("/openapi.json").json()["paths"])
    assert {"/api/episodes/{episode_id}", "/api/episodes/{episode_id}/observations"} <= paths
