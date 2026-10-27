import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { RealtimeData, StationData } from '../types'
import realtimeFixture from '../test-fixtures/realtime.json'
import StationsPanel from '../components/StationsPanel'
import { isTideStation, LIBOURNE_STATION_ID, rateTone, stationTone } from './stations'

const stations = (realtimeFixture as unknown as RealtimeData).stations!.stations

/** Libourne at a high tide: above its orange threshold and rising fast (UAT 2026-10-27). */
function libourneAtHighTide(): StationData {
  const lib = structuredClone(stations.find((s) => s.station_id === LIBOURNE_STATION_ID)!)
  lib.current_level.height_m = lib.threshold_orange + 0.4
  lib.tendency = 'hausse'
  lib.rate_cm_per_hour = 85.7
  return lib
}

describe('tidal station (Libourne): "marée, hors score", neutral', () => {
  it('is recognised by its weight 0 or its code', () => {
    expect(isTideStation(libourneAtHighTide())).toBe(true)
    expect(isTideStation({ station_id: LIBOURNE_STATION_ID, weight: 0.1 })).toBe(true)
    expect(isTideStation(stations.find((s) => s.station_id === 'P555001001')!)).toBe(false)
  })

  it('never takes a level or rate alert tone, unlike a scored station', () => {
    const lib = libourneAtHighTide()
    expect(stationTone(lib)).toBe('tide')
    expect(rateTone(lib)).toBe('neutral')
    const pessac = structuredClone(stations.find((s) => s.station_id === 'P555001001')!)
    pessac.current_level.height_m = pessac.threshold_orange + 0.1
    pessac.tendency = 'hausse'
    expect(stationTone(pessac)).toBe('orange')
    expect(rateTone(pessac)).toBe('rising')
  })

  it('renders the station card without orange or red colouring', () => {
    const others = stations.filter((s) => s.station_id !== LIBOURNE_STATION_ID)
    const html = renderToStaticMarkup(
      <StationsPanel stations={[libourneAtHighTide(), ...others]} selectedStation={null} onSelectStation={() => {}} />,
    )
    const card = html.slice(html.indexOf(`data-station="${LIBOURNE_STATION_ID}"`))
    const libCard = card.slice(0, card.indexOf('data-station=', 20))
    expect(libCard).toContain('data-tone="tide"')
    expect(libCard).toContain('marée, hors score')
    expect(libCard).toContain('+85.7 cm/h')
    expect(libCard).toContain('data-rate-tone="neutral"')
    expect(libCard).not.toMatch(/text-(red|orange)-|bg-(red|orange)-/)
  })
})
