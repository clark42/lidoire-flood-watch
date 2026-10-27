import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { SystemStatus } from '../types'
import statusFixture from '../test-fixtures/field_status.json'
import { freshnessText, groupSources, sourceGroup, sourceLabel, statusCounts } from '../utils/sourceHealth'
import SourceHealth from './SourceHealth'

const status = statusFixture as unknown as SystemStatus
const NOW = Date.parse('2026-10-26T23:10:18Z')

function text(html: string): string {
  return html.replace(/<[^>]+>/g, '').replace(/&#x27;/g, "'").replace(/&amp;/g, '&')
}

describe('source grouping', () => {
  it('sorts every prod source into the four groups', () => {
    const groups = groupSources(status.sources, NOW)
    const keys = Object.fromEntries(groups.map((g) => [g.id, g.items.map((i) => i.key)]))
    expect(keys.critical.sort()).toEqual(['risk_score', 'river_primary'])
    expect(keys.inputs.sort()).toEqual(['infoclimat', 'netatmo', 'rain_openmeteo', 'soil', 'tide'])
    expect(keys.probabilistic.sort()).toEqual(['ensemble_rain', 'glofas', 'radar_opera'])
    expect(keys.indicators).toContain('indicator_flow_eyraud')
    expect(keys.indicators).toContain('indicator_gw_mouliets')
    expect(keys.indicators).toContain('vigilance')
    const total = groups.reduce((n, g) => n + g.items.length, 0)
    expect(total).toBe(Object.keys(status.sources!).length)
  })

  it('puts problems first inside a group', () => {
    const inputs = groupSources(status.sources, NOW).find((g) => g.id === 'inputs')!
    expect(inputs.items.slice(0, 2).map((i) => i.status)).toEqual(['unavailable', 'stale'])
    expect(statusCounts(groupSources(status.sources, NOW))).toMatchObject({ stale: 1, unavailable: 1 })
  })

  it('a critical flag wins over the key', () => {
    expect(sourceGroup('tide', { critical: true })).toBe('critical')
    expect(sourceGroup('something_new', { critical: false })).toBe('indicators')
  })

  it('writes the freshness, the future of the tide and the reason of a gap', () => {
    const s = status.sources!
    expect(freshnessText(s.river_primary!, NOW)).toBe('il y a 15 min')
    expect(freshnessText(s.infoclimat!, NOW)).toBe('il y a 10 h 10')
    expect(freshnessText(s.tide!, NOW)).toMatch(/^connue jusqu’au mar\.? 3 nov\.? 23:00$/)
    expect(freshnessText(s.netatmo!, NOW)).toBe('Netatmo n’est pas connecté')
  })

  it('restores the French accents of the backend labels', () => {
    expect(sourceLabel('Indicateur Debit Eyraud a La Force')).toBe('Débit Eyraud à La Force')
    expect(sourceLabel('Marees (PM/BM a venir)')).toBe('Marées (PM/BM à venir)')
    expect(sourceLabel('Score de risque v3.4 (temps reel)')).toBe('Score de risque v3.4 (temps réel)')
  })
})

describe('SourceHealth', () => {
  it('renders the groups with a shape and a word per status', () => {
    const html = renderToStaticMarkup(<SourceHealth sources={status.sources} now={NOW} />)
    const t = text(html)
    expect(t).toContain('Critiques')
    expect(t).toContain('Entrées du modèle')
    expect(t).toContain('Indicateurs')
    expect(t).toContain('Probabiliste')
    expect(t).toContain('en retard')
    expect(t).toContain('indisponible')
    expect(t).toMatch(/\d+ à jour · 1 en retard · 1 indisponible/)
    expect(html).toContain('<rect') // unavailable = square
    expect(html).toContain('<path d="M6,1') // stale = triangle
  })

  it('says when the status is missing', () => {
    expect(text(renderToStaticMarkup(<SourceHealth sources={null} now={NOW} loading />))).toBe(
      'Chargement de l’état des sources…',
    )
  })
})
