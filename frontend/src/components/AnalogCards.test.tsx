import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import type { EpisodeDriversData, RiskAnalogsData } from '../types'
import analogsFixture from '../test-fixtures/risk_analogs.json'
import driversFixture from '../test-fixtures/episodes_drivers.json'
import { analogDistance, analogsIntro, axisBars, floodRows, observationWindow } from '../utils/analogs'
import { AnalogCardsView, AnalogPhoto } from './AnalogCards'

// Prod (sha-1c08aad) does not serve /api/risk/analogs nor /api/episodes/drivers
// yet: these fixtures are the prototype snapshot of 2026-10-26 (prod + Hub'Eau,
// BRGM, Open-Meteo), ranked with the backend's own rank_analogs.
const analogs = analogsFixture as unknown as RiskAnalogsData
const drivers = driversFixture as unknown as EpisodeDriversData

function text(html: string): string {
  return html.replace(/<[^>]+>/g, '').replace(/&#x27;/g, "'").replace(/&amp;/g, '&')
}

describe('analog helpers', () => {
  it('reproduces the backend distance (RMS of percentile gaps)', () => {
    for (const a of analogs.analogs) {
      expect(analogDistance(analogs.situation.percentiles, a.percentiles, analogs.axes)).toBe(a.distance)
    }
    expect(analogDistance({ pessac: 10 }, { pessac: 90 }, ['pessac', 'eyraud_q'])).toBeNull()
    expect(analogDistance({ pessac: 10, eyraud_q: 20 }, { pessac: 10, eyraud_q: 20 }, ['pessac', 'eyraud_q'])).toBe(0)
  })

  it('pairs today and the episode on every axis', () => {
    const bars = axisBars(analogs.analogs[0], analogs)
    expect(bars.map((b) => b.label)).toEqual(['Pessac', 'Eyraud', 'Pluie 72 h', 'Nappe'])
    expect(bars[0].today).toBe(analogs.situation.percentiles.pessac)
    expect(bars[0].episode).toBe(analogs.analogs[0].percentiles.pessac)
  })

  it('says plainly when no episode is close', () => {
    const intro = analogsIntro(analogs)
    expect(intro.nothingClose).toBe(true)
    expect(intro.headline).toBe('Aucun épisode passé ne ressemble à aujourd’hui.')
    expect(intro.detail).toContain('59 points de percentile (seuil 30')
    const close = analogsIntro({ ...analogs, nothing_close: false, nearest_distance: 12.4 })
    expect(close.nothingClose).toBe(false)
    expect(close.headline).toContain('12 points')
    expect(analogsIntro({ ...analogs, nearest_distance: null }).headline).toContain('impossible')
  })

  it('lists the labelled floods, newest first, with their distance to today', () => {
    const rows = floodRows(drivers, analogs.situation, analogs.axes)
    expect(rows.map((r) => r.key)).toEqual(['2025-04-23', '2024-02-26', '2023-12-11', '2023-12-01', '2023-03-11'])
    expect(rows.every((r) => r.distance != null && r.distance > 30)).toBe(true)
    expect(rows.find((r) => r.key === '2023-03-11')!.notes).toEqual(['infiltration, colmatée depuis'])
    expect(rows.find((r) => r.key === '2023-12-11')!.notes).toEqual(['peut-être le même épisode que le 01/12/2023'])
    expect(rows.find((r) => r.key === '2024-02-26')!.notes).toEqual([])
  })

  it('opens the observation window around the peak', () => {
    expect(observationWindow('2024-02-26T12:00:00Z', 36)).toEqual({
      start: '2024-02-25T00:00:00.000Z',
      end: '2024-02-28T00:00:00.000Z',
    })
    expect(observationWindow('nope', 36)).toBeNull()
  })
})

describe('AnalogCardsView', () => {
  it('renders the honest empty state, three cards and the flood table', () => {
    const html = renderToStaticMarkup(<AnalogCardsView analogs={analogs} drivers={drivers} />)
    const t = text(html)
    expect(t).toContain('Aucun épisode passé ne ressemble à aujourd’hui.')
    expect(html.match(/<article/g)).toHaveLength(4)
    expect(t).toContain('27 novembre 2024')
    expect(t).toContain('Pour comparaison : les crues labellisées')
    expect(t).toContain('Aujourd’hui')
    expect(t).toContain('5 crues seulement')
    // Written values double the bars
    expect(t).toMatch(/p\d+ à l’épisode, p14 aujourd’hui/)
  })

  it('degrades when the drivers or the analogs are missing', () => {
    const noDrivers = text(renderToStaticMarkup(<AnalogCardsView analogs={analogs} drivers={null} driversError="Erreur API 422" />))
    expect(noDrivers).toContain('Tableau des crues indisponible : Erreur API 422')
    const none = text(renderToStaticMarkup(<AnalogCardsView analogs={null} analogsError="Erreur API 404" drivers={null} />))
    expect(none).toBe('Analogues indisponibles : Erreur API 404')
  })
})

describe('AnalogPhoto', () => {
  const analog = analogs.analogs[0]

  it('shows an empty slot when the episode has no photo', () => {
    const html = renderToStaticMarkup(<AnalogPhoto analog={analog} windowHours={36} />)
    expect(text(html)).toContain('Pas encore de photo de cet épisode')
    expect(html).not.toContain('<img')
  })

  it('asks for the API key before fetching a private photo', () => {
    const withPhoto = { ...analog, observations: { count: 2, with_photo: 1 } }
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <AnalogPhoto analog={withPhoto} windowHours={36} />
      </MemoryRouter>,
    )
    expect(text(html)).toContain('1 photo privée : clé API requise')
    expect(html).toContain('href="/config#api-key"')
  })
})
