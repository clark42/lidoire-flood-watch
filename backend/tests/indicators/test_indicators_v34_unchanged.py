"""The hydrological indicators have ZERO effect on the risk model v3.4.

- ``hubeau_stations`` (iterated by the river score, momentum and the
  multi-station branches) is byte-for-byte the pre-indicator configuration;
- model parameters and ``compute_risk`` outputs are identical with and
  without the indicator config blocks, and equal to golden values computed
  on main before the indicators existed;
- no model module imports the indicator code.
"""

import dataclasses
import re
from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.services import influx_client
from app.services.risk_core import (
    MODEL_VERSION,
    RiskFeatures,
    StationFeature,
    compute_risk,
    get_model_params,
    params_from_settings,
)

# Snapshot of main (26446f2) before the indicators: id, name, river, delay, weight, lat, lon, thresholds
HUBEAU_STATIONS_V34 = [
    ("P577001001", "Libourne", "Dordogne", 0, 0.0, 44.9167, -0.2333, (3.0, 4.0, 5.0)),
    ("P555001001", "Pessac-sur-Dordogne", "Dordogne", 4, 0.30, 44.8131, 0.0381, (3.0, 3.8, 4.5)),
    ("P514001001", "Bergerac", "Dordogne", 8, 0.15, 44.8531, 0.4836, (2.5, 3.5, 4.5)),
    ("P726151001", "Abzac", "Isle", 2, 0.25, 44.8789, -0.1278, (2.2, 3.0, 4.0)),
    ("P704151001", "Périgueux", "Isle", 12, 0.10, 45.1837, 0.7187, (1.8, 2.8, 3.8)),
    ("P846251001", "Coutras", "Dronne", 3, 0.15, 45.0411, -0.1233, (1.8, 2.5, 3.5)),
    ("P816251001", "Ribérac", "Dronne", 10, 0.05, 45.2461, 0.3450, (1.2, 2.0, 3.0)),
]

T = datetime(2024, 2, 26, 6, 0)


def _settings_without_indicators() -> Settings:
    return Settings(indicator_flow_stations=[], indicator_groundwater_stations=[], indicator_tide_gauges=[])


def _snapshot(settings) -> list[tuple]:
    return [
        (s["id"], s["name"], s["river"], s["delay_hours"], s["weight"], s["lat"], s["lon"],
         (s["thresholds"]["yellow"], s["thresholds"]["orange"], s["thresholds"]["red"]))
        for s in settings.hubeau_stations
    ]


def test_hubeau_stations_unchanged():
    settings = get_settings()
    assert _snapshot(settings) == HUBEAU_STATIONS_V34
    assert set(settings.hubeau_stations[0]) == {
        "id", "name", "river", "delay_hours", "weight", "lat", "lon", "thresholds", "distance_km",
    }
    assert settings.hubeau_primary_station == "P555001001"
    assert _snapshot(_settings_without_indicators()) == HUBEAU_STATIONS_V34


def test_indicator_stations_are_not_model_stations():
    settings = get_settings()
    model_ids = {s["id"] for s in settings.hubeau_stations}
    indicator_ids = {s["id"] for s in settings.indicator_flow_stations if s["key"] != "PESSAC"}
    assert model_ids.isdisjoint(indicator_ids)
    # Query whitelist of river_level untouched: Eyraud/Argentat... stay unknown there
    assert influx_client.known_station_ids() == model_ids
    with pytest.raises(influx_client.InvalidQueryParameter):
        influx_client.validate_station_ids(["P540401002"])


def _station(settings, sid, h, rate):
    c = next(x for x in settings.hubeau_stations if x["id"] == sid)
    th = c["thresholds"]
    return StationFeature(sid, c["name"], c["river"], h, rate, c["weight"], (th["yellow"], th["orange"], th["red"]))


