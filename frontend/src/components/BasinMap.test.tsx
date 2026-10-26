import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { BasinGeoJson, RadarFrameMeta, RealtimeData, RiskAnalogsData, VigilanceStatus } from '../types'
import basinFixture from '../test-fixtures/geo_basin.json'
import realtimeFixture from '../test-fixtures/realtime.json'
import vigilanceFixture from '../test-fixtures/vigilance.json'
import radarFixture from '../test-fixtures/radar_frame.json'
import analogsFixture from '../test-fixtures/risk_analogs.json'
import { buildBasinMap, mapExtent, mapPercentiles, MILL } from '../utils/basinMap'
import { containsPoint } from '../utils/mapGeo'
import { BasinMapView } from './BasinMap'

const basin = basinFixture as unknown as BasinGeoJson
const realtime = realtimeFixture as unknown as RealtimeData
const vigilance = vigilanceFixture as unknown as VigilanceStatus
const radar = radarFixture as unknown as RadarFrameMeta
const analogs = analogsFixture as unknown as RiskAnalogsData
const stations = realtime.stations!.stations
const NOW = new Date('2026-10-26T23:12:00Z')

function model(width = 343, withRadar = true) {
  return buildBasinMap({
    width,
    basin,
    stations,
    percentiles: mapPercentiles(analogs),
    troncons: vigilance.vigicrues.troncons,
    radar: withRadar ? radar : null,
  })
}

describe('buildBasinMap', () => {
  it('frames the basin, the mill and the near stations, and lists the far ones', () => {
    const m = model()
    const extent = mapExtent(basin, stations)
    expect(containsPoint(extent, MILL)).toBe(true)
    for (const [lon, lat] of basin.features[0].geometry.coordinates[0]) {
      expect(containsPoint(extent, [lon, lat])).toBe(true)
    }
    // Bergerac (38 km) falls inside the radar emprise around the basin
    expect(m.stations.map((s) => s.short).sort()).toEqual(['Abzac', 'Bergerac', 'Coutras', 'Libourne', 'Pessac'])
    expect(m.offMap.map((s) => s.name)).toEqual(['Ribérac', 'Périgueux'])
    expect(m.offMap[1].direction).toBe('NE')
  })

  it('fits a 375 px phone (343 px of map after the gutters)', () => {
    const m = model(343)
    expect(m.width).toBe(343)
    expect(m.height).toBeGreaterThan(120)
    expect(m.height).toBeLessThan(260)
    for (const s of m.stations) {
      expect(s.x).toBeGreaterThanOrEqual(0)
      expect(s.x).toBeLessThanOrEqual(343)
      expect(s.y).toBeGreaterThanOrEqual(0)
      expect(s.y).toBeLessThanOrEqual(m.height)
    }
  })

  it('shows Libourne as a tide station outside the score, without a percentile colour', () => {
    const lib = model().stations.find((s) => s.id === 'P577001001')!
    expect(lib.tide).toBe(true)
    expect(lib.seq).toBeNull()
    expect(lib.title).toContain('marée : hors score')
  })

  it('colours the Dordogne and Pessac by the Pessac percentile, the Isle stays neutral', () => {
    const m = model()
    const pct = analogs.situation.percentiles.pessac
    const dordogne = m.rivers.find((r) => r.key === 'dordogne')!
    expect(dordogne.percentile).toBe(pct)
    expect(dordogne.seq).toBe(1) // p13.6
    expect(m.stations.find((s) => s.short === 'Pessac')!.seq).toBe(1)
    expect(m.rivers.find((r) => r.key === 'isle')!.seq).toBeNull()
    expect(m.rivers.find((r) => r.key === 'eyraud')!.percentile).toBe(analogs.situation.percentiles.eyraud_q)
  })

  it('casts the Vigicrues reaches with their level', () => {
    const m = model()
    expect(m.troncons.map((t) => [t.code, t.level])).toEqual([
      ['DO22', 1],
      ['DO5', 1],
    ])
    expect(m.troncons.every((t) => t.d.startsWith('M'))).toBe(true)
  })

  it('places the radar frame inside the map', () => {
    const m = model(600)
    expect(m.radar).not.toBeNull()
    expect(m.radar!.url).toBe(radar.url)
    expect(m.radar!.rect.x).toBeGreaterThanOrEqual(0)
    expect(m.radar!.rect.x + m.radar!.rect.width).toBeLessThanOrEqual(600 + 1e-6)
    expect(m.radar!.rect.width / m.radar!.rect.height).toBeCloseTo(
      ((0.487 + 0.173) * Math.cos(((m.extent.south + m.extent.north) / 2) * (Math.PI / 180))) / (45.069 - 44.761),
      6,
    )
    expect(model(600, false).radar).toBeNull()
  })

  it('still draws without the basin payload', () => {
    const m = buildBasinMap({ width: 343, basin: null, stations, percentiles: {}, troncons: [] })
    expect(m.basinD).toBeNull()
    expect(m.rivers.length).toBeGreaterThan(0)
    expect(m.troncons).toHaveLength(0)
  })
})

describe('BasinMapView', () => {
  it('renders the map, the mill, the legend and the scale bar', () => {
    const html = renderToStaticMarkup(<BasinMapView model={model()} radarOn={false} now={NOW} />)
    expect(html).toContain('role="img"')
    expect(html).toContain('Moulin')
    // Short label on a phone, the legend explains the dashed ring
    expect(html).toMatch(/>Libourne<\/text>/)
    expect(renderToStaticMarkup(<BasinMapView model={model(600)} radarOn={false} now={NOW} />)).toContain(
      'Libourne (marée)',
    )
    expect(html).toContain('Eyraud (analogue)')
    expect(html).toContain('Nappe Mouliets')
    expect(html).toContain('bassin 231 km²')
    expect(html).toContain('10 km')
    expect(html).toContain('Libourne : marée, hors score')
    expect(html).toContain('tronçon Vigicrues (vert)')
    expect(html).toContain('Hors cadre : Ribérac (NE, 49 km), Périgueux (NE, 66 km)')
    expect(html).not.toContain('<image')
  })

  it('draws the radar frame with its age and the OPERA attribution', () => {
    const html = renderToStaticMarkup(<BasinMapView model={model()} radarOn radarMeta={radar} now={NOW} />)
    expect(html).toContain('<image')
    expect(html).toContain('href="/api/realtime/radar/frame.png?t=2026-10-26T22:45Z"')
    expect(html).toContain('il y a 27 min')
    expect(html).toContain('EUMETNET OPERA')
    expect(html).toContain('aucune pluie détectée')
  })

  it('says when the radar is unavailable', () => {
    const off = renderToStaticMarkup(
      <BasinMapView model={model(343, false)} radarOn radarMeta={{ ...radar, available: false }} now={NOW} />,
    )
    expect(off).toContain('Aucune image radar récente')
    const err = renderToStaticMarkup(
      <BasinMapView model={model(343, false)} radarOn radarError="Erreur API 503" now={NOW} />,
    )
    expect(err).toContain('Radar indisponible : Erreur API 503')
  })
})
