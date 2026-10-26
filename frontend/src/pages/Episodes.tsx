import { useState, useEffect, useCallback } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { format } from 'date-fns'
import {
  Plus,
  Search,
  ChevronRight,
  AlertTriangle,
  RefreshCw,
  Clock,
  BarChart3,
  Check,
  X,
  Trash2,
  Edit3,
  Save,
  CheckCircle,
  XCircle,
  HelpCircle,
  Waves,
} from 'lucide-react'
import {
  getEpisodes,
  createEpisode,
  updateEpisode,
  deleteEpisode,
  confirmEpisode,
  analyzeExistingEpisode,
  errorMessage,
  toActionError,
} from '../api'
import type { ActionErrorState } from '../api'
import type { FloodEpisode, EpisodeAnalysis, EpisodeOutcome } from '../types'
import ActionError from '../components/ActionError'
import { formatParis } from '../utils/time'

// Ground truth comes from the structured `outcome` field (null = not labelled yet)
const OUTCOME_OPTIONS: { value: EpisodeOutcome; label: string }[] = [
  { value: 'flooded', label: 'Inonde' },
  { value: 'near_miss', label: 'Presque (eau haute, pas inonde)' },
  { value: 'false_positive', label: 'Faux positif' },
  { value: 'unknown', label: 'Inconnu' },
]

const OUTCOME_LABELS: Record<EpisodeOutcome, string> = {
  flooded: 'Inonde',
  near_miss: 'Presque',
  false_positive: 'Faux positif',
  unknown: 'Inconnu',
}

function isPending(ep: FloodEpisode): boolean {
  return !ep.outcome
}

