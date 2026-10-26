// Display helpers shared by components
import type { RiskLevel } from '../types'

/** French label of a risk level (the API sends green/yellow/orange/red). */
export const RISK_LEVEL_LABELS: Record<RiskLevel, string> = {
  green: 'Vert',
  yellow: 'Jaune',
  orange: 'Orange',
  red: 'Rouge',
}

export function riskLevelLabel(level: string | null | undefined): string {
  if (!level) return '-'
  return RISK_LEVEL_LABELS[level as RiskLevel] ?? level
}

/**
 * Distance from the moulin, computed by the backend (haversine on lat/lon):
 * one decimal under 10 km, whole kilometres beyond.
 */
export function formatDistanceKm(km: number | null | undefined): string {
  if (km == null || Number.isNaN(km)) return '-'
  return km < 10 ? `${km.toFixed(1)} km` : `${Math.round(km)} km`
}
