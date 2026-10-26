"""Tide phase: realtime, forecast H+0, backfill and TimeMachine see the same high tides.

Review finding (risk_v2.py:395): realtime only knew FUTURE high tides, so for
~6h after each high water the tide phase dropped (hours_to_high = +11.9
instead of -0.5) while the backfill / TimeMachine used past and future highs.
"""

from datetime import timedelta, timezone

import pytest

from app.api import realtime as realtime_api
from app.config import get_settings
from app.models.schemas import TideData, TideType
from app.services import influx_client, risk_core, risk_v2
from app.services.collectors import tide as tide_collector
from app.services.forecast import ForecastInputs, build_forecast
from app.services.risk_core import StationConfig, compute_risk

from risk_scenario import T0, build_scenario

HW_PREV = T0 - timedelta(minutes=30)          # high water that just passed
HW_PREV2 = T0 - timedelta(hours=12, minutes=55)
LW_PREV = T0 - timedelta(hours=6, minutes=42)
LW_NEXT = T0 + timedelta(hours=5, minutes=42)
HW_NEXT = T0 + timedelta(hours=11, minutes=55)

EVENTS = [
    (HW_PREV2, "PM", 5.5, 88),
    (LW_PREV, "BM", 0.9, None),
    (HW_PREV, "PM", 5.6, 90),
    (LW_NEXT, "BM", 0.8, None),
    (HW_NEXT, "PM", 5.7, 92),
]


def _tide_data(source="shom"):
    return [
        TideData(timestamp=ts, port="BORDEAUX",
                 tide_type=TideType.HIGH if kind == "PM" else TideType.LOW,
                 height_m=h, coefficient=c, source=source)
        for ts, kind, h, c in EVENTS
    ]


@pytest.fixture
def realtime_tide(monkeypatch):
    """TideForecast exactly as get_current_tide_data() builds it at T0."""
    monkeypatch.setattr(tide_collector, "_tide_cache", {})
    monkeypatch.setattr(tide_collector, "_cache_expiry", None)
    monkeypatch.setattr(tide_collector, "_utcnow", lambda: T0)

    async def fake_fetch():
        return _tide_data()

    async def no_store(tides):
        return len(tides)

    monkeypatch.setattr(tide_collector, "_fetch_real_tides", fake_fetch)
    monkeypatch.setattr(tide_collector, "store_tides_to_influx", no_store)

    async def _get():
        return await tide_collector.get_current_tide_data()

    return _get


def _backfill_features(sc, tide_rows):
    settings = get_settings()
    configs = [StationConfig.from_settings_dict(c) for c in settings.hubeau_stations if c["weight"] > 0]
    highs, coefs = risk_v2.tide_inputs_from_records(tide_rows)
    tide = risk_core.tide_at(sc.t0, highs, coefficient_records=coefs)
    return risk_core.build_historical_features(
        sc.t0, configs, sc.station_points, settings.hubeau_primary_station,
        dict(sc.rain_hourly), tide,
    )


async def test_reviewfix_collector_keeps_recent_tides(realtime_tide):
    tide = await realtime_tide()
    assert [t.timestamp for t in tide.recent_tides] == [HW_PREV2, LW_PREV, HW_PREV]
    assert all(t.timestamp > T0 for t in tide.tides)  # display list unchanged: future only
    assert tide.current_coefficient == 90  # latest past high water
    assert (HW_PREV, 90) in risk_v2.tide_high_list(tide)


