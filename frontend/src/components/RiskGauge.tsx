import { Info } from 'lucide-react'
import type { RiskLevel } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

interface RiskGaugeProps {
  score: number
  level: RiskLevel
  onClick?: () => void
  clickable?: boolean
}

const LEVEL_COLORS: Record<RiskLevel, string> = {
  green: '#22c55e',
  yellow: '#eab308',
  orange: '#f97316',
  red: '#ef4444',
}

export default function RiskGauge({ score, level, onClick, clickable }: RiskGaugeProps) {
  const color = LEVEL_COLORS[level]
  const angle = (score / 100) * 180 - 90 // -90 to 90 degrees

  return (
    <div
      className={`relative w-32 ${clickable ? 'cursor-pointer hover:opacity-80 transition-opacity' : ''}`}
      onClick={clickable ? onClick : undefined}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={clickable ? (e) => e.key === 'Enter' && onClick?.() : undefined}
    >
      <div className="relative h-20">
        {/* Background arc */}
        <svg viewBox="0 0 100 60" className="w-full h-full">
          {/* Gray background arc */}
          <path
            d="M 10 55 A 40 40 0 0 1 90 55"
            fill="none"
            stroke="#e5e7eb"
            strokeWidth="8"
            strokeLinecap="round"
          />

          {/* Colored progress arc */}
          <path
            d="M 10 55 A 40 40 0 0 1 90 55"
            fill="none"
            stroke={color}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={`${(score / 100) * 126} 126`}
          />

          {/* Needle */}
          <g transform={`rotate(${angle} 50 55)`}>
            <line
              x1="50"
              y1="55"
              x2="50"
              y2="20"
              stroke="#374151"
              strokeWidth="2"
              strokeLinecap="round"
            />
            <circle cx="50" cy="55" r="4" fill="#374151" />
          </g>

          {/* Score text */}
          <text
            x="50"
            y="50"
            textAnchor="middle"
            className="text-xl font-bold"
            fill={color}
          >
            {Math.round(score)}
          </text>
        </svg>

        {/* Labels */}
        <div className="absolute bottom-0 left-0 right-0 flex justify-between px-1 text-xs text-gray-400">
          <span>0</span>
          <span>100</span>
        </div>
      </div>
      {/* Info icon for clickable gauge */}
      {clickable && (
        <div className="flex items-center justify-center gap-1 mt-1">
          <Info className="w-4 h-4 text-gray-400" />
          <InfoTooltip {...TOOLTIPS.riskScore} />
        </div>
      )}
    </div>
  )
}
