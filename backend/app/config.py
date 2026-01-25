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

    # Netatmo API credentials (for local weather station data)
    # Get these from dev.netatmo.com/apps
    netatmo_client_id: str = ""
    netatmo_client_secret: str = ""
    # OAuth2 tokens - generate via Token Generator at dev.netatmo.com
    # Use scope: read_station
    netatmo_refresh_token: str = ""
    netatmo_access_token: str = ""  # Optional: direct access token for testing

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

    # Priority Netatmo stations on Lidoire basin (for rainfall monitoring)
    # Ordered from upstream to downstream
    lidoire_netatmo_priority: list[dict] = [
        {"city": "Saint-Rémy", "lat": 44.9479, "lon": 0.1847, "position": "amont"},
        {"city": "Saint-Antoine-de-Breuilh", "lat": 44.8460, "lon": 0.1484, "position": "amont"},
        {"city": "Flaujagues", "lat": 44.8300, "lon": 0.0365, "position": "milieu"},
        {"city": "Les Salles-de-Castillon", "lat": 44.9046, "lon": -0.0057, "position": "aval"},
        {"city": "Castillon-la-Bataille", "lat": 44.8584, "lon": -0.0122, "position": "aval"},
        {"city": "Lamothe-Montravel", "lat": 44.8535, "lon": -0.0013, "position": "aval"},
    ]

    # Vigicrues stations (legacy format)
    vigicrues_station_libourne: str = "P577001001"
    vigicrues_stations: list[dict] = [
        {"id": "P577001001", "name": "Libourne", "river": "Dordogne"},
    ]

    # Hub'Eau hydrometric stations - comprehensive monitoring network
    # Stations ordered by proximity to moulin and importance for flood prediction
    hubeau_stations: list[dict] = [
        # Dordogne - main river (downstream to upstream)
        {
            "id": "P577001001",
            "name": "Libourne",
            "river": "Dordogne",
            "distance_km": 5,
            "delay_hours": 0,
            "weight": 0.35,
            "thresholds": {"yellow": 3.0, "orange": 4.0, "red": 5.0},
        },
        {
            "id": "P555001001",
            "name": "Pessac-sur-Dordogne",
            "river": "Dordogne",
            "distance_km": 30,
            "delay_hours": 4,
            "weight": 0.15,
            "thresholds": {"yellow": 3.5, "orange": 4.5, "red": 5.5},
        },
        {
            "id": "P514001001",
            "name": "Bergerac",
            "river": "Dordogne",
            "distance_km": 60,
            "delay_hours": 8,
            "weight": 0.10,
            "thresholds": {"yellow": 3.0, "orange": 4.0, "red": 5.0},
        },
        # Isle - major tributary (confluence at Libourne)
        {
            "id": "P726151001",
            "name": "Abzac",
            "river": "Isle",
            "distance_km": 15,
            "delay_hours": 2,
            "weight": 0.15,
            "thresholds": {"yellow": 2.5, "orange": 3.5, "red": 4.5},
        },
        {
            "id": "P704151001",
            "name": "Périgueux",
            "river": "Isle",
            "distance_km": 80,
            "delay_hours": 12,
            "weight": 0.05,
            "thresholds": {"yellow": 2.0, "orange": 3.0, "red": 4.0},
        },
        # Dronne - tributary of Isle (confluence at Coutras)
        {
            "id": "P846251001",
            "name": "Coutras",
            "river": "Dronne",
            "distance_km": 25,
            "delay_hours": 3,
            "weight": 0.10,
            "thresholds": {"yellow": 2.0, "orange": 3.0, "red": 4.0},
        },
        {
            "id": "P816251001",
            "name": "Ribérac",
            "river": "Dronne",
            "distance_km": 70,
            "delay_hours": 10,
            "weight": 0.05,
            "thresholds": {"yellow": 1.5, "orange": 2.5, "red": 3.5},
        },
    ]

    # Primary station for main display (Libourne)
    hubeau_primary_station: str = "P577001001"

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
