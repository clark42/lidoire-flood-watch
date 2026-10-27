// Replay of the confluence (pure): presets, hourly time index, values of the
// synchronised series at a time, and the factual "story" of a preset.

import type { EpisodeLabel, UnifiedTimelineData } from '../types'
import { formatNumber } from './format'
import { formatParis, parseApiDate } from './time'
import {
  backwaterReach,
  confluenceStage,
  reachRelativeToMill,
  type ConfluenceParams,
  type ProfilePoint,
  type Reach,
  type ReachVsMill,
} from './lidoireProfile'

export const HOUR_MS = 3_600_000
export const DAY_MS = 24 * HOUR_MS

export const PESSAC_ID = 'P555001001'
export const LIBOURNE_ID = 'P577001001'
export const REPLAY_RAIN_SOURCE = 'open-meteo:Moulin-Lidoire'
export const EYRAUD_SERIES = 'flow:EYRAUD'

/** Timeline series keys of the strips */
export const REPLAY_SERIES = {
  pessac: `river:${PESSAC_ID}`,
  libourne: `river:${LIBOURNE_ID}`,
  eyraud: EYRAUD_SERIES,
  score: 'risk:score',
  shadow: 'risk:shadow',
  shadowV4: 'risk:shadow_v4',
  rain: `rainfall:${REPLAY_RAIN_SOURCE}`,
} as const

export type ReplaySeriesKey = keyof typeof REPLAY_SERIES

export type ReplayPresetId = 'now' | 'feb2026' | 'feb2024' | 'nov2023' | 'apr2025' | 'custom'

export interface ReplayPreset {
  id: ReplayPresetId
  label: string
  /** Label key in /api/episodes/labels (peak date) */
  labelKey?: string
  /** Peak used when the label is missing (UTC, hour precise labels of 2026-10-26) */
  fallbackPeak?: string
}

export const REPLAY_PRESETS: readonly ReplayPreset[] = [
  { id: 'now', label: 'Ces 10 jours' },
  { id: 'feb2026', label: 'Crue fév. 2026', labelKey: '2026-02-12', fallbackPeak: '2026-02-12T16:00:00Z' },
  { id: 'feb2024', label: 'Crue fév. 2024', labelKey: '2024-02-26', fallbackPeak: '2024-02-26T11:00:00Z' },
  { id: 'nov2023', label: 'Crue nov. 2023', labelKey: '2023-11-30', fallbackPeak: '2023-11-30T22:15:00Z' },
  { id: 'apr2025', label: 'Crue avr. 2025', labelKey: '2025-04-21', fallbackPeak: '2025-04-21T01:30:00Z' },
]

/** Days shown before / after a flood peak or a chosen date */
export const FLOOD_DAYS_BEFORE = 4
export const FLOOD_DAYS_AFTER = 3
export const CUSTOM_HALF_DAYS = 3.5
export const NOW_DAYS = 10

export interface ReplayWindow {
  start: Date
  end: Date
  /** Initial cursor time */
  focus: Date
  /** Labelled episode of the preset, when any */
  label: EpisodeLabel | null
  /** Peak of the episode (label or fallback) */
  peak: Date | null
}

function floorHour(t: number): number {
  return Math.floor(t / HOUR_MS) * HOUR_MS
}

