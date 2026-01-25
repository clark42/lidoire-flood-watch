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
  forecast_scenarios: any[]
}

export interface RainfallSummary {
  last_hour_mm: number
  last_6h_mm: number
  last_24h_mm: number
  last_72h_mm: number
  forecast_24h_mm: number
  forecast_72h_mm: number
}

export interface RiskFactors {
  rain_24h_score: number
  rain_72h_score: number
  river_level_score: number
  tide_score: number
  tide_phase_score: number
  video_level_score: number | null
  tendency_score?: number
  conjunction_multiplier?: number
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
}

export interface InfoclimatData {
  stations_found: number
  nearest_stations: {
    name: string
    distance_km: number
    source: string
    departement?: string
  }[]
  rainfall: {
    available: boolean
    reason?: string
  }
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
  tide_coefficient: number
  rain_forecast_mm: number
}

export interface ForecastData {
  generated_at: string
  hours_ahead: number
  forecasts: ForecastPoint[]
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
}

export interface TimelinePoint {
  timestamp: string
  tide_height_m: number | null
  river_level_m: number | null
  rainfall_mm_h: number | null
  risk_score: number | null
  video_level: number | null
}

export interface FloodEpisode {
  id: number | null
  start_time: string
  peak_time: string | null
  end_time: string | null
  severity: number
  description: string | null
  notes: string | null
  created_at: string | null
}

export interface TimelineData {
  start: string
  end: string
  resolution: string
  points: TimelinePoint[]
  episodes: FloodEpisode[]
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
  rainfall: any[]
  video_data: VideoAnalysis[]
  matched_criteria: string[]
}
