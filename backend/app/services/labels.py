"""Ground-truth helpers for flood episodes.

Two responsibilities:

1. Load the reference labels of the annotated episodes from
   ``app/data/episodes_labels.json`` (single source of truth used by the
   evaluation scripts and the ``GET /api/episodes/labels`` endpoint).
2. Parse the legacy ``[CONFIRMED: ...]`` text prefixes that older versions of
   the confirm endpoint wrote into ``notes`` into a structured outcome.
"""

import json
import logging
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional, Union

from app.models.schemas import EpisodeLabel, EpisodeLabelSet, EpisodeOutcome

logger = logging.getLogger(__name__)

LABELS_PATH = Path(__file__).resolve().parent.parent / "data" / "episodes_labels.json"

# Source recorded on episodes whose outcome was recovered from a notes prefix
LEGACY_PREFIX_SOURCE = "legacy_notes_prefix"

_LEGACY_PREFIX_RE = re.compile(
    r"\[CONFIRMED:\s*(FLOODED|FALSE[ _]POSITIVE)\s*\][ \t]*", re.IGNORECASE
)


# ============================================================================
# Reference labels
# ============================================================================


def load_label_set(path: Optional[Union[str, Path]] = None) -> EpisodeLabelSet:
    """Load and validate the labels file.

    Raises:
        FileNotFoundError: if the file does not exist
        pydantic.ValidationError / ValueError: if the content is invalid
    """
    if path is None:
        return _load_default_label_set()
    return _parse_label_file(Path(path))


@lru_cache(maxsize=1)
def _load_default_label_set() -> EpisodeLabelSet:
    return _parse_label_file(LABELS_PATH)


def _parse_label_file(path: Path) -> EpisodeLabelSet:
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    label_set = EpisodeLabelSet.model_validate(raw)

    keys = [ep.key for ep in label_set.episodes]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"Duplicate episode label keys in {path}: {duplicates}")
    for ep in label_set.episodes:
        if ep.key != ep.peak_time.date().isoformat():
            raise ValueError(
                f"Label key {ep.key} does not match peak date {ep.peak_time.date()}"
            )

    label_set.episodes.sort(key=lambda e: e.peak_time)
    return label_set


def load_episode_labels(path: Optional[Union[str, Path]] = None) -> list[EpisodeLabel]:
    """Return the annotated episodes, sorted by peak time."""
    return list(load_label_set(path).episodes)


def get_label_by_key(
    key: Union[str, date, datetime], path: Optional[Union[str, Path]] = None
) -> Optional[EpisodeLabel]:
    """Find a label by its date key (``YYYY-MM-DD``, a date or a datetime)."""
    if isinstance(key, datetime):
        key = key.date().isoformat()
    elif isinstance(key, date):
        key = key.isoformat()
    for ep in load_episode_labels(path):
        if ep.key == key:
            return ep
    return None


def labels_by_outcome(
    outcome: EpisodeOutcome, path: Optional[Union[str, Path]] = None
) -> list[EpisodeLabel]:
    """Return the labels with a given outcome."""
    return [ep for ep in load_episode_labels(path) if ep.outcome == outcome]


# ============================================================================
# Legacy notes prefixes
# ============================================================================


def has_legacy_prefix(notes: Optional[str]) -> bool:
    return bool(notes) and _LEGACY_PREFIX_RE.search(notes) is not None


def strip_legacy_prefixes(notes: Optional[str]) -> Optional[str]:
    """Remove every ``[CONFIRMED: ...]`` prefix from notes.

    The legacy confirm endpoint prepended the submitted notes to the existing
    ones, and the frontend submitted the full existing notes, so each confirm
    duplicated the text. When prefixes are found, repeated lines are therefore
    collapsed (first occurrence kept). Returns None when nothing remains.
    """
    if not notes:
        return notes
    if not has_legacy_prefix(notes):
        return notes

    cleaned = _LEGACY_PREFIX_RE.sub("", notes)
    seen: set[str] = set()
    lines: list[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if stripped:
            if stripped in seen:
                continue
            seen.add(stripped)
        lines.append(line.rstrip())
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return result or None


def parse_legacy_outcome(
    notes: Optional[str],
) -> tuple[Optional[EpisodeOutcome], Optional[str]]:
    """Parse a legacy notes prefix into an outcome.

    The legacy confirm endpoint *prepended* its prefix, so when several are
    stacked the leftmost one is the most recent decision and wins.

    Returns:
        (outcome or None if no prefix, notes without prefixes)
    """
    if not notes:
        return None, notes
    match = _LEGACY_PREFIX_RE.search(notes)
    if match is None:
        return None, notes
    token = match.group(1).upper()
    outcome = EpisodeOutcome.FLOODED if token == "FLOODED" else EpisodeOutcome.FALSE_POSITIVE
    return outcome, strip_legacy_prefixes(notes)
