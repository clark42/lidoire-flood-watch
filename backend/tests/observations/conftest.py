"""Observations API on a bare app with an in-memory store (no database)."""

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import router as api_router
from app.api import observations as observations_api
from app.config import Settings
from app.models.orm import ObservationRow
from app.services import observations as store

TEST_API_KEY = "test-key-0123456789abcdef"


class FakeObservationStore:
    """Replaces the database functions of services/observations.py."""

    def __init__(self):
        self.rows: dict[int, ObservationRow] = {}
        self.episodes: dict = {}

    def add_row(self, **fields) -> ObservationRow:
        row_id = len(self.rows) + 1
        defaults = {"kind": "normal", "source": "user", "created_at": datetime(2026, 10, 26, 12, 0)}
        row = ObservationRow(id=row_id, **{**defaults, **fields})
        self.rows[row_id] = row
        return row

    async def create_observation(self, data, photo_name=None):
        row = self.add_row(
            observed_at=store.to_naive_utc(data.observed_at) if data.observed_at else datetime(2026, 10, 26, 12, 0),
            kind=data.kind.value,
            water_height_cm=data.water_height_cm,
            reference=data.reference,
            note=data.note,
            photo_path=photo_name,
            source=data.source,
        )
        return store.row_to_observation(row)

    async def list_observations(self, start=None, end=None, limit=500):
        rows = [
            r for r in self.rows.values()
            if (start is None or r.observed_at >= store.to_naive_utc(start))
            and (end is None or r.observed_at <= store.to_naive_utc(end))
        ]
        rows.sort(key=lambda r: (r.observed_at, r.id), reverse=True)
        return [store.row_to_observation(r) for r in rows[:limit]]

    async def get_observation_photo_name(self, observation_id):
        row = self.rows.get(observation_id)
        return (False, None) if row is None else (True, row.photo_path)

    async def delete_observation(self, observation_id):
        row = self.rows.pop(observation_id, None)
        if row is None:
            return False
        store.delete_photo(row.photo_path)
        return True

    async def get_episode_by_id(self, episode_id):
        return self.episodes.get(episode_id)


@pytest.fixture
def settings(monkeypatch, tmp_path):
    current = {"value": Settings(
        _env_file=None, api_key=TEST_API_KEY, observations_photo_dir=str(tmp_path / "observations"),
    )}

    def _get():
        return current["value"]

    for target in ("app.api.deps", "app.api.observations", "app.services.observations"):
        monkeypatch.setattr(f"{target}.get_settings", _get)

    def update(**overrides):
        current["value"] = current["value"].model_copy(update=overrides)
        return current["value"]

    update.current = current
    return update


@pytest.fixture
def fake_store(monkeypatch, settings):
    fake = FakeObservationStore()
    for name in ("create_observation", "list_observations", "get_observation_photo_name", "delete_observation"):
        monkeypatch.setattr(store, name, getattr(fake, name))
    monkeypatch.setattr(observations_api, "get_episode_by_id", fake.get_episode_by_id)
    return fake


@pytest.fixture
def client(fake_store) -> TestClient:
    app = FastAPI()
    app.include_router(api_router, prefix="/api")  # real router: API key dependency included
    return TestClient(app)


@pytest.fixture
def auth() -> dict:
    return {"X-API-Key": TEST_API_KEY}