async def test_reviewfix_same_score_realtime_backfill_just_after_high_tide(realtime_tide, tide_rows_from_events):
    sc = build_scenario()
    tide = await realtime_tide()

    realtime = risk_v2.build_realtime_features(
        sc.stations_data, tide, sc.rainfall, sc.t0,
        westerly_wind_kmh=None, soil_saturation_factor=None,
    )
    backfill = _backfill_features(sc, tide_rows_from_events(EVENTS))

    assert realtime.hours_to_high_tide == pytest.approx(-0.5)
    assert backfill.hours_to_high_tide == pytest.approx(-0.5)
    assert realtime.tide_coefficient == backfill.tide_coefficient == 90
    assert realtime.tide_available and backfill.tide_available

    rt, bf = compute_risk(realtime), compute_risk(backfill)
    assert rt.score == bf.score
    assert rt.tide_phase_score == bf.tide_phase_score
    assert rt.tide_phase_score > 0.9
    assert rt.tide_score == bf.tide_score

    # Old behaviour (future highs only) understated the phase for ~6h
    future_only = tide.model_copy(update={"recent_tides": []})
    old = risk_v2.build_realtime_features(
        sc.stations_data, future_only, sc.rainfall, sc.t0,
        westerly_wind_kmh=None, soil_saturation_factor=None,
    )
    assert old.hours_to_high_tide == pytest.approx(11 + 55 / 60)
    assert compute_risk(old).tide_phase_score < rt.tide_phase_score


async def test_reviewfix_forecast_h0_sees_past_high_tide(realtime_tide):
    sc = build_scenario()
    tide = await realtime_tide()
    settings = get_settings()
    fi = ForecastInputs(
        t0=sc.t0,
        stations=risk_v2.station_features_from_multi(sc.stations_data, sc.t0),
        primary_station_id=sc.stations_data.primary_station_id,
        station_delays={s["id"]: float(s["delay_hours"]) for s in settings.hubeau_stations},
        rain_hourly=dict(sc.rain_hourly),
        observed_rain=sc.rainfall,
        tide_highs=risk_v2.tide_high_list(tide),
        tide_fallback_coefficient=tide.current_coefficient,
        tide_reliable=risk_v2.tide_forecast_is_reliable(tide),
        soil_saturation_factor=None,
        westerly_wind_kmh=None,
    )
    realtime = compute_risk(risk_v2.build_realtime_features(
        sc.stations_data, tide, sc.rainfall, sc.t0,
        westerly_wind_kmh=None, soil_saturation_factor=None,
    ))
    assert build_forecast(fi, 0)[0]["score"] == realtime.score


async def test_reviewfix_collector_reads_stored_past_high_when_list_has_none(monkeypatch, tide_rows_from_events):
    """SHOM-like list starting at fetch time: the past high comes from InfluxDB."""
    monkeypatch.setattr(tide_collector, "_tide_cache", {})
    monkeypatch.setattr(tide_collector, "_cache_expiry", None)
    monkeypatch.setattr(tide_collector, "_utcnow", lambda: T0)
    future = [t for t in _tide_data() if t.timestamp > T0]

    async def fake_fetch():
        return future

    async def no_store(tides):
        return len(tides)

    queried = []

    async def fake_query_tides(port, start, end):
        queried.append((port, start, end))
        return [r for r in tide_rows_from_events(EVENTS) if start <= r["timestamp"] <= end]

    monkeypatch.setattr(tide_collector, "_fetch_real_tides", fake_fetch)
    monkeypatch.setattr(tide_collector, "store_tides_to_influx", no_store)
    monkeypatch.setattr(influx_client, "query_tides", fake_query_tides)

    tide = await tide_collector.get_current_tide_data()
    assert queried and queried[0][0] == "BORDEAUX"
    assert queried[0][1] == T0 - timedelta(hours=13)
    assert HW_PREV in [t.timestamp for t in tide.recent_tides if t.tide_type == TideType.HIGH]
    assert risk_v2.tide_state_from_forecast(tide, T0).hours_to_high_tide == pytest.approx(-0.5)


def test_reviewfix_backfill_high_tides_carry_their_coefficient(tide_rows_from_events):
    highs, coefs = risk_v2.tide_inputs_from_records(tide_rows_from_events(EVENTS))
    assert highs == [(HW_PREV2, 88), (HW_PREV, 90), (HW_NEXT, 92)]
    # 6h10 after HW_PREV the nearest coefficient record is HW_NEXT's, but the
    # coefficient used is still the latest PAST high's (same as realtime)
    t = HW_PREV + timedelta(hours=6, minutes=10)
    assert risk_core.tide_at(t, highs, coefficient_records=coefs).coefficient == 90


