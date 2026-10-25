"""One rate helper: risk_core.rate_cm_per_hour is THE implementation (hubeau imports it)."""

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.models.schemas import RiverLevel
from app.services import risk_core
from app.services.collectors import hubeau

T = datetime(2026, 10, 25, 12, 0)


def _levels(heights, step_min=15, end=T, aware=False):
    n = len(heights)
    out = []
    for i, h in enumerate(heights):
        ts = end - timedelta(minutes=step_min * (n - 1 - i))
        if aware:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(RiverLevel(timestamp=ts, station_id="P555001001", station_name="Pessac",
                              river="Dordogne", height_m=h))
    return out


def test_followups_hubeau_reuses_risk_core_helper():
    assert hubeau.rate_cm_per_hour is risk_core.rate_cm_per_hour
    source = pathlib.Path(hubeau.__file__).read_text()
    assert "def rate_cm_per_hour" not in source


def test_followups_risk_core_stays_pure():
    tree = ast.parse(pathlib.Path(risk_core.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {"httpx", "influxdb_client", "sqlalchemy", "redis", "asyncio"}
    assert not {m.split(".")[0] for m in imported} & forbidden
    assert not any(m.startswith(("app.services", "app.api")) for m in imported)


def test_followups_same_rate_for_every_input_shape():
    heights = [3.0 + 0.02 * i for i in range(13)]  # +8 cm/h
    levels = _levels(heights)
    tuples = [(lv.timestamp, lv.height_m) for lv in levels]
    aware = _levels(heights, aware=True)
    dicts = [{"timestamp": lv.timestamp.isoformat() + "Z", "height_m": lv.height_m} for lv in levels]
    shuffled = list(reversed(levels))
    expected = risk_core.rate_cm_per_hour(tuples, T)
    assert expected == pytest.approx(8.0)
    for series in (levels, aware, dicts, shuffled):
        assert risk_core.rate_cm_per_hour(series, T) == pytest.approx(expected)
    assert risk_core.rate_cm_per_hour(tuples, T.replace(tzinfo=timezone.utc)) == pytest.approx(expected)


def test_followups_rate_ignores_future_points_for_all_shapes():
    past = _levels([3.0] * 13)
    future = _levels([4.0, 5.0], end=T + timedelta(minutes=30))
    series = past + future
    assert risk_core.rate_cm_per_hour(series, T) == 0.0
    assert risk_core.rate_cm_per_hour([(lv.timestamp, lv.height_m) for lv in series], T) == 0.0


def test_followups_rate_thresholds():
    # 2 points spanning 1h are enough (shared default); < 30 min span is not
    two = [(T - timedelta(hours=1), 3.0), (T, 3.1)]
    assert risk_core.rate_cm_per_hour(two, T) == pytest.approx(10.0)
    assert risk_core.rate_cm_per_hour([(T - timedelta(minutes=20), 3.0), (T, 3.1)], T) == 0.0
    assert risk_core.rate_cm_per_hour(two, T, min_points=3) == 0.0
    assert risk_core.rate_cm_per_hour([], T) == 0.0
    # malformed points are skipped in the generic path
    assert risk_core.rate_cm_per_hour([{"timestamp": T}, *two], T) == pytest.approx(10.0)


def test_followups_live_and_backfill_rates_agree():
    """Realtime (hubeau wrapper) and backfill (station_feature_at) give the same rate."""
    heights = [3.0] * 7 + [3.02, 3.05, 3.09, 3.14, 3.20, 3.27]
    levels = _levels(heights)
    tuples = [(lv.timestamp, lv.height_m) for lv in levels]
    cfg = risk_core.StationConfig("P555001001", "Pessac", "Dordogne", 0.30, (3.0, 3.8, 4.5))
    feat = risk_core.station_feature_at(cfg, tuples, T)
    assert feat.rate_cm_h > 1.0
    assert hubeau._calculate_rate_of_change(levels, t=T) == round(feat.rate_cm_h, 1)
