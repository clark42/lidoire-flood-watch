"""Startup migration of flood_episodes and ORM <-> schema mapping.

Uses a synchronous in-memory SQLite engine (stdlib driver) to exercise the
sync migration body; PostgreSQL takes the ADD COLUMN IF NOT EXISTS branch.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text

from app.models.orm import FloodEpisodeRow
from app.models.schemas import EpisodeOutcome, FloodEpisodeCreate
from app.services.data_store import (
    _apply_ground_truth,
    _row_to_episode,
    migrate_episode_schema_sync,
)
from app.services.labels import LEGACY_PREFIX_SOURCE

LEGACY_DDL = """
CREATE TABLE flood_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TIMESTAMP NOT NULL,
    peak_time TIMESTAMP,
    end_time TIMESTAMP,
    severity INTEGER NOT NULL,
    description VARCHAR(500),
    notes TEXT,
    created_at TIMESTAMP
)
"""


@pytest.fixture
def legacy_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        rows = [
            (1, "[CONFIRMED: FLOODED] 25cm\n"),
            (2, "[CONFIRMED: FALSE POSITIVE] [CONFIRMED: FLOODED] seuil\nseuil"),
            (3, "pas de prefixe"),
            (4, None),
        ]
        for row_id, notes in rows:
            conn.execute(
                text(
                    "INSERT INTO flood_episodes (id, start_time, severity, notes) "
                    "VALUES (:id, '2024-02-26 12:00:00', 3, :notes)"
                ),
                {"id": row_id, "notes": notes},
            )
    yield engine
    engine.dispose()


def _rows(engine):
    with engine.connect() as conn:
        return {
            r.id: r
            for r in conn.execute(
                text("SELECT id, notes, outcome, outcome_source FROM flood_episodes")
            )
        }


def test_groundtruth_migration_adds_columns_and_migrates_prefixes(legacy_engine):
    with legacy_engine.begin() as conn:
        report = migrate_episode_schema_sync(conn)

    assert set(report["columns_added"]) == {
        "outcome", "observed_at", "water_height_cm", "outcome_source",
    }
    assert report["legacy_rows_migrated"] == 2

    cols = {c["name"] for c in inspect(legacy_engine).get_columns("flood_episodes")}
    assert {"outcome", "observed_at", "water_height_cm", "outcome_source"} <= cols

    rows = _rows(legacy_engine)
    assert rows[1].outcome == "flooded" and rows[1].notes == "25cm"
    assert rows[1].outcome_source == LEGACY_PREFIX_SOURCE
    assert rows[2].outcome == "false_positive" and rows[2].notes == "seuil"
    assert rows[3].outcome is None and rows[3].notes == "pas de prefixe"
    assert rows[4].outcome is None


def test_groundtruth_migration_is_idempotent(legacy_engine):
    with legacy_engine.begin() as conn:
        migrate_episode_schema_sync(conn)
    with legacy_engine.begin() as conn:
        report = migrate_episode_schema_sync(conn)
    assert report == {"columns_added": [], "legacy_rows_migrated": 0}


def test_groundtruth_migration_does_not_override_existing_outcome(legacy_engine):
    with legacy_engine.begin() as conn:
        migrate_episode_schema_sync(conn)
        conn.execute(text(
            "UPDATE flood_episodes SET outcome = 'near_miss', "
            "notes = '[CONFIRMED: FLOODED] x' WHERE id = 3"
        ))
    with legacy_engine.begin() as conn:
        migrate_episode_schema_sync(conn)
    assert _rows(legacy_engine)[3].outcome == "near_miss"


def test_groundtruth_migration_on_fresh_orm_table():
    engine = create_engine("sqlite://")
    FloodEpisodeRow.metadata.create_all(engine, tables=[FloodEpisodeRow.__table__])
    with engine.begin() as conn:
        report = migrate_episode_schema_sync(conn)
    assert report == {"columns_added": [], "legacy_rows_migrated": 0}
    engine.dispose()


def _row(**overrides):
    fields = dict(
        id=7,
        start_time=datetime(2024, 2, 25),
        peak_time=None,
        end_time=None,
        severity=4,
        description=None,
        notes=None,
        created_at=datetime(2026, 1, 1),
        outcome=None,
        observed_at=None,
        water_height_cm=None,
        outcome_source=None,
    )
    fields.update(overrides)
    return FloodEpisodeRow(**fields)


def test_groundtruth_row_to_episode_structured_outcome():
    ep = _row_to_episode(_row(outcome="near_miss", water_height_cm=3.0, outcome_source="owner"))
    assert ep.outcome == EpisodeOutcome.NEAR_MISS
    assert ep.water_height_cm == 3.0
    assert ep.outcome_source == "owner"


def test_groundtruth_row_to_episode_legacy_fallback():
    ep = _row_to_episode(_row(notes="[CONFIRMED: FALSE POSITIVE] rien"))
    assert ep.outcome == EpisodeOutcome.FALSE_POSITIVE
    assert ep.notes == "rien"
    assert ep.outcome_source == LEGACY_PREFIX_SOURCE


def test_groundtruth_row_to_episode_structured_wins_over_prefix():
    ep = _row_to_episode(_row(outcome="flooded", notes="[CONFIRMED: FALSE POSITIVE] x"))
    assert ep.outcome == EpisodeOutcome.FLOODED


def test_groundtruth_row_to_episode_invalid_outcome_is_none():
    assert _row_to_episode(_row(outcome="garbage")).outcome is None


def test_groundtruth_update_keeps_labels_when_fields_omitted():
    row = _row(outcome="flooded", water_height_cm=25.0, outcome_source="owner")
    legacy_payload = FloodEpisodeCreate(start_time=datetime(2024, 2, 25), severity=4, notes="x")
    _apply_ground_truth(row, legacy_payload, only_set=True)
    assert row.outcome == "flooded"
    assert row.water_height_cm == 25.0


def test_groundtruth_update_applies_explicit_fields():
    row = _row(outcome="flooded", water_height_cm=25.0)
    payload = FloodEpisodeCreate(
        start_time=datetime(2024, 2, 25),
        severity=4,
        outcome="false_positive",
        water_height_cm=None,
        observed_at=datetime(2024, 2, 26, 13, tzinfo=timezone.utc),
    )
    _apply_ground_truth(row, payload, only_set=True)
    assert row.outcome == "false_positive"
    assert row.water_height_cm is None
    assert row.observed_at == datetime(2024, 2, 26, 13)  # tz stripped


def test_groundtruth_create_applies_all_fields():
    row = _row(outcome="flooded")
    _apply_ground_truth(
        row, FloodEpisodeCreate(start_time=datetime(2024, 2, 25), severity=4), only_set=False
    )
    assert row.outcome is None
