// Source list of the "Fiabilité" section (SourceHealth): grouping and
// freshness text of /api/status.sources.
import type { SourceFreshness, SourceFreshnessState, SystemStatus } from '../types'
import { formatAge, formatParis, parseApiDate } from './time'

export type SourceGroupId = 'critical' | 'inputs' | 'indicators' | 'probabilistic'

export const SOURCE_GROUPS: { id: SourceGroupId; title: string; hint: string }[] = [
  { id: 'critical', title: 'Critiques', hint: 'Le score en dépend : en retard, le tableau de bord est dégradé.' },
  { id: 'inputs', title: 'Entrées du modèle', hint: 'Pluie, marée, sol : un retard réduit la confiance du score.' },
  { id: 'indicators', title: 'Indicateurs', hint: 'Mesures et vigilances hors score, pour comprendre.' },
  { id: 'probabilistic', title: 'Probabiliste', hint: 'Ensembles, GloFAS, radar : informatifs, hors score.' },
]

const MODEL_INPUTS = new Set(['rain_openmeteo', 'infoclimat', 'netatmo', 'tide', 'soil'])
const PROBABILISTIC = new Set(['radar_opera', 'glofas', 'ensemble_rain'])

export function sourceGroup(key: string, source: Pick<SourceFreshness, 'critical'>): SourceGroupId {
  if (source.critical) return 'critical'
  if (MODEL_INPUTS.has(key)) return 'inputs'
  if (PROBABILISTIC.has(key)) return 'probabilistic'
  return 'indicators'
}

export const STATUS_LABELS: Record<SourceFreshnessState, string> = {
  ok: 'à jour',
  stale: 'en retard',
  unavailable: 'indisponible',
  unknown: 'inconnu',
}

/** Readable label: "Indicateur Debit Eyraud a La Force" -> "Débit Eyraud à La Force". */
export function sourceLabel(label: string): string {
  return label
    .replace(/^Indicateur\s+/i, '')
    .replace(/\bDebit\b/g, 'Débit')
    .replace(/\bMarees?\b/g, (m) => (m.endsWith('s') ? 'Marées' : 'Marée'))
    .replace(/\bobservee\b/g, 'observée')
    .replace(/\bMeteo\b/g, 'Météo')
    .replace(/\btemps reel\b/g, 'temps réel')
    .replace(/ a venir\b/g, ' à venir')
    .replace(/ a /g, ' à ')
}

/**
 * Freshness of the newest stored point: "il y a 15 min"; for sources that
 * store the future (tide extremes, negative age) "jusqu'au …".
 */
export function freshnessText(source: SourceFreshness, now: number): string {
  const last = parseApiDate(source.last_timestamp)
  if (!last) {
    if (source.status === 'unknown') return 'non vérifié'
    return source.note ?? 'aucune donnée (30 j)'
  }
  if (last.getTime() > now) return `connue jusqu’au ${formatParis(last, 'EEE d MMM HH:mm')}`
  return formatAge(last, now)
}

export interface HealthItem {
  key: string
  label: string
  status: SourceFreshnessState
  freshness: string
  /** Staleness limit, hours (0 for the tide) */
  maxAgeHours: number
  note: string | null
  critical: boolean
}

export interface HealthGroup {
  id: SourceGroupId
  title: string
  hint: string
  items: HealthItem[]
}

const STATUS_ORDER: Record<SourceFreshnessState, number> = { unavailable: 0, stale: 1, unknown: 2, ok: 3 }

/** Groups in display order; inside a group, problems first, then by label. */
export function groupSources(sources: SystemStatus['sources'] | null | undefined, now: number): HealthGroup[] {
  const groups = new Map<SourceGroupId, HealthItem[]>(SOURCE_GROUPS.map((g) => [g.id, []]))
  for (const [key, s] of Object.entries(sources ?? {})) {
    if (!s) continue
    const status: SourceFreshnessState = s.status in STATUS_ORDER ? s.status : 'unknown'
    groups.get(sourceGroup(key, s))!.push({
      key,
      label: sourceLabel(s.label),
      status,
      freshness: freshnessText(s, now),
      maxAgeHours: s.max_age_hours,
      note: s.error ?? (s.last_timestamp ? (s.note ?? null) : null),
      critical: s.critical,
    })
  }
  return SOURCE_GROUPS.map((g) => ({
    ...g,
    items: groups
      .get(g.id)!
      .sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.label.localeCompare(b.label, 'fr')),
  })).filter((g) => g.items.length > 0)
}

export function statusCounts(groups: readonly HealthGroup[]): Record<SourceFreshnessState, number> {
  const out: Record<SourceFreshnessState, number> = { ok: 0, stale: 0, unavailable: 0, unknown: 0 }
  for (const g of groups) for (const i of g.items) out[i.status] += 1
  return out
}
