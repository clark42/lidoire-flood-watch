// Server-rendered markup of the "why" panels on recorded prod payloads
// (src/test-fixtures, GET http://lidoire.ts.kamorion.cloud/api/... on
// 2026-10-26 ~23:10 UTC; episode-drivers and geo-profile built by the real
// backend code, see their _fixture_note: prod did not serve them yet).
import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type {
  EnsembleForecast,
  EpisodeDriversData,
  ForecastData,
  IndicatorsData,
  LidoireProfileData,
  RealtimeData,
  RiskOutlook,
  SystemStatus,
  VigilanceStatus,
} from '../types'
import indicatorsFixture from '../test-fixtures/indicators.json'
import driversFixture from '../test-fixtures/episode-drivers.json'
import ensembleFixture from '../test-fixtures/forecast-ensemble.json'
import realtimeFixture from '../test-fixtures/realtime.json'
import outlookFixture from '../test-fixtures/outlook.json'
import profileFixture from '../test-fixtures/geo-profile.json'
import forecastFixture from '../test-fixtures/risk-forecast.json'
import vigilanceFixture from '../test-fixtures/vigilance.json'
import statusFixture from '../test-fixtures/status.json'
import { MechanismGrid } from './MechanismCards'
import { HorizonRibbonView } from './HorizonRibbon'
import { TriggerListView } from './TriggerList'
import { VigilancePanel } from './VigilanceBadges'
import ConfidencePanel from './ConfidencePanel'
import ActionChecklist from './ActionChecklist'
import VerdictHero from './VerdictHero'
import PercentileRail from './ui/PercentileRail'

const NOW = new Date('2026-10-26T23:10:00Z')
const indicators = indicatorsFixture as unknown as IndicatorsData
const drivers = driversFixture as unknown as EpisodeDriversData
const ensemble = ensembleFixture as unknown as EnsembleForecast
const realtime = realtimeFixture as unknown as RealtimeData
const outlook = outlookFixture as unknown as RiskOutlook
const profile = profileFixture as unknown as LidoireProfileData
const forecast = forecastFixture as unknown as ForecastData
const vigilance = vigilanceFixture as unknown as VigilanceStatus
const sources = (statusFixture as unknown as SystemStatus).sources

/** Visible text of server-rendered markup (tags dropped, entities decoded). */
function text(html: string): string {
  return html
    .replace(/<title>[^<]*<\/title>/g, '')
    .replace(/<[^>]+>/g, '')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
}

describe('MechanismGrid', () => {
  const html = renderToStaticMarkup(
    <MechanismGrid
      indicators={indicators}
      drivers={drivers}
      profile={profile}
      realtime={realtime}
      outlook={outlook}
      ensemble={ensemble}
      now={NOW}
    />,
  )
  const visible = text(html)

  it('renders the four mechanisms, Lidoire runoff first', () => {
    const order = [...html.matchAll(/data-mechanism="([a-z]+)"/g)].map((m) => m[1])
    expect(order).toEqual(['runoff', 'backwater', 'overflow', 'groundwater'])
    expect(visible).toContain('Crue propre de la Lidoire (ruissellement)')
    expect(visible).toContain('Dordogne aval (niveau relevé, remous)')
    expect(visible).toContain('Débordement direct de la Dordogne')
    expect(visible).toContain('Nappe alluviale')
  })

  it('tags every value honestly', () => {
    expect(html).toMatch(/data-mechanism="runoff"[\s\S]*?data-kind="analog"/)
    expect(visible).toContain('≈ estimé')
    expect(visible).toContain('~ modèle')
    expect(visible).toContain('sans bande')
  })

  it('says where the backwater stops and shows the unknown overflow threshold as a band', () => {
    expect(visible).toContain("Le remous s'arrête à ≈ 4,9 km sous le moulin à 00:10 (eau connectée).")
    expect(visible).toContain('seuil inconnu')
    expect(html).toContain('url(#rail-hatch-')
  })

  it('draws the past episodes by shape and states with a shape and a word', () => {
    expect(html.match(/fill="var\(--silt\)" stroke="var\(--surface\)"/g)!.length).toBeGreaterThanOrEqual(5 * 4)
    expect(html.match(/data-state="calm"/g)).toHaveLength(4)
    expect(visible).toContain('Calme')
    expect(visible).toContain('Sous les 5 crues labellisées (la plus faible : 27 m³/s).')
  })

  it('names the sources that failed', () => {
    const failed = text(renderToStaticMarkup(<MechanismGrid indicators={null} drivers={null} profile={null} realtime={null} outlook={null} ensemble={null} missing={['valeurs aux crues']} now={NOW} />))
    expect(failed).toContain("Indisponible pour l'instant : valeurs aux crues.")
    expect(failed).toContain('Inconnu')
  })
})

