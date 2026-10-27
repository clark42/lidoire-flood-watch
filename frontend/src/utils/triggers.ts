// "Ce qui me ferait changer d'avis": the thresholds that would raise the
// verdict, from the data and the config (never invented), with how far the
// value is from each. Provisional = tuned on the labelled floods (in-sample).
import type {
  EnsembleForecast,
  EpisodeDriversData,
  IndicatorsData,
  LidoireProfileData,
  RealtimeData,
  RiskOutlook,
  VigilanceStatus,
} from '../types'
import type { ValueKind } from '../components/ui/valueKinds'
import { formatNumber, formatQuantity } from './format'
import { LEVEL_THRESHOLDS } from './levels'
import { ensembleWindow, preferredModel, ENSEMBLE_MODEL_SHORT } from './ensemble'
import { PESSAC_STATION_ID, pessacEnvelope } from './mechanisms'

export interface Trigger {
  id: string
  label: string
  value: number | null
  valueText: string
  /** null: official information, not a value of the honesty grammar */
  kind: ValueKind | null
  threshold: number
  thresholdText: string
  /** What crossing it means */
  why: string
  /** 0-1 share of the way from `base` to the threshold (log for flows) */
  progress: number
  reached: boolean
  provisional: boolean
  source: string
}

export interface TriggerInputs {
  indicators?: IndicatorsData | null
  profile?: LidoireProfileData | null
  drivers?: EpisodeDriversData | null
  ensemble?: EnsembleForecast | null
  realtime?: RealtimeData | null
  outlook?: RiskOutlook | null
  vigilance?: VigilanceStatus | null
}

/** Share of the way from base to threshold, clamped to [0, 1]; log10 when `log`. */
export function triggerProgress(value: number | null, threshold: number, base = 0, log = false): number {
  if (value == null || !Number.isFinite(value)) return 0
  if (log) {
    const lo = Math.log10(Math.max(base, 1e-6))
    const span = Math.log10(threshold) - lo
    if (span <= 0) return value >= threshold ? 1 : 0
    return Math.min(1, Math.max(0, (Math.log10(Math.max(value, base)) - lo) / span))
  }
  const span = threshold - base
  if (span <= 0) return value >= threshold ? 1 : 0
  return Math.min(1, Math.max(0, (value - base) / span))
}

const VIGILANCE_WORDS = ['', 'vert', 'jaune', 'orange', 'rouge']

function make(
  t: Omit<Trigger, 'progress' | 'reached'>,
  base: number,
  log = false,
): Trigger {
  return {
    ...t,
    progress: triggerProgress(t.value, t.threshold, base, log),
    reached: t.value != null && t.value >= t.threshold,
  }
}

/** Lowest value of a driver among the labelled floods (in-sample). */
function lowestFlood(drivers: EpisodeDriversData | null | undefined, key: 'eyraud_q_m3s' | 'rain_72h_mm'): number | null {
  const values = (drivers?.episodes ?? [])
    .filter((e) => e.outcome === 'flooded' && !e.flags.includes('mechanism_infiltration_since_sealed'))
    .map((e) => e.values[key])
    .filter((v): v is number => v != null && Number.isFinite(v))
  return values.length ? Math.min(...values) : null
}

/**
 * Triggers in mechanism order (runoff first): each one only when both its
 * value and its threshold are known.
 */
