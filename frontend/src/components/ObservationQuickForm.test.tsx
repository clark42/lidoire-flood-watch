import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import ObservationQuickForm from './ObservationQuickForm'

describe('ObservationQuickForm', () => {
  it('opens on three large kind buttons and the near-miss choice', () => {
    const html = renderToStaticMarkup(<ObservationQuickForm />)
    expect(html.match(/aria-pressed="false"/g)).toHaveLength(4)
    expect(html).toContain('min-h-[76px]')
    expect(html).toContain('Inondé')
    expect(html).toContain('RAS')
    expect(html).toContain('Presque inondé')
    expect(html).not.toContain('type="file"')
  })

  it('offers the camera (capture) and the gallery once a kind is chosen', () => {
    const html = renderToStaticMarkup(<ObservationQuickForm initialKind="flooded" />)
    expect(html).toContain('aria-pressed="true"')
    const inputs = html.match(/<input[^>]*type="file"[^>]*>/g) ?? []
    expect(inputs).toHaveLength(2)
    expect(inputs.every((i) => i.includes('accept="image/*"'))).toBe(true)
    expect(inputs.filter((i) => i.includes('capture="environment"'))).toHaveLength(1)
    expect(html).toContain('Prendre une photo')
    expect(html).toContain('Choisir une photo')
    expect(html).toContain('type="datetime-local"')
    expect(html).toContain('Enregistrer')
  })
})
