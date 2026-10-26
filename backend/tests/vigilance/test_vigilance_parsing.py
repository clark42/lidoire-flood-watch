"""Parsing of the recorded Vigicrues and Meteo-France vigilance payloads.

Fixtures (trimmed copies of live answers, 2026-10-26 checks):
- vigicrues_infovigicru_20261026.geojson: InfoVigiCru, geometries removed;
- meteofrance_20261026_150006.json: green map, departements 24/33/40;
- meteofrance_20260212_070229.json: flood of February 2026 (crues rouge
  in 33, orange in 24; yellow vent / pluie-inondation / orages slots);
- meteofrance_listing_20261026.html: day index of the data.gouv mirror.
"""

import copy
from datetime import datetime, timedelta

from vigilance_fixtures import load_fixture

from app.config import Settings
from app.services.collectors import vigilance as v

SETTINGS = Settings(_env_file=None)
TRONCONS = SETTINGS.vigilance_vigicrues_troncons
DEPTS = SETTINGS.vigilance_meteofrance_departments
PHENOMENA = SETTINGS.vigilance_meteofrance_phenomena
NOW = datetime(2026, 10, 26, 21, 0)


def _phen(dept: dict, key: str) -> dict:
    return next(p for p in dept["phenomena"] if p["key"] == key)


def test_vigilance_vigicrues_troncons_green():
    result = v.parse_vigicrues(load_fixture("vigicrues_infovigicru_20261026.geojson"), TRONCONS, NOW)
    assert result["available"] is True
    assert result["issued_at"] == datetime(2026, 10, 26, 14, 56, 47)
    assert [(t["code"], t["name"], t["level"], t["color"]) for t in result["troncons"]] == [
        ("DO5", "Dordogne aval", 1, "vert"),
        ("DO22", "Confluence Garonne - Dordogne", 1, "vert"),
    ]
    assert result["troncons"][0]["url"] == "https://www.vigicrues.gouv.fr/territoire/32"
    assert result["troncons"][0]["station_url"] == "https://www.vigicrues.gouv.fr/station/P555001001"


def test_vigilance_vigicrues_orange_and_missing_troncon():
    data = copy.deepcopy(load_fixture("vigicrues_infovigicru_20261026.geojson"))
    for f in data["features"]:
        if f["properties"]["CdEntCru"] == "DO5":
            f["properties"]["NivInfViCr"] = 3
    data["features"] = [f for f in data["features"] if f["properties"]["CdEntCru"] != "DO22"]
    result = v.parse_vigicrues(data, TRONCONS, NOW)
    do5, do22 = result["troncons"]
    assert (do5["level"], do5["color"]) == (3, "orange")
    assert do22["level"] is None and do22["color"] is None
    assert do22["name"] == "Confluence Garonne - Dordogne"  # configured name kept
    assert result["available"] is True


def test_vigilance_vigicrues_invalid_levels_are_unknown():
    data = {"features": [
        {"properties": {"CdEntCru": "DO5", "NivInfViCr": 7}},
        {"properties": {"CdEntCru": "DO22", "NivInfViCr": "x"}},
        {"properties": None},
    ]}
    result = v.parse_vigicrues(data, TRONCONS, NOW)
    assert [t["level"] for t in result["troncons"]] == [None, None]
    assert result["available"] is False
    assert v.parse_vigicrues({}, TRONCONS, None)["available"] is False


def test_vigilance_meteofrance_green_map():
    result = v.parse_meteofrance(load_fixture("meteofrance_20261026_150006.json"), DEPTS, PHENOMENA, NOW)
    assert result["available"] is True
    assert result["issued_at"] == datetime(2026, 10, 26, 15, 0, 9)
    assert result["valid_from"] == datetime(2026, 10, 26, 15, 0)
    assert result["valid_to"] == datetime(2026, 10, 27, 23, 0)
    assert [d["code"] for d in result["departments"]] == ["33", "24"]
    gironde = result["departments"][0]
    assert gironde["url"] == "https://vigilance.meteofrance.fr/fr/gironde"
    assert gironde["max_level"] == 1
    assert [p["key"] for p in gironde["phenomena"]] == ["pluie-inondation", "crues", "orages", "vent"]
    assert all(p["level"] == 1 and p["color"] == "vert" and p["windows"] == [] for p in gironde["phenomena"])


