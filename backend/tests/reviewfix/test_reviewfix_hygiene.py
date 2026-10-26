"""Episode analyzer v2 in UTC; no utcnow()/print() left in the reviewed modules."""

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.models.schemas import RainfallObservation

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


# --- episode analyzer v2: UTC archive ---------------------------------------


@respx.mock
async def test_reviewfix_analyzer_v2_requests_utc_archive():
    from app.services import episode_analyzer_v2

    route = respx.get(url__startswith="https://archive-api.open-meteo.com/v1/archive").mock(
        return_value=httpx.Response(200, json={"hourly": {
            "time": ["2024-02-15T13:00", "2024-02-15T14:00"], "precipitation": [1.0, 2.0],
        }}),
    )
    rain = await episode_analyzer_v2._fetch_historical_rainfall(
        datetime(2024, 2, 14, 14), datetime(2024, 2, 15, 14),
    )
    assert route.calls.last.request.url.params["timezone"] == "UTC"
    assert [r.timestamp for r in rain] == [datetime(2024, 2, 15, 13), datetime(2024, 2, 15, 14)]


def test_reviewfix_analyzer_v2_converts_aware_episode_date():
    from app.services import episode_analyzer_v2

    paris = timezone(timedelta(hours=1))

    rain = [RainfallObservation(timestamp=datetime(2024, 2, 15, 13), source="a", precipitation_mm_h=2.0),
            RainfallObservation(timestamp=datetime(2024, 2, 15, 14), source="a", precipitation_mm_h=5.0)]
    # 14:00+01:00 == 13:00 UTC: the 14:00 UTC hour is after the episode
    ctx = episode_analyzer_v2._calculate_episode_context(datetime(2024, 2, 15, 14, tzinfo=paris), {}, [], rain)
    assert ctx.rain_24h_before_mm == pytest.approx(2.0)


# --- hygiene: no utcnow()/print() left in the reviewed modules ----------------


FILES = [
    APP / "services" / "risk_v2.py",
    APP / "services" / "scheduler.py",
    APP / "services" / "backfill.py",
    APP / "services" / "data_store.py",
    APP / "services" / "forecast.py",
    APP / "services" / "episode_analyzer.py",
    APP / "services" / "episode_analyzer_v2.py",
    APP / "services" / "episode_detector.py",
    APP / "services" / "video_analyzer.py",
    APP / "api" / "realtime.py",
    APP / "api" / "episodes.py",
    APP / "models" / "orm.py",
]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_reviewfix_no_utcnow_no_print(path):
    tree = ast.parse(path.read_text())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "utcnow":
            offenders.append(f"utcnow line {node.lineno}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            offenders.append(f"print() line {node.lineno}")
    assert offenders == []
