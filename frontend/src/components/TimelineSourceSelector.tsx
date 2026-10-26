import { useMemo } from 'react'
import { Check, Droplets, Waves, AlertTriangle, Anchor, Sprout, Wind, Radio } from 'lucide-react'
import type { RiverStationInfo, RainfallStationInfo } from '../types'

interface TimelineSourceSelectorProps {
  riverStations: RiverStationInfo[]
  rainfallStations: RainfallStationInfo[]
  selectedRiverStations: string[]
  selectedRainfallStations: string[]
  onRiverStationToggle: (stationId: string) => void
  onRainfallStationToggle: (stationId: string) => void
  onSelectAllRivers: () => void
  onSelectAllRainfall: () => void
  onClearAll: () => void
  tideEnabled: boolean
  riskEnabled: boolean
  onTideToggle: () => void
  onRiskToggle: () => void
  riskShadowEnabled?: boolean
  onRiskShadowToggle?: () => void
  soilEnabled?: boolean
  onSoilToggle?: () => void
  infoclimatEnabled?: boolean
  onInfoclimatToggle?: () => void
  windEnabled?: boolean
  onWindToggle?: () => void
  forecastEnabled?: boolean
  onForecastToggle?: () => void
}

export default function TimelineSourceSelector({
  riverStations,
  rainfallStations,
  selectedRiverStations,
  selectedRainfallStations,
  onRiverStationToggle,
  onRainfallStationToggle,
  onSelectAllRivers,
  onSelectAllRainfall,
  onClearAll,
  tideEnabled,
  riskEnabled,
  onTideToggle,
  onRiskToggle,
  riskShadowEnabled,
  onRiskShadowToggle,
  soilEnabled,
  onSoilToggle,
  infoclimatEnabled,
  onInfoclimatToggle,
  windEnabled,
  onWindToggle,
  forecastEnabled,
  onForecastToggle,
}: TimelineSourceSelectorProps) {
  // Group river stations by river
  const stationsByRiver = useMemo(() => {
    const groups: Record<string, RiverStationInfo[]> = {}
    for (const station of riverStations) {
      if (!groups[station.river]) {
        groups[station.river] = []
      }
      groups[station.river].push(station)
    }
    return groups
  }, [riverStations])

  // Group rainfall stations by position
  const stationsByPosition = useMemo(() => {
    const groups: Record<string, RainfallStationInfo[]> = {
      amont: [],
      milieu: [],
      aval: [],
    }
    for (const station of rainfallStations) {
      if (groups[station.position]) {
        groups[station.position].push(station)
      }
    }
    return groups
  }, [rainfallStations])

  const positionLabels: Record<string, string> = {
    amont: 'Amont',
    milieu: 'Milieu',
    aval: 'Aval',
  }

  return (
    <div className="space-y-4">
      {/* Header with quick actions */}
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-gray-700">
          Sources a afficher
        </span>
        <div className="flex gap-2">
          <button
            onClick={onSelectAllRivers}
            className="text-xs px-2 py-1 bg-cyan-50 text-cyan-600 rounded hover:bg-cyan-100"
          >
            Toutes les rivieres
          </button>
          <button
            onClick={onSelectAllRainfall}
            className="text-xs px-2 py-1 bg-green-50 text-green-600 rounded hover:bg-green-100"
          >
            Toute la pluie
          </button>
          <button
            onClick={onClearAll}
            className="text-xs px-2 py-1 bg-gray-100 text-gray-600 rounded hover:bg-gray-200"
          >
            Effacer
          </button>
        </div>
      </div>

      {/* River Stations Section */}
      <div className="border rounded-lg p-3 bg-cyan-50/30">
        <div className="flex items-center gap-2 mb-2">
          <Waves className="w-4 h-4 text-cyan-600" />
          <span className="text-sm font-medium text-cyan-700">
            Niveaux rivieres (Hub'Eau)
          </span>
        </div>
        <div className="space-y-2">
          {Object.entries(stationsByRiver).map(([river, stations]) => (
            <div key={river} className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-gray-500 w-20">
                {river}:
              </span>
              {stations.map((station) => {
                const isSelected = selectedRiverStations.includes(station.id)
                return (
                  <button
                    key={station.id}
                    onClick={() => onRiverStationToggle(station.id)}
                    className={`flex items-center gap-1 px-2 py-1 text-xs rounded-full border transition-all ${
                      isSelected
                        ? 'border-2'
                        : 'border-gray-200 bg-white hover:bg-gray-50'
                    }`}
                    style={
                      isSelected
                        ? {
                            backgroundColor: `${station.color}20`,
                            borderColor: station.color,
                            color: station.color,
                          }
                        : undefined
                    }
                  >
                    {isSelected && <Check className="w-3 h-3" />}
                    {station.name}
                    <span className="opacity-60">
                      ({station.distance_km}km)
                    </span>
                  </button>
                )
              })}
            </div>
          ))}
        </div>
      </div>

      {/* Rainfall Stations Section */}
      <div className="border rounded-lg p-3 bg-green-50/30">
        <div className="flex items-center gap-2 mb-2">
          <Droplets className="w-4 h-4 text-green-600" />
          <span className="text-sm font-medium text-green-700">
            Pluviometrie (Netatmo Lidoire)
          </span>
        </div>
        <div className="space-y-2">
          {Object.entries(positionLabels).map(([position, label]) => {
            const stations = stationsByPosition[position]
            if (!stations || stations.length === 0) return null
            return (
              <div key={position} className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-gray-500 w-20">
                  {label}:
                </span>
                {stations.map((station) => {
                  const isSelected = selectedRainfallStations.includes(
                    station.id
                  )
                  return (
                    <button
                      key={station.id}
                      onClick={() => onRainfallStationToggle(station.id)}
                      className={`flex items-center gap-1 px-2 py-1 text-xs rounded-full border transition-all ${
                        isSelected
                          ? 'border-2'
                          : 'border-gray-200 bg-white hover:bg-gray-50'
                      }`}
                      style={
                        isSelected
                          ? {
                              backgroundColor: `${station.color}20`,
                              borderColor: station.color,
                              color: station.color,
                            }
                          : undefined
                      }
                    >
                      {isSelected && <Check className="w-3 h-3" />}
                      {station.name}
                    </button>
                  )
                })}
              </div>
            )
          })}
        </div>
      </div>

      {/* Tide & Risk Score Toggles */}
      <div className="flex flex-wrap gap-3">
        <button
          onClick={onTideToggle}
          className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-all ${
            tideEnabled
              ? 'bg-blue-50 border-blue-300 text-blue-700 border-2'
              : 'border-gray-200 bg-white hover:bg-gray-50 text-gray-600'
          }`}
        >
          <Anchor className="w-4 h-4" />
          {tideEnabled && <Check className="w-3 h-3" />}
          Maree (Libourne)
        </button>

        <button
          onClick={onRiskToggle}
          className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-all ${
            riskEnabled
              ? 'bg-amber-50 border-amber-300 text-amber-700 border-2'
              : 'border-gray-200 bg-white hover:bg-gray-50 text-gray-600'
          }`}
        >
          <AlertTriangle className="w-4 h-4" />
          {riskEnabled && <Check className="w-3 h-3" />}
          Score de risque
        </button>

        {onRiskShadowToggle && (
          <button
            onClick={onRiskShadowToggle}
            title="Modele shadow 3.3-gate (v3.3 x gate de remous Pessac), stocke pour comparaison, sans effet sur le score affiche"
            className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-all ${
              riskShadowEnabled
                ? 'bg-teal-50 border-teal-300 text-teal-700 border-2'
                : 'border-gray-200 bg-white hover:bg-gray-50 text-gray-600'
            }`}
          >
            <AlertTriangle className="w-4 h-4" />
            {riskShadowEnabled && <Check className="w-3 h-3" />}
            Shadow + remous
          </button>
        )}

        {onSoilToggle && (
          <button
            onClick={onSoilToggle}
            className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-all ${
              soilEnabled
                ? 'bg-violet-50 border-violet-300 text-violet-700 border-2'
                : 'border-gray-200 bg-white hover:bg-gray-50 text-gray-600'
            }`}
          >
            <Sprout className="w-4 h-4" />
            {soilEnabled && <Check className="w-3 h-3" />}
            Conditions sol
          </button>
        )}

        {onInfoclimatToggle && (
          <button
            onClick={onInfoclimatToggle}
            className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-all ${
              infoclimatEnabled
                ? 'bg-red-50 border-red-300 text-red-700 border-2'
                : 'border-gray-200 bg-white hover:bg-gray-50 text-gray-600'
            }`}
          >
            <Radio className="w-4 h-4" />
            {infoclimatEnabled && <Check className="w-3 h-3" />}
            Infoclimat
          </button>
        )}

        {onWindToggle && (
          <button
            onClick={onWindToggle}
            className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-all ${
              windEnabled
                ? 'bg-slate-100 border-slate-400 text-slate-700 border-2'
                : 'border-gray-200 bg-white hover:bg-gray-50 text-gray-600'
            }`}
          >
            <Wind className="w-4 h-4" />
            {windEnabled && <Check className="w-3 h-3" />}
            Vent
          </button>
        )}

        {onForecastToggle && (
          <button
            onClick={onForecastToggle}
            className={`flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-all ${
              forecastEnabled
                ? 'bg-purple-50 border-purple-300 text-purple-700 border-2'
                : 'border-gray-200 bg-white hover:bg-gray-50 text-gray-600'
            }`}
          >
            <AlertTriangle className="w-4 h-4" />
            {forecastEnabled && <Check className="w-3 h-3" />}
            Previsions 7j
          </button>
        )}
      </div>

      {/* Selection summary */}
      <div className="text-xs text-gray-500 flex items-center gap-4">
        <span>
          <span className="font-medium text-cyan-600">
            {selectedRiverStations.length}
          </span>{' '}
          station{selectedRiverStations.length > 1 ? 's' : ''} riviere
        </span>
        <span>
          <span className="font-medium text-green-600">
            {selectedRainfallStations.length}
          </span>{' '}
          station{selectedRainfallStations.length > 1 ? 's' : ''} pluie
        </span>
        {tideEnabled && (
          <span className="font-medium text-blue-600">+ maree</span>
        )}
        {riskEnabled && (
          <span className="font-medium text-amber-600">+ risque</span>
        )}
        {soilEnabled && (
          <span className="font-medium text-violet-600">+ sol</span>
        )}
        {infoclimatEnabled && (
          <span className="font-medium text-red-600">+ infoclimat</span>
        )}
        {windEnabled && (
          <span className="font-medium text-slate-600">+ vent</span>
        )}
        {forecastEnabled && (
          <span className="font-medium text-purple-600">+ previsions</span>
        )}
      </div>
    </div>
  )
}