/** Window and initial cursor of a preset (end clipped to now, whole hours). */
export function replayWindow(
  presetId: ReplayPresetId,
  labels: readonly EpisodeLabel[] | null,
  now: Date,
  customAt?: Date | null,
): ReplayWindow {
  const nowH = floorHour(now.getTime())
  if (presetId === 'custom' && customAt && !Number.isNaN(customAt.getTime())) {
    const at = Math.min(floorHour(customAt.getTime()), nowH)
    const end = Math.min(nowH, at + CUSTOM_HALF_DAYS * DAY_MS)
    const start = Math.min(at, end) - CUSTOM_HALF_DAYS * DAY_MS
    return { start: new Date(start), end: new Date(end), focus: new Date(at), label: null, peak: null }
  }
  const preset = REPLAY_PRESETS.find((p) => p.id === presetId)
  if (!preset || !preset.labelKey) {
    return { start: new Date(nowH - NOW_DAYS * DAY_MS), end: new Date(nowH), focus: new Date(nowH), label: null, peak: null }
  }
  const label = labels?.find((l) => l.key === preset.labelKey) ?? null
  const peak = parseApiDate(label?.peak_time ?? preset.fallbackPeak) ?? new Date(nowH)
  const peakH = floorHour(peak.getTime())
  const end = Math.min(nowH, peakH + FLOOD_DAYS_AFTER * DAY_MS)
  return {
    start: new Date(peakH - FLOOD_DAYS_BEFORE * DAY_MS),
    end: new Date(end),
    focus: new Date(Math.min(peakH, end)),
    label,
    peak,
  }
}

/** Hourly times from start to end inclusive (ms). */
export function hourlyTimes(start: Date, end: Date): number[] {
  const out: number[] = []
  for (let t = floorHour(start.getTime()); t <= end.getTime(); t += HOUR_MS) out.push(t)
  return out
}

/** Index of the time nearest to t in a sorted array (0 when empty). */
export function nearestIndex(times: readonly number[], t: number): number {
  if (times.length === 0) return 0
  let lo = 0
  let hi = times.length - 1
  if (t <= times[lo]) return lo
  if (t >= times[hi]) return hi
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (times[mid] <= t) lo = mid
    else hi = mid
  }
  return t - times[lo] <= times[hi] - t ? lo : hi
}

/** Next cursor index of the player (stops at the end). */
export function nextPlayIndex(index: number, length: number, reducedMotion: boolean): number {
  return Math.min(length - 1, index + (reducedMotion ? 6 : 1))
}

export interface TimePoint {
  t: number
  v: number
}

export function seriesPoints(data: UnifiedTimelineData | null | undefined, key: string): TimePoint[] {
  const out: TimePoint[] = []
  for (const p of data?.series[key]?.data ?? []) {
    const d = parseApiDate(p.timestamp)
    if (d && p.value != null && Number.isFinite(p.value)) out.push({ t: d.getTime(), v: p.value })
  }
  return out.sort((a, b) => a.t - b.t)
}

/** Newest point at or before t, not older than maxAgeMs (causal). */
export function latestAt(points: readonly TimePoint[], t: number, maxAgeMs: number): TimePoint | null {
  let best: TimePoint | null = null
  for (const p of points) {
    if (p.t > t) break
    if (t - p.t <= maxAgeMs) best = p
  }
  return best
}

/** Max of the values with time in (t - hours, t] (causal), null when none. */
export function envelopeMax(points: readonly TimePoint[], t: number, hours: number): number | null {
  const lo = t - hours * HOUR_MS
  let m: number | null = null
  for (const p of points) {
    if (p.t > t) break
    if (p.t > lo) m = m == null ? p.v : Math.max(m, p.v)
  }
  return m
}

export interface ReplaySeries {
  pessac: TimePoint[]
  libourne: TimePoint[]
  eyraud: TimePoint[]
  score: TimePoint[]
  shadow: TimePoint[]
  shadowV4: TimePoint[]
  rain: TimePoint[]
}

export function replaySeries(data: UnifiedTimelineData | null | undefined): ReplaySeries {
  return {
    pessac: seriesPoints(data, REPLAY_SERIES.pessac),
    libourne: seriesPoints(data, REPLAY_SERIES.libourne),
    eyraud: seriesPoints(data, REPLAY_SERIES.eyraud),
    score: seriesPoints(data, REPLAY_SERIES.score),
    shadow: seriesPoints(data, REPLAY_SERIES.shadow),
    shadowV4: seriesPoints(data, REPLAY_SERIES.shadowV4),
    rain: seriesPoints(data, REPLAY_SERIES.rain),
  }
}

