"""Fake clock, in-memory state store, mocked ntfy server and data sources."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

import httpx
import pytest

from app.config import Settings
from app.models.schemas import (
    MultiStationData,
    RainfallSummary,
    RiverLevel,
    StationLevel,
    Tendency,
    TideData,
    TideForecast,
    TideType,
)
from app.services.notifier import Notifier, NtfyChannel, Sources
from app.services.risk_core import score_to_level

T0 = datetime(2024, 2, 10, 12, 0, 0)
NTFY_URL = "https://ntfy.example.test"
NTFY_TOPIC = "lidoire-test-topic"
FRONTEND_URL = "http://lidoire.test"


def make_settings(**overrides) -> Settings:
    values = {"ntfy_url": NTFY_URL, "ntfy_topic": NTFY_TOPIC, "frontend_url": FRONTEND_URL}
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeClock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, hours: float = 1.0) -> datetime:
        self.now += timedelta(hours=hours)
        return self.now


class MemoryStore:
    def __init__(self):
        self.value: Optional[str] = None
        self.saves = 0

    async def load(self) -> Optional[str]:
        return self.value

    async def save(self, value: str) -> None:
        self.value = value
        self.saves += 1


@dataclass
class NtfyServer:
    """httpx.MockTransport handler recording every POST."""

    status: int = 200
    requests: list = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"id": "x"})

    @property
    def titles(self) -> list[str]:
        return [decode_title(r.headers["Title"]) for r in self.requests]

    def take(self) -> list[str]:
        titles = self.titles
        self.requests.clear()
        return titles


def decode_title(value: str) -> str:
    import base64

    if value.startswith("=?UTF-8?B?"):
        return base64.b64decode(value[len("=?UTF-8?B?"):-2]).decode("utf-8")
    return value


def forecast_entries(t0: datetime, scores) -> list[dict]:
    """168 h forecast; ``scores(h)`` gives the score at H+h."""
    return [
        {
            "timestamp": (t0 + timedelta(hours=h)).isoformat() + "+00:00",
            "hours_ahead": h,
            "score": float(scores(h)),
            "level": score_to_level(float(scores(h))).value,
        }
        for h in range(0, 169)
    ]


@dataclass
class World:
    """Observations served to the notifier (mutated by the tests)."""

    clock: FakeClock
    station_age_h: Optional[float] = 0.5   # None = no Pessac point at all
    station_level: float = 3.92
    score: Optional[float] = None           # last stored score (None = none stored)
    score_age_h: float = 0.1
    forecast: Callable[[int], float] = lambda h: 5.0  # score at H+h
    fail_inputs: bool = False

    def inputs(self):
        now = self.clock.now
        stations = []
        if self.station_age_h is not None:
            stations.append(StationLevel(
                station_id="P555001001", station_name="Pessac-sur-Dordogne", river="Dordogne",
                current_level=RiverLevel(
                    timestamp=now - timedelta(hours=self.station_age_h), station_id="P555001001",
                    station_name="Pessac-sur-Dordogne", river="Dordogne", height_m=self.station_level,
                ),
                tendency=Tendency.RISING, rate_cm_per_hour=2.1, weight=0.30,
            ))
        tide_at = now + timedelta(hours=2, minutes=30)
        return type("Inputs", (), {
            "stations": MultiStationData(timestamp=now, stations=stations),
            "rainfall": RainfallSummary(last_72h_mm=48.4),
            "tide": TideForecast(
                port="LIDOIRE",
                next_high_tide=TideData(timestamp=tide_at, port="LIDOIRE", tide_type=TideType.HIGH,
                                        height_m=5.1, coefficient=95),
                available=True,
            ),
        })()

    def sources(self) -> Sources:
        async def realtime_inputs():
            if self.fail_inputs:
                raise RuntimeError("hub'eau down")
            return self.inputs()

        async def latest_score(now):
            if self.score is None:
                return None
            return {
                "timestamp": now - timedelta(hours=self.score_age_h),
                "score": self.score,
                "level": score_to_level(self.score).value,
                "source": "realtime",
            }

        async def forecast():
            return forecast_entries(self.clock.now, self.forecast)

        return Sources(realtime_inputs=realtime_inputs, latest_score=latest_score, forecast=forecast)


@dataclass
class Harness:
    clock: FakeClock
    world: World
    store: MemoryStore
    server: NtfyServer
    settings: Settings

    def notifier(self) -> Notifier:
        """A fresh Notifier (same store): simulates a backend restart."""
        channel = NtfyChannel(
            self.settings.ntfy_url, self.settings.ntfy_topic, self.settings.ntfy_token,
            transport=httpx.MockTransport(self.server),
        )
        return Notifier(channel, self.store, self.settings, clock=self.clock, sources=self.world.sources())

    async def run(self, score: Optional[float] = None, advance: float = 1.0) -> list[str]:
        """Advance the clock, store a new score, run once; returns the titles sent."""
        self.clock.advance(advance)
        if score is not None:
            self.world.score = score
        await self.notifier().run_once()
        return self.server.take()

    @property
    def state(self) -> dict:
        return json.loads(self.store.value) if self.store.value else {}


@pytest.fixture
def harness():
    def _make(**overrides) -> Harness:
        clock = FakeClock()
        return Harness(clock, World(clock), MemoryStore(), NtfyServer(), make_settings(**overrides))

    return _make
