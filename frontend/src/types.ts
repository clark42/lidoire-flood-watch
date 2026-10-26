// API Types

export type RiskLevel = 'green' | 'yellow' | 'orange' | 'red'
export type TideType = 'PM' | 'BM'
export type Tendency = 'hausse' | 'stable' | 'baisse'

export interface TideData {
  timestamp: string
  port: string
  tide_type: TideType
  height_m: number
  coefficient: number | null
}

export interface TideForecast {
  port: string
  next_high_tide: TideData | null
  next_low_tide: TideData | null
  current_coefficient: number | null
  tides: TideData[]
}

export interface RiverLevel {
  timestamp: string
  station_id: string
  station_name: string
  river: string
  height_m: number
  flow_m3s: number | null
  tendency: Tendency | null
}

export interface RiverLevelForecast {
  current: RiverLevel
  tendency: Tendency
  forecast_scenarios: unknown[]
}

export interface RainfallSummary {
  last_hour_mm: number
  last_6h_mm: number
  last_24h_mm: number
  last_72h_mm: number
  last_7d_mm: number
  forecast_24h_mm: number
  forecast_72h_mm: number
  // Soil saturation from Open-Meteo (m3/m3)
  soil_moisture: number | null
  soil_saturation_factor: number  // 1.0 (normal) to 1.5 (saturated)
}

// Default values mirror backend RiskWeights (v3.2)
export interface RiskWeights {
  river: number     // 28% (level alone doesn't predict moulin flooding)
  rain_24h: number  // 10%
  rain_72h: number  // 13%
  rain_7d: number   // 5%
  tide: number      // 5%
  tide_phase: number // 5%
  tendency: number   // 13%
  momentum: number   // 8% (NEW: multi-station rising simultaneously)
  wind_west: number  // 3%
  soil_sat: number   // 5%
  video: number      // 5%
}

export interface StationRiskScore {
  station_id: string
  station_name: string
  river: string
  height_m: number
  score: number
  weight: number
  weighted_score: number
  tendency: Tendency
  rate_cm_per_hour: number
}

export interface RiskFactors {
  rain_24h_score: number
  rain_72h_score: number
  rain_7d_score?: number
  river_level_score: number
  tide_score: number
  tide_phase_score: number
  wind_west_score?: number
  soil_saturation_factor?: number
  video_level_score: number | null
  tendency_score?: number
  momentum_score?: number
  conjunction_multiplier?: number
  stations_detail?: StationRiskScore[]
  weights?: RiskWeights
  rain_24h_raw_mm?: number
  rain_72h_raw_mm?: number
  tide_coefficient_raw?: number
  tide_hours_to_high?: number
  tendency_rate_cm_h?: number
}

export interface RiskScore {
  timestamp: string
  level: RiskLevel
  score: number
  factors: RiskFactors
  description: string
  recommendations: string[]
}

export interface VideoAnalysis {
  timestamp: string
  water_level_index: number
  delta_from_baseline: number | null
  anomaly_detected: boolean
  snapshot_url: string | null
}

export interface StationData {
  station_id: string
  station_name: string
  river: string
  current_level: {
    timestamp: string
    height_m: number
  }
  tendency: Tendency
  rate_cm_per_hour: number
  distance_km: number
  delay_hours: number
  weight: number
  threshold_yellow: number
  threshold_orange: number
  threshold_red: number
  lat?: number
  lon?: number
}

export interface MultiStationData {
  timestamp: string
  primary_station_id: string
  stations: StationData[]
}

export interface NetatmoStation {
  city: string
  distance_km: number
  rain_1h_mm: number | null
  rain_24h_mm: number | null
  rain_72h_mm?: number | null
  rain_7d_mm?: number | null
  lat?: number
  lon?: number
  is_lidoire?: boolean
  position?: 'amont' | 'milieu' | 'aval'
}

export interface NetatmoData {
  available: boolean
  timestamp?: string
  stations_count: number
  rain_1h_avg_mm: number
  rain_24h_avg_mm: number
  closest_station?: NetatmoStation
  lidoire_stations?: NetatmoStation[]
  stations?: NetatmoStation[]
  /** OAuth start path, returned when no account is linked */
  auth_url?: string
}

export interface InfoclimatStation {
  name: string
  distance_km: number
  source: string
  departement?: string
  lat?: number
  lon?: number
}

export interface InfoclimatRainfallStation {
  station_id: string
  station_name: string
  distance_km: number
  rain_24h_mm: number
}

