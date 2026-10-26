"""Reference labels file (app/data/episodes_labels.json) and its loader."""

import json
from datetime import date, datetime

import pytest

from app.models.schemas import EpisodeLabel, EpisodeOutcome
from app.services.labels import (
    LABELS_PATH,
    get_label_by_key,
    labels_by_outcome,
    load_episode_labels,
    load_label_set,
)


def test_groundtruth_labels_file_location():
    assert LABELS_PATH.name == "episodes_labels.json"
    assert LABELS_PATH.parent.name == "data"
    assert LABELS_PATH.parent.parent.name == "app"
    assert LABELS_PATH.exists()


def test_groundtruth_labels_load_typed_and_sorted():
    labels = load_episode_labels()
    assert len(labels) == 15
    assert all(isinstance(lb, EpisodeLabel) for lb in labels)
    assert [lb.peak_time for lb in labels] == sorted(lb.peak_time for lb in labels)
    assert len({lb.key for lb in labels}) == 15


def test_groundtruth_labels_outcome_counts():
    labels = load_episode_labels()
    counts = {o: sum(1 for lb in labels if lb.outcome == o) for o in EpisodeOutcome}
    assert counts[EpisodeOutcome.FLOODED] == 5
    assert counts[EpisodeOutcome.NEAR_MISS] == 3
    assert counts[EpisodeOutcome.FALSE_POSITIVE] == 6
    assert counts[EpisodeOutcome.UNKNOWN] == 1


def test_groundtruth_labels_known_floods_by_date():
    floods = {lb.key for lb in labels_by_outcome(EpisodeOutcome.FLOODED)}
    assert floods == {"2023-03-11", "2023-11-30", "2024-02-26", "2025-04-21", "2026-02-12"}
    near = {lb.key for lb in labels_by_outcome(EpisodeOutcome.NEAR_MISS)}
    assert near == {"2023-12-11", "2024-03-30", "2025-11-25"}
    assert get_label_by_key("2026-01-28").outcome == EpisodeOutcome.UNKNOWN


def test_groundtruth_labels_ep14_conflict_resolved_by_date():
    # "EP14" means Dec 2023 in scripts but April 2025 in risk_v2 docstrings
    dec = get_label_by_key("2023-11-30")
    apr = get_label_by_key(date(2025, 4, 21))
    assert dec.legacy_db_id == 14 and dec.water_height_cm == 25
    assert apr.legacy_db_id == 1
    assert "ep_number_conflict_ep14" in dec.flags
    assert "ep_number_conflict_ep14" in apr.flags
    assert get_label_by_key(datetime(2025, 4, 21, 1, 30)) == apr
    # the old (wrong) keys are gone
    assert get_label_by_key("2023-12-01") is None
    assert get_label_by_key("2025-04-23") is None
    assert get_label_by_key("1999-01-01") is None


def test_groundtruth_labels_match_legacy_script_ids():
    # Every legacy id 1..14 used in validate_model.py appears exactly once;
    # the 2026-02-12 flood was added from the owner's answers (no legacy id)
    labels = load_episode_labels()
    ids = sorted(lb.legacy_db_id for lb in labels if lb.legacy_db_id is not None)
    assert ids == list(range(1, 15))
    assert [lb.key for lb in labels if lb.legacy_db_id is None] == ["2026-02-12"]


# Owner's answers of 2026-10-26: local time (Europe/Paris) -> naive UTC peak, precision (h)
OWNER_PEAKS = {
    "2023-11-30": (datetime(2023, 11, 30, 23, 15), 0.5),  # 23:00-23:30 CET
    "2024-02-26": (datetime(2024, 2, 26, 12, 0), 1.0),  # 12:00 CET
    "2025-04-21": (datetime(2025, 4, 21, 3, 30), 1.0),  # 03:00-04:00 CEST
    "2026-02-12": (datetime(2026, 2, 12, 17, 0), 2.0),  # late afternoon CET
}


