// Percentile rail: one physical axis, the value now, the values of the past
// labelled episodes, thresholds and an "unknown threshold" band. Pure layout
// (positions in px), drawn by components/ui/PercentileRail.tsx.
import { scaleLinear, scaleLog } from 'd3-scale'
import type { EpisodeOutcome } from '../types'
import type { ValueKind } from '../components/ui/valueKinds'

export interface RailEpisode {
  key: string
  value: number
  outcome: EpisodeOutcome
  /** Tooltip / accessible text, e.g. "26/02/2024 · inondé · 26,6 m³/s" */
  title: string
}

export interface RailTick {
  value: number
  label: string
  /** threshold (solid), estimated (dashed), reference (thin, historical fact) */
  kind?: 'threshold' | 'estimated' | 'reference'
  /** Tuned on the 5 labelled floods (in-sample) */
  provisional?: boolean
}

/** Unknown threshold: shown as a band, never as a line */
export interface RailBand {
  from: number
  to: number
  label: string
}

/** Second marker, e.g. the forecast rain (dotted) */
export interface RailMarker {
  value: number
  label: string
}

export interface RailSpec {
  domain: [number, number]
  log?: boolean
  value: number | null
  valueLabel: string
  valueKind: ValueKind
  episodes: RailEpisode[]
  ticks: RailTick[]
  band?: RailBand | null
  marker?: RailMarker | null
  ariaLabel: string
}

export const RAIL_MARGIN = { left: 10, right: 10 }
/** Minimum horizontal gap between two episode dots of the same row (px) */
export const EPISODE_GAP_PX = 10
/** Minimum gap between two tick labels (px); closer labels are hidden */
export const TICK_LABEL_GAP_PX = 34
const MAX_ROWS = 4
const ROW_STEP = 9

export interface RailLayout {
  width: number
  height: number
  trackY: number
  x: (v: number) => number
  /** null when the value is missing */
  valueX: number | null
  /** The value lies outside the domain (drawn at the edge with an arrow) */
  valueOutside: 'below' | 'above' | null
  episodes: (RailEpisode & { x: number; y: number })[]
  /** labelRow: 0 = under the track, 1 = one line lower (to avoid a collision) */
  ticks: (RailTick & { x: number; showLabel: boolean; labelRow: 0 | 1 })[]
  /** Baseline of the band label and of the marker label */
  bandLabelY: number
  markerLabelY: number
  band: (RailBand & { x0: number; x1: number }) | null
  marker: (RailMarker & { x: number }) | null
}

/**
 * Domain covering the given values with some room: linear from 0 (or the
 * minimum) with 8 % padding, log10 rounded to whole decades.
 */
export function railDomain(
  values: (number | null | undefined)[],
  { log = false, floor, minSpan = 0 }: { log?: boolean; floor?: number; minSpan?: number } = {},
): [number, number] {
  const finite = values.filter((v): v is number => v != null && Number.isFinite(v) && (!log || v > 0))
  if (finite.length === 0) return log ? [0.01, 100] : [floor ?? 0, Math.max(1, minSpan)]
  const lo = Math.min(...finite)
  const hi = Math.max(...finite)
  if (log) {
    const d0 = 10 ** Math.floor(Math.log10(lo))
    let d1 = 10 ** Math.ceil(Math.log10(hi * 1.15))
    if (d1 <= d0) d1 = d0 * 10
    return [d0, d1]
  }
  const start = floor ?? lo
  const span = Math.max(hi - start, minSpan, 1e-9)
  return [start, start + span * 1.08]
}

export function railScale(domain: [number, number], log: boolean | undefined, width: number) {
  const range: [number, number] = [RAIL_MARGIN.left, Math.max(RAIL_MARGIN.left + 1, width - RAIL_MARGIN.right)]
  return log
    ? scaleLog().domain(domain).range(range).clamp(true)
    : scaleLinear().domain(domain).range(range).clamp(true)
}

