import { describe, expect, it } from 'vitest'
import type {
  EnsembleForecast,
  EpisodeDriversData,
  IndicatorsData,
  LidoireProfileData,
  RealtimeData,
  RiskOutlook,
  VigilanceStatus,
} from '../types'
import indicatorsFixture from '../test-fixtures/indicators.json'
import driversFixture from '../test-fixtures/episode-drivers.json'
import ensembleFixture from '../test-fixtures/forecast-ensemble.json'
import realtimeFixture from '../test-fixtures/realtime.json'
import outlookFixture from '../test-fixtures/outlook.json'
import vigilanceFixture from '../test-fixtures/vigilance.json'
import profileFixture from '../test-fixtures/geo-profile.json'
import { buildTriggers, triggerProgress } from './triggers'

const inputs = {
  indicators: indicatorsFixture as unknown as IndicatorsData,
  drivers: driversFixture as unknown as EpisodeDriversData,
  ensemble: ensembleFixture as unknown as EnsembleForecast,
  realtime: realtimeFixture as unknown as RealtimeData,
  outlook: outlookFixture as unknown as RiskOutlook,
  vigilance: vigilanceFixture as unknown as VigilanceStatus,
  profile: profileFixture as unknown as LidoireProfileData,
}

describe('triggerProgress', () => {
  it('is the share of the way from base to threshold, clamped', () => {
    expect(triggerProgress(5, 10)).toBe(0.5)
    expect(triggerProgress(12, 10)).toBe(1)
    expect(triggerProgress(-1, 10)).toBe(0)
    expect(triggerProgress(6, 7, 5)).toBe(0.5)
    expect(triggerProgress(null, 10)).toBe(0)
  })

  it('uses decades for flows (log)', () => {
    expect(triggerProgress(0.1, 10, 0.01, true)).toBeCloseTo(1 / 3)
    expect(triggerProgress(0.001, 10, 0.01, true)).toBe(0)
  })
})

describe('buildTriggers on the prod payloads', () => {
  const triggers = buildTriggers(inputs)
  const byId = Object.fromEntries(triggers.map((t) => [t.id, t]))

  it('lists the score, then the runoff first among the mechanisms', () => {
    expect(triggers.map((t) => t.id)).toEqual([
      'score',
      'eyraud',
      'rain_forecast',
      'rain_observed',
      'pessac',
      'groundwater',
      'vigicrues',
    ])
  })

  it('reads every threshold from the data or the config', () => {
    expect(byId.score.threshold).toBe(35)
    expect(byId.eyraud.threshold).toBe(17.5) // /api/indicators thresholds_daily_m3s.alert
    expect(byId.rain_forecast.threshold).toBe(50) // ensemble thresholds["72h"][0]
    expect(byId.pessac.threshold).toBe(3.8) // station threshold_orange
    expect(byId.groundwater.threshold).toBe(7) // thresholds_m_ngf.watch
    expect(byId.vigicrues.thresholdText).toBe('jaune')
  })

  it('takes the lowest labelled flood without the sealed infiltration case', () => {
    // Floods: 2023-11-30 (30,9 mm), 2024-02-26 (49,8), 2025-04-21 (57,6), 2026-02-12 (56,9); 2023-03-11 excluded
    expect(byId.rain_observed.threshold).toBe(30.9)
    expect(byId.eyraud.why).toContain('27 m³/s')
  })

  it('marks the in-sample thresholds as provisional', () => {
    expect(triggers.filter((t) => t.provisional).map((t) => t.id)).toEqual(['eyraud', 'rain_observed', 'groundwater'])
  })

  it('shows how far each value is, none reached today', () => {
    expect(triggers.every((t) => !t.reached)).toBe(true)
    expect(byId.score.progress).toBeCloseTo(23.1 / 35)
    expect(byId.eyraud.valueText).toBe('0,071 m³/s')
    // log10: (log 0.071 - log 0.01) / (log 17.5 - log 0.01)
    expect(byId.eyraud.progress).toBeCloseTo(Math.log10(7.1) / Math.log10(1750))
    expect(byId.pessac.value).toBe(0.61)
    expect(byId.vigicrues.kind).toBeNull()
  })

  it('drops a trigger whose value or threshold is unknown', () => {
    const ids = buildTriggers({ ...inputs, indicators: null, vigilance: null, ensemble: null }).map((t) => t.id)
    expect(ids).toEqual(['score', 'rain_observed', 'pessac'])
    expect(buildTriggers({})).toEqual([])
  })
})
