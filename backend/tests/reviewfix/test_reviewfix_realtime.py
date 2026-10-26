"""Realtime: unrounded station rate, Pessac-based Dordogne alert."""

from datetime import datetime, timedelta

import pytest

from app.api.realtime import primary_station_alert
from app.config import get_settings
from app.models.schemas import RainfallSummary, RiverLevel
from app.services import risk_core, risk_v2
from app.services.collectors import hubeau
from app.services.risk_core import StationConfig, compute_risk

from risk_scenario import build_scenario

T = datetime(2026, 10, 25, 12, 0)
PESSAC = "P555001001"


def _pessac_levels(rate_cm_h: float, hours: float = 3.0, step_min: int = 15):
    cfg = next(s for s in get_settings().hubeau_stations if s["id"] == PESSAC)
    n = int(hours * 60 / step_min)
    return [
        RiverLevel(
            timestamp=T - timedelta(minutes=step_min * (n - i)),
            station_id=PESSAC, station_name=cfg["name"], river=cfg["river"],
            height_m=3.2 - rate_cm_h / 100.0 * step_min * (n - i) / 60,
        )
        for i in range(n + 1)
    ]


async def test_reviewfix_realtime_rate_is_not_rounded_before_rising_test(monkeypatch):
    levels = _pessac_levels(1.04)

    async def fake_fetch(hours_back=48):
        return {PESSAC: levels}

    monkeypatch.setattr(hubeau, "fetch_all_stations_realtime", fake_fetch)
    monkeypatch.setattr(hubeau, "_utcnow", lambda: T)

    data = await hubeau.get_multi_station_data()
    station = next(s for s in data.stations if s.station_id == PESSAC)
    assert station.rate_cm_per_hour == pytest.approx(1.04)
    assert station.rate_cm_per_hour > risk_core.RISING_RATE_CM_H
    # The display helper still rounds (that value used to feed the model)
    assert hubeau._calculate_rate_of_change(levels, t=T) == 1.0

    # Same rate and same rising count as the backfill / TimeMachine path
    settings = get_settings()
    cfg = StationConfig.from_settings_dict(next(s for s in settings.hubeau_stations if s["id"] == PESSAC))
    points = [(lv.timestamp, lv.height_m) for lv in levels]
    feat = risk_core.station_feature_at(cfg, points, T, rate_window_h=settings.risk_rate_window_hours)
    assert station.rate_cm_per_hour == pytest.approx(feat.rate_cm_h)

    rain = RainfallSummary(last_hour_mm=0, last_24h_mm=5, last_72h_mm=10, last_7d_mm=12)
    realtime = compute_risk(risk_v2.build_realtime_features(data, None, rain, T))
    backfill = compute_risk(risk_core.build_historical_features(
        T, [cfg], {PESSAC: points}, PESSAC, {T - timedelta(hours=k): 5 / 24 for k in range(24)},
        risk_core.TideState(False, None, None),
    ))
    assert realtime.n_rising == backfill.n_rising == 1


def test_reviewfix_dordogne_alert_ignores_tidal_libourne():
    sc = build_scenario()
    data = sc.stations_data.model_copy(deep=True)
    libourne = next(s for s in data.stations if s.station_id == "P577001001")
    libourne.current_level.height_m = 4.6  # spring high tide at Libourne
    pessac = next(s for s in data.stations if s.station_id == PESSAC)
    pessac.current_level.height_m = pessac.threshold_orange - 0.01
    assert primary_station_alert(data) is None

    pessac.current_level.height_m = pessac.threshold_orange + 0.2
    alert = primary_station_alert(data)
    assert alert is not None
    assert pessac.station_name in alert and "seuil orange" in alert
    assert primary_station_alert(None) is None


def test_reviewfix_realtime_endpoint_no_longer_uses_fixed_4m_libourne_test():
    import inspect

    from app.api import realtime

    source = inspect.getsource(realtime.get_realtime_data)
    assert "river_data.current.height_m > 4.0" not in source
    assert "primary_station_alert(" in source
