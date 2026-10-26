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

/** Placeholder of a missing value (en dash, as in the prototype). */
export const MISSING_VALUE = '–'

const numberFormats = new Map<number, Intl.NumberFormat>()

/**
 * French number with a fixed number of decimals: 1,05 / 1 234 / "–" when
 * missing. Narrow no-break spaces of fr-FR are kept (no line break inside).
 */
export function formatNumber(value: number | null | undefined, decimals = 1): string {
  if (value == null || !Number.isFinite(value)) return MISSING_VALUE
  let fmt = numberFormats.get(decimals)
  if (!fmt) {
    fmt = new Intl.NumberFormat('fr-FR', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
    numberFormats.set(decimals, fmt)
  }
  // -0 would print "-0"
  return fmt.format(Object.is(Math.round(value * 10 ** decimals), -0) ? 0 : value)
}

/**
 * Physical quantity with a precision that follows its magnitude
 * (flows, rain): 0,083 / 0,48 / 4,8 / 48.
 */
export function formatQuantity(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return MISSING_VALUE
  const abs = Math.abs(value)
  if (abs < 0.1) return formatNumber(value, 3)
  if (abs < 1) return formatNumber(value, 2)
  if (abs < 10) return formatNumber(value, 1)
  return formatNumber(value, 0)
}
