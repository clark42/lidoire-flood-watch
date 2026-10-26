import { lazy, Suspense } from 'react'
import { Routes, Route, NavLink } from 'react-router-dom'
import { Waves, Clock, Calendar, Settings, AlertTriangle, RefreshCw } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import ThemeToggle from './components/ThemeToggle'

// One chunk per route: recharts (Dashboard, Timeline) and leaflet (HydroMap)
// stay out of the entry chunk
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Timeline = lazy(() => import('./pages/Timeline'))
const Episodes = lazy(() => import('./pages/Episodes'))
const Configuration = lazy(() => import('./pages/Configuration'))

const PAGES: { to: string; label: string; icon: LucideIcon }[] = [
  { to: '/', label: 'Dashboard', icon: AlertTriangle },
  { to: '/timeline', label: 'Timeline', icon: Clock },
  { to: '/episodes', label: 'Episodes', icon: Calendar },
  { to: '/config', label: 'Config', icon: Settings },
]

function PageFallback() {
  return (
    <div className="flex items-center justify-center h-64" role="status" aria-label="Chargement">
      <RefreshCw className="w-8 h-8 animate-spin text-accent" aria-hidden="true" />
    </div>
  )
}

function App() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-line bg-surface">
        <div className="page-gutter mx-auto max-w-[1180px] py-2 sm:py-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
          <div className="flex items-center gap-2.5 min-w-0">
            <Waves className="w-7 h-7 flex-none text-accent" aria-hidden="true" />
            <div className="min-w-0">
              <h1 className="font-display text-xl font-bold leading-tight tracking-[0.02em]">Lidoire Flood Watch</h1>
              <p className="text-ink-3 text-[13px] leading-tight">Surveillance du moulin</p>
            </div>
          </div>
          <div className="sm:order-last">
            <ThemeToggle />
          </div>
          {/* Phones: 4 equal tabs (44 px targets); wider screens: inline links */}
          <nav aria-label="Pages" className="grid w-full grid-cols-4 gap-1 sm:flex sm:w-auto sm:ml-auto">
            {PAGES.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                className={({ isActive }) =>
                  `flex min-h-[44px] flex-col items-center justify-center gap-0.5 rounded-lg px-2 text-xs sm:flex-row sm:gap-2 sm:px-3 sm:text-sm ${
                    isActive ? 'bg-ink text-canvas' : 'text-ink-2 hover:bg-surface-2 hover:text-ink'
                  }`
                }
              >
                <Icon className="w-4 h-4" aria-hidden="true" />
                <span>{label}</span>
              </NavLink>
            ))}
          </nav>
        </div>
      </header>

      {/* Main content */}
      <main className="page-gutter mx-auto w-full max-w-[1180px] flex-1 pb-12 pt-4">
        <Suspense fallback={<PageFallback />}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/timeline" element={<Timeline />} />
            <Route path="/episodes" element={<Episodes />} />
            <Route path="/config" element={<Configuration />} />
          </Routes>
        </Suspense>
      </main>

      <footer className="border-t border-line py-4">
        <div className="page-gutter mx-auto max-w-[1180px] text-center text-ink-3 text-sm">
          Lidoire Flood Watch &bull; Moulin <span className="font-mono text-[12.5px]">(44.8759811, 0.0015091)</span>
        </div>
      </footer>
    </div>
  )
}

export default App
