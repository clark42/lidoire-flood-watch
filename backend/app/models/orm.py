"""SQLAlchemy ORM models for PostgreSQL persistence."""

from datetime import datetime
from sqlalchemy import Integer, String, DateTime, Text, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FloodEpisodeRow(Base):
    __tablename__ = "flood_episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    peak_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Structured ground truth (added by data_store.migrate_episode_schema on
    # existing databases; null outcome = not labelled yet)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    water_height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
