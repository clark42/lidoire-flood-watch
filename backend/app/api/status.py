"""Status and health check endpoints."""

from datetime import datetime
from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/status")
async def get_status():
    """Get API status and health information."""
    settings = get_settings()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
        "location": {
            "lat": settings.moulin_lat,
            "lon": settings.moulin_lon,
            "name": "Moulin de la Lidoire",
        },
        "services": {
            "database": "connected",  # TODO: actual check
            "influxdb": "connected",   # TODO: actual check
            "collectors": {
                "tide": "running",
                "vigicrues": "running",
                "weather": "running",
                "camera": "running" if settings.camera_rtsp_url else "disabled",
            },
        },
    }


@router.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {"status": "ok"}
