"""Weather data collector.

Fetches weather data from:
- Open-Meteo (primary, free)
- Météo-France API (secondary)
- Infoclimat (for local observations)
"""

import httpx
from datetime import datetime, timedelta
from typing import Optional

from app.config import get_settings
from app.models.schemas import WeatherForecast, RainfallObservation, RainfallSummary

# Open-Meteo API endpoints
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/meteofrance"
OPEN_METEO_HISTORICAL = "https://archive-api.open-meteo.com/v1/archive"

# Cache
_weather_cache: dict = {}
_cache_expiry: Optional[datetime] = None


async def fetch_open_meteo_forecast(lat: float, lon: float) -> list[WeatherForecast]:
    """
    Fetch weather forecast from Open-Meteo.

    Uses the Météo-France model (AROME/ARPEGE) for best accuracy in France.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                OPEN_METEO_FORECAST,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "precipitation,rain,temperature_2m,relative_humidity_2m,wind_speed_10m",
                    "forecast_days": 7,
                    "timezone": "Europe/Paris",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            forecasts = []
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            precip = hourly.get("precipitation", [])
            rain = hourly.get("rain", [])
            temp = hourly.get("temperature_2m", [])
            humidity = hourly.get("relative_humidity_2m", [])
            wind = hourly.get("wind_speed_10m", [])

            for i, time_str in enumerate(times):
                try:
                    forecasts.append(
                        WeatherForecast(
                            timestamp=datetime.fromisoformat(time_str),
                            point_id="moulin",
                            point_name="Moulin de la Lidoire",
                            precipitation_mm=precip[i] if i < len(precip) else 0,
                            temperature_c=temp[i] if i < len(temp) else None,
                            humidity_percent=humidity[i] if i < len(humidity) else None,
                            wind_speed_kmh=wind[i] if i < len(wind) else None,
                        )
                    )
                except (IndexError, ValueError):
                    continue

            return forecasts

    except Exception as e:
        print(f"Error fetching Open-Meteo forecast: {e}")
        return []


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
                    "timezone": "Europe/Paris",
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
                try:
                    observations.append(
                        RainfallObservation(
                            timestamp=datetime.fromisoformat(time_str),
                            source="open-meteo-archive",
                            precipitation_mm_h=precip[i] if i < len(precip) else 0,
                            basin_avg=False,
                        )
                    )
                except (IndexError, ValueError):
                    continue

            return observations

    except Exception as e:
        print(f"Error fetching Open-Meteo historical data: {e}")
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


def _generate_mock_rainfall() -> RainfallSummary:
    """Generate mock rainfall summary for development."""
    return RainfallSummary(
        last_hour_mm=0.5,
        last_6h_mm=3.2,
        last_24h_mm=12.5,
        last_72h_mm=28.0,
        forecast_24h_mm=8.0,
        forecast_72h_mm=25.0,
    )


async def get_rainfall_summary() -> RainfallSummary:
    """Get current rainfall summary for the basin."""
    global _weather_cache, _cache_expiry

    settings = get_settings()
    now = datetime.utcnow()

    # Check cache
    if _cache_expiry and _cache_expiry > now and "summary" in _weather_cache:
        return _weather_cache["summary"]

    # Fetch forecast data
    forecasts = await fetch_open_meteo_forecast(settings.moulin_lat, settings.moulin_lon)

    if not forecasts:
        summary = _generate_mock_rainfall()
    else:
        # Calculate rainfall sums
        # For observed data, we would need historical API or stored data
        # For now, use forecast for future predictions

        now_local = now
        forecast_24h = sum(
            f.precipitation_mm
            for f in forecasts
            if now_local <= f.timestamp <= now_local + timedelta(hours=24)
        )
        forecast_72h = sum(
            f.precipitation_mm
            for f in forecasts
            if now_local <= f.timestamp <= now_local + timedelta(hours=72)
        )

        # For past rainfall, we'd query our stored data
        # Using placeholder values for now
        summary = RainfallSummary(
            last_hour_mm=0.0,  # Would come from stored data
            last_6h_mm=0.0,
            last_24h_mm=0.0,
            last_72h_mm=0.0,
            forecast_24h_mm=forecast_24h,
            forecast_72h_mm=forecast_72h,
        )

    # Update cache (1 hour)
    _weather_cache["summary"] = summary
    _cache_expiry = now + timedelta(hours=1)

    return summary


async def get_forecast_rainfall(hours: int = 72) -> list[WeatherForecast]:
    """Get hourly rainfall forecast."""
    settings = get_settings()
    forecasts = await fetch_open_meteo_forecast(settings.moulin_lat, settings.moulin_lon)

    now = datetime.utcnow()
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


async def collect_and_store_weather():
    """Collect weather data and store in database."""
    settings = get_settings()

    # Fetch forecast
    forecasts = await fetch_open_meteo_forecast(settings.moulin_lat, settings.moulin_lon)

    # Fetch basin average
    basin_data = await fetch_basin_rainfall(settings.lidoire_basin_points)

    total = len(forecasts) + len(basin_data)

    # Store in InfluxDB
    # TODO: Implement storage

    return total
