"""Risk forecast service.

Generates hourly risk predictions for the next hours (default 7 days):
for each hour it BUILDS risk features (projected station levels, rain
windows, tide, soil, wind) and calls the single scoring function
``risk_core.compute_risk`` - there is no forecast-specific score formula.

Inputs:
- the realtime inputs shared with /api/realtime (stations, tide, fused rain,
  soil saturation, westerly wind), so H+0 equals the realtime score;
- past observed hourly rain (Open-Meteo, UTC) calibrated on the realtime
  multi-source rain, window by window (factor capped to [1/3, 3]);
- hourly rain forecast (Open-Meteo AROME/ARPEGE + ECMWF), converted to UTC;
- tide predictions.

Conventions: all datetimes are naive UTC. Hourly rain is keyed by the END
of its hour; the H+0 past windows are strictly before t0 and rain-driven
river rise only uses rain falling after t0 (rain before t0 is already in
the current levels and in their trend).

The forecast is cached in Redis (30 min TTL).

v1.0 (Jan 2026): initial implementation (Feb 2024 calibration).
v2.0 (Jan 2026): v3.0 model weights with momentum/dampener.
v3.3 (Oct 2026): uses risk_core; timezone, calibration, rain re-injection
and H+0 consistency fixes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence

from app.config import get_settings
from app.models.schemas import RainfallSummary, RiskLevel, TideForecast
from app.services import risk_core
from app.services.risk_core import (
    MODEL_VERSION,
    RISING_RATE_CM_H,
    RiskFeatures,
    StationFeature,
    compute_risk,
    floor_hour,
    rain_window_sum,
    score_to_level,
    to_utc_naive,
)

logger = logging.getLogger(__name__)

# fetch_open_meteo_forecast() requests timezone=UTC and returns naive UTC
# timestamps; aware timestamps are converted as-is.
NAIVE_FORECAST_TZ = timezone.utc

# Hydrological response parameters (calibrated from Feb 2024 flood)
RAIN_TO_LEVEL_COEF = 0.008      # m of river rise per mm of rain (after runoff)
RAIN_RESPONSE_DELAY_H = 18      # Hours for rain to affect river level
RAIN_RESPONSE_DURATION_H = 36   # Hours over which rain effect persists
TREND_EXTRAPOLATION_MAX_H = 12  # Current trend extrapolated at most 12h
MAX_RISE_M = 2.0                # Maximum realistic rise (Feb 2024: ~+1.5 m at Pessac)
MOMENTUM_DECAY_H = 48           # Linear decay of the current rising count

# Past-rain calibration on realtime multi-source rain
CALIBRATION_MAX_FACTOR = 3.0
CALIBRATION_MIN_FACTOR = 1.0 / 3.0


async def _get_redis_client():
    """Shared Redis client (see app.services.cache); never close it here."""
    from app.services.cache import get_redis
    return get_redis()


async def _get_cached_forecast(hours: int) -> Optional[dict]:
    """Get cached forecast from Redis if fresh."""
    try:
        client = await _get_redis_client()
        cached = await client.get(f"forecast:{hours}")
        if cached:
            data = json.loads(cached)
            generated_at = to_utc_naive(datetime.fromisoformat(data.get("generated_at", "2000-01-01")))
            age_minutes = (datetime.utcnow() - generated_at).total_seconds() / 60
            if age_minutes < 30:
                return data
    except Exception as e:
        logger.warning("Redis cache read error: %s", e)
    return None


async def _set_cached_forecast(hours: int, data: dict) -> None:
    """Cache forecast in Redis (30 min TTL)."""
    try:
        client = await _get_redis_client()
        await client.set(
            f"forecast:{hours}",
            json.dumps(data, default=str),
            ex=30 * 60,
        )
    except Exception as e:
        logger.warning("Redis cache write error: %s", e)


def _iso_utc(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


def forecast_ts_to_utc(ts: datetime) -> datetime:
    """Forecast timestamp -> naive UTC (naive input = UTC)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=NAIVE_FORECAST_TZ)
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def westerly_component(speed_kmh: Optional[float], direction_deg: Optional[float]) -> Optional[float]:
    """Westerly wind component (km/h), same formula as the weather collector."""
    import math

    if speed_kmh is None or direction_deg is None:
        return None
    return max(0.0, speed_kmh * math.cos(math.radians(270) - math.radians(direction_deg)))


