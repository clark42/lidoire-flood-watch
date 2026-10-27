import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { EpisodeLabelSet, LidoireProfileData, UnifiedTimelineData } from '../types'
import labelsFixture from '../test-fixtures/episode-labels.json'
import profileFlood from '../test-fixtures/geo-profile-2024-02-26T11.json'
import profileLow from '../test-fixtures/geo-profile-2026-10-26T21.json'
import profileUat from '../test-fixtures/geo-profile.json'
import { buildMechanisms } from '../utils/mechanisms'
import replayFeb2024 from '../test-fixtures/timeline-replay-2024-02.json'
import { centredTextX, DEFAULT_CONFLUENCE_PARAMS, labelBoxX } from '../utils/lidoireProfile'
import { hourlyTimes, nearestIndex, replayFrame, replaySeries, replayWindow } from '../utils/replay'
import LidoireProfile, { LidoireProfileReadout } from './LidoireProfile'
import ReplayPanel from './ReplayPanel'
import ReplayScrubber from './ReplayScrubber'
import SyncStrips from './SyncStrips'

const flood = profileFlood as unknown as LidoireProfileData
const low = profileLow as unknown as LidoireProfileData
const labels = (labelsFixture as unknown as EpisodeLabelSet).episodes
const series = replaySeries(replayFeb2024 as unknown as UnifiedTimelineData)

/** Visible text of server-rendered markup (tags dropped, entities decoded). */
function text(html: string): string {
  return html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
}

function reachOf(p: LidoireProfileData) {
  const b = p.backwater
  return {
    status: b.status,
    distance_m: b.distance_m,
    connected_distance_m: b.connected_distance_m,
    disconnected_low_points: b.disconnected_low_points,
    z_m: b.z_m,
    distanceBelowMillM: b.distance_below_mill_m,
    connectedDistanceBelowMillM: b.connected_distance_below_mill_m,
    reachesMill: b.reaches_mill,
  }
}

