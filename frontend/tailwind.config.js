import colors from 'tailwindcss/colors'
import plugin from 'tailwindcss/plugin'

// Theme tokens (src/styles/tokens.css) exposed as Tailwind colours.
// Opacity modifiers (bg-surface/50) do not apply to these var() colours.
const token = (name) => `var(--${name})`

// ---------------------------------------------------------------------------
// Dark compatibility of the panels not yet redesigned
//
// gray/slate follow the token neutrals (--gray-* channels) and bg-white is
// the surface, in both themes. In the dark theme, chromatic tints are
// remapped: pale backgrounds (50-200) and borders (100-300) become deep,
// dark text (400-950) becomes light. Solid colours (bg-*-300 and above) and
// text-white are untouched. Remove once every panel uses the tokens.
// ---------------------------------------------------------------------------
// Palettes used by the panels (lime, pink, rose, fuchsia are not)
const CHROMATIC = [
  'red', 'orange', 'amber', 'yellow', 'green', 'emerald', 'teal', 'cyan',
  'sky', 'blue', 'indigo', 'violet', 'purple',
]
const DARK_SURFACE = [20, 31, 35] // --surface of the dark theme
// shade -> [palette shade used in dark, weight of that shade over the surface]
const DARK_BG = { 50: [950, 0.55], 100: [900, 0.6], 200: [800, 0.65] }
const DARK_BORDER = { 100: [900, 0.7], 200: [800, 0.7], 300: [700, 0.75] }
const DARK_TEXT = { 400: 300, 500: 300, 600: 400, 700: 300, 800: 200, 900: 100, 950: 50 }

const channels = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16))
const over = (hex, weight) =>
  channels(hex).map((c, i) => Math.round(c * weight + DARK_SURFACE[i] * (1 - weight)))
const rgbVar = (name) => `rgb(var(--${name}) / <alpha-value>)`

const GRAY = Object.fromEntries(
  [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map((s) => [s, rgbVar(`gray-${s}`)]),
)

function legacyPalette(kind, shades) {
  return Object.fromEntries(
    CHROMATIC.map((name) => [
      name,
      Object.fromEntries(Object.keys(shades).map((s) => [s, rgbVar(`legacy-${kind}-${name}-${s}`)])),
    ]),
  )
}

function legacyVars(dark) {
  const vars = {}
  for (const name of CHROMATIC) {
    const p = colors[name]
    for (const [s, [shade, w]] of Object.entries(DARK_BG)) {
      vars[`--legacy-bg-${name}-${s}`] = (dark ? over(p[shade], w) : channels(p[s])).join(' ')
    }
    for (const [s, [shade, w]] of Object.entries(DARK_BORDER)) {
      vars[`--legacy-border-${name}-${s}`] = (dark ? over(p[shade], w) : channels(p[s])).join(' ')
    }
    for (const [s, shade] of Object.entries(DARK_TEXT)) {
      vars[`--legacy-text-${name}-${s}`] = channels(dark ? p[shade] : p[s]).join(' ')
    }
  }
  return vars
}

const legacyDarkCompat = plugin(({ addBase }) => {
  const dark = legacyVars(true)
  addBase({
    ':root': legacyVars(false),
    '@media (prefers-color-scheme: dark)': { ":root:not([data-theme='light'])": dark },
    ":root[data-theme='dark']": dark,
  })
})

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        canvas: token('bg'),
        surface: {
          DEFAULT: token('surface'),
          2: token('surface-2'),
          3: token('surface-3'),
        },
        ink: {
          DEFAULT: token('ink'),
          2: token('ink-2'),
          3: token('ink-3'),
        },
        line: {
          DEFAULT: token('line'),
          2: token('line-2'),
        },
        accent: {
          DEFAULT: token('accent'),
          ink: token('accent-ink'),
          soft: token('accent-soft'),
        },
        water: {
          DEFAULT: token('water'),
          soft: token('water-soft'),
        },
        silt: {
          DEFAULT: token('silt'),
          soft: token('silt-soft'),
        },
        lv: {
          green: token('lv-green'),
          yellow: token('lv-yellow'),
          orange: token('lv-orange'),
          red: token('lv-red'),
          'green-soft': token('lv-green-soft'),
          'yellow-soft': token('lv-yellow-soft'),
          'orange-soft': token('lv-orange-soft'),
          'red-soft': token('lv-red-soft'),
        },
        seq: {
          1: token('seq-1'),
          2: token('seq-2'),
          3: token('seq-3'),
          4: token('seq-4'),
          5: token('seq-5'),
        },
        gray: GRAY,
        slate: GRAY,
      },
      backgroundColor: {
        white: rgbVar('surface-rgb'),
        ...legacyPalette('bg', DARK_BG),
      },
      borderColor: legacyPalette('border', DARK_BORDER),
      textColor: legacyPalette('text', DARK_TEXT),
      fontFamily: {
        display: token('f-display'),
        sans: token('f-body'),
        mono: token('f-mono'),
      },
      borderRadius: {
        card: token('radius'),
      },
      boxShadow: {
        card: token('shadow'),
      },
    },
  },
  plugins: [legacyDarkCompat],
}
