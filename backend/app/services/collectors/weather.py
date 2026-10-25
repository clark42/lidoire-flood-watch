"""Weather data collector.

Fetches weather data from:
- Open-Meteo (primary, free)
- Météo-France API (secondary)
- Infoclimat (for local observations)

Includes:
- Precipitation forecast and historical
- Wind speed and direction (for surge calculation)
- Soil moisture (for saturation factor)
"""

import asyncio
import logging
import math
import time
import httpx
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import get_settings
from app.models.schemas import WeatherForecast, RainfallObservation, RainfallSummary

logger = logging.getLogger(__name__)

# Open-Meteo API endpoints
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/meteofrance"
OPEN_METEO_HISTORICAL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_CURRENT = "https://api.open-meteo.com/v1/forecast"

# Multi-model forecast endpoints (for ensemble averaging)
OPEN_METEO_MODELS = {
    "meteofrance": "https://api.open-meteo.com/v1/meteofrance",  # AROME/ARPEGE - best for France
    "ecmwf": "https://api.open-meteo.com/v1/ecmwf",  # European model - very reliable
    "gfs": "https://api.open-meteo.com/v1/gfs",  # US model - good for 10+ days
    "best_match": "https://api.open-meteo.com/v1/forecast",  # Open-Meteo best match
}

# Cache
_weather_cache: dict = {}
_cache_expiry: Optional[datetime] = None

# In-memory TTL cache for Open-Meteo past hourly rain, keyed by (lat, lon).
# Shared by get_rainfall_summary, get_past_rainfall_sums (Netatmo fallback)
# and fetch_past_hourly_rain (forecast seeding): one HTTP call per 15 min.
PAST_RAIN_CACHE_TTL_S = 15 * 60
_past_rain_cache: dict[tuple[float, float], tuple[float, list[tuple[datetime, float]]]] = {}
_past_rain_lock = asyncio.Lock()


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# Soil moisture thresholds for saturation factor
# m³/m³ volumetric water content
SOIL_MOISTURE_DRY = 0.15  # Below this = dry soil (factor 1.0)
SOIL_MOISTURE_NORMAL = 0.25  # Normal (factor ~1.15)
SOIL_MOISTURE_WET = 0.35  # Wet (factor ~1.3)
SOIL_MOISTURE_SATURATED = 0.45  # Saturated (factor 1.5)


async def fetch_open_meteo_forecast(lat: float, lon: float, forecast_days: int = 14) -> list[WeatherForecast]:
    """
    Fetch weather forecast from Open-Meteo with automatic model blending.

    Uses Météo-France (AROME/ARPEGE) for days 0-4 (best accuracy in France),
    then blends in ECMWF for days 5+ where Météo-France has no data.

    Args:
        lat: Latitude
        lon: Longitude
        forecast_days: Number of days to forecast (default 14, max 16)

    Timestamps are naive UTC. Open-Meteo hourly precipitation at label T is the
    sum over the preceding hour [T-1h, T).
    """
    try:
        async with httpx.AsyncClient() as client:
            # Fetch both Météo-France (short range, high accuracy) and ECMWF (long range)
            mf_response, ecmwf_response = await asyncio.gather(
                client.get(
                    OPEN_METEO_FORECAST,  # Météo-France
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "hourly": "precipitation,rain,temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
                        "forecast_days": min(forecast_days, 16),
                        # UTC: all callers compare these naive timestamps to utcnow()
                        "timezone": "UTC",
                    },
                    timeout=30.0,
                ),
                client.get(
                    OPEN_METEO_MODELS["ecmwf"],  # ECMWF for long range
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "hourly": "precipitation,temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
                        "forecast_days": min(forecast_days, 16),
                        "timezone": "UTC",
                    },
                    timeout=30.0,
                ),
            )
            mf_response.raise_for_status()
            ecmwf_response.raise_for_status()

            mf_data = mf_response.json()
            ecmwf_data = ecmwf_response.json()

            # Build ECMWF lookup for fallback
            ecmwf_hourly = ecmwf_data.get("hourly", {})
            ecmwf_times = ecmwf_hourly.get("time", [])
            ecmwf_precip = ecmwf_hourly.get("precipitation", [])
            ecmwf_lookup = {
                ecmwf_times[i]: ecmwf_precip[i]
                for i in range(len(ecmwf_times))
                if i < len(ecmwf_precip) and ecmwf_precip[i] is not None
            }

            forecasts = []
            hourly = mf_data.get("hourly", {})
            times = hourly.get("time", [])
            precip = hourly.get("precipitation", [])
            rain = hourly.get("rain", [])
            temp = hourly.get("temperature_2m", [])
            humidity = hourly.get("relative_humidity_2m", [])
            wind = hourly.get("wind_speed_10m", [])
            wind_dir = hourly.get("wind_direction_10m", [])

            for i, time_str in enumerate(times):
                try:
                    # Use Météo-France if available, otherwise ECMWF
                    mf_precip = precip[i] if i < len(precip) else None
                    if mf_precip is None:
                        # Fallback to ECMWF
                        mf_precip = ecmwf_lookup.get(time_str, 0)

                    forecasts.append(
                        WeatherForecast(
                            timestamp=datetime.fromisoformat(time_str),
                            point_id="moulin",
                            point_name="Moulin de la Lidoire",
                            precipitation_mm=mf_precip if mf_precip is not None else 0,
                            temperature_c=temp[i] if i < len(temp) else None,
                            humidity_percent=humidity[i] if i < len(humidity) else None,
                            wind_speed_kmh=wind[i] if i < len(wind) else None,
                            wind_direction_deg=wind_dir[i] if i < len(wind_dir) else None,
                        )
                    )
                except (IndexError, ValueError):
                    continue

            return forecasts

    except Exception as e:
        logger.warning(f"Error fetching Open-Meteo forecast: {e}")
        return []


