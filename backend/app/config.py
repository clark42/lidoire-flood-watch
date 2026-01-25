"""Application configuration."""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/lidoire_flood"
    influxdb_url: str = "http://localhost:8086"
    influxdb_token: str = ""
    influxdb_org: str = "lidoire"
    influxdb_bucket: str = "flood_data"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # API Keys
    shom_api_key: str = ""
    meteofrance_api_key: str = ""
    infoclimat_api_key: str = ""

    # Camera
    camera_rtsp_url: str = ""
    camera_snapshot_interval: int = 60  # seconds

    # Location - Moulin coordinates
    moulin_lat: float = 44.8759811
    moulin_lon: float = 0.0015091

    # Lidoire basin points for weather data
    lidoire_basin_points: list[dict] = [
        {"id": "amont", "lat": 44.92, "lon": 0.05, "name": "Amont Lidoire"},
        {"id": "milieu", "lat": 44.90, "lon": 0.02, "name": "Milieu Lidoire"},
        {"id": "aval", "lat": 44.876, "lon": 0.001, "name": "Aval Lidoire (Moulin)"},
    ]

    # Vigicrues stations
    vigicrues_station_libourne: str = "O9720010"
    vigicrues_stations: list[dict] = [
        {"id": "O9720010", "name": "Libourne", "river": "Dordogne"},
    ]

    # Tide ports
    tide_port_bordeaux: str = "BORDEAUX"
    tide_port_libourne: str = "LIBOURNE"

    # Risk thresholds
    risk_rain_24h_yellow: float = 30.0  # mm
    risk_rain_24h_orange: float = 50.0
    risk_rain_24h_red: float = 80.0
    risk_dordogne_level_yellow: float = 3.0  # m
    risk_dordogne_level_orange: float = 4.0
    risk_dordogne_level_red: float = 5.0
    risk_tide_coef_high: int = 90

    # Frontend
    frontend_url: str = "http://localhost:3000"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