describe('PercentileRail', () => {
  it('labels the svg and writes the value now', () => {
    const html = renderToStaticMarkup(
      <PercentileRail
        spec={{
          domain: [0, 10],
          value: 4,
          valueLabel: 'maint. 4',
          valueKind: 'measured',
          episodes: [{ key: '2024-02-26', value: 6, outcome: 'flooded', title: '26/02/2024 · inondé' }],
          ticks: [{ value: 7, label: 'alerte', provisional: true }],
          ariaLabel: 'Test rail',
        }}
      />,
    )
    expect(html).toContain('role="img"')
    expect(html).toContain('aria-label="Test rail"')
    expect(text(html)).toContain('maint. 4')
    expect(text(html)).toContain('alerte*')
    expect(html).toContain('<title>26/02/2024 · inondé</title>')
  })
})

describe('HorizonRibbonView', () => {
  const html = renderToStaticMarkup(
    <HorizonRibbonView forecast={forecast} ensemble={ensemble} realtime={realtime} now={NOW} initialWidth={760} />,
  )
  const visible = text(html)

  it('draws the four rows on one Paris-time axis with nights', () => {
    for (const label of ['Score v3.4', 'Pluie mm / 24 h', 'Pleines mers', 'Pessac m']) expect(visible).toContain(label)
    expect(visible).toContain('mar. 27')
    expect(html.match(/fill="var\(--night\)"/g)).toHaveLength(4)
  })

  it('flags the forecasts without band and the estimated tides', () => {
    expect(visible).toContain('~ modèle, sans bande')
    expect(visible).toContain('~ projection, sans bande')
    expect(visible).toContain('≈91')
    expect(visible).toContain('Vigicrues Libourne 4,76 m [4,58–4,94]')
  })

  it('offers the model and horizon switches, ECMWF by default', () => {
    expect(html).toMatch(/aria-pressed="true"[^>]*>72 h</)
    expect(html).toMatch(/aria-pressed="true"[^>]*>ECMWF</)
    expect(visible).toContain('~ ensemble ECMWF IFS ENS, 51 membres')
  })

  it('writes the values at the cursor (score peak by default)', () => {
    expect(visible).toContain('(pic du score)')
    expect(visible).toMatch(/Score ~ 23\/100 \(Jaune\)/)
  })

  it('hatches the rain beyond the ICON-D2 horizon', () => {
    const d2 = renderToStaticMarkup(
      <HorizonRibbonView forecast={forecast} ensemble={ensemble} realtime={realtime} now={NOW} initialModel="icon_d2" initialWidth={760} />,
    )
    expect(text(d2)).toContain('au-delà de ICON-D2')
  })

  it('says when the score forecast is missing', () => {
    expect(text(renderToStaticMarkup(<HorizonRibbonView forecast={null} ensemble={null} realtime={null} now={NOW} />))).toContain(
      'Prévision du score indisponible.',
    )
  })
})