describe('LidoireProfile (recorded payload of 2024-02-26 11:00)', () => {
  const html = renderToStaticMarkup(
    <LidoireProfile profile={flood} zc={flood.confluence.z_c_m_ngf} reach={reachOf(flood)} floorZ={null} width={640} />,
  )

  it('draws the ground, the two probable weirs and the hatched backwater', () => {
    expect(html.match(/data-step="/g)).toHaveLength(2)
    expect(html).toContain('data-backwater=""')
    expect(html).toMatch(/fill="url\(#water-[^"]+\)"/)
    expect(text(html)).toContain('seuil ?')
  })

  it('labels Z_c as estimated and gives the distance left below the mill', () => {
    const t = text(html)
    expect(t).toContain('≈ 6,54 m NGF')
    expect(t).toContain('Dordogne à la confluence, estimé')
    // Connected water (server connected_distance_below_mill_m = 3372.7 m), not the 2.8 km upper bound
    expect(t).toContain('≈ 3,4 km sans remous')
    expect(html).toContain('aria-label="Profil en long de la Lidoire')
  })

  it('marks the mill ground, an unknown floor and the unmeasured Lidoire level', () => {
    const t = text(html)
    expect(t).toContain('Moulin')
    expect(t).toContain('terrain 12,0')
    expect(html).toContain('data-floor="unknown"')
    expect(t).toContain('plancher ?')
    expect(html).toContain('data-lidoire-unmeasured=""')
  })

  it('draws a tested floor as a dashed line', () => {
    const withFloor = renderToStaticMarkup(
      <LidoireProfile profile={flood} zc={flood.confluence.z_c_m_ngf} reach={reachOf(flood)} floorZ={12.4} width={640} />,
    )
    expect(withFloor).toContain('data-floor="12.4"')
    expect(text(withFloor)).toContain('plancher (hypothèse) 12,40')
  })

  it('fits a 343 px phone column', () => {
    const phone = renderToStaticMarkup(
      <LidoireProfile profile={flood} zc={flood.confluence.z_c_m_ngf} reach={reachOf(flood)} floorZ={null} width={343} />,
    )
    expect(phone).toContain('width="343"')
    expect(text(phone)).toContain('Dordogne, estimé')
  })

  it('shows a short reach at low water (2026-10-26)', () => {
    const lowHtml = renderToStaticMarkup(
      <LidoireProfile profile={low} zc={low.confluence.z_c_m_ngf} reach={reachOf(low)} floorZ={null} width={640} />,
    )
    expect(text(lowHtml)).toContain('≈ 5,0 km sans remous')
  })
})

describe('one backwater reach everywhere (UAT 2026-10-27: 4,9 vs 4,7 km)', () => {
  const uat = profileUat as unknown as LidoireProfileData
  it('gives the same connected distance in the mechanism card and in the profile', () => {
    // Upper bound 4.72 km, connected water 4.95 km below the mill: both places show the connected one
    expect(uat.backwater.distance_below_mill_m).toBeCloseTo(4720.1)
    const card = buildMechanisms({ profile: uat, now: new Date('2026-10-26T23:15:00Z') }).find((m) => m.id === 'backwater')!
    expect(card.lead).toContain("Le remous s'arrête à ≈ 4,9 km sous le moulin à 00:10 (eau connectée).")
    const svg = text(renderToStaticMarkup(<LidoireProfile profile={uat} zc={uat.confluence.z_c_m_ngf} reach={reachOf(uat)} floorZ={null} width={390} />))
    expect(svg).toContain('≈ 4,9 km sans remous')
    const readout = text(
      renderToStaticMarkup(
        <LidoireProfileReadout profile={uat} zc={uat.confluence.z_c_m_ngf} reach={reachOf(uat)} floorZ={null} at={Date.parse(uat.at)} />,
      ),
    )
    expect(readout).toContain('Le remous s’arrête à ≈ 4,9 km sous le moulin à 27/10 00:10')
    expect(readout).not.toContain('4,7')
  })
})

describe('labels stay inside the SVG (UAT: "plancher ?" clipped at 390 px)', () => {
  for (const width of [320, 343, 375, 390, 560, 640, 960, 1280]) {
    it(`keeps the floor pill and the gap label within ${width} px`, () => {
      const html = renderToStaticMarkup(
        <LidoireProfile profile={flood} zc={flood.confluence.z_c_m_ngf} reach={reachOf(flood)} floorZ={null} width={width} />,
      )
      const pill = html.match(/data-floor="unknown"><rect x="([\d.-]+)"[^>]*width="(\d+)"/)!
      const x = Number(pill[1])
      expect(x).toBeGreaterThanOrEqual(0)
      expect(x + Number(pill[2])).toBeLessThanOrEqual(width)
      const gap = html.match(/data-gap="[^"]+">[\s\S]*?<text x="([\d.-]+)"/)!
      expect(Number(gap[1]) - 65).toBeGreaterThanOrEqual(0)
      expect(Number(gap[1]) + 65).toBeLessThanOrEqual(width)
    })
  }

  it('moves the pill to the left of the mill when the right side is too short', () => {
    expect(labelBoxX(380, 66, 6, [2, 388])).toBe(308)
    expect(labelBoxX(200, 66, 6, [2, 388])).toBe(206)
    expect(labelBoxX(30, 66, 6, [2, 60])).toBe(2)
    expect(centredTextX(380, 130, [2, 388])).toBe(323)
  })
})

describe('LidoireProfileReadout', () => {
  it('states the reach, the estimate and that the mill level is not measured', () => {
    const t = text(
      renderToStaticMarkup(
        <LidoireProfileReadout
          profile={flood}
          zc={flood.confluence.z_c_m_ngf}
          reach={reachOf(flood)}
          floorZ={12.4}
          at={Date.parse(flood.at)}
        />,
      ),
    )
    expect(t).toContain('Le remous s’arrête à ≈ 3,4 km sous le moulin à 26/02 12:00')
    expect(t).toContain('eau connectée jusqu’à 2,46 km de la confluence')
    expect(t).toContain('soit 5,5 m sous le terrain du moulin')
    expect(t).toContain('il manquerait 5,86 m au plan d’eau')
    expect(t).toContain('Le niveau de la Lidoire au moulin n’est pas mesuré.')
    expect(t).toContain('Un capteur de niveau au moulin est prévu')
  })
})

describe('SyncStrips (recorded replay of February 2024)', () => {
  const win = replayWindow('feb2024', labels, new Date('2026-10-26T21:00:00Z'))
  const times = hourlyTimes(win.start, win.end)
  const index = nearestIndex(times, win.focus.getTime())
  const zc = times
    .map((t) => ({ t, v: replayFrame(series, t, [], 5833.4, DEFAULT_CONFLUENCE_PARAMS).zc }))
    .filter((p): p is { t: number; v: number } => p.v != null)
  const html = renderToStaticMarkup(
    <SyncStrips
      series={series}
      times={times}
      index={index}
      width={640}
      zeros={{ pessac: 3.33, libourne: -1.17 }}
      zc={zc}
      markerAt={win.peak!.getTime()}
      markerLabel="inondation labellisée"
    />,
  )

  it('puts the cursor on the labelled hour and marks the flood', () => {
    expect(html).toContain(`data-cursor="${Date.parse('2024-02-26T11:00:00Z')}"`)
    expect(html).toContain(`data-marker="${Date.parse('2024-02-26T11:00:00Z')}"`)
    expect(text(html)).toContain('inondation labellisée')
  })

  it('draws the four strips with their legend and flags the shadow without points', () => {
    const t = text(html)
    for (const title of ['Dordogne, m NGF', 'Eyraud, m³/s', 'Score /100', 'Pluie, mm/h']) expect(t).toContain(title)
    expect(html.match(/<path d="M/g)!.length).toBeGreaterThanOrEqual(5)
    expect(t).toContain('Shadow 4.0a (aucun point)')
    expect(t).not.toContain('Shadow 3.4-gate (aucun point)')
    expect(t).toContain('Libourne (marée, hors score)')
  })
})

describe('ReplayScrubber', () => {
  it('offers the five presets, a keyboard range and the open-the-dashboard action', () => {
    const times = [0, 3_600_000, 7_200_000]
    const html = renderToStaticMarkup(
      <ReplayScrubber
        presetId="feb2024"
        onPreset={() => {}}
        customAt={null}
        onCustomDate={() => {}}
        times={times}
        index={1}
        onIndex={() => {}}
        playing={false}
        onTogglePlay={() => {}}
        reducedMotion
        frame={null}
        isLive={false}
        story="Inondation labellisée."
        onOpenDashboard={() => {}}
      />,
    )
    const t = text(html)
    for (const label of ['Ces 10 jours', 'Crue fév. 2026', 'Crue fév. 2024', 'Crue nov. 2023', 'Crue avr. 2025']) {
      expect(t).toContain(label)
    }
    expect(html.match(/aria-pressed="true"/g)).toHaveLength(1)
    expect(html).toContain('type="range"')
    expect(html).toContain('max="2"')
    expect(t).toContain('lecture par pas de 6 h')
    expect(t).toContain('Tout le tableau de bord au')
  })
})

describe('ReplayPanel', () => {
  it('renders its controls before any data (server render, no fetch)', () => {
    const t = text(renderToStaticMarkup(<ReplayPanel />))
    expect(t).toContain('Ces 10 jours')
    expect(t).toContain('Lire')
  })
})
