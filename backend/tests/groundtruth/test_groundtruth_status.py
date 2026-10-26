"""Derived episode status: an episode without end is not "ongoing" forever."""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import episodes as episodes_api
from app.models.schemas import EpisodeOutcome, EpisodeStatus, FloodEpisode
from app.services.data_store import episode_status

NOW = datetime(2026, 10, 26, 19, 0)
FRESH = NOW - timedelta(minutes=40)


def _ep(start, peak=None, end=None, **kw) -> FloodEpisode:
    return FloodEpisode(start_time=start, peak_time=peak, end_time=end, severity=3, **kw)


@pytest.mark.parametrize("episode, last_live, expected", [
    # Episode 14 in prod: peak 2026-01-28, no end_time, still shown "EN COURS"
    (_ep(datetime(2026, 1, 4), datetime(2026, 1, 28, 6)), FRESH, EpisodeStatus.CLOSED),
    # End in the past
    (_ep(NOW - timedelta(hours=30), NOW - timedelta(hours=20), NOW - timedelta(hours=1)), FRESH, EpisodeStatus.CLOSED),
    # Recent, no end, live data flowing
    (_ep(NOW - timedelta(hours=30), NOW - timedelta(hours=6)), FRESH, EpisodeStatus.ONGOING),
    # Recent, no end, but the live data stopped (or is unknown)
    (_ep(NOW - timedelta(hours=30), NOW - timedelta(hours=6)), NOW - timedelta(hours=5), EpisodeStatus.CLOSED),
    (_ep(NOW - timedelta(hours=30), NOW - timedelta(hours=6)), None, EpisodeStatus.CLOSED),
    # End in the future: ongoing while data flows, closed once it stops
    (_ep(NOW - timedelta(days=10), end=NOW + timedelta(hours=6)), FRESH, EpisodeStatus.ONGOING),
    (_ep(NOW - timedelta(days=10), end=NOW + timedelta(hours=6)), NOW - timedelta(days=1), EpisodeStatus.CLOSED),
    # No peak: the start is the reference
    (_ep(NOW - timedelta(hours=73)), FRESH, EpisodeStatus.CLOSED),
    (_ep(NOW - timedelta(hours=70)), FRESH, EpisodeStatus.ONGOING),
])
def test_groundtruth_episode_status(episode, last_live, expected):
    assert episode_status(episode, NOW, last_live) == expected


@pytest.fixture
def client(monkeypatch):
    episodes = {
        14: _ep(datetime(2026, 1, 4), datetime(2026, 1, 28, 6), id=14, outcome=EpisodeOutcome.UNKNOWN),
        15: _ep(NOW - timedelta(hours=20), NOW - timedelta(hours=4), id=15),
    }
    calls = []

    async def get_all(limit=50, offset=0, min_severity=None):
        return list(episodes.values())

    async def get_one(episode_id):
        return episodes.get(episode_id)

    async def last_live():
        calls.append(1)
        return FRESH

    monkeypatch.setattr(episodes_api, "get_all_episodes", get_all)
    monkeypatch.setattr(episodes_api, "get_episode_by_id", get_one)
    monkeypatch.setattr(episodes_api, "last_live_risk_score_at", last_live)
    monkeypatch.setattr(episodes_api, "_utcnow", lambda: NOW)
    app = FastAPI()
    app.include_router(episodes_api.router, prefix="/api/episodes")
    c = TestClient(app)
    c.calls = calls
    return c


def test_groundtruth_episodes_list_exposes_status(client):
    body = client.get("/api/episodes").json()
    assert {e["id"]: e["status"] for e in body} == {14: "closed", 15: "ongoing"}
    assert client.calls == [1]  # live data read once for the whole list


def test_groundtruth_old_episode_does_not_read_live_data(client):
    assert client.get("/api/episodes/14").json()["status"] == "closed"
    assert client.calls == []
    assert client.get("/api/episodes/15").json()["status"] == "ongoing"
