import { useEffect, useState } from 'react'
import { GeoJSON, Tooltip } from 'react-leaflet'
import { getBasinGeoJson } from '../api'
import type { BasinGeoJson } from '../types'

const BASIN_STYLE = {
  color: '#0d9488',
  weight: 2,
  opacity: 0.8,
  fillColor: '#14b8a6',
  fillOpacity: 0.08,
}

/**
 * Lidoire topographic catchment (Sandre BD TOPAGE) drawn on the map.
 * Fetched once; the map keeps working without it (request failure = no layer).
 */
export default function BasinLayer() {
  const [basin, setBasin] = useState<BasinGeoJson | null>(null)

  useEffect(() => {
    let cancelled = false
    getBasinGeoJson()
      .then((data) => {
        if (!cancelled) setBasin(data)
      })
      .catch(() => {
        // Optional layer: nothing to show
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (!basin || basin.features.length === 0) return null
  const props = basin.features[0].properties

  return (
    <GeoJSON
      data={basin}
      style={BASIN_STYLE}
      attribution={`Bassin versant : ${props.attribution}`}
      interactive={true}
    >
      <Tooltip sticky>
        <span className="font-medium">Bassin versant de la Lidoire</span>
        <br />
        {props.area_km2.toLocaleString('fr-FR')} km² (BD TOPAGE)
      </Tooltip>
    </GeoJSON>
  )
}
