import { describe, expect, it } from 'vitest'
import { DASHBOARD_SECTIONS } from './dashboardSections'

describe('Dashboard sections', () => {
  it('follows the reading order of the spec: decide, why, next hours, act', () => {
    expect(DASHBOARD_SECTIONS.slice(0, 4).map((s) => s.id)).toEqual([
      'maintenant',
      'pourquoi',
      'prochaines-heures',
      'agir',
    ])
    expect(DASHBOARD_SECTIONS[DASHBOARD_SECTIONS.length - 1].id).toBe('fiabilite')
  })

  it('has unique anchor ids usable in a URL hash', () => {
    const ids = DASHBOARD_SECTIONS.map((s) => s.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const id of ids) expect(id).toMatch(/^[a-z][a-z0-9-]*$/)
  })

  it('keeps the bar labels short enough for a phone', () => {
    for (const s of DASHBOARD_SECTIONS) expect(s.label.length).toBeLessThanOrEqual(14)
  })
})
