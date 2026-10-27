import type {
  EnsembleForecast,
  EpisodeDriversData,
  IndicatorsData,
  LidoireProfileData,
  RealtimeData,
  RiskOutlook,
} from '../types'
import { buildMechanisms } from '../utils/mechanisms'
import {
  useEnsembleForecast,
  useEpisodeDrivers,
  useIndicators,
  useLidoireProfile,
} from '../hooks/useSharedResource'
import MechanismCard from './MechanismCard'
import { OutcomeLegend } from './ui/PercentileRail'

export interface MechanismGridProps {
  indicators: IndicatorsData | null
  drivers: EpisodeDriversData | null
  profile: LidoireProfileData | null
  realtime: RealtimeData | null
  outlook: RiskOutlook | null
  ensemble: EnsembleForecast | null
  /** Sources that failed to load (named in a note) */
  missing?: string[]
  now: Date
}

/** The four mechanism cards (pure: payloads in, markup out). */
export function MechanismGrid({ missing = [], now, ...payloads }: MechanismGridProps) {
  const views = buildMechanisms({ ...payloads, now })
  const lowCount = payloads.drivers?.episodes.filter((e) => e.outcome === 'flooded').length ?? 0
  return (
    <div className="space-y-3">
      {missing.length > 0 && (
        <p role="status" className="rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink-2">
          Indisponible pour l&apos;instant : {missing.join(', ')}. Les cartes montrent ce qui est connu.
        </p>
      )}
      <div className="grid grid-cols-1 gap-3 min-[700px]:grid-cols-2">
        {views.map((v, i) => (
          <MechanismCard key={v.id} view={v} primary={i === 0} />
        ))}
      </div>
      <div className="space-y-1 rounded-lg border border-line bg-surface px-3 py-2.5">
        <OutcomeLegend />
        <p className="text-[12.5px] text-ink-3">
          Points : valeurs aux épisodes labellisés ({lowCount} crues, issues du propriétaire, datées au jour près).
          * seuil provisoire, tiré des crues labellisées (en échantillon). Une bande hachurée = seuil inconnu.
        </p>
      </div>
    </div>
  )
}

/**
 * "Why" of the Dashboard: what floods the mill, mechanism by mechanism.
 * Reads /api/indicators, /api/episodes/drivers, /api/geo/profile and
 * /api/forecast/ensemble (shared fetches), plus the realtime payload and the
 * outlook already loaded by the Dashboard. Live mode only.
 */
export default function MechanismCards({
  realtime,
  outlook,
  now,
}: {
  realtime: RealtimeData | null
  outlook: RiskOutlook | null
  now: Date
}) {
  const indicators = useIndicators()
  const drivers = useEpisodeDrivers()
  const profile = useLidoireProfile()
  const ensemble = useEnsembleForecast()
  const missing = [
    indicators.error && 'indicateurs (Eyraud, nappe)',
    drivers.error && 'valeurs aux crues',
    profile.error && 'profil en long (remous)',
    ensemble.error && "pluie d'ensemble",
  ].filter((m): m is string => Boolean(m))

  if (indicators.loading && !indicators.data && drivers.loading && !drivers.data) {
    return (
      <div className="grid grid-cols-1 gap-3 min-[700px]:grid-cols-2" aria-busy="true" aria-label="Chargement des mécanismes">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-72 animate-pulse rounded-card bg-surface-2" />
        ))}
      </div>
    )
  }

  return (
    <MechanismGrid
      indicators={indicators.data}
      drivers={drivers.data}
      profile={profile.data}
      realtime={realtime}
      outlook={outlook}
      ensemble={ensemble.data}
      missing={missing}
      now={now}
    />
  )
}
