// Horizon ribbon (72 h / 7 days): one Paris-time axis, nights shaded, rows
// for the forecast score, the ensemble rain, the high tides and Pessac.
// Pure helpers; components/HorizonRibbon.tsx draws them.
import { scaleLinear } from 'd3-scale'
import type {
  EnsembleForecast,
  ForecastData,
  ForecastHighTide,
  RiskLevel,
  VigicruesScenario,
} from '../types'
import { formatParis, parisHour, parseApiDate } from './time'

const HOUR_MS = 3600_000

export type RibbonHours = 72 | 168

/** Paris hours shaded as night: 20:00 to 07:00 ("rentrer ce soir ?"). */
export const NIGHT_START_HOUR = 20
export const NIGHT_END_HOUR = 7

export function ribbonWindow(now: Date, hours: RibbonHours): [number, number] {
  const t0 = Math.floor(now.getTime() / HOUR_MS) * HOUR_MS
  return [t0, t0 + hours * HOUR_MS]
}

export function timeScale([t0, t1]: [number, number], [x0, x1]: [number, number]) {
  return scaleLinear().domain([t0, t1]).range([x0, x1]).clamp(true)
}

function isNight(t: number): boolean {
  const h = parisHour(new Date(t))
  return h >= NIGHT_START_HOUR || h < NIGHT_END_HOUR
}

/** Night intervals [start, end] (ms) within [t0, t1], hour by hour (DST safe). */
export function nightIntervals(t0: number, t1: number): [number, number][] {
  const out: [number, number][] = []
  let start: number | null = null
  for (let t = Math.floor(t0 / HOUR_MS) * HOUR_MS; t < t1; t += HOUR_MS) {
    const night = isNight(t)
    if (night && start === null) start = Math.max(t, t0)
    if (!night && start !== null) {
      out.push([start, t])
      start = null
    }
  }
  if (start !== null) out.push([start, t1])
  return out
}

export interface TimeTick {
  t: number
  label: string
  /** Paris midnight (day label) */
  major: boolean
}

/**
 * Day ticks at Paris midnight ("mar. 27", or "27" when `shortDays`), plus
 * 6 h (72 h, wide) or 12 h (72 h, narrow) ticks.
 */
export function timeTicks(t0: number, t1: number, hours: RibbonHours, narrow: boolean, shortDays = false): TimeTick[] {
  const ticks: TimeTick[] = []
  for (let t = Math.ceil(t0 / HOUR_MS) * HOUR_MS; t <= t1; t += HOUR_MS) {
    const h = parisHour(new Date(t))
    if (h === 0) ticks.push({ t, label: formatParis(new Date(t), shortDays ? 'd' : 'EEE d'), major: true })
    else if (hours === 72 && (narrow ? h === 12 : h % 6 === 0)) ticks.push({ t, label: `${h}h`, major: false })
  }
  return ticks
}

export interface ScorePoint {
  t: number
  hoursAhead: number
  score: number
  level: RiskLevel
  pessac: number
}

/** Forecast hours within [t0, t1] (the H+0 point is kept even if a little before t0). */
export function forecastPoints(forecast: ForecastData | null | undefined, t0: number, t1: number): ScorePoint[] {
  const out: ScorePoint[] = []
  for (const f of forecast?.forecasts ?? []) {
    const at = parseApiDate(f.timestamp)
    if (!at) continue
    const t = at.getTime()
    if (t > t1 || t < t0 - HOUR_MS) continue
    out.push({ t, hoursAhead: f.hours_ahead, score: f.score, level: f.level, pessac: f.projected_level_m })
  }
  return out.sort((a, b) => a.t - b.t)
}

export interface LevelRun {
  t0: number
  t1: number
  level: RiskLevel
}

/** Consecutive hours of the same level (the coloured strip of the score row). */
export function levelRuns(points: ScorePoint[]): LevelRun[] {
  const runs: LevelRun[] = []
  points.forEach((p, i) => {
    const end = points[i + 1]?.t ?? p.t + HOUR_MS
    const last = runs[runs.length - 1]
    if (last && last.level === p.level) last.t1 = end
    else runs.push({ t0: p.t, t1: end, level: p.level })
  })
  return runs
}

