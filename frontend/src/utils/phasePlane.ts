// Flood phase plane (pure): Eyraud discharge (log) against Pessac level,
// labelled episodes, daily trails and the honest reading of the plane.

import { scaleLinear, scaleLog, type ScaleLinear, type ScaleLogarithmic } from 'd3-scale'
import { line } from 'd3-shape'
import type { EpisodeDriversData, EpisodeOutcome, UnifiedTimelineData } from '../types'
import { DAY_MS, EYRAUD_SERIES, PESSAC_ID, envelopeMax, latestAt, HOUR_MS, type TimePoint } from './replay'
import { formatNumber } from './format'
import { formatParis, parseApiDate } from './time'

export const OUTCOME_LABELS: Record<EpisodeOutcome, string> = {
  flooded: 'inondé',
  near_miss: 'presque',
  false_positive: 'pas inondé',
  unknown: 'non labellisé, à annoter',
}

export const PHASE_PESSAC_SERIES = `river:${PESSAC_ID}`
export const PHASE_EYRAUD_SERIES = EYRAUD_SERIES

/** Discharges below the domain start are pinned to it (log scale) */
export const PHASE_Q_MIN = 0.03
/** Right end of the discharge axis: room right of the floods for their labels */
export const PHASE_Q_MAX = { narrow: 150, wide: 300 }
/** Discharge where the episode labels column starts */
export const PHASE_LABEL_Q = 42
export const INFILTRATION_FLAG = 'mechanism_infiltration_since_sealed'

/**
 * Reference lines (display only, from CLAUDE.md / config.py): yellow
 * threshold of Pessac (3.0 m, start of the backwater gate) and the 2-year
 * flood of the Eyraud (QIXnJ 2010-2022, 18.89 m3/s).
 */
export const PESSAC_YELLOW_M = 3.0
export const EYRAUD_Q2_M3S = 18.89

export interface PhaseLayout {
  width: number
  height: number
  narrow: boolean
  margin: { top: number; right: number; bottom: number; left: number }
  x: ScaleLogarithmic<number, number>
  y: ScaleLinear<number, number>
}

export function phaseLayout(width: number, maxPessac = 8): PhaseLayout {
  const narrow = width < 560
  const height = narrow ? Math.round(Math.min(420, Math.max(300, width * 1.05))) : 420
  const margin = narrow ? { top: 16, right: 12, bottom: 44, left: 38 } : { top: 18, right: 20, bottom: 46, left: 52 }
  return {
    width,
    height,
    narrow,
    margin,
    x: scaleLog()
      .domain([PHASE_Q_MIN, narrow ? PHASE_Q_MAX.narrow : PHASE_Q_MAX.wide])
      .range([margin.left, width - margin.right])
      .clamp(true),
    y: scaleLinear()
      .domain([0, Math.max(8.5, Math.ceil(maxPessac + 0.5))])
      .range([height - margin.bottom, margin.top]),
  }
}

/** Grid ticks of the discharge axis inside the domain */
export const PHASE_Q_TICKS = [0.1, 0.3, 1, 3, 10, 30, 100]

export interface TrailPoint {
  /** UTC day "yyyy-MM-dd" */
  day: string
  /** Start of the day (ms) */
  t: number
  pessac: number
  q: number
}

function utcDay(t: number): string {
  return new Date(t).toISOString().slice(0, 10)
}

/**
 * Daily maxima of a timeline served at resolution 1d. Influx dates each
 * window by its stop, so the day is the one just before the timestamp.
 * Days missing either value are dropped.
 */
export function dailyTrail(data: UnifiedTimelineData | null | undefined): TrailPoint[] {
  const byDay = new Map<string, { pessac?: number; q?: number }>()
  const add = (key: string, field: 'pessac' | 'q') => {
    for (const p of data?.series[key]?.data ?? []) {
      const d = parseApiDate(p.timestamp)
      if (!d || p.value == null) continue
      const day = utcDay(d.getTime() - 1)
      const row = byDay.get(day) ?? {}
      row[field] = Math.max(row[field] ?? -Infinity, p.value)
      byDay.set(day, row)
    }
  }
  add(PHASE_PESSAC_SERIES, 'pessac')
  add(PHASE_EYRAUD_SERIES, 'q')
  return [...byDay]
    .filter(([, r]) => r.pessac != null && r.q != null)
    .map(([day, r]) => ({ day, t: Date.parse(`${day}T00:00:00Z`), pessac: r.pessac!, q: r.q! }))
    .sort((a, b) => a.t - b.t)
}

