"""Tide phase lag: source times are shifted once, at collection, to the Lidoire confluence."""

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.config import get_settings
from app.models.schemas import TideType
from app.services.collectors import tide
from app.services.risk_core import to_utc_naive

MARINE_TIMES = [f"2026-10-24T{h:02d}:00" for h in range(16)]
MARINE_LEVELS = [0.83, 1.12, 1.07, 0.65, -0.06, -0.92, -1.69, -2.14,
                 -2.11, -1.56, -0.63, 0.37, 1.12, 1.53, 1.55, 1.18]
# Extremes of the raw series (Gironde mouth time): PM 01h, BM 07h, PM 14h
RAW_EXTREMES = [datetime(2026, 10, 24, 1), datetime(2026, 10, 24, 7), datetime(2026, 10, 24, 14)]


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


def _lag(source: str) -> timedelta:
    return timedelta(hours=get_settings().tide_phase_lag_hours[source])


def _port() -> str:
    return get_settings().tide_port_confluence


def test_lag_per_source_from_settings():
    lags = get_settings().tide_phase_lag_hours
    assert tide.tide_phase_lag_hours(tide.SOURCE_OPEN_METEO) == lags["open-meteo-marine"]
    assert tide.tide_phase_lag_hours(tide.SOURCE_SHOM) == lags["shom"]
    # The Gironde mouth is further from the mill than the Bordeaux port
    assert tide.tide_phase_lag_hours(tide.SOURCE_OPEN_METEO) > tide.tide_phase_lag_hours(tide.SOURCE_SHOM) > 0
    assert tide.tide_phase_lag_hours("unknown") == 0.0
    assert tide.tide_phase_lag_hours(None) == 0.0


@pytest.mark.parametrize("lag", [0.0, 4.0, 4.5, 6.0])
def test_extract_shifts_times_only(lag):
    raw = tide._extract_tides_from_levels(MARINE_TIMES, MARINE_LEVELS, lag_hours=0)
    shifted = tide._extract_tides_from_levels(MARINE_TIMES, MARINE_LEVELS, lag_hours=lag)
    assert [t.timestamp for t in raw] == RAW_EXTREMES
    assert [t.timestamp for t in shifted] == [ts + timedelta(hours=lag) for ts in RAW_EXTREMES]
    # Same events, heights and coefficients: only the phase moves
    assert [(t.tide_type, t.height_m, t.coefficient) for t in shifted] == [
        (t.tide_type, t.height_m, t.coefficient) for t in raw
    ]
    assert all(t.port == _port() for t in shifted)


def test_extract_default_lag_is_open_meteo_setting():
    tides = tide._extract_tides_from_levels(MARINE_TIMES, MARINE_LEVELS)
    assert [t.timestamp for t in tides] == [ts + _lag("open-meteo-marine") for ts in RAW_EXTREMES]


@respx.mock
async def test_current_tide_data_is_confluence_time(writes):
    respx.get(url__startswith=tide.OPEN_METEO_MARINE_URL).mock(
        return_value=httpx.Response(200, json={"hourly": {"time": MARINE_TIMES,
                                                          "sea_level_height_msl": MARINE_LEVELS}}))
    result = await tide.get_current_tide_data()
    lag = _lag("open-meteo-marine")
    assert result.port == _port()
    assert result.phase_lag_hours == lag.total_seconds() / 3600
    # now = 03h: the mouth PM of 01h has not reached the confluence yet
    assert result.next_high_tide.timestamp == RAW_EXTREMES[0] + lag
    # Stored points carry the shifted times under the confluence port
    assert sorted(to_utc_naive(w["timestamp"]) for w in writes) == [ts + lag for ts in RAW_EXTREMES]
    assert {w["tags"]["port"] for w in writes} == {_port()}


@respx.mock
async def test_shom_uses_its_own_lag(monkeypatch):
    monkeypatch.setattr(get_settings(), "shom_api_key", "test-key")
    respx.get(url__startswith="https://services.data.shom.fr/").mock(
        return_value=httpx.Response(200, json={"predictions": [
            {"datetime": "2026-10-24T10:00:00", "type": "PM", "height": 5.1, "coefficient": 90},
        ]}))
    tides = await tide.fetch_shom_api()
    assert tides[0].timestamp == datetime(2026, 10, 24, 10) + _lag("shom")
    assert tides[0].port == _port()
    assert tides[0].source == tide.SOURCE_SHOM


@respx.mock
async def test_range_storage_shifts_hourly_heights_and_widens_raw_window(writes):
    route = respx.get(url__startswith=tide.OPEN_METEO_MARINE_URL).mock(
        return_value=httpx.Response(200, json={"hourly": {"time": MARINE_TIMES,
                                                          "sea_level_height_msl": MARINE_LEVELS}}))
    stored = await tide.fetch_and_store_tides_range(datetime(2026, 10, 24, 2), datetime(2026, 10, 25))
    assert stored == len(MARINE_TIMES)
    lag = _lag("open-meteo-marine")
    # The raw request starts `lag` earlier so the requested window stays covered
    assert route.calls[0].request.url.params["start_date"] == "2026-10-23"
    hourly = sorted(to_utc_naive(w["timestamp"]) for w in writes if "tide_type" not in w["tags"])
    assert hourly[0] == datetime(2026, 10, 24, 0) + lag
    highs = sorted(
        to_utc_naive(w["timestamp"]) for w in writes if w["tags"].get("tide_type") == TideType.HIGH.value
    )
    assert highs == [RAW_EXTREMES[0] + lag, RAW_EXTREMES[2] + lag]
    assert {w["tags"]["port"] for w in writes} == {_port()}


async def test_readers_ignore_legacy_ports_without_lag(monkeypatch):
    from app.services import influx_client
    from app.services.risk_v2 import load_tide_rows

    queried = []

    async def fake_query_tides(port, start, end):
        queried.append(port)
        # Only legacy (unshifted) points are stored
        return [{"timestamp": start, "height_m": 5.0, "tide_type": "PM", "port": port}] if port == "BORDEAUX" else []

    monkeypatch.setattr(influx_client, "query_tides", fake_query_tides)
    assert await load_tide_rows(datetime(2026, 10, 24), datetime(2026, 10, 25)) == []
    assert queried == [_port()]