/** Index of the point nearest to t (-1 when empty). */
export function nearestIndex(points: { t: number }[], t: number): number {
  let best = -1
  let dist = Infinity
  points.forEach((p, i) => {
    const d = Math.abs(p.t - t)
    if (d < dist) {
      dist = d
      best = i
    }
  })
  return best
}

/** Index of the highest score (first one on ties). */
export function peakIndex(points: ScorePoint[]): number {
  let best = -1
  points.forEach((p, i) => {
    if (best < 0 || p.score > points[best].score) best = i
  })
  return best
}

export interface RainBlock {
  t0: number
  t1: number
  p10: number
  p50: number
  p90: number
  /** Total of the wettest member over the window */
  max: number
  members: number
}

export interface RainRow {
  blocks: RainBlock[]
  /** End of the model run (beyond: hatched, no data) */
  horizonEnd: number | null
  members: number
}

/**
 * 24 h ensemble windows of `model` overlapping [t0, t1] (totals in mm per
 * 24 h: the API serves windows, not hours), clipped to the ribbon.
 */
export function rainRow(ensemble: EnsembleForecast | null | undefined, model: string, t0: number, t1: number): RainRow {
  const blocks: RainBlock[] = []
  const windows = ensemble?.rain?.available ? ensemble.rain.horizons?.['24h'] ?? [] : []
  for (const w of windows) {
    const stats = w.models[model]
    const start = parseApiDate(w.start)?.getTime()
    const end = parseApiDate(w.end)?.getTime()
    if (!stats || start == null || end == null || end <= t0 || start >= t1) continue
    blocks.push({
      t0: Math.max(start, t0),
      t1: Math.min(end, t1),
      p10: stats.p10,
      p50: stats.p50,
      p90: stats.p90,
      max: stats.max,
      members: stats.members,
    })
  }
  const info = ensemble?.rain?.models?.find((m) => m.id === model)
  const horizonEnd = parseApiDate(info?.horizon_end ?? null)?.getTime() ?? null
  return { blocks, horizonEnd, members: info?.members ?? blocks[0]?.members ?? 0 }
}

/** Radius of a high-tide circle: 4 px at coefficient 40 or less, 10 px at 120. */
export function tideRadius(coefficient: number | null | undefined): number {
  if (coefficient == null) return 4
  return 4 + Math.min(1, Math.max(0, (coefficient - 40) / 80)) * 6
}

export interface TideMark {
  t: number
  coefficient: number | null
  r: number
}

export function tideMarks(highTides: ForecastHighTide[] | null | undefined, t0: number, t1: number): TideMark[] {
  const out: TideMark[] = []
  for (const tide of highTides ?? []) {
    const t = parseApiDate(tide.timestamp)?.getTime()
    if (t == null || t < t0 || t > t1) continue
    out.push({ t, coefficient: tide.coefficient, r: tideRadius(tide.coefficient) })
  }
  return out
}

export function isVigicruesScenario(value: unknown): value is VigicruesScenario {
  const v = value as Partial<VigicruesScenario> | null
  return !!v && typeof v.timestamp === 'string' && typeof v.mean_m === 'number' && typeof v.min_m === 'number' && typeof v.max_m === 'number'
}

export interface VigicruesMark extends VigicruesScenario {
  t: number
}

/** Official Vigicrues forecast at Libourne within the ribbon. */
export function vigicruesMarks(scenarios: unknown[] | null | undefined, t0: number, t1: number): VigicruesMark[] {
  const out: VigicruesMark[] = []
  for (const s of scenarios ?? []) {
    if (!isVigicruesScenario(s)) continue
    const t = parseApiDate(s.timestamp)?.getTime()
    if (t == null || t < t0 || t > t1) continue
    out.push({ ...s, t })
  }
  return out
}

/** Highest point of a series, with a minimum (keeps a reference line in view). */
export function seriesMax(values: number[], minimum: number): number {
  return Math.max(minimum, ...values.filter((v) => Number.isFinite(v)))
}