export interface InfoclimatRainfall {
  available: boolean
  reason?: string
  stations_count?: number
  rain_24h_avg_mm?: number
  stations?: InfoclimatRainfallStation[]
}

export interface InfoclimatData {
  stations_found: number
  nearest_stations: InfoclimatStation[]
  rainfall: InfoclimatRainfall
}

export interface DataSource {
  name: string
  status: 'ok' | 'degraded' | 'error'
  description: string
  details?: string
  url?: string
}

export interface ForecastPoint {
  timestamp: string
  hours_ahead: number
  score: number
  level: RiskLevel
  rain_1h_mm: number
  rain_24h_mm: number
  rain_72h_mm: number
  tide_coefficient: number
  hours_to_high_tide: number
  projected_level_m: number
}

export interface ForecastData {
  generated_at: string
  hours_ahead: number
  forecasts: ForecastPoint[]
  max_risk: number
  max_level: RiskLevel
  critical_periods: ForecastPoint[]
  summary: string
  data_sources: {
    rain: { status: string; source?: string; hours?: number; error?: string }
    tide: { status: string; port?: string; error?: string }
    river: { status: string; stations?: number; error?: string }
    soil_wind: { status: string; error?: string }
  }
}

export interface SoilConditions {
  soil_moisture: number | null
  moisture_deep: number | null
  soil_temperature_0cm: number | null
  et0_mm: number | null
  soil_saturation_factor: number
  precip_probability: number | null
  water_balance_30d_mm: number | null
  surplus_deficit_30d_pct: number | null
  deficit_12m_mm: number | null
}

export interface WindData {
  wind_speed_kmh: number
  wind_direction_deg: number
  westerly_wind_kmh: number
}

export interface NowcastRainData {
  timestamp: string
  commune: string
  total_60min_mm: number
  max_intensity_mm: number
  rain_minutes: number
  minute_data: { minute: string; mm: number }[]
}

export interface MeteoAgricoleData {
  timestamp: string
  commune: string
  rain_min_mm: number
  rain_max_mm: number
  rain_probability_pct: number
  wind_avg_kmh: number
  wind_gust_kmh: number
  wind_direction: string
  precip_30d_mm: number | null
  surplus_deficit_30d_pct: number | null
  deficit_12m_mm: number | null
  rain_forecast_3d_mm: number
  rain_forecast_7d_mm: number
}

export interface RealtimeData {
  timestamp: string
  risk: RiskScore
  tide: TideForecast
  river: RiverLevelForecast
  rainfall: RainfallSummary
  video: VideoAnalysis | null
  alerts: string[]
  stations?: MultiStationData
  netatmo_available?: boolean
  soil?: SoilConditions
  wind?: WindData
  meteo_agricole?: MeteoAgricoleData
  nowcast_rain?: NowcastRainData
}

// Ground-truth outcome at the mill (null on an episode = not labelled yet)
export type EpisodeOutcome = 'flooded' | 'false_positive' | 'near_miss' | 'unknown'

export interface FloodEpisode {
  id: number | null
  start_time: string
  peak_time: string | null
  end_time: string | null
  severity: number
  description: string | null
  notes: string | null
  created_at: string | null
  outcome?: EpisodeOutcome | null
  observed_at?: string | null // observed peak time at the mill
  water_height_cm?: number | null
  outcome_source?: string | null // who/what labelled the episode
}

export interface EpisodeLabel {
  key: string // peak date YYYY-MM-DD
  peak_time: string
  outcome: EpisodeOutcome
  severity: number
  water_height_cm: number | null
  source: string
  notes: string | null
  flags: string[]
  legacy_db_id: number | null
  legacy_refs: string[]
  legacy_stored_score: number | null
}

export interface EpisodeLabelSet {
  version: number
  updated_at: string
  description: string
  exhaustive: boolean
  period_start: string | null
  period_end: string | null
  caveats: string[]
  inconsistencies: string[]
  episodes: EpisodeLabel[]
}

export interface EpisodeContext {
  rain_24h_before_mm: number
  rain_48h_before_mm: number
  rain_72h_before_mm: number
  max_dordogne_level_m: number
  max_tide_coefficient: number
  tide_phase_at_peak: string | null
  hours_since_high_tide: number | null
}

