import { useState, useEffect } from 'react'
import { format } from 'date-fns'
import { fr } from 'date-fns/locale'
import {
  Droplets,
  Waves,
  TrendingUp,
  TrendingDown,
  Minus,
  Clock,
  AlertTriangle,
  RefreshCw,
} from 'lucide-react'
import { getRealtimeData } from '../api'
import type { RealtimeData, RiskLevel } from '../types'
import RiskGauge from '../components/RiskGauge'
import DataCard from '../components/DataCard'

const RISK_COLORS: Record<RiskLevel, string> = {
  green: 'bg-green-500',
  yellow: 'bg-yellow-500',
  orange: 'bg-orange-500',
  red: 'bg-red-500',
}

const RISK_BG_COLORS: Record<RiskLevel, string> = {
  green: 'bg-green-50 border-green-200',
  yellow: 'bg-yellow-50 border-yellow-200',
  orange: 'bg-orange-50 border-orange-200',
  red: 'bg-red-50 border-red-200',
}

function TendencyIcon({ tendency }: { tendency: string | null }) {
  if (tendency === 'hausse') return <TrendingUp className="w-4 h-4 text-red-500" />
  if (tendency === 'baisse') return <TrendingDown className="w-4 h-4 text-green-500" />
  return <Minus className="w-4 h-4 text-gray-400" />
}

export default function Dashboard() {
  const [data, setData] = useState<RealtimeData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)

  const fetchData = async () => {
    try {
      setLoading(true)
      const result = await getRealtimeData()
      setData(result)
      setLastUpdate(new Date())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Erreur de chargement')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 60000) // Refresh every minute
    return () => clearInterval(interval)
  }, [])

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-6 text-center">
        <AlertTriangle className="w-12 h-12 text-red-500 mx-auto mb-4" />
        <p className="text-red-700">{error}</p>
        <button
          onClick={fetchData}
          className="mt-4 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
        >
          Réessayer
        </button>
      </div>
    )
  }

  if (!data) return null

  const riskLevel = data.risk.level

  return (
    <div className="space-y-6">
      {/* Alerts */}
      {data.alerts.length > 0 && (
        <div className="space-y-2">
          {data.alerts.map((alert, i) => (
            <div
              key={i}
              className="bg-orange-50 border border-orange-200 rounded-lg p-4 flex items-center gap-3"
            >
              <AlertTriangle className="w-5 h-5 text-orange-500 flex-shrink-0" />
              <span className="text-orange-800">{alert}</span>
            </div>
          ))}
        </div>
      )}

      {/* Risk Overview */}
      <div className={`rounded-xl border-2 p-6 ${RISK_BG_COLORS[riskLevel]}`}>
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-medium text-gray-600">Niveau de risque actuel</h2>
            <p className="text-3xl font-bold mt-2">{data.risk.description}</p>
            <div className="mt-4 flex flex-wrap gap-2">
              {data.risk.recommendations.map((rec, i) => (
                <span
                  key={i}
                  className="px-3 py-1 bg-white/50 rounded-full text-sm"
                >
                  {rec}
                </span>
              ))}
            </div>
          </div>
          <RiskGauge score={data.risk.score} level={riskLevel} />
        </div>
      </div>

      {/* Data Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Tide */}
        <DataCard
          title="Marée"
          icon={<Waves className="w-5 h-5" />}
          color="blue"
        >
          <div className="space-y-2">
            <div className="flex justify-between">
              <span className="text-gray-500">Coefficient</span>
              <span className="font-bold text-lg">
                {data.tide.current_coefficient || '-'}
              </span>
            </div>
            {data.tide.next_high_tide && (
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Prochaine PM</span>
                <span>
                  {format(new Date(data.tide.next_high_tide.timestamp), 'HH:mm', {
                    locale: fr,
                  })}
                </span>
              </div>
            )}
            {data.tide.next_low_tide && (
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Prochaine BM</span>
                <span>
                  {format(new Date(data.tide.next_low_tide.timestamp), 'HH:mm', {
                    locale: fr,
                  })}
                </span>
              </div>
            )}
          </div>
        </DataCard>

        {/* River Level */}
        <DataCard
          title="Dordogne (Libourne)"
          icon={<TrendingUp className="w-5 h-5" />}
          color="cyan"
        >
          <div className="space-y-2">
            <div className="flex items-end justify-between">
              <span className="text-gray-500">Niveau</span>
              <div className="flex items-center gap-2">
                <span className="font-bold text-2xl">
                  {data.river.current.height_m.toFixed(2)}
                </span>
                <span className="text-gray-500">m</span>
                <TendencyIcon tendency={data.river.tendency} />
              </div>
            </div>
            <div className="text-sm text-gray-500">
              {data.river.tendency === 'hausse' && 'En hausse'}
              {data.river.tendency === 'baisse' && 'En baisse'}
              {data.river.tendency === 'stable' && 'Stable'}
            </div>
          </div>
        </DataCard>

        {/* Rainfall */}
        <DataCard
          title="Pluie"
          icon={<Droplets className="w-5 h-5" />}
          color="indigo"
        >
          <div className="space-y-2">
            <div className="flex justify-between">
              <span className="text-gray-500">24h</span>
              <span className="font-bold">
                {data.rainfall.last_24h_mm.toFixed(1)} mm
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-500">72h</span>
              <span>{data.rainfall.last_72h_mm.toFixed(1)} mm</span>
            </div>
            <div className="flex justify-between text-sm text-blue-600">
              <span>Prévu 24h</span>
              <span>{data.rainfall.forecast_24h_mm.toFixed(1)} mm</span>
            </div>
          </div>
        </DataCard>

        {/* Risk Factors */}
        <DataCard
          title="Facteurs de risque"
          icon={<AlertTriangle className="w-5 h-5" />}
          color="amber"
        >
          <div className="space-y-1">
            <FactorBar label="Pluie 24h" value={data.risk.factors.rain_24h_score} />
            <FactorBar label="Pluie 72h" value={data.risk.factors.rain_72h_score} />
            <FactorBar label="Niveau" value={data.risk.factors.river_level_score} />
            <FactorBar label="Marée" value={data.risk.factors.tide_score} />
            <FactorBar label="Phase" value={data.risk.factors.tide_phase_score} />
          </div>
        </DataCard>
      </div>

      {/* Last Update */}
      <div className="flex items-center justify-between text-sm text-gray-500">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4" />
          <span>
            Dernière mise à jour :{' '}
            {lastUpdate
              ? format(lastUpdate, "dd/MM/yyyy HH:mm:ss", { locale: fr })
              : '-'}
          </span>
        </div>
        <button
          onClick={fetchData}
          disabled={loading}
          className="flex items-center gap-2 px-3 py-1 rounded hover:bg-gray-100"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Actualiser
        </button>
      </div>
    </div>
  )
}

function FactorBar({ label, value }: { label: string; value: number }) {
  const percent = Math.round(value * 100)
  const color =
    percent < 33 ? 'bg-green-500' : percent < 66 ? 'bg-yellow-500' : 'bg-red-500'

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-gray-500 w-16">{label}</span>
      <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
        <div
          className={`h-full ${color} transition-all`}
          style={{ width: `${percent}%` }}
        />
      </div>
      <span className="text-xs w-8 text-right">{percent}%</span>
    </div>
  )
}
