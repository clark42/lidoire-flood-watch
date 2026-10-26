import { useState, useEffect } from 'react'
import {
  RefreshCw,
  Sliders,
  MapPin,
  Database,
  Droplets,
  Waves,
  CloudRain,
  Radio,
  Thermometer,
  Camera,
  AlertTriangle,
  ExternalLink,
} from 'lucide-react'
import { useLocation } from 'react-router-dom'
import { getStatus, getSourcesDetail, simulateRisk, errorMessage, toActionError } from '../api'
import type { ActionErrorState } from '../api'
import type {
  DependencyCheck,
  DependencyName,
  DependencyState,
  RiskSimulationResult,
  SourceFreshness,
  SourceFreshnessState,
  SourcesDetail,
  SystemStatus,
} from '../types'
import { formatAge, parseApiDate } from '../utils/time'
import ApiKeyPanel from '../components/ApiKeyPanel'
import { formatDistanceKm } from '../utils/format'
import ActionError from '../components/ActionError'
import ForecastSkillPanel from '../components/ForecastSkillPanel'

const DEPENDENCIES: { key: DependencyName; label: string }[] = [
  { key: 'database', label: 'PostgreSQL' },
  { key: 'influxdb', label: 'InfluxDB' },
  { key: 'redis', label: 'Redis' },
]

const DEPENDENCY_BADGE: Record<DependencyState, string> = {
  connected: 'bg-green-100 text-green-700',
  timeout: 'bg-orange-100 text-orange-700',
  error: 'bg-red-100 text-red-700',
}

function statusBadgeClass(status: string): string {
  if (status === 'healthy') return 'bg-green-100 text-green-800'
  if (status === 'degraded') return 'bg-orange-100 text-orange-800'
  return 'bg-red-100 text-red-800'
}

const FRESHNESS_BADGE: Record<SourceFreshnessState, { label: string; className: string }> = {
  ok: { label: 'a jour', className: 'bg-green-100 text-green-700' },
  stale: { label: 'perime', className: 'bg-orange-100 text-orange-700' },
  unavailable: { label: 'indisponible', className: 'bg-red-100 text-red-700' },
  unknown: { label: 'inconnu', className: 'bg-gray-100 text-gray-600' },
}

