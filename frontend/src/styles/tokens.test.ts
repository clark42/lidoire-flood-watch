import { describe, expect, it } from 'vitest'
import tokensCss from './tokens.css?raw'
import { contrastRatio } from '../theme/contrast'

type Tokens = Record<string, string>

function block(selector: RegExp): Tokens {
  const match = selector.exec(tokensCss)
  if (!match) throw new Error(`Block not found: ${selector}`)
  const tokens: Tokens = {}
  for (const decl of match[1].matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) tokens[decl[1]] = decl[2].trim()
  return tokens
}

const light = block(/^:root\s*\{([^}]*)\}/m)
const darkMedia = block(/:root:not\(\[data-theme='light'\]\)\s*\{([^}]*)\}/)
const darkForced = block(/:root\[data-theme='dark'\]\s*\{([^}]*)\}/)

const THEMES: [string, Tokens][] = [
  ['light', light],
  ['dark', darkForced],
]

// Text colour on background colour: WCAG AA for normal text (4.5:1)
const TEXT_PAIRS: [string, string][] = [
  ...['ink', 'ink-2', 'ink-3'].flatMap((fg) =>
    ['bg', 'surface', 'surface-2'].map((bg): [string, string] => [fg, bg]),
  ),
  ['ink', 'surface-3'],
  ['ink-2', 'surface-3'],
  ['accent', 'bg'],
  ['accent', 'surface'],
  ['accent-ink', 'accent'],
  ['ink', 'accent-soft'],
  ['ink', 'lv-green-soft'],
  ['ink', 'lv-yellow-soft'],
  ['ink', 'lv-orange-soft'],
  ['ink', 'lv-red-soft'],
  ['silt', 'silt-soft'],
  ['silt', 'surface'],
  // Inverted tooltip (InfoTooltip)
  ['bg', 'ink'],
  // Legacy Tailwind greys used as text in the panels not yet redesigned
  ...['gray-400', 'gray-500', 'gray-600', 'gray-700', 'gray-900'].flatMap((fg) =>
    ['surface', 'gray-50'].map((bg): [string, string] => [fg, bg]),
  ),
]

// Non-text marks that carry meaning on their own: focus ring (3:1)
const MARK_PAIRS: [string, string][] = [
  ['accent', 'bg'],
  ['accent', 'surface'],
]

function color(tokens: Tokens, name: string): string {
  const value = tokens[name]
  if (!value) throw new Error(`Missing token --${name}`)
  return value
}

describe('theme tokens', () => {
  it('defines the same tokens in light and dark', () => {
    expect(Object.keys(darkForced).sort()).toEqual(
      Object.keys(light).filter((k) => !k.startsWith('f-') && k !== 'radius').sort(),
    )
  })

  it('keeps the prefers-color-scheme dark block identical to [data-theme=dark]', () => {
    expect(darkMedia).toEqual(darkForced)
  })

  it('keeps --surface-rgb in sync with --surface', () => {
    for (const [, tokens] of THEMES) {
      const hex = tokens['surface'].replace('#', '')
      const channels = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16)).join(' ')
      expect(tokens['surface-rgb']).toBe(channels)
    }
  })

  for (const [theme, tokens] of THEMES) {
    it.each(TEXT_PAIRS)(`${theme}: text --%s on --%s meets WCAG AA (4.5:1)`, (fg, bg) => {
      expect(contrastRatio(color(tokens, fg), color(tokens, bg))).toBeGreaterThanOrEqual(4.5)
    })

    it.each(MARK_PAIRS)(`${theme}: mark --%s on --%s meets 3:1`, (fg, bg) => {
      expect(contrastRatio(color(tokens, fg), color(tokens, bg))).toBeGreaterThanOrEqual(3)
    })
  }
})
