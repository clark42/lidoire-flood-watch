import { describe, expect, it } from 'vitest'
import {
  bboxFromArray,
  bboxOf,
  compassFrom,
  fitMap,
  imagePlacement,
  kmPerPixel,
  padBbox,
  percentileClass,
  polygonPath,
  scaleBar,
} from './mapGeo'
import radarFrame from '../test-fixtures/radar_frame.json'

const EXTENT = { west: -0.3, south: 44.8, east: 0.5, north: 45.1 }

describe('fitMap', () => {
  it('maps the north-west corner to (0, 0) and the south-east one to (width, height)', () => {
    const { projection, width, height } = fitMap(EXTENT, 343)
    const nw = projection([EXTENT.west, EXTENT.north])!
    const se = projection([EXTENT.east, EXTENT.south])!
    expect(nw[0]).toBeCloseTo(0, 6)
    expect(nw[1]).toBeCloseTo(0, 6)
    expect(se[0]).toBeCloseTo(343, 6)
    expect(se[1]).toBeCloseTo(height, 0)
    expect(width).toBe(343)
  })

  it('keeps the ground aspect ratio (longitudes shrunk by cos(latitude))', () => {
    const { height } = fitMap(EXTENT, 800)
    const k = Math.cos((44.95 * Math.PI) / 180)
    expect(height).toBe(Math.round((800 * 0.3) / (0.8 * k)))
  })

  it('keeps meridians and parallels straight and axis-aligned', () => {
    const { projection } = fitMap(EXTENT, 500)
    const a = projection([0.1, 44.85])!
    const b = projection([0.1, 45.05])!
    const c = projection([0.4, 44.85])!
    expect(a[0]).toBeCloseTo(b[0], 9)
    expect(a[1]).toBeCloseTo(c[1], 9)
  })

  it('inverts screen points back to lon/lat', () => {
    const { projection } = fitMap(EXTENT, 500)
    const p = projection([0.0015, 44.876])!
    const back = projection.invert!(p)!
    expect(back[0]).toBeCloseTo(0.0015, 9)
    expect(back[1]).toBeCloseTo(44.876, 9)
  })
})

describe('radar frame placement', () => {
  it('places the frame on its bbox, whatever the map extent', () => {
    const bbox = bboxFromArray(radarFrame.bbox)!
    const { projection } = fitMap(EXTENT, 600)
    const rect = imagePlacement(projection, bbox)!
    const nw = projection([bbox.west, bbox.north])!
    expect(rect.x).toBeCloseTo(nw[0], 9)
    expect(rect.y).toBeCloseTo(nw[1], 9)
    // Same aspect as the PNG grid once longitudes are shrunk by cos(lat0)
    const k = Math.cos(((EXTENT.south + EXTENT.north) / 2) * (Math.PI / 180))
    expect(rect.width / rect.height).toBeCloseTo(((bbox.east - bbox.west) * k) / (bbox.north - bbox.south), 6)
    // 132 x 62 pixels of 0.005 degree
    expect(radarFrame.width * radarFrame.pixel_deg).toBeCloseTo(bbox.east - bbox.west, 2)
  })

  it('rejects a malformed bbox', () => {
    expect(bboxFromArray([1, 2, 3])).toBeNull()
    expect(bboxFromArray([0.5, 44, 0.1, 45])).toBeNull()
    expect(bboxFromArray(null)).toBeNull()
  })
})

describe('scale bar', () => {
  it('measures about 79 km per degree of longitude at 45 N', () => {
    const map = fitMap(EXTENT, 800)
    const kpp = kmPerPixel(map.projection, map)
    const kmPerDegLon = (800 / 0.8) * kpp
    expect(kmPerDegLon).toBeGreaterThan(78)
    expect(kmPerDegLon).toBeLessThan(80)
  })

  it('picks the longest round length within a quarter of the width', () => {
    const map = fitMap(EXTENT, 343)
    const bar = scaleBar(map.projection, map)!
    // 0.8 degree ~ 63 km over 343 px: a quarter is ~16 km -> 10 km
    expect(bar.km).toBe(10)
    expect(bar.px).toBeGreaterThan(50)
    expect(bar.px).toBeLessThan(343 / 4)
  })
})

describe('bbox helpers', () => {
  it('bounds points and pads them in km', () => {
    const b = bboxOf([
      [0, 44.9],
      [0.2, 45],
    ])!
    expect(b).toEqual({ west: 0, south: 44.9, east: 0.2, north: 45 })
    const p = padBbox(b, 11.0574)
    expect(p.north - b.north).toBeCloseTo(0.1, 6)
    expect(b.west - p.west).toBeGreaterThan(0.1)
    expect(bboxOf([])).toBeNull()
  })

  it('names the compass direction', () => {
    expect(compassFrom([0, 44.9], [0, 45.2])).toBe('N')
    expect(compassFrom([0, 44.9], [0.7, 45.2])).toBe('NE')
    expect(compassFrom([0, 44.9], [-0.5, 44.9])).toBe('O')
  })
})

describe('percentileClass', () => {
  it('splits at p50, p75, p90 and p99', () => {
    expect([10, 50, 80, 95, 99, 100].map(percentileClass)).toEqual([1, 2, 3, 4, 5, 5])
    expect(percentileClass(null)).toBeNull()
    expect(percentileClass(Number.NaN)).toBeNull()
  })
})

describe('polygonPath', () => {
  it('closes every ring, whatever its winding', () => {
    const { projection } = fitMap(EXTENT, 300)
    const ring: [number, number][] = [
      [0, 44.9],
      [0.1, 44.9],
      [0.1, 45],
      [0, 44.9],
    ]
    const cw = polygonPath(projection, [ring])
    const ccw = polygonPath(projection, [[...ring].reverse()])
    expect(cw.startsWith('M')).toBe(true)
    expect(cw.endsWith('Z')).toBe(true)
    expect(ccw.endsWith('Z')).toBe(true)
  })
})