async def fetch_multi_model_forecast(
    lat: float, lon: float, forecast_days: int = 14
) -> dict[str, list[dict]]:
    """
    Fetch precipitation forecasts from multiple models and return ensemble data.

    Returns daily precipitation forecasts from:
    - Open-Meteo Météo-France (AROME/ARPEGE)
    - Open-Meteo ECMWF (European model)
    - Open-Meteo GFS (US model)
    - Open-Meteo Best Match (auto-select)
    - Météo Agricole (scraped, 10-day)

    Returns:
        Dict with:
        - models: dict of model_name -> list of {date, rain_mm}
        - ensemble: list of {date, rain_mm, min_mm, max_mm, sources}
    """
    import asyncio
    from statistics import median

    async def fetch_model(model_name: str, url: str) -> tuple[str, list[dict]]:
        """Fetch daily precipitation from a single Open-Meteo model."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "daily": "precipitation_sum",
                        "forecast_days": min(forecast_days, 16),
                        "timezone": "Europe/Paris",
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json()

                daily = data.get("daily", {})
                dates = daily.get("time", [])
                precip = daily.get("precipitation_sum", [])

                return model_name, [
                    {"date": dates[i], "rain_mm": precip[i] if i < len(precip) else 0}
                    for i in range(len(dates))
                ]
        except Exception as e:
            logger.warning(f"Error fetching {model_name} forecast: {e}")
            return model_name, []

    # Fetch all Open-Meteo models in parallel
    tasks = [
        fetch_model(name, url)
        for name, url in OPEN_METEO_MODELS.items()
    ]
    results = await asyncio.gather(*tasks)

    models = {name: data for name, data in results if data}

    # Try to add Météo Agricole
    try:
        from app.services.collectors.meteo_agricole import fetch_meteo_agricole
        ma_data = await fetch_meteo_agricole()
        if ma_data and ma_data.rain_forecast_7d_mm > 0:
            # Météo Agricole gives total 7d, we distribute it roughly
            # based on probability pattern (more rain in middle of period)
            models["meteo_agricole"] = [{
                "date": "7d_total",
                "rain_mm": ma_data.rain_forecast_7d_mm,
                "rain_min_mm": ma_data.rain_min_mm,
                "rain_max_mm": ma_data.rain_max_mm,
            }]
    except Exception as e:
        logger.warning(f"Error fetching Meteo Agricole for ensemble: {e}")

    # Build ensemble (average across models for each day)
    all_dates = set()
    for model_data in models.values():
        for point in model_data:
            if point.get("date") and point["date"] != "7d_total":
                all_dates.add(point["date"])

    ensemble = []
    for date in sorted(all_dates):
        values = []
        sources = []
        for model_name, model_data in models.items():
            if model_name == "meteo_agricole":
                continue  # Skip, different format
            for point in model_data:
                if point.get("date") == date:
                    val = point.get("rain_mm")
                    if val is not None:
                        values.append(val)
                        sources.append(model_name)
                    break

        if values:
            ensemble.append({
                "date": date,
                "rain_mm": round(sum(values) / len(values), 1),  # Mean
                "rain_median_mm": round(median(values), 1) if len(values) >= 3 else None,
                "rain_min_mm": round(min(values), 1),
                "rain_max_mm": round(max(values), 1),
                "sources": sources,
                "n_sources": len(sources),
            })

    return {
        "models": models,
        "ensemble": ensemble,
        "generated_at": _utcnow().isoformat(),
    }


def _current_hour_index(times: list[str], now: datetime) -> int:
    """Index of the last hourly label <= now (naive UTC). 0 if none parse."""
    idx = 0
    for i, time_str in enumerate(times):
        try:
            if datetime.fromisoformat(time_str) <= now:
                idx = i
            else:
                break
        except (TypeError, ValueError):
            continue
    return idx


async def fetch_soil_and_wind(lat: float, lon: float) -> dict:
    """
    Fetch current soil moisture and wind data from Open-Meteo.

    Includes agricultural parameters:
    - et0_fao_evapotranspiration (mm/day)
    - soil_temperature_0cm (frost = runoff)
    - soil_moisture_9_to_27cm + soil_moisture_27_to_81cm (deep layers)
    - precipitation_probability

    Returns:
        dict with soil_moisture (m³/m³), wind_speed_kmh, wind_direction_deg,
        and calculated soil_saturation_factor and westerly_wind_kmh
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                OPEN_METEO_CURRENT,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": ",".join([
                        "soil_moisture_0_to_1cm",
                        "soil_moisture_1_to_3cm",
                        "soil_moisture_3_to_9cm",
                        "soil_moisture_9_to_27cm",
                        "soil_moisture_27_to_81cm",
                        "soil_temperature_0cm",
                        "et0_fao_evapotranspiration",
                        "precipitation_probability",
                        "wind_speed_10m",
                        "wind_direction_10m",
                    ]),
                    # Daily ET0 sum (mm/day): the hourly value is mm/hour and
                    # cannot be compared to the mm/day thresholds below.
                    "daily": "et0_fao_evapotranspiration",
                    "forecast_days": 1,
                    "timezone": "UTC",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            hourly = data.get("hourly", {})
            # Index of the current UTC hour in the (UTC) hourly series.
            # Name kept as current_hour for the lookups below.
            current_hour = _current_hour_index(hourly.get("time", []), _utcnow())

            soil_0_1 = hourly.get("soil_moisture_0_to_1cm", [])
            soil_1_3 = hourly.get("soil_moisture_1_to_3cm", [])
            soil_3_9 = hourly.get("soil_moisture_3_to_9cm", [])
            soil_9_27 = hourly.get("soil_moisture_9_to_27cm", [])
            soil_27_81 = hourly.get("soil_moisture_27_to_81cm", [])
            soil_temp_list = hourly.get("soil_temperature_0cm", [])
            et0_list = hourly.get("et0_fao_evapotranspiration", [])
            precip_prob_list = hourly.get("precipitation_probability", [])
            wind_speed = hourly.get("wind_speed_10m", [])
            wind_dir = hourly.get("wind_direction_10m", [])

            # Average soil moisture across shallow depths (0-9cm)
            soil_values = []
            for depth in [soil_0_1, soil_1_3, soil_3_9]:
                if current_hour < len(depth) and depth[current_hour] is not None:
                    soil_values.append(depth[current_hour])

            soil_moisture = sum(soil_values) / len(soil_values) if soil_values else 0.25

            # Deep moisture (9-81cm)
            deep_values = []
            for depth in [soil_9_27, soil_27_81]:
                if current_hour < len(depth) and depth[current_hour] is not None:
                    deep_values.append(depth[current_hour])
            moisture_deep = sum(deep_values) / len(deep_values) if deep_values else None

            # Soil temperature
            soil_temp = (
                soil_temp_list[current_hour]
                if current_hour < len(soil_temp_list) and soil_temp_list[current_hour] is not None
                else None
            )

            # ET0 evapotranspiration (mm/day): daily sum, hourly fallback x24
            daily_et0 = (data.get("daily", {}) or {}).get("et0_fao_evapotranspiration", []) or []
            if daily_et0 and daily_et0[0] is not None:
                et0_mm = daily_et0[0]
            elif current_hour < len(et0_list) and et0_list[current_hour] is not None:
                et0_mm = et0_list[current_hour] * 24
            else:
                et0_mm = None

            # Precipitation probability
            precip_prob = (
                precip_prob_list[current_hour]
                if current_hour < len(precip_prob_list) and precip_prob_list[current_hour] is not None
                else None
            )

            # Calculate saturation factor (1.0 to 1.5)
            if soil_moisture <= SOIL_MOISTURE_DRY:
                saturation_factor = 1.0
            elif soil_moisture >= SOIL_MOISTURE_SATURATED:
                saturation_factor = 1.5
            else:
                # Linear interpolation
                ratio = (soil_moisture - SOIL_MOISTURE_DRY) / (SOIL_MOISTURE_SATURATED - SOIL_MOISTURE_DRY)
                saturation_factor = 1.0 + (ratio * 0.5)

            # Agricultural adjustments to saturation factor
            # Deep moisture high (>0.40 m³/m³) = water table high -> +0.1
            if moisture_deep is not None and moisture_deep > 0.40:
                saturation_factor = min(1.5, saturation_factor + 0.1)
            # Frozen soil = impermeable -> force max runoff
            if soil_temp is not None and soil_temp < 0:
                saturation_factor = 1.5
            # Low ET0 (<0.5 mm/day) = little evaporation -> +0.05
            if et0_mm is not None and et0_mm < 0.5:
                saturation_factor = min(1.5, saturation_factor + 0.05)

            saturation_factor = round(saturation_factor, 2)

            # Get current wind (None-safe)
            current_wind_speed = (
                wind_speed[current_hour]
                if current_hour < len(wind_speed) and wind_speed[current_hour] is not None
                else 0
            )
            current_wind_dir = (
                wind_dir[current_hour]
                if current_hour < len(wind_dir) and wind_dir[current_hour] is not None
                else 0
            )

            # Calculate westerly wind component
            # West = 270°, we want winds from 225° (SW) to 315° (NW)
            wind_rad = math.radians(current_wind_dir)
            # Westerly component: positive when wind comes from west
            westerly_component = current_wind_speed * math.cos(math.radians(270) - wind_rad)
            westerly_wind = max(0, westerly_component)  # Only positive (from west)

            return {
                "soil_moisture": round(soil_moisture, 3),
                "moisture_deep": round(moisture_deep, 3) if moisture_deep is not None else None,
                "soil_temperature_0cm": round(soil_temp, 1) if soil_temp is not None else None,
                "et0_mm": round(et0_mm, 2) if et0_mm is not None else None,
                "precip_probability": precip_prob,
                "soil_saturation_factor": saturation_factor,
                "wind_speed_kmh": current_wind_speed,
                "wind_direction_deg": current_wind_dir,
                "westerly_wind_kmh": round(westerly_wind, 1),
            }

    except Exception as e:
        logger.warning(f"Error fetching soil/wind data: {e}")
        return {
            "soil_moisture": 0.25,
            "moisture_deep": None,
            "soil_temperature_0cm": None,
            "et0_mm": None,
            "precip_probability": None,
            "soil_saturation_factor": 1.0,
            "wind_speed_kmh": 0,
            "wind_direction_deg": 0,
            "westerly_wind_kmh": 0,
        }


