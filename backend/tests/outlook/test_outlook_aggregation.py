"""Outlook aggregation (pure): horizons, why, checklist, confidence notes."""

import json
from datetime import timedelta

import pytest

from app.services import outlook
from app.services.outlook import (
    build_outlook,
    explain_drivers,
    factor_points,
    load_action_plan,
    next_high_tide,
    summarize_horizons,
)

from outlook_fixtures import T0, forecast_result, rising_inputs, storm_inputs

PLAN = {
    "levels": {
        "yellow": {"title": "Surveiller", "actions": ["Regarder le tableau de bord"]},
        "orange": {"title": "Se preparer", "actions": ["Surelever les objets"]},
        "red": {"title": "Proteger", "actions": ["Mettre les protections", "Mettre a l'abri"]},
    }
}


def _entries(scores):
    from app.services.risk_core import score_to_level

    return [
        {"hours_ahead": h, "timestamp": (T0 + timedelta(hours=h)).isoformat() + "+00:00",
         "score": s, "level": score_to_level(s).value}
        for h, s in enumerate(scores)
    ]


def test_outlook_horizons_max_level_and_time():
    result, breakdowns = forecast_result(rising_inputs())
    entries = result["forecasts"]
    horizons = {h["hours"]: h for h in result["horizons"]}
    assert list(horizons) == [6, 24, 72]
    for hours, h in horizons.items():
        window = [f for f in entries if f["hours_ahead"] <= hours]
        best = max(window, key=lambda f: f["score"])
        assert h["max_score"] == best["score"]
        assert h["level"] == best["level"]
        assert h["max_at"] == best["timestamp"]
        assert h["first_at_level"] <= h["max_at"]
    # Rain arriving later: the longer horizons see more
    assert horizons[6]["max_score"] <= horizons[24]["max_score"] <= horizons[72]["max_score"]
    assert horizons[6]["level"] == "yellow"
    assert horizons[72]["level"] == "red"


def test_outlook_ties_keep_earliest_and_first_at_level():
    scores = [10, 20, 40, 40, 30, 60, 60, 20]
    entries = _entries(scores)
    result, breakdowns = forecast_result(rising_inputs(), hours=len(scores) - 1)
    out = summarize_horizons(entries, breakdowns, horizons=((3, "3 h"), (7, "7 h")))
    h3, h7 = out
    assert h3["max_score"] == 40 and h3["max_hours_ahead"] == 2       # earliest of the tie
    assert h3["level"] == "orange" and h3["first_at_level"] == entries[2]["timestamp"]
    assert h7["max_hours_ahead"] == 5 and h7["level"] == "red"
    assert h7["first_at_level"] == entries[5]["timestamp"]


def test_outlook_factor_points_sum_to_score():
    _, breakdowns = forecast_result(rising_inputs(), hours=48)
    for bd in breakdowns:
        total = sum(factor_points(bd).values())
        assert total == pytest.approx(bd.score, abs=0.11)        # score is rounded to 0.1
    # Capped score: points scaled down to 100
    _, storm = forecast_result(storm_inputs(), hours=6)
    capped = [bd for bd in storm if bd.score == 100.0]
    assert capped and sum(factor_points(capped[0]).values()) == pytest.approx(100.0)


def test_outlook_drivers_plain_french_sorted_top3():
    _, breakdowns = forecast_result(rising_inputs(), hours=36)
    bd = max(breakdowns, key=lambda b: b.score)
    drivers = explain_drivers(bd)
    assert 1 <= len(drivers) <= 3
    points = [d["points"] for d in drivers]
    assert points == sorted(points, reverse=True)
    assert all(d["points"] >= outlook.MIN_DRIVER_POINTS for d in drivers)
    river = next(d for d in drivers if d["factor"] == "river")
    assert river["label"] == "Niveau des rivières"
    assert "Pessac" in river["text"] and " m " in river["text"] and "," in river["text"]
    unavailable = set(bd.unavailable)
    assert not unavailable & {d["factor"] for d in drivers}


def test_outlook_calm_horizon_is_green_without_drivers():
    import dataclasses

    from app.services.risk_core import StationFeature

    calm = dataclasses.replace(
        rising_inputs(),
        stations=(StationFeature("P555001001", "Pessac", "Dordogne", 0.5, 0.0, 0.30, (3.0, 3.8, 4.5)),),
        rain_hourly={},
        soil_saturation_factor=1.0,
        westerly_wind_kmh=0.0,
        primary_levels=[],
        tide_highs=[],
    )
    result, _ = forecast_result(calm, hours=6)
    h6 = result["horizons"][0]
    assert h6["level"] == "green"
    assert h6["summary"].startswith("Risque vert")
    assert all(d["points"] >= outlook.MIN_DRIVER_POINTS for d in h6["drivers"])


