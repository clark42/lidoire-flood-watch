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
    assert len(labels) == 14
    assert all(isinstance(lb, EpisodeLabel) for lb in labels)
    assert [lb.peak_time for lb in labels] == sorted(lb.peak_time for lb in labels)
    assert len({lb.key for lb in labels}) == 14


def test_groundtruth_labels_outcome_counts():
    labels = load_episode_labels()
    counts = {o: sum(1 for lb in labels if lb.outcome == o) for o in EpisodeOutcome}
    assert counts[EpisodeOutcome.FLOODED] == 5
    assert counts[EpisodeOutcome.NEAR_MISS] == 2
    assert counts[EpisodeOutcome.FALSE_POSITIVE] == 6
    assert counts[EpisodeOutcome.UNKNOWN] == 1


def test_groundtruth_labels_known_floods_by_date():
    floods = {lb.key for lb in labels_by_outcome(EpisodeOutcome.FLOODED)}
    assert floods == {"2023-03-11", "2023-12-01", "2023-12-11", "2024-02-26", "2025-04-23"}


def test_groundtruth_labels_ep14_conflict_resolved_by_date():
    # "EP14" means Dec 2023 in scripts but April 2025 in risk_v2 docstrings
    dec = get_label_by_key("2023-12-01")
    apr = get_label_by_key(date(2025, 4, 23))
    assert dec.legacy_db_id == 14 and dec.water_height_cm == 25
    assert apr.legacy_db_id == 1
    assert "ep_number_conflict_ep14" in dec.flags
    assert "ep_number_conflict_ep14" in apr.flags
    assert get_label_by_key(datetime(2025, 4, 23, 16)) == apr
    assert get_label_by_key("1999-01-01") is None


def test_groundtruth_labels_match_legacy_script_ids():
    # Every legacy id 1..14 used in validate_model.py appears exactly once
    ids = sorted(lb.legacy_db_id for lb in load_episode_labels())
    assert ids == list(range(1, 15))


def test_groundtruth_label_set_metadata():
    label_set = load_label_set()
    assert label_set.exhaustive is False
    assert label_set.inconsistencies
    assert label_set.caveats


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
