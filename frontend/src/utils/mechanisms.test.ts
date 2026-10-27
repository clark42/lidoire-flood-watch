import { describe, expect, it } from 'vitest'
import type {
  EnsembleForecast,
  EpisodeDriversData,
  IndicatorsData,
  LidoireProfileData,
  RealtimeData,
  RiskOutlook,
} from '../types'
import indicatorsFixture from '../test-fixtures/indicators.json'
import driversFixture from '../test-fixtures/episode-drivers.json'
import ensembleFixture from '../test-fixtures/forecast-ensemble.json'
import realtimeFixture from '../test-fixtures/realtime.json'
import outlookFixture from '../test-fixtures/outlook.json'
import profileFixture from '../test-fixtures/geo-profile.json'
import profileFeb2026 from '../test-fixtures/geo-profile-2026-02-12.json'
import { buildMechanisms, pessacAtMillGround, pessacEnvelope, railEpisodes, type MechanismInputs } from './mechanisms'
import { ensembleAgreement } from './ensemble'

const NOW = new Date('2026-10-26T23:10:00Z')
const inputs: MechanismInputs = {
  indicators: indicatorsFixture as unknown as IndicatorsData,
  drivers: driversFixture as unknown as EpisodeDriversData,
  ensemble: ensembleFixture as unknown as EnsembleForecast,
  realtime: realtimeFixture as unknown as RealtimeData,
  outlook: outlookFixture as unknown as RiskOutlook,
  profile: profileFixture as unknown as LidoireProfileData,
  now: NOW,
}

describe('buildMechanisms', () => {
  const [runoff, backwater, overflow, groundwater] = buildMechanisms(inputs)

  it('puts the Lidoire runoff first and the Dordogne as a modulating factor', () => {
    expect(buildMechanisms(inputs).map((m) => m.id)).toEqual(['runoff', 'backwater', 'overflow', 'groundwater'])
    expect(runoff.role).toBe('Mécanisme principal')
    expect(backwater.role).toBe('Modulateur')
    expect(overflow.role).toBe('Rare, seuil inconnu')
  })

  it('runoff: Eyraud now as an analog, below every labelled flood', () => {
    expect(runoff.valueText).toBe('0,066')
    expect(runoff.kind).toBe('analog')
    expect(runoff.state).toBe('calm')
    const [q, rain] = runoff.rails
    expect(q.spec.log).toBe(true)
    expect(q.spec.episodes).toHaveLength(15)
    expect(q.comparison).toBe('Sous les 5 crues labellisées (la plus faible : 27 m³/s).')
    expect(q.spec.ticks.map((t) => [t.label, t.value, !!t.provisional])).toEqual([
      ['p99', 13.1, false],
      ['alerte', 17.5, true],
    ])
    // Observed rain on the rail, forecast as the dotted marker
    expect(rain.spec.value).toBe(9.1)
    expect(rain.spec.marker?.value).toBe(0)
    expect(runoff.facts.map((f) => f.label)).toContain('Ensemble ECMWF IFS ENS, 72 h')
  })

  it('backwater: Pessac 12.5 h max and the reach of the backwater below the mill', () => {
    expect(backwater.value).toBe(0.61)
    expect(backwater.state).toBe('calm')
    expect(backwater.lead).toContain("Le remous s'arrête à ≈ 4,9 km sous le moulin à 00:10 (eau connectée).")
    expect(backwater.facts.find((f) => f.label === 'Niveau à la confluence Z_c')).toMatchObject({
      value: '≈ 3,84 m NGF',
      kind: 'estimated',
    })
    const atMill = backwater.rails[0].spec.ticks.find((t) => t.kind === 'estimated')
    expect(atMill?.value).toBeCloseTo(8.67)
  })

  it('backwater: says when the reach is unknown (profile not served)', () => {
    const [, bw] = buildMechanisms({ ...inputs, profile: null })
    expect(bw.value).toBe(0.61) // falls back on the outlook's H
    expect(bw.lead).toContain('Portée du remous indisponible')
  })

  it('backwater: February 2026 stops ~2.7 km below the mill', () => {
    const [, bw] = buildMechanisms({ ...inputs, profile: profileFeb2026 as unknown as LidoireProfileData })
    expect(bw.value).toBe(6.53)
    expect(bw.state).toBe('active')
    expect(bw.lead).toContain('≈ 2,7 km sous le moulin')
  })

  it('overflow: an unknown threshold drawn as a band, never a line', () => {
    const band = overflow.rails[0].spec.band
    expect(band?.label).toBe('seuil inconnu')
    expect(band?.from).toBe(5.195) // highest Pessac with the mill dry (2023-12-11)
    expect(band?.to).toBeCloseTo(8.67) // mill ground 12.0 m NGF - Pessac zero 3.33
    expect(overflow.valueText).toBe('0,44')
    expect(overflow.state).toBe('calm')
  })

  it('groundwater: measured level and its provisional threshold', () => {
    expect(groundwater.valueText).toBe('5,84')
    expect(groundwater.kind).toBe('measured')
    expect(groundwater.state).toBe('calm')
    expect(groundwater.rails[0].spec.ticks[0]).toMatchObject({ value: 7, provisional: true })
    expect(groundwater.rails[0].spec.domain[0]).toBeLessThanOrEqual(5.5)
  })

  it('degrades to "unknown" without any payload', () => {
    const views = buildMechanisms({ now: NOW })
    expect(views.map((v) => v.state)).toEqual(['unknown', 'unknown', 'unknown', 'unknown'])
    expect(views.every((v) => v.valueText === '–' && v.kind === 'unavailable')).toBe(true)
  })
})

