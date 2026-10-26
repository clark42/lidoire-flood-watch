"""Shared frozen scenario and fixtures for risk tests (no network, no database)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from app.config import get_settings
from app.models.schemas import (
    MultiStationData,
    RainfallSummary,
    RiverLevel,
    StationLevel,
    Tendency,
    TideData,
    TideForecast,
    TideType,
)
from app.services import risk_core

T0 = datetime(2024, 2, 10, 12, 0, 0)

# station_id -> (height at T0, rate cm/h)
STATION_STATE = {
    "P577001001": (5.0, 8.0),    # Libourne: tidal, weight 0, must be ignored
    "P555001001": (3.2, 3.0),    # Pessac (primary): yellow (3.0-3.8), rising
    "P514001001": (3.0, 0.0),    # Bergerac: yellow (2.5-3.5), stable
    "P726151001": (2.4, 2.0),    # Abzac: yellow (2.2-3.0), rising
    "P704151001": (1.5, -2.0),   # Perigueux: falling
    "P846251001": (1.6, 1.5),    # Coutras: rising
    "P816251001": (0.8, 0.5),    # Riberac: stable
}


def station_cfg(station_id: str) -> dict:
    return next(s for s in get_settings().hubeau_stations if s["id"] == station_id)


def level_series(height: float, rate_cm_h: float, t0: datetime = T0, hours_before: float = 6.0,
                 step_min: int = 15, hours_after: float = 6.0) -> list[tuple[datetime, float]]:
    """Linear level series around t0 (includes FUTURE points after t0)."""
    pts = []
    t = t0 - timedelta(hours=hours_before)
    while t <= t0 + timedelta(hours=hours_after):
        dt_h = (t - t0).total_seconds() / 3600
        pts.append((t, height + rate_cm_h / 100.0 * dt_h))
        t += timedelta(minutes=step_min)
    return pts


def rain_hourly_frozen(t0: datetime = T0) -> dict[datetime, float]:
    """Hourly rain keyed by hour end: 0.2 mm/h last 24h, 0.1 next 48h, 0.05 to 7d."""
    out = {}
    for k in range(0, 168):
        if k < 24:
            mm = 0.2
        elif k < 72:
            mm = 0.1
        else:
            mm = 0.05
        out[t0 - timedelta(hours=k)] = mm
    return out


@dataclass
class Scenario:
    t0: datetime
    station_points: dict[str, list[tuple[datetime, float]]]
    rain_hourly: dict[datetime, float]
    rainfall: RainfallSummary
    stations_data: MultiStationData
    tide: TideForecast
    tide_rows: list[dict] = field(default_factory=list)


def build_scenario(t0: datetime = T0, forecast_24h: float = 0.0, forecast_72h: float = 0.0) -> Scenario:
    station_points = {
        sid: level_series(h, r, t0) for sid, (h, r) in STATION_STATE.items()
    }
    stations = []
    for sid, (h, r) in STATION_STATE.items():
        cfg = station_cfg(sid)
        stations.append(StationLevel(
            station_id=sid,
            station_name=cfg["name"],
            river=cfg["river"],
            current_level=RiverLevel(
                timestamp=t0, station_id=sid, station_name=cfg["name"],
                river=cfg["river"], height_m=h,
            ),
            tendency=risk_core.tendency_from_rate(r),
            rate_cm_per_hour=r,
            distance_km=cfg["distance_km"],
            delay_hours=cfg["delay_hours"],
            weight=cfg["weight"],
            threshold_yellow=cfg["thresholds"]["yellow"],
            threshold_orange=cfg["thresholds"]["orange"],
            threshold_red=cfg["thresholds"]["red"],
        ))
    stations_data = MultiStationData(
        timestamp=t0, stations=stations, primary_station_id=get_settings().hubeau_primary_station,
    )

    rain_hourly = rain_hourly_frozen(t0)
    last_24h = sum(v for k, v in rain_hourly.items() if k > t0 - timedelta(hours=24))
    last_72h = sum(v for k, v in rain_hourly.items() if k > t0 - timedelta(hours=72))
    last_7d = sum(rain_hourly.values())
    rainfall = RainfallSummary(
        last_hour_mm=1.0,
        last_24h_mm=last_24h,
        last_72h_mm=last_72h,
        last_7d_mm=last_7d,
        forecast_24h_mm=forecast_24h,
        forecast_72h_mm=forecast_72h,
    )

    prev_high = t0 - timedelta(hours=10, minutes=25)
    next_high = t0 + timedelta(hours=2)
    next_low = t0 + timedelta(hours=8, minutes=12)
    following_high = t0 + timedelta(hours=14, minutes=25)
    tide = TideForecast(
        port="LIDOIRE",
        next_high_tide=TideData(timestamp=next_high, port="LIDOIRE", tide_type=TideType.HIGH,
                                height_m=5.6, coefficient=95),
        next_low_tide=TideData(timestamp=next_low, port="LIDOIRE", tide_type=TideType.LOW,
                               height_m=0.8),
        # As computed by collectors/tide.py: coefficient of the latest past high
        current_coefficient=93,
        recent_tides=[
            TideData(timestamp=prev_high, port="LIDOIRE", tide_type=TideType.HIGH, height_m=5.5, coefficient=93),
        ],
        tides=[
            TideData(timestamp=next_high, port="LIDOIRE", tide_type=TideType.HIGH, height_m=5.6, coefficient=95),
            TideData(timestamp=next_low, port="LIDOIRE", tide_type=TideType.LOW, height_m=0.8),
            TideData(timestamp=following_high, port="LIDOIRE", tide_type=TideType.HIGH, height_m=5.7, coefficient=97),
        ],
    )
    tide_rows = [
        {"timestamp": prev_high, "height_m": 5.5, "coefficient": None, "tide_type": "PM", "port": "LIDOIRE"},
        {"timestamp": prev_high, "height_m": None, "coefficient": 93, "tide_type": "PM", "port": "LIDOIRE"},
        {"timestamp": next_high, "height_m": 5.6, "coefficient": None, "tide_type": "PM", "port": "LIDOIRE"},
        {"timestamp": next_high, "height_m": None, "coefficient": 95, "tide_type": "PM", "port": "LIDOIRE"},
        {"timestamp": next_low, "height_m": 0.8, "coefficient": None, "tide_type": "BM", "port": "LIDOIRE"},
    ]
    return Scenario(t0, station_points, rain_hourly, rainfall, stations_data, tide, tide_rows)


@pytest.fixture
def scenario() -> Scenario:
    return build_scenario()


@pytest.fixture(autouse=True)
def _no_influx_writes(monkeypatch):
    """Fail loudly if any test reaches the real InfluxDB write API."""
    from app.services import influx_client

    def _boom():
        raise AssertionError("real InfluxDB write API used in a test")

    monkeypatch.setattr(influx_client, "get_write_api", _boom)
    monkeypatch.setattr(influx_client, "get_query_api", _boom)
