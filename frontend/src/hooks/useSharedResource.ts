import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react'
import {
  errorMessage,
  getEnsembleForecast,
  getEpisodeDrivers,
  getForecast,
  getIndicators,
  getLidoireProfile,
  getRadarBasinRain,
  getVigilance,
} from '../api'
import type {
  EnsembleForecast,
  EpisodeDriversData,
  ForecastData,
  IndicatorsData,
  LidoireProfileData,
  RadarBasinRain,
  VigilanceStatus,
} from '../types'

// One fetch per endpoint shared by every panel of the Dashboard (mechanism
// cards, ribbon, triggers, map, analogs and confidence read the same
// payloads): concurrent callers join the request in flight, and a payload
// younger than its refresh period is reused. The single data hook of the
// app (it replaces the former useApiResource).

interface Entry {
  data: unknown
  error: string | null
  /** Time of the last successful fetch (ms), 0 = never */
  at: number
  promise: Promise<void> | null
  version: number
  listeners: Set<() => void>
}

const store = new Map<string, Entry>()

function entryOf(key: string): Entry {
  let entry = store.get(key)
  if (!entry) {
    entry = { data: null, error: null, at: 0, promise: null, version: 0, listeners: new Set() }
    store.set(key, entry)
  }
  return entry
}

function notify(entry: Entry) {
  entry.version += 1
  for (const listener of entry.listeners) listener()
}

/**
 * Fetches `key` unless a fetch is in flight (joined) or the last payload is
 * younger than `maxAgeMs` (kept; Infinity = fetched once per page load).
 * Errors keep the previous payload.
 */
export function loadResource<T>(
  key: string,
  fetcher: () => Promise<T>,
  maxAgeMs: number,
  now: number = Date.now(),
): Promise<void> {
  const entry = entryOf(key)
  if (entry.promise) return entry.promise
  if (entry.at > 0 && now - entry.at < maxAgeMs) return Promise.resolve()
  entry.promise = fetcher()
    .then((data) => {
      entry.data = data
      entry.error = null
      entry.at = Date.now()
    })
    .catch((e: unknown) => {
      entry.error = errorMessage(e)
    })
    .finally(() => {
      entry.promise = null
      notify(entry)
    })
  notify(entry)
  return entry.promise
}

/** Current state of a shared resource (tests and non-React callers). */
export function peekResource<T>(key: string): { data: T | null; error: string | null; loading: boolean } {
  const entry = entryOf(key)
  return { data: entry.data as T | null, error: entry.error, loading: entry.promise !== null }
}

/** Drops every cached payload (tests). */
export function resetSharedResources() {
  store.clear()
}

export interface SharedResource<T> {
  data: T | null
  /** Error of the last fetch; `data` keeps the previous payload */
  error: string | null
  loading: boolean
  refresh: () => void
}

const IDLE: SharedResource<never> = { data: null, error: null, loading: false, refresh: () => {} }

/**
 * Shared GET keyed by `key`. `key` null or `enabled` false: nothing is
 * fetched (a disabled panel, a date not chosen yet). A key that encodes its
 * parameters (`risk/analogs@<date>`) never shows the payload of another
 * one. `refreshMs` omitted: fetched once per page load (static payloads).
 * Polling pauses while the tab is hidden.
 */
export function useSharedResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  refreshMs?: number,
  enabled = true,
): SharedResource<T> {
  const active = enabled && key !== null
  const maxAge = refreshMs ?? Infinity
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const subscribe = useCallback(
    (listener: () => void) => {
      if (key === null) return () => {}
      const e = entryOf(key)
      e.listeners.add(listener)
      return () => {
        e.listeners.delete(listener)
      }
    },
    [key],
  )
  const version = useCallback(() => (key === null ? 0 : entryOf(key).version), [key])
  // The version changes on every state change of the entry
  useSyncExternalStore(subscribe, version, version)

  useEffect(() => {
    if (!active || key === null) return
    const run = () => loadResource(key, () => fetcherRef.current(), maxAge)
    void run()
    if (refreshMs == null) return
    const timer = setInterval(() => {
      if (!document.hidden) void run()
    }, refreshMs)
    return () => clearInterval(timer)
  }, [key, active, maxAge, refreshMs])

  const refresh = useCallback(() => {
    if (key !== null) void loadResource(key, () => fetcherRef.current(), 0)
  }, [key])

  if (key === null) return IDLE
  const entry = entryOf(key)
  return {
    data: entry.data as T | null,
    error: entry.error,
    loading: entry.promise !== null || (active && entry.at === 0 && entry.error === null),
    refresh,
  }
}

const MIN = 60_000

// The backend caches these routes (2 min to 6 h): polling faster is useless
export const useIndicators = (enabled = true) =>
  useSharedResource<IndicatorsData>('indicators', getIndicators, 5 * MIN, enabled)
export const useEpisodeDrivers = (enabled = true) =>
  useSharedResource<EpisodeDriversData>('episode-drivers', getEpisodeDrivers, 60 * MIN, enabled)
export const useLidoireProfile = (enabled = true) =>
  useSharedResource<LidoireProfileData>('geo-profile', () => getLidoireProfile(undefined, false), 10 * MIN, enabled)
export const useEnsembleForecast = (enabled = true) =>
  useSharedResource<EnsembleForecast>('forecast-ensemble', getEnsembleForecast, 30 * MIN, enabled)
export const useVigilance = (enabled = true) =>
  useSharedResource<VigilanceStatus>('vigilance', getVigilance, 10 * MIN, enabled)
export const useRiskForecast = (enabled = true) =>
  useSharedResource<ForecastData>('risk-forecast-168', () => getForecast(168), 10 * MIN, enabled)
export const useRadarBasinRain = (enabled = true) =>
  useSharedResource<RadarBasinRain>('radar-basin', getRadarBasinRain, 5 * MIN, enabled)
