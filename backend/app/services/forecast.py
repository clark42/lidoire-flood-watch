"""Risk forecast service.

Generates hourly risk predictions for the next 7 days based on:
- Hourly rainfall forecast from Open-Meteo (AROME/ARPEGE models)
- Tide predictions (coefficients and high/low times)
- Projected river levels from upstream stations with propagation delays
- Current soil moisture and saturation factor
- Momentum (multi-station rising pattern)
- Multi-station above threshold trigger (v3.2)

The forecast is cached in Redis (30 min TTL) since weather forecasts
update approximately every 6 hours but we want fresh data for users.

v1.0 (Jan 2026): Initial implementation based on Feb 2024 calibration.
v2.0 (Jan 2026): Updated to v3.0 model weights with momentum/dampener.
v3.0 (Jan 2026): Updated to v3.2 model with multi-station threshold trigger.
"""

import json
from datetime import datetime, timedelta
from typing import Optional

from app.config import get_settings
from app.models.schemas import (
    RiskLevel,
    TideType,
    WeatherForecast,
)


async def _get_redis_client():
    """Get Redis client (lazy initialization)."""
    import redis.asyncio as redis
    settings = get_settings()
    return redis.from_url(settings.redis_url, decode_responses=True)


async def _get_cached_forecast(hours: int) -> Optional[dict]:
    """Get cached forecast from Redis if fresh."""
    try:
        client = await _get_redis_client()
        cached = await client.get(f"forecast:{hours}")
        await client.aclose()
        if cached:
            data = json.loads(cached)
            # Check if still fresh (30 min)
            generated_at = datetime.fromisoformat(data.get("generated_at", "2000-01-01"))
            age_minutes = (datetime.utcnow() - generated_at).total_seconds() / 60
            if age_minutes < 30:
                return data
    except Exception as e:
        print(f"Redis cache read error: {e}")
    return None


async def _set_cached_forecast(hours: int, data: dict) -> None:
    """Cache forecast in Redis (30 min TTL)."""
    try:
        client = await _get_redis_client()
        await client.set(
            f"forecast:{hours}",
            json.dumps(data, default=str),
            ex=30 * 60,  # 30 min TTL
        )
        await client.aclose()
    except Exception as e:
        print(f"Redis cache write error: {e}")


def _score_to_level(score: float) -> RiskLevel:
    """Convert numerical score (0-100) to risk level (v2.5 thresholds)."""
    if score < 15:
        return RiskLevel.GREEN
    elif score < 35:
        return RiskLevel.YELLOW
    elif score < 55:
        return RiskLevel.ORANGE
    else:
        return RiskLevel.RED


def _normalize_score(
    value: float,
    thresholds: tuple[float, float, float],
    exponential: bool = True,
) -> float:
    """Normalize a value to 0-1 based on thresholds (same as risk_v2)."""
    yellow, orange, red = thresholds

    if value <= 0:
        return 0.0
    elif value < yellow:
        linear = (value / yellow) * 0.33
    elif value < orange:
        linear = 0.33 + ((value - yellow) / (orange - yellow)) * 0.33
    elif value < red:
        linear = 0.66 + ((value - orange) / (red - orange)) * 0.34
    else:
        linear = 1.0

    if exponential and linear > 0:
        return linear ** 0.6
    return linear


