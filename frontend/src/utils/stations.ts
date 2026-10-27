// River stations of /api/realtime: the tidal one (Libourne, weight 0) is
// shown as "marée, hors score", in a neutral colour: it swings by metres at
// every tide and its thresholds would turn it orange or red twice a day.
import type { RiskLevel, StationData } from '../types'

export const LIBOURNE_STATION_ID = 'P577001001'

export const TIDE_STATION_LABEL = 'marée, hors score'
export const TIDE_STATION_HINT =
  'Station soumise à la marée : elle monte et descend de plusieurs mètres à chaque marée, exclue du score de risque (poids 0).'

/** True for the tidal station (weight 0 or Libourne): never alert-coloured. */
export function isTideStation(station: Pick<StationData, 'station_id' | 'weight'>): boolean {
  return station.weight === 0 || station.station_id === LIBOURNE_STATION_ID
}

export type StationTone = RiskLevel | 'tide' | 'none'

/** Colour family of a station level: its threshold level, 'tide' (neutral) or 'none' (no measure). */
export function stationTone(
  station: Pick<StationData, 'station_id' | 'weight' | 'threshold_yellow' | 'threshold_orange' | 'threshold_red'> & {
    current_level?: { height_m: number } | null
  },
): StationTone {
  if (isTideStation(station)) return 'tide'
  const h = station.current_level?.height_m
  if (h == null || !Number.isFinite(h) || h === 0) return 'none'
  if (h >= station.threshold_red) return 'red'
  if (h >= station.threshold_orange) return 'orange'
  if (h >= station.threshold_yellow) return 'yellow'
  return 'green'
}

/** Colour family of a rate of change: rising = alert, falling = calm, the tidal station stays neutral. */
export function rateTone(
  station: Pick<StationData, 'station_id' | 'weight' | 'tendency'>,
): 'rising' | 'falling' | 'neutral' {
  if (isTideStation(station)) return 'neutral'
  if (station.tendency === 'hausse') return 'rising'
  if (station.tendency === 'baisse') return 'falling'
  return 'neutral'
}