/** Positions of every mark of a rail drawn `width` px wide. */
export function layoutRail(spec: RailSpec, width: number): RailLayout {
  const scale = railScale(spec.domain, spec.log, width)
  const x = (v: number) => scale(spec.log ? Math.max(v, spec.domain[0]) : v)

  // Episode dots stacked in rows above the track so that none overlap
  const lastX: number[] = []
  const placed = [...spec.episodes]
    .filter((e) => Number.isFinite(e.value))
    .sort((a, b) => a.value - b.value)
    .map((e) => {
      const ex = x(e.value)
      let row = 0
      while (row < MAX_ROWS - 1 && lastX[row] !== undefined && ex - lastX[row] < EPISODE_GAP_PX) row += 1
      lastX[row] = ex
      return { ...e, x: ex, row }
    })
  const rows = placed.reduce((m, e) => Math.max(m, e.row + 1), 1)
  const top = 26 // value label line
  const trackY = top + 10 + rows * ROW_STEP + 4

  // Tick labels: first line, else a second line, else hidden
  const lastLabel = [-Infinity, -Infinity]
  const ticks = [...spec.ticks]
    .filter((t) => Number.isFinite(t.value))
    .sort((a, b) => a.value - b.value)
    .map((t) => {
      const tx = x(t.value)
      const labelRow: 0 | 1 = tx - lastLabel[0] >= TICK_LABEL_GAP_PX ? 0 : 1
      const showLabel = tx - lastLabel[labelRow] >= TICK_LABEL_GAP_PX
      if (showLabel) lastLabel[labelRow] = tx
      return { ...t, x: tx, showLabel, labelRow }
    })
  const labelLines = ticks.some((t) => t.showLabel && t.labelRow === 1) ? 2 : 1
  const bandLabelY = trackY + 21 + labelLines * 13
  const markerLabelY = bandLabelY + (spec.band ? 13 : 0)

  const [d0, d1] = spec.domain
  const value = spec.value
  const valueOutside = value == null ? null : value < d0 ? 'below' : value > d1 ? 'above' : null

  return {
    width,
    // tick labels under the track, then the band and marker labels
    height: (spec.marker ? markerLabelY : spec.band ? bandLabelY : trackY + 21 + (labelLines - 1) * 13) + 8,
    bandLabelY,
    markerLabelY,
    trackY,
    x,
    valueX: value == null ? null : x(value),
    valueOutside,
    episodes: placed.map(({ row, ...e }) => ({ ...e, y: trackY - 12 - row * ROW_STEP })),
    ticks,
    band: spec.band ? { ...spec.band, x0: x(spec.band.from), x1: x(spec.band.to) } : null,
    marker: spec.marker && Number.isFinite(spec.marker.value) ? { ...spec.marker, x: x(spec.marker.value) } : null,
  }
}

export interface FloodComparison {
  /** Labelled floods with a value on this axis */
  floods: number
  /** Floods whose value is at or below the value now */
  reached: number
  lowestFlood: number | null
  highestFlood: number | null
}

export function compareToFloods(value: number | null, episodes: RailEpisode[]): FloodComparison {
  const floods = episodes.filter((e) => e.outcome === 'flooded').map((e) => e.value)
  return {
    floods: floods.length,
    reached: value == null ? 0 : floods.filter((v) => v <= value).length,
    lowestFlood: floods.length ? Math.min(...floods) : null,
    highestFlood: floods.length ? Math.max(...floods) : null,
  }
}

/** "Sous les 5 crues labellisées (la plus faible : 26,6 m³/s)." */
export function comparisonSentence(
  value: number | null,
  cmp: FloodComparison,
  format: (v: number) => string,
): string {
  if (value == null) return 'Valeur du moment indisponible.'
  if (cmp.floods === 0 || cmp.lowestFlood == null) return 'Aucune crue labellisée sur cet axe.'
  const plural = cmp.floods > 1 ? 's' : ''
  if (cmp.reached === 0) {
    return `Sous ${cmp.floods > 1 ? `les ${cmp.floods}` : 'la'} crue${plural} labellisée${plural} (la plus faible : ${format(cmp.lowestFlood)}).`
  }
  if (cmp.reached === cmp.floods) return `Au niveau ou au-dessus des ${cmp.floods} crues labellisées.`
  return `Au niveau de ${cmp.reached} crue${cmp.reached > 1 ? 's' : ''} labellisée${cmp.reached > 1 ? 's' : ''} sur ${cmp.floods}.`
}
