import { lazy, Suspense, useState, type ComponentProps } from 'react'

// Leaflet (~48 kB gzip + its CSS) and the OSM tiles are only loaded when
// the user asks for the detailed map: the Dashboard draws the SVG basin map.
const HydroMap = lazy(() => import('./HydroMap'))

/** Optional OpenStreetMap view (rain gauges, tide), loaded on demand. */
export default function DetailedMap(props: ComponentProps<typeof HydroMap>) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="min-h-[44px] rounded-lg border border-line-2 bg-surface px-3 text-[14px] text-ink-2 hover:bg-surface-2 sm:min-h-[36px]"
      >
        {open ? 'Masquer la carte détaillée' : 'Carte détaillée (fond OpenStreetMap, pluviomètres)'}
      </button>
      {open && (
        // isolate: Leaflet panes (z-index 400-1000) stay under the sticky section bar
        <div className="isolate mt-3">
          <Suspense fallback={<div className="h-[380px] w-full animate-pulse rounded-card bg-surface-2" />}>
            <HydroMap {...props} />
          </Suspense>
        </div>
      )}
    </div>
  )
}
