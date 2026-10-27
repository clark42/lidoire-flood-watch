import { describe, expect, it } from 'vitest'
import type { IndicatorsData, LidoireProfileData, RealtimeData, RiskOutlook } from '../types'
import indicatorsFixture from '../test-fixtures/indicators.json'
import realtimeFixture from '../test-fixtures/realtime.json'
import outlookFixture from '../test-fixtures/outlook.json'
import { mechanismWhy } from './verdictWhy'

const indicators = indicatorsFixture as unknown as IndicatorsData
const realtime = realtimeFixture as unknown as RealtimeData
const outlook = outlookFixture as unknown as RiskOutlook

function withPessacMax(h: number): LidoireProfileData {
  return { confluence: { inputs: { pessac_max_m: h } } } as unknown as LidoireProfileData
}

describe('mechanismWhy (verdict "Pourquoi", runoff first)', () => {
  it('leads with the Eyraud discharge, then observed and forecast rain (prod payloads)', () => {
    const why = mechanismWhy({ indicators, realtime, outlook })
    expect(why.lead).toMatch(/ruissellement/)
    expect(why.reasons[0]).toBe('Eyraud 0,066 m³/s, stable (moyenne 24 h 0,071 m³/s, à surveiller dès 13,1 m³/s)')
    expect(why.reasons[1]).toBe('pluie tombée 0,3 mm sur 24 h, 9,1 mm sur 72 h')
    expect(why.reasons[2]).toBe('pluie prévue ~ 0,0 mm sur 24 h, 0,0 mm sur 72 h')
    // Pessac (max 12.5 h 0.61 m) is low: the Dordogne is not mentioned
    expect(why.reasons).toHaveLength(3)
    expect(why.reasons.join(' ')).not.toMatch(/Pessac|Dordogne/)
  })

  it('mentions the Dordogne second, only when Pessac reaches its yellow threshold', () => {
    const high = mechanismWhy({ indicators, realtime, outlook, profile: withPessacMax(3.4) })
    expect(high.reasons).toHaveLength(4)
    expect(high.reasons[3]).toMatch(/^en second, Dordogne haute : Pessac 3,40 m sur 12,5 h \(jaune 3,0 m\)/)
    expect(mechanismWhy({ indicators, realtime, outlook, profile: withPessacMax(2.9) }).reasons).toHaveLength(3)
  })

  it('says when the Eyraud is loading or unavailable instead of inventing a value', () => {
    expect(mechanismWhy({ realtime }).reasons[0]).toBe("débit de l'Eyraud en cours de chargement")
    expect(mechanismWhy({ indicators: null, realtime }).reasons[0]).toBe("débit de l'Eyraud indisponible")
  })

  it('states the threshold reached by the Eyraud', () => {
    const wet = structuredClone(indicators)
    const e = wet.runoff.find((r) => r.key === 'EYRAUD')!
    e.q_m3s = 21.4
    e.trend = 'rising'
    e.threshold.exceeded = ['watch', 'alert']
    expect(mechanismWhy({ indicators: wet, realtime }).reasons[0]).toMatch(
      /^Eyraud 21 m³\/s, en hausse \(.*au-delà du seuil d'alerte 17,5 m³\/s\)$/,
    )
  })
})
