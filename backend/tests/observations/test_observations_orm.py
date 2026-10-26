"""ORM mapping of the observations table (synchronous in-memory SQLite)."""

from datetime import datetime

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.db import Base
from app.models.orm import ObservationRow
from app.services.observations import row_to_observation


def test_observations_table_created_and_mapped():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[ObservationRow.__table__])
    columns = {c["name"]: c for c in inspect(engine).get_columns("observations")}
    assert set(columns) == {
        "id", "observed_at", "kind", "water_height_cm", "reference", "note", "photo_path", "created_at", "source",
    }
    assert columns["water_height_cm"]["nullable"] and columns["photo_path"]["nullable"]
    assert not columns["observed_at"]["nullable"] and not columns["kind"]["nullable"]
    assert "ix_observations_observed_at" in {i["name"] for i in inspect(engine).get_indexes("observations")}

    with Session(engine) as session:
        row = ObservationRow(observed_at=datetime(2026, 2, 14, 5, 30), kind="flooded", water_height_cm=12.0,
                             reference="plancher", photo_path="0123456789abcdef0123456789abcdef.jpg")
        session.add(row)
        session.commit()
        obs = row_to_observation(row)
    assert obs.id == 1 and obs.source == "user" and obs.created_at is not None
    assert obs.has_photo and obs.photo_url == "/api/observations/1/photo"
    engine.dispose()