# ---------------------------------------------------------------------------
# Pure forecast core
# ---------------------------------------------------------------------------

@dataclass
class ForecastInputs:
    """Everything the forecast needs, already fetched (naive UTC)."""

    t0: datetime
    stations: tuple[StationFeature, ...]
    primary_station_id: str
    station_delays: Mapping[str, float]
    # Hourly rain keyed by hour END: past (<= floor(t0)) and future (> floor(t0))
    rain_hourly: Mapping[datetime, float]
    # Realtime (fused) rain summary: H+0 rain windows and calibration target
    observed_rain: Optional[RainfallSummary] = None
    tide_highs: Sequence[tuple[datetime, Optional[int]]] = ()
    tide_fallback_coefficient: Optional[int] = None
    tide_reliable: bool = True
    soil_saturation_factor: Optional[float] = None
    westerly_wind_kmh: Optional[float] = None
    westerly_wind_hourly: Mapping[datetime, float] = field(default_factory=dict)


def calibrate_past_rain(
    rain_hourly: Mapping[datetime, float],
    t0: datetime,
    observed: Optional[RainfallSummary],
) -> tuple[dict[datetime, float], dict]:
    """Scale past hourly rain so window sums match realtime multi-source rain.

    Windows (anchored at floor(t0), hour-end keys): (t0-24h, t0],
    (t0-72h, t0-24h], (t0-168h, t0-72h]. Each window gets its own factor,
    capped to [1/3, 3], applied only to that window. When the model has no
    rain in a window but observations do, the observed amount is spread
    uniformly over the window.
    """
    out = dict(rain_hourly)
    info: dict = {}
    if observed is None:
        return out, info

    base = floor_hour(t0)
    windows = [
        ("24h", 0, 24, observed.last_24h_mm),
        ("72h", 24, 72, max(0.0, observed.last_72h_mm - observed.last_24h_mm)),
        ("7d", 72, 168, max(0.0, observed.last_7d_mm - observed.last_72h_mm)),
    ]
    for name, k_from, k_to, target in windows:
        keys = [base - timedelta(hours=k) for k in range(k_from, k_to)]
        model_sum = sum(out.get(k, 0.0) for k in keys)
        if model_sum > 0.1:
            factor = min(CALIBRATION_MAX_FACTOR, max(CALIBRATION_MIN_FACTOR, target / model_sum))
            if abs(factor - 1.0) > 1e-9:
                for k in keys:
                    if k in out:
                        out[k] *= factor
            info[name] = {"factor": round(factor, 3), "model_mm": round(model_sum, 1), "observed_mm": round(target, 1)}
        elif target > 1.0:
            per_hour = target / len(keys)
            for k in keys:
                out[k] = out.get(k, 0.0) + per_hour
            info[name] = {"injected_mm": round(target, 1)}
    return out, info


def _rain_after_t0(rain_hourly: Mapping[datetime, float], base: datetime, k_from: int, k_to: int) -> float:
    """Rain of hours ending at base+k for k in [max(1,k_from), k_to] (post-t0 only)."""
    return sum(
        rain_hourly.get(base + timedelta(hours=k), 0.0)
        for k in range(max(1, k_from), k_to + 1)
    )


def _rain_driven_rising(rain_mm: float) -> int:
    """Rising stations expected from rain that reached the rivers."""
    if rain_mm >= 40:
        return 5
    if rain_mm >= 25:
        return 4
    if rain_mm >= 15:
        return 3
    if rain_mm >= 8:
        return 2
    if rain_mm >= 3:
        return 1
    return 0


