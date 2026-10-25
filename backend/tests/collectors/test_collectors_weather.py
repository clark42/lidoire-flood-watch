"""Weather collector: UTC rain windows, no future hours, last complete hour, cache."""

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.services.collectors import weather

NOW = datetime(2026, 10, 25, 22, 35)  # frozen UTC "now"


def _series(start: datetime, values: list[float]) -> list[tuple[datetime, float]]:
    return [(start + timedelta(hours=i), v) for i, v in enumerate(values)]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(weather, "_past_rain_cache", {})
    monkeypatch.setattr(weather, "_weather_cache", {})
    monkeypatch.setattr(weather, "_cache_expiry", None)
    monkeypatch.setattr(weather, "_utcnow", lambda: NOW)


def test_windows_exclude_future_and_are_half_open():
    # 1 mm every hour from NOW-30h to NOW+5h (labels on the hour)
    start = datetime(2026, 10, 24, 16, 0)
    series = _series(start, [1.0] * 36)
    sums = weather.sum_rain_windows(series, NOW, (1, 6, 24))
    # Latest complete hour label is 22:00 (rain 21:00-22:00); 23:00+ is future
    assert sums[1] == 1.0
    assert sums[6] == 6.0
    assert sums[24] == 24.0  # not 25: window is half-open


def test_windows_on_exact_hour():
    now = datetime(2026, 10, 25, 22, 0)
    series = _series(datetime(2026, 10, 25, 0, 0), [1.0] * 24)  # 00:00..23:00
    sums = weather.sum_rain_windows(series, now, (1, 24))
    assert sums[1] == 1.0  # label 22:00 complete at 22:00
    assert sums[24] == 23.0  # labels 00:00..22:00 (23:00 is future)


def test_hourly_offsets_keep_last_full_hour():
    series = [(datetime(2026, 10, 25, 22, 0), 2.0),   # ended 35 min ago
              (datetime(2026, 10, 25, 21, 0), 3.0),
              (datetime(2026, 10, 25, 23, 0), 9.0)]   # future
    offsets = weather.hourly_offsets(series, NOW, past_days=7)
    assert offsets == {-1: 2.0, -2: 3.0}


def test_hourly_offsets_respect_past_days():
    series = _series(NOW.replace(minute=0) - timedelta(hours=50), [1.0] * 51)
    offsets = weather.hourly_offsets(series, NOW, past_days=1)
    assert min(offsets) == -24 and max(offsets) == -1 and len(offsets) == 24


def _om_payload(start: datetime, values: list):
    return {"hourly": {
        "time": [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(len(values))],
        "precipitation": values,
    }}


@respx.mock
async def test_past_sums_request_and_cache():
    start = datetime(2026, 10, 18, 0, 0)
    n = 8 * 24
    values = [0.5] * n
    values[5] = None  # missing value is skipped, not counted as 0
    route = respx.get(url__startswith=weather.OPEN_METEO_CURRENT).mock(
        return_value=httpx.Response(200, json=_om_payload(start, values)))

    sums = await weather.get_past_rainfall_sums(44.87, 0.0015)
    params = route.calls[0].request.url.params
    assert params["timezone"] == "UTC"
    assert params["past_days"] == "7"
    assert params["forecast_days"] == "1"  # 0 would drop today's hours
    assert sums["rain_1h_mm"] == 0.5
    assert sums["rain_24h_mm"] == 12.0
    assert sums["rain_7d_mm"] == 84.0

    offsets = await weather.fetch_past_hourly_rain(44.87, 0.0015, past_days=1)
    assert offsets[-1] == 0.5 and len(offsets) == 24
    assert route.call_count == 1  # served from the 15 min cache


@respx.mock
async def test_past_sums_error_returns_empty_and_not_cached():
    route = respx.get(url__startswith=weather.OPEN_METEO_CURRENT).mock(return_value=httpx.Response(429))
    assert await weather.get_past_rainfall_sums(44.87, 0.0015) == {}
    assert await weather.get_past_rainfall_sums(44.87, 0.0015) == {}
    assert route.call_count == 2


