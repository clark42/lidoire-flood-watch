"""Indicator collectors: parsing of recorded payloads and InfluxDB points (no network)."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.config import get_settings
from app.services.collectors import hydro_indicators as hi
from app.services.influx_client import point_as_dict

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def station(key: str) -> dict:
    return next(s for s in get_settings().indicator_flow_stations if s["key"] == key)


def test_parse_hubeau_tr_flow_l_per_s_to_m3s():
    rows = hi.parse_hubeau_tr(load("hubeau_tr_q_eyraud.json"))
    # null value dropped, sorted ascending, naive UTC
    assert rows == [
        (datetime(2026, 10, 26, 12, 35), pytest.approx(0.071)),
        (datetime(2026, 10, 26, 12, 40), pytest.approx(0.070)),
        (datetime(2026, 10, 26, 12, 45), pytest.approx(0.069)),
    ]
    assert all(ts.tzinfo is None for ts, _ in rows)


def test_parse_hubeau_tr_height_mm_to_m():
    rows = hi.parse_hubeau_tr(load("hubeau_tr_h_eyraud.json"))
    assert [v for _, v in rows] == [pytest.approx(0.234), pytest.approx(0.233), pytest.approx(0.232)]


def test_parse_hydroportail_flow_sorted():
    rows = hi.parse_hydroportail(load("hydroportail_q_eyraud.json"))
    assert rows[0] == (datetime(2024, 2, 20, 0, 10), pytest.approx(0.94))
    assert rows[-1] == (datetime(2024, 2, 25, 15, 30), pytest.approx(20.5))
    assert [ts for ts, _ in rows] == sorted(ts for ts, _ in rows)


def test_parse_obs_elab_daily_mean():
    rows = hi.parse_obs_elab(load("obs_elab_qmnj_eyraud.json"))
    assert rows[1] == (datetime(2024, 2, 26), pytest.approx(23.064))
    assert len(rows) == 3


def test_parse_piezo_tr_and_daily():
    tr = hi.parse_piezo_tr(load("piezo_tr_mouliets.json"))
    assert tr[-1] == (datetime(2026, 10, 26, 7, 0), pytest.approx(5.836), pytest.approx(2.224))
    assert [ts.hour for ts, _, _ in tr] == [5, 6, 7]
    daily = hi.parse_piezo_daily(load("piezo_chroniques_mouliets.json"))
    # Record of the alluvial piezometer, 2026-02-12 flood
    assert daily[1] == (datetime(2026, 2, 12), pytest.approx(7.47), daily[1][2])
    assert max(level for _, level, _ in daily) == pytest.approx(7.47)


def test_parse_shom_resamples_to_10_min_means():
    rows = hi.parse_shom(load("shom_bordeaux.json"))
    assert rows == [
        (datetime(2026, 10, 26, 0, 0), pytest.approx(0.651)),
        (datetime(2026, 10, 26, 0, 10), pytest.approx(0.565)),
        (datetime(2026, 10, 26, 0, 20), pytest.approx(0.5)),
    ]
    raw = hi.parse_shom(load("shom_bordeaux.json"), resample_minutes=0)
    assert len(raw) == 25


@pytest.mark.parametrize("payload", [None, {}, {"data": None}, {"data": [{"date_obs": "bad", "resultat_obs": 1}]}])
def test_parsers_tolerate_empty_or_bad_payloads(payload):
    assert hi.parse_hubeau_tr(payload) == []
    assert hi.parse_piezo_tr(payload) == []
    assert hi.parse_shom(payload) == []


def test_flow_points_merge_q_and_h():
    eyraud = station("EYRAUD")
    q = hi.parse_hubeau_tr(load("hubeau_tr_q_eyraud.json"))
    h = hi.parse_hubeau_tr(load("hubeau_tr_h_eyraud.json"))
    points = [point_as_dict(p) for p in hi.flow_points(eyraud, hi.SOURCE_HUBEAU_TR, q, h)]
    assert len(points) == 3
    first = points[0]
    assert first["measurement"] == "river_flow"
    assert first["tags"] == {"key": "EYRAUD", "station_id": "P540401002", "river": "Eyraud", "source": "hubeau-tr"}
    assert first["fields"] == {"q_m3s": pytest.approx(0.071), "h_m": pytest.approx(0.234)}
    # Q only (no H collected)
    pessac = station("PESSAC")
    only_q = point_as_dict(hi.flow_points(pessac, hi.SOURCE_HUBEAU_TR, q)[0])
    assert set(only_q["fields"]) == {"q_m3s"}
    assert only_q["tags"]["key"] == "PESSAC"


def test_groundwater_and_tide_points():
    mouliets = get_settings().indicator_groundwater_stations[0]
    p = point_as_dict(hi.make_groundwater_point(mouliets, datetime(2026, 2, 12), hi.SOURCE_ADES_DAILY, 7.47, 0.59))
    assert p["measurement"] == "groundwater"
    assert p["tags"] == {"key": "MOULIETS", "code_bss": "08048X0088/P", "source": "ades-daily"}
    assert p["fields"] == {"level_m_ngf": 7.47, "depth_m": 0.59}

    bordeaux = next(g for g in get_settings().indicator_tide_gauges if g["key"] == "BORDEAUX")
    t = point_as_dict(hi.make_tide_observed_point(bordeaux, datetime(2026, 10, 26), hi.SOURCE_SHOM, 5.38))
    assert t["measurement"] == "tide_observed"
    assert t["tags"] == {"key": "BORDEAUX", "source": "shom-refmar"}
    assert t["fields"] == {"height_m": 5.38}


def test_measurements_are_new_and_distinct_from_model_inputs():
    """The model reads river_level / tide / rainfall: indicators never write there."""
    assert {hi.FLOW_MEASUREMENT, hi.GROUNDWATER_MEASUREMENT, hi.TIDE_OBSERVED_MEASUREMENT} == {
        "river_flow", "groundwater", "tide_observed",
    }