def _project_level(
    station: StationFeature,
    delay_h: float,
    h: int,
    rain_hourly: Mapping[datetime, float],
    base: datetime,
    saturation: float,
) -> float:
    """Projected level of a station at H+h."""
    # Component 1: current trend continues (capped)
    trend_m = (station.rate_cm_h / 100.0) * min(h, TREND_EXTRAPOLATION_MAX_H)

    # Component 2: rain falling AFTER t0, lagged (rain before t0 is already
    # contained in the current level and its trend)
    rain_mm = _rain_after_t0(
        rain_hourly, base,
        h - RAIN_RESPONSE_DELAY_H - RAIN_RESPONSE_DURATION_H,
        h - RAIN_RESPONSE_DELAY_H,
    )
    runoff_factor = 0.3 + (saturation - 1.0) * 0.8  # 0.3 dry, 0.7 saturated
    rain_response_m = rain_mm * RAIN_TO_LEVEL_COEF * runoff_factor
    station_rain_delay = max(0.0, RAIN_RESPONSE_DELAY_H - delay_h)
    if h >= station_rain_delay:
        rain_rise = rain_response_m
    else:
        rain_rise = rain_response_m * (h / max(1.0, station_rain_delay))

    return max(0.0, station.height_m + min(trend_m + rain_rise, MAX_RISE_M))


def build_forecast_features(fi: ForecastInputs, h: int) -> tuple[RiskFeatures, Optional[float]]:
    """Risk features at H+h, plus the projected primary level (display)."""
    base = floor_hour(fi.t0)
    t_h = fi.t0 + timedelta(hours=h)
    saturation = fi.soil_saturation_factor if fi.soil_saturation_factor is not None else 1.0

    # --- Projected stations (same scoring path as realtime) ---
    stations = []
    primary_level = None
    for s in fi.stations:
        if h == 0:
            height, rate = s.height_m, s.rate_cm_h
        else:
            delay = fi.station_delays.get(s.station_id, 0.0)
            height = _project_level(s, delay, h, fi.rain_hourly, base, saturation)
            prev = _project_level(s, delay, h - 1, fi.rain_hourly, base, saturation)
            rate = (height - prev) * 100.0
        stations.append(StationFeature(
            station_id=s.station_id,
            station_name=s.station_name,
            river=s.river,
            height_m=height,
            rate_cm_h=rate,
            weight=s.weight,
            thresholds=s.thresholds,
            display_tendency=s.display_tendency if h == 0 else None,
        ))
        if s.station_id == fi.primary_station_id:
            primary_level = height

    # --- Projected rising count: current count decays, rain after t0 adds ---
    active = [s for s in fi.stations if s.weight > 0]
    n_rising_now = sum(1 for s in active if s.rate_cm_h > RISING_RATE_CM_H)
    decay = max(0.0, 1.0 - h / MOMENTUM_DECAY_H)
    rain_reaching_rivers = _rain_after_t0(
        fi.rain_hourly, base,
        h - RAIN_RESPONSE_DELAY_H - RAIN_RESPONSE_DURATION_H,
        h - RAIN_RESPONSE_DELAY_H,
    )
    rain_rising = min(_rain_driven_rising(rain_reaching_rivers), len(active))
    n_rising = max(round(n_rising_now * decay), rain_rising)

    # --- Rain windows ---
    if h == 0 and fi.observed_rain is not None:
        # H+0 is the realtime nowcast: same rain inputs as /api/realtime
        obs = fi.observed_rain
        rain_24h, rain_72h, rain_7d = obs.last_24h_mm, obs.last_72h_mm, obs.last_7d_mm
        fc_24h, fc_72h = obs.forecast_24h_mm, obs.forecast_72h_mm
    else:
        t_key = base + timedelta(hours=h)
        rain_24h = rain_window_sum(fi.rain_hourly, t_key, 24)
        rain_72h = rain_window_sum(fi.rain_hourly, t_key, 72)
        rain_7d = rain_window_sum(fi.rain_hourly, t_key, 168)
        fc_24h = rain_window_sum(fi.rain_hourly, t_key, 24, forward=True)
        fc_72h = rain_window_sum(fi.rain_hourly, t_key, 72, forward=True)

    # --- Tide (same rule as realtime) ---
    tide = risk_core.tide_at(t_h, fi.tide_highs, fallback_coefficient=fi.tide_fallback_coefficient)
    tide_available = tide.available and fi.tide_reliable

    # --- Wind ---
    wind = fi.westerly_wind_kmh
    if h > 0:
        wind = fi.westerly_wind_hourly.get(base + timedelta(hours=h), wind)

    features = RiskFeatures(
        timestamp=t_h,
        stations=tuple(stations),
        primary_station_id=fi.primary_station_id,
        rain_24h_mm=rain_24h,
        rain_72h_mm=rain_72h,
        rain_7d_mm=rain_7d,
        rain_forecast_24h_mm=fc_24h,
        rain_forecast_72h_mm=fc_72h,
        tide_available=tide_available,
        tide_coefficient=tide.coefficient,
        hours_to_high_tide=tide.hours_to_high_tide,
        westerly_wind_kmh=wind,
        soil_saturation_factor=fi.soil_saturation_factor,
        n_rising_override=n_rising,
    )
    return features, primary_level


