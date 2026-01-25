import { useState, useEffect, useMemo } from 'react'
import { format, subDays, addDays } from 'date-fns'
import { fr } from 'date-fns/locale'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'
import { ChevronLeft, ChevronRight, Calendar, MapPin, Check } from 'lucide-react'
import { getTimelineData, getMultiStationTimeline, getStations } from '../api'
import type { TimelineData, StationData } from '../types'
import type { MultiStationTimelineData } from '../api'

type Resolution = '15m' | '1h' | '6h' | '1d'

// Station colors for multi-station display
const STATION_COLORS: Record<string, string> = {
  'P577001001': '#0891b2', // Libourne - cyan
  'P555001001': '#0ea5e9', // Pessac-sur-Dordogne - sky
  'P514001001': '#06b6d4', // Bergerac - teal
  'P726151001': '#8b5cf6', // Abzac (Isle) - violet
  'P704151001': '#a855f7', // Périgueux (Isle) - purple
  'P846251001': '#f59e0b', // Coutras (Dronne) - amber
  'P816251001': '#fbbf24', // Ribérac (Dronne) - yellow
}

export default function Timeline() {
  const [data, setData] = useState<TimelineData | null>(null)
  const [multiStationData, setMultiStationData] = useState<MultiStationTimelineData | null>(null)
  const [stations, setStations] = useState<StationData[]>([])
  const [selectedStations, setSelectedStations] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [startDate, setStartDate] = useState(() => subDays(new Date(), 7))
  const [endDate, setEndDate] = useState(() => new Date())
  const [resolution, setResolution] = useState<Resolution>('1h')
  const [viewMode, setViewMode] = useState<'combined' | 'multi-station'>('combined')
  const [visibleSeries, setVisibleSeries] = useState({
    river: true,
    tide: true,
    rainfall: true,
    risk: true,
  })

  // Fetch available stations on mount
  useEffect(() => {
    async function fetchStations() {
      try {
        const result = await getStations()
        setStations(result.stations)
        // Default to primary station selected
        if (result.primary_station_id) {
          setSelectedStations([result.primary_station_id])
        }
      } catch (err) {
        console.error('Error fetching stations:', err)
      }
    }
    fetchStations()
  }, [])

  const fetchData = async () => {
    setLoading(true)
    try {
      if (viewMode === 'combined') {
        const result = await getTimelineData(
          startDate.toISOString(),
          endDate.toISOString(),
          resolution
        )
        setData(result)
      } else {
        // Multi-station mode
        const result = await getMultiStationTimeline(
          startDate.toISOString(),
          endDate.toISOString(),
          selectedStations.length > 0 ? selectedStations : undefined
        )
        setMultiStationData(result)
      }
    } catch (err) {
      console.error('Error fetching timeline:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [startDate, endDate, resolution, viewMode, selectedStations])

  const shiftPeriod = (days: number) => {
    setStartDate((prev) => addDays(prev, days))
    setEndDate((prev) => addDays(prev, days))
  }

  const setPeriod = (days: number) => {
    const now = new Date()
    setEndDate(now)
    setStartDate(subDays(now, days))
  }

  const toggleStation = (stationId: string) => {
    setSelectedStations((prev) =>
      prev.includes(stationId)
        ? prev.filter((id) => id !== stationId)
        : [...prev, stationId]
    )
  }

  // Combined chart data
  const chartData = useMemo(() => {
    return data?.points.map((p) => ({
      time: new Date(p.timestamp).getTime(),
      displayTime: format(new Date(p.timestamp), 'dd/MM HH:mm', { locale: fr }),
      river: p.river_level_m,
      tide: p.tide_height_m,
      rainfall: p.rainfall_mm_h,
      risk: p.risk_score,
    }))
  }, [data])

  // Multi-station chart data
  const multiStationChartData = useMemo(() => {
    if (!multiStationData) return []

    // Collect all timestamps and create a merged dataset
    const timestampMap = new Map<string, Record<string, number | null>>()

    for (const [stationId, stationData] of Object.entries(multiStationData.stations)) {
      for (const point of stationData.data) {
        const ts = point.timestamp
        if (!timestampMap.has(ts)) {
          timestampMap.set(ts, { time: new Date(ts).getTime() })
        }
        const row = timestampMap.get(ts)!
        row[stationId] = point.height_m
      }
    }

    // Sort by timestamp and format
    return Array.from(timestampMap.entries())
      .sort((a, b) => new Date(a[0]).getTime() - new Date(b[0]).getTime())
      .map(([ts, values]) => ({
        ...values,
        displayTime: format(new Date(ts), 'dd/MM HH:mm', { locale: fr }),
      }))
  }, [multiStationData])

  // Group stations by river
  const stationsByRiver = useMemo(() => {
    const groups: Record<string, StationData[]> = {}
    for (const station of stations) {
      if (!groups[station.river]) {
        groups[station.river] = []
      }
      groups[station.river].push(station)
    }
    return groups
  }, [stations])

  return (
    <div className="space-y-6">
      {/* Controls */}
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          {/* Period Navigation */}
          <div className="flex items-center gap-2">
            <button
              onClick={() => shiftPeriod(-7)}
              className="p-2 hover:bg-gray-100 rounded"
              title="Semaine precedente"
            >
              <ChevronLeft className="w-5 h-5" />
            </button>

            <div className="flex items-center gap-2 px-3 py-1 bg-gray-100 rounded">
              <Calendar className="w-4 h-4 text-gray-500" />
              <span>
                {format(startDate, 'dd/MM/yyyy', { locale: fr })} -{' '}
                {format(endDate, 'dd/MM/yyyy', { locale: fr })}
              </span>
            </div>

            <button
              onClick={() => shiftPeriod(7)}
              className="p-2 hover:bg-gray-100 rounded"
              title="Semaine suivante"
            >
              <ChevronRight className="w-5 h-5" />
            </button>
          </div>

          {/* Quick Period Buttons */}
          <div className="flex gap-2">
            {[
              { label: '24h', days: 1 },
              { label: '3j', days: 3 },
              { label: '7j', days: 7 },
              { label: '14j', days: 14 },
              { label: '30j', days: 30 },
            ].map(({ label, days }) => (
              <button
                key={days}
                onClick={() => setPeriod(days)}
                className="px-3 py-1 text-sm rounded border hover:bg-gray-50"
              >
                {label}
              </button>
            ))}
          </div>

          {/* Resolution */}
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">Resolution:</span>
            <select
              value={resolution}
              onChange={(e) => setResolution(e.target.value as Resolution)}
              className="border rounded px-2 py-1 text-sm"
            >
              <option value="15m">15 min</option>
              <option value="1h">1 heure</option>
              <option value="6h">6 heures</option>
              <option value="1d">1 jour</option>
            </select>
          </div>
        </div>

        {/* View Mode Toggle */}
        <div className="flex gap-4 mt-4 pt-4 border-t">
          <div className="flex gap-2">
            <button
              onClick={() => setViewMode('combined')}
              className={`px-3 py-1 text-sm rounded ${
                viewMode === 'combined'
                  ? 'bg-cyan-100 text-cyan-700 border border-cyan-300'
                  : 'bg-gray-100 hover:bg-gray-200'
              }`}
            >
              Vue combinee
            </button>
            <button
              onClick={() => setViewMode('multi-station')}
              className={`px-3 py-1 text-sm rounded ${
                viewMode === 'multi-station'
                  ? 'bg-cyan-100 text-cyan-700 border border-cyan-300'
                  : 'bg-gray-100 hover:bg-gray-200'
              }`}
            >
              Multi-stations
            </button>
          </div>

          {viewMode === 'combined' && (
            <div className="flex gap-4 ml-4">
              {[
                { key: 'river', label: 'Niveau Dordogne', color: '#0891b2' },
                { key: 'tide', label: 'Maree', color: '#3b82f6' },
                { key: 'rainfall', label: 'Pluie', color: '#6366f1' },
                { key: 'risk', label: 'Score risque', color: '#f59e0b' },
              ].map(({ key, label, color }) => (
                <label key={key} className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={visibleSeries[key as keyof typeof visibleSeries]}
                    onChange={(e) =>
                      setVisibleSeries((prev) => ({
                        ...prev,
                        [key]: e.target.checked,
                      }))
                    }
                    className="rounded"
                  />
                  <span
                    className="w-3 h-3 rounded-full"
                    style={{ backgroundColor: color }}
                  />
                  <span className="text-sm">{label}</span>
                </label>
              ))}
            </div>
          )}
        </div>

        {/* Station Selector for Multi-Station Mode */}
        {viewMode === 'multi-station' && (
          <div className="mt-4 pt-4 border-t">
            <div className="text-sm font-medium text-gray-700 mb-2">
              Stations a afficher:
            </div>
            <div className="space-y-2">
              {Object.entries(stationsByRiver).map(([river, riverStations]) => (
                <div key={river} className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-medium text-gray-500 w-20">{river}:</span>
                  {riverStations.map((station) => {
                    const isSelected = selectedStations.includes(station.station_id)
                    const color = STATION_COLORS[station.station_id] || '#6b7280'
                    return (
                      <button
                        key={station.station_id}
                        onClick={() => toggleStation(station.station_id)}
                        className={`flex items-center gap-1 px-2 py-1 text-xs rounded-full border transition-all ${
                          isSelected
                            ? 'border-2'
                            : 'border-gray-200 bg-gray-50 hover:bg-gray-100'
                        }`}
                        style={
                          isSelected
                            ? {
                                backgroundColor: `${color}20`,
                                borderColor: color,
                                color: color,
                              }
                            : undefined
                        }
                      >
                        {isSelected && <Check className="w-3 h-3" />}
                        <MapPin className="w-3 h-3" />
                        {station.station_name}
                        <span className="opacity-60">({station.distance_km}km)</span>
                      </button>
                    )
                  })}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Chart */}
      <div className="bg-white rounded-lg shadow p-4">
        {loading ? (
          <div className="h-96 flex items-center justify-center">
            <div className="animate-pulse text-gray-400">Chargement...</div>
          </div>
        ) : viewMode === 'combined' ? (
          /* Combined View Chart */
          <ResponsiveContainer width="100%" height={400}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="displayTime"
                tick={{ fontSize: 12 }}
                interval="preserveStartEnd"
              />
              <YAxis
                yAxisId="level"
                orientation="left"
                domain={[0, 'auto']}
                label={{
                  value: 'Niveau (m)',
                  angle: -90,
                  position: 'insideLeft',
                }}
              />
              <YAxis
                yAxisId="rain"
                orientation="right"
                domain={[0, 'auto']}
                label={{
                  value: 'Pluie (mm/h)',
                  angle: 90,
                  position: 'insideRight',
                }}
              />
              <Tooltip
                labelFormatter={(label) => label}
                formatter={(value: number, name: string) => [
                  value?.toFixed(2),
                  name,
                ]}
              />
              <Legend />

              {visibleSeries.river && (
                <Line
                  yAxisId="level"
                  type="monotone"
                  dataKey="river"
                  name="Dordogne (m)"
                  stroke="#0891b2"
                  strokeWidth={2}
                  dot={false}
                />
              )}
              {visibleSeries.tide && (
                <Line
                  yAxisId="level"
                  type="monotone"
                  dataKey="tide"
                  name="Maree (m)"
                  stroke="#3b82f6"
                  strokeWidth={2}
                  dot={false}
                />
              )}
              {visibleSeries.rainfall && (
                <Line
                  yAxisId="rain"
                  type="monotone"
                  dataKey="rainfall"
                  name="Pluie (mm/h)"
                  stroke="#6366f1"
                  strokeWidth={1}
                  dot={false}
                />
              )}
              {visibleSeries.risk && (
                <Line
                  yAxisId="rain"
                  type="monotone"
                  dataKey="risk"
                  name="Score risque"
                  stroke="#f59e0b"
                  strokeWidth={2}
                  strokeDasharray="5 5"
                  dot={false}
                />
              )}

              {/* Episode markers */}
              {data?.episodes.map((ep) => (
                <ReferenceLine
                  key={ep.id}
                  x={format(new Date(ep.start_time), 'dd/MM HH:mm', {
                    locale: fr,
                  })}
                  yAxisId="level"
                  stroke="#ef4444"
                  strokeDasharray="3 3"
                  label={{
                    value: `Episode ${ep.severity}/5`,
                    position: 'top',
                    fontSize: 10,
                  }}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        ) : (
          /* Multi-Station View Chart */
          <ResponsiveContainer width="100%" height={400}>
            <LineChart data={multiStationChartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="displayTime"
                tick={{ fontSize: 12 }}
                interval="preserveStartEnd"
              />
              <YAxis
                domain={[0, 'auto']}
                label={{
                  value: 'Niveau (m)',
                  angle: -90,
                  position: 'insideLeft',
                }}
              />
              <Tooltip
                labelFormatter={(label) => label}
                formatter={(value: number, name: string) => {
                  const stationInfo = multiStationData?.stations[name]
                  const displayName = stationInfo
                    ? `${stationInfo.name} (${stationInfo.river})`
                    : name
                  return [value?.toFixed(2) + ' m', displayName]
                }}
              />
              <Legend
                formatter={(value) => {
                  const stationInfo = multiStationData?.stations[value]
                  return stationInfo
                    ? `${stationInfo.name} (${stationInfo.river})`
                    : value
                }}
              />

              {multiStationData &&
                Object.keys(multiStationData.stations).map((stationId) => (
                  <Line
                    key={stationId}
                    type="monotone"
                    dataKey={stationId}
                    name={stationId}
                    stroke={STATION_COLORS[stationId] || '#6b7280'}
                    strokeWidth={2}
                    dot={false}
                    connectNulls
                  />
                ))}

              {/* Threshold reference lines for primary station */}
              {multiStationData?.stations['P577001001']?.thresholds && (
                <>
                  <ReferenceLine
                    y={multiStationData.stations['P577001001'].thresholds.yellow || 3}
                    stroke="#eab308"
                    strokeDasharray="3 3"
                    label={{ value: 'Jaune', position: 'right', fontSize: 10 }}
                  />
                  <ReferenceLine
                    y={multiStationData.stations['P577001001'].thresholds.orange || 4}
                    stroke="#f97316"
                    strokeDasharray="3 3"
                    label={{ value: 'Orange', position: 'right', fontSize: 10 }}
                  />
                  <ReferenceLine
                    y={multiStationData.stations['P577001001'].thresholds.red || 5}
                    stroke="#ef4444"
                    strokeDasharray="3 3"
                    label={{ value: 'Rouge', position: 'right', fontSize: 10 }}
                  />
                </>
              )}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Station Legend (Multi-station mode) */}
      {viewMode === 'multi-station' && multiStationData && (
        <div className="bg-white rounded-lg shadow p-4">
          <h3 className="font-medium mb-3">Stations affichees</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {Object.entries(multiStationData.stations).map(([stationId, info]) => (
              <div
                key={stationId}
                className="flex items-center gap-2 p-2 rounded bg-gray-50"
              >
                <div
                  className="w-3 h-3 rounded-full"
                  style={{ backgroundColor: STATION_COLORS[stationId] || '#6b7280' }}
                />
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm truncate">{info.name}</div>
                  <div className="text-xs text-gray-500">{info.river} - {info.distance_km}km</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Episodes in range (Combined mode only) */}
      {viewMode === 'combined' && data?.episodes && data.episodes.length > 0 && (
        <div className="bg-white rounded-lg shadow p-4">
          <h3 className="font-medium mb-3">Episodes dans la periode</h3>
          <div className="space-y-2">
            {data.episodes.map((ep) => (
              <div
                key={ep.id}
                className="flex items-center justify-between p-3 bg-red-50 rounded border border-red-200"
              >
                <div>
                  <span className="font-medium">
                    {format(new Date(ep.start_time), 'dd/MM/yyyy HH:mm', {
                      locale: fr,
                    })}
                  </span>
                  {ep.description && (
                    <span className="text-gray-600 ml-2">{ep.description}</span>
                  )}
                </div>
                <span className="px-2 py-1 bg-red-100 rounded text-sm">
                  Severite {ep.severity}/5
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
