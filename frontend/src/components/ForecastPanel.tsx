import { useState, useEffect, useCallback } from 'react'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'
import { Clock, CloudRain, Waves, TrendingUp, AlertTriangle, RefreshCw } from 'lucide-react'
import { getForecast } from '../api'
import type { ForecastData, ForecastPoint, RiskLevel } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

const LEVEL_COLORS: Record<RiskLevel, string> = {
  green: '#22c55e',
  yellow: '#eab308',
  orange: '#f97316',
  red: '#ef4444',
}

const LEVEL_BG_COLORS: Record<RiskLevel, string> = {
  green: 'bg-green-100 text-green-800 border-green-300',
  yellow: 'bg-yellow-100 text-yellow-800 border-yellow-300',
  orange: 'bg-orange-100 text-orange-800 border-orange-300',
  red: 'bg-red-100 text-red-800 border-red-300',
}

interface ForecastPanelProps {
  hours?: number
  autoRefresh?: boolean
  refreshInterval?: number // ms
}

function formatHour(timestamp: string): string {
  const d = new Date(timestamp)
  const day = d.toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric' })
  const hour = d.getHours().toString().padStart(2, '0') + 'h'
  return `${day} ${hour}`
}

function CustomTooltip({ active, payload }: { active?: boolean; payload?: { payload: ForecastPoint }[] }) {
  if (!active || !payload || !payload.length) return null

  const data = payload[0].payload as ForecastPoint
  const levelColor = LEVEL_COLORS[data.level]

  return (
    <div className="bg-white border rounded-lg shadow-lg p-3 text-sm">
      <div className="font-medium mb-2">{formatHour(data.timestamp)}</div>
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <div
            className="w-3 h-3 rounded-full"
            style={{ backgroundColor: levelColor }}
          />
          <span className="font-bold">{data.score.toFixed(0)}/100</span>
          <span className="text-gray-500 capitalize">({data.level})</span>
        </div>
        <div className="flex items-center gap-2 text-gray-600">
          <CloudRain className="w-4 h-4" />
          <span>24h: {data.rain_24h_mm.toFixed(1)} mm</span>
        </div>
        <div className="flex items-center gap-2 text-gray-600">
          <Waves className="w-4 h-4" />
          <span>Coef: {data.tide_coefficient}</span>
          <span className="text-xs">
            (PM {data.hours_to_high_tide > 0 ? '+' : ''}{data.hours_to_high_tide.toFixed(1)}h)
          </span>
        </div>
        <div className="flex items-center gap-2 text-gray-600">
          <TrendingUp className="w-4 h-4" />
          <span>Niveau proj.: {data.projected_level_m.toFixed(2)} m</span>
        </div>
      </div>
    </div>
  )
}