# --- TimeMachine: tides over +/-13h ----------------------------------------


def _snapshot(sc, tide_rows, window_h=6):
    river = {
        sid: [
            {"timestamp": ts.replace(tzinfo=timezone.utc).isoformat(), "height_m": h,
             "station_name": sid, "river": "x"}
            for ts, h in pts if abs((ts - sc.t0).total_seconds()) <= window_h * 3600
        ]
        for sid, pts in sc.station_points.items()
    }
    tides = []
    for r in tide_rows:
        if abs((r["timestamp"] - sc.t0).total_seconds()) > window_h * 3600:
            continue
        ts = r["timestamp"].replace(tzinfo=timezone.utc).isoformat()
        field = "height_m" if r["height_m"] is not None else "coefficient"
        tides.append({"timestamp": ts, "value": r[field], "field": field,
                      "port": "BORDEAUX", "tide_type": r["tide_type"]})
    return {"river_levels": river, "rainfall": [], "tides": tides, "risk_scores": []}


async def test_reviewfix_timemachine_finds_high_tide_6h05_away(monkeypatch, tide_rows_from_events):
    sc = build_scenario()
    # Target 6h05 after a high water, next one 6h20 later: both outside +/-6h
    events = [
        (sc.t0 - timedelta(hours=6, minutes=5), "PM", 5.6, 90),
        (sc.t0 + timedelta(minutes=8), "BM", 0.8, None),
        (sc.t0 + timedelta(hours=6, minutes=20), "PM", 5.7, 92),
    ]
    rows = tide_rows_from_events(events)
    writes = []

    async def fake_snapshot(target_time, window_hours=1):
        return _snapshot(sc, rows, window_hours)

    async def fake_rainfall(start, end, source=None):
        return [
            {"timestamp": k.replace(tzinfo=timezone.utc), "precipitation_mm_h": v,
             "source": "open-meteo", "city": "Moulin"}
            for k, v in sc.rain_hourly.items() if start <= k < end
        ]

    async def fake_tides(port, start, end):
        # Real query_tides rows carry AWARE UTC timestamps
        if port != "BORDEAUX":
            return []
        return [{**r, "timestamp": r["timestamp"].replace(tzinfo=timezone.utc)}
                for r in rows if start <= r["timestamp"] <= end]

    async def forbidden(*a, **k):
        writes.append((a, k))
        return True

    monkeypatch.setattr(realtime_api, "query_historical_snapshot", fake_snapshot)
    monkeypatch.setattr(realtime_api, "query_rainfall", fake_rainfall)
    monkeypatch.setattr(realtime_api, "write_river_levels_batch", forbidden)
    monkeypatch.setattr(realtime_api, "write_points_batch", forbidden)
    monkeypatch.setattr(influx_client, "query_tides", fake_tides)

    # The +/-6h snapshot alone has no high tide (the old behaviour)
    snap_highs, _ = risk_v2.tide_inputs_from_records(
        realtime_api._snapshot_tide_rows(_snapshot(sc, rows))
    )
    assert not risk_core.tide_at(sc.t0, snap_highs).available

    data = await realtime_api.get_realtime_data_at(sc.t0.isoformat())
    assert writes == []
    assert data.risk.factors.tide_available is True

    settings = get_settings()
    configs = [StationConfig.from_settings_dict(c) for c in settings.hubeau_stations]
    highs, coefs = risk_v2.tide_inputs_from_records(rows)
    feats = risk_core.build_historical_features(
        sc.t0, configs, sc.station_points, settings.hubeau_primary_station, sc.rain_hourly,
        risk_core.tide_at(sc.t0, highs, coefficient_records=coefs),
    )
    assert feats.hours_to_high_tide == pytest.approx(-(6 + 5 / 60))
    assert data.risk.score == compute_risk(feats).score
    # Display: PM/BM events of the +/-13h window, next ones after the target
    assert data.tide.next_high_tide.timestamp == sc.t0 + timedelta(hours=6, minutes=20)
    assert data.tide.next_low_tide.timestamp == sc.t0 + timedelta(minutes=8)
