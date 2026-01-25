"""Pydantic schemas for API data models."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from enum import Enum


class TideType(str, Enum):
    """Type of tide."""
    HIGH = "PM"  # Pleine Mer
    LOW = "BM"   # Basse Mer


class RiskLevel(str, Enum):
    """Risk level classification."""
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


class Tendency(str, Enum):
    """Water level tendency."""
    RISING = "hausse"
    STABLE = "stable"
    FALLING = "baisse"


# Tide data
class TideData(BaseModel):
    """Tide data point."""
    timestamp: datetime
    port: str
    tide_type: TideType
    height_m: float
    coefficient: Optional[int] = None


class TideForecast(BaseModel):
    """Tide forecast for upcoming hours."""
    port: str
    next_high_tide: Optional[TideData] = None
    next_low_tide: Optional[TideData] = None
    current_coefficient: Optional[int] = None
    tides: list[TideData] = []


# River level data
class RiverLevel(BaseModel):
    """River level measurement."""
    timestamp: datetime
    station_id: str
    station_name: str
    river: str
    height_m: float
    flow_m3s: Optional[float] = None
    tendency: Optional[Tendency] = None


class RiverLevelForecast(BaseModel):
    """River level with forecast scenarios."""
    current: RiverLevel
    tendency: Tendency
    forecast_scenarios: list[dict] = []


# Weather data
class WeatherForecast(BaseModel):
    """Weather forecast data point."""
    timestamp: datetime
    point_id: str
    point_name: str
    precipitation_mm: float
    temperature_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    storm_probability: Optional[float] = None


class RainfallObservation(BaseModel):
    """Observed rainfall data."""
    timestamp: datetime
    source: str  # radar, station, comephore
    precipitation_mm_h: float
    precipitation_mm_24h: Optional[float] = None
    precipitation_mm_72h: Optional[float] = None
    basin_avg: bool = True  # Is this averaged over the basin?


class RainfallSummary(BaseModel):
    """Rainfall summary for the basin."""
    last_hour_mm: float = 0.0
    last_6h_mm: float = 0.0
    last_24h_mm: float = 0.0
    last_72h_mm: float = 0.0
    forecast_24h_mm: float = 0.0
    forecast_72h_mm: float = 0.0


# Video analysis
class VideoAnalysis(BaseModel):
    """Video analysis result from cascade camera."""
    timestamp: datetime
    water_level_index: float = Field(..., ge=0, le=100, description="Water level index 0-100")
    delta_from_baseline: Optional[float] = None
    anomaly_detected: bool = False
    snapshot_url: Optional[str] = None


# Risk scoring
class RiskFactors(BaseModel):
    """Individual risk factors."""
    rain_24h_score: float = Field(..., ge=0, le=1)
    rain_72h_score: float = Field(..., ge=0, le=1)
    river_level_score: float = Field(..., ge=0, le=1)
    tide_score: float = Field(..., ge=0, le=1)
    tide_phase_score: float = Field(..., ge=0, le=1)
    video_level_score: Optional[float] = Field(None, ge=0, le=1)


class RiskScore(BaseModel):
    """Overall risk score and breakdown."""
    timestamp: datetime
    level: RiskLevel
    score: float = Field(..., ge=0, le=100, description="Risk score 0-100")
    factors: RiskFactors
    description: str
    recommendations: list[str] = []


# Flood episodes
class FloodEpisode(BaseModel):
    """Historical flood episode record."""
    id: Optional[int] = None
    start_time: datetime
    peak_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    severity: int = Field(..., ge=1, le=5, description="Severity 1-5")
    description: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None


class FloodEpisodeCreate(BaseModel):
    """Schema for creating a flood episode."""
    start_time: datetime
    peak_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    severity: int = Field(..., ge=1, le=5)
    description: Optional[str] = None
    notes: Optional[str] = None


class EpisodeContext(BaseModel):
    """Reconstructed context for an episode."""
    rain_24h_before_mm: float
    rain_48h_before_mm: float
    rain_72h_before_mm: float
    max_dordogne_level_m: float
    max_tide_coefficient: int
    tide_phase_at_peak: Optional[str] = None
    hours_since_high_tide: Optional[float] = None


class EpisodeAnalysis(BaseModel):
    """Full analysis of a flood episode."""
    episode: FloodEpisode
    context: EpisodeContext
    tides: list[TideData] = []
    river_levels: list[RiverLevel] = []
    rainfall: list[RainfallObservation] = []
    video_data: list[VideoAnalysis] = []
    matched_criteria: list[str] = []


# Realtime dashboard data
class RealtimeData(BaseModel):
    """Real-time dashboard data."""
    timestamp: datetime
    risk: RiskScore
    tide: TideForecast
    river: RiverLevelForecast
    rainfall: RainfallSummary
    video: Optional[VideoAnalysis] = None
    alerts: list[str] = []


# Timeline data
class TimelinePoint(BaseModel):
    """Single point on the timeline."""
    timestamp: datetime
    tide_height_m: Optional[float] = None
    river_level_m: Optional[float] = None
    rainfall_mm_h: Optional[float] = None
    risk_score: Optional[float] = None
    video_level: Optional[float] = None


class TimelineData(BaseModel):
    """Timeline data for charts."""
    start: datetime
    end: datetime
    resolution: str  # "1h", "15m", "1d"
    points: list[TimelinePoint]
    episodes: list[FloodEpisode] = []