describe('mechanism helpers', () => {
  it('turns drivers into rail dots, flagging the infiltration flood', () => {
    const dots = railEpisodes(inputs.drivers, (v) => v.eyraud_q_m3s, (v) => `${v}`)
    expect(dots.find((d) => d.key === '2023-03-11')?.title).toContain('(infiltration)')
    expect(dots.find((d) => d.key === '2024-05-09')?.outcome).toBe('false_positive')
  })

  it('reads Pessac max 12.5 h from the profile, else from the outlook', () => {
    expect(pessacEnvelope(inputs.profile, null)).toBe(0.61)
    expect(pessacEnvelope(null, inputs.outlook)).toBe(0.61)
    expect(pessacEnvelope(null, null)).toBeNull()
    expect(pessacAtMillGround(inputs)).toBeCloseTo(12.0 - 3.33)
  })
})

describe('ensembleAgreement', () => {
  const ens = inputs.ensemble as EnsembleForecast

  it('reports agreement of the models over 72 h on the prod payload', () => {
    const a = ensembleAgreement(ens, NOW.getTime())
    expect(a.kind).toBe('ok')
    expect(a.lead).toBe("Ensembles d'accord (2 modèles)")
    expect(a.text).toContain('au-dessus de 50 mm : ECMWF 0 %, ICON-EU 0 %')
    expect(a.text).toContain('91 membres')
  })

  it('flags a disagreement between models', () => {
    const w = structuredClone(ens)
    w.rain.horizons!['72h'][0].models.icon_eu.prob['50'] = 0.6
    const a = ensembleAgreement(w, NOW.getTime())
    expect(a.kind).toBe('warn')
    expect(a.lead).toBe('Ensembles en désaccord')
    expect(a.text).toContain('ICON-EU 60 %')
  })

  it('flags a late ensemble and an absent one', () => {
    const late = ensembleAgreement(ens, Date.parse('2026-10-27T14:00:00Z'))
    expect(late.kind).toBe('warn')
    expect(late.text).toContain('Ensembles relevés il y a 15 h')
    expect(ensembleAgreement(null).kind).toBe('info')
  })
})