export interface EpisodeAnalysis {
  episode: FloodEpisode
  context: EpisodeContext
  tides: TideData[]
  river_levels: RiverLevel[]
  rainfall: unknown[]
  video_data: VideoAnalysis[]
  matched_criteria: string[]
}

// Unified Timeline Types (Multi-Source Comparison)
export interface TimelineDataPoint {
  timestamp: string
  value: number
}

export interface TimelineSeries {
  type: 'river_level' | 'rainfall' | 'rainfall_cumul' | 'tide' | 'risk_score' | 'soil_moisture' | 'soil_temperature' | 'evapotranspiration' | 'wind_speed' | 'wind_westerly' | 'forecast_risk_score' | 'forecast_rainfall' | 'forecast_level'
  name: string
  unit: string
  color: string
  data: TimelineDataPoint[]
}

export interface UnifiedTimelineData {
  start: string
  end: string
  resolution: string
  series: Record<string, TimelineSeries>
}

export interface RiverStationInfo {
  id: string
  name: string
  river: string
  color: string
  distance_km: number
  delay_hours: number
}

export interface RainfallStationInfo {
  id: string
  name: string
  position: 'amont' | 'milieu' | 'aval'
  color: string
}

export interface AvailableSources {
  river_stations: RiverStationInfo[]
  rainfall_stations: RainfallStationInfo[]
  has_tide: boolean
  has_risk_score: boolean
  has_infoclimat: boolean
  has_wind: boolean
  has_soil: boolean
}

// Basin Rainfall (aggregated Lidoire basin)
export interface BasinRainfallData {
  timestamp: string
  basin_average_1h_mm: number
  basin_average_24h_mm: number
  weighted_average_24h_mm: number
  by_position: Record<string, { total: number; count: number; avg: number }>
  sparkline_24h: { timestamp: string; value: number }[]
}

// /api/status: real dependency checks (database/influxdb/redis)
export type DependencyState = 'connected' | 'error' | 'timeout'

export interface DependencyCheck {
  status: DependencyState
  latency_ms: number
  error?: string
}

export type DependencyName = 'database' | 'influxdb' | 'redis'

export interface SystemStatus {
  status: 'healthy' | 'degraded'
  timestamp: string
  version: string
  location: { lat: number; lon: number; name: string }
  services: {
    database: DependencyState
    influxdb: DependencyState
    redis: DependencyState
    checks: Partial<Record<DependencyName, DependencyCheck>>
    collectors: Record<string, string>
  }
}

// POST /api/risk/simulate
export interface RiskSimulationResult {
  simulated_values: Record<string, number>
  risk: RiskScore
  model_version: string
}

// /api/sources-detail (exhaustive config, display only)
export interface SourcesDetailRiverStation {
  id: string
  name: string
  river: string
  distance_km: number
  delay_hours: number
  weight: number
  lat?: number | null
  lon?: number | null
  thresholds?: { yellow?: number; orange?: number; red?: number }
}

export interface SourcesDetailNetatmoStation {
  city: string
  lat: number
  lon: number
  position: string
}

export interface SourcesDetailPoint {
  id?: string
  name: string
  lat: number
  lon: number
}

export interface SourcesDetailLevels {
  yellow: number
  orange: number
  red: number
}

export interface SourcesDetail {
  location?: { name: string; lat: number; lon: number }
  primary_station?: string
  sources?: {
    river?: { name: string; description: string; api_url: string; stations?: SourcesDetailRiverStation[] }
    netatmo?: {
      name: string
      description: string
      api_url?: string
      configured: boolean
      search_radius_km: number
      priority_stations?: SourcesDetailNetatmoStation[]
    }
    infoclimat?: {
      name: string
      description: string
      configured: boolean
      search_radius_km: number
      max_stations: number
      note?: string
    }
    open_meteo?: {
      name: string
      description: string
      api_forecast: string
      api_archive: string
      fields?: string[]
      basin_points?: SourcesDetailPoint[]
    }
    tide?: {
      name: string
      description: string
      ports?: SourcesDetailPoint[]
      reference_point?: SourcesDetailPoint
    }
    meteo_agricole?: { name: string; description: string; url?: string; fields?: string[] }
    camera?: { name: string; description: string; configured: boolean }
  }
  risk_model?: {
    version: string
    weights: Record<string, string>
    conjunction_multiplier?: string
    stability_dampener?: string
    thresholds?: {
      rain_24h?: SourcesDetailLevels
      dordogne_level?: SourcesDetailLevels
      tide_coef_high?: number | null
    }
  }
}
