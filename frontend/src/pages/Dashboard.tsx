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
import {
  getRealtimeData,
  getNetatmoData,
  getInfoclimatData,
  getDataSourcesStatus,
  getForecast,
} from '../api'
import type {
  RealtimeData,
  RiskLevel,
  NetatmoData,
  InfoclimatData,
  DataSource,
  ForecastData,
} from '../types'
import RiskGauge from '../components/RiskGauge'
import DataCard from '../components/DataCard'
import StationsPanel from '../components/StationsPanel'
import RainfallPanel from '../components/RainfallPanel'
import ForecastPanel from '../components/ForecastPanel'
import DataSourcesStatus from '../components/DataSourcesStatus'

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
  const [netatmo, setNetatmo] = useState<NetatmoData | null>(null)
  const [infoclimat, setInfoclimat] = useState<InfoclimatData | null>(null)
  const [sources, setSources] = useState<DataSource[]>([])
  const [forecast, setForecast] = useState<ForecastData | null>(null)
  const [selectedStation, setSelectedStation] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)

  const fetchData = async () => {
    try {
      setLoading(true)

      // Fetch all data in parallel
      const [realtimeResult, netatmoResult, infoclimatResult, sourcesResult, forecastResult] =
        await Promise.allSettled([
          getRealtimeData(),
          getNetatmoData(),
          getInfoclimatData(),
          getDataSourcesStatus(),
          getForecast(48),
        ])

      if (realtimeResult.status === 'fulfilled') {
        setData(realtimeResult.value)
        // Set default selected station to primary
        if (realtimeResult.value.stations && !selectedStation) {
          setSelectedStation(realtimeResult.value.stations.primary_station_id)
        }
      }
      if (netatmoResult.status === 'fulfilled') setNetatmo(netatmoResult.value)
      if (infoclimatResult.status === 'fulfilled') setInfoclimat(infoclimatResult.value)
      if (sourcesResult.status === 'fulfilled') setSources(sourcesResult.value.sources)
      if (forecastResult.status === 'fulfilled') setForecast(forecastResult.value)

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
          Reessayer
        </button>
      </div>
    )
  }

  if (!data) return null

  const riskLevel = data.risk.level
  const stations = data.stations?.stations || []

  return (
    <div className="space-y-6">
      {/* Data Sources Status - Compact at top */}
      <DataSourcesStatus sources={sources} loading={loading} />

      {/* Alerts */}
      {data.alerts.length > 0 && (
        <div className="space-y-2">
          {data.alerts.filter(a => !a.includes('Netatmo')).map((alert, i) => (
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

      {/* Main Grid - 2 columns on desktop */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Left Column - Stations */}
        <div className="space-y-6">
          <StationsPanel
            stations={stations}
            selectedStation={selectedStation}
            onSelectStation={setSelectedStation}
            loading={loading}
          />
        </div>

        {/* Right Column - Weather & Forecasts */}
        <div className="space-y-6">
          <RainfallPanel
            netatmo={netatmo}
            infoclimat={infoclimat ? {
              available: infoclimat.rainfall?.available || false,
              stations_found: infoclimat.stations_found,
              nearest_stations: infoclimat.nearest_stations,
            } : undefined}
            forecast={{
              forecast_24h_mm: data.rainfall.forecast_24h_mm,
              forecast_72h_mm: data.rainfall.forecast_72h_mm,
            }}
            loading={loading}
          />

          <ForecastPanel forecast={forecast} loading={loading} />
        </div>
      </div>

      {/* Quick Data Cards - Secondary info */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Tide */}
        <DataCard
          title="Maree"
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

        {/* Selected Station Level */}
        <DataCard
          title={
            stations.find((s) => s.station_id === selectedStation)?.station_name ||
            'Dordogne'
          }
          icon={<TrendingUp className="w-5 h-5" />}
          color="cyan"
        >
          {(() => {
            const station = stations.find((s) => s.station_id === selectedStation)
            if (!station) {
              return (
                <div className="text-gray-500">
                  Selectionnez une station
                </div>
              )
            }
            return (
              <div className="space-y-2">
                <div className="flex items-end justify-between">
                  <span className="text-gray-500">Niveau</span>
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-2xl">
                      {station.current_level.height_m.toFixed(2)}
                    </span>
                    <span className="text-gray-500">m</span>
                    <TendencyIcon tendency={station.tendency} />
                  </div>
                </div>
                <div className="text-sm text-gray-500">
                  {station.tendency === 'hausse' && (
                    <span className="text-red-500">
                      +{station.rate_cm_per_hour.toFixed(1)} cm/h
                    </span>
                  )}
                  {station.tendency === 'baisse' && (
                    <span className="text-green-500">
                      {station.rate_cm_per_hour.toFixed(1)} cm/h
                    </span>
                  )}
                  {station.tendency === 'stable' && 'Stable'}
                </div>
              </div>
            )
          })()}
        </DataCard>

        {/* Rainfall */}
        <DataCard
          title="Pluie observee"
          icon={<Droplets className="w-5 h-5" />}
          color="indigo"
        >
          <div className="space-y-2">
            <div className="flex justify-between">
              <span className="text-gray-500">1h</span>
              <span className="font-bold">
                {data.rainfall.last_hour_mm.toFixed(1)} mm
              </span>
            </div>
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
          </div>
        </DataCard>

        {/* Risk Factors */}
        <DataCard
          title="Facteurs de risque"
          icon={<AlertTriangle className="w-5 h-5" />}
          color="amber"
        >
          <div className="space-y-1">
            <FactorBar label="Niveau" value={data.risk.factors.river_level_score} />
            <FactorBar label="Pluie 24h" value={data.risk.factors.rain_24h_score} />
            <FactorBar label="Maree" value={data.risk.factors.tide_score} />
            <FactorBar label="Tendance" value={data.risk.factors.tendency_score || 0} />
          </div>
        </DataCard>
      </div>

      {/* Last Update */}
      <div className="flex items-center justify-between text-sm text-gray-500">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4" />
          <span>
            Derniere mise a jour :{' '}
            {lastUpdate
              ? format(lastUpdate, 'dd/MM/yyyy HH:mm:ss', { locale: fr })
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
