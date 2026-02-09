import { useState } from 'react'
import { Calculator, ChevronDown, ChevronUp, HelpCircle, TrendingUp, TrendingDown, Minus } from 'lucide-react'
import type { RiskScore, StationRiskScore, Tendency } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

interface RiskBreakdownPanelProps {
  risk: RiskScore
  defaultExpanded?: boolean
}

const FACTOR_LABELS: Record<string, string> = {
  river: 'Niveaux rivieres',
  rain_24h: 'Pluie 24h',
  rain_72h: 'Pluie 72h',
  rain_7d: 'Pluie 7j',
  tide: 'Coef. maree',
  tide_phase: 'Phase maree',
  tendency: 'Tendance',
  momentum: 'Momentum multi-stations',
  wind_west: "Vent d'ouest",
  soil_sat: 'Saturation sol',
  video: 'Video',
}

const FACTOR_TOOLTIPS: Record<string, keyof typeof TOOLTIPS> = {
  river: 'factorRiver',
  rain_24h: 'factorRain24h',
  rain_72h: 'factorRain72h',
  rain_7d: 'factorRain7d',
  tide: 'factorTide',
  tide_phase: 'factorTidePhase',
  tendency: 'factorTendency',
  momentum: 'factorMomentum',
  wind_west: 'factorWindWest',
  soil_sat: 'factorSoilSat',
  video: 'factorVideo',
}

function TendencyIcon({ tendency }: { tendency: Tendency }) {
  if (tendency === 'hausse') return <TrendingUp className="w-3 h-3 text-red-500" />
  if (tendency === 'baisse') return <TrendingDown className="w-3 h-3 text-green-500" />
  return <Minus className="w-3 h-3 text-gray-400" />
}

function ScoreBar({ score, small = false }: { score: number; small?: boolean }) {
  const percent = Math.round(score * 100)
  const color =
    percent < 33 ? 'bg-green-500' : percent < 66 ? 'bg-yellow-500' : 'bg-red-500'

  return (
    <div className={`flex-1 ${small ? 'h-1.5' : 'h-2'} bg-gray-200 rounded-full overflow-hidden`}>
      <div
        className={`h-full ${color} transition-all`}
        style={{ width: `${percent}%` }}
      />
    </div>
  )
}

function formatRawValue(
  factorKey: string,
  risk: RiskScore
): string {
  const { factors } = risk

  switch (factorKey) {
    case 'river':
      return factors.stations_detail?.length
        ? `${factors.stations_detail.length} stations`
        : '-'
    case 'rain_24h':
      return factors.rain_24h_raw_mm != null
        ? `${factors.rain_24h_raw_mm.toFixed(1)} mm`
        : '-'
    case 'rain_72h':
      return factors.rain_72h_raw_mm != null
        ? `${factors.rain_72h_raw_mm.toFixed(1)} mm`
        : '-'
    case 'tide':
      return factors.tide_coefficient_raw != null
        ? `${factors.tide_coefficient_raw}`
        : '-'
    case 'tide_phase':
      if (factors.tide_hours_to_high == null) return '-'
      const hours = Math.abs(factors.tide_hours_to_high)
      if (hours < 1) return 'PM'
      return `PM${factors.tide_hours_to_high > 0 ? '-' : '+'}${hours.toFixed(0)}h`
    case 'tendency':
      return factors.tendency_rate_cm_h != null
        ? `${factors.tendency_rate_cm_h > 0 ? '+' : ''}${factors.tendency_rate_cm_h.toFixed(1)} cm/h`
        : '-'
    case 'momentum': {
      const active = factors.stations_detail?.filter(s => s.weight > 0) || []
      const rising = active.filter(s => s.rate_cm_per_hour > 1).length
      return `${rising}/${active.length} stations`
    }
    case 'rain_7d':
      return '-'  // No raw value stored yet
    case 'wind_west':
      return '-'  // No raw value stored yet
    case 'soil_sat':
      return factors.soil_saturation_factor != null
        ? `x${factors.soil_saturation_factor.toFixed(2)}`
        : '-'
    case 'video':
      return factors.video_level_score != null ? `${Math.round(factors.video_level_score * 100)}%` : 'N/A'
    default:
      return '-'
  }
}