export function buildTriggers(inputs: TriggerInputs): Trigger[] {
  const out: Trigger[] = []

  // Forecast score: the verdict itself moves at the orange threshold
  const peak = inputs.outlook?.horizons.reduce<number | null>((m, h) => Math.max(m ?? 0, h.max_score), null) ?? null
  if (peak != null) {
    out.push(
      make(
        {
          id: 'score',
          label: 'Score prévu, max 72 h',
          value: peak,
          valueText: `~ ${formatNumber(peak, 0)}/100`,
          kind: 'model',
          threshold: LEVEL_THRESHOLDS.orange,
          thresholdText: `${LEVEL_THRESHOLDS.orange}/100 (orange)`,
          why: 'le verdict passe à « Préparer le moulin »',
          provisional: false,
          source: 'Score v3.4, /api/risk/outlook',
        },
        0,
      ),
    )
  }

  // Runoff: Eyraud daily-mean alert threshold (in-sample)
  const eyraud = inputs.indicators?.runoff.find((r) => r.key === 'EYRAUD')
  const alert = eyraud?.thresholds_daily_m3s?.alert
  if (eyraud && alert != null) {
    const v = eyraud.q_mean_24h_m3s ?? eyraud.q_m3s
    const low = lowestFlood(inputs.drivers, 'eyraud_q_m3s')
    out.push(
      make(
        {
          id: 'eyraud',
          label: "Eyraud, débit moyen 24 h",
          value: v,
          valueText: v == null ? '–' : `${formatQuantity(v)} m³/s`,
          kind: 'analog',
          threshold: alert,
          thresholdText: `${formatNumber(alert, 1)} m³/s`,
          why:
            'ruissellement de crue : la Lidoire déborde au pic de l’Eyraud' +
            (low != null ? ` (plus faible crue labellisée : ${formatQuantity(low)} m³/s au max)` : ''),
          provisional: true,
          source: "Seuil 'alert' de /api/indicators",
        },
        0.01,
        true,
      ),
    )
  }

  // Rain forecast: wettest ensemble member over 72 h vs the first 72 h threshold of the model
  const window = ensembleWindow(inputs.ensemble, '72h', 0)
  const model = preferredModel(window)
  const rainThreshold = inputs.ensemble?.rain?.thresholds?.['72h']?.[0]
  if (window && model && rainThreshold != null) {
    const v = window.models[model].max
    out.push(
      make(
        {
          id: 'rain_forecast',
          label: `Pluie prévue 72 h, membre le plus pluvieux (${ENSEMBLE_MODEL_SHORT[model] ?? model})`,
          value: v,
          valueText: `~ ${formatNumber(v, 1)} mm`,
          kind: 'model',
          threshold: rainThreshold,
          thresholdText: `${formatNumber(rainThreshold, 0)} mm`,
          why: 'premier seuil du facteur pluie 72 h du modèle',
          provisional: false,
          source: 'Seuils pluie 72 h de la config (get_model_params)',
        },
        0,
      ),
    )
  }

  // Observed rain vs the lowest rain of the labelled floods (in-sample)
  const rainObs = inputs.realtime?.rainfall?.last_72h_mm
  const rainFlood = lowestFlood(inputs.drivers, 'rain_72h_mm')
  if (rainObs != null && rainFlood != null) {
    out.push(
      make(
        {
          id: 'rain_observed',
          label: 'Pluie tombée sur 72 h',
          value: rainObs,
          valueText: `${formatNumber(rainObs, 1)} mm`,
          kind: 'measured',
          threshold: rainFlood,
          thresholdText: `${formatNumber(rainFlood, 1)} mm`,
          why: 'pluie de la crue labellisée la moins arrosée',
          provisional: true,
          source: '/api/episodes/drivers',
        },
        0,
      ),
    )
  }

  // Downstream Dordogne raised (modulating factor): Pessac orange threshold
  const h = pessacEnvelope(inputs.profile, inputs.outlook)
  const pessac = inputs.realtime?.stations?.stations.find((s) => s.station_id === PESSAC_STATION_ID)
  if (h != null && pessac?.threshold_orange != null) {
    out.push(
      make(
        {
          id: 'pessac',
          label: 'Pessac, max 12,5 h',
          value: h,
          valueText: `${formatNumber(h, 2)} m`,
          kind: 'measured',
          threshold: pessac.threshold_orange,
          thresholdText: `${formatNumber(pessac.threshold_orange, 2)} m (orange)`,
          why: 'Dordogne relevée : elle freine la Lidoire (modulateur, ne suffit pas seule)',
          provisional: false,
          source: 'Seuils de station de la config',
        },
        0,
      ),
    )
  }

  // Alluvial water table (in-sample)
  const gw = inputs.indicators?.groundwater?.[0]
  const gwWatch = gw?.thresholds_m_ngf?.watch
  if (gw && gw.level_m_ngf != null && gwWatch != null) {
    out.push(
      make(
        {
          id: 'groundwater',
          label: 'Nappe de Mouliets',
          value: gw.level_m_ngf,
          valueText: `${formatNumber(gw.level_m_ngf, 2)} m NGF`,
          kind: 'measured',
          threshold: gwWatch,
          thresholdText: `${formatNumber(gwWatch, 1)} m NGF`,
          why: 'niveau dépassé à chaque crue connue',
          provisional: true,
          source: "Seuil 'watch' de /api/indicators",
        },
        Math.floor(gwWatch - 2),
      ),
    )
  }

  // Official vigilance on the Dordogne aval section
  const do5 = inputs.vigilance?.vigicrues?.available
    ? inputs.vigilance.vigicrues.troncons.find((t) => t.code === 'DO5') ?? inputs.vigilance.vigicrues.troncons[0]
    : undefined
  if (do5?.level != null) {
    out.push(
      make(
        {
          id: 'vigicrues',
          label: `Vigicrues ${do5.name}`,
          value: do5.level,
          valueText: VIGILANCE_WORDS[do5.level],
          kind: null,
          threshold: 2,
          thresholdText: 'jaune',
          why: 'vigilance officielle crues (hors score)',
          provisional: false,
          source: '/api/vigilance',
        },
        1,
      ),
    )
  }

  return out
}
