"""Shared API dependencies (authentication).

Write protection model
----------------------
All state-changing requests (POST/PUT/PATCH/DELETE) on ``/api`` routes must
carry an ``X-API-Key`` header matching ``settings.api_key``. Safe methods
(GET/HEAD/OPTIONS) stay public so the dashboard and the Netatmo OAuth
redirect/callback (plain browser GETs) keep working without a key.

If ``API_KEY`` is not configured the check is disabled (dev mode) and a
warning is logged at startup by :func:`warn_if_api_key_missing`. A key (or
any backend secret) left at a template placeholder such as ``change-me``
makes the backend refuse to start (:func:`check_secrets_not_placeholders`).

Frontend contract: the key is sent in the ``X-API-Key`` header, its value is
stored in ``localStorage`` under the ``lfw_api_key`` key. The only exception
is ``GET /api/auth/netatmo`` (a plain browser navigation that cannot carry a
header): the frontend appends ``?api_key=<lfw_api_key>`` to that link, see
``app.api.auth.netatmo_auth_start``.
"""

import hmac
import logging
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

API_KEY_HEADER_NAME = "X-API-Key"

# Methods that never change server state and therefore need no API key.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Startup sanity checks on the configured key (weak key: warning only).
PLACEHOLDER_API_KEY = "change-me"
MIN_API_KEY_LENGTH = 16

# Template / documentation values that must never be used as a real secret
# (compared case-insensitively). The backend refuses to start with them.
KNOWN_PLACEHOLDER_SECRETS = frozenset({
    PLACEHOLDER_API_KEY,
    "changeme",
    "change_me",
    "change-me-please",
    "replace-me",
    "replaceme",
    "your-api-key",
    "your_api_key",
    "<api_key>",
    "<your-api-key>",
    "api-key",
    "apikey",
    "xxx",
    "todo",
})


class InsecureConfigurationError(RuntimeError):
    """A secret is still set to a template placeholder."""


def is_placeholder_secret(value: str | None) -> bool:
    """True when ``value`` is a known template placeholder (not an empty value)."""
    return bool(value) and value.strip().lower() in KNOWN_PLACEHOLDER_SECRETS


def _url_password(url: str) -> str | None:
    try:
        password = urlsplit(url).password
    except ValueError:
        return None
    return unquote(password) if password else None


def check_secrets_not_placeholders(settings: Settings | None = None) -> None:
    """Refuse to start when a secret is still a template placeholder.

    Checks ``API_KEY``, the InfluxDB tokens, ``NTFY_TOKEN`` and the passwords
    embedded in ``DATABASE_URL`` / ``REDIS_URL``. Empty values are allowed
    (dev mode for API_KEY, warned separately). The error names the variable,
    never its value.

    Raises:
        InsecureConfigurationError: with a message explaining how to
            generate a real secret (``openssl rand -hex 32``).
    """
    settings = settings or get_settings()
    candidates = {
        "API_KEY": settings.api_key,
        "INFLUXDB_TOKEN": settings.influxdb_token,
        "INFLUXDB_READ_TOKEN": settings.influxdb_read_token,
        "DATABASE_URL (password)": _url_password(settings.database_url),
        "REDIS_URL (password)": _url_password(settings.redis_url),
        "NTFY_TOKEN": settings.ntfy_token,
    }
    bad = [name for name, value in candidates.items() if is_placeholder_secret(value)]
    if bad:
        raise InsecureConfigurationError(
            "Refusing to start: placeholder value (e.g. 'change-me') for "
            + ", ".join(bad)
            + ". Set real secrets in .env, generated with `openssl rand -hex 32` "
            "(or leave API_KEY empty for local development only)."
        )

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
        if settings.api_key == PLACEHOLDER_API_KEY or len(settings.api_key) < MIN_API_KEY_LENGTH:
            logger.warning(
                "API_KEY is weak (placeholder or shorter than %d chars): "
                "generate one with `openssl rand -hex 32`.",
                MIN_API_KEY_LENGTH,
            )
        return True
    logger.warning(
        "API_KEY is not set: write endpoints (POST/PUT/PATCH/DELETE /api/*) are "
        "UNPROTECTED. This is only acceptable for local development."
    )
    return False
