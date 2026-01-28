import { Droplets, Thermometer, Sun, CloudRain } from 'lucide-react'
import type { SoilConditions } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

interface SoilConditionsPanelProps {
  soil?: SoilConditions | null
  loading?: boolean
}

function MoistureBar({ value, max = 0.5 }: { value: number; max?: number }) {
  const percent = Math.min(100, (value / max) * 100)
  const color =
    percent < 33 ? 'bg-yellow-400' : percent < 66 ? 'bg-blue-400' : 'bg-blue-600'

  return (
    <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
      <div
        className={`h-full ${color} transition-all`}
        style={{ width: `${percent}%` }}
      />
    </div>
  )
}

function SaturationBadge({ factor }: { factor: number }) {
  const label =
    factor >= 1.4 ? 'Sature' : factor >= 1.2 ? 'Humide' : factor >= 1.1 ? 'Normal' : 'Sec'
  const color =
    factor >= 1.4
      ? 'bg-red-100 text-red-700'
      : factor >= 1.2
        ? 'bg-blue-100 text-blue-700'
        : factor >= 1.1
          ? 'bg-green-100 text-green-700'
          : 'bg-yellow-100 text-yellow-700'

  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${color}`}>
      {label} (x{factor.toFixed(2)})
    </span>
  )
}

export default function SoilConditionsPanel({ soil, loading }: SoilConditionsPanelProps) {
  if (loading && !soil) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-4 animate-pulse">
        <div className="h-4 bg-gray-200 rounded w-1/3 mb-4" />
        <div className="space-y-3">
          <div className="h-3 bg-gray-200 rounded" />
          <div className="h-3 bg-gray-200 rounded w-2/3" />
        </div>
      </div>
    )
  }

  if (!soil) return null

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="p-4 border-b border-gray-100">
        <h3 className="font-medium flex items-center gap-2">
          <Droplets className="w-5 h-5 text-blue-500" />
          Sol & Conditions agricoles
        </h3>
      </div>

      <div className="p-4 space-y-4">
        {/* Saturation factor */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm text-gray-600 flex items-center gap-1">
              Saturation du sol
              <InfoTooltip {...TOOLTIPS.soilSaturationFactor} />
            </span>
            <SaturationBadge factor={soil.soil_saturation_factor} />
          </div>
        </div>

        {/* Soil moisture gauge */}
        {soil.soil_moisture != null && (
          <div>
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm text-gray-600 flex items-center gap-1">
                <Droplets className="w-3 h-3" />
                Humidite 0-9cm
                <InfoTooltip {...TOOLTIPS.soilMoisture} />
              </span>
              <span className="text-sm font-medium">{soil.soil_moisture.toFixed(3)} m3/m3</span>
            </div>
            <MoistureBar value={soil.soil_moisture} />
          </div>
        )}

        {/* Deep moisture */}
        {soil.moisture_deep != null && (
          <div>
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm text-gray-600 flex items-center gap-1">
                <Droplets className="w-3 h-3" />
                Humidite profonde (9-81cm)
              </span>
              <span className="text-sm font-medium">{soil.moisture_deep.toFixed(3)} m3/m3</span>
            </div>
            <MoistureBar value={soil.moisture_deep} />
          </div>
        )}

        {/* Temperature & ET0 row */}
        <div className="grid grid-cols-2 gap-4">
          {soil.soil_temperature_0cm != null && (
            <div className="flex items-center gap-2">
              <Thermometer className="w-4 h-4 text-orange-500" />
              <div>
                <div className="text-xs text-gray-500">Temp. sol</div>
                <div className="font-medium text-sm">
                  {soil.soil_temperature_0cm.toFixed(1)}°C
                  {soil.soil_temperature_0cm < 0 && (
                    <span className="text-red-500 text-xs ml-1">(gel)</span>
                  )}
                </div>
              </div>
            </div>
          )}
          {soil.et0_mm != null && (
            <div className="flex items-center gap-2">
              <Sun className="w-4 h-4 text-yellow-500" />
              <div>
                <div className="text-xs text-gray-500 flex items-center gap-1">
                  ET0
                  <InfoTooltip {...TOOLTIPS.et0} />
                </div>
                <div className="font-medium text-sm">{soil.et0_mm.toFixed(1)} mm/j</div>
              </div>
            </div>
          )}
        </div>

        {/* Precipitation probability */}
        {soil.precip_probability != null && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-gray-600 flex items-center gap-1">
              <CloudRain className="w-3 h-3" />
              Proba. precipitation
            </span>
            <span className="text-sm font-medium">{soil.precip_probability}%</span>
          </div>
        )}

        {/* Water balance (from Meteo Agricole) */}
        {(soil.water_balance_30d_mm != null || soil.surplus_deficit_30d_pct != null) && (
          <div className="border-t border-gray-100 pt-3">
            <div className="text-xs text-gray-500 mb-2 flex items-center gap-1">
              Bilan hydrique 30j
              <InfoTooltip {...TOOLTIPS.waterBalance30d} />
            </div>
            <div className="grid grid-cols-2 gap-3 text-sm">
              {soil.water_balance_30d_mm != null && (
                <div>
                  <span className="text-gray-500">Cumul</span>
                  <div className="font-medium">{soil.water_balance_30d_mm.toFixed(0)} mm</div>
                </div>
              )}
              {soil.surplus_deficit_30d_pct != null && (
                <div>
                  <span className="text-gray-500">Ecart</span>
                  <div className={`font-medium ${soil.surplus_deficit_30d_pct > 0 ? 'text-blue-600' : 'text-orange-600'}`}>
                    {soil.surplus_deficit_30d_pct > 0 ? '+' : ''}{soil.surplus_deficit_30d_pct.toFixed(0)}%
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* 12-month deficit */}
        {soil.deficit_12m_mm != null && (
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-500">Deficit 12 mois</span>
            <span className="font-medium text-orange-600">{soil.deficit_12m_mm.toFixed(0)} mm</span>
          </div>
        )}
      </div>
    </div>
  )
}
