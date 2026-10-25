"""Ground-truth mapping for the evaluation (app.services.labels -> EvalEvent)."""

import json
from datetime import datetime

import pytest

from app.models.schemas import EpisodeOutcome
from scripts.eval.truth import binary_outcome, event_window, load_events, split_events


@pytest.mark.parametrize("outcome, expected", [
    (EpisodeOutcome.FLOODED, True),
    (EpisodeOutcome.NEAR_MISS, False),
    (EpisodeOutcome.FALSE_POSITIVE, False),
    (EpisodeOutcome.UNKNOWN, None),
])
def test_evaluation_binary_outcome_mapping(outcome, expected):
    assert binary_outcome(outcome) is expected


def test_evaluation_reference_labels_split():
    events, period = load_events()
    groups = split_events(events)
    assert len(events) == 14
    assert [e.key for e in groups["positive"]] == [
        "2023-03-11", "2023-12-01", "2023-12-11", "2024-02-26", "2025-04-23",
    ]
    assert {e.key for e in groups["negative"] if e.outcome == EpisodeOutcome.NEAR_MISS} == {
        "2024-03-30", "2025-11-25",
    }
    assert len(groups["negative"]) == 8
    assert [e.key for e in groups["excluded"]] == ["2026-01-28"]
    assert not groups["excluded"][0].evaluable
    assert period.start == datetime(2022, 11, 1, 0)
    assert period.end == datetime(2026, 1, 31, 23)
    assert all(period.contains(e.peak_time) for e in events)
    assert [e.peak_time for e in events] == sorted(e.peak_time for e in events)


def test_evaluation_custom_label_file(tmp_path):
    payload = {
        "version": 1,
        "updated_at": "2026-10-25",
        "description": "test",
        "exhaustive": False,
        "period_start": "2024-01-01",
        "period_end": "2024-03-31",
        "episodes": [
            {"key": "2024-02-02", "peak_time": "2024-02-02T06:00:00", "outcome": "near_miss",
             "severity": 2, "source": "test"},
            {"key": "2024-01-15", "peak_time": "2024-01-15T12:00:00", "outcome": "flooded",
             "severity": 4, "source": "test", "flags": ["x"], "legacy_stored_score": 70.0},
        ],
    }
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    events, period = load_events(path)
    assert [e.key for e in events] == ["2024-01-15", "2024-02-02"]
    assert events[0].positive is True and events[0].flags == ("x",)
    assert events[0].legacy_stored_score == 70.0
    assert events[1].positive is False
    assert period.end == datetime(2024, 3, 31, 23)
    w0, w1 = event_window(events[0], 72, 6)
    assert w0 == datetime(2024, 1, 12, 12) and w1 == datetime(2024, 1, 15, 18)


def test_evaluation_label_file_without_period(tmp_path):
    payload = {"version": 1, "updated_at": "x", "description": "x", "episodes": []}
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    events, period = load_events(path)
    assert events == [] and period is None
