import type { RealtimeData, RiskOutlook } from '../types'
import { buildTriggers, type Trigger, type TriggerInputs } from '../utils/triggers'
import { formatNumber } from '../utils/format'
import { LABELLED_FLOODS } from '../utils/confidence'
import {
  useEnsembleForecast,
  useEpisodeDrivers,
  useIndicators,
  useLidoireProfile,
  useVigilance,
} from '../hooks/useSharedResource'
import ValueTag from './ui/ValueTag'

function TriggerRow({ t }: { t: Trigger }) {
  const pct = Math.round(t.progress * 100)
  return (
    <li className="grid gap-1.5 border-b border-line py-2.5 last:border-b-0" data-trigger={t.id} data-reached={t.reached || undefined}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <span className="min-w-0 font-bold text-ink">
          {t.label}
          {t.provisional && (
            <span className="ml-1.5 rounded-full border border-dashed border-ink-3 px-1.5 py-px align-middle text-[11px] font-normal text-ink-2">
              provisoire
            </span>
          )}
        </span>
        <span className="num text-[16px] text-ink">
          {t.valueText}
          {t.kind && <ValueTag kind={t.kind} className="ml-1.5 align-middle font-sans text-[11px]" />}
        </span>
      </div>
      <div
        className="h-2 overflow-hidden rounded-full bg-surface-3"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-label={`${t.label} : ${pct} % du chemin vers le seuil`}
      >
        <span
          className={`block h-full rounded-full ${t.reached ? 'bg-lv-red' : t.progress >= 0.5 ? 'bg-lv-yellow' : 'bg-accent'}`}
          style={{ width: `${Math.max(2, pct)}%` }}
        />
      </div>
      <p className="text-[13px] text-ink-2">
        {t.reached ? <b className="text-ink">Seuil atteint</b> : <span>{formatNumber(pct, 0)} % du chemin</span>}
        {' · '}seuil {t.thresholdText} : {t.why}.
      </p>
    </li>
  )
}

/** "What would change my mind": thresholds from the data and the config (pure). */
export function TriggerListView(inputs: TriggerInputs) {
  const triggers = buildTriggers(inputs)
  return (
    <section aria-labelledby="triggers-title" className="rounded-card border border-line bg-surface p-4">
      <h3 id="triggers-title" className="font-display text-[19px] font-semibold tracking-[0.01em]">
        Ce qui me ferait changer d&apos;avis
      </h3>
      <p className="mt-1 text-[13px] text-ink-3">
        Seuils lus dans les données et la configuration. « Provisoire » : tiré des {LABELLED_FLOODS} crues labellisées (en échantillon), à
        revoir à chaque nouvelle crue. La barre montre le chemin parcouru vers le seuil (échelle log pour l&apos;Eyraud).
      </p>
      {triggers.length === 0 ? (
        <p className="mt-2 text-sm text-ink-2">Seuils indisponibles : données en cours de chargement ou absentes.</p>
      ) : (
        <ul className="mt-1">
          {triggers.map((t) => (
            <TriggerRow key={t.id} t={t} />
          ))}
        </ul>
      )}
    </section>
  )
}

/** Container: shared fetches of the "why" payloads. Live mode only. */
export default function TriggerList({ realtime, outlook }: { realtime: RealtimeData | null; outlook: RiskOutlook | null }) {
  const indicators = useIndicators()
  const drivers = useEpisodeDrivers()
  const profile = useLidoireProfile()
  const ensemble = useEnsembleForecast()
  const vigilance = useVigilance()
  return (
    <TriggerListView
      indicators={indicators.data}
      drivers={drivers.data}
      profile={profile.data}
      ensemble={ensemble.data}
      vigilance={vigilance.data}
      realtime={realtime}
      outlook={outlook}
    />
  )
}