// Backend datetimes are UTC ("Z"); legacy strings without offset are UTC too
function parseApiDate(value: string): Date {
  return new Date(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`)
}

function toLocalInput(value?: string | null): string {
  return value ? format(parseApiDate(value), "yyyy-MM-dd'T'HH:mm") : ''
}

export default function Episodes() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const [episodes, setEpisodes] = useState<FloodEpisode[]>([])
  const [loading, setLoading] = useState(true)
  const [showAddForm, setShowAddForm] = useState(false)
  const [selectedEpisode, setSelectedEpisode] = useState<FloodEpisode | null>(null)
  const [selectedAnalysis, setSelectedAnalysis] = useState<EpisodeAnalysis | null>(null)
  const [analyzingId, setAnalyzingId] = useState<number | null>(null)
  const [analyzeError, setAnalyzeError] = useState<string | null>(null)
  const [editingNotes, setEditingNotes] = useState<string>('')
  const [editingSeverity, setEditingSeverity] = useState<number>(3)
  const [editingOutcome, setEditingOutcome] = useState<EpisodeOutcome | ''>('')
  const [editingWaterHeight, setEditingWaterHeight] = useState<string>('')
  const [editingObservedAt, setEditingObservedAt] = useState<string>('')
  const [isEditing, setIsEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  // Error of the last action on the selected episode (confirm/save/delete)
  const [actionError, setActionError] = useState<ActionErrorState | null>(null)
  const [listError, setListError] = useState<string | null>(null)

  useEffect(() => {
    fetchEpisodes()
  }, [])

  const fetchEpisodes = async () => {
    setLoading(true)
    setListError(null)
    try {
      const data = await getEpisodes(100)
      // Sort by peak_time descending
      data.sort((a, b) => {
        const ta = parseApiDate(a.peak_time || a.start_time).getTime()
        const tb = parseApiDate(b.peak_time || b.start_time).getTime()
        return tb - ta
      })
      setEpisodes(data)
    } catch (err) {
      console.error('Error fetching episodes:', err)
      setListError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const resetEditing = useCallback((ep: FloodEpisode) => {
    setEditingNotes(ep.notes || '')
    setEditingSeverity(ep.severity)
    setEditingOutcome(ep.outcome || '')
    setEditingWaterHeight(
      ep.water_height_cm !== null && ep.water_height_cm !== undefined
        ? ep.water_height_cm.toString()
        : ''
    )
    setEditingObservedAt(toLocalInput(ep.observed_at))
  }, [])

  const replaceEpisode = (updated: FloodEpisode) => {
    setEpisodes((prev) => prev.map((ep) => (ep.id === updated.id ? updated : ep)))
    setSelectedEpisode(updated)
    resetEditing(updated)
  }

  const handleSelectEpisode = useCallback(
    async (ep: FloodEpisode) => {
      setSelectedEpisode(ep)
      resetEditing(ep)
      setIsEditing(false)
      setAnalyzeError(null)
      setActionError(null)

      if (ep.id) {
        setSearchParams({ id: ep.id.toString() }, { replace: true })
        setAnalyzingId(ep.id)
        try {
          const analysis = await analyzeExistingEpisode(ep.id)
          setSelectedAnalysis(analysis)
        } catch (err) {
          const message = err instanceof Error ? err.message : "Erreur lors de l'analyse"
          setAnalyzeError(message)
          console.error('Error analyzing episode:', err)
        } finally {
          setAnalyzingId(null)
        }
      }
    },
    [setSearchParams, resetEditing]
  )

  // Auto-select from URL ?id= on mount
  useEffect(() => {
    const idParam = searchParams.get('id')
    if (idParam && episodes.length > 0 && !selectedEpisode) {
      const id = parseInt(idParam, 10)
      const ep = episodes.find((e) => e.id === id)
      if (ep) handleSelectEpisode(ep)
    }
  }, [episodes, searchParams, selectedEpisode, handleSelectEpisode])

  const handleConfirm = async (outcome: EpisodeOutcome) => {
    if (!selectedEpisode?.id) return
    setSaving(true)
    setActionError(null)
    try {
      // Re-confirming replaces the outcome server-side (no notes prefixes)
      const result = await confirmEpisode(selectedEpisode.id, outcome, {
        severity: editingSeverity,
        source: 'user',
      })
      replaceEpisode(result.episode)
      setIsEditing(false)
    } catch (err) {
      console.error('Error confirming episode:', err)
      setActionError(toActionError(err, 'Validation impossible'))
    } finally {
      setSaving(false)
    }
  }

  const handleSaveNotes = async () => {
    if (!selectedEpisode?.id) return
    setSaving(true)
    setActionError(null)
    try {
      const outcome = editingOutcome || null
      const outcomeChanged = outcome !== (selectedEpisode.outcome || null)
      const waterHeight = editingWaterHeight.trim() === '' ? null : Number(editingWaterHeight)
      const observedChanged = editingObservedAt !== toLocalInput(selectedEpisode.observed_at)
      const updated = await updateEpisode(selectedEpisode.id, {
        ...selectedEpisode,
        notes: editingNotes,
        severity: editingSeverity,
        outcome,
        outcome_source: outcomeChanged ? (outcome ? 'user' : null) : selectedEpisode.outcome_source,
        water_height_cm: waterHeight !== null && Number.isFinite(waterHeight) ? waterHeight : null,
        observed_at: observedChanged
          ? editingObservedAt
            ? new Date(editingObservedAt).toISOString()
            : null
          : selectedEpisode.observed_at,
      })
      replaceEpisode(updated)
      setIsEditing(false)
    } catch (err) {
      console.error('Error saving notes:', err)
      setActionError(toActionError(err, 'Enregistrement impossible'))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async () => {
    if (!selectedEpisode?.id) return
    if (!confirm('Supprimer cet episode ?')) return
    setActionError(null)
    try {
      await deleteEpisode(selectedEpisode.id)
      setEpisodes((prev) => prev.filter((ep) => ep.id !== selectedEpisode.id))
      setSelectedEpisode(null)
      setSelectedAnalysis(null)
      setSearchParams({}, { replace: true })
    } catch (err) {
      console.error('Error deleting episode:', err)
      setActionError(toActionError(err, 'Suppression impossible'))
    }
  }

  const severityColors = [
    'bg-green-100 text-green-800 border-green-300',
    'bg-yellow-100 text-yellow-800 border-yellow-300',
    'bg-orange-100 text-orange-800 border-orange-300',
    'bg-red-100 text-red-800 border-red-300',
    'bg-red-200 text-red-900 border-red-400',
  ]

  const statusBadge = (outcome?: EpisodeOutcome | null) => {
    if (outcome === 'flooded')
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-red-100 text-red-700">
          <CheckCircle className="w-3 h-3" /> {OUTCOME_LABELS.flooded}
        </span>
      )
    if (outcome === 'near_miss')
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-orange-100 text-orange-700">
          <Waves className="w-3 h-3" /> {OUTCOME_LABELS.near_miss}
        </span>
      )
    if (outcome === 'false_positive')
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600">
          <XCircle className="w-3 h-3" /> {OUTCOME_LABELS.false_positive}
        </span>
      )
    if (outcome === 'unknown')
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-slate-100 text-slate-600">
          <HelpCircle className="w-3 h-3" /> {OUTCOME_LABELS.unknown}
        </span>
      )
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-amber-100 text-amber-700">
        A valider
      </span>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">Episodes d'inondation</h2>
          <p className="text-sm text-gray-500 mt-1">
            {episodes.length} episodes - {episodes.filter(isPending).length} a valider
          </p>
        </div>
        <button
          onClick={() => setShowAddForm(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          <Plus className="w-4 h-4" />
          Ajouter
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Episodes List */}
        <div className="bg-white rounded-lg shadow max-h-[calc(100vh-200px)] overflow-hidden flex flex-col">
          <div className="p-4 border-b bg-gray-50">
            <h3 className="font-medium">Episodes detectes</h3>
          </div>

          {loading ? (
            <div className="p-8 text-center text-gray-400">Chargement...</div>
          ) : listError ? (
            <div className="p-8 text-center space-y-3">
              <AlertTriangle className="w-12 h-12 text-red-400 mx-auto" />
              <p className="text-red-600 text-sm">Chargement des episodes impossible : {listError}</p>
              <button
                onClick={fetchEpisodes}
                className="inline-flex items-center gap-2 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
              >
                <RefreshCw className="w-4 h-4" />
                Reessayer
              </button>
            </div>
          ) : episodes.length === 0 ? (
            <div className="p-8 text-center">
              <AlertTriangle className="w-12 h-12 text-gray-300 mx-auto mb-4" />
              <p className="text-gray-500">Aucun episode enregistre</p>
            </div>
          ) : (
            <div className="divide-y overflow-y-auto flex-1">
              {episodes.map((ep) => {
                const isSelected = selectedEpisode?.id === ep.id
                return (
                  <div
                    key={ep.id}
                    className={`p-3 cursor-pointer flex items-center justify-between transition-colors ${
                      isSelected ? 'bg-blue-50 border-l-4 border-blue-500' : 'hover:bg-gray-50'
                    }`}
                    onClick={() => handleSelectEpisode(ep)}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium">
                          {formatParis(ep.peak_time || ep.start_time, 'dd MMM yyyy')}
                        </span>
                        {statusBadge(ep.outcome)}
                      </div>
                      {ep.description && (
                        <p className="text-sm text-gray-500 truncate mt-0.5">{ep.description}</p>
                      )}
                    </div>
                    <div className="flex items-center gap-2 ml-2">
                      <span
                        className={`px-2 py-0.5 rounded text-sm border ${severityColors[ep.severity - 1]}`}
                      >
                        {ep.severity}/5
                      </span>
                      {analyzingId === ep.id ? (
                        <RefreshCw className="w-4 h-4 text-blue-500 animate-spin" />
                      ) : (
                        <ChevronRight className="w-4 h-4 text-gray-400" />
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Detail Panel */}
        <div className="bg-white rounded-lg shadow">
          <div className="p-4 border-b bg-gray-50 flex items-center justify-between">
            <h3 className="font-medium">Detail de l'episode</h3>
            {selectedEpisode && (
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setIsEditing(!isEditing)}
                  className="p-1.5 rounded hover:bg-gray-200 text-gray-600"
                  title="Editer"
                >
                  <Edit3 className="w-4 h-4" />
                </button>
                <button
                  onClick={handleDelete}
                  className="p-1.5 rounded hover:bg-red-100 text-red-600"
                  title="Supprimer"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            )}
          </div>

          <ActionError
            error={actionError}
            onDismiss={() => setActionError(null)}
            className="m-4 mb-0"
          />

          {analyzeError ? (
            <div className="p-6 text-center space-y-4">
              <AlertTriangle className="w-12 h-12 text-red-400 mx-auto" />
              <p className="text-red-600">{analyzeError}</p>
              <button
                onClick={() => selectedEpisode && handleSelectEpisode(selectedEpisode)}
                className="inline-flex items-center gap-2 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
              >
                <RefreshCw className="w-4 h-4" />
                Reessayer
              </button>
            </div>
          ) : selectedEpisode ? (
            <div className="p-4 space-y-4 max-h-[calc(100vh-300px)] overflow-y-auto">
              {/* Episode header */}
              <div className="bg-gray-50 rounded-lg p-4">
                <div className="flex items-center justify-between mb-2">
                  <h4 className="font-medium">
                    {formatParis(
                      selectedEpisode.peak_time || selectedEpisode.start_time,
                      'dd MMMM yyyy HH:mm',
                    )}
                  </h4>
                  {statusBadge(selectedEpisode.outcome)}
                </div>
                <p className="text-sm text-gray-600">{selectedEpisode.description}</p>
                {(selectedEpisode.water_height_cm != null ||
                  selectedEpisode.observed_at ||
                  selectedEpisode.outcome_source) && (
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
                    {selectedEpisode.water_height_cm != null && (
                      <span>Hauteur d'eau : {selectedEpisode.water_height_cm} cm</span>
                    )}
                    {selectedEpisode.observed_at && (
                      <span>
                        Pic observe :{' '}
                        {formatParis(selectedEpisode.observed_at, 'dd MMM yyyy HH:mm')}
                      </span>
                    )}
                    {selectedEpisode.outcome_source && (
                      <span>Source : {selectedEpisode.outcome_source}</span>
                    )}
                  </div>
                )}
              </div>

              {/* Severity edit */}
              {isEditing && (
                <div className="bg-amber-50 rounded-lg p-4 space-y-3">
                  <label className="block text-sm font-medium" htmlFor="episode-outcome">
                    Resultat au moulin
                  </label>
                  <select
                    id="episode-outcome"
                    value={editingOutcome}
                    onChange={(e) => setEditingOutcome(e.target.value as EpisodeOutcome | '')}
                    className="w-full border rounded px-3 py-2 text-sm bg-white"
                  >
                    <option value="">A valider (non qualifie)</option>
                    {OUTCOME_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>

                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-sm font-medium" htmlFor="episode-water-height">
                        Hauteur d'eau (cm)
                      </label>
                      <input
                        id="episode-water-height"
                        type="number"
                        min="0"
                        step="1"
                        value={editingWaterHeight}
                        onChange={(e) => setEditingWaterHeight(e.target.value)}
                        className="w-full border rounded px-3 py-2 text-sm"
                        placeholder="ex: 25"
                      />
                    </div>
                    <div>
                      <label className="block text-sm font-medium" htmlFor="episode-observed-at">
                        Pic observe au moulin
                      </label>
                      <input
                        id="episode-observed-at"
                        type="datetime-local"
                        value={editingObservedAt}
                        onChange={(e) => setEditingObservedAt(e.target.value)}
                        className="w-full border rounded px-3 py-2 text-sm"
                      />
                    </div>
                  </div>

                  <label className="block text-sm font-medium">Severite reelle</label>
                  <input
                    type="range"
                    min="1"
                    max="5"
                    value={editingSeverity}
                    onChange={(e) => setEditingSeverity(parseInt(e.target.value))}
                    className="w-full"
                  />
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>1 - Mineur</span>
                    <span>3 - Modere</span>
                    <span>5 - Majeur</span>
                  </div>

                  <label className="block text-sm font-medium mt-4">Notes</label>
                  <textarea
                    value={editingNotes}
                    onChange={(e) => setEditingNotes(e.target.value)}
                    className="w-full border rounded px-3 py-2 text-sm"
                    rows={3}
                    placeholder="Niveau atteint au moulin, degats, observations..."
                  />

                  <button
                    onClick={handleSaveNotes}
                    disabled={saving}
                    className="flex items-center gap-2 px-3 py-1.5 bg-amber-600 text-white rounded text-sm hover:bg-amber-700 disabled:opacity-50"
                  >
                    <Save className="w-4 h-4" />
                    Enregistrer
                  </button>
                </div>
              )}

              {/* Existing notes display */}
              {!isEditing && selectedEpisode.notes && (
                <div className="bg-gray-50 rounded-lg p-3">
                  <div className="text-xs font-medium text-gray-500 mb-1">Notes</div>
                  <p className="text-sm whitespace-pre-wrap">{selectedEpisode.notes}</p>
                </div>
              )}

              {/* Context metrics from analysis */}
              {selectedAnalysis && (
                <div>
                  <h4 className="font-medium mb-2 text-sm text-gray-600">Contexte hydro-meteo</h4>
                  <div className="grid grid-cols-2 gap-2">
                    <MetricCard
                      label="Pluie 24h"
                      value={`${selectedAnalysis.context.rain_24h_before_mm.toFixed(1)} mm`}
                    />
                    <MetricCard
                      label="Pluie 72h"
                      value={`${selectedAnalysis.context.rain_72h_before_mm.toFixed(1)} mm`}
                    />
                    <MetricCard
                      label="Max Dordogne"
                      value={`${selectedAnalysis.context.max_dordogne_level_m.toFixed(2)} m`}
                    />
                    <MetricCard
                      label="Coef maree"
                      value={selectedAnalysis.context.max_tide_coefficient.toString()}
                    />
                  </div>
                </div>
              )}

              {/* Matched criteria */}
              {selectedAnalysis && selectedAnalysis.matched_criteria.length > 0 && (
                <div>
                  <h4 className="font-medium mb-2 text-sm text-gray-600">Criteres atteints</h4>
                  <ul className="space-y-1">
                    {selectedAnalysis.matched_criteria.slice(0, 6).map((criteria, i) => (
                      <li
                        key={i}
                        className="flex items-start gap-2 text-xs text-orange-700 bg-orange-50 p-2 rounded"
                      >
                        <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-0.5" />
                        {criteria}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Confirmation buttons */}
              {isPending(selectedEpisode) && (
                <div className="border-t pt-4">
                  <p className="text-sm text-gray-600 mb-3">
                    Est-ce que le moulin a ete inonde a cette date ?
                  </p>
                  <div className="flex flex-wrap gap-3">
                    <button
                      onClick={() => handleConfirm('flooded')}
                      disabled={saving}
                      className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
                    >
                      <Check className="w-4 h-4" />
                      Oui, inonde
                    </button>
                    <button
                      onClick={() => handleConfirm('near_miss')}
                      disabled={saving}
                      className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-orange-500 text-white rounded-lg hover:bg-orange-600 disabled:opacity-50"
                    >
                      <Waves className="w-4 h-4" />
                      Presque
                    </button>
                    <button
                      onClick={() => handleConfirm('false_positive')}
                      disabled={saving}
                      className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-gray-600 text-white rounded-lg hover:bg-gray-700 disabled:opacity-50"
                    >
                      <X className="w-4 h-4" />
                      Non, faux positif
                    </button>
                  </div>
                  <p className="text-xs text-gray-400 mt-2">
                    Hauteur d'eau et heure du pic : bouton Editer.
                  </p>
                </div>
              )}

              {!isPending(selectedEpisode) && !isEditing && (
                <div className="border-t pt-3 text-xs text-gray-500">
                  Resultat errone ?{' '}
                  <button
                    onClick={() => setIsEditing(true)}
                    className="text-blue-600 hover:underline"
                  >
                    Requalifier
                  </button>
                </div>
              )}

              {/* Navigation buttons */}
              <div className="flex gap-2 pt-2 border-t">
                <button
                  onClick={() => {
                    const targetDate = selectedEpisode.peak_time || selectedEpisode.start_time
                    navigate(`/?tm=${parseApiDate(targetDate).toISOString()}`)
                  }}
                  className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-cyan-50 text-cyan-700 rounded-lg hover:bg-cyan-100 text-sm font-medium"
                >
                  <Clock className="w-4 h-4" />
                  TimeMachine
                </button>
                <button
                  onClick={() => {
                    const start = parseApiDate(selectedEpisode.start_time)
                    const end = selectedEpisode.end_time
                      ? parseApiDate(selectedEpisode.end_time)
                      : new Date(start.getTime() + 5 * 24 * 60 * 60 * 1000)
                    const bufferStart = new Date(start.getTime() - 3 * 24 * 60 * 60 * 1000)
                    navigate(`/timeline?start=${bufferStart.toISOString()}&end=${end.toISOString()}`)
                  }}
                  className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-indigo-50 text-indigo-700 rounded-lg hover:bg-indigo-100 text-sm font-medium"
                >
                  <BarChart3 className="w-4 h-4" />
                  Timeline
                </button>
              </div>
            </div>
          ) : (
            <div className="p-8 text-center text-gray-400">
              <Search className="w-12 h-12 mx-auto mb-4 opacity-50" />
              <p>Selectionnez un episode pour voir le detail</p>
            </div>
          )}
        </div>
      </div>

      {/* Add Episode Modal */}
      {showAddForm && (
        <AddEpisodeModal
          onClose={() => setShowAddForm(false)}
          onSave={async (episode) => {
            await createEpisode(episode)
            setShowAddForm(false)
            fetchEpisodes()
          }}
        />
      )}
    </div>
  )
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-50 rounded p-2">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="font-bold">{value}</div>
    </div>
  )
}

function AddEpisodeModal({
  onClose,
  onSave,
}: {
  onClose: () => void
  onSave: (episode: Omit<FloodEpisode, 'id' | 'created_at'>) => Promise<void>
}) {
  const [formData, setFormData] = useState({
    start_time: format(new Date(), "yyyy-MM-dd'T'HH:mm"),
    peak_time: '',
    severity: 3,
    description: '',
    notes: '',
    outcome: '' as EpisodeOutcome | '',
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<ActionErrorState | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await onSave({
        ...formData,
        start_time: new Date(formData.start_time).toISOString(),
        peak_time: formData.peak_time ? new Date(formData.peak_time).toISOString() : null,
        end_time: null,
        outcome: formData.outcome || null,
        outcome_source: formData.outcome ? 'user' : null,
      })
    } catch (err) {
      // The modal stays open with the form data so the user can retry
      console.error('Error creating episode:', err)
      setError(toActionError(err, 'Creation impossible'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md">
        <div className="p-4 border-b">
          <h3 className="font-bold text-lg">Nouvel episode</h3>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">Date/heure de debut *</label>
            <input
              type="datetime-local"
              value={formData.start_time}
              onChange={(e) => setFormData({ ...formData, start_time: e.target.value })}
              className="w-full border rounded px-3 py-2"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Date/heure du pic</label>
            <input
              type="datetime-local"
              value={formData.peak_time}
              onChange={(e) => setFormData({ ...formData, peak_time: e.target.value })}
              className="w-full border rounded px-3 py-2"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Severite (1-5) *</label>
            <input
              type="range"
              min="1"
              max="5"
              value={formData.severity}
              onChange={(e) => setFormData({ ...formData, severity: parseInt(e.target.value) })}
              className="w-full"
            />
            <div className="flex justify-between text-xs text-gray-500">
              <span>1 - Mineur</span>
              <span>3 - Modere</span>
              <span>5 - Majeur</span>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="new-episode-outcome">
              Resultat au moulin
            </label>
            <select
              id="new-episode-outcome"
              value={formData.outcome}
              onChange={(e) =>
                setFormData({ ...formData, outcome: e.target.value as EpisodeOutcome | '' })
              }
              className="w-full border rounded px-3 py-2 bg-white"
            >
              <option value="">A valider (non qualifie)</option>
              {OUTCOME_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Description</label>
            <input
              type="text"
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              className="w-full border rounded px-3 py-2"
              placeholder="Ex: Crue suite a orage violent"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Notes</label>
            <textarea
              value={formData.notes}
              onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
              className="w-full border rounded px-3 py-2"
              rows={3}
              placeholder="Observations, niveau atteint, degats..."
            />
          </div>

          <ActionError error={error} onDismiss={() => setError(null)} />

          <div className="flex gap-3 pt-4">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 border rounded-lg hover:bg-gray-50"
            >
              Annuler
            </button>
            <button
              type="submit"
              disabled={saving}
              className="flex-1 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              {saving ? 'Enregistrement...' : 'Enregistrer'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
