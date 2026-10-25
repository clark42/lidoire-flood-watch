"""Ground truth for the evaluation, from app.services.labels.

Binary mapping (task decision, see episodes_labels.json caveats):

- ``flooded`` -> positive
- ``near_miss`` and ``false_positive`` -> negative
- ``unknown`` -> excluded (neither hit, miss nor false alarm; alerts near it
  are excluded from the false-alarm counts)

The label set is NOT exhaustive: every negative is a former model detection,
and floods nobody annotated may exist. Alerts that match no label are
therefore reported separately ("unlabelled") so the owner can label them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional, Sequence, Union

from app.models.schemas import EpisodeLabel, EpisodeOutcome
from app.services.labels import load_label_set


def binary_outcome(outcome: EpisodeOutcome) -> Optional[bool]:
    """flooded -> True, near_miss/false_positive -> False, unknown -> None."""
    if outcome == EpisodeOutcome.FLOODED:
        return True
    if outcome in (EpisodeOutcome.NEAR_MISS, EpisodeOutcome.FALSE_POSITIVE):
        return False
    return None


@dataclass(frozen=True)
class EvalEvent:
    """One annotated episode as seen by the evaluation."""

    key: str
    peak_time: datetime  # naive UTC (day-level precision in the labels)
    outcome: EpisodeOutcome
    positive: Optional[bool]
    severity: int
    flags: tuple[str, ...] = ()
    notes: Optional[str] = None
    legacy_stored_score: Optional[float] = None

    @property
    def evaluable(self) -> bool:
        return self.positive is not None


@dataclass(frozen=True)
class LabelPeriod:
    """Period the annotations cover (alerts outside it are not judged)."""

    start: datetime
    end: datetime

    def contains(self, t: datetime) -> bool:
        return self.start <= t <= self.end


def event_from_label(label: EpisodeLabel) -> EvalEvent:
    peak = label.peak_time
    if peak.tzinfo is not None:
        from app.services.risk_core import to_utc_naive

        peak = to_utc_naive(peak)
    return EvalEvent(
        key=label.key,
        peak_time=peak,
        outcome=label.outcome,
        positive=binary_outcome(label.outcome),
        severity=label.severity,
        flags=tuple(label.flags),
        notes=label.notes,
        legacy_stored_score=label.legacy_stored_score,
    )


def _parse_day(value: Optional[str], end_of_day: bool) -> Optional[datetime]:
    if not value:
        return None
    d = date.fromisoformat(str(value)[:10])
    return datetime.combine(d, time(23) if end_of_day else time(0))


def load_events(path: Optional[Union[str, Path]] = None) -> tuple[list[EvalEvent], Optional[LabelPeriod]]:
    """Events (sorted by peak) and the annotated period from the labels file."""
    label_set = load_label_set(path)
    events = [event_from_label(lb) for lb in label_set.episodes]
    start = _parse_day(label_set.period_start, end_of_day=False)
    end = _parse_day(label_set.period_end, end_of_day=True)
    period = LabelPeriod(start, end) if start and end else None
    return sorted(events, key=lambda e: e.peak_time), period


def split_events(events: Sequence[EvalEvent]) -> dict[str, list[EvalEvent]]:
    return {
        "positive": [e for e in events if e.positive is True],
        "negative": [e for e in events if e.positive is False],
        "excluded": [e for e in events if e.positive is None],
    }


def event_window(event: EvalEvent, lead_h: float, lag_h: float) -> tuple[datetime, datetime]:
    """[peak - lead_h, peak + lag_h]."""
    return event.peak_time - timedelta(hours=lead_h), event.peak_time + timedelta(hours=lag_h)