function getFactorScore(factorKey: string, factors: RiskScore['factors']): number | null {
  switch (factorKey) {
    case 'river':
      return factors.river_level_score
    case 'rain_24h':
      return factors.rain_24h_score
    case 'rain_72h':
      return factors.rain_72h_score
    case 'rain_7d':
      return factors.rain_7d_score ?? 0
    case 'tide':
      return factors.tide_score
    case 'tide_phase':
      return factors.tide_phase_score
    case 'tendency':
      return factors.tendency_score ?? 0
    case 'momentum':
      return factors.momentum_score ?? 0
    case 'wind_west':
      return factors.wind_west_score ?? 0
    case 'soil_sat':
      return factors.soil_saturation_factor != null
        ? Math.min(1, Math.max(0, (factors.soil_saturation_factor - 1.0) / 0.5))
        : 0
    case 'video':
      return factors.video_level_score
    default:
      return null
  }
}

export default function RiskBreakdownPanel({ risk, defaultExpanded = false }: RiskBreakdownPanelProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const [stationsExpanded, setStationsExpanded] = useState(false)
  const [helpExpanded, setHelpExpanded] = useState(false)

  const { factors } = risk
  const weights = factors.weights || {
    river: 0.28,
    rain_24h: 0.10,
    rain_72h: 0.13,
    rain_7d: 0.05,
    tide: 0.05,
    tide_phase: 0.05,
    tendency: 0.13,
    momentum: 0.08,
    wind_west: 0.03,
    soil_sat: 0.05,
    video: 0.05,
  }

  // Calculate base score (before multiplier)
  const baseScore = risk.score / (factors.conjunction_multiplier || 1)

  // Build factor rows sorted by contribution
  const factorRows = Object.entries(weights)
    .map(([key, weight]) => {
      const score = getFactorScore(key, factors)
      const contribution = score != null ? score * weight * 100 : 0
      return {
        key,
        label: FACTOR_LABELS[key] || key,
        weight,
        score,
        contribution,
        raw: formatRawValue(key, risk),
      }
    })
    .sort((a, b) => b.contribution - a.contribution)

  const stations = factors.stations_detail || []

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between p-4 hover:bg-gray-50 transition-colors"
      >
        <div className="flex items-center gap-2">
          <Calculator className="w-5 h-5 text-gray-500" />
          <span className="font-medium">Detail du calcul de risque</span>
          <InfoTooltip {...TOOLTIPS.riskScore} />
        </div>
        {expanded ? (
          <ChevronUp className="w-5 h-5 text-gray-400" />
        ) : (
          <ChevronDown className="w-5 h-5 text-gray-400" />
        )}
      </button>

      {/* Content */}
      {expanded && (
        <div className="border-t border-gray-200">
          {/* Score summary */}
          <div className="p-4 bg-gray-50 border-b border-gray-200">
            <div className="text-2xl font-bold">
              Score final: {risk.score.toFixed(0)}/100
            </div>
            {(factors.conjunction_multiplier || 1) > 1 && (
              <div className="text-sm text-gray-600 mt-1 flex items-center gap-1 flex-wrap">
                = Score de base ({baseScore.toFixed(1)}) x Multiplicateur conjonction ({(factors.conjunction_multiplier || 1).toFixed(2)})
                <InfoTooltip {...TOOLTIPS.conjunctionMultiplier} />
              </div>
            )}
          </div>

          {/* Factors table */}
          <div className="p-4">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-500 text-xs uppercase">
                  <th className="text-left pb-2 font-medium">Facteur</th>
                  <th className="text-center pb-2 font-medium w-16">Poids</th>
                  <th className="text-center pb-2 font-medium w-28">Score</th>
                  <th className="text-right pb-2 font-medium w-20">Contrib.</th>
                  <th className="text-right pb-2 font-medium w-20">Brut</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {factorRows.map(({ key, label, weight, score, contribution, raw }) => (
                  <tr key={key} className={score === null ? 'opacity-50' : ''}>
                    <td className="py-2 font-medium">
                      <span className="inline-flex items-center gap-1">
                        {label}
                        {FACTOR_TOOLTIPS[key] && (
                          <InfoTooltip {...TOOLTIPS[FACTOR_TOOLTIPS[key]]} />
                        )}
                      </span>
                    </td>
                    <td className="py-2 text-center text-gray-500">
                      {(weight * 100).toFixed(0)}%
                    </td>
                    <td className="py-2">
                      {score !== null ? (
                        <div className="flex items-center gap-2">
                          <ScoreBar score={score} />
                          <span className="text-xs w-8 text-right">
                            {(score * 100).toFixed(0)}%
                          </span>
                        </div>
                      ) : (
                        <span className="text-gray-400">N/A</span>
                      )}
                    </td>
                    <td className="py-2 text-right font-mono">
                      {contribution.toFixed(1)} pts
                    </td>
                    <td className="py-2 text-right text-gray-500">{raw}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Stations detail */}
          {stations.length > 0 && (
            <div className="border-t border-gray-200">
              <button
                onClick={() => setStationsExpanded(!stationsExpanded)}
                className="w-full flex items-center justify-between p-4 hover:bg-gray-50 transition-colors"
              >
                <span className="text-sm font-medium text-gray-600">
                  Detail par station ({stations.length})
                </span>
                {stationsExpanded ? (
                  <ChevronUp className="w-4 h-4 text-gray-400" />
                ) : (
                  <ChevronDown className="w-4 h-4 text-gray-400" />
                )}
              </button>

              {stationsExpanded && (
                <div className="px-4 pb-4 space-y-2">
                  {stations
                    .sort((a: StationRiskScore, b: StationRiskScore) => b.weighted_score - a.weighted_score)
                    .map((station: StationRiskScore) => (
                      <div
                        key={station.station_id}
                        className="flex items-center gap-3 text-sm bg-gray-50 rounded-lg p-2"
                      >
                        <div className="flex-1">
                          <div className="flex items-center gap-2">
                            <span className="font-medium">{station.station_name}</span>
                            <span className="text-gray-400 text-xs">
                              ({(station.weight * 100).toFixed(0)}%)
                            </span>
                            <TendencyIcon tendency={station.tendency} />
                          </div>
                          <div className="text-gray-500 text-xs">
                            {station.river} - {station.height_m.toFixed(2)}m
                            {station.rate_cm_per_hour !== 0 && (
                              <span className={`inline-flex items-center gap-0.5 ${station.rate_cm_per_hour > 0 ? 'text-red-500' : 'text-green-500'}`}>
                                {' '}({station.rate_cm_per_hour > 0 ? '+' : ''}{station.rate_cm_per_hour.toFixed(1)} cm/h)
                                <InfoTooltip {...TOOLTIPS.rateOfRise} />
                              </span>
                            )}
                          </div>
                        </div>
                        <div className="w-20">
                          <ScoreBar score={station.score} small />
                        </div>
                        <div className="w-16 text-right font-mono text-xs">
                          {(station.weighted_score * 100).toFixed(1)} pts
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </div>
          )}

          {/* Help section */}
          <div className="border-t border-gray-200">
            <button
              onClick={() => setHelpExpanded(!helpExpanded)}
              className="w-full flex items-center justify-between p-4 hover:bg-gray-50 transition-colors"
            >
              <div className="flex items-center gap-2">
                <HelpCircle className="w-4 h-4 text-gray-400" />
                <span className="text-sm text-gray-600">Comment est calcule le score ?</span>
              </div>
              {helpExpanded ? (
                <ChevronUp className="w-4 h-4 text-gray-400" />
              ) : (
                <ChevronDown className="w-4 h-4 text-gray-400" />
              )}
            </button>

            {helpExpanded && (
              <div className="px-4 pb-4 text-sm text-gray-600 space-y-2">
                <p>Le score de risque (0-100) est calcule en plusieurs etapes :</p>
                <ol className="list-decimal list-inside space-y-1 pl-2">
                  <li>Chaque facteur est normalise de 0 a 1 selon des seuils predefinnis</li>
                  <li>Chaque score est multiplie par son poids (10 facteurs, total = 100%)</li>
                  <li>La saturation du sol amplifie aussi les scores de pluie (multiplicateur)</li>
                  <li>La somme des contributions donne le score de base</li>
                  <li>
                    Si maree haute + niveau eleve + hausse : multiplicateur de conjonction (1.0 a 1.5x)
                  </li>
                </ol>
                <p className="text-xs text-gray-500 mt-3">
                  Les seuils sont calibres sur les donnees historiques d'inondations du moulin.
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
