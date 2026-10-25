"""Shared API dependencies (authentication).

Write protection model
----------------------
All state-changing requests (POST/PUT/PATCH/DELETE) on ``/api`` routes must
carry an ``X-API-Key`` header matching ``settings.api_key``. Safe methods
(GET/HEAD/OPTIONS) stay public so the dashboard and the Netatmo OAuth
redirect/callback (plain browser GETs) keep working without a key.

If ``API_KEY`` is not configured the check is disabled (dev mode) and a
warning is logged at startup by :func:`warn_if_api_key_missing`.

Frontend contract: the key is sent in the ``X-API-Key`` header, its value is
stored in ``localStorage`` under the ``lfw_api_key`` key.
"""

import hmac
import logging

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

API_KEY_HEADER_NAME = "X-API-Key"

# Methods that never change server state and therefore need no API key.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# auto_error=False: missing header is handled below (safe methods / dev mode).
# Declared as a security scheme so Swagger UI shows an "Authorize" button.
api_key_header = APIKeyHeader(
    name=API_KEY_HEADER_NAME,
    auto_error=False,
    description="Required for POST/PUT/PATCH/DELETE requests on /api routes.",
)


def api_key_matches(provided: str | None, expected: str) -> bool:
    """Compare an API key in constant time."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def require_api_key_for_writes(
    request: Request,
    api_key: str | None = Security(api_key_header),
) -> None:
    """Router-level dependency: enforce the API key on non-safe methods."""
    if request.method.upper() in SAFE_METHODS:
        return

    expected = get_settings().api_key
    if not expected:
        # Dev mode: no key configured, writes are open (warned at startup).
        return

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing {API_KEY_HEADER_NAME} header",
            headers={"WWW-Authenticate": API_KEY_HEADER_NAME},
        )

    if not api_key_matches(api_key, expected):
        logger.warning(
            "Rejected %s %s: invalid API key (client %s)",
            request.method,
            request.url.path,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": API_KEY_HEADER_NAME},
        )


def warn_if_api_key_missing(settings: Settings | None = None) -> bool:
    """Log a warning when write protection is disabled.

    Returns True when an API key is configured.
    """
    settings = settings or get_settings()
    if settings.api_key:
        return True
    logger.warning(
        "API_KEY is not set: write endpoints (POST/PUT/PATCH/DELETE /api/*) are "
        "UNPROTECTED. This is only acceptable for local development."
    )
    return False
