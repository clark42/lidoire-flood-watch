"""API routes."""

from fastapi import APIRouter

from app.api.status import router as status_router
from app.api.realtime import router as realtime_router
from app.api.timeline import router as timeline_router
from app.api.episodes import router as episodes_router
from app.api.risk import router as risk_router
from app.api.auth import router as auth_router
from app.api.photos import router as photos_router

router = APIRouter()

router.include_router(status_router, tags=["Status"])
router.include_router(realtime_router, prefix="/realtime", tags=["Realtime"])
router.include_router(timeline_router, prefix="/timeline", tags=["Timeline"])
router.include_router(episodes_router, prefix="/episodes", tags=["Episodes"])
router.include_router(risk_router, prefix="/risk", tags=["Risk"])
router.include_router(auth_router, prefix="/auth", tags=["Authentication"])
router.include_router(photos_router, prefix="/photos", tags=["Photos"])
