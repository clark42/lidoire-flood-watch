"""Tide collector: estimated tides are flagged and never stored."""

from datetime import datetime

import httpx
import pytest
import respx

from app.models.schemas import TideType
from app.services.collectors import tide


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(tide, "_tide_cache", {})
    monkeypatch.setattr(tide, "_cache_expiry", None)
    monkeypatch.setattr(tide, "_utcnow", lambda: datetime(2026, 10, 24, 3, 0))


@pytest.fixture
def writes(monkeypatch):
    from app.services import influx_client

    calls = []

    async def fake_write_points_batch(points, chunk_size=5000):
        calls.extend(influx_client.point_as_dict(p) for p in points)
        return len(points)

    monkeypatch.setattr(influx_client, "write_points_batch", fake_write_points_batch)
    return calls


MARINE_TIMES = [f"2026-10-24T{h:02d}:00" for h in range(16)]
MARINE_LEVELS = [0.83, 1.12, 1.07, 0.65, -0.06, -0.92, -1.69, -2.14,
                 -2.11, -1.56, -0.63, 0.37, 1.12, 1.53, 1.55, 1.18]


def test_extract_tides_range_based_coefficient():
    tides = tide._extract_tides_from_levels(MARINE_TIMES, MARINE_LEVELS, lag_hours=0)
    types = [(t.timestamp.hour, t.tide_type) for t in tides]
    assert types == [(1, TideType.HIGH), (7, TideType.LOW), (14, TideType.HIGH)]
    # PM at 14h: range 1.55 - (-2.14) = 3.69 m -> ~74 (not clamped to 30)
    assert tides[2].coefficient == 74
    assert tides[1].coefficient is None
    assert all(t.source == "open-meteo-marine" for t in tides)


@respx.mock
async def test_no_source_returns_estimated_and_stores_nothing(writes):
    respx.get(url__startswith=tide.OPEN_METEO_MARINE_URL).mock(return_value=httpx.Response(500))

    result = await tide.get_current_tide_data()
    assert result.available is False
    assert result.source == "estimated"
    assert result.tides and all(t.source == "estimated" for t in result.tides)
    assert writes == []

    assert await tide.collect_and_store_tides() == 0
    assert writes == []


@respx.mock
async def test_open_meteo_marine_is_available_and_stored(writes):
    respx.get(url__startswith=tide.OPEN_METEO_MARINE_URL).mock(
        return_value=httpx.Response(200, json={"hourly": {"time": MARINE_TIMES,
                                                          "sea_level_height_msl": MARINE_LEVELS}}))
    result = await tide.get_current_tide_data()
    assert result.available is True
    assert result.source == "open-meteo-marine"
    assert len(writes) == 3


async def test_store_skips_estimated(writes):
    assert await tide.store_tides_to_influx(tide._generate_estimated_tides()) == 0
    assert writes == []
