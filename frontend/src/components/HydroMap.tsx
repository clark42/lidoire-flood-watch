import { useMemo } from 'react'
import {
  MapContainer,
  TileLayer,
  CircleMarker,
  Marker,
  Popup,
  Tooltip,
  Polyline,
} from 'react-leaflet'
import { DivIcon, type LatLngExpression } from 'leaflet'
import type {
  StationData,
  NetatmoStation,
  InfoclimatStation,
  TideForecast,
  Tendency,
} from '../types'
import { formatDistanceKm } from '../utils/format'

interface HydroMapProps {
  stations: StationData[]
  netatmoStations?: NetatmoStation[]
  infoclimatStations?: InfoclimatStation[]
  tide?: TideForecast | null
}

const MOULIN_POSITION: LatLngExpression = [44.876, 0.001]

// Tide station: Libourne port on the Dordogne
const LIBOURNE_PORT: LatLngExpression = [44.917, -0.233]

const RIVER_PATHS: Record<string, { points: LatLngExpression[]; color: string }> = {
  Dordogne: {
    points: [
      [44.85, 0.49],
      [44.83, 0.04],
      [44.88, -0.13],
      [44.92, -0.23],
    ],
    color: '#3b82f6',
  },
  Isle: {
    points: [
      [45.18, 0.72],
      [44.88, -0.13],
      [44.92, -0.23],
    ],
    color: '#a855f7',
  },
  Dronne: {
    points: [
      [45.25, 0.35],
      [45.04, -0.12],
      [44.88, -0.13],
    ],
    color: '#f59e0b',
  },
}

const MOULIN_SVG = `
<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 24 24"
  fill="none" stroke="#1e3a5f" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M3 21h18"/>
  <path d="M5 21V7l7-4 7 4v14"/>
  <path d="M9 21v-6h6v6"/>
  <path d="M9 10h6"/>
</svg>`

const ANCHOR_SVG = `
<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24"
  fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="5" r="3"/>
  <line x1="12" y1="22" x2="12" y2="8"/>
  <path d="M5 12H2a10 10 0 0 0 20 0h-3"/>
</svg>`

function getStationColor(station: StationData): string {
  const height = station.current_level.height_m
  if (height === 0) return '#9ca3af'
  if (height >= station.threshold_red) return '#ef4444'
  if (height >= station.threshold_orange) return '#f97316'
  if (height >= station.threshold_yellow) return '#eab308'
  return '#22c55e'
}

function getRainColor(rain1h: number | null): string {
  if (rain1h === null || rain1h === 0) return '#93c5fd'
  if (rain1h < 2) return '#60a5fa'
  if (rain1h < 5) return '#3b82f6'
  return '#1d4ed8'
}

function tendencyLabel(t: Tendency): string {
  if (t === 'hausse') return 'En hausse'
  if (t === 'baisse') return 'En baisse'
  return 'Stable'
}

