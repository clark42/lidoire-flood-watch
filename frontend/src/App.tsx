import { Routes, Route, NavLink } from 'react-router-dom'
import { Waves, Clock, Calendar, Settings, AlertTriangle } from 'lucide-react'
import Dashboard from './pages/Dashboard'
import Timeline from './pages/Timeline'
import Episodes from './pages/Episodes'
// import Photos from './pages/Photos'
import Configuration from './pages/Configuration'

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
              {/* Photos page temporarily disabled
              <NavLink
                to="/photos"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-4 py-2 rounded-lg transition ${
                    isActive ? 'bg-blue-600' : 'hover:bg-blue-600/50'
                  }`
                }
              >
                <Camera className="w-4 h-4" />
                <span>Photos</span>
              </NavLink>
              */}
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
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/timeline" element={<Timeline />} />
          <Route path="/episodes" element={<Episodes />} />
          {/* <Route path="/photos" element={<Photos />} /> */}
          <Route path="/config" element={<Configuration />} />
        </Routes>
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
