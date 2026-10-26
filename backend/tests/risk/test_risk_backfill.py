"""Backfill feature building: no future data, clean persistence."""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.services import influx_client, risk_core, risk_v2
from app.services.risk_core import (
    StationConfig,
    aggregate_hourly_rain,
    build_historical_features,
    latest_level,
    rain_window_sum,
    rate_cm_per_hour,
)

T = datetime(2024, 2, 10, 12, 0)
PESSAC = StationConfig("P555001001", "Pessac", "Dordogne", 0.30, (3.0, 3.8, 4.5))


def test_risk_latest_level_ignores_future_and_stale_points():
    pts = [(T - timedelta(hours=4), 3.0), (T - timedelta(hours=1), 3.1), (T + timedelta(minutes=5), 9.9)]
    assert latest_level(pts, T) == (T - timedelta(hours=1), 3.1)
    # Only a point older than max age -> no level
    assert latest_level(pts[:1], T, max_age_hours=3) is None
    # Exactly at t is allowed
    assert latest_level([(T, 3.3)], T) == (T, 3.3)


def test_risk_rate_uses_only_past_points():
    past = [(T - timedelta(minutes=15 * k), 3.0 - 0.02 * k / 4) for k in range(12, -1, -1)]  # +2 cm/h
    future_spike = [(T + timedelta(minutes=15 * k), 5.0 + k) for k in range(1, 8)]
    assert rate_cm_per_hour(past, T) == pytest.approx(2.0)
    assert rate_cm_per_hour(past + future_spike, T) == pytest.approx(2.0)
    # A single past point gives no rate (would previously use the next future point)
    assert rate_cm_per_hour([(T - timedelta(hours=1), 3.0)] + future_spike, T) == 0.0


def test_risk_hourly_rain_buckets_are_strictly_past():
    records = [
        (T - timedelta(minutes=30), 4.0, "netatmo:A"),   # in (t-1h, t] -> bucket t
        (T - timedelta(minutes=15), 2.0, "netatmo:A"),   # same bucket, same series -> mean 3
        (T, 1.0, "open-meteo:Moulin"),                    # Open-Meteo value at t = hour ending at t
        (T + timedelta(minutes=10), 50.0, "netatmo:A"),  # FUTURE: bucket t+1h
    ]
    hourly = aggregate_hourly_rain(records)
    assert hourly[T] == pytest.approx((3.0 + 1.0) / 2)  # mean of series means, not a sum
    assert rain_window_sum(hourly, T, 24) == pytest.approx(2.0)
    assert rain_window_sum(hourly, T, 1, forward=True) == pytest.approx(50.0)


def test_risk_historical_features_have_no_future_data():
    past = [(T - timedelta(minutes=15 * k), 3.2 - 0.01 * k) for k in range(12, -1, -1)]
    future = [(T + timedelta(minutes=15 * k), 4.4 + 0.05 * k) for k in range(1, 24)]
    rain = {T - timedelta(hours=k): 0.5 for k in range(24)}
    rain_future = dict(rain)
    rain_future.update({T + timedelta(hours=k): 30.0 for k in range(1, 24)})
    tide = risk_core.TideState(True, 80, 3.0)

    f_past = build_historical_features(T, [PESSAC], {PESSAC.station_id: past}, PESSAC.station_id, rain, tide)
    f_all = build_historical_features(T, [PESSAC], {PESSAC.station_id: past + future}, PESSAC.station_id,
                                      rain_future, tide)
    assert f_all == f_past
    assert f_all.stations[0].height_m == pytest.approx(3.2)
    assert f_all.stations[0].rate_cm_h == pytest.approx(4.0)
    assert f_all.rain_24h_mm == pytest.approx(12.0)
    assert f_all.rain_forecast_24h_mm == 0.0

    # Station without a recent (<= 3h) past point is dropped, not filled from the future
    only_future = build_historical_features(T, [PESSAC], {PESSAC.station_id: future}, PESSAC.station_id, rain, tide)
    assert only_future.stations == ()


class _InfluxStub:
    """In-memory stand-in for the influx_client functions used by the backfill."""

    def __init__(self, points, rain_rows, tide_rows):
        self.points = points
        self.rain_rows = rain_rows
        self.tide_rows = tide_rows
        self.writes: list[dict] = []
        self.batches = 0
        self.existing_scores: list[dict] = []

    async def query_river_levels(self, station_id, start, end):
        return [
            {"timestamp": ts.replace(tzinfo=timezone.utc), "height_m": h, "station_id": station_id}
            for ts, h in self.points.get(station_id, []) if start <= ts < end
        ]

    async def query_rainfall(self, start, end, source=None):
        return [r for r in self.rain_rows if start <= r["timestamp"] < end]

    async def query_tides(self, port, start, end):
        return self.tide_rows if port == "LIDOIRE" else []

    async def write_risk_score(self, score, level, timestamp, factors=None, model_version=None, source=None):
        self.writes.append({"score": score, "level": level, "timestamp": timestamp,
                            "factors": factors, "model_version": model_version, "source": source})
        return True

    async def write_points_batch(self, points, chunk_size=5000):
        """Backfill path: one batched write of risk_score points."""
        for p in points:
            d = influx_client.point_as_dict(p)
            assert d["measurement"] == "risk_score"
            fields = d["fields"]
            self.writes.append({
                "score": fields["score"], "level": fields["level"],
                "timestamp": d["timestamp"],
                "factors": {k[len("factor_"):]: v for k, v in fields.items() if k.startswith("factor_")},
                "model_version": d["tags"].get("model_version"),
                "source": d["tags"].get("source"),
            })
        self.batches += 1
        return len(points)

    async def query_risk_scores(self, start, end, model_version=None):
        return [
            r for r in self.existing_scores
            if start <= r["timestamp"].astimezone(timezone.utc).replace(tzinfo=None) < end
        ]


