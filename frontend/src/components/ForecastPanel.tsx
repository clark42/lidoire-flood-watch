import { useState } from 'react'
import {
  Clock,
  ChevronDown,
  ChevronUp,
  CloudRain,
  Waves,
} from 'lucide-react'
import { format } from 'date-fns'
import { fr } from 'date-fns/locale'
import type { RiskLevel } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

interface ForecastPoint {
  timestamp: string
  hours_ahead: number
  score: number
  level: RiskLevel
  tide_coefficient: number
  rain_forecast_mm: number
}

interface ForecastData {
  generated_at: string
  hours_ahead: number
  forecasts: ForecastPoint[]
}

interface ForecastPanelProps {
  forecast: ForecastData | null
  loading?: boolean
}

const LEVEL_COLORS: Record<RiskLevel, string> = {
  green: 'bg-green-100 text-green-700 border-green-300',
  yellow: 'bg-yellow-100 text-yellow-700 border-yellow-300',
  orange: 'bg-orange-100 text-orange-700 border-orange-300',
  red: 'bg-red-100 text-red-700 border-red-300',
}

const LEVEL_BG: Record<RiskLevel, string> = {
  green: 'bg-green-500',
  yellow: 'bg-yellow-500',
  orange: 'bg-orange-500',
  red: 'bg-red-500',
}

