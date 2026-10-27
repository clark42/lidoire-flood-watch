import { describe, expect, it } from 'vitest'
import type { RiskOutlook, SystemStatus } from '../types'
import statusFixture from '../test-fixtures/status.json'
import outlookFixture from '../test-fixtures/outlook.json'
import { confidenceItems } from './confidence'
import { groupSources } from './sourceHealth'
import { needsNetatmoConnect, plainText } from './userText'

const sources = (statusFixture as unknown as SystemStatus).sources
const outlook = outlookFixture as unknown as RiskOutlook
const NOW = Date.parse('2026-10-26T23:10:00Z')

describe('no API path in user-facing text (UAT: "autoriser via /api/auth/netatmo")', () => {
  it('turns the Netatmo token note into a plain sentence and flags the action', () => {
    const note = 'aucun jeton : autoriser via /api/auth/netatmo'
    expect(plainText(note)).toBe('Netatmo n’est pas connecté')
    expect(needsNetatmoConnect(note)).toBe(true)
    expect(needsNetatmoConnect('dernier point il y a 9,6 h')).toBe(false)
    expect(plainText('Source Pluie Netatmo : aucune donnée récente (aucun jeton : autoriser via /api/auth/netatmo).')).toBe(
      'Source Pluie Netatmo : aucune donnée récente (Netatmo n’est pas connecté).',
    )
    expect(plainText('No tokens stored. Visit /api/auth/netatmo to authorize.')).not.toMatch(/\/api\//)
  })

  it('cleans the source list of the reliability section and offers the connection', () => {
    const items = groupSources(sources, NOW).flatMap((g) => g.items)
    for (const i of items) {
      expect(i.freshness).not.toMatch(/\/api\//)
      expect(i.note ?? '').not.toMatch(/\/api\//)
    }
    const netatmo = items.find((i) => i.key === 'netatmo')!
    expect(netatmo.freshness).toBe('Netatmo n’est pas connecté')
    expect(netatmo.connectNetatmo).toBe(true)
    expect(items.filter((i) => i.connectNetatmo)).toHaveLength(1)
  })

  it('cleans the confidence reasons of the verdict', () => {
    const items = confidenceItems(outlook.confidence, sources, NOW)
    for (const i of items) expect(`${i.lead} ${i.text}`).not.toMatch(/\/api\//)
    const netatmo = items.find((i) => i.lead === 'Source Pluie Netatmo')!
    expect(netatmo.text).toBe('aucune donnée récente (Netatmo n’est pas connecté).')
    expect(netatmo.connectNetatmo).toBe(true)
  })
})
