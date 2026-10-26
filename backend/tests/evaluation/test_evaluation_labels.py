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
    assert len(events) == 15
    assert [e.key for e in groups["positive"]] == [
        "2023-03-11", "2023-11-30", "2024-02-26", "2025-04-21", "2026-02-12",
    ]
    assert {e.key for e in groups["negative"] if e.outcome == EpisodeOutcome.NEAR_MISS} == {
        "2023-12-11", "2024-03-30", "2025-11-25",
    }
    assert len(groups["negative"]) == 9
    assert [e.key for e in groups["excluded"]] == ["2026-01-28"]
    assert not groups["excluded"][0].evaluable
    assert period.start == datetime(2022, 11, 1, 0)
    assert period.end == datetime(2026, 2, 28, 23)
    # hour-precision peaks from the owner's answers reach the evaluation
    by_key = {e.key: e for e in events}
    assert by_key["2023-11-30"].peak_time == datetime(2023, 11, 30, 22, 15)
    assert by_key["2023-11-30"].peak_time_precision_hours == 0.5
    assert by_key["2025-04-21"].peak_time == datetime(2025, 4, 21, 1, 30)
    assert by_key["2026-02-12"].peak_time_precision_hours == 2
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


def test_evaluation_peak_with_minutes_maps_to_its_hour_row():
    # owner peaks carry minutes (2023-11-30T22:15): the row of 22:00 is the peak row
    from datetime import timedelta

    from scripts.eval.dataset import EvalDataset, FeatureRow

    t0 = datetime(2023, 11, 30, 20)
    rows = [FeatureRow(timestamp=t0 + timedelta(hours=i), features=None, values={}) for i in range(4)]
    ds = EvalDataset(rows, (), "P555001001")
    assert ds.index_of(datetime(2023, 11, 30, 22, 15)) is None
    assert ds.hour_index(datetime(2023, 11, 30, 22, 15)) == 2
    assert ds.hour_index(datetime(2023, 11, 30, 23)) == 3
    assert ds.hour_index(datetime(2023, 12, 1, 0)) is None