/** UTC day holding a time: the window of the daily fallback request. */
export function dayWindow(iso: string): { start: string; end: string } | null {
  const d = parseApiDate(iso)
  if (!d) return null
  const start = Date.parse(`${utcDay(d.getTime())}T00:00:00Z`)
  return { start: new Date(start).toISOString(), end: new Date(start + DAY_MS).toISOString() }
}

export type QBasis = 'envelope' | 'daily' | 'missing'

export interface EpisodePoint {
  key: string
  peak: string
  outcome: EpisodeOutcome
  pessac: number | null
  q: number | null
  /** envelope = max 12.5 h at the peak (drivers); daily = max of the peak day (fallback) */
  qBasis: QBasis
  infiltration: boolean
  waterHeightCm: number | null
  notes: string | null
}

/** Episodes of /api/episodes/drivers, with the daily fallback when Q is null. */
export function episodePoints(
  drivers: EpisodeDriversData | null | undefined,
  dailyFallback: Record<string, number | null | undefined> = {},
): EpisodePoint[] {
  return (drivers?.episodes ?? []).map((e) => {
    const env = e.values.eyraud_q_m3s
    const fb = dailyFallback[e.key]
    const qBasis: QBasis = env != null ? 'envelope' : fb != null ? 'daily' : 'missing'
    return {
      key: e.key,
      peak: e.peak_time,
      outcome: e.outcome,
      pessac: e.values.pessac_m,
      q: env ?? fb ?? null,
      qBasis,
      infiltration: e.flags.includes(INFILTRATION_FLAG),
      waterHeightCm: e.water_height_cm,
      notes: e.notes,
    }
  })
}

export interface TodayPoint {
  t: number
  pessac: number | null
  q: number | null
  qBasis: 'envelope' | 'latest' | 'missing'
}

/**
 * Today's point with the windows of the episode values: max over the last
 * 12.5 h; the Eyraud falls back to its latest point within 36 h (Hub'Eau
 * publishes it irregularly).
 */
export function todayPoint(pessac: readonly TimePoint[], eyraud: readonly TimePoint[], now: number): TodayPoint {
  const p = envelopeMax(pessac, now, 12.5)
  const qEnv = envelopeMax(eyraud, now, 12.5)
  if (qEnv != null) return { t: now, pessac: p, q: qEnv, qBasis: 'envelope' }
  const latest = latestAt(eyraud, now, 36 * HOUR_MS)
  return { t: now, pessac: p, q: latest?.v ?? null, qBasis: latest ? 'latest' : 'missing' }
}

export interface PhaseReading {
  floodCount: number
  /** Floods except the infiltration of 2023 (sealed since) */
  runoffFloods: EpisodePoint[]
  minFloodQ: EpisodePoint | null
  minFloodPessac: EpisodePoint | null
  /** Highest discharge among the labelled episodes without flooding */
  maxDryQ: EpisodePoint | null
  /** Highest Pessac among the episodes without flooding */
  maxDryPessac: EpisodePoint | null
  /** Floods all strictly above every dry episode on the discharge axis */
  qSeparates: boolean | null
  /** Floods all strictly above every dry episode on the Pessac axis */
  pessacSeparates: boolean | null
}

const DRY: EpisodeOutcome[] = ['near_miss', 'false_positive']

function minBy(points: EpisodePoint[], f: (p: EpisodePoint) => number | null): EpisodePoint | null {
  let best: EpisodePoint | null = null
  for (const p of points) {
    const v = f(p)
    if (v != null && (best == null || v < f(best)!)) best = p
  }
  return best
}

function maxBy(points: EpisodePoint[], f: (p: EpisodePoint) => number | null): EpisodePoint | null {
  let best: EpisodePoint | null = null
  for (const p of points) {
    const v = f(p)
    if (v != null && (best == null || v > f(best)!)) best = p
  }
  return best
}

