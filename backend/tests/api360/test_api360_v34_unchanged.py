"""The profile / drivers / analogs / radar frame endpoints have ZERO effect on v3.4.

- model parameters and ``compute_risk`` outputs do not depend on the new
  settings (NGF zeros, alpha, mill ground, analogs, radar frame);
- no model module imports the new code or reads the new settings.
"""

import dataclasses
import re
from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.services.risk_core import (
    MODEL_VERSION,
    RiskFeatures,
    StationFeature,
    compute_risk,
    get_model_params,
    params_from_settings,
)

T = datetime(2024, 2, 26, 6, 0)

ALTERED = dict(
    ngf_station_zeros_m={"P555001001": 0.0, "P577001001": 0.0},
    confluence_downstream_weight=0.9,
    confluence_envelope_hours=1.0,
    mill_ground_z_m_ngf=0.0,
    drivers_envelope_hours=1.0,
    drivers_rain_hours=1.0,
    analog_axes=[],
    analog_distance_threshold=0.0,
    radar_frame_margin_km=0.0,
    radar_frame_pixel_deg=1.0,
)


def _features(settings) -> RiskFeatures:
    def st(sid, h, rate):
        c = next(x for x in settings.hubeau_stations if x["id"] == sid)
        th = c["thresholds"]
        return StationFeature(sid, c["name"], c["river"], h, rate, c["weight"],
                              (th["yellow"], th["orange"], th["red"]))

    return RiskFeatures(
        timestamp=T,
        stations=(st("P555001001", 3.2, 1.5), st("P514001001", 2.6, 1.2),
                  st("P726151001", 2.0, 0.3), st("P846251001", 1.5, 0.0)),
        primary_station_id="P555001001", rain_24h_mm=18, rain_72h_mm=45, rain_7d_mm=70,
        tide_available=True, tide_coefficient=88, hours_to_high_tide=-2.0,
        backwater_level_m=3.3, westerly_wind_kmh=35, soil_saturation_factor=1.2,
    )


def test_api360_settings_do_not_change_model_params():
    altered = Settings(**ALTERED)
    assert params_from_settings(altered) == params_from_settings(get_settings()) == get_model_params()
    assert altered.hubeau_stations == get_settings().hubeau_stations
    assert MODEL_VERSION == "3.4"


def test_api360_compute_risk_matches_golden():
    a = compute_risk(_features(get_settings()))
    b = compute_risk(_features(Settings(**ALTERED)), params_from_settings(Settings(**ALTERED)))
    assert dataclasses.asdict(a) == dataclasses.asdict(b)
    # Golden "moderate" case of tests/indicators/test_indicators_v34_unchanged.py (main 26446f2)
    assert a.score == 76.9 and a.level.value == "red"
    assert a.weighted_sum == pytest.approx(0.730079, abs=1e-6)
    assert a.conjunction_multiplier == pytest.approx(1.8)
    assert a.tide_gate == pytest.approx(0.375)


MODEL_MODULES = [
    "risk_core.py", "risk_v2.py", "forecast.py", "risk_shadow.py", "risk_v4.py",
    "episode_detector.py", "notifier.py", "scheduler.py", "backfill.py",
]
FORBIDDEN = re.compile(
    r"episode_drivers|lidoire_profile|radar_frame|ngf_station_zeros|confluence_(upstream|downstream|envelope)"
    r"|mill_ground_z|analog_|drivers_"
)


@pytest.mark.parametrize("module", MODEL_MODULES)
def test_api360_model_modules_never_import_the_new_code(module):
    path = Path(__file__).resolve().parents[2] / "app" / "services" / module
    if not path.exists():
        pytest.skip(f"{module} absent")
    assert not FORBIDDEN.search(path.read_text(encoding="utf-8"))
