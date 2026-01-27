import { useState } from 'react'
import {
  CloudRain,
  Droplets,
  ChevronDown,
  ChevronUp,
  MapPin,
  Cloud,
  Navigation,
} from 'lucide-react'
import type { NetatmoData, NetatmoStation, InfoclimatStation, InfoclimatRainfall } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

interface RainfallPanelProps {
  netatmo: NetatmoData | null
  infoclimat?: {
    available: boolean
    stations_found?: number
    nearest_stations?: InfoclimatStation[]
    rainfall?: InfoclimatRainfall
  }
  forecast?: {
    forecast_24h_mm: number
    forecast_72h_mm: number
  }
  loading?: boolean
}

function getRainIntensityColor(mm: number): string {
  if (mm >= 20) return 'text-purple-600 bg-purple-50'
  if (mm >= 10) return 'text-blue-600 bg-blue-50'
  if (mm >= 5) return 'text-cyan-600 bg-cyan-50'
  if (mm > 0) return 'text-sky-600 bg-sky-50'
  return 'text-gray-500 bg-gray-50'
}

function getPositionLabel(position?: string): string {
  switch (position) {
    case 'amont': return 'Amont'
    case 'milieu': return 'Milieu'
    case 'aval': return 'Aval'
    default: return ''
  }
}

function getPositionColor(position?: string): string {
  switch (position) {
    case 'amont': return 'bg-emerald-100 text-emerald-700'
    case 'milieu': return 'bg-blue-100 text-blue-700'
    case 'aval': return 'bg-purple-100 text-purple-700'
    default: return 'bg-gray-100 text-gray-600'
  }
}

function LidoireStationRow({ station }: { station: NetatmoStation }) {
  const formatRain = (value: number | null | undefined) => {
    if (value == null) return '-'
    return value.toFixed(1)
  }

  const rainClass = (value: number | null | undefined) => {
    if (value == null || value === 0) return ''
    if (value >= 20) return 'text-purple-600'
    if (value >= 10) return 'text-blue-600'
    if (value >= 5) return 'text-cyan-600'
    return 'text-sky-600'
  }

  return (
    <div className="flex items-center justify-between p-2 text-sm hover:bg-emerald-50 rounded">
      <div className="flex items-center gap-2 min-w-0">
        <Navigation className="w-3 h-3 text-emerald-600 flex-shrink-0" />
        <span className="font-medium truncate">{station.city}</span>
        <span className={`text-xs px-1.5 py-0.5 rounded flex-shrink-0 ${getPositionColor(station.position)}`}>
          {getPositionLabel(station.position)}
        </span>
      </div>
      <div className="flex gap-2 text-xs flex-shrink-0">
        <span className="w-14 text-right" title="Derniere heure">
          <span className="text-gray-400">1h:</span>{' '}
          <strong className={rainClass(station.rain_1h_mm)}>
            {formatRain(station.rain_1h_mm)}
          </strong>
        </span>
        <span className="w-14 text-right" title="Dernieres 24 heures">
          <span className="text-gray-400">24h:</span>{' '}
          <strong className={rainClass(station.rain_24h_mm)}>
            {formatRain(station.rain_24h_mm)}
          </strong>
        </span>
        <span className="w-14 text-right" title="Dernieres 72 heures">
          <span className="text-gray-400">72h:</span>{' '}
          <strong className={rainClass(station.rain_72h_mm)}>
            {formatRain(station.rain_72h_mm)}
          </strong>
        </span>
        <span className="w-14 text-right" title="Derniers 7 jours">
          <span className="text-gray-400">7j:</span>{' '}
          <strong className={rainClass(station.rain_7d_mm)}>
            {formatRain(station.rain_7d_mm)}
          </strong>
        </span>
      </div>
    </div>
  )
}

function StationRow({ station }: { station: NetatmoStation }) {
  return (
    <div className="flex items-center justify-between p-2 text-sm hover:bg-gray-50 rounded">
      <div className="flex items-center gap-2">
        <MapPin className="w-3 h-3 text-gray-400" />
        <span>{station.city}</span>
        <span className="text-xs text-gray-400">{station.distance_km}km</span>
      </div>
      <div className="flex gap-3">
        <span className="text-xs">
          1h: <strong>{station.rain_1h_mm?.toFixed(1) || '0'}</strong>mm
        </span>
        <span className="text-xs">
          24h: <strong>{station.rain_24h_mm?.toFixed(1) || '0'}</strong>mm
        </span>
      </div>
    </div>
  )
}

