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
  Activity,
  CloudRain,
} from 'lucide-react'
import {
  getRealtimeData,
  getNetatmoData,
  getInfoclimatData,
  getDataSourcesStatus,
  getForecast,
  getBasinRainfall,
  getRealtimeDataAt,
  getEpisodesList,
} from '../api'
import type {
  RealtimeData,
  RiskLevel,
  NetatmoData,
  InfoclimatData,
  DataSource,
  ForecastData,
  BasinRainfallData,
  FloodEpisode,
} from '../types'
import RiskGauge from '../components/RiskGauge'
import DataCard from '../components/DataCard'
import StationsPanel from '../components/StationsPanel'
import RainfallPanel from '../components/RainfallPanel'
import ForecastPanel from '../components/ForecastPanel'
import DataSourcesStatus from '../components/DataSourcesStatus'
import RiskBreakdownPanel from '../components/RiskBreakdownPanel'
import HydroMap from '../components/HydroMap'
import BasinRainfallGauge from '../components/BasinRainfallGauge'
import MetricSparkline from '../components/MetricSparkline'
import TimeMachineBanner from '../components/TimeMachineBanner'
import InfoTooltip from '../components/InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

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
  const [breakdownExpanded, setBreakdownExpanded] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)
  const [basinRainfall, setBasinRainfall] = useState<BasinRainfallData | null>(null)
  const [episodes, setEpisodes] = useState<FloodEpisode[]>([])

  // TimeMachine state
  const [timeMachineActive, setTimeMachineActive] = useState(false)
  const [timeMachineDate, setTimeMachineDate] = useState<Date | null>(null)
  const [timeMachineLoading, setTimeMachineLoading] = useState(false)

  // Sparkline accumulation
  const [sparklineData, setSparklineData] = useState<{
    river: { t: number; v: number }[]
    tide: { t: number; v: number }[]
    rain: { t: number; v: number }[]
    risk: { t: number; v: number }[]
  }>({ river: [], tide: [], rain: [], risk: [] })

  const fetchData = async () => {
    try {
      setLoading(true)

      // Fetch all data in parallel
      const [
        realtimeResult,
        netatmoResult,
        infoclimatResult,
        sourcesResult,
        forecastResult,
        basinResult,
        episodesResult,
      ] = await Promise.allSettled([
        getRealtimeData(),
        getNetatmoData(),
        getInfoclimatData(),
        getDataSourcesStatus(),
        getForecast(48),
        getBasinRainfall(),
        getEpisodesList(),
      ])

      if (realtimeResult.status === 'fulfilled') {
        const d = realtimeResult.value
        setData(d)
        // Set default selected station to primary
        if (d.stations && !selectedStation) {
          setSelectedStation(d.stations.primary_station_id)
        }

        // Accumulate sparkline data
        const now = Date.now()
        setSparklineData(prev => ({
          river: [...prev.river, { t: now, v: d.river.current.height_m }].slice(-60),
          tide: [...prev.tide, { t: now, v: d.tide.current_coefficient || 0 }].slice(-60),
          rain: [...prev.rain, { t: now, v: d.rainfall.last_hour_mm }].slice(-60),
          risk: [...prev.risk, { t: now, v: d.risk.score }].slice(-60),
        }))
      }
      if (netatmoResult.status === 'fulfilled') setNetatmo(netatmoResult.value)
      if (infoclimatResult.status === 'fulfilled') setInfoclimat(infoclimatResult.value)
      if (sourcesResult.status === 'fulfilled') setSources(sourcesResult.value.sources)
      if (forecastResult.status === 'fulfilled') setForecast(forecastResult.value)
      if (basinResult.status === 'fulfilled') setBasinRainfall(basinResult.value)
      if (episodesResult.status === 'fulfilled') setEpisodes(episodesResult.value)

      setLastUpdate(new Date())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Erreur de chargement')
    } finally {
      setLoading(false)
    }
  }

  const handleTimeMachineToggle = () => {
    if (timeMachineActive) {
      // Return to live
      setTimeMachineActive(false)
      setTimeMachineDate(null)
      fetchData() // Refresh live data
    } else {
      setTimeMachineActive(true)
    }
  }

  const handleTimeMachineDateChange = async (date: Date) => {
    setTimeMachineDate(date)
    setTimeMachineLoading(true)
    try {
      const result = await getRealtimeDataAt(date.toISOString())
      setData(result)
      if (result.stations && !selectedStation) {
        setSelectedStation(result.stations.primary_station_id)
      }
    } catch (err) {
      console.error('TimeMachine error:', err)
    } finally {
      setTimeMachineLoading(false)
    }
  }

  useEffect(() => {
    if (!timeMachineActive) {
      fetchData()
      const interval = setInterval(fetchData, 60000)
      return () => clearInterval(interval)
    }
  }, [timeMachineActive])

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
      {/* TimeMachine Banner */}
      <TimeMachineBanner
        active={timeMachineActive}
        onToggle={handleTimeMachineToggle}
        selectedDate={timeMachineDate}
        onDateChange={handleTimeMachineDateChange}
        episodes={episodes}
        loading={timeMachineLoading}
      />

      {/* Data Sources Status - Compact at top */}
      <DataSourcesStatus sources={sources} loading={loading} />

      {/* Alerts */}
      {data.alerts.length > 0 && (
        <div className="space-y-2">
          {data.alerts
            .filter(a => !a.includes('Netatmo') && !a.includes('Mode TimeMachine'))
            .map((alert, i) => (
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
          <RiskGauge
            score={data.risk.score}
            level={riskLevel}
            onClick={() => setBreakdownExpanded(!breakdownExpanded)}
            clickable
          />
        </div>
      </div>

      {/* Risk Breakdown Panel */}
      <RiskBreakdownPanel risk={data.risk} defaultExpanded={breakdownExpanded} />

      {/* HydroMap - Full Width */}
      <HydroMap
        stations={stations}
        netatmoStations={netatmo?.lidoire_stations}
        infoclimatStations={infoclimat?.nearest_stations}
        tide={data.tide}
      />

      {/* Sparklines Row - 4 cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <MetricSparkline
          title="Dordogne"
          value={`${(stations.find(s => s.station_id === 'P577001001')?.current_level.height_m || 0).toFixed(2)} m`}
          subtitle={stations.find(s => s.station_id === 'P577001001')?.tendency}
          data={sparklineData.river}
          color="#0891b2"
          icon={<Waves className="w-4 h-4" />}
        />
        <MetricSparkline
          title="Maree"
          value={`Coef ${data.tide.current_coefficient || '-'}`}
          subtitle={
            data.tide.next_high_tide
              ? `PM ${format(new Date(data.tide.next_high_tide.timestamp), 'HH:mm', { locale: fr })}`
              : undefined
          }
          data={sparklineData.tide}
          color="#3b82f6"
          icon={<Waves className="w-4 h-4" />}
        />
        <MetricSparkline
          title="Pluie bassin"
          value={`${basinRainfall?.weighted_average_24h_mm?.toFixed(1) || data.rainfall.last_24h_mm.toFixed(1)} mm`}
          subtitle="24h cumul"
          data={sparklineData.rain}
          color="#6366f1"
          icon={<CloudRain className="w-4 h-4" />}
        />
        <MetricSparkline
          title="Risque"
          value={`${data.risk.score.toFixed(0)}/100`}
          subtitle={data.risk.level}
          data={sparklineData.risk}
          color={
            data.risk.level === 'red'
              ? '#ef4444'
              : data.risk.level === 'orange'
                ? '#f97316'
                : data.risk.level === 'yellow'
                  ? '#eab308'
                  : '#22c55e'
          }
          icon={<Activity className="w-4 h-4" />}
        />
      </div>

      {/* Main Grid - 2 columns on desktop */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Left Column - Stations */}
        <StationsPanel
          stations={stations}
          selectedStation={selectedStation}
          onSelectStation={setSelectedStation}
          loading={loading}
        />

        {/* Right Column - Basin Rainfall + Weather & Forecasts */}
        <div className="space-y-6">
          <BasinRainfallGauge data={basinRainfall} loading={loading} />

          <RainfallPanel
            netatmo={netatmo}
            infoclimat={infoclimat ? {
              available: infoclimat.rainfall?.available || false,
              stations_found: infoclimat.stations_found,
              nearest_stations: infoclimat.nearest_stations,
              rainfall: infoclimat.rainfall,
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
              <span className="text-gray-500 flex items-center gap-1">
                Coefficient
                <InfoTooltip {...TOOLTIPS.tideCoefficient} />
              </span>
              <span className="font-bold text-lg">
                {data.tide.current_coefficient || '-'}
              </span>
            </div>
            {data.tide.next_high_tide && (
              <div className="flex justify-between text-sm">
                <span className="text-gray-500 flex items-center gap-1">
                  Prochaine PM
                  <InfoTooltip {...TOOLTIPS.tidePhaseBmPm} />
                </span>
                <span>
                  {format(new Date(data.tide.next_high_tide.timestamp), 'HH:mm', {
                    locale: fr,
                  })}
                </span>
              </div>
            )}
            {data.tide.next_low_tide && (
              <div className="flex justify-between text-sm">
                <span className="text-gray-500 flex items-center gap-1">
                  Prochaine BM
                </span>
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
                <div className="text-sm text-gray-500 flex items-center gap-1">
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
                  {station.tendency !== 'stable' && (
                    <InfoTooltip {...TOOLTIPS.rateOfRise} />
                  )}
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
      </div>

      {/* Last Update */}
      <div className="flex items-center justify-between text-sm text-gray-500">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4" />
          <span>
            {timeMachineActive && timeMachineDate
              ? `Mode TimeMachine - ${format(timeMachineDate, 'dd/MM/yyyy HH:mm', { locale: fr })}`
              : `Derniere mise a jour : ${lastUpdate ? format(lastUpdate, 'dd/MM/yyyy HH:mm:ss', { locale: fr }) : '-'}`}
          </span>
        </div>
        <button
          onClick={fetchData}
          disabled={loading || timeMachineActive}
          className="flex items-center gap-2 px-3 py-1 rounded hover:bg-gray-100 disabled:opacity-50"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Actualiser
        </button>
      </div>
    </div>
  )
}