def test_outlook_checklist_for_highest_level_and_act_before():
    result, _ = forecast_result(rising_inputs())
    out = build_outlook(result, PLAN, T0)
    assert out["highest_level"] == "red"
    assert out["checklist"]["level"] == "red"
    assert out["checklist"]["actions"] == PLAN["levels"]["red"]["actions"]
    h72 = next(h for h in out["horizons"] if h["hours"] == 72)
    assert out["checklist"]["act_before"] == h72["first_at_level"]


def test_outlook_green_checklist_is_empty():
    result, _ = forecast_result(rising_inputs(), hours=72)
    for h in result["horizons"]:
        h["level"] = "green"
    out = build_outlook(result, PLAN, T0)
    assert out["highest_level"] == "green"
    assert out["checklist"]["actions"] == [] and out["checklist"]["act_before"] is None


def test_outlook_next_high_tide_after_now():
    result, _ = forecast_result(rising_inputs())
    now = T0 + timedelta(hours=3)                   # first high (T0 + 2 h) already passed
    ht = next_high_tide(result["high_tides"], now)
    assert ht["timestamp"] == (T0 + timedelta(hours=2 + 12.42)).isoformat() + "+00:00"
    assert ht["coefficient"] == 95 and ht["estimated"] is False
    assert ht["hours_from_now"] == pytest.approx(11.4, abs=0.05)
    assert next_high_tide([], now) is None


def test_outlook_confidence_notes():
    result, _ = forecast_result(rising_inputs(tide_reliable=False))
    sources = {
        "river_primary": {"label": "Niveau Pessac", "status": "stale", "age_hours": 4.2, "critical": True},
        "netatmo": {"label": "Pluie Netatmo", "status": "unavailable", "critical": False,
                    "note": "aucun jeton"},
        "rain_openmeteo": {"label": "Pluie Open-Meteo", "status": "ok", "critical": False},
    }
    conf = build_outlook(result, PLAN, T0, sources)["confidence"]
    assert conf["level"] == "low"                     # critical source stale
    assert conf["tide_estimated"] is True
    assert "video" in conf["unavailable_factors"]
    assert set(conf["stale_sources"]) == {"river_primary", "netatmo"}
    text = " ".join(conf["notes"])
    assert "caméra" in text and "Marées estimées" in text
    assert "4,2 h" in text and "aucun jeton" in text
    assert conf["notes"][-1] == outlook.LONG_RANGE_NOTE


def test_outlook_confidence_good_vs_reduced():
    result, _ = forecast_result(rising_inputs())
    for h in result["horizons"]:
        h["unavailable"] = []
    assert build_outlook(result, PLAN, T0, {})["confidence"]["level"] == "good"
    result["data_sources"]["rain_past"] = {"status": "error"}
    assert build_outlook(result, PLAN, T0, {})["confidence"]["level"] == "reduced"
    result["data_sources"]["rain"] = {"status": "error"}
    assert build_outlook(result, PLAN, T0, {})["confidence"]["level"] == "low"


def test_outlook_default_action_plan_is_valid():
    plan = load_action_plan([outlook.ACTION_PLAN_PATH])
    assert set(plan["levels"]) == {"yellow", "orange", "red"}
    for entry in plan["levels"].values():
        assert entry["title"] and 3 <= len(entry["actions"]) <= 8
        assert all(isinstance(a, str) and 0 < len(a) <= 140 for a in entry["actions"])


def test_outlook_action_plan_override_and_invalid_file(tmp_path):
    override = tmp_path / "action_plan.json"
    override.write_text(json.dumps(PLAN), encoding="utf-8")
    assert load_action_plan([override, outlook.ACTION_PLAN_PATH]) == PLAN
    override.write_text('{"levels": {"red": {"actions": "not a list"}}}', encoding="utf-8")
    default = load_action_plan([outlook.ACTION_PLAN_PATH])
    assert load_action_plan([override, outlook.ACTION_PLAN_PATH]) == default
    assert load_action_plan([tmp_path / "missing.json"]) == {"levels": {}}
