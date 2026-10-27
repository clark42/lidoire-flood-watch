import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { DASHBOARD_SECTIONS } from '../pages/dashboardSections'
import { initialSectionOpen } from '../utils/sectionDisclosure'
import DashboardSection from './DashboardSection'

describe('collapsible sections (mobile page length)', () => {
  it('collapses only the long secondary sections, never the decision, the why or the actions', () => {
    const collapsible = DASHBOARD_SECTIONS.filter((s) => 'collapsible' in s && s.collapsible).map((s) => s.id)
    expect(collapsible).toEqual(['replay', 'plan', 'analogues', 'carte', 'pluie-sol', 'fiabilite'])
    for (const id of ['maintenant', 'pourquoi', 'agir']) expect(collapsible).not.toContain(id)
  })

  it('starts collapsed under 640 px and open on desktop; a deep link opens its section', () => {
    expect(initialSectionOpen('replay', { wide: false, hash: '' })).toBe(false)
    expect(initialSectionOpen('replay', { wide: true, hash: '' })).toBe(true)
    expect(initialSectionOpen('replay', { wide: false, hash: '#replay' })).toBe(true)
    expect(initialSectionOpen('plan', { wide: false, hash: '#replay' })).toBe(false)
  })

  it('renders an accessible disclosure: heading button, aria-expanded, aria-controls, hidden content', () => {
    const closed = renderToStaticMarkup(
      <DashboardSection id="replay" defaultOpen={false}>
        <p>profil</p>
      </DashboardSection>,
    )
    expect(closed).toMatch(/<h2 id="replay-title"[^>]*><button type="button" aria-expanded="false" aria-controls="replay-content"/)
    expect(closed).toMatch(/<div id="replay-content" hidden=""/)
    expect(closed).toContain('Afficher')
    // The anchor and the heading stay in the page: #replay still lands on the section
    expect(closed).toMatch(/<section id="replay"/)
    expect(closed).toContain('De la confluence au moulin')

    const open = renderToStaticMarkup(
      <DashboardSection id="replay" defaultOpen>
        <p>profil</p>
      </DashboardSection>,
    )
    expect(open).toContain('aria-expanded="true"')
    expect(open).not.toContain('hidden=""')
    expect(open).toContain('Replier')
  })

  it('keeps the other sections as plain headings, always open', () => {
    const html = renderToStaticMarkup(
      <DashboardSection id="agir" defaultOpen={false}>
        <p>liste</p>
      </DashboardSection>,
    )
    expect(html).not.toContain('aria-expanded')
    expect(html).not.toContain('hidden=""')
  })

  it('renders open on the server (no window): nothing hidden without the browser', () => {
    const html = renderToStaticMarkup(
      <DashboardSection id="plan">
        <p>plan</p>
      </DashboardSection>,
    )
    expect(html).toContain('aria-expanded="true"')
  })
})
