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

    # La Meteo Agricole (scraping)
    meteo_agricole_url: str = "https://www.lameteoagricole.net/previsions-meteo-agricole/Castillon-la-Bataille-33350.html"

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
    #
    # NOTE v2.2 (Jan 2026): Libourne weight=0 — excluded from risk calculation.
    # Libourne is tidally modulated (~4m oscillation) which masks the flood signal.
    # It remains in the list for display/monitoring but does not contribute to the
    # weighted river risk score. Weights redistributed to non-tidal stations.
    # Analysis: episode avril 2025 (id=1).
    hubeau_stations: list[dict] = [
        # Dordogne - main river (downstream to upstream)
        {
            "id": "P577001001",
            "name": "Libourne",
            "river": "Dordogne",
            "distance_km": 5,
            "delay_hours": 0,
            "weight": 0.0,  # Excluded: tidal oscillation ~4m masks flood signal
            "lat": 44.9167,
            "lon": -0.2333,
            "thresholds": {"yellow": 3.0, "orange": 4.0, "red": 5.0},
        },
        {
            # v2.4: Thresholds lowered based on Feb 2024 flood (peaked 4.475m = inondation)
            "id": "P555001001",
            "name": "Pessac-sur-Dordogne",
            "river": "Dordogne",
            "distance_km": 30,
            "delay_hours": 4,
            "weight": 0.30,  # Primary non-tidal Dordogne station
            "lat": 44.8131,
            "lon": 0.0381,
            "thresholds": {"yellow": 3.0, "orange": 3.8, "red": 4.5},
        },
        {
            "id": "P514001001",
            "name": "Bergerac",
            "river": "Dordogne",
            "distance_km": 60,
            "delay_hours": 8,
            "weight": 0.15,  # Upstream Dordogne, good lead time
            "lat": 44.8531,
            "lon": 0.4836,
            "thresholds": {"yellow": 2.5, "orange": 3.5, "red": 4.5},
        },
        # Isle - major tributary (confluence at Libourne)
        {
            "id": "P726151001",
            "name": "Abzac",
            "river": "Isle",
            "distance_km": 15,
            "delay_hours": 2,
            "weight": 0.25,  # Closest non-tidal station (Isle)
            "lat": 44.8789,
            "lon": -0.1278,
            "thresholds": {"yellow": 2.2, "orange": 3.0, "red": 4.0},
        },
        {
            "id": "P704151001",
            "name": "Périgueux",
            "river": "Isle",
            "distance_km": 80,
            "delay_hours": 12,
            "weight": 0.10,  # Far upstream Isle
            "lat": 45.1837,
            "lon": 0.7187,
            "thresholds": {"yellow": 1.8, "orange": 2.8, "red": 3.8},
        },
        # Dronne - tributary of Isle (confluence at Coutras)
        {
            "id": "P846251001",
            "name": "Coutras",
            "river": "Dronne",
            "distance_km": 25,
            "delay_hours": 3,
            "weight": 0.15,  # Dronne at confluence
            "lat": 45.0411,
            "lon": -0.1233,
            "thresholds": {"yellow": 1.8, "orange": 2.5, "red": 3.5},
        },
        {
            "id": "P816251001",
            "name": "Ribérac",
            "river": "Dronne",
            "distance_km": 70,
            "delay_hours": 10,
            "weight": 0.05,  # Far upstream Dronne, early warning
            "lat": 45.2461,
            "lon": 0.3450,
            "thresholds": {"yellow": 1.2, "orange": 2.0, "red": 3.0},
        },
    ]

    # Primary station for tendency/rate calculation (non-tidal)
    # Changed from Libourne (P577001001) to Pessac-sur-Dordogne in v2.2
    hubeau_primary_station: str = "P555001001"

    # Tide ports
    tide_port_bordeaux: str = "BORDEAUX"
    tide_port_libourne: str = "LIBOURNE"

    # Risk thresholds (v2.4: lowered based on Feb 2024 analysis)
    risk_rain_24h_yellow: float = 25.0  # mm (was 30)
    risk_rain_24h_orange: float = 40.0  # (was 50)
    risk_rain_24h_red: float = 60.0     # (was 80)
    risk_dordogne_level_yellow: float = 3.0  # m
    risk_dordogne_level_orange: float = 3.8  # (was 4.0)
    risk_dordogne_level_red: float = 4.5     # (was 5.0)
    risk_tide_coef_high: int = 85  # (was 90)

    # Frontend
    frontend_url: str = "http://localhost:3000"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
