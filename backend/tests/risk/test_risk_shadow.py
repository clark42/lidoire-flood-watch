"""Shadow model 3.4-gate: stored by the hourly job and the opt-in backfill, v3.4 untouched."""

from datetime import timedelta, timezone

import pytest

from app.services import influx_client, risk_shadow, risk_v2
from risk_scenario import build_scenario


class _ShadowInflux:
    """In-memory influx_client for realtime + backfill writes (both measurements)."""

    def __init__(self, sc):
        self.sc = sc
        self.points = {sid: list(p) for sid, p in sc.station_points.items()}
        self.rain_rows = [
            {"timestamp": k, "precipitation_mm_h": v, "source": "open-meteo", "city": "Moulin"}
            for k, v in sc.rain_hourly.items()
        ]
        self.writes: list[dict] = []
        self.river_queries: list[tuple] = []

    async def query_river_levels(self, station_id, start, end):
        self.river_queries.append((station_id, start, end))
        return [
            {"timestamp": ts.replace(tzinfo=timezone.utc), "height_m": h, "station_id": station_id}
            for ts, h in self.points.get(station_id, []) if start <= ts < end
        ]

    async def query_rainfall(self, start, end, source=None):
        return [r for r in self.rain_rows if start <= r["timestamp"] < end]

    async def query_tides(self, port, start, end):
        return self.sc.tide_rows if port == "LIDOIRE" else []

    async def query_risk_scores(self, start, end, model_version=None):
        return []

    def _record(self, d):
        fields = d["fields"]
        self.writes.append({
            "measurement": d["measurement"], "score": fields["score"], "level": fields["level"],
            "timestamp": d["timestamp"], "model_version": d["tags"].get("model_version"),
            "source": d["tags"].get("source"),
            "factors": {k[len("factor_"):]: v for k, v in fields.items() if k.startswith("factor_")},
        })

    async def write_risk_score(self, score, level, timestamp, factors=None, model_version=None, source=None):
        self._record(influx_client.point_as_dict(
            influx_client.make_risk_point(score, level, timestamp, factors, model_version, source)
        ))
        return True

    async def write_points_batch(self, points, chunk_size=5000):
        for p in points:
            self._record(influx_client.point_as_dict(p))
        return len(points)

    def of(self, measurement):
        return [w for w in self.writes if w["measurement"] == measurement]


@pytest.fixture
def shadow_influx(monkeypatch):
    sc = build_scenario()
    stub = _ShadowInflux(sc)
    for name in ("query_river_levels", "query_rainfall", "query_tides", "query_risk_scores",
                 "write_risk_score", "write_points_batch"):
        monkeypatch.setattr(influx_client, name, getattr(stub, name))

    inputs = risk_v2.RealtimeInputs(
        timestamp=sc.t0, tide=sc.tide, rainfall=sc.rainfall, rainfall_raw=sc.rainfall,
        stations=sc.stations_data,
    )

    async def fake_gather(now=None):
        return inputs

    monkeypatch.setattr(risk_v2, "gather_realtime_inputs", fake_gather)
    return sc, stub, inputs


async def test_shadow_realtime_job_stores_both_series(shadow_influx):
    sc, stub, inputs = shadow_influx
    risk, _ = await risk_v2.compute_current_risk(store=True)

    # Displayed score is exactly v3.4, stored once in risk_score
    assert risk.model_version == "3.4"
    assert risk.score == risk_v2.risk_from_inputs(inputs).score
    main = stub.of("risk_score")
    assert [(w["score"], w["model_version"], w["source"]) for w in main] == [(risk.score, "3.4", "realtime")]

    # Shadow: Pessac max over the trailing 12.5h (points <= t0) = 3.2 m -> gate 0.625
    [shadow] = stub.of("risk_score_shadow")
    assert shadow["model_version"] == risk_shadow.SHADOW_MODEL_VERSION == "3.4-gate"
    assert shadow["source"] == "realtime"
    assert shadow["timestamp"] == sc.t0
    assert shadow["factors"]["h_aval_m"] == pytest.approx(3.2)
    assert shadow["factors"]["gate"] == pytest.approx(0.625)
    assert shadow["factors"]["base_score"] == risk.score
    assert shadow["score"] == pytest.approx(risk.score * 0.625)
    assert shadow["level"] == risk_shadow.shadow_level(shadow["score"]).value
    # H_aval is the backwater level of the production features (v3.4 tide gate input)
    assert shadow["factors"]["h_aval_m"] == inputs.features().backwater_level_m


async def test_shadow_not_stored_when_store_false(shadow_influx):
    _, stub, _ = shadow_influx
    await risk_v2.compute_current_risk(store=False)
    assert stub.writes == []


async def test_shadow_failure_never_breaks_the_v33_score(shadow_influx, monkeypatch):
    _, stub, inputs = shadow_influx

    async def boom(*a, **k):
        raise RuntimeError("influx down")

    monkeypatch.setattr(risk_shadow, "gated_score", boom)
    risk, _ = await risk_v2.compute_current_risk(store=True)
    assert risk.score == risk_v2.risk_from_inputs(inputs).score
    assert len(stub.of("risk_score")) == 1
    assert stub.of("risk_score_shadow") == []


async def test_shadow_uses_realtime_level_when_not_yet_stored(shadow_influx):
    sc, stub, inputs = shadow_influx
    stub.points["P555001001"] = []  # river job has not stored anything yet
    risk, _ = await risk_v2.compute_current_risk(store=True)
    [shadow] = stub.of("risk_score_shadow")
    assert shadow["factors"]["h_aval_m"] == pytest.approx(3.2)  # Hub'Eau current level at t0


async def test_shadow_backfill_is_opt_in_and_leaves_v33_unchanged(shadow_influx):
    sc, stub, _ = shadow_influx
    start = sc.t0 - timedelta(hours=3)

    await risk_v2.backfill_risk_scores(start, sc.t0)
    assert stub.of("risk_score_shadow") == []
    baseline = [(w["timestamp"], w["score"]) for w in stub.of("risk_score")]
    assert len(baseline) == 4

    stub.writes.clear()
    stored = await risk_v2.backfill_risk_scores(start, sc.t0, shadow=True)
    assert stored == 4  # v3.4 count only
    assert [(w["timestamp"], w["score"]) for w in stub.of("risk_score")] == baseline
    shadow = stub.of("risk_score_shadow")
    assert [w["timestamp"] for w in shadow] == [ts for ts, _ in baseline]
    assert {(w["model_version"], w["source"]) for w in shadow} == {("3.4-gate", "backfill")}
    pessac = stub.points["P555001001"]
    for w, (ts, base) in zip(shadow, baseline):
        h = risk_shadow.backwater_level(pessac, ts)
        assert w["factors"]["h_aval_m"] == pytest.approx(h)
        assert w["score"] == pytest.approx(base * risk_shadow.backwater_gate(h))
        assert w["score"] <= base


async def test_shadow_backfill_flag_reaches_risk_backfill(monkeypatch):
    from app.services import backfill

    seen = {}

    async def fake_backfill(start, end, shadow=False):
        seen["shadow"] = shadow
        return 0

    monkeypatch.setattr(risk_v2, "backfill_risk_scores", fake_backfill)
    s, e = build_scenario().t0 - timedelta(days=1), build_scenario().t0
    flags = dict(skip_rivers=True, skip_rainfall=True, skip_tides=True, skip_soil=True)
    await backfill.run_full_backfill(s, e, **flags)
    assert seen["shadow"] is False
    await backfill.run_full_backfill(s, e, risk_shadow=True, **flags)
    assert seen["shadow"] is True
