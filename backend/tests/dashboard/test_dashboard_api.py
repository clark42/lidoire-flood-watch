"""Dashboard acceptance fixes: distances, UTC serialisation, Netatmo/soil honesty."""

import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from app.api import realtime as realtime_api
from app.api import timeline as timeline_api
from app.config import Settings, get_settings
from app.geo import haversine_km
from app.models.schemas import RiverLevel, TideData, TideType
from app.services import influx_client
from app.services.collectors import netatmo, vigicrues

BACKEND = pathlib.Path(__file__).resolve().parents[2]

# Haversine distances from the moulin (44.8759811, 0.0015091), rounded to 0.1 km
EXPECTED_KM = {
    "P577001001": 19.0,  # Libourne
    "P555001001": 7.6,   # Pessac-sur-Dordogne
    "P514001001": 38.1,  # Bergerac
    "P726151001": 10.2,  # Abzac
    "P704151001": 65.9,  # Perigueux
    "P846251001": 20.8,  # Coutras
    "P816251001": 49.2,  # Riberac
}


def test_dashboard_haversine_reference_distance():
    # Paris (Notre-Dame) -> Lyon (Bellecour): ~392 km
    assert haversine_km(48.8530, 2.3499, 45.7578, 4.8320) == pytest.approx(392, abs=2)


def test_dashboard_station_distances_computed_from_lat_lon():
    stations = {s["id"]: s for s in Settings(_env_file=None).hubeau_stations}
    assert {sid: s["distance_km"] for sid, s in stations.items()} == EXPECTED_KM


def test_dashboard_config_does_not_hard_code_distances():
    assert '"distance_km":' not in (BACKEND / "app" / "config.py").read_text()


def test_dashboard_timeline_sources_distances_and_netatmo_availability(monkeypatch):
    monkeypatch.setattr(netatmo, "is_authorised", lambda: False)
    app = FastAPI()
    app.include_router(timeline_api.router, prefix="/api/timeline")
    body = TestClient(app).get("/api/timeline/sources").json()

    assert {s["id"]: s["distance_km"] for s in body["river_stations"]} == EXPECTED_KM
    by_id = {s["id"]: s for s in body["rainfall_stations"]}
    assert by_id[timeline_api.OPEN_METEO_KEY]["available"] is True
    netatmo_ids = [k for k in by_id if k.startswith("netatmo-lidoire:")]
    assert netatmo_ids and all(by_id[k]["available"] is False for k in netatmo_ids)


def test_dashboard_netatmo_is_authorised_needs_credentials_and_token(monkeypatch):
    monkeypatch.setattr(netatmo, "_get_stored_tokens", lambda: {})
    monkeypatch.setattr(netatmo, "get_settings", lambda: Settings(_env_file=None))
    assert netatmo.is_authorised() is False

    configured = Settings(_env_file=None, netatmo_client_id="id", netatmo_client_secret="secret")
    monkeypatch.setattr(netatmo, "get_settings", lambda: configured)
    assert netatmo.is_authorised() is False  # no refresh token yet
    monkeypatch.setattr(netatmo, "_get_stored_tokens", lambda: {"refresh_token": "r"})
    assert netatmo.is_authorised() is True


# --- UTC serialisation -----------------------------------------------------

def test_dashboard_naive_datetimes_serialise_as_utc_with_offset():
    tide = TideData(timestamp=datetime(2026, 10, 27, 3, 0), port="BORDEAUX", tide_type=TideType.HIGH,
                    height_m=1.9)
    assert tide.model_dump(mode="json")["timestamp"] == "2026-10-27T03:00:00Z"
    assert jsonable_encoder(tide)["timestamp"] == "2026-10-27T03:00:00Z"
    # Python-mode dumps keep the (naive) datetime for internal code
    assert tide.model_dump()["timestamp"] == datetime(2026, 10, 27, 3, 0)


def test_dashboard_aware_datetimes_are_converted_to_utc():
    paris = timezone(timedelta(hours=1))
    tide = TideData(timestamp=datetime(2026, 10, 27, 4, 0, tzinfo=paris), port="BORDEAUX",
                    tide_type=TideType.HIGH, height_m=1.9)
    assert tide.model_dump(mode="json")["timestamp"] == "2026-10-27T03:00:00Z"


def test_dashboard_vigicrues_timestamps_are_utc():
    # Vigicrues states the offset (+00:00): 17:00 is UTC, i.e. 18:00 Europe/Paris
    # in winter, and the datum is the same as Hub'Eau (identical values).
    payload = {"Serie": {"ObssHydro": [
        {"DtObsHydro": "2026-10-26T16:00:00+00:00", "ResObsHydro": 1.2},
        {"DtObsHydro": "2026-10-26T17:00:00+00:00", "ResObsHydro": 2.43},
    ]}}
    levels = vigicrues.parse_observations(payload, "P577001001")
    assert [lv.timestamp for lv in levels] == [datetime(2026, 10, 26, 16), datetime(2026, 10, 26, 17)]
    assert levels[-1].model_dump(mode="json")["timestamp"] == "2026-10-26T17:00:00Z"
    assert isinstance(levels[-1], RiverLevel)


# --- Netatmo basin summary ---------------------------------------------------

@pytest.fixture
def no_sparkline(monkeypatch):
    async def _sparkline(hours):
        return []

    monkeypatch.setattr(realtime_api, "query_basin_rainfall_sparkline", _sparkline)


async def test_dashboard_basin_rainfall_unavailable_without_netatmo(monkeypatch, no_sparkline):
    async def _unavailable():
        return {"available": False, "stations_count": 0, "auth_url": "/api/auth/netatmo"}

    monkeypatch.setattr(realtime_api, "get_basin_rainfall", _unavailable)
    summary = await realtime_api.get_basin_rainfall_summary()
    assert summary.available is False
    assert summary.stations_count == 0


async def test_dashboard_basin_weighted_average_ignores_empty_positions(monkeypatch, no_sparkline):
    async def _aval_only():
        return {"available": True, "lidoire_stations": [
            {"position": "aval", "rain_24h_mm": 10.0, "rain_1h_mm": 1.0},
            {"position": "aval", "rain_24h_mm": 20.0, "rain_1h_mm": 0.0},
        ]}

    monkeypatch.setattr(realtime_api, "get_basin_rainfall", _aval_only)
    summary = await realtime_api.get_basin_rainfall_summary()
    assert summary.available is True
    assert summary.stations_count == 2
    # amont/milieu have no station: they must not count as 0 mm
    assert summary.weighted_average_24h_mm == pytest.approx(15.0)


# --- Soil ----------------------------------------------------------------------

def test_dashboard_soil_timeline_never_mixes_realtime_moisture():
    timeline_fields = {tf for _m, tf in influx_client._SOIL_REALTIME_FIELDS.values()}
    assert timeline_fields == {"et0_mm"}


def test_dashboard_settings_cache_still_has_distances():
    assert get_settings().hubeau_stations[0]["distance_km"] > 0
