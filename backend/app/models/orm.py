"""SQLAlchemy ORM models for PostgreSQL persistence."""

from datetime import datetime, timezone
from sqlalchemy import JSON, Integer, String, DateTime, Text, Float, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    """Naive UTC now (column default)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FloodEpisodeRow(Base):
    __tablename__ = "flood_episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    peak_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Structured ground truth (added by data_store.migrate_episode_schema on
    # existing databases; null outcome = not labelled yet)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    water_height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_source: Mapped[str | None] = mapped_column(String(100), nullable=True)


class NotifierStateRow(Base):
    """Notifier state (JSON), one row per key: survives backend restarts."""

    __tablename__ = "notifier_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ForecastRunRow(Base):
    """One archived forecast run (services/forecast_archive.py), one per hour.

    ``scores`` / ``levels`` / ``pessac_levels_m``: compact JSON lists indexed
    by lead hour (index 0 = H+0 = ``run_at``). Rows are never updated.
    """

    __tablename__ = "forecast_runs"
    __table_args__ = (UniqueConstraint("run_at", "model_version", name="uq_forecast_runs_run_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    scores: Mapped[list] = mapped_column(JSON, nullable=False)
    levels: Mapped[list] = mapped_column(JSON, nullable=False)
    pessac_levels_m: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Key input summaries (forecast.forecast_inputs_summary)
    rain_forecast_24h_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    rain_forecast_72h_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    pessac_projected_max_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    tide_gate: Mapped[float | None] = mapped_column(Float, nullable=True)
    inputs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ObservationRow(Base):
    """Field observation at the mill (ground truth reported by the owner).

    ``water_height_cm`` is relative to ``reference`` (free text such as
    "terrasse", "plancher", "seuil du moulin"); ``photo_path`` is a file name
    inside settings.observations_photo_dir (never a client-supplied path).
    """

    __tablename__ = "observations"
    __table_args__ = (Index("ix_observations_observed_at", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    water_height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_path: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="user")