async def test_rainfall_summary_uses_past_sums_and_future_forecast(monkeypatch):
    async def fake_past(lat, lon):
        return {"rain_1h_mm": 1.0, "rain_6h_mm": 4.0, "rain_24h_mm": 10.0,
                "rain_72h_mm": 20.0, "rain_7d_mm": 30.0}

    async def fake_forecast(lat, lon, forecast_days=14):
        from app.models.schemas import WeatherForecast
        base = NOW.replace(minute=0)
        return [WeatherForecast(timestamp=base + timedelta(hours=h), point_id="m",
                                point_name="m", precipitation_mm=1.0)
                for h in range(-3, 80)]

    async def fake_soil(lat, lon):
        return {"soil_moisture": 0.3, "soil_saturation_factor": 1.2}

    monkeypatch.setattr(weather, "get_past_rainfall_sums", fake_past)
    monkeypatch.setattr(weather, "fetch_open_meteo_forecast", fake_forecast)
    monkeypatch.setattr(weather, "fetch_soil_and_wind", fake_soil)

    summary = await weather.get_rainfall_summary()
    assert summary.last_hour_mm == 1.0
    assert summary.last_24h_mm == 10.0
    assert summary.last_72h_mm == 20.0
    assert summary.last_7d_mm == 30.0
    # labels 23:00 .. 22:00+24h -> 24 future hours, past labels excluded
    assert summary.forecast_24h_mm == 24.0
    assert summary.forecast_72h_mm == 72.0


def test_current_hour_index_utc():
    times = [f"2026-10-25T{h:02d}:00" for h in range(24)]
    assert weather._current_hour_index(times, NOW) == 22


@respx.mock
async def test_soil_and_wind_daily_et0_and_utc_index():
    hourly_keys = ["soil_moisture_0_to_1cm", "soil_moisture_1_to_3cm", "soil_moisture_3_to_9cm",
                   "soil_moisture_9_to_27cm", "soil_moisture_27_to_81cm", "soil_temperature_0cm",
                   "et0_fao_evapotranspiration", "precipitation_probability",
                   "wind_speed_10m", "wind_direction_10m"]
    hourly = {k: [0.1] * 24 for k in hourly_keys}
    hourly["time"] = [f"2026-10-25T{h:02d}:00" for h in range(24)]
    hourly["wind_speed_10m"] = [float(h) for h in range(24)]
    hourly["wind_direction_10m"] = [270.0] * 24
    route = respx.get(url__startswith=weather.OPEN_METEO_CURRENT).mock(
        return_value=httpx.Response(200, json={"hourly": hourly,
                                               "daily": {"et0_fao_evapotranspiration": [1.8]}}))
    data = await weather.fetch_soil_and_wind(44.87, 0.0015)
    assert route.calls[0].request.url.params["timezone"] == "UTC"
    assert data["wind_speed_kmh"] == 22.0  # UTC hour 22, not local hour
    assert data["et0_mm"] == 1.8
    assert data["westerly_wind_kmh"] == 22.0


async def test_collect_writes_aligned_hour(monkeypatch):
    from app.services import influx_client

    writes = []

    async def fake_write_rainfall(**kwargs):
        writes.append(kwargs)
        return True

    async def fake_summary():
        from app.models.schemas import RainfallSummary
        return RainfallSummary(last_24h_mm=7.0)

    async def fake_series(lat, lon):
        return _series(datetime(2026, 10, 25, 20, 0), [0.2, 0.4, 0.6, 0.8])  # 20..23h

    async def fake_soil_wind():
        return {}

    monkeypatch.setattr(influx_client, "write_rainfall", fake_write_rainfall)
    monkeypatch.setattr(weather, "get_rainfall_summary", fake_summary)
    monkeypatch.setattr(weather, "_fetch_past_hourly_series", fake_series)
    monkeypatch.setattr(weather, "get_soil_wind_cached", fake_soil_wind)

    await weather.collect_and_store_weather()
    assert len(writes) == 1
    assert writes[0]["timestamp"] == datetime(2026, 10, 25, 22, 0)
    assert writes[0]["precipitation_mm_h"] == 0.6
    assert writes[0]["precipitation_mm_24h"] == 7.0
