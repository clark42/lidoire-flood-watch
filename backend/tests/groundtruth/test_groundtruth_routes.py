"""Episodes API: route order, confirm semantics, labels and detect endpoints.

The router is mounted on a bare FastAPI app (no lifespan, no scheduler) and
every DB-backed function is replaced by an in-memory fake store.
"""

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import episodes as episodes_api
from app.models.schemas import FloodEpisode, FloodEpisodeCreate


class FakeStore:
    """In-memory replacement of the data_store episode functions."""

    def __init__(self):
        self.rows: dict[int, FloodEpisode] = {}

    def add(self, **fields) -> FloodEpisode:
        ep_id = len(self.rows) + 1
        ep = FloodEpisode(id=ep_id, created_at=datetime(2026, 1, 1), **fields)
        self.rows[ep_id] = ep
        return ep

    async def get_all_episodes(self, limit=50, offset=0, min_severity=None):
        return list(self.rows.values())[offset:offset + limit]

    async def get_episode_by_id(self, episode_id):
        return self.rows.get(episode_id)

    async def update_episode(self, episode_id, data: FloodEpisodeCreate):
        # Mirror data_store.update_episode: ground-truth fields only if sent
        current = self.rows[episode_id].model_dump()
        base = data.model_dump(exclude={"outcome", "observed_at", "water_height_cm", "outcome_source"})
        current.update(base)
        for field in ("outcome", "observed_at", "water_height_cm", "outcome_source"):
            if field in data.model_fields_set:
                current[field] = getattr(data, field)
        self.rows[episode_id] = FloodEpisode(**current)
        return self.rows[episode_id]


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    for name in ("get_all_episodes", "get_episode_by_id", "update_episode"):
        monkeypatch.setattr(episodes_api, name, getattr(fake, name))
    return fake


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(episodes_api.router, prefix="/api/episodes")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Route order
# ---------------------------------------------------------------------------


def test_groundtruth_dynamic_routes_declared_after_static_ones():
    paths = [r.path for r in episodes_api.router.routes]
    first_dynamic = min(i for i, p in enumerate(paths) if "{" in p)
    static_after = [p for p in paths[first_dynamic:] if "{" not in p]
    assert static_after == [], f"static routes shadowed by path params: {static_after}"


def test_groundtruth_patterns_route_reachable(client, store):
    # With no episodes the endpoint answers "insufficient_data" (no analysis call).
    # Before the fix, GET /{episode_id} caught it and returned 422.
    resp = client.get("/api/episodes/patterns")
    assert resp.status_code == 200
    assert resp.json()["status"] == "insufficient_data"


def test_groundtruth_station_extremes_route_reachable(client, monkeypatch):
    async def fake_max_levels(start, end):
        return {"P555001001": {"max_m": 5.1}}

    monkeypatch.setattr(episodes_api, "get_station_max_levels", fake_max_levels)
    resp = client.get(
        "/api/episodes/station-extremes",
        params={"start_date": "2024-01-01T00:00:00", "end_date": "2024-12-31T00:00:00"},
    )
    assert resp.status_code == 200
    assert resp.json()["stations"]["P555001001"]["max_m"] == 5.1


def test_groundtruth_labels_route_reachable(client):
    resp = client.get("/api/episodes/labels")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["episodes"]) == 14
    assert body["exhaustive"] is False
    assert {"flooded", "false_positive", "near_miss", "unknown"} >= {
        e["outcome"] for e in body["episodes"]
    }


def test_groundtruth_backfill_status_route_reachable(client):
    resp = client.get("/api/episodes/backfill/status")
    assert resp.status_code == 200
    assert "running" in resp.json()


def test_groundtruth_get_by_id_still_works(client, store):
    store.add(start_time=datetime(2024, 2, 25), severity=4)
    assert client.get("/api/episodes/1").status_code == 200
    assert client.get("/api/episodes/99").status_code == 404


# ---------------------------------------------------------------------------
# Confirm endpoint
# ---------------------------------------------------------------------------


