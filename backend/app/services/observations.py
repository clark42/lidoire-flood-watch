"""Field observations at the mill: storage, photo files and episode linking.

Rows live in the PostgreSQL table ``observations``; photos are files on the
backend data volume (``settings.observations_photo_dir``, PVC /app/data),
named ``<uuid hex>.<ext>`` by the server only. The client file name is never
used, and a stored name is only served when it matches PHOTO_NAME_RE and
resolves inside the photo directory.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import delete, select

from app.config import get_settings
from app.models.orm import ObservationRow
from app.models.schemas import (
    EpisodeOutcome,
    FloodEpisode,
    Observation,
    ObservationCreate,
    ObservationKind,
    OutcomeSuggestion,
)

logger = logging.getLogger(__name__)

PHOTO_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp|heic)$")
LIST_LIMIT = 500


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def row_to_observation(row: ObservationRow) -> Observation:
    has_photo = bool(row.photo_path)
    return Observation(
        id=row.id,
        observed_at=row.observed_at,
        kind=ObservationKind(row.kind),
        water_height_cm=row.water_height_cm,
        reference=row.reference,
        note=row.note,
        has_photo=has_photo,
        photo_url=f"/api/observations/{row.id}/photo" if has_photo else None,
        created_at=row.created_at,
        source=row.source,
    )


# ---------------------------------------------------------------------------
# Photo files
# ---------------------------------------------------------------------------


def photo_dir() -> Path:
    return Path(get_settings().observations_photo_dir)


def save_photo(data: bytes, extension: str) -> str:
    """Write the photo under a fresh random name (0600); returns the name."""
    directory = photo_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{extension}"
    if not PHOTO_NAME_RE.match(name):
        raise ValueError(f"unsupported photo extension {extension!r}")
    final = directory / name
    tmp = directory / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return name


def resolve_photo(name: Optional[str]) -> Optional[Path]:
    """Path of a stored photo, or None (bad name, outside the dir, missing)."""
    if not name or not PHOTO_NAME_RE.match(name):
        return None
    directory = photo_dir().resolve()
    path = (directory / name).resolve()
    if path.parent != directory or not path.is_file():
        return None
    return path


def delete_photo(name: Optional[str]) -> None:
    path = resolve_photo(name)
    if path is not None:
        try:
            path.unlink()
        except OSError as e:
            logger.warning("Cannot delete observation photo %s: %r", name, e)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


async def create_observation(data: ObservationCreate, photo_name: Optional[str] = None) -> Observation:
    from app.db import get_session_factory

    row = ObservationRow(
        observed_at=to_naive_utc(data.observed_at) if data.observed_at else _utcnow(),
        kind=data.kind.value,
        water_height_cm=data.water_height_cm,
        reference=data.reference,
        note=data.note,
        photo_path=photo_name,
        source=data.source,
        created_at=_utcnow(),
    )
    async with get_session_factory()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row_to_observation(row)


async def list_observations(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = LIST_LIMIT,
) -> list[Observation]:
    """Observations with start <= observed_at <= end, newest first."""
    from app.db import get_session_factory

    query = select(ObservationRow)
    if start is not None:
        query = query.where(ObservationRow.observed_at >= to_naive_utc(start))
    if end is not None:
        query = query.where(ObservationRow.observed_at <= to_naive_utc(end))
    query = query.order_by(ObservationRow.observed_at.desc(), ObservationRow.id.desc()).limit(limit)
    async with get_session_factory()() as session:
        rows = (await session.execute(query)).scalars().all()
    return [row_to_observation(r) for r in rows]


async def get_observation_photo_name(observation_id: int) -> tuple[bool, Optional[str]]:
    """``(exists, photo_name)`` of an observation."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        row = await session.get(ObservationRow, observation_id)
    if row is None:
        return False, None
    return True, row.photo_path


async def delete_observation(observation_id: int) -> bool:
    """Delete the row and its photo file; False when it does not exist."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        row = await session.get(ObservationRow, observation_id)
        if row is None:
            return False
        photo_name = row.photo_path
        await session.execute(delete(ObservationRow).where(ObservationRow.id == observation_id))
        await session.commit()
    delete_photo(photo_name)
    return True


# ---------------------------------------------------------------------------
# Episode linking (pure)
# ---------------------------------------------------------------------------


def episode_window(episode: FloodEpisode, margin_hours: Optional[float] = None) -> tuple[datetime, datetime]:
    """[start - margin, (end or peak or start) + margin], naive UTC."""
    if margin_hours is None:
        margin_hours = get_settings().observation_episode_margin_hours
    margin = timedelta(hours=margin_hours)
    start = to_naive_utc(episode.start_time)
    last = episode.end_time or episode.peak_time or episode.start_time
    return start - margin, to_naive_utc(last) + margin


def observations_in_window(
    observations: Iterable[Observation], window: tuple[datetime, datetime],
) -> list[Observation]:
    start, end = window
    return sorted(
        (o for o in observations if start <= to_naive_utc(o.observed_at) <= end),
        key=lambda o: o.observed_at,
    )


def suggest_outcome(episode: FloodEpisode, observations: list[Observation]) -> Optional[OutcomeSuggestion]:
    """Outcome offered for an episode without one: flooded beats near_miss.

    Never applied automatically: the frontend proposes it and the user
    confirms through POST /api/episodes/confirm/{id}.
    """
    if episode.outcome is not None:
        return None
    for kind, outcome in (
        (ObservationKind.FLOODED, EpisodeOutcome.FLOODED),
        (ObservationKind.NEAR_MISS, EpisodeOutcome.NEAR_MISS),
    ):
        matching = [o for o in observations if o.kind == kind]
        if not matching:
            continue
        heights = [o.water_height_cm for o in matching if o.water_height_cm is not None and o.water_height_cm >= 0]
        # Highest water seen, else the first report
        best = max(matching, key=lambda o: (o.water_height_cm is not None, o.water_height_cm or 0.0))
        return OutcomeSuggestion(
            outcome=outcome,
            observed_at=best.observed_at,
            water_height_cm=max(heights) if heights and outcome == EpisodeOutcome.FLOODED else None,
            observation_id=best.id,
            reason=f"{len(matching)} observation(s) '{kind.value}' dans la fenetre de l'episode",
        )
    return None
