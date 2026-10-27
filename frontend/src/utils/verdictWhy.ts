// "Pourquoi" sentence of the verdict, led by the mechanisms (CLAUDE.md,
// "Controle physique du remous"): the mill floods from the Lidoire's own
// runoff (Eyraud discharge as the analog, observed and forecast rain);
// the Dordogne (Pessac) is a regional-wetness proxy, mentioned only when
// it is high. The v3.4 score drivers stay in "Détail par horizon". Pure.
import type { IndicatorsData, LidoireProfileData, RealtimeData, RiskOutlook } from '../types'
import { formatNumber, formatQuantity } from './format'
import { PESSAC_STATION_ID, pessacEnvelope, TREND_LABELS } from './mechanisms'

export interface MechanismWhyInputs {
  /** undefined = still loading, null = unavailable */
  indicators?: IndicatorsData | null
  realtime?: Pick<RealtimeData, 'rainfall' | 'stations'> | null
  profile?: LidoireProfileData | null
  outlook?: Pick<RiskOutlook, 'inputs_summary'> | null
}

export interface MechanismWhy {
  lead: string
  /** Each reason carries its figure */
  reasons: string[]
}

const q = (v: number) => `${formatQuantity(v)} m³/s`
const qThreshold = (v: number) => `${formatNumber(v, 1)} m³/s`
const mm = (v: number) => `${formatNumber(v, 1)} mm`

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function eyraudReason(indicators: IndicatorsData | null | undefined): string {
  if (indicators === undefined) return "débit de l'Eyraud en cours de chargement"
  const runoff = indicators?.runoff ?? []
  const e = runoff.find((r) => r.key === 'EYRAUD') ?? runoff.find((r) => r.role === 'runoff_analog')
  const now = e?.available ? num(e.q_m3s) : null
  if (!e || now == null) return "débit de l'Eyraud indisponible"
  const parts = [`Eyraud ${q(now)}`]
  if (e.trend) parts.push(TREND_LABELS[e.trend])
  const mean = num(e.q_mean_24h_m3s)
  const context: string[] = []
  if (mean != null) context.push(`moyenne 24 h ${q(mean)}`)
  const exceeded = e.threshold?.exceeded ?? []
  const watch = num(e.thresholds_daily_m3s?.watch)
  const alert = num(e.thresholds_daily_m3s?.alert)
  if (exceeded.includes('alert') && alert != null) context.push(`au-delà du seuil d'alerte ${qThreshold(alert)}`)
  else if (exceeded.includes('watch') && watch != null) context.push(`au-delà du p99 ${qThreshold(watch)}`)
  else if (watch != null) context.push(`à surveiller dès ${qThreshold(watch)}`)
  return `${parts.join(', ')}${context.length ? ` (${context.join(', ')})` : ''}`
}

/**
 * Mechanisms behind the verdict, runoff first: Eyraud discharge and its
 * context (24 h mean, threshold), observed rain, forecast rain; the high
 * Dordogne last and only when Pessac (max 12.5 h) reaches its yellow
 * threshold.
 */
export function mechanismWhy(inputs: MechanismWhyInputs): MechanismWhy {
  const reasons = [eyraudReason(inputs.indicators)]
  const rain = inputs.realtime?.rainfall
  const r24 = num(rain?.last_24h_mm)
  const r72 = num(rain?.last_72h_mm)
  if (r24 != null || r72 != null) {
    reasons.push(
      `pluie tombée ${[r24 != null && `${mm(r24)} sur 24 h`, r72 != null && `${mm(r72)} sur 72 h`].filter(Boolean).join(', ')}`,
    )
  }
  const f24 = num(rain?.forecast_24h_mm)
  const f72 = num(rain?.forecast_72h_mm)
  if (f24 != null || f72 != null) {
    reasons.push(
      `pluie prévue ~ ${[f24 != null && `${mm(f24)} sur 24 h`, f72 != null && `${mm(f72)} sur 72 h`].filter(Boolean).join(', ')}`,
    )
  }
  const h = pessacEnvelope(inputs.profile, inputs.outlook as RiskOutlook | null | undefined)
  const pessac = inputs.realtime?.stations?.stations.find((s) => s.station_id === PESSAC_STATION_ID)
  const yellow = num(pessac?.threshold_yellow)
  if (h != null && yellow != null && h >= yellow) {
    reasons.push(
      `en second, Dordogne haute : Pessac ${formatNumber(h, 2)} m sur 12,5 h (jaune ${formatNumber(yellow, 1)} m), elle freine l'évacuation de la Lidoire`,
    )
  }
  return { lead: 'Mécanismes, ruissellement de la Lidoire d’abord :', reasons }
}