def _display_hours_to_high(fi: ForecastInputs, t_h: datetime, value: Optional[float]) -> float:
    if value is not None:
        return value
    if fi.tide_highs:
        nearest = min(fi.tide_highs, key=lambda x: abs((x[0] - t_h).total_seconds()))
        return (nearest[0] - t_h).total_seconds() / 3600
    return 0.0


def build_forecast(fi: ForecastInputs, hours: int) -> list[dict]:
    """Hourly forecast entries for H+0..H+hours (pure)."""
    params = risk_core.get_model_params()
    base = floor_hour(fi.t0)
    out = []
    for h in range(0, hours + 1):
        features, primary_level = build_forecast_features(fi, h)
        bd = compute_risk(features, params)
        out.append({
            "timestamp": _iso_utc(features.timestamp),
            "hours_ahead": h,
            "score": bd.score,
            "level": bd.level.value,
            "rain_1h_mm": round(fi.rain_hourly.get(base + timedelta(hours=h + 1), 0.0), 1),
            "rain_24h_mm": round(features.rain_24h_mm, 1),
            "rain_72h_mm": round(features.rain_72h_mm, 1),
            "tide_coefficient": bd.tide_coefficient or params.default_tide_coefficient,
            "hours_to_high_tide": round(_display_hours_to_high(fi, features.timestamp, bd.hours_to_high_tide), 1),
            "tide_available": bd.tide_available,
            "projected_level_m": round(primary_level, 2) if primary_level is not None else 0.0,
            "n_rising": bd.n_rising,
            "conjunction_multiplier": round(bd.conjunction_multiplier, 2),
            "stability_dampener": round(bd.stability_dampener, 2),
        })
    return out


