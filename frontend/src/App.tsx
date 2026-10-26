import { lazy, Suspense } from 'react'
import { Routes, Route, NavLink } from 'react-router-dom'
import { Waves, Clock, Calendar, Settings, AlertTriangle, RefreshCw } from 'lucide-react'

// One chunk per route: recharts (Dashboard, Timeline) and leaflet (HydroMap)
// stay out of the entry chunk
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Timeline = lazy(() => import('./pages/Timeline'))
const Episodes = lazy(() => import('./pages/Episodes'))
const Configuration = lazy(() => import('./pages/Configuration'))

function PageFallback() {
  return (
    <div className="flex items-center justify-center h-64">
      <RefreshCw className="w-8 h-8 animate-spin text-blue-500" />
    </div>
  )
}

function App() {
  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="bg-blue-700 text-white shadow-lg">
        <div className="container mx-auto px-4 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <Waves className="w-8 h-8" />
              <div>
                <h1 className="text-xl font-bold">Lidoire Flood Watch</h1>
                <p className="text-blue-200 text-sm">Surveillance du moulin</p>
              </div>
            </div>
            <nav className="flex gap-1">
              <NavLink
                to="/"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-4 py-2 rounded-lg transition ${
                    isActive ? 'bg-blue-600' : 'hover:bg-blue-600/50'
                  }`
                }
              >
                <AlertTriangle className="w-4 h-4" />
                <span>Dashboard</span>
              </NavLink>
              <NavLink
                to="/timeline"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-4 py-2 rounded-lg transition ${
                    isActive ? 'bg-blue-600' : 'hover:bg-blue-600/50'
                  }`
                }
              >
                <Clock className="w-4 h-4" />
                <span>Timeline</span>
              </NavLink>
              <NavLink
                to="/episodes"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-4 py-2 rounded-lg transition ${
                    isActive ? 'bg-blue-600' : 'hover:bg-blue-600/50'
                  }`
                }
              >
                <Calendar className="w-4 h-4" />
                <span>Episodes</span>
              </NavLink>
              <NavLink
                to="/config"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-4 py-2 rounded-lg transition ${
                    isActive ? 'bg-blue-600' : 'hover:bg-blue-600/50'
                  }`
                }
              >
                <Settings className="w-4 h-4" />
                <span>Config</span>
              </NavLink>
            </nav>
          </div>
        </div>
      </header>

      {/* Main content */}
      <main className="flex-1 container mx-auto px-4 py-6">
        <Suspense fallback={<PageFallback />}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/timeline" element={<Timeline />} />
            <Route path="/episodes" element={<Episodes />} />
            <Route path="/config" element={<Configuration />} />
          </Routes>
        </Suspense>
      </main>

      {/* Footer */}
      <footer className="bg-gray-100 border-t py-4">
        <div className="container mx-auto px-4 text-center text-gray-500 text-sm">
          Lidoire Flood Watch &bull; Moulin (44.8759811, 0.0015091)
        </div>
      </footer>
    </div>
  )
}

export default App
