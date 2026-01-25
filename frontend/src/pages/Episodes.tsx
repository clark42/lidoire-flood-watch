import { useState, useEffect } from 'react'
import { format } from 'date-fns'
import { fr } from 'date-fns/locale'
import { Plus, Search, ChevronRight, AlertTriangle } from 'lucide-react'
import { getEpisodes, createEpisode, analyzeExistingEpisode } from '../api'
import type { FloodEpisode, EpisodeAnalysis } from '../types'

export default function Episodes() {
  const [episodes, setEpisodes] = useState<FloodEpisode[]>([])
  const [loading, setLoading] = useState(true)
  const [showAddForm, setShowAddForm] = useState(false)
  const [selectedAnalysis, setSelectedAnalysis] = useState<EpisodeAnalysis | null>(null)
  const [analyzingId, setAnalyzingId] = useState<number | null>(null)

  useEffect(() => {
    fetchEpisodes()
  }, [])

  const fetchEpisodes = async () => {
    setLoading(true)
    try {
      const data = await getEpisodes()
      setEpisodes(data)
    } catch (err) {
      console.error('Error fetching episodes:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleAnalyze = async (id: number) => {
    setAnalyzingId(id)
    try {
      const analysis = await analyzeExistingEpisode(id)
      setSelectedAnalysis(analysis)
    } catch (err) {
      console.error('Error analyzing episode:', err)
    } finally {
      setAnalyzingId(null)
    }
  }

  const severityColors = [
    'bg-green-100 text-green-800',
    'bg-yellow-100 text-yellow-800',
    'bg-orange-100 text-orange-800',
    'bg-red-100 text-red-800',
    'bg-red-200 text-red-900',
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">Episodes d'inondation</h2>
        <button
          onClick={() => setShowAddForm(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          <Plus className="w-4 h-4" />
          Ajouter un episode
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Episodes List */}
        <div className="bg-white rounded-lg shadow">
          <div className="p-4 border-b">
            <h3 className="font-medium">Episodes enregistres</h3>
          </div>

          {loading ? (
            <div className="p-8 text-center text-gray-400">Chargement...</div>
          ) : episodes.length === 0 ? (
            <div className="p-8 text-center">
              <AlertTriangle className="w-12 h-12 text-gray-300 mx-auto mb-4" />
              <p className="text-gray-500">Aucun episode enregistre</p>
              <p className="text-sm text-gray-400 mt-2">
                Ajoutez des dates d'inondation pour analyser les patterns
              </p>
            </div>
          ) : (
            <div className="divide-y">
              {episodes.map((ep) => (
                <div
                  key={ep.id}
                  className="p-4 hover:bg-gray-50 cursor-pointer flex items-center justify-between"
                  onClick={() => ep.id && handleAnalyze(ep.id)}
                >
                  <div>
                    <div className="font-medium">
                      {format(new Date(ep.start_time), 'dd MMMM yyyy, HH:mm', {
                        locale: fr,
                      })}
                    </div>
                    {ep.description && (
                      <p className="text-sm text-gray-500">{ep.description}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-3">
                    <span
                      className={`px-2 py-1 rounded text-sm ${
                        severityColors[ep.severity - 1]
                      }`}
                    >
                      {ep.severity}/5
                    </span>
                    {analyzingId === ep.id ? (
                      <div className="w-5 h-5 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
                    ) : (
                      <ChevronRight className="w-5 h-5 text-gray-400" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Analysis Panel */}
        <div className="bg-white rounded-lg shadow">
          <div className="p-4 border-b">
            <h3 className="font-medium">Analyse de l'episode</h3>
          </div>

          {selectedAnalysis ? (
            <div className="p-4 space-y-4">
              {/* Episode info */}
              <div className="bg-gray-50 rounded-lg p-4">
                <h4 className="font-medium mb-2">
                  {format(
                    new Date(selectedAnalysis.episode.start_time),
                    'dd MMMM yyyy, HH:mm',
                    { locale: fr }
                  )}
                </h4>
                <p className="text-sm text-gray-600">
                  {selectedAnalysis.episode.description}
                </p>
              </div>

              {/* Context metrics */}
              <div>
                <h4 className="font-medium mb-2">Contexte hydro-meteo</h4>
                <div className="grid grid-cols-2 gap-3">
                  <MetricCard
                    label="Pluie 24h avant"
                    value={`${selectedAnalysis.context.rain_24h_before_mm.toFixed(1)} mm`}
                  />
                  <MetricCard
                    label="Pluie 72h avant"
                    value={`${selectedAnalysis.context.rain_72h_before_mm.toFixed(1)} mm`}
                  />
                  <MetricCard
                    label="Max Dordogne"
                    value={`${selectedAnalysis.context.max_dordogne_level_m.toFixed(2)} m`}
                  />
                  <MetricCard
                    label="Max coef maree"
                    value={selectedAnalysis.context.max_tide_coefficient.toString()}
                  />
                </div>
              </div>

              {/* Matched criteria */}
              {selectedAnalysis.matched_criteria.length > 0 && (
                <div>
                  <h4 className="font-medium mb-2">Criteres atteints</h4>
                  <ul className="space-y-1">
                    {selectedAnalysis.matched_criteria.map((criteria, i) => (
                      <li
                        key={i}
                        className="flex items-start gap-2 text-sm text-orange-700 bg-orange-50 p-2 rounded"
                      >
                        <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                        {criteria}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <div className="p-8 text-center text-gray-400">
              <Search className="w-12 h-12 mx-auto mb-4 opacity-50" />
              <p>Selectionnez un episode pour voir l'analyse</p>
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
    <div className="bg-gray-50 rounded p-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="font-bold text-lg">{value}</div>
    </div>
  )
}

function AddEpisodeModal({
  onClose,
  onSave,
}: {
  onClose: () => void
  onSave: (episode: any) => Promise<void>
}) {
  const [formData, setFormData] = useState({
    start_time: format(new Date(), "yyyy-MM-dd'T'HH:mm"),
    peak_time: '',
    severity: 3,
    description: '',
    notes: '',
  })
  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    try {
      await onSave({
        ...formData,
        start_time: new Date(formData.start_time).toISOString(),
        peak_time: formData.peak_time
          ? new Date(formData.peak_time).toISOString()
          : null,
      })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md">
        <div className="p-4 border-b">
          <h3 className="font-bold text-lg">Nouvel episode d'inondation</h3>
        </div>
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">
              Date/heure de debut *
            </label>
            <input
              type="datetime-local"
              value={formData.start_time}
              onChange={(e) =>
                setFormData({ ...formData, start_time: e.target.value })
              }
              className="w-full border rounded px-3 py-2"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">
              Date/heure du pic
            </label>
            <input
              type="datetime-local"
              value={formData.peak_time}
              onChange={(e) =>
                setFormData({ ...formData, peak_time: e.target.value })
              }
              className="w-full border rounded px-3 py-2"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">
              Severite (1-5) *
            </label>
            <input
              type="range"
              min="1"
              max="5"
              value={formData.severity}
              onChange={(e) =>
                setFormData({ ...formData, severity: parseInt(e.target.value) })
              }
              className="w-full"
            />
            <div className="flex justify-between text-xs text-gray-500">
              <span>1 - Mineur</span>
              <span>3 - Modere</span>
              <span>5 - Majeur</span>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Description</label>
            <input
              type="text"
              value={formData.description}
              onChange={(e) =>
                setFormData({ ...formData, description: e.target.value })
              }
              className="w-full border rounded px-3 py-2"
              placeholder="Ex: Crue suite a orage violent"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Notes</label>
            <textarea
              value={formData.notes}
              onChange={(e) =>
                setFormData({ ...formData, notes: e.target.value })
              }
              className="w-full border rounded px-3 py-2"
              rows={3}
              placeholder="Observations, photos, etc."
            />
          </div>

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
