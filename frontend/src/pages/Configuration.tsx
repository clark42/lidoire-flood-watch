import { useState, useEffect } from 'react'
import { Save, RefreshCw, Sliders, MapPin, Database } from 'lucide-react'
import { getStatus, simulateRisk } from '../api'

export default function Configuration() {
  const [status, setStatus] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  // Simulation state
  const [simParams, setSimParams] = useState({
    rain_24h_mm: 30,
    rain_72h_mm: 60,
    dordogne_level_m: 3.0,
    tide_coefficient: 80,
    hours_to_high_tide: 2,
  })
  const [simResult, setSimResult] = useState<any>(null)
  const [simulating, setSimulating] = useState(false)

  useEffect(() => {
    fetchStatus()
  }, [])

  const fetchStatus = async () => {
    setLoading(true)
    try {
      const data = await getStatus()
      setStatus(data)
    } catch (err) {
      console.error('Error fetching status:', err)
    } finally {
      setLoading(false)
    }
  }

  const runSimulation = async () => {
    setSimulating(true)
    try {
      const result = await simulateRisk(simParams)
      setSimResult(result)
    } catch (err) {
      console.error('Error simulating:', err)
    } finally {
      setSimulating(false)
    }
  }

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-bold">Configuration</h2>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Status */}
        <div className="bg-white rounded-lg shadow">
          <div className="p-4 border-b flex items-center justify-between">
            <h3 className="font-medium flex items-center gap-2">
              <Database className="w-5 h-5" />
              Etat du systeme
            </h3>
            <button
              onClick={fetchStatus}
              className="p-2 hover:bg-gray-100 rounded"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
          <div className="p-4">
            {status ? (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-gray-500">Status</span>
                  <span className="px-2 py-1 bg-green-100 text-green-800 rounded text-sm">
                    {status.status}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-gray-500">Version</span>
                  <span>{status.version}</span>
                </div>

                <div className="pt-4 border-t">
                  <h4 className="text-sm font-medium text-gray-500 mb-2">
                    Services
                  </h4>
                  <div className="space-y-2">
                    {status.services?.collectors &&
                      Object.entries(status.services.collectors).map(
                        ([name, state]) => (
                          <div
                            key={name}
                            className="flex items-center justify-between text-sm"
                          >
                            <span className="capitalize">{name}</span>
                            <span
                              className={`px-2 py-0.5 rounded ${
                                state === 'running'
                                  ? 'bg-green-100 text-green-700'
                                  : 'bg-gray-100 text-gray-600'
                              }`}
                            >
                              {state as string}
                            </span>
                          </div>
                        )
                      )}
                  </div>
                </div>
              </div>
            ) : (
              <div className="text-gray-400 text-center py-4">Chargement...</div>
            )}
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

        {/* Risk Simulator */}
        <div className="bg-white rounded-lg shadow lg:col-span-2">
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
                            • {rec}
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
    </div>
  )
}