def summarize_forecast(forecasts: list[dict], hours: int, data_sources: dict) -> dict:
    """Summary fields (max risk, critical periods, text)."""
    critical = [f for f in forecasts if f["level"] in ("orange", "red")]
    max_score = max((f["score"] for f in forecasts), default=0)
    max_level = score_to_level(max_score)

    if max_level == RiskLevel.RED:
        summary = f"ALERTE: Risque ROUGE prévu (max {max_score:.0f}/100)"
    elif max_level == RiskLevel.ORANGE:
        summary = f"Vigilance: Risque ORANGE prévu (max {max_score:.0f}/100)"
    elif max_level == RiskLevel.YELLOW:
        summary = f"Attention: Risque JAUNE prévu (max {max_score:.0f}/100)"
    else:
        summary = "Conditions favorables prévues"

    red_hours = sum(1 for f in forecasts if f["level"] == "red")
    orange_hours = sum(1 for f in forecasts if f["level"] == "orange")
    if red_hours > 0:
        summary += f" - {red_hours}h en rouge"
    if orange_hours > 0:
        summary += f" - {orange_hours}h en orange"

    failed = [k for k, v in data_sources.items() if isinstance(v, dict) and v.get("status") == "error"]
    if failed:
        summary += f" (sources indisponibles: {', '.join(failed)})"

    return {
        "duration_label": "7 jours" if hours >= 168 else f"{hours}h",
        "max_risk": round(max_score, 1),
        "max_level": max_level.value,
        "critical_periods": critical,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

async def _fetch_past_hourly_rain_utc(lat: float, lon: float, past_days: int = 7) -> dict[datetime, float]:
    """Past observed hourly rain from Open-Meteo, keyed by hour END (naive UTC)."""
    import httpx
    from app.services.collectors.weather import OPEN_METEO_CURRENT

    async with httpx.AsyncClient() as client:
        response = await client.get(
            OPEN_METEO_CURRENT,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "precipitation",
                "past_days": past_days,
                "forecast_days": 1,
                "timezone": "UTC",
            },
            timeout=30.0,
        )
        response.raise_for_status()
        hourly = response.json().get("hourly", {})

    out: dict[datetime, float] = {}
    for time_str, mm in zip(hourly.get("time", []), hourly.get("precipitation", [])):
        if mm is None:
            continue
        out[datetime.fromisoformat(time_str)] = float(mm)
    return out


async def _fetch_tide_highs_from_influx(port: str, start: datetime, end: datetime) -> list[tuple[datetime, Optional[int]]]:
    """Stored tide predictions -> high tides (ts, coefficient) in [start, end]."""
    from app.services import influx_client
    from app.services.risk_v2 import tide_inputs_from_records

    rows = await influx_client.query_tides(port, start, end)
    highs, coefs = tide_inputs_from_records(rows)
    result = []
    for ts, _ in highs:
        coef = None
        if coefs:
            c_ts, c = min(coefs, key=lambda x: abs((x[0] - ts).total_seconds()))
            if abs((c_ts - ts).total_seconds()) <= 3600:
                coef = c
        result.append((ts, coef))
    return result


