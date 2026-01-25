// API client for Lidoire Flood Watch backend

import type {
  RealtimeData,
  TimelineData,
  FloodEpisode,
  EpisodeAnalysis,
  RiskScore,
  MultiStationData,
  NetatmoData,
  InfoclimatData,
  DataSource,
  ForecastData,
} from './types'

const API_BASE = '/api'

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })

  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }

  return response.json()
}

// Realtime data
export async function getRealtimeData(): Promise<RealtimeData> {
  return fetchJson<RealtimeData>(`${API_BASE}/realtime`)
}

// Timeline data
export async function getTimelineData(
  start?: string,
  end?: string,
  resolution: string = '1h'
): Promise<TimelineData> {
  const params = new URLSearchParams({ resolution })
  if (start) params.set('start', start)
  if (end) params.set('end', end)

  return fetchJson<TimelineData>(`${API_BASE}/timeline?${params}`)
}

// Multi-station timeline data
export interface StationTimelineData {
  name: string
  river: string
  distance_km: number
  thresholds: {
    yellow?: number
    orange?: number
    red?: number
  }
  data: {
    timestamp: string
    height_m: number
  }[]
}

export interface MultiStationTimelineData {
  start: string
  end: string
  stations: Record<string, StationTimelineData>
}

export async function getMultiStationTimeline(
  start?: string,
  end?: string,
  stationIds?: string[]
): Promise<MultiStationTimelineData> {
  const params = new URLSearchParams()
  if (start) params.set('start', start)
  if (end) params.set('end', end)
  if (stationIds && stationIds.length > 0) {
    params.set('station_ids', stationIds.join(','))
  }

  return fetchJson<MultiStationTimelineData>(`${API_BASE}/timeline/stations?${params}`)
}

// Episodes
export async function getEpisodes(
  limit: number = 50,
  offset: number = 0
): Promise<FloodEpisode[]> {
  return fetchJson<FloodEpisode[]>(
    `${API_BASE}/episodes?limit=${limit}&offset=${offset}`
  )
}

export async function getEpisode(id: number): Promise<FloodEpisode> {
  return fetchJson<FloodEpisode>(`${API_BASE}/episodes/${id}`)
}

export async function createEpisode(
  episode: Omit<FloodEpisode, 'id' | 'created_at'>
): Promise<FloodEpisode> {
  return fetchJson<FloodEpisode>(`${API_BASE}/episodes`, {
    method: 'POST',
    body: JSON.stringify(episode),
  })
}

export async function analyzeEpisode(
  date: string,
  windowBefore: number = 3,
  windowAfter: number = 2
): Promise<EpisodeAnalysis> {
  const params = new URLSearchParams({
    date,
    window_before_days: windowBefore.toString(),
    window_after_days: windowAfter.toString(),
  })

  return fetchJson<EpisodeAnalysis>(`${API_BASE}/episodes/analyze?${params}`, {
    method: 'POST',
  })
}

export async function analyzeExistingEpisode(id: number): Promise<EpisodeAnalysis> {
  return fetchJson<EpisodeAnalysis>(`${API_BASE}/episodes/analyze/${id}`)
}

// Risk
export async function getCurrentRisk(): Promise<RiskScore> {
  return fetchJson<RiskScore>(`${API_BASE}/risk`)
}

export async function getRiskForecast(hours: number = 72): Promise<any> {
  return fetchJson<any>(`${API_BASE}/risk/forecast?hours=${hours}`)
}

export async function simulateRisk(params: {
  rain_24h_mm: number
  rain_72h_mm: number
  dordogne_level_m: number
  tide_coefficient: number
  hours_to_high_tide: number
}): Promise<any> {
  const searchParams = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    searchParams.set(key, value.toString())
  })

  return fetchJson<any>(`${API_BASE}/risk/simulate?${searchParams}`, {
    method: 'POST',
  })
}

// Status
export async function getStatus(): Promise<any> {
  return fetchJson<any>(`${API_BASE}/status`)
}

// Stations
export async function getStations(): Promise<MultiStationData> {
  return fetchJson<MultiStationData>(`${API_BASE}/realtime/stations`)
}

// Netatmo
export async function getNetatmoData(): Promise<NetatmoData> {
  return fetchJson<NetatmoData>(`${API_BASE}/realtime/netatmo`)
}

// Infoclimat
export async function getInfoclimatData(): Promise<InfoclimatData> {
  return fetchJson<InfoclimatData>(`${API_BASE}/realtime/infoclimat`)
}

// Data sources status
export async function getDataSourcesStatus(): Promise<{ sources: DataSource[] }> {
  return fetchJson<{ sources: DataSource[] }>(`${API_BASE}/realtime/sources`)
}

// Forecast
export async function getForecast(hours: number = 48): Promise<ForecastData> {
  return fetchJson<ForecastData>(`${API_BASE}/risk/forecast?hours=${hours}`)
}