def test_vigilance_meteofrance_flood_february_2026():
    result = v.parse_meteofrance(load_fixture("meteofrance_20260212_070229.json"), DEPTS, PHENOMENA, NOW)
    gironde, dordogne = result["departments"]
    assert gironde["max_level"] == 4 and dordogne["max_level"] == 3

    crues = _phen(gironde, "crues")
    assert (crues["level"], crues["color"], crues["level_tomorrow"]) == (4, "rouge", 4)
    # No time slot: the colour covers J and J1, merged into one window
    assert crues["windows"] == [
        {"begin": datetime(2026, 2, 12, 7, 0), "end": datetime(2026, 2, 13, 23, 0), "level": 4},
    ]
    assert _phen(dordogne, "crues")["level"] == 3

    rain = _phen(gironde, "pluie-inondation")
    assert (rain["level"], rain["color"], rain["level_tomorrow"]) == (2, "jaune", 1)
    assert rain["windows"] == [
        {"begin": datetime(2026, 2, 12, 7, 0), "end": datetime(2026, 2, 12, 22, 0), "level": 2},
    ]
    storms = _phen(gironde, "orages")
    assert storms["windows"] == [
        {"begin": datetime(2026, 2, 12, 12, 0), "end": datetime(2026, 2, 12, 17, 0), "level": 2},
    ]


def test_vigilance_meteofrance_unknown_departement_and_empty_payload():
    result = v.parse_meteofrance(
        load_fixture("meteofrance_20261026_150006.json"),
        [{"code": "2A", "name": "Corse-du-Sud", "slug": "corse-du-sud"}], ["2"], NOW,
    )
    assert result["available"] is False
    assert result["departments"][0]["phenomena"][0]["level"] is None
    empty = v.parse_meteofrance({}, DEPTS, PHENOMENA, None)
    assert empty["available"] is False and len(empty["departments"]) == 2


def test_vigilance_run_directories_newest_first():
    assert v.parse_run_directories(load_fixture("meteofrance_listing_20261026.html")) == ["150006", "050006"]
    assert v.parse_run_directories("") == []


def test_vigilance_current_levels_and_change_detection():
    vigicrues = v.parse_vigicrues(load_fixture("vigicrues_infovigicru_20261026.geojson"), TRONCONS, NOW)
    meteofrance = v.parse_meteofrance(load_fixture("meteofrance_20260212_070229.json"), DEPTS, PHENOMENA, NOW)
    levels = v.current_levels(vigicrues, meteofrance)
    assert levels[("vigicrues", "DO5", "crues")] == 1
    assert levels[("meteofrance", "33", "crues")] == 4
    assert levels[("meteofrance", "24", "pluie-inondation")] == 2
    assert len(levels) == 2 + 2 * 4

    # Nothing stored yet: everything is written
    assert set(v.points_to_write(levels, {}, NOW)) == set(levels)
    stored = {k: (lvl, NOW) for k, lvl in levels.items()}
    assert v.points_to_write(levels, stored, NOW + timedelta(minutes=30)) == []
    changed = {**levels, ("vigicrues", "DO5", "crues"): 2}
    assert v.points_to_write(changed, stored, NOW + timedelta(minutes=30)) == [("vigicrues", "DO5", "crues")]
    # Keepalive
    assert set(v.points_to_write(levels, stored, NOW + v.KEEPALIVE)) == set(levels)
    # Unavailable sources contribute nothing
    assert v.current_levels(None, {"available": False}) == {}
