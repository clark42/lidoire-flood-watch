"""Pure climatology helpers (percentiles, trends, thresholds)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import climatology as c

T = datetime(2026, 2, 12, 12, 0)


def test_percentile_of_mid_rank_and_bounds():
    history = list(range(1, 101))  # 1..100
    assert c.percentile_of(100, history) == 99.5
    assert c.percentile_of(1000, history) == 100.0
    assert c.percentile_of(-5, history) == 0.0
    assert c.percentile_of(50.5, history) == 50.0
    assert c.percentile_of(50, history) == 49.5


def test_percentile_of_ties_and_missing():
    assert c.percentile_of(1.0, [1.0] * 40) == 50.0
    assert c.percentile_of(None, range(100)) is None
    assert c.percentile_of(3.0, [1.0, 2.0]) is None              # history too short
    assert c.percentile_of(3.0, [1.0, 2.0], min_history=1) == 100.0
    assert c.percentile_of(2.0, [None, 1.0, 3.0], min_history=2) == 50.0


def test_percentile_label():
    assert c.percentile_label(99.6) == "p99"
    assert c.percentile_label(100.0) == "p100"
    assert c.percentile_label(None) is None


def test_quantile():
    assert c.quantile([0, 10], 0.5) == 5.0
    assert c.quantile([3.0, 1.0, 2.0], 1.0) == 3.0
    assert c.quantile([], 0.5) is None


def _series(values, step_min=60, end=T):
    n = len(values)
    return [{"timestamp": end - timedelta(minutes=step_min * (n - 1 - i)), "value": v}
            for i, v in enumerate(values)]


def test_slope_and_trend():
    pts = _series([1.0, 2.0, 3.0, 4.0])  # +1 per hour
    assert c.slope_per_hour(pts, T, window_h=4) == pytest.approx(1.0)
    assert c.trend_label(1.0, 0.1) == "rising"
    assert c.trend_label(-1.0, 0.1) == "falling"
    assert c.trend_label(0.05, 0.1) == "stable"
    assert c.trend_label(None, 0.1) is None
    # points after t are ignored, too few points -> None
    assert c.slope_per_hour(pts + [{"timestamp": T + timedelta(hours=1), "value": 100}], T, 4) == pytest.approx(1.0)
    assert c.slope_per_hour(pts[-2:], T, 4) is None


def test_latest_and_trailing_mean_accept_aware_timestamps():
    aware = [{"timestamp": (T - timedelta(hours=h)).replace(tzinfo=timezone.utc), "value": float(h)}
             for h in range(30)]
    assert c.latest(aware, T) == (T, 0.0)
    assert c.trailing_mean(aware, T, 24) == pytest.approx(sum(range(24)) / 24)
    assert c.latest([], T) is None
    assert c.trailing_mean([], T, 24) is None


def test_threshold_status():
    th = {"watch": 13.1, "alert": 17.5}
    s = c.threshold_status(15.0, th)
    assert s["exceeded"] == ["watch"] and s["next"] == "alert"
    assert s["distance"] == pytest.approx(2.5)
    assert s["ratio_to_next"] == pytest.approx(15.0 / 17.5, abs=1e-3)
    top = c.threshold_status(27.4, th)
    assert top["exceeded"] == ["watch", "alert"] and top["next"] is None
    assert c.threshold_status(None, th)["exceeded"] == []
    assert c.threshold_status(1.0, {})["next"] is None
