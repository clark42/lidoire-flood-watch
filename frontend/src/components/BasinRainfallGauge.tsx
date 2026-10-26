import { CloudRain } from 'lucide-react'
import { AreaChart, Area, ResponsiveContainer, YAxis } from 'recharts'
import type { BasinRainfallData } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

interface BasinRainfallGaugeProps {
  data: BasinRainfallData | null
  loading?: boolean
}

const POSITION_CONFIG: Record<string, { label: string; color: string; bg: string }> = {
  amont: { label: 'Amont', color: 'bg-emerald-500', bg: 'bg-emerald-50' },
  milieu: { label: 'Milieu', color: 'bg-blue-500', bg: 'bg-blue-50' },
  aval: { label: 'Aval', color: 'bg-purple-500', bg: 'bg-purple-50' },
}

function getThresholdColor(mm: number): string {
  if (mm >= 50) return 'text-red-600'
  if (mm >= 30) return 'text-orange-500'
  if (mm >= 10) return 'text-yellow-600'
  return 'text-green-600'
}

export default function BasinRainfallGauge({ data, loading }: BasinRainfallGaugeProps) {
  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="animate-pulse space-y-3">
          <div className="h-4 bg-gray-200 rounded w-2/3" />
          <div className="h-8 bg-gray-200 rounded w-1/3" />
          <div className="h-4 bg-gray-200 rounded w-1/2" />
          <div className="space-y-2">
            <div className="h-3 bg-gray-200 rounded" />
            <div className="h-3 bg-gray-200 rounded" />
            <div className="h-3 bg-gray-200 rounded" />
          </div>
          <div className="h-12 bg-gray-200 rounded" />
        </div>
      </div>
    )
  }

  // available === false: no Netatmo basin station reported, the 0.0 values
  // are placeholders and must not be shown as measurements.
  if (!data || data.available === false) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex items-center gap-2 mb-2">
          <CloudRain className="w-5 h-5 text-gray-400" />
          <h3 className="font-semibold text-gray-600">Pluie Bassin Lidoire</h3>
          <InfoTooltip {...TOOLTIPS.basinHeader} />
        </div>
        <p className="text-sm text-gray-500">
          {data ? 'Netatmo indisponible : aucune station du bassin ne remonte de donnees.' : 'Pas de donnees'}
        </p>
        {data && (
          <p className="text-xs text-gray-400 mt-1">
            Pluie du modele : voir Pluie (modele), Open-Meteo et Infoclimat.
          </p>
        )}
      </div>
    )
  }

  const mainValue = data.weighted_average_24h_mm
  const colorClass = getThresholdColor(mainValue)

  const positions = Object.entries(data.by_position)
  const maxPositionAvg = Math.max(...positions.map(([, v]) => v.avg), 1)

  return (
    <div className="bg-white rounded-lg shadow p-4">
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <CloudRain className="w-5 h-5 text-indigo-600" />
        <h3 className="font-semibold text-gray-700">Pluie Bassin Lidoire</h3>
        <InfoTooltip {...TOOLTIPS.basinHeader} />
      </div>

      {/* Main value */}
      <div className="mb-1">
        <span className={`text-2xl font-bold ${colorClass}`}>
          {mainValue.toFixed(1)}
        </span>
        <span className="text-sm text-gray-500 ml-1">mm/24h</span>
      </div>

      {/* Secondary value */}
      <div className="text-sm text-gray-500 mb-4">
        Derniere heure : {data.basin_average_1h_mm.toFixed(1)} mm
      </div>

      {/* Position bars */}
      <div className="space-y-2 mb-4">
        {positions.map(([key, posData]) => {
          const config = POSITION_CONFIG[key] || {
            label: key,
            color: 'bg-gray-400',
            bg: 'bg-gray-50',
          }
          const barWidth = maxPositionAvg > 0
            ? Math.max((posData.avg / maxPositionAvg) * 100, 2)
            : 2

          return (
            <div key={key} className="flex items-center gap-2">
              <span className="text-xs text-gray-500 w-12 text-right">
                {config.label}
              </span>
              <span className="text-xs font-medium w-14 text-right">
                {posData.count > 0 ? `${posData.avg.toFixed(1)} mm` : 'n/d'}
              </span>
              <div className={`flex-1 h-2 rounded-full ${config.bg}`}>
                <div
                  className={`h-full rounded-full ${config.color} transition-all`}
                  style={{ width: `${barWidth}%` }}
                />
              </div>
            </div>
          )
        })}
      </div>

      {/* Sparkline */}
      {data.sparkline_24h && data.sparkline_24h.length > 0 && (
        <div className="w-full h-[50px]">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data.sparkline_24h}>
              <defs>
                <linearGradient id="basinSparkGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                </linearGradient>
              </defs>
              <YAxis domain={['dataMin', 'dataMax']} hide />
              <Area
                type="monotone"
                dataKey="value"
                stroke="#3b82f6"
                strokeWidth={1.5}
                fill="url(#basinSparkGradient)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}
