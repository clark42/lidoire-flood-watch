"""Official vigilance endpoint (Vigicrues troncons, Meteo-France departements).

Read-only view of the cache filled by the scheduler job
(``collectors/vigilance.py``): this route never calls Vigicrues or
Meteo-France. Informative only, not a risk model input.
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import get_settings
from app.models.schemas import (
    MeteoFranceVigilance,
    VigicruesVigilance,
    VigilanceStatus,
)
from app.services.collectors import vigilance as vigilance_collector

router = APIRouter()


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("", response_model=VigilanceStatus)
async def get_vigilance():
    """Current official colours (1 vert, 2 jaune, 3 orange, 4 rouge) and links.

    A source is ``available=false`` until the background job has fetched it
    (first run at startup, then every 30 min) or when its last result is
    older than 2 h.
    """
    settings = get_settings()
    cached = vigilance_collector.peek_vigilance()
    # Not fetched yet: the configured zones with unknown colours
    vigicrues = cached.get(vigilance_collector.SOURCE_VIGICRUES) or vigilance_collector.parse_vigicrues(
        {}, settings.vigilance_vigicrues_troncons, None,
    )
    meteofrance = cached.get(vigilance_collector.SOURCE_METEOFRANCE) or vigilance_collector.parse_meteofrance(
        {}, settings.vigilance_meteofrance_departments, settings.vigilance_meteofrance_phenomena, None,
    )
    return VigilanceStatus(
        timestamp=_utcnow(),
        vigicrues=VigicruesVigilance(
            available=bool(vigicrues.get("available")),
            issued_at=vigicrues.get("issued_at"),
            fetched_at=vigicrues.get("fetched_at"),
            troncons=vigicrues.get("troncons", []),
            url=vigilance_collector.VIGICRUES_HOME,
        ),
        meteofrance=MeteoFranceVigilance(
            available=bool(meteofrance.get("available")),
            issued_at=meteofrance.get("issued_at"),
            fetched_at=meteofrance.get("fetched_at"),
            valid_from=meteofrance.get("valid_from"),
            valid_to=meteofrance.get("valid_to"),
            departments=meteofrance.get("departments", []),
            url=vigilance_collector.MF_HOME,
        ),
    )
