"""Application configuration."""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

from app.geo import display_distance_km


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application version (APP_VERSION): the image tag in Kubernetes (set by
    # the Helm chart, e.g. sha-1a2b3c4), "dev" when not set
    app_version: str = "dev"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/lidoire_flood"
    influxdb_url: str = "http://localhost:8086"
    influxdb_token: str = ""
    influxdb_org: str = "lidoire"
    influxdb_bucket: str = "flood_data"

    # Optional read-only InfluxDB token for query paths (falls back to
    # influxdb_token when empty). Limits the blast radius of Flux injection.
    influxdb_read_token: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Security
    # API key required in the X-API-Key header for POST/PUT/PATCH/DELETE on /api.
    # Empty = write protection disabled (dev mode, warning logged at startup).
    api_key: str = ""
    # Comma-separated list of allowed CORS origins (no wildcard).
    cors_origins: str = "http://localhost:3100,http://localhost:5173"

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
    # Stations ordered by river and importance for flood prediction.
    # ``distance_km`` (display only) is computed from lat/lon by
    # ``_station_distances`` below: never hard-code it here.
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

    # ------------------------------------------------------------------
    # Hydrological INDICATORS (display, evaluation, future v4). Read by
    # app/services/indicators.py and collectors/hydro_indicators.py only:
    # the risk model (risk_core / risk_v2 / forecast / risk_shadow) never
    # reads these blocks, and they must NOT be merged into hubeau_stations
    # (iterated by the river score, momentum and multi-station branches).
    # ``key`` is the tag value and the timeline id suffix (flow:EYRAUD...).
    # Thresholds are on the DAILY MEAN discharge (Hub'Eau QmnJ), compared
    # with the trailing 24 h mean of the stored instantaneous Q:
    # "watch" = p99 of QmnJ 2022-11 -> 2026-10; Eyraud "alert" = 17.5 m3/s,
    # above which the 6 mill flood dates (and no other day) lie (research
    # report 2026-10-26, in sample). ``max_age_hours``: /api/status
    # freshness (Eyraud/Caudeau publish with ~7 h latency outside floods).
    # ------------------------------------------------------------------
    indicator_flow_stations: list[dict] = [
        {
            "key": "EYRAUD",
            "id": "P540401002",
            "name": "Eyraud a La Force",
            "river": "Eyraud",
            "role": "runoff_analog",  # right-bank tributary, Lidoire-like basin
            "basin_km2": 73.5,
            "lat": 44.8869,
            "lon": 0.3624,
            "collect_h": True,
            "max_age_hours": 12.0,
            "thresholds_daily_m3s": {"watch": 13.1, "alert": 17.5},
        },
        {
            "key": "CAUDEAU",
            "id": "P524401001",
            "name": "Caudeau a Lembras",
            "river": "Caudeau",
            "role": "runoff_analog",
            "basin_km2": 253.0,
            "lat": 44.8854,
            "lon": 0.5488,
            "max_age_hours": 12.0,
            "thresholds_daily_m3s": {"watch": 11.7},
        },
        {
            "key": "PESSAC",
            "id": "P555001001",
            "name": "Dordogne a Pessac",
            "river": "Dordogne",
            "role": "dordogne",
            "basin_km2": 14976.0,
            "lat": 44.8212,
            "lon": 0.0780,
            "max_age_hours": 3.0,
            "thresholds_daily_m3s": {"watch": 1316.0},
        },
        {
            # Bergerac publishes no realtime Q: Lamonzie is its substitute
            "key": "LAMONZIE",
            "id": "P532001001",
            "name": "Dordogne a Lamonzie-Saint-Martin",
            "river": "Dordogne",
            "role": "dordogne_upstream",
            "basin_km2": 14288.0,
            "lat": 44.8547,
            "lon": 0.3573,
            "max_age_hours": 12.0,
            "thresholds_daily_m3s": {"watch": 1249.0},
        },
        {
            # Below the last EDF dam: the releases actually restored
            "key": "ARGENTAT",
            "id": "P135001001",
            "name": "Dordogne a Argentat",
            "river": "Dordogne",
            "role": "dam_release",
            "basin_km2": 4420.0,
            "lat": 45.0924,
            "lon": 1.9424,
            "max_age_hours": 3.0,
            "thresholds_daily_m3s": {"watch": 455.0},
        },
    ]
    # Alluvial piezometer (BDLISA 942AA01, 6 m): p99-p100 at every flood,
    # record 7.47 m NGF on 2026-02-12. Hub'Eau niveaux_nappes (BRGM ADES).
    indicator_groundwater_stations: list[dict] = [
        {
            "key": "MOULIETS",
            "code_bss": "08048X0088/P",
            "name": "Mouliets-et-Villemartin (nappe alluviale)",
            "aquifer": "Alluvions Dordogne",
            "depth_m": 6.0,
            "lat": 44.8344,
            "lon": -0.0446,
            "max_age_hours": 36.0,  # realtime published with ~12 h latency
            "thresholds_m_ngf": {"watch": 7.0},
        },
    ]
    # Observed estuary water level (tide + surge + river flow), display and
    # evaluation only: v3.4 keeps the astronomical tide (tide_port_confluence).
    # SHOM REFMAR (keyless, 1 min, resampled to 10 min, zero hydrographique)
    # and Hub'Eau (Vigicrues, 5 min, station datum).
    indicator_tide_gauges: list[dict] = [
        {
            "key": "BORDEAUX",
            "source": "shom",
            "shom_id": 196,
            "name": "Bordeaux (SHOM REFMAR)",
            "datum": "zero hydrographique",
            "lat": 44.86,
            "lon": -0.5528,
            "max_age_hours": 3.0,
        },
        {
            "key": "BEC_AMBES",
            "source": "hubeau",
            "id": "P916000101",
            "name": "Bec d'Ambes (Hub'Eau)",
            "datum": "zero station Hub'Eau",
            "lat": 45.0405,
            "lon": -0.6036,
            "max_age_hours": 3.0,
        },
    ]

    # Tide ports. Collectors store the tide ESTIMATED at the Lidoire
    # confluence (source time + tide_phase_lag_hours) under
    # tide_port_confluence. BORDEAUX (Open-Meteo/SHOM times, no lag) and
    # LIBOURNE are legacy tags, still valid for queries but no longer read.
    tide_port_confluence: str = "LIDOIRE"
    tide_port_bordeaux: str = "BORDEAUX"
    tide_port_libourne: str = "LIBOURNE"
    # Delay (h) of high water from each tide source to the Lidoire
    # confluence, added to every PM/BM time and hourly height at collection.
    # open-meteo-marine (model sea level off the Gironde mouth): measured
    # high water lag (Open-Meteo archive vs Hydroportail, 2023-06 -> 2025-12)
    # median 4.0 h at Libourne (p10-p90 3.25-4.5 h) and 7.0 h at Pessac
    # (6.25-7.75 h). The confluence, ~30 km above Libourne and ~7 km below
    # Pessac, is between the two: 6 h.
    # shom (Bordeaux port, ~as far above the Bec d'Ambes as Libourne): not
    # measured, 6 h - 4 h = 2 h as an order of magnitude.
    tide_phase_lag_hours: dict[str, float] = {
        "open-meteo-marine": 6.0,
        "shom": 2.0,
    }
    # Open-Meteo Marine forecast length requested by the tide collector. The
    # sea level is served ~9.5 days ahead (null beyond): 7 days left the risk
    # forecast without tides after ~5.5 days. Beyond the last stored high
    # tide the forecast marks the tide unavailable (forecast.tide_horizon).
    tide_forecast_days: int = 10

    # Risk thresholds (v2.4: lowered based on Feb 2024 analysis)
    risk_rain_24h_yellow: float = 25.0  # mm (was 30)
    risk_rain_24h_orange: float = 40.0  # (was 50)
    risk_rain_24h_red: float = 60.0     # (was 80)
    risk_dordogne_level_yellow: float = 3.0  # m
    risk_dordogne_level_orange: float = 3.8  # (was 4.0)
    risk_dordogne_level_red: float = 4.5     # (was 5.0)
    risk_tide_coef_high: int = 85  # (was 90)

    # Risk model v3.4 parameters (single source for app/services/risk_core.py).
    # Weights are the v3.2 calibration (14 annotated episodes) - unchanged.
    risk_weights: dict[str, float] = {
        "river": 0.28,
        "rain_24h": 0.10,
        "rain_72h": 0.13,
        "rain_7d": 0.05,
        "tide": 0.05,
        "tide_phase": 0.05,
        "tendency": 0.13,
        "momentum": 0.08,
        "wind_west": 0.03,
        "soil_sat": 0.05,
        "video": 0.05,
    }
    risk_rain_7d_yellow: float = 60.0   # mm
    risk_rain_7d_orange: float = 100.0
    risk_rain_7d_red: float = 150.0
    risk_tide_coef_low: int = 70        # Coefficient mapped to the "yellow" step
    risk_tide_coef_max: int = 110       # Coefficient mapped to the "red" step
    risk_tide_default_coef: int = 70    # Used when tide data is valid but has no coefficient
    # Score (0-100) -> level boundaries
    risk_level_yellow: float = 15.0
    risk_level_orange: float = 35.0
    risk_level_red: float = 55.0
    # Max age of the last river level point (<= t) used to score a station
    risk_level_max_age_hours: float = 3.0
    # Window for the rate of change (cm/h) regression, points <= t only
    risk_rate_window_hours: float = 3.0

    # Frontend
    frontend_url: str = "http://localhost:3000"

    # Notifications (app/services/notifier.py). ntfy: POST <ntfy_url>/<ntfy_topic>.
    # Empty URL or topic = notifications disabled (one info log at startup).
    # On a public server the topic acts as a password: anyone knowing it can subscribe.
    ntfy_url: str = ""
    ntfy_topic: str = ""
    ntfy_token: str = ""  # Optional bearer token (access-controlled topic)
    # Observed level: rising to orange/red is sent at once; a lower level is
    # sent only once it has held notify_downgrade_hold_hours (the score
    # oscillates around the thresholds with the tide).
    notify_yellow: bool = False  # Also notify green <-> yellow transitions
    notify_downgrade_hold_hours: float = 3.0
    # Forecast heads-up: max score over the next N hours reaches orange/red
    notify_forecast_hours: int = 48
    # Data staleness: primary station last point / last stored realtime score
    notify_station_stale_hours: float = 3.0
    notify_score_stale_hours: float = 2.0

    # Official vigilance (app/services/collectors/vigilance.py), informative
    # only: never a model input. Vigicrues troncons covering the confluence
    # (DO5 holds Pessac, DO22 holds Libourne), SPC territory 32.
    vigilance_vigicrues_troncons: list[dict] = [
        {"code": "DO5", "name": "Dordogne aval", "territory": "32", "station_id": "P555001001"},
        {"code": "DO22", "name": "Confluence Garonne - Dordogne", "territory": "32", "station_id": "P577001001"},
    ]
    # Meteo-France departements (code -> page slug on vigilance.meteofrance.fr)
    vigilance_meteofrance_departments: list[dict] = [
        {"code": "33", "name": "Gironde", "slug": "gironde"},
        {"code": "24", "name": "Dordogne", "slug": "dordogne"},
    ]
    # Phenomenon ids of the Meteo-France vigilance map kept by the collector
    vigilance_meteofrance_phenomena: list[str] = ["2", "4", "3", "1"]

    # Field observations (app/api/observations.py): photos on the backend
    # data volume (PVC /app/data), never served by nginx.
    observations_photo_dir: str = "/app/data/observations"
    observation_photo_max_bytes: int = 8 * 1024 * 1024
    # An observation belongs to an episode when observed_at is inside
    # [start - margin, (end or peak or start) + margin]
    observation_episode_margin_hours: float = 12.0

    # Probabilistic forecasts and observed basin rain: new series and endpoints
    # only, NEVER read by the risk model (v3.4 scoring is unchanged).
    # OPERA radar (EUMETNET, CC BY 4.0): ACRR 1 h accumulation every 15 min,
    # 2 km, keyless COG on the MeteoGate object store (rolling 24 h).
    radar_enabled: bool = True
    radar_opera_base_url: str = "https://s3.waw3-1.cloudferro.com/openradar-24h"
    radar_backfill_hours: float = 6.0     # missing images re-fetched up to this far back
    radar_max_images_per_run: int = 12
    # GloFAS v4 (Open-Meteo Flood API), Lidoire cell of the 0.05 deg river grid
    glofas_enabled: bool = True
    glofas_lat: float = 44.875
    glofas_lon: float = -0.025
    glofas_forecast_days: int = 30
    # Open-Meteo Ensemble API, basin rain (members averaged over the points,
    # weights = sub-basin areas of the BD TOPAGE polygon)
    ensemble_rain_enabled: bool = True
    ensemble_rain_models: list[dict] = [
        {"id": "icon_d2", "label": "ICON-D2-EPS", "meta": "dwd_icon_d2_eps", "forecast_days": 3},
        {"id": "icon_eu", "label": "ICON-EU-EPS", "meta": "dwd_icon_eu_eps", "forecast_days": 6},
        {"id": "ecmwf_ifs025", "label": "ECMWF IFS ENS", "meta": "ecmwf_ifs025_ensemble", "forecast_days": 15},
    ]
    ensemble_rain_points: list[dict] = [
        {"id": "amont", "lat": 44.942, "lon": 0.261, "weight": 62.8},   # source -> Roudier
        {"id": "milieu", "lat": 44.931, "lon": 0.162, "weight": 70.5},  # Roudier -> affluent
        {"id": "aval", "lat": 44.907, "lon": 0.033, "weight": 97.4},    # Lechout + aval
    ]

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.ntfy_url.strip() and self.ntfy_topic.strip())

    # extra="ignore": the shared .env also holds compose-only variables
    # (POSTGRES_PASSWORD, REDIS_PASSWORD...) that are not settings.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _station_distances(self) -> "Settings":
        """Fill each station's display ``distance_km`` from its lat/lon.

        Single source for every distance shown by the API (dashboard cards,
        /timeline/sources, /sources-detail): the haversine distance from the
        moulin, rounded to 0.1 km. Never used by the risk model.
        """
        for station in self.hubeau_stations:
            distance = display_distance_km(
                self.moulin_lat, self.moulin_lon, station.get("lat"), station.get("lon")
            )
            station["distance_km"] = distance if distance is not None else 0.0
        return self

    @model_validator(mode="after")
    def _indicator_distances(self) -> "Settings":
        """Display ``distance_km`` of the indicator stations (never a model input)."""
        for block in (
            self.indicator_flow_stations,
            self.indicator_groundwater_stations,
            self.indicator_tide_gauges,
        ):
            for station in block:
                distance = display_distance_km(
                    self.moulin_lat, self.moulin_lon, station.get("lat"), station.get("lon")
                )
                station["distance_km"] = distance if distance is not None else 0.0
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        """Allowed CORS origins: cors_origins + frontend_url, deduplicated."""
        origins = [o.strip().rstrip("/") for o in self.cors_origins.split(",")]
        if self.frontend_url:
            origins.append(self.frontend_url.strip().rstrip("/"))
        result: list[str] = []
        for origin in origins:
            if origin and origin != "*" and origin not in result:
                result.append(origin)
        return result


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
