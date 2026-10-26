import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { RiskOutlook } from '../types'
import fixture from '../test-fixtures/outlook.json'
import VerdictHero from './VerdictHero'
import LevelMeter from './ui/LevelMeter'
import ValueTag from './ui/ValueTag'

const outlook = fixture as unknown as RiskOutlook
const NOW = new Date('2026-10-26T22:40:00Z')

/** Visible text of server-rendered markup (tags dropped, entities decoded). */
function text(html: string): string {
  return html
    .replace(/<[^>]+>/g, '')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
}

describe('LevelMeter', () => {
  it('fills as many pips as the level rank and spells the level', () => {
    const html = renderToStaticMarkup(<LevelMeter level="orange" />)
    expect(html.match(/data-on=""/g)).toHaveLength(3)
    expect(html.match(/<span( data-on="")?><\/span>/g)).toHaveLength(4)
    expect(html).toContain('data-level="orange"')
    expect(text(html)).toBe('Orange (niveau 3 sur 4)')
  })
})

describe('ValueTag', () => {
  it('renders the label and the line marker of each kind', () => {
    expect(text(renderToStaticMarkup(<ValueTag kind="estimated" />))).toBe('≈ estimé')
    expect(renderToStaticMarkup(<ValueTag kind="model" />)).toContain('<i aria-hidden="true"></i>')
    expect(renderToStaticMarkup(<ValueTag kind="mockup" />)).not.toContain('<i')
    expect(text(renderToStaticMarkup(<ValueTag kind="unavailable" />))).toBe('indisponible')
  })
})

describe('VerdictHero', () => {
  const html = renderToStaticMarkup(
    <VerdictHero outlook={outlook} current={{ score: 21.5, level: 'yellow' }} now={NOW} />,
  )
  const visible = text(html)

  it('puts the one-sentence verdict first', () => {
    expect(html).toMatch(/<h2 id="verdict-title"[^>]*>Surveiller/)
    expect(visible).toContain('et revenir dans quelques heures')
    expect(visible).toContain('Maintenant : Jaune · score 22/100')
  })

  it('shows the three horizons with their max, level and Paris peak time', () => {
    for (const label of ["D'ici 6 h", "D'ici 24 h", "D'ici 72 h"]) expect(visible).toContain(label)
    expect(visible).toContain('pic demain 05:33')
    expect(visible).toContain('pic demain 11:33')
    expect(html.match(/data-level="yellow"/g)!.length).toBeGreaterThanOrEqual(4)
  })

  it('explains why with the drivers of the peak', () => {
    expect(visible).toContain('Maximum de 23/100 demain à 11:33, surtout :')
    expect(visible).toContain('Pessac-sur-Dordogne monte de')
  })

  it('flags the score as a model value without uncertainty band', () => {
    expect(visible).toContain('~ modèle')
    expect(visible).toContain('sans bande')
  })

  it('lists confidence reasons and links to the checklist', () => {
    expect(visible).toContain('Confiance')
    expect(visible).toContain('réduite')
    expect(visible).toContain('Modèle calé sur 5 crues')
    expect(html).toContain('href="#agir"')
  })

  it('offers a retry when the outlook is unavailable', () => {
    const failed = text(
      renderToStaticMarkup(<VerdictHero outlook={null} error="HTTP 500" onRetry={() => {}} now={NOW} />),
    )
    expect(failed).toContain('Prévision 6 h / 24 h / 72 h indisponible : HTTP 500.')
    expect(failed).toContain('Réessayer')
  })
})
