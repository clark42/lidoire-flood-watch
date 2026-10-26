// WCAG 2.x relative luminance and contrast ratio (used by the token tests)

/** Parse "#rrggbb", "#rgb" or "r g b" channels into [r, g, b] (0-255). */
export function parseColor(value: string): [number, number, number] {
  const v = value.trim()
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v)
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].replace(/./g, (c) => c + c) : hex[1]
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as [number, number, number]
  }
  const channels = /^(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})$/.exec(v)
  if (channels) return [Number(channels[1]), Number(channels[2]), Number(channels[3])]
  throw new Error(`Unsupported colour: ${value}`)
}

export function relativeLuminance(value: string): number {
  const [r, g, b] = parseColor(value).map((c) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** Contrast ratio between two colours, 1 to 21. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}