export default function HydroMap({
  stations,
  netatmoStations,
  infoclimatStations,
  tide,
}: HydroMapProps) {
  const moulinIcon = useMemo(
    () =>
      new DivIcon({
        html: `<div class="marker-moulin">${MOULIN_SVG}</div>`,
        className: '',
        iconSize: [28, 28],
        iconAnchor: [14, 14],
      }),
    []
  )

  const anchorIcon = useMemo(
    () =>
      new DivIcon({
        html: `<div style="background:white;border-radius:50%;padding:3px;box-shadow:0 1px 4px rgba(0,0,0,.3);display:inline-flex;">${ANCHOR_SVG}</div>`,
        className: '',
        iconSize: [28, 28],
        iconAnchor: [14, 14],
      }),
    []
  )

  const stationsWithCoords = useMemo(
    () => stations.filter((s) => s.lat != null && s.lon != null),
    [stations]
  )

  const lidoireNetatmo = useMemo(
    () =>
      (netatmoStations ?? []).filter(
        (s) => s.is_lidoire === true && s.lat != null && s.lon != null
      ),
    [netatmoStations]
  )

  const infoclimatWithCoords = useMemo(
    () =>
      (infoclimatStations ?? []).filter(
        (s) => s.lat != null && s.lon != null
      ),
    [infoclimatStations]
  )

  const pinchActive = useMemo(() => {
    const pessac = stations.find((s) => s.station_id === 'P555001001')
    const libourne = stations.find((s) => s.station_id === 'P577001001')
    if (!pessac || !libourne) return false
    return (
      pessac.current_level.height_m >= pessac.threshold_yellow &&
      libourne.current_level.height_m >= libourne.threshold_yellow
    )
  }, [stations])

  const pinchLine = useMemo<LatLngExpression[] | null>(() => {
    if (!pinchActive) return null
    const pessac = stations.find((s) => s.station_id === 'P555001001')
    const libourne = stations.find((s) => s.station_id === 'P577001001')
    if (!pessac?.lat || !pessac?.lon || !libourne?.lat || !libourne?.lon) return null
    return [
      [pessac.lat, pessac.lon],
      MOULIN_POSITION,
      [libourne.lat, libourne.lon],
    ]
  }, [pinchActive, stations])

  return (
    <div className="relative w-full h-[380px] rounded-xl shadow overflow-hidden">
      <MapContainer
        center={MOULIN_POSITION}
        zoom={10}
        className="w-full h-full"
        scrollWheelZoom={true}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />

        {/* River polylines */}
        {Object.entries(RIVER_PATHS).map(([name, { points, color }]) => (
          <Polyline
            key={name}
            positions={points}
            pathOptions={{
              color,
              weight: 2,
              opacity: 0.5,
              dashArray: '8 6',
            }}
          />
        ))}

        {/* Moulin marker */}
        <Marker position={MOULIN_POSITION} icon={moulinIcon}>
          <Popup>
            <div className="text-sm font-semibold">Le Moulin</div>
            <div className="text-xs text-gray-500">Point de surveillance</div>
          </Popup>
        </Marker>

        {/* Tide station marker */}
        <Marker position={LIBOURNE_PORT} icon={anchorIcon}>
          <Tooltip direction="top" offset={[0, -8]}>
            <div>
              <span className="font-medium">Maree (estimee a la confluence)</span>
              {tide?.current_coefficient != null && (
                <>
                  <br />
                  Coef : {tide.current_coefficient}
                </>
              )}
            </div>
          </Tooltip>
          <Popup>
            <div className="text-sm space-y-1">
              <div className="font-semibold">Maree estimee</div>
              <div className="text-xs text-gray-500">Confluence de la Lidoire (embouchure + 6 h)</div>
              {tide?.current_coefficient != null && (
                <div>
                  Coefficient :{' '}
                  <span className="font-bold">{tide.current_coefficient}</span>
                </div>
              )}
              <div className="text-xs text-gray-400">
                Source : Open-Meteo Marine (embouchure de la Gironde)
              </div>
            </div>
          </Popup>
        </Marker>

        {/* River stations */}
        {stationsWithCoords.map((station) => {
          const color = getStationColor(station)
          const radius = Math.max(8, station.weight * 50)
          return (
            <CircleMarker
              key={station.station_id}
              center={[station.lat!, station.lon!]}
              radius={radius}
              pathOptions={{
                color,
                fillColor: color,
                fillOpacity: 0.6,
                weight: 2,
              }}
            >
              <Tooltip direction="top" offset={[0, -8]}>
                <span className="font-medium">{station.station_name}</span>
                {' - '}
                <span>{station.current_level.height_m.toFixed(2)} m</span>
              </Tooltip>
              <Popup>
                <div className="text-sm space-y-1">
                  <div className="font-semibold">{station.station_name}</div>
                  <div className="text-xs text-gray-500">{station.river}</div>
                  <div>
                    Niveau :{' '}
                    <span className="font-bold">
                      {station.current_level.height_m.toFixed(2)} m
                    </span>
                  </div>
                  <div>Tendance : {tendencyLabel(station.tendency)}</div>
                  <div>
                    Vitesse : {station.rate_cm_per_hour > 0 ? '+' : ''}
                    {station.rate_cm_per_hour.toFixed(1)} cm/h
                  </div>
                  <hr className="my-1" />
                  <div className="text-xs text-gray-400">
                    Seuils : J={station.threshold_yellow}m / O=
                    {station.threshold_orange}m / R={station.threshold_red}m
                  </div>
                </div>
              </Popup>
            </CircleMarker>
          )
        })}

        {/* Netatmo rainfall stations (Lidoire basin only) */}
        {lidoireNetatmo.map((ns, idx) => {
          const color = getRainColor(ns.rain_1h_mm)
          return (
            <CircleMarker
              key={`netatmo-${idx}`}
              center={[ns.lat!, ns.lon!]}
              radius={6}
              pathOptions={{
                color,
                fillColor: color,
                fillOpacity: 0.7,
                weight: 1,
              }}
            >
              <Tooltip direction="top" offset={[0, -6]}>
                <div>
                  <span className="font-medium">{ns.city}</span>
                  <br />
                  1h : {ns.rain_1h_mm != null ? `${ns.rain_1h_mm.toFixed(1)} mm` : '-'}
                  <br />
                  24h : {ns.rain_24h_mm != null ? `${ns.rain_24h_mm.toFixed(1)} mm` : '-'}
                </div>
              </Tooltip>
            </CircleMarker>
          )
        })}

        {/* Infoclimat weather stations */}
        {infoclimatWithCoords.map((ic, idx) => (
          <CircleMarker
            key={`infoclimat-${idx}`}
            center={[ic.lat!, ic.lon!]}
            radius={5}
            pathOptions={{
              color: '#8b5cf6',
              fillColor: '#8b5cf6',
              fillOpacity: 0.5,
              weight: 1.5,
              dashArray: '3 3',
            }}
          >
            <Tooltip direction="top" offset={[0, -6]}>
              <div>
                <span className="font-medium">{ic.name}</span>
                <br />
                {ic.distance_km != null && (
                  <span className="text-gray-500">{formatDistanceKm(ic.distance_km)}</span>
                )}
                <br />
                <span className="text-gray-400">{ic.source}</span>
              </div>
            </Tooltip>
          </CircleMarker>
        ))}

        {/* Pinch effect line */}
        {pinchLine && (
          <Polyline
            positions={pinchLine}
            pathOptions={{
              color: '#ef4444',
              weight: 4,
              opacity: 0.8,
            }}
            className="pinch-line"
          />
        )}
      </MapContainer>

      {/* Legend overlay */}
      <div className="absolute bottom-3 left-3 z-[1000] bg-white/90 backdrop-blur-sm rounded-lg px-3 py-2 text-xs shadow-md space-y-1">
        <div className="font-semibold text-gray-700 mb-1">Legende</div>
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-full bg-[#22c55e]" />
          <span>Vert - Normal</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-full bg-[#eab308]" />
          <span>Jaune - Vigilance</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-full bg-[#f97316]" />
          <span>Orange - Alerte</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-full bg-[#ef4444]" />
          <span>Rouge - Danger</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-full bg-[#3b82f6]" />
          <span>Pluie (Netatmo)</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-full border border-dashed border-[#8b5cf6]" style={{ backgroundColor: '#8b5cf620' }} />
          <span>Meteo (Infoclimat)</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-2.5 rounded-full bg-[#2563eb]" />
          <span>Maree (estimee)</span>
        </div>
      </div>
    </div>
  )
}
