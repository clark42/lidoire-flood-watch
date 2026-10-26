"""Replay tides use the production phase lag (overridable for the lag sweep)."""

import math
from datetime import datetime, timedelta

from app.config import get_settings
from scripts.eval.dataset import BuildOptions, high_tides_from_sea_level

T0 = datetime(2024, 2, 1)
SEA = [(T0 + timedelta(hours=k), 2.0 * math.cos(2 * math.pi * k / 12.42)) for k in range(72)]


def test_default_lag_is_the_production_open_meteo_lag():
    lag = get_settings().tide_phase_lag_hours["open-meteo-marine"]
    assert BuildOptions().resolved_tide_lag() == lag
    assert BuildOptions(tide_lag_hours=0).resolved_tide_lag() == 0
    raw = high_tides_from_sea_level(SEA, 0)
    assert high_tides_from_sea_level(SEA) == [(ts + timedelta(hours=lag), c) for ts, c in raw]


def test_lag_override_shifts_high_tides_only():
    raw = high_tides_from_sea_level(SEA, 0)
    assert raw and raw[0][0] == T0 + timedelta(hours=12)  # first interior maximum
    shifted = high_tides_from_sea_level(SEA, 4.5)
    assert [ts for ts, _ in shifted] == [ts + timedelta(hours=4.5) for ts, _ in raw]
    assert [c for _, c in shifted] == [c for _, c in raw]
