"""Geographic reference data (public)."""

from fastapi import APIRouter

from app.services.basin import basin_geojson

router = APIRouter()


@router.get("/basin")
async def get_basin():
    """Lidoire topographic catchment (GeoJSON FeatureCollection, WGS84).

    Sandre BD TOPAGE, union of the Lidoire and Lechout elementary catchments
    (230.8 km2), simplified to 25 m. Source and licence in ``properties``.
    """
    return basin_geojson()
