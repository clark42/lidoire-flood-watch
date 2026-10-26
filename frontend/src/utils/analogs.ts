// Analog cards ("ça ressemble à…"): per-axis bars, intro text, comparison
// table of the labelled floods. Pure helpers over /api/risk/analogs and
// /api/episodes/drivers.
import type {
  DriverAxis,
  DriverValues,
  EpisodeDriversData,
  EpisodeOutcome,
  RiskAnalog,
  RiskAnalogsData,
} from '../types'

export const AXIS_LABELS: Record<DriverAxis, string> = {
  pessac: 'Pessac',
  eyraud_q: 'Eyraud',
  rain_72h: 'Pluie 72 h',
  piezo: 'Nappe',
  glofas: 'GloFAS',
}

/** Minimum number of shared axes for a distance (config analog_min_axes) */
export const ANALOG_MIN_AXES = 2

export const OUTCOME_LABELS: Record<EpisodeOutcome, string> = {
  flooded: 'Inondé',
  near_miss: 'Presque',
  false_positive: 'Pas inondé',
  unknown: 'À annoter',
}

export interface AxisBar {
  axis: DriverAxis
  label: string
  /** Percentile 0-100 of today, null when unknown */
  today: number | null
  /** Percentile 0-100 at the episode peak */
  episode: number | null
}

/** Today versus the episode, one bar pair per axis of the method. */
export function axisBars(analog: RiskAnalog, data: Pick<RiskAnalogsData, 'axes' | 'situation'>): AxisBar[] {
  return data.axes.map((axis) => ({
    axis,
    label: AXIS_LABELS[axis] ?? axis,
    today: clampPct(data.situation?.percentiles?.[axis]),
    episode: clampPct(analog.percentiles?.[axis]),
  }))
}

function clampPct(v: number | null | undefined): number | null {
  if (v == null || !Number.isFinite(v)) return null
  return Math.min(100, Math.max(0, v))
}

/**
 * Same distance as the backend (episode_drivers.analog_distance): RMS of
 * the percentile differences on the axes both sides have, null under
 * `minAxes` shared axes.
 */
export function analogDistance(
  a: Partial<Record<DriverAxis, number | null>>,
  b: Partial<Record<DriverAxis, number | null>>,
  axes: readonly DriverAxis[],
  minAxes = ANALOG_MIN_AXES,
): number | null {
  const used = axes.filter((ax) => a[ax] != null && b[ax] != null)
  if (used.length < Math.max(1, minAxes)) return null
  const acc = used.reduce((s, ax) => s + (a[ax]! - b[ax]!) ** 2, 0)
  return Math.round(Math.sqrt(acc / used.length) * 10) / 10
}

export interface AnalogsIntro {
  /** Bold first sentence */
  headline: string
  detail: string
  nothingClose: boolean
}

export function analogsIntro(data: RiskAnalogsData): AnalogsIntro {
  const axes = data.axes.map((a) => AXIS_LABELS[a] ?? a).join(', ')
  if (data.nearest_distance == null) {
    return {
      headline: 'Comparaison impossible aujourd’hui.',
      detail: `Trop peu de moteurs disponibles pour calculer une distance (${axes}).`,
      nothingClose: true,
    }
  }
  const d = Math.round(data.nearest_distance)
  if (data.nothing_close) {
    return {
      headline: 'Aucun épisode passé ne ressemble à aujourd’hui.',
      detail:
        `Le plus proche est à ${d} points de percentile (seuil ${data.threshold} ; 0 = identique). ` +
        `Distance sur ${axes}. Les cartes montrent les moins éloignés, pas des situations semblables.`,
      nothingClose: true,
    }
  }
  return {
    headline: `L’épisode labellisé le plus proche est à ${d} points de percentile.`,
    detail: `Distance sur ${axes} (seuil ${data.threshold} ; 0 = identique).`,
    nothingClose: false,
  }
}

export interface FloodRow {
  key: string
  peakTime: string
  values: DriverValues
  waterHeightCm: number | null
  distance: number | null
  /** Readable caveats of the label (infiltration, possible duplicate) */
  notes: string[]
}

/** French note of a label flag worth showing next to a flood, null otherwise. */
export function flagNote(flag: string): string | null {
  if (flag.startsWith('mechanism_infiltration')) return 'infiltration, colmatée depuis'
  const same = /^possible_same_event_as_(\d{4}-\d{2}-\d{2})$/.exec(flag)
  if (same) return `peut-être le même épisode que le ${same[1].split('-').reverse().join('/')}`
  return null
}

/** Labelled floods, newest first, with their distance to today. */
export function floodRows(
  drivers: Pick<EpisodeDriversData, 'episodes'>,
  situation: RiskAnalogsData['situation'] | null,
  axes: readonly DriverAxis[],
): FloodRow[] {
  return drivers.episodes
    .filter((e) => e.outcome === 'flooded')
    .sort((a, b) => b.key.localeCompare(a.key))
    .map((e) => ({
      key: e.key,
      peakTime: e.peak_time,
      values: e.values,
      waterHeightCm: e.water_height_cm,
      distance: situation ? analogDistance(situation.percentiles, e.percentiles, axes) : null,
      notes: (e.flags ?? []).map(flagNote).filter((n): n is string => n !== null),
    }))
}

/** Observation search window around an episode peak (photos of the analog). */
export function observationWindow(peakTime: string, marginHours: number): { start: string; end: string } | null {
  const t = Date.parse(peakTime)
  if (!Number.isFinite(t)) return null
  const m = marginHours * 3600_000
  return { start: new Date(t - m).toISOString(), end: new Date(t + m).toISOString() }
}
