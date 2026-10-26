"""Notification endpoints (POST protected by the X-API-Key router dependency)."""

from fastapi import APIRouter, HTTPException, status

from app.config import get_settings
from app.services.notifier import send_test_notification

router = APIRouter()


@router.post("/test")
async def notify_test():
    """Send a test message on the configured channel (ntfy)."""
    settings = get_settings()
    if not settings.notifications_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Notifications disabled: set NTFY_URL and NTFY_TOPIC",
        )
    if not await send_test_notification(settings):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Notification not delivered (see backend logs)",
        )
    return {"sent": True, "channel": "ntfy"}
