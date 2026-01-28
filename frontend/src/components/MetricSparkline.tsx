import { AreaChart, Area, ResponsiveContainer, YAxis } from 'recharts'

interface MetricSparklineProps {
  title: string
  value: string
  subtitle?: string
  data: { t: number; v: number }[]
  color: string
  icon: React.ReactNode
}

export default function MetricSparkline({
  title,
  value,
  subtitle,
  data,
  color,
  icon,
}: MetricSparklineProps) {
  const gradientId = `metricGradient-${title.replace(/\s+/g, '-')}`

  return (
    <div className="bg-white rounded-lg shadow p-3">
      {/* Top row: icon + title */}
      <div className="flex items-center gap-2 mb-1">
        <div
          className="w-7 h-7 rounded-full flex items-center justify-center"
          style={{ backgroundColor: `${color}20` }}
        >
          {icon}
        </div>
        <span className="text-sm text-gray-500">{title}</span>
      </div>

      {/* Value */}
      <div className="text-xl font-bold text-gray-900">{value}</div>

      {/* Subtitle */}
      {subtitle && (
        <div className="text-xs text-gray-400">{subtitle}</div>
      )}

      {/* Sparkline */}
      {data.length > 0 && (
        <div className="w-full h-[40px] mt-2">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data}>
              <defs>
                <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={color} stopOpacity={0.3} />
                  <stop offset="95%" stopColor={color} stopOpacity={0} />
                </linearGradient>
              </defs>
              <YAxis domain={['dataMin', 'dataMax']} hide />
              <Area
                type="monotone"
                dataKey="v"
                stroke={color}
                strokeWidth={1.5}
                fill={`url(#${gradientId})`}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}