def test_groundtruth_confirm_sets_structured_outcome(client, store):
    store.add(start_time=datetime(2024, 2, 25), severity=3, notes="eau au seuil")
    resp = client.post(
        "/api/episodes/confirm/1",
        params={
            "outcome": "flooded",
            "actual_severity": 5,
            "water_height_cm": 25,
            "observed_at": "2024-02-26T12:00:00",
            "source": "owner",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    ep = body["episode"]
    assert ep["outcome"] == "flooded"
    assert ep["severity"] == 5
    assert ep["water_height_cm"] == 25
    assert ep["outcome_source"] == "owner"
    assert ep["observed_at"].startswith("2024-02-26T12:00")
    assert ep["notes"] == "eau au seuil"
    assert "[CONFIRMED" not in ep["notes"]


def test_groundtruth_reconfirm_replaces_and_requalifies(client, store):
    store.add(start_time=datetime(2024, 2, 25), severity=3, notes="eau au seuil")
    client.post("/api/episodes/confirm/1", params={"was_flooded": "true", "water_height_cm": 10})
    resp = client.post("/api/episodes/confirm/1", params={"was_flooded": "false"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    ep = body["episode"]
    assert ep["outcome"] == "false_positive"
    assert ep["water_height_cm"] is None  # cleared for a false positive
    assert ep["notes"] == "eau au seuil"  # never stacked / duplicated


def test_groundtruth_confirm_notes_replace_and_strip_legacy(client, store):
    store.add(
        start_time=datetime(2024, 2, 25),
        severity=3,
        notes="[CONFIRMED: FLOODED] vieux\nvieux",
    )
    resp = client.post(
        "/api/episodes/confirm/1",
        params={"outcome": "near_miss", "notes": "[CONFIRMED: FLOODED] monte jusqu'au seuil"},
    )
    ep = resp.json()["episode"]
    assert resp.json()["status"] == "near_miss"
    assert ep["outcome"] == "near_miss"
    assert ep["notes"] == "monte jusqu'au seuil"

    # Without notes, existing notes are kept (and legacy prefixes stripped)
    resp = client.post("/api/episodes/confirm/1", params={"outcome": "unknown"})
    assert resp.json()["episode"]["notes"] == "monte jusqu'au seuil"


def test_groundtruth_confirm_requires_outcome(client, store):
    store.add(start_time=datetime(2024, 2, 25), severity=3)
    assert client.post("/api/episodes/confirm/1").status_code == 422
    assert client.post("/api/episodes/confirm/1", params={"outcome": "maybe"}).status_code == 422


def test_groundtruth_confirm_outcome_wins_over_was_flooded(client, store):
    store.add(start_time=datetime(2024, 2, 25), severity=3)
    resp = client.post(
        "/api/episodes/confirm/1", params={"outcome": "near_miss", "was_flooded": "true"}
    )
    assert resp.json()["episode"]["outcome"] == "near_miss"


def test_groundtruth_confirm_unknown_episode_404(client, store):
    assert client.post("/api/episodes/confirm/42", params={"outcome": "flooded"}).status_code == 404


# ---------------------------------------------------------------------------
# Detect threshold
# ---------------------------------------------------------------------------


def test_groundtruth_detect_threshold_rejected_when_unsupported(client, monkeypatch):
    calls = []

    async def detector(start_date=None, end_date=None):
        calls.append((start_date, end_date))
        return {"episodes_count": 0, "episodes": []}

    monkeypatch.setattr(episodes_api, "run_episode_detection", detector)
    assert client.post("/api/episodes/detect", params={"threshold": 45}).status_code == 400
    assert calls == []
    assert client.post("/api/episodes/detect").status_code == 200
    assert len(calls) == 1


def test_groundtruth_detect_threshold_forwarded_when_supported(client, monkeypatch):
    seen = {}

    async def detector(start_date=None, end_date=None, threshold=None):
        seen["threshold"] = threshold
        return {"episodes_count": 0, "episodes": []}

    monkeypatch.setattr(episodes_api, "run_episode_detection", detector)
    resp = client.post("/api/episodes/detect", params={"threshold": 45})
    assert resp.status_code == 200
    assert seen["threshold"] == 45