async def get_risk_forecast_v2(hours: int = 168) -> dict:
    """
    Generate detailed risk forecast for the next hours (default: 7 days).

    Uses:
    - Open-Meteo hourly precipitation forecast (AROME/ARPEGE)
    - Tide predictions with coefficients
    - Upstream station levels with propagation delays
    - Current soil moisture for saturation factor
    - Multi-station momentum (v3.0)

    Returns:
        dict with:
        - generated_at: timestamp
        - hours_ahead: int
        - forecasts: list of hourly predictions
        - max_risk: highest score in forecast
        - critical_periods: list of ORANGE/RED periods
        - summary: text summary of upcoming risk
    """
    # Check cache first
    cached = await _get_cached_forecast(hours)
    if cached:
        print("forecast: from Redis cache")
        return cached

    from app.services.collectors.tide import get_current_tide_data
    from app.services.collectors.weather import (
        fetch_open_meteo_forecast,
        fetch_soil_and_wind,
    )
    from app.services.collectors.hubeau import get_multi_station_data
    from app.services.risk_v2 import (
        _calculate_momentum_score,
        _calculate_conjunction_multiplier,
        _calculate_stability_dampener,
    )
    from app.models.schemas import Tendency, StationRiskScore

    settings = get_settings()
    now = datetime.utcnow()

    # Fetch all data in parallel
    import asyncio
    tide_data, rain_forecast, soil_wind, stations_data = await asyncio.gather(
        get_current_tide_data(),
        fetch_open_meteo_forecast(settings.moulin_lat, settings.moulin_lon),
        fetch_soil_and_wind(settings.moulin_lat, settings.moulin_lon),
        get_multi_station_data(),
    )

    # Build rainfall lookup by hour
    rain_by_hour: dict[int, float] = {}
    for fc in rain_forecast:
        hours_ahead = int((fc.timestamp - now).total_seconds() / 3600)
        if 0 <= hours_ahead < hours:
            rain_by_hour[hours_ahead] = fc.precipitation_mm

    # Build tide events lookup
    tide_events = []
    for t in tide_data.tides:
        hours_ahead = (t.timestamp - now).total_seconds() / 3600
        if -12 <= hours_ahead <= hours + 12:
            tide_events.append({
                "timestamp": t.timestamp,
                "type": t.tide_type.value if t.tide_type else None,
                "coefficient": t.coefficient,
                "height_m": t.height_m,
            })
    tide_events.sort(key=lambda t: t["timestamp"])

    # Get current station levels, rates, and thresholds for projection
    current_levels: dict[str, float] = {}
    current_rates: dict[str, float] = {}
    station_weights: dict[str, float] = {}
    station_thresholds: dict[str, tuple[float, float, float]] = {}
    station_names: dict[str, str] = {}
    station_rivers: dict[str, str] = {}
    for station in stations_data.stations:
        sid = station.station_id
        current_levels[sid] = station.current_level.height_m
        current_rates[sid] = station.rate_cm_per_hour
        station_weights[sid] = station.weight
        station_thresholds[sid] = (station.threshold_yellow, station.threshold_orange, station.threshold_red)
        station_names[sid] = station.station_name
        station_rivers[sid] = station.river

    # Compute current momentum (number of rising stations with weight > 0)
    n_rising = sum(1 for sid, r in current_rates.items() if r > 1.0 and station_weights.get(sid, 0) > 0)
    n_total = max(sum(1 for w in station_weights.values() if w > 0), 1)

    # Soil saturation factor
    saturation = soil_wind.get("soil_saturation_factor", 1.0)

    # v3.0 weights (must match risk_v2.py)
    weights = {
        "river": 0.28,
        "rain_24h": 0.10,
        "rain_72h": 0.12,
        "rain_7d": 0.05,
        "tide": 0.05,
        "tide_phase": 0.05,
        "tendency": 0.12,
        "momentum": 0.08,
        "wind_west": 0.03,
        "soil_sat": 0.05,
        "video": 0.05,
    }
    # Exclude video (not available in forecast)
    total_w = sum(weights.values()) - weights["video"]

    # Generate hourly forecasts
    forecasts = []
    for h in range(0, hours + 1):
        forecast_time = now + timedelta(hours=h)

        # --- Rain: cumulative 24h, 72h, 7d up to this hour ---
        rain_24h = sum(
            rain_by_hour.get(hour, 0.0)
            for hour in range(max(0, h - 23), h + 1)
        )
        rain_72h = sum(
            rain_by_hour.get(hour, 0.0)
            for hour in range(max(0, h - 71), h + 1)
        )
        rain_7d = sum(
            rain_by_hour.get(hour, 0.0)
            for hour in range(max(0, h - 167), h + 1)
        )

        # Apply saturation factor
        rain_24h_eff = rain_24h * saturation
        rain_72h_eff = rain_72h * saturation

        # --- Tide: find coefficient and hours to high tide ---
        tide_coef = 70
        hours_to_high = 6.0

        # Find closest high tide (before or after)
        high_tides = [t for t in tide_events if t["type"] == "PM"]
        if high_tides:
            closest_ht = min(
                high_tides,
                key=lambda t: abs((t["timestamp"] - forecast_time).total_seconds()),
            )
            hours_to_high = (closest_ht["timestamp"] - forecast_time).total_seconds() / 3600
            if closest_ht["coefficient"]:
                tide_coef = closest_ht["coefficient"]

        # --- River level projection with station scores (v3.2) ---
        projected_level = 2.0  # Default baseline
        total_weight = 0.0
        weighted_sum = 0.0
        projected_station_scores: list[StationRiskScore] = []

        for station_cfg in settings.hubeau_stations:
            sid = station_cfg["id"]
            weight = station_cfg.get("weight", 0)
            delay = station_cfg.get("delay_hours", 0)

            if sid not in current_levels:
                continue

            # Project level forward
            if h >= delay:
                base_level = current_levels[sid]
                rate = current_rates.get(sid, 0)
                # Project forward but cap the extrapolation
                projected = base_level + (rate / 100) * min(h - delay, 12)
                projected = max(0, min(projected, base_level + 2.0))
            else:
                projected = current_levels[sid]

            # Calculate station score using thresholds
            thresholds = station_thresholds.get(sid, (3.0, 3.8, 4.5))
            station_score = _normalize_score(projected, thresholds)

            # Project tendency with decay
            base_rate = current_rates.get(sid, 0)
            decay_factor = max(0.0, 1.0 - h / 24.0) if h > 0 else 1.0
            proj_rate = base_rate * decay_factor
            if proj_rate > 1.0:
                proj_tendency = Tendency.RISING
            elif proj_rate < -1.0:
                proj_tendency = Tendency.FALLING
            else:
                proj_tendency = Tendency.STABLE

            projected_station_scores.append(StationRiskScore(
                station_id=sid,
                station_name=station_names.get(sid, sid),
                river=station_rivers.get(sid, "Unknown"),
                height_m=projected,
                score=station_score,
                weight=weight,
                weighted_score=station_score * weight if weight > 0 else 0,
                tendency=proj_tendency,
                rate_cm_per_hour=proj_rate,
            ))

            if weight > 0:
                weighted_sum += projected * weight
                total_weight += weight

        if total_weight > 0:
            projected_level = weighted_sum / total_weight

        # --- Calculate factor scores ---
        rain_24h_score = _normalize_score(
            rain_24h_eff,
            (settings.risk_rain_24h_yellow, settings.risk_rain_24h_orange, settings.risk_rain_24h_red),
        )
        rain_72h_score = _normalize_score(
            rain_72h_eff,
            (settings.risk_rain_24h_yellow * 2, settings.risk_rain_24h_orange * 2, settings.risk_rain_24h_red * 2),
        )
        rain_7d_score = _normalize_score(rain_7d, (60.0, 100.0, 150.0))
        river_score = _normalize_score(
            projected_level,
            (settings.risk_dordogne_level_yellow, settings.risk_dordogne_level_orange, settings.risk_dordogne_level_red),
        )
        tide_score = _normalize_score(tide_coef, (70, settings.risk_tide_coef_high, 110))

        # Tide phase score
        if abs(hours_to_high) <= 1:
            tide_phase_score = 1.0
        elif abs(hours_to_high) <= 2:
            tide_phase_score = 0.8
        elif abs(hours_to_high) <= 3:
            tide_phase_score = 0.5
        elif abs(hours_to_high) <= 4:
            tide_phase_score = 0.3
        else:
            tide_phase_score = 0.1

        # Tendency score: carry current momentum forward, decaying over time
        # Beyond 24h, assume momentum decays to baseline
        decay = max(0.0, 1.0 - h / 48.0) if h > 0 else 1.0
        proj_n_rising = max(0, round(n_rising * decay))
        if proj_n_rising >= 3:
            tendency_score = 0.7
        elif proj_n_rising >= 1:
            tendency_score = 0.5
        else:
            tendency_score = 0.0

        tendency = Tendency.RISING if proj_n_rising >= 2 else (Tendency.STABLE if proj_n_rising == 1 else Tendency.FALLING)

        # Momentum score (v3.0 with river gate)
        momentum_score = _calculate_momentum_score(
            n_rising=proj_n_rising, n_total=n_total, river_score=river_score,
        )

        # Conjunction multiplier (v3.2 with station_scores)
        conjunction = _calculate_conjunction_multiplier(
            river_score, tide_phase_score, tendency,
            rain_72h_score=rain_72h_score, rain_24h_score=rain_24h_score,
            n_rising=proj_n_rising,
            station_scores=projected_station_scores,
        )

        # Stability dampener (v3.2 with station_scores)
        dampener = _calculate_stability_dampener(
            river_score, tendency, n_rising=proj_n_rising,
            rain_72h_score=rain_72h_score,
            station_scores=projected_station_scores,
        )

        # Weighted calculation (v3.0 weights)
        weighted = (
            river_score * weights["river"] +
            rain_24h_score * weights["rain_24h"] +
            rain_72h_score * weights["rain_72h"] +
            rain_7d_score * weights["rain_7d"] +
            tide_score * weights["tide"] +
            tide_phase_score * weights["tide_phase"] +
            tendency_score * weights["tendency"] +
            momentum_score * weights["momentum"]
        )

        weighted *= conjunction
        weighted *= dampener
        score = min((weighted / total_w) * 100, 100)

        level = _score_to_level(score)

        forecasts.append({
            "timestamp": forecast_time.isoformat(),
            "hours_ahead": h,
            "score": round(score, 1),
            "level": level.value,
            "rain_1h_mm": round(rain_by_hour.get(h, 0.0), 1),
            "rain_24h_mm": round(rain_24h, 1),
            "rain_72h_mm": round(rain_72h, 1),
            "tide_coefficient": tide_coef,
            "hours_to_high_tide": round(hours_to_high, 1),
            "projected_level_m": round(projected_level, 2),
        })

    # Find critical periods
    critical = [f for f in forecasts if f["level"] in ["orange", "red"]]

    # Generate summary
    max_score = max(f["score"] for f in forecasts) if forecasts else 0
    max_level = _score_to_level(max_score)

    if max_level == RiskLevel.RED:
        summary = f"ALERTE: Risque ROUGE prévu (max {max_score:.0f}/100)"
    elif max_level == RiskLevel.ORANGE:
        summary = f"Vigilance: Risque ORANGE prévu (max {max_score:.0f}/100)"
    elif max_level == RiskLevel.YELLOW:
        summary = f"Attention: Risque JAUNE prévu (max {max_score:.0f}/100)"
    else:
        summary = "Conditions favorables prévues"

    # Count critical hours
    red_hours = sum(1 for f in forecasts if f["level"] == "red")
    orange_hours = sum(1 for f in forecasts if f["level"] == "orange")
    if red_hours > 0:
        summary += f" - {red_hours}h en rouge"
    if orange_hours > 0:
        summary += f" - {orange_hours}h en orange"

    # Duration label
    if hours >= 168:
        duration_label = "7 jours"
    elif hours >= 72:
        duration_label = f"{hours}h"
    else:
        duration_label = f"{hours}h"

    result = {
        "generated_at": now.isoformat(),
        "hours_ahead": hours,
        "duration_label": duration_label,
        "forecasts": forecasts,
        "max_risk": round(max_score, 1),
        "max_level": max_level.value,
        "critical_periods": critical,
        "summary": summary,
        "data_sources": {
            "rain": "Open-Meteo (AROME/ARPEGE)",
            "tide": tide_data.port,
            "river": f"{len(current_levels)} stations",
            "soil_saturation": round(saturation, 2),
        },
    }

    # Cache result
    await _set_cached_forecast(hours, result)

    return result


async def invalidate_forecast_cache() -> None:
    """Invalidate forecast cache (call when new data arrives)."""
    try:
        client = await _get_redis_client()
        keys = await client.keys("forecast:*")
        if keys:
            await client.delete(*keys)
        await client.aclose()
    except Exception as e:
        print(f"Redis cache invalidate error: {e}")