@pytest.mark.parametrize("key", sorted(OWNER_PEAKS))
def test_groundtruth_labels_owner_peaks_converted_from_paris_time(key):
    from zoneinfo import ZoneInfo

    local, precision = OWNER_PEAKS[key]
    expected = local.replace(tzinfo=ZoneInfo("Europe/Paris")).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    label = get_label_by_key(key)
    peak = label.peak_time.replace(tzinfo=None) if label.peak_time.tzinfo else label.peak_time
    assert peak == expected
    assert label.peak_time_precision_hours == precision
    assert label.outcome == EpisodeOutcome.FLOODED
    assert label.statements and label.statements[-1].source == "owner"
    assert label.statements[-1].date == "2026-10-26"
    assert "Europe/Paris" in label.statements[-1].local_time


def test_groundtruth_labels_precision_and_statements():
    labels = load_episode_labels()
    # every label states its precision; untouched labels keep day-level (12 h)
    assert all(lb.peak_time_precision_hours is not None for lb in labels)
    revised = {lb.key for lb in labels if lb.statements}
    assert revised == {"2023-11-30", "2023-12-11", "2024-02-26", "2025-04-21", "2026-02-12"}
    assert all(lb.peak_time_precision_hours == 12 for lb in labels if lb.key not in OWNER_PEAKS)
    dec11 = get_label_by_key("2023-12-11")
    assert dec11.outcome == EpisodeOutcome.NEAR_MISS
    assert "presque" in dec11.statements[0].text


def test_groundtruth_label_set_metadata():
    label_set = load_label_set()
    assert label_set.exhaustive is False
    assert label_set.version == 2
    assert label_set.period_end == "2026-02-28"
    assert label_set.inconsistencies
    assert label_set.caveats


def test_groundtruth_loader_new_fields_optional(tmp_path):
    # labels written before peak_time_precision_hours / statements still load
    path = _write(tmp_path, _base_payload([_ep("2024-01-01", "2024-01-01T06:00:00")]))
    (label,) = load_episode_labels(path)
    assert label.peak_time_precision_hours is None
    assert label.statements == []


def test_groundtruth_loader_rejects_non_positive_precision(tmp_path):
    ep = {**_ep("2024-01-01", "2024-01-01T06:00:00"), "peak_time_precision_hours": 0}
    with pytest.raises(ValueError):
        load_label_set(_write(tmp_path, _base_payload([ep])))


def _write(tmp_path, payload):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base_payload(episodes):
    return {"version": 1, "updated_at": "2026-10-25", "description": "t", "episodes": episodes}


def _ep(key, peak, outcome="flooded"):
    return {"key": key, "peak_time": peak, "outcome": outcome, "severity": 3, "source": "test"}


def test_groundtruth_loader_rejects_duplicate_keys(tmp_path):
    path = _write(tmp_path, _base_payload([
        _ep("2024-01-01", "2024-01-01T06:00:00"),
        _ep("2024-01-01", "2024-01-01T12:00:00"),
    ]))
    with pytest.raises(ValueError, match="Duplicate"):
        load_label_set(path)


def test_groundtruth_loader_rejects_key_date_mismatch(tmp_path):
    path = _write(tmp_path, _base_payload([_ep("2024-01-02", "2024-01-01T06:00:00")]))
    with pytest.raises(ValueError, match="does not match"):
        load_label_set(path)


def test_groundtruth_loader_rejects_invalid_outcome(tmp_path):
    path = _write(tmp_path, _base_payload([_ep("2024-01-01", "2024-01-01T06:00:00", "maybe")]))
    with pytest.raises(ValueError):
        load_label_set(path)


def test_groundtruth_loader_custom_path(tmp_path):
    path = _write(tmp_path, _base_payload([
        _ep("2024-02-01", "2024-02-01T06:00:00", "near_miss"),
        _ep("2024-01-01", "2024-01-01T06:00:00"),
    ]))
    labels = load_episode_labels(path)
    assert [lb.key for lb in labels] == ["2024-01-01", "2024-02-01"]
    assert labels[1].outcome == EpisodeOutcome.NEAR_MISS
