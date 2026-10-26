// GloFAS 30-day fan (GlofasFan): log domain, bands, lines and summary.
// Pure helpers over /api/forecast/ensemble.glofas.
import type { EpisodeDriversData, GlofasDay, GlofasForecast } from '../types'
import type { Scale } from './scale'

/** A fan day; p25/p75 are drawn when the API serves them (not yet the case). */
export type FanDay = GlofasDay & { p25?: number | null; p75?: number | null }

export type FanKey = 'p10' | 'p25' | 'p50' | 'p75' | 'p90' | 'max'

/** Smallest discharge drawn (m3/s): the log axis needs a positive floor */
export const FAN_FLOOR = 0.005

/** Days with a value for every key, in date order. */
export function fanDays(glofas: GlofasForecast | null | undefined): FanDay[] {
  return [...((glofas?.days ?? []) as FanDay[])]
    .filter((d) => d.date && Number.isFinite(d.p50))
    .sort((a, b) => a.date.localeCompare(b.date))
}

export function hasQuartiles(days: readonly FanDay[]): boolean {
  return days.length > 0 && days.every((d) => d.p25 != null && d.p75 != null)
}

/** Log domain in whole decades covering the fan, the climatology and the floods. */
export function fanDomain(days: readonly FanDay[], extra: readonly (number | null | undefined)[] = []): [number, number] {
  const values = [...days.flatMap((d) => [d.p10, d.p50, d.p90, d.max]), ...extra].filter(
    (v): v is number => v != null && Number.isFinite(v) && v > 0,
  )
  if (values.length === 0) return [0.01, 10]
  const lo = Math.max(FAN_FLOOR, Math.min(...values))
  const hi = Math.max(...values)
  const d0 = 10 ** Math.floor(Math.log10(lo))
  let d1 = 10 ** Math.ceil(Math.log10(hi))
  if (d1 <= d0) d1 = d0 * 10
  return [d0, d1]
}

/** One tick per decade (0.01, 0.1, 1, 10...). */
export function decadeTicks([d0, d1]: [number, number]): number[] {
  const out: number[] = []
  for (let e = Math.round(Math.log10(d0)); e <= Math.round(Math.log10(d1)); e += 1) out.push(Number((10 ** e).toPrecision(1)))
  return out
}

/** Calendar day (GloFAS days have no time zone) at noon UTC, as ms. */
export function dayTime(date: string): number {
  return Date.parse(`${date}T12:00:00Z`)
}

/** Closed band between two quantiles (SVG path data), "" when not served. */
export function bandPath(days: readonly FanDay[], x: Scale, y: Scale, low: FanKey, high: FanKey): string {
  const pts = days.filter((d) => d[low] != null && d[high] != null)
  if (pts.length < 2) return ''
  const top = pts.map((d) => `${x(dayTime(d.date)).toFixed(1)},${y(d[high] as number).toFixed(1)}`)
  const bottom = [...pts].reverse().map((d) => `${x(dayTime(d.date)).toFixed(1)},${y(d[low] as number).toFixed(1)}`)
  return `M${top.join('L')}L${bottom.join('L')}Z`
}

export function linePath(days: readonly FanDay[], x: Scale, y: Scale, key: FanKey): string {
  const pts = days.filter((d) => d[key] != null)
  if (pts.length < 2) return ''
  return `M${pts.map((d) => `${x(dayTime(d.date)).toFixed(1)},${y(d[key] as number).toFixed(1)}`).join('L')}`
}

export interface FloodBand {
  min: number
  max: number
  count: number
  years: string[]
}

/**
 * Range of the GloFAS values simulated at the labelled floods
 * (/api/episodes/drivers values.glofas_m3s). Null while the API serves none:
 * GloFAS is only stored since its collector started, older floods need the
 * reanalysis.
 */
export function floodBand(drivers: Pick<EpisodeDriversData, 'episodes'> | null | undefined): FloodBand | null {
  const floods = (drivers?.episodes ?? []).filter(
    (e) => e.outcome === 'flooded' && e.values?.glofas_m3s != null && e.values.glofas_m3s > 0,
  )
  if (floods.length === 0) return null
  const v = floods.map((e) => e.values.glofas_m3s as number)
  return {
    min: Math.min(...v),
    max: Math.max(...v),
    count: floods.length,
    years: Array.from(new Set(floods.map((e) => e.key.slice(0, 4)))).sort(),
  }
}

export interface FanSummary {
  /** Wettest member over the run and its day */
  wettest: { value: number; date: string } | null
  /** Highest median and its day */
  peakMedian: { value: number; date: string } | null
  /** First day whose p90 reaches the climatology p90, if any */
  firstAboveP90: string | null
}

export function fanSummary(days: readonly FanDay[], clim: GlofasForecast['climatology']): FanSummary {
  if (days.length === 0) return { wettest: null, peakMedian: null, firstAboveP90: null }
  const wet = days.reduce((a, b) => (b.max > a.max ? b : a))
  const med = days.reduce((a, b) => (b.p50 > a.p50 ? b : a))
  const above = clim ? days.find((d) => d.p90 >= clim.p90) : undefined
  return {
    wettest: { value: wet.max, date: wet.date },
    peakMedian: { value: med.p50, date: med.date },
    firstAboveP90: above?.date ?? null,
  }
}