export interface ReplayFrame {
  t: number
  /** Latest heights (station datum), at most 3 h old */
  pessac: number | null
  libourne: number | null
  /** Latest Eyraud discharge, at most 26 h old (the stored history is often daily) */
  eyraud: number | null
  score: number | null
  shadow: number | null
  shadowV4: number | null
  rain: number | null
  /** Envelope maxima used for Z_c */
  pessacEnvelope: number | null
  libourneEnvelope: number | null
  /** Z_c (m NGF, estimated) */
  zc: number | null
  reach: Reach & ReachVsMill
}

const LEVEL_MAX_AGE_MS = 3 * HOUR_MS
const FLOW_MAX_AGE_MS = 26 * HOUR_MS
const SCORE_MAX_AGE_MS = 2 * HOUR_MS

/** Every displayed value at time t, from the series alone (no API call). */
export function replayFrame(
  series: ReplaySeries,
  t: number,
  ground: readonly ProfilePoint[],
  millD: number,
  params: ConfluenceParams,
): ReplayFrame {
  const pessacEnvelope = envelopeMax(series.pessac, t, params.envelopeHours)
  const libourneEnvelope = envelopeMax(series.libourne, t, params.envelopeHours)
  const zc = confluenceStage(pessacEnvelope, libourneEnvelope, params)
  const reach = backwaterReach(ground, zc)
  return {
    t,
    pessac: latestAt(series.pessac, t, LEVEL_MAX_AGE_MS)?.v ?? null,
    libourne: latestAt(series.libourne, t, LEVEL_MAX_AGE_MS)?.v ?? null,
    eyraud: latestAt(series.eyraud, t, FLOW_MAX_AGE_MS)?.v ?? null,
    score: latestAt(series.score, t, SCORE_MAX_AGE_MS)?.v ?? null,
    shadow: latestAt(series.shadow, t, SCORE_MAX_AGE_MS)?.v ?? null,
    shadowV4: latestAt(series.shadowV4, t, SCORE_MAX_AGE_MS)?.v ?? null,
    rain: latestAt(series.rain, t, HOUR_MS)?.v ?? null,
    pessacEnvelope,
    libourneEnvelope,
    zc,
    reach: { ...reach, ...reachRelativeToMill(reach, millD) },
  }
}

/** Point of max value in [start, end] (first one on ties), null when none. */
export function maxIn(points: readonly TimePoint[], start: number, end: number): TimePoint | null {
  let best: TimePoint | null = null
  for (const p of points) {
    if (p.t < start || p.t > end) continue
    if (!best || p.v > best.v) best = p
  }
  return best
}

/** Median spacing of the points (h), to tell a daily series from an hourly one. */
export function medianStepHours(points: readonly TimePoint[]): number | null {
  if (points.length < 2) return null
  const steps = points.slice(1).map((p, i) => p.t - points[i].t).sort((a, b) => a - b)
  return steps[Math.floor(steps.length / 2)] / HOUR_MS
}

function km(m: number | null | undefined): string {
  return m == null ? '–' : formatNumber(m / 1000, 1)
}

function hoursBetween(a: number, b: number): string {
  return `${formatNumber(Math.abs(b - a) / HOUR_MS, 0)} h`
}

/**
 * One factual sentence (several clauses) for a replay window, derived from
 * the series and the label only. `atPeak` is the frame at the labelled time.
 */