async def get_risk_forecast_v2(hours: int = 168) -> dict:
    """
    Generate detailed risk forecast for the next hours (default: 7 days).

    Returns:
        dict with generated_at, hours_ahead, forecasts (hourly), max_risk,
        max_level, critical_periods, summary, data_sources, model_version.
    """
    cached = await _get_cached_forecast(hours)
    if cached:
        logger.info("forecast: from Redis cache")
        return cached

    from app.services.collectors.weather import fetch_open_meteo_forecast
    from app.services.risk_v2 import (
        gather_realtime_inputs,
        station_features_from_multi,
        tide_forecast_is_reliable,
        tide_high_list,
    )

    settings = get_settings()
    now = datetime.utcnow()
    base = floor_hour(now)
    forecast_days = min((hours // 24) + 2, 16)
    data_sources: dict = {}

    # Realtime inputs (same as /api/realtime -> H+0 == realtime score)
    inputs = None
    try:
        inputs = await gather_realtime_inputs(now)
        n_stations = len(inputs.stations.stations) if inputs.stations else 0
        data_sources["river"] = {"status": "ok" if n_stations > 0 else "empty", "stations": n_stations}
        data_sources["tide"] = (
            {"status": "ok", "port": inputs.tide.port}
            if inputs.tide and tide_forecast_is_reliable(inputs.tide)
            else {"status": "estimated" if inputs.tide else "error"}
        )
        data_sources["soil_wind"] = {"status": "ok" if inputs.soil_wind else "empty"}
    except Exception as e:
        logger.error("Forecast: realtime inputs failed: %s", e)
        data_sources["river"] = {"status": "error", "error": str(e)}

    # Future rain forecast (hourly, converted to UTC, hour-end keys)
    rain_hourly: dict[datetime, float] = {}
    wind_hourly: dict[datetime, float] = {}
    try:
        rain_forecast = await fetch_open_meteo_forecast(
            settings.moulin_lat, settings.moulin_lon, forecast_days
        )
        for fc in rain_forecast:
            key = forecast_ts_to_utc(fc.timestamp)
            if key > base:
                rain_hourly[key] = fc.precipitation_mm or 0.0
                westerly = westerly_component(fc.wind_speed_kmh, fc.wind_direction_deg)
                if westerly is not None:
                    wind_hourly[key] = westerly
        data_sources["rain"] = {
            "status": "ok" if rain_forecast else "empty",
            "source": "Open-Meteo (AROME/ARPEGE)",
            "hours": len(rain_forecast),
        }
    except Exception as e:
        logger.error("Forecast: rain forecast fetch failed: %s", e)
        data_sources["rain"] = {"status": "error", "error": str(e)}

    # Past observed rain (UTC), only hours ending at or before t0
    try:
        past = await _fetch_past_hourly_rain_utc(settings.moulin_lat, settings.moulin_lon, past_days=7)
        past = {k: v for k, v in past.items() if k <= base}
        rain_hourly.update(past)
        data_sources["rain_past"] = {"status": "ok" if past else "empty", "hours": len(past)}
    except Exception as e:
        logger.warning("Forecast: past rain fetch failed: %s", e)
        data_sources["rain_past"] = {"status": "error", "error": str(e)}

    observed = inputs.rainfall if inputs else None
    rain_hourly, calibration = calibrate_past_rain(rain_hourly, now, observed)
    if calibration:
        data_sources["rain_calibration"] = {"status": "ok", **calibration}

    # Tides: realtime forecast list extended with stored predictions
    tide = inputs.tide if inputs else None
    tide_highs = tide_high_list(tide)
    try:
        last_known = max((ts for ts, _ in tide_highs), default=now)
        extra = await _fetch_tide_highs_from_influx(
            tide.port if tide else settings.tide_port_bordeaux,
            last_known + timedelta(minutes=1),
            now + timedelta(hours=hours + 13),
        )
        tide_highs = sorted(tide_highs + [x for x in extra if x[0] > last_known])
    except Exception as e:
        logger.warning("Forecast: stored tide predictions unavailable: %s", e)

    stations = station_features_from_multi(inputs.stations, now) if inputs else ()
    fi = ForecastInputs(
        t0=now,
        stations=stations,
        primary_station_id=(
            inputs.stations.primary_station_id if inputs and inputs.stations
            else settings.hubeau_primary_station
        ),
        station_delays={s["id"]: float(s.get("delay_hours", 0)) for s in settings.hubeau_stations},
        rain_hourly=rain_hourly,
        observed_rain=observed,
        tide_highs=tide_highs,
        tide_fallback_coefficient=tide.current_coefficient if tide else None,
        tide_reliable=tide_forecast_is_reliable(tide),
        soil_saturation_factor=inputs.soil_saturation_factor if inputs else None,
        westerly_wind_kmh=inputs.westerly_wind_kmh if inputs else None,
        westerly_wind_hourly=wind_hourly,
    )

    forecasts = build_forecast(fi, hours)
    result = {
        "generated_at": _iso_utc(now),
        "hours_ahead": hours,
        "forecasts": forecasts,
        "data_sources": data_sources,
        "model_version": MODEL_VERSION,
        **summarize_forecast(forecasts, hours, data_sources),
    }

    await _set_cached_forecast(hours, result)
    return result


async def invalidate_forecast_cache() -> None:
    """Invalidate forecast cache (call when new data arrives)."""
    try:
        client = await _get_redis_client()
        keys = await client.keys("forecast:*")
        if keys:
            await client.delete(*keys)
    except Exception as e:
        logger.warning("Redis cache invalidate error: %s", e)