export default function RainfallPanel({
  netatmo,
  infoclimat,
  forecast,
  loading,
}: RainfallPanelProps) {
  const [expanded, setExpanded] = useState(true)
  const [showAllStations, setShowAllStations] = useState(false)

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="animate-pulse space-y-3">
          <div className="h-4 bg-gray-200 rounded w-1/3"></div>
          <div className="h-16 bg-gray-200 rounded"></div>
        </div>
      </div>
    )
  }

  const totalStations = (netatmo?.stations_count || 0) + (infoclimat?.stations_found || 0)
  const lidoireStations = netatmo?.lidoire_stations || []
  const otherStations = netatmo?.stations || []

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between p-4 bg-indigo-50 border-b border-indigo-100 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2">
          <CloudRain className="w-5 h-5 text-indigo-600" />
          <h3 className="font-semibold text-indigo-800">Pluviometrie</h3>
          <span className="text-sm text-indigo-600">({totalStations} stations)</span>
        </div>
        {expanded ? (
          <ChevronUp className="w-5 h-5 text-indigo-600" />
        ) : (
          <ChevronDown className="w-5 h-5 text-indigo-600" />
        )}
      </div>

      {expanded && (
        <div className="p-4 space-y-4">
          {/* Summary cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div className="p-3 bg-gray-50 rounded-lg">
              <div className="text-xs text-gray-500 mb-1">Derniere heure</div>
              <div className={`text-xl font-bold ${getRainIntensityColor(netatmo?.rain_1h_avg_mm || 0)}`}>
                {netatmo?.rain_1h_avg_mm?.toFixed(1) || '0.0'} mm
              </div>
            </div>
            <div className="p-3 bg-gray-50 rounded-lg">
              <div className="text-xs text-gray-500 mb-1 flex items-center gap-1">
                24 heures
                <InfoTooltip {...TOOLTIPS.rain24h} />
              </div>
              <div className={`text-xl font-bold ${getRainIntensityColor(netatmo?.rain_24h_avg_mm || 0)}`}>
                {netatmo?.rain_24h_avg_mm?.toFixed(1) || '0.0'} mm
              </div>
            </div>
            <div className="p-3 bg-blue-50 rounded-lg">
              <div className="text-xs text-blue-600 mb-1 flex items-center gap-1">
                <Cloud className="w-3 h-3" />
                Prevu 24h
                <InfoTooltip {...TOOLTIPS.rainfallForecast} />
              </div>
              <div className="text-xl font-bold text-blue-700">
                {forecast?.forecast_24h_mm?.toFixed(1) || '0.0'} mm
              </div>
            </div>
            <div className="p-3 bg-blue-50 rounded-lg">
              <div className="text-xs text-blue-600 mb-1 flex items-center gap-1">
                <Cloud className="w-3 h-3" />
                Prevu 72h
                <InfoTooltip {...TOOLTIPS.rain72h} />
              </div>
              <div className="text-xl font-bold text-blue-700">
                {forecast?.forecast_72h_mm?.toFixed(1) || '0.0'} mm
              </div>
            </div>
          </div>

          {/* Data sources */}
          <div className="space-y-3">
            {/* Lidoire Basin Stations - Priority section */}
            {lidoireStations.length > 0 && (
              <div className="border-2 border-emerald-200 rounded-lg overflow-hidden">
                <div className="flex items-center justify-between p-3 bg-emerald-50">
                  <div className="flex items-center gap-2">
                    <Navigation className="w-4 h-4 text-emerald-600" />
                    <span className="font-medium text-sm text-emerald-800">Bassin Lidoire</span>
                    <span className="text-xs text-emerald-600">
                      {lidoireStations.length} stations sur le tracé
                    </span>
                  </div>
                </div>
                <div className="p-2 space-y-1">
                  {lidoireStations.map((station, idx) => (
                    <LidoireStationRow key={idx} station={station} />
                  ))}
                </div>
              </div>
            )}

            {/* Netatmo other stations */}
            {netatmo && (
              <div className="border rounded-lg overflow-hidden">
                <div className="flex items-center justify-between p-3 bg-gray-50">
                  <div className="flex items-center gap-2">
                    <div className={`w-2 h-2 rounded-full ${netatmo.available ? 'bg-green-500' : 'bg-red-500'}`} />
                    <span className="font-medium text-sm">Netatmo</span>
                    <InfoTooltip {...TOOLTIPS.netatmoSource} />
                    <span className="text-xs text-gray-500">
                      {netatmo.stations_count} stations
                    </span>
                  </div>
                  {netatmo.closest_station && (
                    <span className="text-xs text-gray-500">
                      Plus proche: {netatmo.closest_station.city} ({netatmo.closest_station.distance_km}km)
                    </span>
                  )}
                </div>

                {netatmo.available && otherStations.length > 0 && (
                  <div className="p-2">
                    <div className="space-y-1">
                      {(showAllStations ? otherStations : otherStations.slice(0, 5)).map(
                        (station, idx) => (
                          <StationRow key={idx} station={station} />
                        )
                      )}
                    </div>

                    {otherStations.length > 5 && (
                      <button
                        onClick={() => setShowAllStations(!showAllStations)}
                        className="w-full mt-2 py-1 text-xs text-indigo-600 hover:bg-indigo-50 rounded"
                      >
                        {showAllStations
                          ? 'Voir moins'
                          : `Voir les ${otherStations.length - 5} autres stations`}
                      </button>
                    )}
                  </div>
                )}

                {!netatmo.available && (
                  <div className="p-3 text-sm text-gray-500">
                    <a href="/api/auth/netatmo" className="text-indigo-600 hover:underline">
                      Connecter Netatmo
                    </a>
                  </div>
                )}
              </div>
            )}

            {/* Infoclimat section */}
            {infoclimat && (
              <div className="border rounded-lg overflow-hidden">
                <div className="flex items-center justify-between p-3 bg-gray-50">
                  <div className="flex items-center gap-2">
                    <div className={`w-2 h-2 rounded-full ${infoclimat.available ? 'bg-green-500' : 'bg-yellow-500'}`} />
                    <span className="font-medium text-sm">Infoclimat</span>
                    <InfoTooltip {...TOOLTIPS.infoclimatSource} />
                    <span className="text-xs text-gray-500">
                      {infoclimat.rainfall?.stations_count || infoclimat.stations_found || 0} stations
                    </span>
                  </div>
                  {infoclimat.rainfall?.available && infoclimat.rainfall.rain_24h_avg_mm != null && (
                    <span className="text-xs text-gray-500">
                      Moy 24h: <strong className={getRainIntensityColor(infoclimat.rainfall.rain_24h_avg_mm).split(' ')[0]}>
                        {infoclimat.rainfall.rain_24h_avg_mm.toFixed(1)}mm
                      </strong>
                    </span>
                  )}
                </div>

                {/* Rainfall observation stations (when API key works) */}
                {infoclimat.rainfall?.available && infoclimat.rainfall.stations && infoclimat.rainfall.stations.length > 0 && (
                  <div className="p-2 space-y-1">
                    {infoclimat.rainfall.stations.map((station, idx) => (
                      <div
                        key={idx}
                        className="flex items-center justify-between p-2 text-sm hover:bg-gray-50 rounded"
                      >
                        <div className="flex items-center gap-2">
                          <MapPin className="w-3 h-3 text-purple-400" />
                          <span>{station.station_name}</span>
                          <span className="text-xs text-gray-400">
                            {station.distance_km}km
                          </span>
                        </div>
                        <span className="text-xs">
                          <span className="text-gray-400">24h:</span>{' '}
                          <strong className={station.rain_24h_mm >= 10 ? 'text-blue-600' : station.rain_24h_mm >= 5 ? 'text-cyan-600' : station.rain_24h_mm > 0 ? 'text-sky-600' : 'text-gray-500'}>
                            {station.rain_24h_mm.toFixed(1)}
                          </strong>
                          <span className="text-gray-400">mm</span>
                        </span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Discovery stations (when no rainfall data) */}
                {!infoclimat.rainfall?.available && infoclimat.nearest_stations && infoclimat.nearest_stations.length > 0 && (
                  <div className="p-2 space-y-1">
                    {infoclimat.nearest_stations.slice(0, 3).map((station, idx) => (
                      <div
                        key={idx}
                        className="flex items-center justify-between p-2 text-sm hover:bg-gray-50 rounded"
                      >
                        <div className="flex items-center gap-2">
                          <MapPin className="w-3 h-3 text-gray-400" />
                          <span>{station.name}</span>
                          <span className="text-xs text-gray-400">
                            {station.distance_km}km
                          </span>
                        </div>
                        <span className="text-xs text-gray-400">{station.source}</span>
                      </div>
                    ))}
                  </div>
                )}

                {!infoclimat.available && (
                  <div className="p-3 text-sm text-amber-600">
                    API restreinte par IP
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Rain intensity legend */}
          <div className="flex items-center justify-center gap-4 text-xs text-gray-500 pt-2 border-t">
            <span className="flex items-center gap-1">
              <Droplets className="w-3 h-3 text-sky-500" /> Faible
            </span>
            <span className="flex items-center gap-1">
              <Droplets className="w-3 h-3 text-cyan-500" /> Moderee
            </span>
            <span className="flex items-center gap-1">
              <Droplets className="w-3 h-3 text-blue-500" /> Forte
            </span>
            <span className="flex items-center gap-1">
              <Droplets className="w-3 h-3 text-purple-500" /> Tres forte
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
