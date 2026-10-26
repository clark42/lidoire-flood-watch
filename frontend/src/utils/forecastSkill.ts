// Forecast vs observed (ForecastVsObserved): archive state, pairs of a lead
// and the skill table. Pure helpers over /api/risk/forecast-skill.
import type { ForecastSkill, ForecastSkillLead } from '../types'
import { formatNumber, MISSING_VALUE } from './format'

/** Pairs per lead under which the scores are flagged as fragile */
export const SHORT_ARCHIVE_PAIRS = 48

export type SkillState = 'empty' | 'short' | 'ok'

/** empty: no pair yet; short: some, too few to judge; ok: enough on every lead. */
export function skillState(skill: Pick<ForecastSkill, 'by_lead' | 'pairs'>): SkillState {
  const counts = skill.by_lead.map((l) => l.n)
  if (skill.pairs.length === 0 && counts.every((n) => n === 0)) return 'empty'
  return counts.length > 0 && counts.every((n) => n >= SHORT_ARCHIVE_PAIRS) ? 'ok' : 'short'
}

export interface SkillPoint {
  /** Valid time (ms) */
  t: number
  forecast: number
  actual: number
  runAt: number
}

/** Forecast at `lead` against the realised score, by valid time. */
export function pairsForLead(skill: Pick<ForecastSkill, 'pairs'>, lead: number): SkillPoint[] {
  return skill.pairs
    .filter((p) => p.lead_hours === lead)
    .map((p) => ({
      t: Date.parse(p.valid_at),
      forecast: p.forecast_score,
      actual: p.actual_score,
      runAt: Date.parse(p.run_at),
    }))
    .filter((p) => Number.isFinite(p.t))
    .sort((a, b) => a.t - b.t)
}

/** Default lead of the chart: 24 h when served, else the first one. */
export function defaultLead(skill: Pick<ForecastSkill, 'lead_hours'>): number | null {
  if (skill.lead_hours.includes(24)) return 24
  return skill.lead_hours[0] ?? null
}

export interface SkillRow {
  lead: number
  n: number
  mae: string
  bias: string
  /** hits / misses / false alarms / correct negatives */
  contingency: [number, number, number, number]
  pod: string
  far: string
  csi: string
}

const ratio = (v: number | null) => (v == null ? MISSING_VALUE : `${formatNumber(v * 100, 0)} %`)
const signed = (v: number | null) =>
  v == null ? MISSING_VALUE : `${v > 0 ? '+' : ''}${formatNumber(v, 1)}`

export function skillRow(l: ForecastSkillLead): SkillRow {
  return {
    lead: l.lead_hours,
    n: l.n,
    mae: l.mae == null ? MISSING_VALUE : formatNumber(l.mae, 1),
    bias: signed(l.bias),
    contingency: [l.hits, l.misses, l.false_alarms, l.correct_negatives],
    pod: ratio(l.pod),
    far: ratio(l.far),
    csi: ratio(l.csi),
  }
}

export function skillRows(skill: Pick<ForecastSkill, 'by_lead'>): SkillRow[] {
  return [...skill.by_lead].sort((a, b) => a.lead_hours - b.lead_hours).map(skillRow)
}

/** Score domain of the chart: 0-100, tightened to 0-60 when everything stays low. */
export function scoreDomain(points: readonly SkillPoint[], threshold: number): [number, number] {
  const hi = Math.max(threshold, ...points.flatMap((p) => [p.forecast, p.actual]))
  return [0, hi <= 50 ? 60 : 100]
}
