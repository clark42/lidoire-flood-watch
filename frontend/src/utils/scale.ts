// Small numeric scales for the hand-drawn SVG charts (rails, ribbon, profile).
// A domain [d0, d1] maps linearly (or in log10) onto a range [r0, r1];
// `invert` maps back (pointer -> value).

export interface Scale {
  (value: number): number
  invert(position: number): number
  domain: [number, number]
  range: [number, number]
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

export function linearScale(domain: [number, number], range: [number, number]): Scale {
  const [d0, d1] = domain
  const [r0, r1] = range
  const span = d1 - d0 || 1
  const f = ((v: number) => r0 + ((v - d0) / span) * (r1 - r0)) as Scale
  f.invert = (p: number) => d0 + ((p - r0) / (r1 - r0 || 1)) * span
  f.domain = domain
  f.range = range
  return f
}

/** log10 scale; values at or below the domain start are pinned to it. */
export function logScale(domain: [number, number], range: [number, number]): Scale {
  const [d0, d1] = domain
  if (!(d0 > 0 && d1 > 0)) throw new RangeError('logScale needs a strictly positive domain')
  const lin = linearScale([Math.log10(d0), Math.log10(d1)], range)
  const f = ((v: number) => lin(Math.log10(Math.max(v, d0)))) as Scale
  f.invert = (p: number) => 10 ** lin.invert(p)
  f.domain = domain
  f.range = range
  return f
}

/** About `count` round ticks (steps of 1, 2, 2.5 or 5 x 10^n) covering [min, max]. */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || count < 1) return []
  if (min === max) return [min]
  const [lo, hi] = min < max ? [min, max] : [max, min]
  const rough = (hi - lo) / count
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rough) ?? magnitude * 10
  const ticks: number[] = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
    ticks.push(Number(v.toFixed(10)))
  }
  return ticks
}
