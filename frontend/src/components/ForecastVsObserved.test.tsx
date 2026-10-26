import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ForecastSkill } from '../types'
import skillFixture from '../test-fixtures/forecast_skill.json'
import { defaultLead, pairsForLead, scoreDomain, skillRows, skillState } from '../utils/forecastSkill'
import { ForecastVsObservedView } from './ForecastVsObserved'

// Prod payload of 2026-10-26: 2 runs archived, no lead reached yet
const empty = skillFixture as unknown as ForecastSkill

function text(html: string): string {
  return html.replace(/<[^>]+>/g, '').replace(/&#x27;/g, "'").replace(/&amp;/g, '&')
}

/** A short archive: three 24 h pairs, one hit, one false alarm. */
const SHORT: ForecastSkill = {
  ...empty,
  n_runs: 30,
  pairs: [
    { run_at: '2026-10-27T10:00:00Z', lead_hours: 24, valid_at: '2026-10-28T10:00:00Z', forecast_score: 40, actual_score: 20, actual_at: '2026-10-28T10:02:00Z' },
    { run_at: '2026-10-27T08:00:00Z', lead_hours: 24, valid_at: '2026-10-28T08:00:00Z', forecast_score: 50, actual_score: 45, actual_at: '2026-10-28T08:01:00Z' },
    { run_at: '2026-10-27T09:00:00Z', lead_hours: 24, valid_at: '2026-10-28T09:00:00Z', forecast_score: 10, actual_score: 12, actual_at: '2026-10-28T09:01:00Z' },
    { run_at: '2026-10-28T03:00:00Z', lead_hours: 6, valid_at: '2026-10-28T09:00:00Z', forecast_score: 11, actual_score: 12, actual_at: '2026-10-28T09:01:00Z' },
  ],
  by_lead: [
    { lead_hours: 72, n: 0, mae: null, bias: null, hits: 0, misses: 0, false_alarms: 0, correct_negatives: 0, pod: null, far: null, csi: null },
    { lead_hours: 24, n: 3, mae: 9, bias: 7.67, hits: 1, misses: 0, false_alarms: 1, correct_negatives: 1, pod: 1, far: 0.5, csi: 0.5 },
    { lead_hours: 6, n: 1, mae: 1, bias: -1, hits: 0, misses: 0, false_alarms: 0, correct_negatives: 1, pod: null, far: null, csi: null },
  ],
}

describe('forecast skill helpers', () => {
  it('tells an empty, a short and a usable archive apart', () => {
    expect(skillState(empty)).toBe('empty')
    expect(skillState(SHORT)).toBe('short')
    const full = { ...SHORT, by_lead: SHORT.by_lead.map((l) => ({ ...l, n: 60 })) }
    expect(skillState(full)).toBe('ok')
  })

  it('orders the pairs of one lead by valid time', () => {
    const pts = pairsForLead(SHORT, 24)
    expect(pts.map((p) => p.forecast)).toEqual([50, 10, 40])
    expect(pairsForLead(SHORT, 6)).toHaveLength(1)
    expect(pairsForLead(empty, 24)).toEqual([])
    expect(defaultLead(empty)).toBe(24)
    expect(defaultLead({ lead_hours: [6, 72] })).toBe(6)
  })

  it('formats the table by increasing lead', () => {
    const rows = skillRows(SHORT)
    expect(rows.map((r) => r.lead)).toEqual([6, 24, 72])
    const r24 = rows[1]
    expect(r24.mae).toBe('9,0')
    expect(r24.bias).toBe('+7,7')
    expect(r24.contingency).toEqual([1, 0, 1, 1])
    expect(r24.pod).toBe('100 %')
    expect(r24.far).toBe('50 %')
    expect(rows[2].mae).toBe('–')
  })

  it('keeps the threshold in view and zooms on low scores', () => {
    expect(scoreDomain(pairsForLead(SHORT, 24), 35)).toEqual([0, 60])
    expect(scoreDomain([{ t: 0, forecast: 80, actual: 10, runAt: 0 }], 35)).toEqual([0, 100])
  })
})

describe('ForecastVsObservedView', () => {
  it('shows an honest empty state on the prod archive', () => {
    const t = text(renderToStaticMarkup(<ForecastVsObservedView skill={empty} width={311} />))
    expect(t).toContain('Pas encore de quoi juger.')
    expect(t).toContain('2 prévisions archivées')
    expect(t).toContain('aucune échéance (6 / 24 / 72 h)')
    expect(t).toContain('Runs archivés, mais aucune échéance encore atteinte')
  })

  it('draws the 24 h pairs, flags the short archive and fills the table', () => {
    const html = renderToStaticMarkup(<ForecastVsObservedView skill={SHORT} width={311} />)
    const t = text(html)
    expect(t).toContain('Archive courte')
    expect(html).toContain('class="fvo-forecast"')
    expect(html).toContain('aria-pressed="true"')
    expect(t).toContain('prévu 24 h avant')
    expect(t).toContain('1 / 0 / 1 / 1')
    expect(t).toContain('seuil orange 35')
  })
})
