import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { EpisodeDriversData, UnifiedTimelineData } from '../types'
import driversFixture from '../test-fixtures/episode-drivers-plan.json'
import trailFeb2026 from '../test-fixtures/timeline-trail-2026-02.json'
import trailNow from '../test-fixtures/timeline-trail-now.json'
import { dailyTrail, episodePoints } from '../utils/phasePlane'
import FloodPhasePlane, { FloodPhaseLegend, FloodPhaseTable } from './FloodPhasePlane'
import FloodPlanPanel from './FloodPlanPanel'

const drivers = driversFixture as unknown as EpisodeDriversData
const episodes = episodePoints(drivers, { '2023-11-30': 20.6, '2026-02-12': 27.4 })
const nowTrail = dailyTrail(trailNow as unknown as UnifiedTimelineData)
const febTrail = dailyTrail(trailFeb2026 as unknown as UnifiedTimelineData)
const today = { t: Date.parse('2026-10-26T21:00:00Z'), pessac: 0.61, q: 0.08, qBasis: 'envelope' as const }
const trails = [
  { id: 'now', label: '21 derniers jours', color: 'var(--ink)', points: nowTrail },
  { id: 'feb2026', label: 'fév. 2026', color: 'var(--silt)', points: febTrail, animate: true },
]

function text(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ')
}

describe('FloodPhasePlane (recorded drivers)', () => {
  const html = renderToStaticMarkup(<FloodPhasePlane episodes={episodes} today={today} trails={trails} width={640} />)

  it('places every labelled episode with a shape per outcome', () => {
    expect(html.match(/data-episode="/g)).toHaveLength(drivers.episodes.length)
    expect(html.match(/data-outcome="flooded"/g)).toHaveLength(5)
    expect(html.match(/data-outcome="near_miss"/g)).toHaveLength(3)
    expect(html.match(/data-outcome="false_positive"/g)).toHaveLength(6)
    expect(html.match(/data-outcome="unknown"/g)).toHaveLength(1)
    // Daily fallback of the two floods without an hourly Eyraud point
    expect(html.match(/data-q-basis="daily"/g)).toHaveLength(2)
  })

  it('shows today, the current trail and an animated February trail', () => {
    expect(html).toContain('data-today=""')
    expect(text(html)).toContain('aujourd’hui')
    expect(html).toContain('data-trail="now"')
    expect(html).toContain('data-trail="feb2026"')
    expect(html.match(/class="phase-trail-draw"/g)).toHaveLength(1)
    expect(text(html)).toContain('fév. 2026')
  })

  it('shades the discharge of the labelled floods and keeps the reference lines', () => {
    expect(html).toMatch(/data-flood-zone="19\.6/)
    expect(text(html)).toContain('3,0 m : seuil jaune de Pessac')
    expect(text(html)).toContain('Q2 ≈ 18,9')
    expect(text(html)).toContain('Eyraud, débit max (m³/s, échelle log)')
  })

  it('fits a phone column', () => {
    const phone = renderToStaticMarkup(<FloodPhasePlane episodes={episodes} today={today} trails={trails} width={343} />)
    expect(phone).toContain('width="343"')
    expect(text(phone)).toContain('crues')
  })
})

describe('FloodPhaseLegend and table', () => {
  it('names the four outcome shapes, the daily basis and the trails', () => {
    const t = text(renderToStaticMarkup(<FloodPhaseLegend trails={trails} />))
    for (const s of ['inondé', 'presque', 'pas inondé', 'non labellisé, à annoter', '≈ débit journalier', 'trace fév. 2026']) {
      expect(t).toContain(s)
    }
  })

  it('doubles the tooltips with a table of the values', () => {
    const t = text(renderToStaticMarkup(<FloodPhaseTable episodes={episodes} />))
    expect(t).toContain('Valeurs des 15 épisodes')
    expect(t).toContain('≈ 27 (jour)')
    expect(t).toContain('26/02/2024 12:00')
  })
})

describe('FloodPlanPanel', () => {
  it('renders the trail toggles before any data (server render, no fetch)', () => {
    const t = text(renderToStaticMarkup(<FloodPlanPanel />))
    expect(t).toContain('Trace actuelle')
    expect(t).toContain('Trace fév. 2024')
  })
})