function freshnessText(source: SourceFreshness): string {
  const last = parseApiDate(source.last_timestamp)
  if (!last) return source.status === 'unknown' ? 'non verifie' : 'aucune donnee (30 j)'
  if (last.getTime() > Date.now()) {
    const until = last.toLocaleString('fr-FR', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
    return `jusqu'au ${until}`
  }
  return formatAge(last)
}

function SourceFreshnessRow({ source }: { source: SourceFreshness }) {
  const badge = FRESHNESS_BADGE[source.status] ?? FRESHNESS_BADGE.unknown
  const limit =
    source.max_age_hours > 0 ? `perime au-dela de ${source.max_age_hours} h` : 'doit couvrir le futur'
  const detail = source.error ?? source.note
  return (
    <div className="text-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0">
          {source.label}
          {source.critical && (
            <span className="ml-1 text-xs text-gray-400" title="Perime ou indisponible : statut degrade">
              (critique)
            </span>
          )}
        </span>
        <span className="flex items-center gap-2 shrink-0">
          <span
            className="text-xs text-gray-400"
            title={`${source.last_timestamp ? `${source.last_timestamp} UTC, ` : ''}${limit}`}
          >
            {freshnessText(source)}
          </span>
          <span className={`px-2 py-0.5 rounded ${badge.className}`}>{badge.label}</span>
        </span>
      </div>
      {detail && (
        <p className={`text-xs mt-0.5 text-right break-words ${source.error ? 'text-red-600' : 'text-gray-500'}`}>
          {detail}
        </p>
      )}
    </div>
  )
}

function DependencyRow({
  label,
  state,
  check,
}: {
  label: string
  state: DependencyState | undefined
  check: DependencyCheck | undefined
}) {
  const current = state ?? check?.status
  const badge = current ? DEPENDENCY_BADGE[current] ?? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-600'
  return (
    <div className="text-sm">
      <div className="flex items-center justify-between gap-2">
        <span>{label}</span>
        <span className="flex items-center gap-2">
          {check?.latency_ms != null && (
            <span className="text-xs text-gray-400 font-mono">{check.latency_ms.toFixed(0)} ms</span>
          )}
          <span className={`px-2 py-0.5 rounded ${badge}`}>{current ?? 'inconnu'}</span>
        </span>
      </div>
      {check?.error && (
        <p className="text-xs text-red-600 mt-0.5 text-right break-words">{check.error}</p>
      )}
    </div>
  )
}

export default function Configuration() {
  const location = useLocation()
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [sources, setSources] = useState<SourcesDetail | null>(null)
  const [sourcesError, setSourcesError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [sourcesLoading, setSourcesLoading] = useState(true)

  // Simulation state
  const [simParams, setSimParams] = useState({
    rain_24h_mm: 30,
    rain_72h_mm: 60,
    dordogne_level_m: 3.0,
    tide_coefficient: 80,
    hours_to_high_tide: 2,
  })
  const [simResult, setSimResult] = useState<RiskSimulationResult | null>(null)
  const [simError, setSimError] = useState<ActionErrorState | null>(null)
  const [simulating, setSimulating] = useState(false)

  useEffect(() => {
    fetchStatus()
    fetchSources()
  }, [])

  // Links like /config#api-key (from a 401/403 error) scroll to the panel
  useEffect(() => {
    if (!location.hash) return
    const target = document.getElementById(location.hash.slice(1))
    target?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    target?.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true })
  }, [location.hash])

  const fetchStatus = async () => {
    setLoading(true)
    try {
      const data = await getStatus()
      setStatus(data)
      setStatusError(null)
    } catch (err) {
      console.error('Error fetching status:', err)
      setStatusError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const fetchSources = async () => {
    setSourcesLoading(true)
    try {
      const data = await getSourcesDetail()
      setSources(data)
      setSourcesError(null)
    } catch (err) {
      console.error('Error fetching sources detail:', err)
      setSourcesError(errorMessage(err))
    } finally {
      setSourcesLoading(false)
    }
  }

  const runSimulation = async () => {
    setSimulating(true)
    setSimError(null)
    try {
      const result = await simulateRisk(simParams)
      setSimResult(result)
    } catch (err) {
      console.error('Error simulating:', err)
      setSimResult(null)
      setSimError(toActionError(err, 'Simulation impossible'))
    } finally {
      setSimulating(false)
    }
  }

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold">Configuration</h2>

      <div id="api-key" className="scroll-mt-20">
        <ApiKeyPanel />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Status */}
        <div className="bg-white rounded-lg shadow">
          <div className="p-4 border-b flex items-center justify-between">
            <h3 className="font-medium flex items-center gap-2">
              <Database className="w-5 h-5" />
              Etat du systeme
            </h3>
            <button
              onClick={() => { fetchStatus(); fetchSources() }}
              className="p-2 hover:bg-gray-100 rounded"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
          <div className="p-4">
            {statusError && (
              <div role="alert" className="mb-4 bg-red-50 border border-red-200 text-red-800 rounded px-3 py-2 text-sm">
                Etat du systeme indisponible : {statusError}
                {status ? ' (valeurs ci-dessous perimees)' : ''}
              </div>
            )}
            {status ? (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-gray-500">Status</span>
                  <span className={`px-2 py-1 rounded text-sm ${statusBadgeClass(status.status)}`}>
                    {status.status}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-gray-500">Version</span>
                  <span>{status.version}</span>
                </div>

                <div className="pt-4 border-t">
                  <h4 className="text-sm font-medium text-gray-500 mb-2">
                    Dependances
                  </h4>
                  <div className="space-y-2">
                    {DEPENDENCIES.map(({ key, label }) => (
                      <DependencyRow
                        key={key}
                        label={label}
                        state={status.services?.[key]}
                        check={status.services?.checks?.[key]}
                      />
                    ))}
                  </div>
                </div>

                <div className="pt-4 border-t">
                  <h4 className="text-sm font-medium text-gray-500 mb-2">
                    Fraicheur des donnees
                  </h4>
                  {status.sources ? (
                    <div className="space-y-2">
                      {Object.entries(status.sources).map(([name, source]) =>
                        source ? <SourceFreshnessRow key={name} source={source} /> : null
                      )}
                    </div>
                  ) : (
                    <p className="text-sm text-gray-400">Non fournie par cette version du backend</p>
                  )}
                </div>
              </div>
            ) : loading ? (
              <div className="text-gray-400 text-center py-4">Chargement...</div>
            ) : null}
          </div>
        </div>

        {/* Location */}
        <div className="bg-white rounded-lg shadow">
          <div className="p-4 border-b">
            <h3 className="font-medium flex items-center gap-2">
              <MapPin className="w-5 h-5" />
              Localisation
            </h3>
          </div>
          <div className="p-4">
            {status?.location && (
              <div className="space-y-3">
                <div>
                  <span className="text-gray-500 text-sm">Nom</span>
                  <p className="font-medium">{status.location.name}</p>
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <span className="text-gray-500 text-sm">Latitude</span>
                    <p className="font-mono">{status.location.lat}</p>
                  </div>
                  <div>
                    <span className="text-gray-500 text-sm">Longitude</span>
                    <p className="font-mono">{status.location.lon}</p>
                  </div>
                </div>
                <div className="pt-3 border-t">
                  <a
                    href={`https://www.google.com/maps?q=${status.location.lat},${status.location.lon}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-600 hover:underline text-sm"
                  >
                    Voir sur Google Maps
                  </a>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ===== SOURCES DE DONNEES ===== */}
      {sourcesLoading ? (
        <div className="bg-white rounded-lg shadow p-8 text-center text-gray-400">
          Chargement des sources...
        </div>
      ) : sourcesError && !sources ? (
        <div role="alert" className="bg-red-50 border border-red-200 text-red-800 rounded-lg p-4 text-sm">
          Detail des sources indisponible : {sourcesError}
        </div>
      ) : sources ? (
        <div className="space-y-6">
          <h3 className="text-lg font-bold">Sources de donnees</h3>

          {/* River stations */}
          {sources.sources?.river && (
            <div className="bg-white rounded-lg shadow">
              <div className="p-4 border-b">
                <h4 className="font-medium flex items-center gap-2">
                  <Droplets className="w-5 h-5 text-blue-600" />
                  {sources.sources.river.name}
                </h4>
                <p className="text-sm text-gray-500 mt-1">
                  {sources.sources.river.description}
                </p>
                <a
                  href={sources.sources.river.api_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-500 hover:underline flex items-center gap-1 mt-1"
                >
                  <ExternalLink className="w-3 h-3" />
                  {sources.sources.river.api_url}
                </a>
              </div>
              <div className="p-4 overflow-x-auto">
                <p className="text-sm text-gray-500 mb-2">
                  Station primaire : <strong>{sources.primary_station}</strong>
                </p>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-gray-500">
                      <th className="py-2 pr-3">Station</th>
                      <th className="py-2 pr-3">Code</th>
                      <th className="py-2 pr-3">Riviere</th>
                      <th className="py-2 pr-3 text-right">Distance</th>
                      <th className="py-2 pr-3 text-right">Delai</th>
                      <th className="py-2 pr-3 text-right">Poids</th>
                      <th className="py-2 pr-3 text-right">Seuil J</th>
                      <th className="py-2 pr-3 text-right">Seuil O</th>
                      <th className="py-2 text-right">Seuil R</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sources.sources.river.stations?.map((s) => (
                      <tr key={s.id} className="border-b last:border-0">
                        <td className="py-2 pr-3 font-medium">{s.name}</td>
                        <td className="py-2 pr-3 font-mono text-xs text-gray-500">
                          {s.id}
                        </td>
                        <td className="py-2 pr-3">{s.river}</td>
                        <td className="py-2 pr-3 text-right">{formatDistanceKm(s.distance_km)}</td>
                        <td className="py-2 pr-3 text-right">{s.delay_hours}h</td>
                        <td className="py-2 pr-3 text-right font-medium">
                          {(s.weight * 100).toFixed(0)}%
                        </td>
                        <td className="py-2 pr-3 text-right text-yellow-600">
                          {s.thresholds?.yellow ?? '—'}
                        </td>
                        <td className="py-2 pr-3 text-right text-orange-600">
                          {s.thresholds?.orange ?? '—'}
                        </td>
                        <td className="py-2 text-right text-red-600">
                          {s.thresholds?.red ?? '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Netatmo */}
          {sources.sources?.netatmo && (
            <div className="bg-white rounded-lg shadow">
              <div className="p-4 border-b">
                <h4 className="font-medium flex items-center gap-2">
                  <CloudRain className="w-5 h-5 text-green-600" />
                  {sources.sources.netatmo.name}
                  {!sources.sources.netatmo.configured && (
                    <span className="text-xs px-2 py-0.5 bg-gray-100 text-gray-500 rounded">
                      Non configure
                    </span>
                  )}
                </h4>
                <p className="text-sm text-gray-500 mt-1">
                  {sources.sources.netatmo.description}
                </p>
                <p className="text-xs text-gray-400 mt-1">
                  Rayon de recherche : {sources.sources.netatmo.search_radius_km} km
                </p>
              </div>
              {(sources.sources.netatmo.priority_stations?.length ?? 0) > 0 && (
                <div className="p-4 overflow-x-auto">
                  <p className="text-sm font-medium text-gray-500 mb-2">
                    Stations prioritaires
                  </p>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b text-left text-gray-500">
                        <th className="py-2 pr-3">Ville</th>
                        <th className="py-2 pr-3">Position</th>
                        <th className="py-2 pr-3 text-right">Lat</th>
                        <th className="py-2 text-right">Lon</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sources.sources.netatmo.priority_stations?.map((s, i) => (
                        <tr key={i} className="border-b last:border-0">
                          <td className="py-2 pr-3 font-medium">{s.city}</td>
                          <td className="py-2 pr-3 capitalize">{s.position}</td>
                          <td className="py-2 pr-3 text-right font-mono text-xs">
                            {s.lat}
                          </td>
                          <td className="py-2 text-right font-mono text-xs">
                            {s.lon}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Infoclimat */}
          {sources.sources?.infoclimat && (
            <div className="bg-white rounded-lg shadow">
              <div className="p-4 border-b">
                <h4 className="font-medium flex items-center gap-2">
                  <Radio className="w-5 h-5 text-purple-600" />
                  {sources.sources.infoclimat.name}
                  {!sources.sources.infoclimat.configured && (
                    <span className="text-xs px-2 py-0.5 bg-gray-100 text-gray-500 rounded">
                      Non configure
                    </span>
                  )}
                </h4>
                <p className="text-sm text-gray-500 mt-1">
                  {sources.sources.infoclimat.description}
                </p>
              </div>
              <div className="p-4 space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-gray-500">Rayon de recherche</span>
                  <span>{sources.sources.infoclimat.search_radius_km} km</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-500">Stations max</span>
                  <span>{sources.sources.infoclimat.max_stations}</span>
                </div>
                {sources.sources.infoclimat.note && (
                  <div className="mt-2 p-2 bg-amber-50 text-amber-700 rounded text-xs flex items-start gap-2">
                    <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                    {sources.sources.infoclimat.note}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Open-Meteo */}
          {sources.sources?.open_meteo && (
            <div className="bg-white rounded-lg shadow">
              <div className="p-4 border-b">
                <h4 className="font-medium flex items-center gap-2">
                  <Thermometer className="w-5 h-5 text-sky-600" />
                  {sources.sources.open_meteo.name}
                </h4>
                <p className="text-sm text-gray-500 mt-1">
                  {sources.sources.open_meteo.description}
                </p>
                <div className="mt-1 space-y-0.5">
                  <a
                    href={sources.sources.open_meteo.api_forecast}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-blue-500 hover:underline flex items-center gap-1"
                  >
                    <ExternalLink className="w-3 h-3" />
                    Forecast : {sources.sources.open_meteo.api_forecast}
                  </a>
                  <a
                    href={sources.sources.open_meteo.api_archive}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-blue-500 hover:underline flex items-center gap-1"
                  >
                    <ExternalLink className="w-3 h-3" />
                    Archive : {sources.sources.open_meteo.api_archive}
                  </a>
                </div>
              </div>
              <div className="p-4 space-y-4">
                <div>
                  <p className="text-sm font-medium text-gray-500 mb-2">
                    Champs collectes
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {sources.sources.open_meteo.fields?.map((f: string) => (
                      <span
                        key={f}
                        className="px-2 py-0.5 bg-sky-50 text-sky-700 rounded text-xs"
                      >
                        {f}
                      </span>
                    ))}
                  </div>
                </div>
                {(sources.sources.open_meteo.basin_points?.length ?? 0) > 0 && (
                  <div>
                    <p className="text-sm font-medium text-gray-500 mb-2">
                      Points du bassin Lidoire
                    </p>
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b text-left text-gray-500">
                          <th className="py-1 pr-3">ID</th>
                          <th className="py-1 pr-3">Nom</th>
                          <th className="py-1 pr-3 text-right">Lat</th>
                          <th className="py-1 text-right">Lon</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sources.sources.open_meteo.basin_points?.map((p) => (
                          <tr key={p.id} className="border-b last:border-0">
                            <td className="py-1 pr-3 font-mono text-xs">{p.id}</td>
                            <td className="py-1 pr-3">{p.name}</td>
                            <td className="py-1 pr-3 text-right font-mono text-xs">
                              {p.lat}
                            </td>
                            <td className="py-1 text-right font-mono text-xs">
                              {p.lon}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Tides */}
          {sources.sources?.tide && (
            <div className="bg-white rounded-lg shadow">
              <div className="p-4 border-b">
                <h4 className="font-medium flex items-center gap-2">
                  <Waves className="w-5 h-5 text-teal-600" />
                  {sources.sources.tide.name}
                </h4>
                <p className="text-sm text-gray-500 mt-1">
                  {sources.sources.tide.description}
                </p>
              </div>
              <div className="p-4 space-y-3 text-sm">
                <div>
                  <p className="text-gray-500 mb-1">Ports surveilles</p>
                  <div className="space-y-1">
                    {sources.sources.tide.ports?.map((port, i) => (
                      <div key={i} className="flex justify-between">
                        <span className="font-medium">{port.name}</span>
                        <span className="font-mono text-xs text-gray-400">
                          {port.lat}, {port.lon}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
                {sources.sources.tide.reference_point && (
                  <div className="pt-2 border-t">
                    <p className="text-gray-500 mb-1">Point de reference</p>
                    <div className="flex justify-between">
                      <span>{sources.sources.tide.reference_point.name}</span>
                      <span className="font-mono text-xs text-gray-400">
                        {sources.sources.tide.reference_point.lat},{' '}
                        {sources.sources.tide.reference_point.lon}
                      </span>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Meteo Agricole */}
          {sources.sources?.meteo_agricole && (
            <div className="bg-white rounded-lg shadow">
              <div className="p-4 border-b">
                <h4 className="font-medium flex items-center gap-2">
                  <CloudRain className="w-5 h-5 text-amber-600" />
                  {sources.sources.meteo_agricole.name}
                </h4>
                <p className="text-sm text-gray-500 mt-1">
                  {sources.sources.meteo_agricole.description}
                </p>
                {sources.sources.meteo_agricole.url && (
                  <a
                    href={sources.sources.meteo_agricole.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-blue-500 hover:underline flex items-center gap-1 mt-1"
                  >
                    <ExternalLink className="w-3 h-3" />
                    {sources.sources.meteo_agricole.url}
                  </a>
                )}
              </div>
              <div className="p-4">
                <p className="text-sm font-medium text-gray-500 mb-2">
                  Champs collectes
                </p>
                <div className="flex flex-wrap gap-2">
                  {sources.sources.meteo_agricole.fields?.map((f: string) => (
                    <span
                      key={f}
                      className="px-2 py-0.5 bg-amber-50 text-amber-700 rounded text-xs"
                    >
                      {f}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Camera */}
          {sources.sources?.camera && (
            <div className="bg-white rounded-lg shadow">
              <div className="p-4 border-b">
                <h4 className="font-medium flex items-center gap-2">
                  <Camera className="w-5 h-5 text-gray-600" />
                  {sources.sources.camera.name}
                  {!sources.sources.camera.configured && (
                    <span className="text-xs px-2 py-0.5 bg-gray-100 text-gray-500 rounded">
                      Non configure
                    </span>
                  )}
                </h4>
                <p className="text-sm text-gray-500 mt-1">
                  {sources.sources.camera.description}
                </p>
              </div>
            </div>
          )}

          {/* Risk Model */}
          {sources.risk_model && (
            <div className="bg-white rounded-lg shadow">
              <div className="p-4 border-b">
                <h4 className="font-medium flex items-center gap-2">
                  <AlertTriangle className="w-5 h-5 text-red-500" />
                  Modele de risque {sources.risk_model.version}
                </h4>
              </div>
              <div className="p-4 space-y-4">
                <div>
                  <p className="text-sm font-medium text-gray-500 mb-2">
                    Ponderation des facteurs
                  </p>
                  <div className="space-y-1">
                    {Object.entries(sources.risk_model.weights).map(
                      ([key, value]) => (
                        <div
                          key={key}
                          className="flex items-center justify-between text-sm"
                        >
                          <span className="text-gray-600">
                            {key.replace(/_/g, ' ')}
                          </span>
                          <span className="font-mono text-xs">{value as string}</span>
                        </div>
                      )
                    )}
                  </div>
                </div>
                {sources.risk_model.conjunction_multiplier && (
                  <div className="pt-3 border-t text-sm">
                    <span className="text-gray-500">Multiplicateur conjonction : </span>
                    <span>{sources.risk_model.conjunction_multiplier}</span>
                  </div>
                )}
                {sources.risk_model.thresholds && (
                  <div className="pt-3 border-t">
                    <p className="text-sm font-medium text-gray-500 mb-2">
                      Seuils d'alerte
                    </p>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
                      {sources.risk_model.thresholds.rain_24h && (
                        <div className="bg-gray-50 p-2 rounded">
                          <p className="font-medium mb-1">Pluie 24h (mm)</p>
                          <div className="flex gap-3 text-xs">
                            <span className="text-yellow-600">
                              J: {sources.risk_model.thresholds.rain_24h.yellow}
                            </span>
                            <span className="text-orange-600">
                              O: {sources.risk_model.thresholds.rain_24h.orange}
                            </span>
                            <span className="text-red-600">
                              R: {sources.risk_model.thresholds.rain_24h.red}
                            </span>
                          </div>
                        </div>
                      )}
                      {sources.risk_model.thresholds.dordogne_level && (
                        <div className="bg-gray-50 p-2 rounded">
                          <p className="font-medium mb-1">Niveau Dordogne (m)</p>
                          <div className="flex gap-3 text-xs">
                            <span className="text-yellow-600">
                              J: {sources.risk_model.thresholds.dordogne_level.yellow}
                            </span>
                            <span className="text-orange-600">
                              O: {sources.risk_model.thresholds.dordogne_level.orange}
                            </span>
                            <span className="text-red-600">
                              R: {sources.risk_model.thresholds.dordogne_level.red}
                            </span>
                          </div>
                        </div>
                      )}
                      {sources.risk_model.thresholds.tide_coef_high != null && (
                        <div className="bg-gray-50 p-2 rounded">
                          <p className="font-medium mb-1">Coef. maree (fort)</p>
                          <span className="text-xs text-orange-600">
                            {'>='} {sources.risk_model.thresholds.tide_coef_high}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      ) : null}

      <ForecastSkillPanel />

      {/* Risk Simulator */}
      <div className="bg-white rounded-lg shadow">
        <div className="p-4 border-b">
          <h3 className="font-medium flex items-center gap-2">
            <Sliders className="w-5 h-5" />
            Simulateur de risque
          </h3>
          <p className="text-sm text-gray-500 mt-1">
            Testez differentes combinaisons de conditions pour comprendre le modele de risque
          </p>
        </div>
        <div className="p-4">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {/* Inputs */}
            <div className="md:col-span-2 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium mb-1">
                    Pluie 24h (mm)
                  </label>
                  <input
                    type="number"
                    value={simParams.rain_24h_mm}
                    onChange={(e) =>
                      setSimParams({
                        ...simParams,
                        rain_24h_mm: parseFloat(e.target.value),
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                    min="0"
                    max="200"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">
                    Pluie 72h (mm)
                  </label>
                  <input
                    type="number"
                    value={simParams.rain_72h_mm}
                    onChange={(e) =>
                      setSimParams({
                        ...simParams,
                        rain_72h_mm: parseFloat(e.target.value),
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                    min="0"
                    max="400"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">
                    Niveau Dordogne (m)
                  </label>
                  <input
                    type="number"
                    step="0.1"
                    value={simParams.dordogne_level_m}
                    onChange={(e) =>
                      setSimParams({
                        ...simParams,
                        dordogne_level_m: parseFloat(e.target.value),
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                    min="0"
                    max="10"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">
                    Coefficient maree
                  </label>
                  <input
                    type="number"
                    value={simParams.tide_coefficient}
                    onChange={(e) =>
                      setSimParams({
                        ...simParams,
                        tide_coefficient: parseInt(e.target.value),
                      })
                    }
                    className="w-full border rounded px-3 py-2"
                    min="20"
                    max="120"
                  />
                </div>
                <div className="col-span-2">
                  <label className="block text-sm font-medium mb-1">
                    Heures jusqu'a pleine mer: {simParams.hours_to_high_tide}h
                  </label>
                  <input
                    type="range"
                    value={simParams.hours_to_high_tide}
                    onChange={(e) =>
                      setSimParams({
                        ...simParams,
                        hours_to_high_tide: parseFloat(e.target.value),
                      })
                    }
                    className="w-full"
                    min="-6"
                    max="6"
                    step="0.5"
                  />
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>-6h (apres PM)</span>
                    <span>0 (PM)</span>
                    <span>+6h (avant PM)</span>
                  </div>
                </div>
              </div>

              <button
                onClick={runSimulation}
                disabled={simulating}
                className="w-full py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
              >
                {simulating ? 'Calcul en cours...' : 'Calculer le risque'}
              </button>
              <ActionError error={simError} onDismiss={() => setSimError(null)} />
            </div>

            {/* Result */}
            <div className="bg-gray-50 rounded-lg p-4">
              <h4 className="font-medium mb-3">Resultat</h4>
              {simResult ? (
                <div className="space-y-3">
                  <div className="text-center">
                    <div
                      className={`inline-block px-4 py-2 rounded-lg text-white font-bold text-lg ${
                        simResult.risk.level === 'green'
                          ? 'bg-green-500'
                          : simResult.risk.level === 'yellow'
                          ? 'bg-yellow-500'
                          : simResult.risk.level === 'orange'
                          ? 'bg-orange-500'
                          : 'bg-red-500'
                      }`}
                    >
                      {simResult.risk.score.toFixed(1)} / 100
                    </div>
                    <p className="mt-2 text-sm">{simResult.risk.description}</p>
                  </div>
                  <div className="pt-3 border-t space-y-1 text-sm">
                    {simResult.risk.recommendations.map(
                      (rec: string, i: number) => (
                        <p key={i} className="text-gray-600">
                          {rec}
                        </p>
                      )
                    )}
                  </div>
                </div>
              ) : (
                <p className="text-gray-400 text-center">
                  Ajustez les parametres et lancez la simulation
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
