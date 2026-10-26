import { describe, expect, it } from 'vitest'
import type { OutlookHorizon, RiskLevel, RiskOutlook } from '../types'
import fixture from '../test-fixtures/outlook.json'
import { buildVerdict, levelFromScore, levelRank, peakHorizon, worstLevel } from './levels'

// Production payload of GET /api/risk/outlook (2026-10-26 22:33 UTC)
const outlook = fixture as unknown as RiskOutlook
const NOW = new Date('2026-10-26T22:40:00Z') // 23:40 in Paris (CET)

function horizon(hours: number, level: RiskLevel, over: Partial<OutlookHorizon> = {}): OutlookHorizon {
  return {
    hours,
    label: `${hours} h`,
    max_score: 10,
    level,
    max_at: '2026-10-27T10:00:00Z',
    max_hours_ahead: hours,
    first_at_level: '2026-10-27T08:00:00Z',
    drivers: [],
    modifiers: [],
    unavailable: [],
    summary: '',
    ...over,
  }
}

describe('levels', () => {
  it('ranks green to red as 1 to 4 pips', () => {
    expect(['green', 'yellow', 'orange', 'red'].map((l) => levelRank(l as RiskLevel))).toEqual([1, 2, 3, 4])
  })

  it('maps scores with the model thresholds 15 / 35 / 55', () => {
    expect(levelFromScore(0)).toBe('green')
    expect(levelFromScore(14.9)).toBe('green')
    expect(levelFromScore(15)).toBe('yellow')
    expect(levelFromScore(34.99)).toBe('yellow')
    expect(levelFromScore(35)).toBe('orange')
    expect(levelFromScore(55)).toBe('red')
    expect(levelFromScore(100)).toBe('red')
  })

  it('keeps the worst level and ignores missing values', () => {
    expect(worstLevel([])).toBe('green')
    expect(worstLevel([null, 'yellow', undefined, 'green'])).toBe('yellow')
    expect(worstLevel(['orange', 'red', 'yellow'])).toBe('red')
  })

  it('takes the 72 h horizon as the peak', () => {
    expect(peakHorizon([horizon(6, 'green'), horizon(72, 'yellow'), horizon(24, 'green')])?.hours).toBe(72)
    expect(peakHorizon([])).toBeNull()
  })
})

describe('buildVerdict', () => {
  it('reads the production payload: yellow, watch, peak tomorrow', () => {
    const v = buildVerdict(outlook, 'yellow', NOW)
    expect(v.level).toBe('yellow')
    expect(v.title).toBe('Surveiller')
    expect(v.detail).toBe('et revenir dans quelques heures')
    expect(v.peak?.hours).toBe(72)
    expect(v.why.lead).toBe('Maximum de 23/100 demain à 11:33, surtout :')
    expect(v.why.reasons).toHaveLength(3)
    expect(v.why.reasons[0]).toMatch(/Pessac-sur-Dordogne/)
  })

  it('says there is nothing to do when everything is green', () => {
    const v = buildVerdict({ horizons: [horizon(6, 'green'), horizon(24, 'green'), horizon(72, 'green')] }, 'green', NOW)
    expect(v.title).toBe('Rien à faire')
    expect(v.detail).toBe("ni ce soir, ni d'ici 3 jours")
    expect(v.why.lead).toBe("Aucun facteur notable : score au plus 10/100 d'ici 72 h.")
  })

  it('uses the worst horizon, not the current level only', () => {
    const v = buildVerdict(
      { horizons: [horizon(6, 'green'), horizon(24, 'orange', { first_at_level: '2026-10-27T13:00:00Z' }), horizon(72, 'orange', { first_at_level: '2026-10-27T13:00:00Z' })] },
      'green',
      NOW,
    )
    expect(v.level).toBe('orange')
    expect(v.title).toBe('Préparer le moulin')
    // 13:00 UTC = 14:00 in Paris
    expect(v.detail).toBe('avant demain à 14:00')
  })

  it('lets a worse current score win over the forecast', () => {
    const v = buildVerdict({ horizons: [horizon(6, 'yellow'), horizon(72, 'yellow')] }, 'red', NOW)
    expect(v.level).toBe('red')
    expect(v.title).toBe('Agir maintenant')
    expect(v.detail).toBe("l'eau peut arriver")
  })

  it('announces a red level reached later', () => {
    const v = buildVerdict(
      { horizons: [horizon(6, 'yellow'), horizon(72, 'red', { first_at_level: '2026-10-28T20:00:00Z' })] },
      'yellow',
      NOW,
    )
    expect(v.detail).toBe('niveau rouge prévu mer. 28 à 21:00')
  })

  it('says "dès maintenant" when an orange level is already reached', () => {
    const v = buildVerdict({ horizons: [horizon(6, 'orange', { first_at_level: '2026-10-26T22:00:00Z' })] }, null, NOW)
    expect(v.detail).toBe('dès maintenant')
  })

  it('handles an empty outlook', () => {
    const v = buildVerdict({ horizons: [] }, null, NOW)
    expect(v.level).toBe('green')
    expect(v.peak).toBeNull()
    expect(v.why.lead).toBe('Prévision du score indisponible.')
  })
})
