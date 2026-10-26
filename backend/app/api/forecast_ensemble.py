"""Probabilistic forecasts: ensemble basin rain and GloFAS discharge.

``GET /api/forecast/ensemble`` (public). Independent of the risk model: the
v3.4 score never reads these values. Cache-through (Open-Meteo, 3 h in
process); the route never stores anything (the scheduler jobs do).
"""

import asyncio
import logging

from fastapi import APIRouter

from app.models.probabilistic import api_json
from app.services.collectors.ensemble_rain import get_ensemble_rain
from app.services.collectors.glofas import get_glofas_forecast

logger = logging.getLogger(__name__)

router = APIRouter()


async def _safe(name: str, coro) -> dict:
    try:
        return await coro
    except Exception as e:  # one source down must not hide the other
        logger.warning("Ensemble forecast: %s failed: %r", name, e)
        return {"available": False, "note": f"{name} indisponible"}


@router.get("/ensemble")
async def get_ensemble_forecast():
    """Rain exceedance probabilities per horizon and GloFAS quantiles.

    - ``rain.horizons["24h"|"72h"]``: windows from the current hour (day k,
      and 72 h windows starting every 24 h), per model (quantiles, members,
      ``prob`` = share of members above each v3.4 threshold, keys in mm) and
      ``blend`` (mean of the models covering the window).
    - ``glofas.days``: daily discharge quantiles (m3/s) of the latest run at
      the Lidoire cell, with the probability of exceeding the cell's
      reanalysis p95 / p99 (``glofas.climatology``).
    """
    rain, glofas = await asyncio.gather(
        _safe("pluie d'ensemble", get_ensemble_rain()),
        _safe("GloFAS", get_glofas_forecast()),
    )
    return api_json({
        "rain": rain,
        "glofas": glofas,
        "note": "Indicatif : ces previsions n'entrent pas dans le score de risque v3.4",
    })
