// Display helpers of the field observations
import type { ObservationKind } from '../types'

export const OBSERVATION_KIND_LABELS: Record<ObservationKind, string> = {
  flooded: 'Inondé',
  water_rising: "L'eau monte",
  near_miss: 'Presque inondé',
  normal: 'RAS',
}

export const OBSERVATION_KIND_BADGES: Record<ObservationKind, string> = {
  flooded: 'bg-red-100 text-red-800',
  water_rising: 'bg-orange-100 text-orange-800',
  near_miss: 'bg-yellow-100 text-yellow-800',
  normal: 'bg-green-100 text-green-800',
}
