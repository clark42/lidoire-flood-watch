"""OAuth2 authentication endpoints for external services."""

import json
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.config import get_settings


router = APIRouter()

# Netatmo OAuth2 configuration
NETATMO_AUTH_URL = "https://api.netatmo.com/oauth2/authorize"
NETATMO_TOKEN_URL = "https://api.netatmo.com/oauth2/token"
NETATMO_SCOPES = "read_station"

# Token storage path (inside container volume for persistence)
TOKEN_STORAGE_PATH = Path("/app/data/netatmo_tokens.json")

# In-memory state for CSRF protection
_oauth_states: dict[str, datetime] = {}


def _load_tokens() -> dict:
    """Load stored tokens from file."""
    if TOKEN_STORAGE_PATH.exists():
        try:
            return json.loads(TOKEN_STORAGE_PATH.read_text())
        except Exception as e:
            print(f"Error loading Netatmo tokens: {e}")
    return {}


def _save_tokens(tokens: dict) -> None:
    """Save tokens to file."""
    try:
        TOKEN_STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_STORAGE_PATH.write_text(json.dumps(tokens, indent=2))
        print(f"Netatmo tokens saved to {TOKEN_STORAGE_PATH}")
    except Exception as e:
        print(f"Error saving Netatmo tokens: {e}")


def get_netatmo_tokens() -> dict:
    """Get current Netatmo tokens (for use by collector)."""
    return _load_tokens()


def update_netatmo_tokens(access_token: str, refresh_token: str, expires_in: int) -> None:
    """Update stored Netatmo tokens (called after refresh)."""
    tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    _save_tokens(tokens)


def _get_callback_url(settings) -> str:
    """
    Get the OAuth callback URL.

    IMPORTANT: This URL must be registered in Netatmo app settings at dev.netatmo.com
    """
    # Use frontend URL with /api proxy (standard Docker setup)
    return f"{settings.frontend_url.rstrip('/')}/api/auth/netatmo/callback"


@router.get("/netatmo")
async def netatmo_auth_start():
    """
    Start Netatmo OAuth2 authorization flow.

    Redirects the user to Netatmo to authorize access to weather station data.
    After authorization, Netatmo will redirect back to /api/auth/netatmo/callback.

    IMPORTANT: Register the callback URL in your Netatmo app at dev.netatmo.com:
    http://localhost:3000/api/auth/netatmo/callback
    """
    settings = get_settings()

    if not settings.netatmo_client_id:
        raise HTTPException(
            status_code=400,
            detail="NETATMO_CLIENT_ID not configured"
        )

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = datetime.utcnow()

    # Clean old states (older than 10 minutes)
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    for old_state in [s for s, t in _oauth_states.items() if t < cutoff]:
        del _oauth_states[old_state]

    callback_url = _get_callback_url(settings)

    params = {
        "client_id": settings.netatmo_client_id,
        "redirect_uri": callback_url,
        "scope": NETATMO_SCOPES,
        "state": state,
    }

    auth_url = f"{NETATMO_AUTH_URL}?{urlencode(params)}"

    return RedirectResponse(url=auth_url)


@router.get("/netatmo/callback")
async def netatmo_auth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    """
    Handle Netatmo OAuth2 callback.

    Exchanges the authorization code for access and refresh tokens.
    """
    settings = get_settings()

    # Check for errors from Netatmo
    if error:
        raise HTTPException(
            status_code=400,
            detail=f"Netatmo authorization error: {error} - {error_description}"
        )

    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="Missing code or state parameter"
        )

    # Verify state for CSRF protection
    if state not in _oauth_states:
        raise HTTPException(
            status_code=400,
            detail="Invalid state parameter (possible CSRF attack or expired session)"
        )
    del _oauth_states[state]

    # Build callback URL (must match the one used in authorize request)
    callback_url = _get_callback_url(settings)

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                NETATMO_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.netatmo_client_id,
                    "client_secret": settings.netatmo_client_secret,
                    "code": code,
                    "redirect_uri": callback_url,
                    "scope": NETATMO_SCOPES,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30.0,
            )

            if response.status_code != 200:
                print(f"Netatmo token error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to exchange code for tokens: {response.text}"
                )

            data = response.json()

            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 10800)

            if not access_token or not refresh_token:
                raise HTTPException(
                    status_code=400,
                    detail="Missing tokens in Netatmo response"
                )

            # Save tokens
            update_netatmo_tokens(access_token, refresh_token, expires_in)

            # Redirect to frontend with success message
            return RedirectResponse(
                url=f"{settings.frontend_url}/?netatmo_auth=success"
            )

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Network error exchanging code: {str(e)}"
        )


@router.get("/netatmo/status")
async def netatmo_auth_status():
    """
    Check Netatmo authentication status.

    Returns whether we have valid tokens stored.
    """
    settings = get_settings()
    tokens = _load_tokens()
    callback_url = _get_callback_url(settings)

    if not tokens:
        return {
            "authenticated": False,
            "message": "No tokens stored. Visit /api/auth/netatmo to authorize.",
            "callback_url": callback_url,
            "setup_required": True,
        }

    expires_at = tokens.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            is_expired = expiry < datetime.utcnow()
        except:
            is_expired = True
    else:
        is_expired = True

    return {
        "authenticated": True,
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "access_token_expired": is_expired,
        "expires_at": expires_at,
        "updated_at": tokens.get("updated_at"),
        "message": "Tokens stored. Refresh will happen automatically.",
        "callback_url": callback_url,
        "setup_required": False,
    }


@router.get("/netatmo/setup")
async def netatmo_setup_info():
    """
    Get Netatmo OAuth setup instructions.

    Returns the callback URL to register on dev.netatmo.com.
    """
    settings = get_settings()
    callback_url = _get_callback_url(settings)

    return {
        "instructions": [
            "1. Go to https://dev.netatmo.com/apps",
            "2. Select your app (or create one)",
            "3. In 'Redirect URI', add the callback URL below",
            "4. Save changes",
            "5. Then visit /api/auth/netatmo to authorize",
        ],
        "callback_url": callback_url,
        "client_id_configured": bool(settings.netatmo_client_id),
        "client_secret_configured": bool(settings.netatmo_client_secret),
    }