export default function ForecastPanel({
  hours = 72,
  autoRefresh = true,
  refreshInterval = 5 * 60 * 1000, // 5 min
}: ForecastPanelProps) {
  const [data, setData] = useState<ForecastData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const result = await getForecast(hours)
      setData(result)
      setLastUpdated(new Date())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erreur de chargement')
    } finally {
      setLoading(false)
    }
  }, [hours])

  useEffect(() => {
    fetchData()

    if (autoRefresh) {
      // Skip refreshes while the tab is hidden
      const interval = setInterval(() => {
        if (!document.hidden) fetchData()
      }, refreshInterval)
      return () => clearInterval(interval)
    }
  }, [fetchData, autoRefresh, refreshInterval])

  if (loading && !data) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="animate-pulse flex items-center gap-2">
          <div className="w-5 h-5 bg-gray-200 rounded" />
          <div className="h-4 bg-gray-200 rounded w-32" />
        </div>
        <div className="h-48 bg-gray-100 rounded mt-4" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex items-center gap-2 text-red-600">
          <AlertTriangle className="w-5 h-5" />
          <span>{error}</span>
        </div>
        <button
          onClick={fetchData}
          className="mt-2 text-sm text-blue-600 hover:underline flex items-center gap-1"
        >
          <RefreshCw className="w-4 h-4" /> Reessayer
        </button>
      </div>
    )
  }

  if (!data) return null

  // Prepare chart data with color gradient based on level
  const chartData = data.forecasts.map((f) => ({
    ...f,
    fill: LEVEL_COLORS[f.level],
  }))

  // Find critical periods summary
  const criticalCount = data.critical_periods.length
  const redCount = data.critical_periods.filter((p) => p.level === 'red').length
  const orangeCount = data.critical_periods.filter((p) => p.level === 'orange').length

  return (
    <div className="bg-white rounded-lg shadow">
      {/* Header */}
      <div className="p-4 border-b">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Clock className="w-5 h-5 text-blue-500" />
            <h3 className="font-medium text-gray-700">
              Previsions {hours >= 168 ? '7 jours' : `${hours}h`}
            </h3>
            {TOOLTIPS.forecast && <InfoTooltip {...TOOLTIPS.forecast} />}
          </div>
          <div className="flex items-center gap-3">
            {lastUpdated && (
              <span className="text-xs text-gray-400">
                MAJ {lastUpdated.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}
              </span>
            )}
            <button
              onClick={fetchData}
              disabled={loading}
              className="p-1 rounded hover:bg-gray-100 disabled:opacity-50"
              title="Rafraichir"
            >
              <RefreshCw className={`w-4 h-4 text-gray-500 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>

        {/* Summary banner */}
        <div
          className={`mt-3 p-3 rounded-lg border ${LEVEL_BG_COLORS[data.max_level]}`}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              {data.max_level === 'red' && <AlertTriangle className="w-5 h-5" />}
              <span className="font-medium">{data.summary}</span>
            </div>
            <span className="text-lg font-bold">{data.max_risk}/100</span>
          </div>
          {criticalCount > 0 && (
            <div className="mt-1 text-sm opacity-80">
              {redCount > 0 && <span className="mr-3">{redCount}h en rouge</span>}
              {orangeCount > 0 && <span>{orangeCount}h en orange</span>}
            </div>
          )}
        </div>
      </div>

      {/* Chart */}
      <div className="p-4">
        <ResponsiveContainer width="100%" height={200}>
          <AreaChart data={chartData} margin={{ top: 10, right: 10, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="riskGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
            <XAxis
              dataKey="timestamp"
              tickFormatter={(ts) => {
                const d = new Date(ts)
                return d.getHours() === 0
                  ? d.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit' })
                  : d.getHours() + 'h'
              }}
              tick={{ fontSize: 10 }}
              interval="preserveStartEnd"
              minTickGap={40}
            />
            <YAxis
              domain={[0, 100]}
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => v.toString()}
              width={30}
            />
            {/* Threshold reference lines */}
            <ReferenceLine y={15} stroke="#22c55e" strokeDasharray="3 3" />
            <ReferenceLine y={35} stroke="#eab308" strokeDasharray="3 3" />
            <ReferenceLine y={55} stroke="#f97316" strokeDasharray="3 3" />
            <Tooltip content={<CustomTooltip />} />
            <Area
              type="monotone"
              dataKey="score"
              stroke="#3b82f6"
              strokeWidth={2}
              fill="url(#riskGradient)"
              dot={false}
              activeDot={{ r: 4, fill: '#3b82f6' }}
            />
          </AreaChart>
        </ResponsiveContainer>

        {/* Legend */}
        <div className="flex items-center justify-center gap-4 mt-2 text-xs text-gray-500">
          <div className="flex items-center gap-1">
            <div className="w-3 h-1 bg-green-500 rounded" />
            <span>&lt;15</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="w-3 h-1 bg-yellow-500 rounded" />
            <span>15-35</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="w-3 h-1 bg-orange-500 rounded" />
            <span>35-55</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="w-3 h-1 bg-red-500 rounded" />
            <span>&ge;55</span>
          </div>
        </div>
      </div>

      {/* Data sources footer */}
      <div className="px-4 pb-3 text-xs text-gray-400">
        Sources: {data.data_sources.rain?.source || data.data_sources.rain?.status || '?'}
        {' | '}{data.data_sources.tide?.port || data.data_sources.tide?.status || '?'}
        {' | '}{data.data_sources.river?.stations ? `${data.data_sources.river.stations} stations` : data.data_sources.river?.status || '?'}
        {Object.values(data.data_sources).some(s => s?.status === 'error') && (
          <span className="ml-2 text-red-400">
            (sources degradees)
          </span>
        )}
      </div>
    </div>
  )
}