/** What the labelled points say, computed (never written by hand). */
export function phaseReading(points: readonly EpisodePoint[]): PhaseReading {
  const floods = points.filter((p) => p.outcome === 'flooded')
  const runoffFloods = floods.filter((p) => !p.infiltration)
  const dry = points.filter((p) => DRY.includes(p.outcome))
  const minFloodQ = minBy(runoffFloods, (p) => p.q)
  const minFloodPessac = minBy(runoffFloods, (p) => p.pessac)
  const maxDryQ = maxBy(dry, (p) => p.q)
  const maxDryPessac = maxBy(dry, (p) => p.pessac)
  return {
    floodCount: floods.length,
    runoffFloods,
    minFloodQ,
    minFloodPessac,
    maxDryQ,
    maxDryPessac,
    qSeparates: minFloodQ && maxDryQ ? minFloodQ.q! > maxDryQ.q! : null,
    pessacSeparates: minFloodPessac && maxDryPessac ? minFloodPessac.pessac! > maxDryPessac.pessac! : null,
  }
}

/** SVG path of a trail (pinned to the axes like the points). */
export function trailPath(points: readonly Pick<TrailPoint, 'pessac' | 'q'>[], layout: PhaseLayout): string {
  return (
    line<Pick<TrailPoint, 'pessac' | 'q'>>()
      .x((p) => layout.x(Math.max(p.q, PHASE_Q_MIN)))
      .y((p) => layout.y(p.pessac))(points as Pick<TrailPoint, 'pessac' | 'q'>[]) ?? ''
  )
}

/**
 * Honest caption, computed from the points: few floods, runoff (Eyraud)
 * separates them, Pessac is a regional wetness proxy.
 */
export function floodPhaseCaption(episodes: readonly EpisodePoint[]): string {
  const r = phaseReading(episodes)
  const parts: string[] = [
    `${r.floodCount} crue${r.floodCount > 1 ? 's' : ''} labellisée${r.floodCount > 1 ? 's' : ''} seulement, en échantillon : une lecture, pas une loi.`,
  ]
  if (r.minFloodQ && r.maxDryQ) {
    const margin = r.minFloodQ.q! - r.maxDryQ.q!
    parts.push(
      `Le ruissellement est l’axe qui sépare le mieux : les crues de ruissellement ont toutes un débit de l’Eyraud (analogue mesuré de la Lidoire) ≥ ${formatNumber(r.minFloodQ.q, 1)} m³/s ; ` +
        `le plus fort épisode resté sec atteint ${formatNumber(r.maxDryQ.q, 1)} m³/s (${formatParis(r.maxDryQ.peak, 'dd/MM/yyyy')}, ${OUTCOME_LABELS[r.maxDryQ.outcome]})` +
        (margin > 0
          ? margin < 0.15 * r.minFloodQ.q! ? ' : marge très faible.' : '.'
          : ' : chevauchement, pas de seuil net.'),
    )
  }
  if (r.maxDryPessac && r.minFloodPessac && r.pessacSeparates === false) {
    parts.push(
      `Pessac n’est qu’un indicateur d’humidité régionale : le ${formatParis(r.maxDryPessac.peak, 'dd/MM/yyyy')} il était à ${formatNumber(r.maxDryPessac.pessac, 2)} m, ` +
        `au-dessus de la plus basse des crues (${formatNumber(r.minFloodPessac.pessac, 2)} m), et le moulin est resté sec.`,
    )
  } else {
    parts.push('Pessac est un indicateur d’humidité régionale (remous de la Dordogne), pas une mesure au moulin.')
  }
  const infiltration = episodes.find((e) => e.outcome === 'flooded' && e.infiltration)
  if (infiltration) {
    parts.push(`La crue du ${formatParis(infiltration.peak, 'dd/MM/yyyy')} (infiltration, colmatée depuis) est mise à part.`)
  }
  parts.push(
    'Épisodes : maxima sur 12,5 h à l’heure du pic labellisé (cerclés en tirets : débit journalier, faute de point horaire stocké) ; traces : maxima journaliers.',
  )
  return parts.join(' ')
}
