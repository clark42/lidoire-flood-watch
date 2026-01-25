import { useState, useEffect } from 'react'
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
import { ChevronLeft, ChevronRight, Calendar } from 'lucide-react'
import { getTimelineData } from '../api'
import type { TimelineData, TimelinePoint } from '../types'

type Resolution = '15m' | '1h' | '6h' | '1d'

export default function Timeline() {
  const [data, setData] = useState<TimelineData | null>(null)
  const [loading, setLoading] = useState(true)
  const [startDate, setStartDate] = useState(() => subDays(new Date(), 7))
  const [endDate, setEndDate] = useState(() => new Date())
  const [resolution, setResolution] = useState<Resolution>('1h')
  const [visibleSeries, setVisibleSeries] = useState({
    river: true,
    tide: true,
    rainfall: true,
    risk: true,
  })

  const fetchData = async () => {
    setLoading(true)
    try {
      const result = await getTimelineData(
        startDate.toISOString(),
        endDate.toISOString(),
        resolution
      )
      setData(result)
    } catch (err) {
      console.error('Error fetching timeline:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [startDate, endDate, resolution])

  const shiftPeriod = (days: number) => {
    setStartDate((prev) => addDays(prev, days))
    setEndDate((prev) => addDays(prev, days))
  }

  const setPeriod = (days: number) => {
    const now = new Date()
    setEndDate(now)
    setStartDate(subDays(now, days))
  }

  const chartData = data?.points.map((p: TimelinePoint) => ({
    time: new Date(p.timestamp).getTime(),
    displayTime: format(new Date(p.timestamp), 'dd/MM HH:mm', { locale: fr }),
    river: p.river_level_m,
    tide: p.tide_height_m,
    rainfall: p.rainfall_mm_h,
    risk: p.risk_score,
  }))

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

        {/* Series Toggle */}
        <div className="flex gap-4 mt-4 pt-4 border-t">
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
      </div>

      {/* Chart */}
      <div className="bg-white rounded-lg shadow p-4">
        {loading ? (
          <div className="h-96 flex items-center justify-center">
            <div className="animate-pulse text-gray-400">Chargement...</div>
          </div>
        ) : (
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
        )}
      </div>

      {/* Episodes in range */}
      {data?.episodes && data.episodes.length > 0 && (
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
