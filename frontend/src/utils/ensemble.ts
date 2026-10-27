// Ensemble rain (GET /api/forecast/ensemble): windows, models, agreement
import type { EnsembleForecast, EnsembleHorizon, EnsembleWindowStats } from '../types'
import { formatNumber } from './format'
import { formatAge, parseApiDate } from './time'

/** Longest first: ECMWF covers 15 days, ICON-EU ~5.5, ICON-D2 ~2.5. */
export const ENSEMBLE_MODEL_ORDER = ['ecmwf_ifs025', 'icon_eu', 'icon_d2'] as const

export const ENSEMBLE_MODEL_SHORT: Record<string, string> = {
  ecmwf_ifs025: 'ECMWF',
  icon_eu: 'ICON-EU',
  icon_d2: 'ICON-D2',
}

/** Window of `horizon` ("24h" | "72h") starting `leadStart` hours after the issue time. */
export function ensembleWindow(
  ensemble: EnsembleForecast | null | undefined,
  horizon: '24h' | '72h',
  leadStart = 0,
): EnsembleHorizon | null {
  const windows = ensemble?.rain?.available ? ensemble.rain.horizons?.[horizon] : undefined
  return windows?.find((w) => w.lead_start_h === leadStart) ?? null
}

/** Models of the ensemble that are available, in ENSEMBLE_MODEL_ORDER. */
export function availableModels(ensemble: EnsembleForecast | null | undefined): string[] {
  const models = ensemble?.rain?.models ?? []
  return ENSEMBLE_MODEL_ORDER.filter((id) => models.some((m) => m.id === id && m.available))
}

/** First model (ECMWF, then ICON-EU, ICON-D2) present in the window. */
export function preferredModel(window: EnsembleHorizon | null): string | null {
  if (!window) return null
  return ENSEMBLE_MODEL_ORDER.find((id) => window.models[id]) ?? Object.keys(window.models)[0] ?? null
}

export function modelLabel(ensemble: EnsembleForecast | null | undefined, id: string): string {
  return ensemble?.rain?.models?.find((m) => m.id === id)?.label ?? ENSEMBLE_MODEL_SHORT[id] ?? id
}

/** "0,1 mm [0,0–0,3]" : median and p10-p90 of a window total. */
export function formatEnsembleStats(stats: Pick<EnsembleWindowStats, 'p10' | 'p50' | 'p90'>): string {
  return `${formatNumber(stats.p50, 1)} mm [${formatNumber(stats.p10, 1)}–${formatNumber(stats.p90, 1)}]`
}

export interface EnsembleAgreement {
  kind: 'ok' | 'warn' | 'info'
  lead: string
  text: string
}

/** Two models disagree when their shares above the first 72 h threshold differ by this much. */
export const DISAGREEMENT_PROB = 0.3
/** Issue older than this: the ensemble is flagged as late. */
export const ENSEMBLE_STALE_HOURS = 12

const pct = (p: number) => `${formatNumber(p * 100, 0)} %`

/**
 * Agreement of the rain ensembles over the next 72 h, as a checkable reason
 * of the confidence block: share of members above the first 72 h threshold
 * of the model (50 mm) for each model, and the medians.
 */
export function ensembleAgreement(
  ensemble: EnsembleForecast | null | undefined,
  now: number = Date.now(),
): EnsembleAgreement {
  const window = ensembleWindow(ensemble, '72h', 0)
  const threshold = ensemble?.rain?.thresholds?.['72h']?.[0]
  const ids = window ? ENSEMBLE_MODEL_ORDER.filter((id) => window.models[id]) : []
  if (!window || ids.length === 0 || threshold == null) {
    return {
      kind: 'info',
      lead: "Pluie d'ensemble indisponible",
      text: ensemble?.rain?.note ?? "l'accord des modèles sur 72 h n'est pas connu.",
    }
  }
  const key = String(threshold)
  const probs = ids.map((id) => window.models[id].prob[key] ?? 0)
  const members = ids.reduce((n, id) => n + window.models[id].members, 0)
  const spread = Math.max(...probs) - Math.min(...probs)
  const perModel = ids
    .map((id, i) => `${ENSEMBLE_MODEL_SHORT[id] ?? id} ${pct(probs[i])}`)
    .join(', ')
  const medians = ids.map((id) => window.models[id].p50)
  const medianText =
    Math.min(...medians) === Math.max(...medians)
      ? `${formatNumber(medians[0], 1)} mm`
      : `${formatNumber(Math.min(...medians), 1)} à ${formatNumber(Math.max(...medians), 1)} mm`
  const issued = parseApiDate(ensemble?.rain?.issued_at)
  const late = issued !== null && now - issued.getTime() > ENSEMBLE_STALE_HOURS * 3600_000
  const lateText = late && issued ? ` Ensembles relevés ${formatAge(issued, now)}.` : ''

  if (ids.length > 1 && spread >= DISAGREEMENT_PROB) {
    return {
      kind: 'warn',
      lead: 'Ensembles en désaccord',
      text: `part des membres au-dessus de ${formatNumber(threshold, 0)} mm en 72 h : ${perModel}.${lateText}`,
    }
  }
  return {
    kind: late ? 'warn' : 'ok',
    lead: ids.length > 1 ? `Ensembles d'accord (${ids.length} modèles)` : 'Un seul ensemble',
    text:
      `pluie médiane sur 72 h ${medianText} ; au-dessus de ${formatNumber(threshold, 0)} mm : ${perModel}` +
      ` (${members} membres).${lateText}`,
  }
}
