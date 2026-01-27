import { useState } from 'react'
import {
  TrendingUp,
  TrendingDown,
  Minus,
  ChevronDown,
  ChevronUp,
  MapPin,
  Clock,
  Activity,
} from 'lucide-react'
import { format } from 'date-fns'
import { fr } from 'date-fns/locale'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

interface StationData {
  station_id: string
  station_name: string
  river: string
  current_level: {
    timestamp: string
    height_m: number
  }
  tendency: 'hausse' | 'stable' | 'baisse'
  rate_cm_per_hour: number
  distance_km: number
  delay_hours: number
  weight: number
  threshold_yellow: number
  threshold_orange: number
  threshold_red: number
}

interface StationsPanelProps {
  stations: StationData[]
  selectedStation: string | null
  onSelectStation: (stationId: string) => void
  loading?: boolean
}

function TendencyIcon({ tendency }: { tendency: string }) {
  if (tendency === 'hausse') return <TrendingUp className="w-4 h-4 text-red-500" />
  if (tendency === 'baisse') return <TrendingDown className="w-4 h-4 text-green-500" />
  return <Minus className="w-4 h-4 text-gray-400" />
}

function getLevelColor(height: number, thresholds: { yellow: number; orange: number; red: number }) {
  if (height >= thresholds.red) return 'text-red-600 bg-red-50'
  if (height >= thresholds.orange) return 'text-orange-600 bg-orange-50'
  if (height >= thresholds.yellow) return 'text-yellow-600 bg-yellow-50'
  return 'text-green-600 bg-green-50'
}

function getLevelBarWidth(height: number, thresholds: { yellow: number; orange: number; red: number }) {
  const maxLevel = thresholds.red * 1.2
  return Math.min(100, (height / maxLevel) * 100)
}