describe('TriggerListView', () => {
  it('lists the thresholds with their distance, provisional ones marked', () => {
    const html = renderToStaticMarkup(
      <TriggerListView
        indicators={indicators}
        drivers={drivers}
        ensemble={ensemble}
        realtime={realtime}
        outlook={outlook}
        vigilance={vigilance}
        profile={profile}
      />,
    )
    const visible = text(html)
    expect(visible).toContain("Ce qui me ferait changer d'avis")
    expect(html.match(/data-trigger=/g)).toHaveLength(7)
    expect(visible.match(/provisoire/g)!.length).toBeGreaterThanOrEqual(3)
    expect(visible).toContain('seuil 17,5 m³/s')
    expect(html).toContain('role="meter"')
  })
})

describe('VigilancePanel', () => {
  it('shows official levels as pips and words, never colour alone', () => {
    const html = renderToStaticMarkup(<VigilancePanel data={vigilance} now={NOW} />)
    const visible = text(html)
    expect(visible).toContain('Vigilances officielles')
    expect(visible).toContain('Dordogne aval')
    expect(visible).toContain('Gironde')
    expect(html.match(/data-level="green"/g)).toHaveLength(4)
    expect(visible).toContain('Vert')
    expect(visible).toContain('hors score')
  })

  it('shows the issue time of each service (UAT: only the Vigicrues time was shown)', () => {
    const later = structuredClone(vigilance)
    later.meteofrance.issued_at = '2026-10-27T05:00:00Z'
    const html = renderToStaticMarkup(<VigilancePanel data={later} now={new Date('2026-10-27T08:00:00Z')} />)
    const issued = text(html.match(/data-testid="vigilance-issued">([^<]*)</)![1])
    expect(issued).toBe("Vigicrues hier à 15:56 · Météo-France aujourd'hui à 06:00")
  })

  it('reports an unavailable feed', () => {
    expect(text(renderToStaticMarkup(<VigilancePanel data={null} failed />))).toContain('Vigilance officielle indisponible.')
    expect(renderToStaticMarkup(<VigilancePanel data={null} />)).toBe('')
  })
})

describe('ConfidencePanel', () => {
  it('adds the agreement of the rain ensembles to the reasons', () => {
    const visible = text(
      renderToStaticMarkup(<ConfidencePanel confidence={outlook.confidence} sources={sources} ensemble={ensemble} now={NOW} />),
    )
    expect(visible).toContain('Confiance')
    expect(visible).toContain("Ensembles d'accord (2 modèles)")
    expect(visible).toContain('Modèle calé sur 5 crues')
  })

  it('says nothing about the ensembles while they load, and flags them when absent', () => {
    const loading = text(renderToStaticMarkup(<ConfidencePanel confidence={outlook.confidence} now={NOW} />))
    expect(loading).not.toContain('ensemble')
    const absent = text(renderToStaticMarkup(<ConfidencePanel confidence={outlook.confidence} ensemble={null} now={NOW} />))
    expect(absent).toContain("Pluie d'ensemble indisponible")
  })

  it('is the confidence block of the VerdictHero', () => {
    const visible = text(renderToStaticMarkup(<VerdictHero outlook={outlook} ensemble={ensemble} now={NOW} />))
    expect(visible).toContain("Ensembles d'accord")
  })
})

describe('ActionChecklist', () => {
  it('shows the level, the deadline and marks the default texts as examples', () => {
    const html = renderToStaticMarkup(<ActionChecklist checklist={outlook.checklist} now={NOW} />)
    const visible = text(html)
    expect(visible).toContain('Liste du moulin')
    expect(visible).toContain('Vigilance : surveiller')
    expect(visible).toContain('À faire maintenant')
    expect(visible).toContain('0/4 fait')
    expect(html.match(/type="checkbox"/g)).toHaveLength(4)
    expect(visible).toContain('exemples')
  })

  it('has nothing to do in green', () => {
    const visible = text(
      renderToStaticMarkup(
        <ActionChecklist checklist={{ level: 'green', title: 'Rien à signaler', actions: [], act_before: null }} now={NOW} />,
      ),
    )
    expect(visible).toContain('Rien à faire sur 72 h.')
  })
})
