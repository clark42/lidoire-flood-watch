"""Tides: no duplicate timestamps (write and read), longer horizon, explicit forecast degrade."""

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.config import get_settings
from app.models.schemas import TideData, TideType
from app.services import forecast, influx_client
from app.services.collectors import tide
from app.services.forecast import ForecastInputs, build_forecast, build_forecast_features
from app.services.risk_core import StationFeature

UTC = timezone.utc


@pytest.fixture
def writes(monkeypatch):
    calls = []

    async def fake_write_points_batch(points, chunk_size=5000):
        calls.extend(influx_client.point_as_dict(p) for p in points)
        return len(points)

    monkeypatch.setattr(influx_client, "write_points_batch", fake_write_points_batch)
    return calls


def _series_key(point: dict) -> tuple:
    """Influx identity of a point: measurement + tag set + time."""
    return point["measurement"], tuple(sorted(point["tags"].items())), point["timestamp"]


TIMES = [f"2026-10-24T{h:02d}:00" for h in range(16)]
LEVELS = [0.83, 1.12, 1.07, 0.65, -0.06, -0.92, -1.69, -2.14,
          -2.11, -1.56, -0.63, 0.37, 1.12, 1.53, 1.55, 1.18]


@respx.mock
async def test_tide_range_stores_one_point_per_timestamp(writes):
    respx.get(url__startswith=tide.OPEN_METEO_MARINE_URL).mock(
        return_value=httpx.Response(200, json={"hourly": {"time": TIMES, "sea_level_height_msl": LEVELS}}))
    stored = await tide.fetch_and_store_tides_range(datetime(2026, 10, 24, 6), datetime(2026, 10, 24, 12))
    assert stored == len(writes) == len(TIMES)  # 13 hourly heights + 3 PM/BM events
    per_time = Counter(w["timestamp"] for w in writes)
    assert max(per_time.values()) == 1  # PM/BM hours are not written twice
    events = [w for w in writes if "tide_type" in w["tags"]]
    assert [(w["timestamp"].hour, w["tags"]["tide_type"]) for w in events] == [(7, "PM"), (13, "BM"), (20, "PM")]


async def test_tide_store_dedupes_events_of_one_batch(writes):
    t = datetime(2026, 10, 24, 14)
    tides = [
        TideData(timestamp=t, port="LIDOIRE", tide_type=TideType.HIGH, height_m=1.5, coefficient=70, source="shom"),
        TideData(timestamp=t.replace(tzinfo=UTC), port="LIDOIRE", tide_type=TideType.HIGH, height_m=1.6,
                 coefficient=72, source="shom"),
        TideData(timestamp=t, port="LIDOIRE", tide_type=TideType.LOW, height_m=-2.0, source="shom"),
    ]
    assert await tide.store_tides_to_influx(tides) == 2
    assert len({_series_key(w) for w in writes}) == 2
    assert next(w for w in writes if w["tags"]["tide_type"] == "PM")["fields"]["coefficient"] == 72


def test_tide_extract_ignores_trailing_nulls():
    # Beyond the model horizon Open-Meteo returns nulls: the last value of a
    # rising series must not become a high tide
    times = [f"2026-10-24T{h:02d}:00" for h in range(8)]
    levels = [-2.0, -1.0, 0.0, 1.0, 1.5, None, None, None]
    assert tide._extract_tides_from_levels(times, levels, lag_hours=0) == []


@respx.mock
async def test_tide_collector_requests_the_longer_horizon(monkeypatch):
    route = respx.get(url__startswith=tide.OPEN_METEO_MARINE_URL).mock(
        return_value=httpx.Response(200, json={"hourly": {"time": TIMES, "sea_level_height_msl": LEVELS}}))
    await tide.fetch_open_meteo_marine()
    assert get_settings().tide_forecast_days == 10
    assert route.calls.last.request.url.params["forecast_days"] == "10"


def test_tide_inputs_ignore_duplicate_heights():
    from app.services.risk_v2 import tide_inputs_from_records

    t = datetime(2026, 10, 24, 12)
    rows = [{"timestamp": t + timedelta(hours=k), "height_m": h, "tide_type": None}
            for k, h in enumerate([0.0, 1.0, 2.0, 1.0, 0.0])]
    dup = rows + [dict(rows[2])]
    assert tide_inputs_from_records(dup) == tide_inputs_from_records(rows) == ([(t + timedelta(hours=2), None)], [])


# --- forecast beyond the tide horizon ----------------------------------------

T0 = datetime(2026, 10, 26, 19, 0)


def _inputs(last_high_h: float) -> ForecastInputs:
    pessac = StationFeature("P555001001", "Pessac", "Dordogne", 3.9, 0.0, 0.30, (3.0, 3.8, 4.5))
    first = T0 - timedelta(hours=5)
    highs = []
    t = first
    while t <= T0 + timedelta(hours=last_high_h):
        highs.append((t, 95))
        t += timedelta(hours=12, minutes=25)
    return ForecastInputs(
        t0=T0, stations=(pessac,), primary_station_id="P555001001",
        station_delays={"P555001001": 4.0}, rain_hourly={},
        tide_highs=highs, soil_saturation_factor=1.0, westerly_wind_kmh=0.0,
        primary_levels=((T0 - timedelta(hours=1), 3.9),),
    )


def test_forecast_tide_unavailable_beyond_horizon():
    fi = _inputs(last_high_h=130)
    horizon = forecast.tide_horizon(fi)
    last_high = max(ts for ts, _ in fi.tide_highs)
    assert horizon == last_high + timedelta(hours=forecast.TIDE_HALF_PERIOD_H)
    entries = build_forecast(fi, 168)
    for e in entries:
        ts = datetime.fromisoformat(e["timestamp"]).replace(tzinfo=None)
        if ts <= horizon:
            assert e["tide_available"] and not e["beyond_tide_horizon"], e["hours_ahead"]
            assert abs(e["hours_to_high_tide"]) <= 6.3
        else:
            assert not e["tide_available"] and e["beyond_tide_horizon"], e["hours_ahead"]


def test_forecast_tide_horizon_leaves_known_hours_unchanged():
    short, long_ = _inputs(last_high_h=60), _inputs(last_high_h=200)
    horizon = forecast.tide_horizon(short)
    for h in range(0, 168, 5):
        f_short, _ = build_forecast_features(short, h)
        f_long, _ = build_forecast_features(long_, h)
        if f_short.timestamp <= horizon:
            assert f_short == f_long, h
        else:
            assert f_short.tide_available is False and f_short.hours_to_high_tide is None
            assert f_long.tide_available is True


def test_forecast_h0_keeps_the_realtime_tide_rule():
    # All known highs in the past: H+0 still uses the nearest high (realtime rule)
    fi = replace(_inputs(last_high_h=0), tide_highs=[(T0 - timedelta(hours=8), 90)])
    f0, _ = build_forecast_features(fi, 0)
    assert f0.tide_available is True and f0.hours_to_high_tide == pytest.approx(-8.0)
    f1, _ = build_forecast_features(fi, 1)
    assert f1.tide_available is False