def _cases(settings) -> dict[str, RiskFeatures]:
    st = lambda sid, h, r: _station(settings, sid, h, r)  # noqa: E731
    return {
        "calm": RiskFeatures(
            timestamp=T, stations=(st("P555001001", 1.2, 0.0), st("P726151001", 1.0, -0.2)),
            primary_station_id="P555001001", rain_24h_mm=2, rain_72h_mm=5, rain_7d_mm=10,
            tide_available=True, tide_coefficient=60, hours_to_high_tide=5.0,
            westerly_wind_kmh=10, soil_saturation_factor=1.0,
        ),
        "flood": RiskFeatures(
            timestamp=T,
            stations=tuple(st(i, h, r) for i, h, r in [
                ("P555001001", 4.3, 4.0), ("P514001001", 3.6, 2.0), ("P726151001", 3.1, 1.5),
                ("P704151001", 2.9, 1.2), ("P846251001", 2.6, 1.1), ("P816251001", 1.5, 0.5),
            ]),
            primary_station_id="P555001001", rain_24h_mm=35, rain_72h_mm=90, rain_7d_mm=130,
            rain_forecast_24h_mm=10, rain_forecast_72h_mm=20, tide_available=True, tide_coefficient=95,
            hours_to_high_tide=0.5, backwater_level_m=4.4, westerly_wind_kmh=50, soil_saturation_factor=1.4,
        ),
        "high_stable": RiskFeatures(
            timestamp=T,
            stations=(st("P555001001", 3.9, -0.5), st("P514001001", 2.0, 0.0), st("P726151001", 1.8, 0.0)),
            primary_station_id="P555001001", rain_24h_mm=3, rain_72h_mm=8, rain_7d_mm=None,
            tide_available=False, backwater_level_m=4.0,
        ),
        "moderate": RiskFeatures(
            timestamp=T,
            stations=(st("P555001001", 3.2, 1.5), st("P514001001", 2.6, 1.2),
                      st("P726151001", 2.0, 0.3), st("P846251001", 1.5, 0.0)),
            primary_station_id="P555001001", rain_24h_mm=18, rain_72h_mm=45, rain_7d_mm=70,
            tide_available=True, tide_coefficient=88, hours_to_high_tide=-2.0,
            backwater_level_m=3.3, westerly_wind_kmh=35, soil_saturation_factor=1.2,
        ),
    }


# compute_risk on main (26446f2), before the indicators:
# score, level, weighted_sum, total_weight, conjunction, dampener, tide_gate
GOLDEN = {
    "calm": (12.9, "green", 0.122952, 0.95, 1.0, 1.0, 0.0),
    "flood": (100.0, "red", 1.992185, 0.95, 2.5, 1.0, 1.0),
    "high_stable": (16.4, "yellow", 0.134439, 0.82, 1.0, 0.65, 1.0),
    "moderate": (76.9, "red", 0.730079, 0.95, 1.8, 1.0, 0.375),
}


def test_model_params_identical_with_and_without_indicator_config():
    with_indicators = params_from_settings(get_settings())
    without = params_from_settings(_settings_without_indicators())
    assert with_indicators == without
    assert get_model_params() == without
    assert MODEL_VERSION == "3.4"


@pytest.mark.parametrize("name", list(GOLDEN))
def test_compute_risk_identical_and_matches_golden(name):
    with_cfg = get_settings()
    without_cfg = _settings_without_indicators()
    a = compute_risk(_cases(with_cfg)[name], params_from_settings(with_cfg))
    b = compute_risk(_cases(without_cfg)[name], params_from_settings(without_cfg))
    c = compute_risk(_cases(with_cfg)[name])  # default cached params (app path)
    assert dataclasses.asdict(a) == dataclasses.asdict(b) == dataclasses.asdict(c)

    score, level, wsum, total, conj, damp, gate = GOLDEN[name]
    assert a.score == score and a.level.value == level
    assert a.weighted_sum == pytest.approx(wsum, abs=1e-6)
    assert a.total_weight == pytest.approx(total)
    assert a.conjunction_multiplier == pytest.approx(conj)
    assert a.stability_dampener == pytest.approx(damp)
    assert a.tide_gate == pytest.approx(gate)


MODEL_MODULES = [
    "risk_core.py", "risk_v2.py", "forecast.py", "risk_shadow.py",
    "episode_detector.py", "notifier.py", "data_store.py",
]


@pytest.mark.parametrize("module", MODEL_MODULES)
def test_model_modules_never_read_indicators(module):
    text = (Path(__file__).resolve().parents[2] / "app" / "services" / module).read_text(encoding="utf-8")
    assert not re.search(r"indicator|hydro_indicators|climatology|river_flow|groundwater|tide_observed", text)