async def fetch_open_meteo_historical(
    lat: float,
    lon: float,
    start_date: datetime,
    end_date: datetime,
) -> list[RainfallObservation]:
    """Fetch historical weather data from Open-Meteo archive."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                OPEN_METEO_HISTORICAL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "start_date": start_date.strftime("%Y-%m-%d"),
                    "end_date": end_date.strftime("%Y-%m-%d"),
                    "hourly": "precipitation,rain",
                    "timezone": "UTC",
                },
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()

            observations = []
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            precip = hourly.get("precipitation", [])

            for i, time_str in enumerate(times):
                # Archive (ERA5) lags several days: missing hours are null.
                # Skip them instead of reporting 0 mm.
                p = precip[i] if i < len(precip) else None
                if p is None:
                    continue
                try:
                    observations.append(
                        RainfallObservation(
                            timestamp=datetime.fromisoformat(time_str),
                            source="open-meteo-archive",
                            precipitation_mm_h=p,
                            basin_avg=False,
                        )
                    )
                except (IndexError, ValueError):
                    continue

            return observations

    except Exception as e:
        logger.warning(f"Error fetching Open-Meteo historical data: {e}")
        return []


async def fetch_basin_rainfall(points: list[dict]) -> list[RainfallObservation]:
    """
    Fetch rainfall for multiple points in the Lidoire basin and average.
    """
    all_forecasts = []

    for point in points:
        forecasts = await fetch_open_meteo_forecast(point["lat"], point["lon"])
        all_forecasts.append(forecasts)

    if not all_forecasts or not all_forecasts[0]:
        return []

    # Average across all points
    observations = []
    num_points = len(all_forecasts)

    for i in range(len(all_forecasts[0])):
        total_precip = sum(
            fc[i].precipitation_mm
            for fc in all_forecasts
            if i < len(fc)
        )
        avg_precip = total_precip / num_points

        observations.append(
            RainfallObservation(
                timestamp=all_forecasts[0][i].timestamp,
                source="open-meteo",
                precipitation_mm_h=avg_precip,
                basin_avg=True,
            )
        )

    return observations


async def get_rainfall_summary() -> RainfallSummary:
    """
    Get current rainfall summary for the basin (moulin point).

    Includes:
    - Observed rainfall (1h, 6h, 24h, 72h, 7d) from the Open-Meteo forecast
      endpoint with past_days in UTC (see get_past_rainfall_sums): windows
      contain the N latest *complete* hours, never future hours.
    - Forecast rainfall (24h, 72h): hourly labels in (now, now + N h].
    - Soil moisture and saturation factor
    """
    global _weather_cache, _cache_expiry

    settings = get_settings()
    now = _utcnow()

    # Check cache (15 min for rainfall data)
    if _cache_expiry and _cache_expiry > now and "summary" in _weather_cache:
        return _weather_cache["summary"]

    forecasts, past, soil_wind = await asyncio.gather(
        fetch_open_meteo_forecast(settings.moulin_lat, settings.moulin_lon),
        get_past_rainfall_sums(settings.moulin_lat, settings.moulin_lon),
        fetch_soil_and_wind(settings.moulin_lat, settings.moulin_lon),
    )
    if not past:
        logger.warning("Rainfall summary: Open-Meteo past rain unavailable, observed sums set to 0")

    # Calculate forecast rainfall sums (strictly future hours)
    forecast_24h = 0.0
    forecast_72h = 0.0
    for f in forecasts or []:
        hours_ahead = (f.timestamp - now).total_seconds() / 3600
        if 0 < hours_ahead <= 24:
            forecast_24h += f.precipitation_mm
        if 0 < hours_ahead <= 72:
            forecast_72h += f.precipitation_mm

    summary = RainfallSummary(
        last_hour_mm=past.get("rain_1h_mm", 0.0),
        last_6h_mm=past.get("rain_6h_mm", 0.0),
        last_24h_mm=past.get("rain_24h_mm", 0.0),
        last_72h_mm=past.get("rain_72h_mm", 0.0),
        last_7d_mm=past.get("rain_7d_mm", 0.0),
        forecast_24h_mm=round(forecast_24h, 1),
        forecast_72h_mm=round(forecast_72h, 1),
        soil_moisture=soil_wind["soil_moisture"],
        soil_saturation_factor=soil_wind["soil_saturation_factor"],
    )

    # Update cache (15 min for rainfall)
    _weather_cache["summary"] = summary
    _cache_expiry = now + timedelta(minutes=15)

    return summary


async def get_forecast_rainfall(hours: int = 72) -> list[WeatherForecast]:
    """Get hourly rainfall forecast."""
    settings = get_settings()
    forecasts = await fetch_open_meteo_forecast(settings.moulin_lat, settings.moulin_lon)

    now = _utcnow()
    cutoff = now + timedelta(hours=hours)

    return [f for f in forecasts if f.timestamp <= cutoff]


async def get_historical_rainfall(
    start: datetime,
    end: datetime,
) -> list[RainfallObservation]:
    """Get historical rainfall data."""
    settings = get_settings()

    # Fetch from Open-Meteo archive
    observations = await fetch_open_meteo_historical(
        settings.moulin_lat,
        settings.moulin_lon,
        start,
        end,
    )

    return observations


async def get_soil_wind_cached() -> dict:
    """Get soil/wind data with InfluxDB cache-through.

    1. Check InfluxDB for recent data (< 1h)
    2. If present: return from InfluxDB
    3. Otherwise: fetch Open-Meteo, store to InfluxDB, return

    Returns:
        dict with all soil/wind fields
    """
    from app.services import influx_client

    settings = get_settings()

    # 1. Check InfluxDB for recent soil data
    cached = await influx_client.query_latest_point("soil_moisture", "moulin", max_age_hours=1)
    if cached and "moisture" in cached:
        # Also get wind data
        wind_cached = await influx_client.query_latest_point("wind", "moulin", max_age_hours=1)
        wind_speed = wind_cached.get("speed_kmh", 0) if wind_cached else 0
        wind_dir = wind_cached.get("direction_deg", 0) if wind_cached else 0
        westerly = wind_cached.get("westerly_kmh", 0) if wind_cached else 0

        # Get agricultural data if available
        agri_cached = await influx_client.query_latest_point("soil_agricultural", "moulin", max_age_hours=1)

        logger.info("soil/wind: from InfluxDB cache")
        return {
            "soil_moisture": cached.get("moisture", 0.25),
            "moisture_deep": agri_cached.get("moisture_deep") if agri_cached else None,
            "soil_temperature_0cm": agri_cached.get("temperature_0cm") if agri_cached else None,
            "et0_mm": agri_cached.get("et0_mm") if agri_cached else None,
            "precip_probability": agri_cached.get("precip_probability") if agri_cached else None,
            "soil_saturation_factor": cached.get("saturation_factor", 1.0),
            "wind_speed_kmh": wind_speed,
            "wind_direction_deg": wind_dir,
            "westerly_wind_kmh": westerly,
        }

    # 2. Fetch from Open-Meteo
    logger.info("soil/wind: fetching from Open-Meteo API")
    data = await fetch_soil_and_wind(settings.moulin_lat, settings.moulin_lon)

    # 3. Store to InfluxDB
    now = _utcnow()
    try:
        points = [
            influx_client.make_point(
                measurement="soil_moisture",
                tags={"location": "moulin"},
                fields={
                    "moisture": data["soil_moisture"],
                    "saturation_factor": data["soil_saturation_factor"],
                },
                timestamp=now,
            ),
            influx_client.make_point(
                measurement="wind",
                tags={"location": "moulin"},
                fields={
                    "speed_kmh": data["wind_speed_kmh"],
                    "direction_deg": data["wind_direction_deg"],
                    "westerly_kmh": data["westerly_wind_kmh"],
                },
                timestamp=now,
            ),
        ]
        # Store agricultural data
        agri_fields = {}
        if data.get("moisture_deep") is not None:
            agri_fields["moisture_deep"] = data["moisture_deep"]
        if data.get("soil_temperature_0cm") is not None:
            agri_fields["temperature_0cm"] = data["soil_temperature_0cm"]
        if data.get("et0_mm") is not None:
            agri_fields["et0_mm"] = data["et0_mm"]
        if data.get("precip_probability") is not None:
            agri_fields["precip_probability"] = float(data["precip_probability"])
        if agri_fields:
            points.append(influx_client.make_point(
                measurement="soil_agricultural",
                tags={"location": "moulin"},
                fields=agri_fields,
                timestamp=now,
            ))
        await influx_client.write_points_batch(points)
    except Exception as e:
        logger.warning(f"Error storing soil/wind to InfluxDB: {e}")

    return data


async def get_current_wind() -> dict:
    """
    Get current wind data for surge calculation (with InfluxDB cache-through).

    Returns:
        dict with wind_speed_kmh, wind_direction_deg, westerly_wind_kmh
    """
    data = await get_soil_wind_cached()

    return {
        "wind_speed_kmh": data["wind_speed_kmh"],
        "wind_direction_deg": data["wind_direction_deg"],
        "westerly_wind_kmh": data["westerly_wind_kmh"],
    }


def _parse_hourly_series(data: dict) -> list[tuple[datetime, float]]:
    """Parse an Open-Meteo hourly precipitation payload (timezone=UTC).

    Returns sorted (label, mm) pairs; null values are skipped (missing, not 0).
    """
    hourly = (data or {}).get("hourly", {}) or {}
    times = hourly.get("time", []) or []
    precip = hourly.get("precipitation", []) or []
    series: list[tuple[datetime, float]] = []
    for time_str, p in zip(times, precip):
        if p is None:
            continue
        try:
            series.append((datetime.fromisoformat(time_str), float(p)))
        except (TypeError, ValueError):
            continue
    series.sort(key=lambda x: x[0])
    return series


def sum_rain_windows(
    series: list[tuple[datetime, float]],
    now: datetime,
    windows_h: tuple[int, ...] = (1, 6, 24, 72, 168),
) -> dict[int, float]:
    """Sum hourly rain over trailing windows ending at ``now`` (naive UTC).

    Label T holds rain over [T-1h, T). An hour is counted in the N-hour window
    when it is complete (T <= now) and 0 <= now - T < N h, i.e. the N latest
    complete hours, which approximates [now - N h, now). Future (forecast)
    hours are never counted.
    """
    sums = {w: 0.0 for w in windows_h}
    for ts, p in series:
        hours_ago = (now - ts).total_seconds() / 3600
        if hours_ago < 0:
            continue
        for w in windows_h:
            if hours_ago < w:
                sums[w] += p
    return {w: round(v, 1) for w, v in sums.items()}


def hourly_offsets(
    series: list[tuple[datetime, float]],
    now: datetime,
    past_days: int = 7,
) -> dict[int, float]:
    """Map complete past hours to negative offsets: -1 = latest complete hour.

    Offset = -(floor((now - T) / 1h) + 1) for labels T <= now, so the hour that
    ended less than one hour ago is -1 (it used to be lost by int() truncation).
    """
    result: dict[int, float] = {}
    max_hours = past_days * 24
    for ts, p in series:
        hours_ago = (now - ts).total_seconds() / 3600
        if hours_ago < 0:
            continue
        offset = -(math.floor(hours_ago) + 1)
        if -offset <= max_hours:
            result[offset] = p
    return result


async def _fetch_past_hourly_series(lat: float, lon: float) -> list[tuple[datetime, float]]:
    """Hourly precipitation for the last 7 days + today (UTC), cached 15 min.

    Uses the forecast endpoint (no multi-day lag, unlike the ERA5 archive).
    ``forecast_days=1`` is required: with ``forecast_days=0`` Open-Meteo stops at
    yesterday 23:00 and today's hours would be missing. Future labels are
    filtered by the consumers. Returns [] on error (errors are not cached).
    """
    key = (round(lat, 4), round(lon, 4))
    cached = _past_rain_cache.get(key)
    if cached and time.monotonic() - cached[0] < PAST_RAIN_CACHE_TTL_S:
        return cached[1]

    async with _past_rain_lock:
        cached = _past_rain_cache.get(key)
        if cached and time.monotonic() - cached[0] < PAST_RAIN_CACHE_TTL_S:
            return cached[1]
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    OPEN_METEO_CURRENT,
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "hourly": "precipitation",
                        "past_days": 7,
                        "forecast_days": 1,
                        "timezone": "UTC",
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
                series = _parse_hourly_series(response.json())
        except Exception as e:
            logger.warning("Error fetching past hourly rain from Open-Meteo: %s", e)
            return []

        if series:
            _past_rain_cache[key] = (time.monotonic(), series)
        return series


async def fetch_past_hourly_rain(lat: float, lon: float, past_days: int = 7) -> dict[int, float]:
    """
    Fetch past observed hourly precipitation from Open-Meteo.

    Uses the standard forecast endpoint with past_days parameter, which
    provides observed data for the recent past (no multi-day lag).

    Returns:
        Dict mapping hour_offset (negative int, -1 = latest complete hour,
        i.e. the hour that ended less than 1h ago) to precipitation_mm.
    """
    series = await _fetch_past_hourly_series(lat, lon)
    return hourly_offsets(series, _utcnow(), past_days)


async def get_past_rainfall_sums(lat: float, lon: float) -> dict:
    """
    Get past rainfall sums from Open-Meteo forecast API (past_days parameter).

    Uses the standard forecast endpoint which provides observed data
    for the recent past, unlike the archive API which has a multi-day lag.
    Windows follow sum_rain_windows (latest complete hours, UTC).

    Returns:
        Dict with keys: rain_1h_mm, rain_6h_mm, rain_24h_mm, rain_72h_mm,
        rain_7d_mm (in mm), or {} when Open-Meteo is unavailable.
    """
    series = await _fetch_past_hourly_series(lat, lon)
    if not series:
        return {}
    sums = sum_rain_windows(series, _utcnow())
    return {
        "rain_1h_mm": sums[1],
        "rain_6h_mm": sums[6],
        "rain_24h_mm": sums[24],
        "rain_72h_mm": sums[72],
        "rain_7d_mm": sums[168],
    }


async def get_last_complete_hour_rain(lat: float, lon: float) -> Optional[tuple[datetime, float]]:
    """Latest complete hour (label T <= now, rain over [T-1h, T)) or None."""
    series = await _fetch_past_hourly_series(lat, lon)
    now = _utcnow()
    past = [(ts, p) for ts, p in series if ts <= now]
    return past[-1] if past else None


async def collect_and_store_weather():
    """Collect weather data and store in database.

    The Open-Meteo rainfall point is written at the label of the latest complete
    hour (same timestamp convention as fetch_and_store_historical_rainfall), so
    the "open-meteo / Moulin-Lidoire" series keeps exactly one point per hour
    and sum aggregations stay correct.

    Uses get_soil_wind_cached() which handles its own InfluxDB storage,
    avoiding double writes for soil/wind data.
    """
    from app.services import influx_client

    settings = get_settings()
    stored = 0

    summary = await get_rainfall_summary()
    last_hour = await get_last_complete_hour_rain(settings.moulin_lat, settings.moulin_lon)

    if last_hour is not None:
        hour_ts, hour_mm = last_hour
        try:
            success = await influx_client.write_rainfall(
                source="open-meteo",
                precipitation_mm_h=hour_mm,
                timestamp=hour_ts,
                precipitation_mm_24h=summary.last_24h_mm,
                station_city="Moulin-Lidoire",
            )
            if success:
                stored += 1
        except Exception as e:
            logger.warning("Error storing rainfall to InfluxDB: %s", e)
    else:
        logger.warning("No Open-Meteo past rain available: rainfall point not stored")

    # Fetch soil/wind data via cache-through (handles its own InfluxDB storage)
    try:
        await get_soil_wind_cached()
        stored += 2  # soil + wind
    except Exception as e:
        logger.warning("Error collecting soil/wind data: %s", e)

    return stored


async def fetch_and_store_historical_rainfall(start: datetime, end: datetime) -> int:
    """Fetch historical hourly precipitation from Open-Meteo and store in InfluxDB.

    Uses the Open-Meteo forecast API with start_date/end_date (supports ~90 days back)
    or the archive API for older dates.

    Stores with source='open-meteo' and city='Moulin-Lidoire' for timeline display.

    Args:
        start: Start datetime
        end: End datetime

    Returns:
        Number of points stored
    """
    from app.services import influx_client

    settings = get_settings()
    lat = settings.moulin_lat
    lon = settings.moulin_lon

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    # Use forecast API for recent data (supports past_days up to 92)
    days_ago = (_utcnow() - start).days
    url = OPEN_METEO_HISTORICAL if days_ago > 90 else OPEN_METEO_CURRENT

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "precipitation",
                    "start_date": start_str,
                    "end_date": end_str,
                    "timezone": "UTC",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            precips = hourly.get("precipitation", [])

            if not times or not precips:
                logger.info(f"No precipitation data from Open-Meteo for {start_str} -> {end_str}")
                return 0

            points = []
            for t, p in zip(times, precips):
                if p is not None and p >= 0:
                    points.append(influx_client.make_rainfall_point(
                        source="open-meteo",
                        precipitation_mm_h=p,
                        timestamp=datetime.fromisoformat(t),
                        station_city="Moulin-Lidoire",
                    ))
            stored = await influx_client.write_points_batch(points) if points else 0

            logger.info(f"Stored {stored} hourly precipitation points for {start_str} -> {end_str}")
            return stored

    except Exception as e:
        logger.warning(f"Error fetching historical rainfall from Open-Meteo: {e}")
        return 0


def daily_et0_by_date(
    daily: dict,
    hourly_times: list[str],
    hourly_et0: list,
) -> dict[str, float]:
    """Daily ET0 (mm/day) keyed by UTC date 'YYYY-MM-DD'.

    Uses Open-Meteo's daily ``et0_fao_evapotranspiration`` sum when present;
    for days missing from it, sums the hourly values (mm/h) of complete days
    (24 non-null hours) only, so partial days never look drier than they are.
    """
    out: dict[str, float] = {}
    for date_str, value in zip(daily.get("time", []) or [], daily.get("et0_fao_evapotranspiration", []) or []):
        if date_str and value is not None:
            out[str(date_str)[:10]] = float(value)

    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for time_str, value in zip(hourly_times, hourly_et0):
        if not time_str or value is None:
            continue
        day = time_str[:10]
        sums[day] = sums.get(day, 0.0) + float(value)
        counts[day] = counts.get(day, 0) + 1
    for day, total in sums.items():
        if day not in out and counts[day] >= 24:
            out[day] = total
    return out


async def fetch_and_store_soil_historical(start: datetime, end: datetime) -> int:
    """Fetch historical soil moisture, soil temperature, and ET0 from Open-Meteo and store in InfluxDB.

    Uses the forecast API (start_date/end_date) for recent data (< 90 days)
    or the archive API for older dates.

    Stores to measurement 'soil_timeline' with tag location=moulin.

    ET0 units: ``et0_mm`` is the DAILY reference evapotranspiration (mm/day,
    same unit as the realtime ``soil_agricultural.et0_mm``) of the UTC day of
    each hourly point; the raw hourly value is kept as ``et0_mm_h`` (mm/h).
    The daily value comes from Open-Meteo's daily sum, or from the sum of the
    24 hourly values of that day when the daily series is missing.

    Args:
        start: Start datetime
        end: End datetime

    Returns:
        Number of points stored
    """
    from app.services import influx_client

    settings = get_settings()
    lat = settings.moulin_lat
    lon = settings.moulin_lon

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    days_ago = (_utcnow() - start).days
    url = OPEN_METEO_HISTORICAL if days_ago > 90 else OPEN_METEO_CURRENT

    hourly_params = ",".join([
        "soil_moisture_0_to_7cm",
        "soil_moisture_7_to_28cm",
        "soil_moisture_28_to_100cm",
        "soil_temperature_0_to_7cm",
        "et0_fao_evapotranspiration",
    ])

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": hourly_params,
                    # Daily ET0 sum (mm/day), see docstring
                    "daily": "et0_fao_evapotranspiration",
                    "start_date": start_str,
                    "end_date": end_str,
                    "timezone": "UTC",
                },
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            sm_0_7 = hourly.get("soil_moisture_0_to_7cm", [])
            sm_7_28 = hourly.get("soil_moisture_7_to_28cm", [])
            sm_28_100 = hourly.get("soil_moisture_28_to_100cm", [])
            st_0_7 = hourly.get("soil_temperature_0_to_7cm", [])
            et0_list = hourly.get("et0_fao_evapotranspiration", [])

            if not times:
                logger.info(f"No soil data from Open-Meteo for {start_str} -> {end_str}")
                return 0

            et0_daily = daily_et0_by_date(data.get("daily") or {}, times, et0_list)

            points = []
            for i, time_str in enumerate(times):
                # Shallow moisture: 0-7cm
                shallow = sm_0_7[i] if i < len(sm_0_7) else None

                # Deep moisture: average of 7-28cm and 28-100cm
                deep_values = []
                if i < len(sm_7_28) and sm_7_28[i] is not None:
                    deep_values.append(sm_7_28[i])
                if i < len(sm_28_100) and sm_28_100[i] is not None:
                    deep_values.append(sm_28_100[i])
                deep = sum(deep_values) / len(deep_values) if deep_values else None

                soil_temp = st_0_7[i] if i < len(st_0_7) else None
                et0_h = et0_list[i] if i < len(et0_list) else None
                et0_day = et0_daily.get(time_str[:10])

                # Build fields (skip None values)
                fields = {}
                if shallow is not None:
                    fields["moisture_shallow"] = round(shallow, 4)
                if deep is not None:
                    fields["moisture_deep"] = round(deep, 4)
                if soil_temp is not None:
                    fields["soil_temp"] = round(soil_temp, 1)
                if et0_day is not None:
                    fields["et0_mm"] = round(et0_day, 2)
                if et0_h is not None:
                    fields["et0_mm_h"] = round(et0_h, 3)

                if not fields:
                    continue

                try:
                    ts = datetime.fromisoformat(time_str)
                except ValueError:
                    continue
                points.append(influx_client.make_point(
                    measurement="soil_timeline",
                    tags={"location": "moulin"},
                    fields=fields,
                    timestamp=ts,
                ))

            stored = await influx_client.write_points_batch(points) if points else 0
            logger.info(f"Stored {stored} soil timeline points for {start_str} -> {end_str}")
            return stored

    except Exception as e:
        logger.warning(f"Error fetching historical soil data from Open-Meteo: {e}")
        return 0


async def fetch_and_store_wind_historical(start: datetime, end: datetime) -> int:
    """Fetch historical wind data from Open-Meteo and store in InfluxDB.

    Uses the forecast API for recent data (< 90 days) or the archive API for older dates.
    Stores to measurement 'wind' with tag location=moulin.
    """
    import math
    from app.services import influx_client

    settings = get_settings()
    lat = settings.moulin_lat
    lon = settings.moulin_lon

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    days_ago = (_utcnow() - start).days
    url = OPEN_METEO_HISTORICAL if days_ago > 90 else OPEN_METEO_CURRENT

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "wind_speed_10m,wind_direction_10m",
                    "start_date": start_str,
                    "end_date": end_str,
                    "timezone": "UTC",
                },
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            speeds = hourly.get("wind_speed_10m", [])
            directions = hourly.get("wind_direction_10m", [])

            if not times:
                logger.info(f"No wind data from Open-Meteo for {start_str} -> {end_str}")
                return 0

            points = []
            for i, time_str in enumerate(times):
                speed = speeds[i] if i < len(speeds) else None
                direction = directions[i] if i < len(directions) else None

                if speed is None:
                    continue

                # Calculate westerly component
                westerly = 0.0
                if direction is not None:
                    dir_rad = math.radians(direction)
                    westerly = max(0, -speed * math.sin(dir_rad))

                fields = {
                    "speed_kmh": round(speed, 1),
                    "direction_deg": round(direction, 0) if direction is not None else 0,
                    "westerly_kmh": round(westerly, 1),
                }

                try:
                    ts = datetime.fromisoformat(time_str)
                except ValueError:
                    continue
                points.append(influx_client.make_point(
                    measurement="wind",
                    tags={"location": "moulin"},
                    fields=fields,
                    timestamp=ts,
                ))

            stored = await influx_client.write_points_batch(points) if points else 0
            logger.info(f"Stored {stored} wind timeline points for {start_str} -> {end_str}")
            return stored

    except Exception as e:
        logger.warning(f"Error fetching historical wind data from Open-Meteo: {e}")
        return 0