export default function StationsPanel({
  stations,
  selectedStation,
  onSelectStation,
  loading,
}: StationsPanelProps) {
  const [expanded, setExpanded] = useState(true)
  const [groupByRiver, setGroupByRiver] = useState(true)

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="animate-pulse space-y-3">
          <div className="h-4 bg-gray-200 rounded w-1/3"></div>
          <div className="h-20 bg-gray-200 rounded"></div>
          <div className="h-20 bg-gray-200 rounded"></div>
        </div>
      </div>
    )
  }

  // Group stations by river
  const stationsByRiver = stations.reduce((acc, station) => {
    if (!acc[station.river]) acc[station.river] = []
    acc[station.river].push(station)
    return acc
  }, {} as Record<string, StationData[]>)

  const riverOrder = ['Dordogne', 'Isle', 'Dronne']

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between p-4 bg-cyan-50 border-b border-cyan-100 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2">
          <Activity className="w-5 h-5 text-cyan-600" />
          <h3 className="font-semibold text-cyan-800">Stations hydrometriques</h3>
          <InfoTooltip {...TOOLTIPS.stationsHeader} />
          <span className="text-sm text-cyan-600">({stations.length} stations)</span>
        </div>
        {expanded ? (
          <ChevronUp className="w-5 h-5 text-cyan-600" />
        ) : (
          <ChevronDown className="w-5 h-5 text-cyan-600" />
        )}
      </div>

      {expanded && (
        <div className="p-4 space-y-4">
          {/* View toggle */}
          <div className="flex gap-2 text-sm">
            <button
              onClick={() => setGroupByRiver(true)}
              className={`px-3 py-1 rounded ${
                groupByRiver ? 'bg-cyan-100 text-cyan-700' : 'bg-gray-100'
              }`}
            >
              Par riviere
            </button>
            <button
              onClick={() => setGroupByRiver(false)}
              className={`px-3 py-1 rounded ${
                !groupByRiver ? 'bg-cyan-100 text-cyan-700' : 'bg-gray-100'
              }`}
            >
              Par distance
            </button>
          </div>

          {groupByRiver ? (
            // Grouped by river
            <div className="space-y-4">
              {riverOrder.map((river) => {
                const riverStations = stationsByRiver[river]
                if (!riverStations) return null

                return (
                  <div key={river} className="space-y-2">
                    <h4 className="text-sm font-medium text-gray-500 uppercase tracking-wide">
                      {river}
                    </h4>
                    {riverStations.map((station) => (
                      <StationCard
                        key={station.station_id}
                        station={station}
                        isSelected={selectedStation === station.station_id}
                        onSelect={() => onSelectStation(station.station_id)}
                      />
                    ))}
                  </div>
                )
              })}
            </div>
          ) : (
            // Sorted by distance
            <div className="space-y-2">
              {[...stations]
                .sort((a, b) => a.distance_km - b.distance_km)
                .map((station) => (
                  <StationCard
                    key={station.station_id}
                    station={station}
                    isSelected={selectedStation === station.station_id}
                    onSelect={() => onSelectStation(station.station_id)}
                  />
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function StationCard({
  station,
  isSelected,
  onSelect,
}: {
  station: StationData
  isSelected: boolean
  onSelect: () => void
}) {
  const thresholds = {
    yellow: station.threshold_yellow,
    orange: station.threshold_orange,
    red: station.threshold_red,
  }
  const levelColor = getLevelColor(station.current_level.height_m, thresholds)
  const barWidth = getLevelBarWidth(station.current_level.height_m, thresholds)

  return (
    <div
      onClick={onSelect}
      className={`group p-3 rounded-lg border-2 cursor-pointer transition-all ${
        isSelected
          ? 'border-cyan-500 bg-cyan-50'
          : 'border-gray-200 hover:border-gray-300 bg-white'
      }`}
    >
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium">{station.station_name}</span>
            <span className="text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded">
              {station.river}
            </span>
          </div>
          <div className="flex items-center gap-3 mt-1 text-xs text-gray-500">
            <span className="flex items-center gap-1">
              <MapPin className="w-3 h-3" />
              {station.distance_km} km
            </span>
            {station.delay_hours > 0 && (
              <span className="flex items-center gap-1">
                <Clock className="w-3 h-3" />
                +{station.delay_hours}h
                <InfoTooltip {...TOOLTIPS.delayHours} />
              </span>
            )}
          </div>
        </div>

        <div className="text-right">
          <div className={`text-lg font-bold px-2 py-1 rounded ${levelColor}`}>
            {station.current_level.height_m.toFixed(2)} m
          </div>
          <div className="flex items-center justify-end gap-1 mt-1">
            <TendencyIcon tendency={station.tendency} />
            <span
              className={`text-xs ${
                station.tendency === 'hausse'
                  ? 'text-red-500'
                  : station.tendency === 'baisse'
                  ? 'text-green-500'
                  : 'text-gray-500'
              }`}
            >
              {station.rate_cm_per_hour > 0 ? '+' : ''}
              {station.rate_cm_per_hour.toFixed(1)} cm/h
            </span>
            <InfoTooltip {...TOOLTIPS.rateOfRise} />
          </div>
        </div>
      </div>

      {/* Level bar */}
      <div className="mt-2 h-2 bg-gray-100 rounded-full overflow-hidden">
        <div className="h-full flex">
          <div
            className="bg-green-400 transition-all"
            style={{ width: `${Math.min(barWidth, (thresholds.yellow / (thresholds.red * 1.2)) * 100)}%` }}
          />
          {barWidth > (thresholds.yellow / (thresholds.red * 1.2)) * 100 && (
            <div
              className="bg-yellow-400 transition-all"
              style={{
                width: `${Math.min(
                  barWidth - (thresholds.yellow / (thresholds.red * 1.2)) * 100,
                  ((thresholds.orange - thresholds.yellow) / (thresholds.red * 1.2)) * 100
                )}%`,
              }}
            />
          )}
          {barWidth > (thresholds.orange / (thresholds.red * 1.2)) * 100 && (
            <div
              className="bg-orange-400 transition-all"
              style={{
                width: `${Math.min(
                  barWidth - (thresholds.orange / (thresholds.red * 1.2)) * 100,
                  ((thresholds.red - thresholds.orange) / (thresholds.red * 1.2)) * 100
                )}%`,
              }}
            />
          )}
          {barWidth > 83 && (
            <div
              className="bg-red-500 transition-all"
              style={{ width: `${barWidth - 83}%` }}
            />
          )}
        </div>
      </div>

      {/* Thresholds markers */}
      <div className="relative h-1 mt-1">
        <div
          className="absolute w-0.5 h-2 bg-yellow-500 -top-1"
          style={{ left: `${(thresholds.yellow / (thresholds.red * 1.2)) * 100}%` }}
        />
        <div
          className="absolute w-0.5 h-2 bg-orange-500 -top-1"
          style={{ left: `${(thresholds.orange / (thresholds.red * 1.2)) * 100}%` }}
        />
        <div
          className="absolute w-0.5 h-2 bg-red-500 -top-1"
          style={{ left: `${(thresholds.red / (thresholds.red * 1.2)) * 100}%` }}
        />
      </div>

      {/* Threshold labels - visible on hover */}
      <div className="flex items-center gap-2 mt-1 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity">
        <span className="text-yellow-600 font-medium">J:{thresholds.yellow}m</span>
        <span className="text-orange-600 font-medium">O:{thresholds.orange}m</span>
        <span className="text-red-600 font-medium">R:{thresholds.red}m</span>
        <InfoTooltip {...TOOLTIPS.thresholds} />
      </div>

      {/* Timestamp */}
      <div className="mt-2 text-xs text-gray-400">
        {format(new Date(station.current_level.timestamp), "HH:mm", { locale: fr })}
      </div>
    </div>
  )
}
