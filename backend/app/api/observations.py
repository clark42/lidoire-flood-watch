"""Field observations at the mill (ground truth reported by the owner).

- ``POST /api/observations``: JSON body (:class:`ObservationCreate`), or
  multipart/form-data (or urlencoded) with the same JSON in a ``data`` field
  (or one form field per attribute) plus an optional ``photo`` file. X-API-Key required
  (router-level write check).
- ``GET /api/observations?start&end``: newest first (public, like episodes).
- ``DELETE /api/observations/{id}``: row and photo file (X-API-Key).
- ``GET /api/observations/{id}/photo``: the photo, X-API-Key required even
  for this read (photos are private).
- ``GET /api/episodes/{id}/observations``: observations inside the episode
  window and, for an episode without outcome, a suggested outcome that the
  user may apply through POST /api/episodes/confirm/{id} (never automatic).

Photo checks: magic bytes (JPEG, PNG, WebP, HEIC/HEIF), 8 MB max, GPS
metadata removed except for HEIC (see services/photo_safety.py), stored as
``<uuid>.<ext>`` under settings.observations_photo_dir.
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api.deps import require_api_key
from app.config import get_settings
from app.models.schemas import EpisodeObservations, Observation, ObservationCreate
from app.services import observations as store
from app.services import photo_safety
from app.services.data_store import get_episode_by_id

logger = logging.getLogger(__name__)

router = APIRouter()
episode_router = APIRouter()

# Multipart framing and text fields on top of the photo itself
FORM_OVERHEAD_BYTES = 256 * 1024
FORM_FIELDS = ("observed_at", "kind", "water_height_cm", "reference", "note", "source")

# Inline schema: the route reads the raw request, so FastAPI does not
# register ObservationCreate itself (ObservationKind comes with Observation).
_JSON_BODY_SCHEMA = ObservationCreate.model_json_schema(ref_template="#/components/schemas/{model}")
_JSON_BODY_SCHEMA.pop("$defs", None)

_REQUEST_BODY_DOC = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": _JSON_BODY_SCHEMA},
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "string", "description": "ObservationCreate as JSON"},
                        "photo": {
                            "type": "string",
                            "format": "binary",
                            "description": "JPEG, PNG, WebP or HEIC, 8 MB max",
                        },
                    },
                },
            },
        },
    }
}


def _validation_error(e: ValidationError) -> RequestValidationError:
    return RequestValidationError(e.errors(include_url=False, include_context=False))


def _parse_payload(raw: Any) -> ObservationCreate:
    try:
        return ObservationCreate.model_validate(raw)
    except ValidationError as e:
        raise _validation_error(e)


async def _read_photo(upload: UploadFile, max_bytes: int) -> tuple[bytes, str]:
    """Bounded read + magic bytes; returns (scrubbed bytes, extension)."""
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Photo too large (max {max_bytes // (1024 * 1024)} MB)")
    if not data:
        raise HTTPException(status_code=400, detail="Empty photo")
    fmt = photo_safety.detect_format(data[:64])
    if fmt is None:
        raise HTTPException(status_code=415, detail="Unsupported photo: JPEG, PNG, WebP or HEIC only")
    try:
        photo_safety.check_declared_type(fmt, upload.content_type)
        result = photo_safety.scrub(fmt, data)
    except photo_safety.PhotoRejected as e:
        raise HTTPException(status_code=415, detail=f"Unsupported photo: {e}")
    if not result.gps_removed:
        logger.info("Observation photo stored with its metadata (%s: GPS not stripped)", fmt)
    return result.data, photo_safety.FORMATS[fmt][0]


@router.post("", response_model=Observation, status_code=201, openapi_extra=_REQUEST_BODY_DOC)
async def create_observation(request: Request):
    """Record an observation (JSON, or multipart with an optional photo)."""
    settings = get_settings()
    max_bytes = settings.observation_photo_max_bytes
    try:
        declared_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared_length = 0
    if declared_length > max_bytes + FORM_OVERHEAD_BYTES:
        raise HTTPException(status_code=413, detail="Request too large")

    content_type = request.headers.get("content-type", "").lower()
    photo: Optional[tuple[bytes, str]] = None
    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        async with request.form(max_files=1, max_fields=len(FORM_FIELDS) + 2) as form:
            raw_data = form.get("data")
            if isinstance(raw_data, str) and raw_data.strip():
                try:
                    raw: Any = json.loads(raw_data)
                except json.JSONDecodeError:
                    raise HTTPException(status_code=422, detail="Field 'data' is not valid JSON")
            else:
                raw = {
                    k: v for k in FORM_FIELDS
                    if isinstance(v := form.get(k), str) and v.strip() != ""
                }
            payload = _parse_payload(raw)
            upload = form.get("photo")
            if isinstance(upload, UploadFile) and (upload.filename or upload.size):
                photo = await _read_photo(upload, max_bytes)
    else:
        try:
            raw = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="Body is not valid JSON")
        payload = _parse_payload(raw)

    photo_name = store.save_photo(*photo) if photo else None
    try:
        return await store.create_observation(payload, photo_name)
    except Exception:
        store.delete_photo(photo_name)
        raise


@router.get("", response_model=list[Observation])
async def list_observations(
    start: Optional[datetime] = Query(None, description="Inclusive lower bound (UTC)"),
    end: Optional[datetime] = Query(None, description="Inclusive upper bound (UTC)"),
    limit: int = Query(store.LIST_LIMIT, ge=1, le=store.LIST_LIMIT),
):
    """Observations, newest first."""
    if start is not None and end is not None and store.to_naive_utc(start) >= store.to_naive_utc(end):
        raise HTTPException(status_code=422, detail="start must be before end")
    return await store.list_observations(start, end, limit)


@router.get("/{observation_id}/photo", dependencies=[Depends(require_api_key)])
async def get_observation_photo(observation_id: int):
    """The observation photo (private: X-API-Key required when API_KEY is set)."""
    exists, name = await store.get_observation_photo_name(observation_id)
    path = store.resolve_photo(name) if exists else None
    if path is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    extension = path.suffix.lstrip(".")
    return FileResponse(
        path,
        media_type=photo_safety.EXTENSION_CONTENT_TYPES[extension],
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="observation-{observation_id}.{extension}"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.delete("/{observation_id}")
async def delete_observation(observation_id: int):
    """Delete an observation and its photo."""
    if not await store.delete_observation(observation_id):
        raise HTTPException(status_code=404, detail="Observation not found")
    return {"status": "deleted", "id": observation_id}


@episode_router.get("/{episode_id}/observations", response_model=EpisodeObservations)
async def get_episode_observations(episode_id: int):
    """Observations inside the episode window (+/- margin) and a suggested outcome.

    The suggestion (flooded, else near_miss) is only given for an episode
    without outcome and is never applied here.
    """
    episode = await get_episode_by_id(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    window = store.episode_window(episode)
    observations = store.observations_in_window(await store.list_observations(*window), window)
    return EpisodeObservations(
        episode_id=episode_id,
        window_start=window[0],
        window_end=window[1],
        observations=observations,
        suggested_outcome=store.suggest_outcome(episode, observations),
    )