export function replayStory(
  presetId: ReplayPresetId,
  series: ReplaySeries,
  window: Pick<ReplayWindow, 'start' | 'end' | 'label' | 'peak'>,
  atPeak: ReplayFrame | null,
  last: ReplayFrame | null,
): string {
  const s = window.start.getTime()
  const e = window.end.getTime()
  const pMax = maxIn(series.pessac, s, e)
  const qMax = maxIn(series.eyraud, s, e)
  const rain = series.rain.filter((p) => p.t >= s && p.t <= e).reduce((acc, p) => acc + p.v, 0)
  const daily = (medianStepHours(series.eyraud) ?? 0) >= 12
  const when = (t: number) => formatParis(new Date(t), 'dd/MM à HH:mm')
  const parts: string[] = []

  if (window.label || window.peak) {
    const label = window.label
    const peak = window.peak?.getTime() ?? null
    const precise = label?.peak_time_precision_hours != null && label.peak_time_precision_hours <= 3
    const outcome = label?.outcome ?? 'flooded'
    const verb =
      outcome === 'flooded' ? 'Inondation labellisée' : outcome === 'near_miss' ? 'Presque inondé' : 'Épisode labellisé'
    if (peak != null) {
      const date = precise ? `le ${formatParis(new Date(peak), 'dd/MM/yyyy')} vers ${formatParis(new Date(peak), 'HH:mm')}` : `le ${formatParis(new Date(peak), 'dd/MM/yyyy')} (au jour près)`
      const height = label?.water_height_cm != null ? `, ${formatNumber(label.water_height_cm, 0)} cm d'eau` : ''
      parts.push(`${verb} ${date}${height}.`)
    }
    if (qMax && daily) {
      // A daily value has no hour: no lead or lag is claimed
      parts.push(`L'Eyraud culmine à ${formatNumber(qMax.v, 1)} m³/s le ${formatParis(new Date(qMax.t), 'dd/MM')} (valeur journalière).`)
    } else if (qMax) {
      const rel =
        peak != null && Math.abs(qMax.t - peak) >= 3 * HOUR_MS
          ? ` (${hoursBetween(qMax.t, peak)} ${qMax.t < peak ? 'avant' : 'après'})`
          : peak != null
            ? ' (au même moment)'
            : ''
      parts.push(`L'Eyraud culmine à ${formatNumber(qMax.v, 1)} m³/s le ${when(qMax.t)}${rel}.`)
    }
    if (pMax) {
      const rel =
        peak != null && Math.abs(pMax.t - peak) >= 3 * HOUR_MS
          ? `, ${hoursBetween(pMax.t, peak)} ${pMax.t < peak ? 'avant' : 'après'} l'inondation`
          : ''
      parts.push(`Pessac culmine à ${formatNumber(pMax.v, 2)} m le ${when(pMax.t)}${rel}.`)
    }
    if (atPeak?.reach.distanceBelowMillM != null) {
      parts.push(
        atPeak.reach.reachesMill
          ? `À l'heure labellisée, le plan d'eau de la Dordogne atteindrait le moulin (approximation).`
          : `À l'heure labellisée, le remous s'arrête à ≈ ${km(atPeak.reach.distanceBelowMillM)} km sous le moulin : la Dordogne seule n'explique pas l'eau au moulin (approximation plan d'eau horizontal).`,
      )
    }
    return parts.join(' ')
  }

  const lib = series.libourne.filter((p) => p.t >= s && p.t <= e).map((p) => p.v)
  const intro = presetId === 'now' ? 'Sur les 10 derniers jours' : `Du ${formatParis(window.start, 'dd/MM')} au ${formatParis(window.end, 'dd/MM/yyyy')}`
  const clauses: string[] = []
  if (pMax) clauses.push(`Pessac culmine à ${formatNumber(pMax.v, 2)} m (${when(pMax.t)})`)
  if (lib.length > 1) {
    clauses.push(`Libourne oscille avec la marée entre ${formatNumber(Math.min(...lib), 2)} et ${formatNumber(Math.max(...lib), 2)} m`)
  }
  if (qMax) clauses.push(`l'Eyraud ne dépasse pas ${formatNumber(qMax.v, 1)} m³/s`)
  clauses.push(`il tombe ${formatNumber(rain, 0)} mm (Open-Meteo)`)
  parts.push(`${intro}, ${clauses.join(', ')}.`)
  if (last?.reach.distanceBelowMillM != null) {
    parts.push(`À la fin de la période, le remous s'arrête à ≈ ${km(last.reach.distanceBelowMillM)} km sous le moulin.`)
  }
  return parts.join(' ')
}