@pytest.fixture
def influx_stub(monkeypatch):
    from risk_scenario import build_scenario

    sc = build_scenario()
    rain_rows = [
        {"timestamp": k, "precipitation_mm_h": v, "source": "open-meteo", "city": "Moulin"}
        for k, v in sc.rain_hourly.items()
    ]
    stub = _InfluxStub(sc.station_points, rain_rows, sc.tide_rows)
    for name in ("query_river_levels", "query_rainfall", "query_tides", "write_risk_score",
                 "write_points_batch", "query_risk_scores"):
        monkeypatch.setattr(influx_client, name, getattr(stub, name))
    return sc, stub


async def test_risk_backfill_stores_with_model_version_and_audit_fields(influx_stub):
    sc, stub = influx_stub
    stored = await risk_v2.backfill_risk_scores(sc.t0 - timedelta(hours=2), sc.t0)
    assert stored == 3 == len(stub.writes)
    assert stub.batches == 1  # one batched write, not one write per score
    last = stub.writes[-1]
    assert last["model_version"] == risk_v2.MODEL_VERSION
    assert {w["source"] for w in stub.writes} == {"backfill"}
    assert last["timestamp"] == sc.t0
    for key in ("momentum", "conjunction", "dampener", "n_rising", "tide_available", "river_level"):
        assert key in last["factors"]

    # Same score as computing directly from the frozen features at t0
    from app.services.risk_core import compute_risk
    configs = [StationConfig.from_settings_dict(c) for c in get_settings().hubeau_stations if c["weight"] > 0]
    highs, coefs = risk_v2.tide_inputs_from_records(sc.tide_rows)
    feats = build_historical_features(
        sc.t0, configs, sc.station_points, "P555001001", sc.rain_hourly,
        risk_core.tide_at(sc.t0, highs, coefficient_records=coefs),
    )
    assert last["score"] == compute_risk(feats).score


async def test_risk_backfill_future_data_does_not_change_scores(influx_stub):
    sc, stub = influx_stub
    await risk_v2.backfill_risk_scores(sc.t0 - timedelta(hours=1), sc.t0)
    baseline = [w["score"] for w in stub.writes]

    # Inject a huge flood AFTER t0 (levels and rain)
    for sid in stub.points:
        stub.points[sid] = [(ts, h + (5.0 if ts > sc.t0 else 0.0)) for ts, h in stub.points[sid]]
    stub.rain_rows += [
        {"timestamp": sc.t0 + timedelta(minutes=30 * k), "precipitation_mm_h": 40.0, "source": "netatmo", "city": "X"}
        for k in range(1, 20)
    ]
    stub.writes.clear()
    await risk_v2.backfill_risk_scores(sc.t0 - timedelta(hours=1), sc.t0)
    assert [w["score"] for w in stub.writes] == baseline


async def test_risk_backfill_store_false_writes_nothing(influx_stub):
    sc, stub = influx_stub
    computed = await risk_v2.backfill_risk_scores(sc.t0 - timedelta(hours=2), sc.t0, store=False)
    assert computed == 3
    assert stub.writes == []


async def test_risk_backfill_skip_live_hours_keeps_realtime_points(influx_stub):
    """Timeline cache-through: hours with a realtime score are not recomputed."""
    sc, stub = influx_stub
    stub.existing_scores = [
        # realtime point of the current model at 11:02 -> hour 11:00 is live
        {"timestamp": (sc.t0 - timedelta(minutes=58)).replace(tzinfo=timezone.utc),
         "score": 40.0, "model_version": risk_v2.MODEL_VERSION, "source": "realtime"},
        # backfill and older-model points do not protect their hour
        {"timestamp": (sc.t0 - timedelta(hours=2)).replace(tzinfo=timezone.utc),
         "score": 40.0, "model_version": risk_v2.MODEL_VERSION, "source": "backfill"},
        # realtime point of the previous model (3.3) does not protect its hour either
        {"timestamp": (sc.t0 - timedelta(hours=2) + timedelta(minutes=3)).replace(tzinfo=timezone.utc),
         "score": 40.0, "model_version": "3.3", "source": "realtime"},
        {"timestamp": sc.t0.replace(tzinfo=timezone.utc),
         "score": 40.0, "model_version": "3.2", "source": None},
    ]
    stored = await risk_v2.backfill_risk_scores(sc.t0 - timedelta(hours=2), sc.t0, skip_live_hours=True)
    assert stored == 2
    assert [w["timestamp"] for w in stub.writes] == [sc.t0 - timedelta(hours=2), sc.t0]


async def test_risk_backfill_steps_start_on_full_hours(influx_stub):
    sc, stub = influx_stub
    await risk_v2.backfill_risk_scores(sc.t0 - timedelta(hours=2, minutes=17, seconds=5), sc.t0, store=True)
    assert [w["timestamp"] for w in stub.writes] == [
        sc.t0 - timedelta(hours=2), sc.t0 - timedelta(hours=1), sc.t0,
    ]
