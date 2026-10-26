"""Hydrological indicators (display / evaluation only, never a v3.4 input)."""

from fastapi import APIRouter

from app.services.indicators import get_indicators

router = APIRouter()


@router.get("")
async def read_indicators():
    """Current hydrological indicators, grouped by mechanism.

    - ``runoff``: discharge of the analog tributaries (Eyraud, Caudeau): Q
      now, 24 h mean, 3 h trend, percentile of the 24 h mean among the daily
      means since 2022-11, distance to the daily thresholds;
    - ``dordogne``: Dordogne discharge (Pessac, Lamonzie, Argentat);
    - ``groundwater``: alluvial piezometer level (m NGF), 24 h change, percentile;
    - ``tide_observed``: observed estuary level (tide + surge), 24 h max, percentile.

    Reads stored series only (scheduler jobs and the explicit backfill fill
    them): no external API is called. ``model_input`` is always false.
    """
    return await get_indicators()