export default function ForecastPanel({ forecast, loading }: ForecastPanelProps) {
  const [expanded, setExpanded] = useState(true)
  const [viewMode, setViewMode] = useState<'timeline' | 'table'>('timeline')

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="animate-pulse space-y-3">
          <div className="h-4 bg-gray-200 rounded w-1/3"></div>
          <div className="h-24 bg-gray-200 rounded"></div>
        </div>
      </div>
    )
  }

  if (!forecast || !forecast.forecasts.length) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="text-gray-500 text-center py-4">
          Previsions non disponibles
        </div>
      </div>
    )
  }

  // Find max risk in forecast period
  const maxRisk = forecast.forecasts.reduce(
    (max, f) => (f.score > max.score ? f : max),
    forecast.forecasts[0]
  )

  // Group forecasts by time periods
  const next6h = forecast.forecasts.filter((f) => f.hours_ahead <= 6)
  const next12h = forecast.forecasts.filter((f) => f.hours_ahead <= 12)
  const next24h = forecast.forecasts.filter((f) => f.hours_ahead <= 24)

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between p-4 bg-amber-50 border-b border-amber-100 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2">
          <Clock className="w-5 h-5 text-amber-600" />
          <h3 className="font-semibold text-amber-800">Previsions</h3>
          <InfoTooltip {...TOOLTIPS.forecastHeader} />
          <span className="text-sm text-amber-600">
            ({forecast.hours_ahead}h)
          </span>
        </div>
        <div className="flex items-center gap-3">
          {maxRisk.level !== 'green' && (
            <span className={`px-2 py-1 text-xs rounded border ${LEVEL_COLORS[maxRisk.level]}`}>
              Max: {maxRisk.score.toFixed(0)}% dans {maxRisk.hours_ahead}h
            </span>
          )}
          {expanded ? (
            <ChevronUp className="w-5 h-5 text-amber-600" />
          ) : (
            <ChevronDown className="w-5 h-5 text-amber-600" />
          )}
        </div>
      </div>

      {expanded && (
        <div className="p-4 space-y-4">
          {/* View mode toggle */}
          <div className="flex gap-2 text-sm">
            <button
              onClick={() => setViewMode('timeline')}
              className={`px-3 py-1 rounded ${
                viewMode === 'timeline' ? 'bg-amber-100 text-amber-700' : 'bg-gray-100'
              }`}
            >
              Timeline
            </button>
            <button
              onClick={() => setViewMode('table')}
              className={`px-3 py-1 rounded ${
                viewMode === 'table' ? 'bg-amber-100 text-amber-700' : 'bg-gray-100'
              }`}
            >
              Tableau
            </button>
          </div>

          {viewMode === 'timeline' ? (
            /* Timeline view */
            <div className="space-y-4">
              {/* Visual timeline */}
              <div className="relative">
                <div className="flex h-16 gap-px">
                  {forecast.forecasts.map((point, idx) => (
                    <div
                      key={idx}
                      className={`flex-1 ${LEVEL_BG[point.level]} opacity-80 hover:opacity-100 transition-opacity relative group`}
                      style={{ minWidth: '8px' }}
                    >
                      {/* Tooltip */}
                      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 hidden group-hover:block z-10">
                        <div className="bg-gray-900 text-white text-xs rounded p-2 whitespace-nowrap">
                          <div className="font-medium">
                            {format(new Date(point.timestamp), 'dd/MM HH:mm', { locale: fr })}
                          </div>
                          <div>Risque: {point.score.toFixed(0)}%</div>
                          <div>Coef: {point.tide_coefficient}</div>
                          <div>Pluie: {point.rain_forecast_mm.toFixed(1)}mm</div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Time labels */}
                <div className="flex justify-between mt-1 text-xs text-gray-500">
                  <span>Maintenant</span>
                  <span>+6h</span>
                  <span>+12h</span>
                  <span>+18h</span>
                  <span>+24h</span>
                </div>
              </div>

              {/* Summary cards */}
              <div className="grid grid-cols-3 gap-3">
                <SummaryCard
                  title="6 prochaines heures"
                  forecasts={next6h}
                />
                <SummaryCard
                  title="12 prochaines heures"
                  forecasts={next12h}
                />
                <SummaryCard
                  title="24 prochaines heures"
                  forecasts={next24h}
                />
              </div>
            </div>
          ) : (
            /* Table view */
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b">
                    <th className="text-left p-2">Heure</th>
                    <th className="text-center p-2">Risque</th>
                    <th className="text-center p-2">
                      <span className="inline-flex items-center gap-1">
                        Coef maree
                        <InfoTooltip {...TOOLTIPS.tideCoefficient} />
                      </span>
                    </th>
                    <th className="text-center p-2">Pluie prevue</th>
                  </tr>
                </thead>
                <tbody>
                  {forecast.forecasts.map((point, idx) => (
                    <tr key={idx} className="border-b hover:bg-gray-50">
                      <td className="p-2">
                        <div className="font-medium">
                          {format(new Date(point.timestamp), 'HH:mm', { locale: fr })}
                        </div>
                        <div className="text-xs text-gray-500">
                          +{point.hours_ahead}h
                        </div>
                      </td>
                      <td className="p-2 text-center">
                        <span
                          className={`inline-block px-2 py-1 rounded text-xs font-medium ${LEVEL_COLORS[point.level]}`}
                        >
                          {point.score.toFixed(0)}%
                        </span>
                      </td>
                      <td className="p-2 text-center">
                        <span className="flex items-center justify-center gap-1">
                          <Waves className="w-3 h-3 text-blue-500" />
                          {point.tide_coefficient}
                        </span>
                      </td>
                      <td className="p-2 text-center">
                        <span className="flex items-center justify-center gap-1">
                          <CloudRain className="w-3 h-3 text-indigo-500" />
                          {point.rain_forecast_mm.toFixed(1)} mm
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Legend */}
          <div className="flex items-center justify-center gap-4 text-xs text-gray-500 pt-2 border-t">
            <span className="flex items-center gap-1">
              <div className="w-3 h-3 rounded bg-green-500" /> Faible
            </span>
            <span className="flex items-center gap-1">
              <div className="w-3 h-3 rounded bg-yellow-500" /> Modere
            </span>
            <span className="flex items-center gap-1">
              <div className="w-3 h-3 rounded bg-orange-500" /> Eleve
            </span>
            <span className="flex items-center gap-1">
              <div className="w-3 h-3 rounded bg-red-500" /> Critique
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

function SummaryCard({
  title,
  forecasts,
}: {
  title: string
  forecasts: ForecastPoint[]
}) {
  if (!forecasts.length) return null

  const maxRisk = Math.max(...forecasts.map((f) => f.score))
  const maxLevel = forecasts.reduce(
    (max, f) => (f.score > max.score ? f : max),
    forecasts[0]
  ).level
  const totalRain = forecasts.reduce((sum, f) => sum + f.rain_forecast_mm, 0)
  const maxCoef = Math.max(...forecasts.map((f) => f.tide_coefficient))

  return (
    <div className={`p-3 rounded-lg border ${LEVEL_COLORS[maxLevel]}`}>
      <div className="text-xs opacity-75 mb-2">{title}</div>
      <div className="space-y-1">
        <div className="flex justify-between">
          <span className="text-xs">Risque max</span>
          <span className="font-bold">{maxRisk.toFixed(0)}%</span>
        </div>
        <div className="flex justify-between">
          <span className="text-xs">Pluie cumulee</span>
          <span className="font-medium">{totalRain.toFixed(1)}mm</span>
        </div>
        <div className="flex justify-between">
          <span className="text-xs">Coef max</span>
          <span className="font-medium">{maxCoef}</span>
        </div>
      </div>
    </div>
  )
}
