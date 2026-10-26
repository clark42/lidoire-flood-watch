"""API routes."""

from fastapi import APIRouter, Depends

from app.api.deps import require_api_key_for_writes

from app.api.status import router as status_router
from app.api.realtime import router as realtime_router
from app.api.timeline import router as timeline_router
from app.api.episodes import router as episodes_router
from app.api.risk import router as risk_router
from app.api.auth import router as auth_router
from app.api.notify import router as notify_router
from app.api.vigilance import router as vigilance_router
from app.api.observations import router as observations_router
from app.api.observations import episode_router as observations_episode_router
from app.api.indicators import router as indicators_router

# Every route below inherits the API key check: safe methods (GET/HEAD/OPTIONS)
# stay public, state-changing methods require the X-API-Key header.
router = APIRouter(dependencies=[Depends(require_api_key_for_writes)])

router.include_router(status_router, tags=["Status"])
router.include_router(realtime_router, prefix="/realtime", tags=["Realtime"])
router.include_router(timeline_router, prefix="/timeline", tags=["Timeline"])
router.include_router(episodes_router, prefix="/episodes", tags=["Episodes"])
router.include_router(risk_router, prefix="/risk", tags=["Risk"])
router.include_router(auth_router, prefix="/auth", tags=["Authentication"])
router.include_router(notify_router, prefix="/notify", tags=["Notifications"])
router.include_router(vigilance_router, prefix="/vigilance", tags=["Vigilance"])
router.include_router(observations_router, prefix="/observations", tags=["Observations"])
router.include_router(observations_episode_router, prefix="/episodes", tags=["Observations"])
router.include_router(indicators_router, prefix="/indicators", tags=["Indicators"])
