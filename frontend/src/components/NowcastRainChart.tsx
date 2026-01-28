import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Tooltip } from 'recharts'
import { CloudRain } from 'lucide-react'
import type { NowcastRainData } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

interface NowcastRainChartProps {
  data?: NowcastRainData | null
}

export default function NowcastRainChart({ data }: NowcastRainChartProps) {
  if (!data || data.minute_data.length === 0) {
    return null
  }

  const hasRain = data.total_60min_mm > 0

  const chartData = data.minute_data.map((d) => ({
    minute: d.minute,
    mm: d.mm,
  }))

  return (
    <div className="bg-white rounded-lg shadow p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <CloudRain className="w-5 h-5 text-blue-500" />
          <h3 className="font-medium text-gray-700">Pluie prochaine heure</h3>
          <InfoTooltip {...TOOLTIPS.timelineNowcast} />
        </div>
        <div className="flex items-center gap-3 text-sm">
          <span className={`font-bold ${hasRain ? 'text-blue-600' : 'text-gray-400'}`}>
            {data.total_60min_mm.toFixed(1)} mm
          </span>
          {hasRain && (
            <span className="text-gray-500">
              {data.rain_minutes} min de pluie
            </span>
          )}
        </div>
      </div>

      {hasRain ? (
        <ResponsiveContainer width="100%" height={100}>
          <BarChart data={chartData} margin={{ top: 0, right: 0, bottom: 0, left: 0 }}>
            <XAxis
              dataKey="minute"
              tick={{ fontSize: 10 }}
              interval={9}
              tickLine={false}
              axisLine={false}
            />
            <YAxis hide domain={[0, 'auto']} />
            <Tooltip
              formatter={(value: number) => [`${value.toFixed(2)} mm`, 'Precipitation']}
              labelFormatter={(label) => `${label}`}
              contentStyle={{ fontSize: 12 }}
            />
            <Bar
              dataKey="mm"
              fill="#3b82f6"
              radius={[2, 2, 0, 0]}
              maxBarSize={8}
            />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-[60px] flex items-center justify-center text-sm text-gray-400">
          Pas de pluie prevue dans l'heure
        </div>
      )}

      {hasRain && data.max_intensity_mm > 0 && (
        <div className="mt-2 text-xs text-gray-500">
          Intensite max : {data.max_intensity_mm.toFixed(2)} mm/min
        </div>
      )}
    </div>
  )
}
