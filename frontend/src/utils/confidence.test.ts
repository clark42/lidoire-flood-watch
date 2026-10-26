import { describe, expect, it } from 'vitest'
import type { RiskOutlook, SourceFreshness } from '../types'
import fixture from '../test-fixtures/outlook.json'
import { confidenceItems } from './confidence'

const outlook = fixture as unknown as RiskOutlook
const NOW = Date.parse('2026-10-26T22:40:00Z')

function source(over: Partial<SourceFreshness>): SourceFreshness {
  return {
    label: 'x',
    status: 'ok',
    last_timestamp: '2026-10-26T22:15:00',
    age_hours: 0.4,
    max_age_hours: 3,
    critical: true,
    ...over,
  }
}

describe('confidenceItems', () => {
  it('lists fresh Pessac first, warnings, then notes and the model reminder', () => {
    const items = confidenceItems(
      outlook.confidence,
      { river_primary: source({}), risk_score: source({ last_timestamp: '2026-10-26T22:30:00' }) },
      NOW,
    )
    expect(items[0]).toEqual({
      kind: 'ok',
      lead: 'Pessac à jour',
      text: 'dernière mesure il y a 25 min, score recalculé il y a 10 min.',
    })
    const kinds = items.map((i) => i.kind)
    // warnings come before infos
    expect(kinds.lastIndexOf('warn')).toBeLessThan(kinds.indexOf('info'))
    expect(items.find((i) => i.lead === 'Source Pluie Infoclimat')?.kind).toBe('warn')
    expect(items[items.length - 1].lead).toBe('Modèle calé sur 5 crues')
  })

  it('omits the Pessac line when the station is stale or status is unknown', () => {
    const stale = confidenceItems(outlook.confidence, { river_primary: source({ status: 'stale' }) }, NOW)
    expect(stale.some((i) => i.lead === 'Pessac à jour')).toBe(false)
    const none = confidenceItems(outlook.confidence, null, NOW)
    expect(none.some((i) => i.kind === 'ok')).toBe(false)
  })

  it('keeps a note without " : " whole', () => {
    const items = confidenceItems({ notes: ['Marées estimées (pas de prédiction fiable)'] }, null, NOW)
    expect(items[0]).toEqual({ kind: 'warn', lead: '', text: 'Marées estimées (pas de prédiction fiable)' })
  })
})
