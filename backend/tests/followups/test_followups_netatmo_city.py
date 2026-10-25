"""Netatmo: city tags canonicalised to the config spelling at write time."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.collectors import netatmo

NOW = datetime(2026, 10, 25, 14, 5)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(netatmo, "_last_hourly_write", {})
    monkeypatch.setattr(netatmo, "_history_cache", {})
    monkeypatch.setattr(netatmo, "_utcnow", lambda: NOW)


@pytest.mark.parametrize("raw,expected", [
    ("Saint-Rémy", "Saint-Rémy"),
    ("Saint-Remy", "Saint-Rémy"),
    ("saint remy", "Saint-Rémy"),
    ("LES SALLES DE CASTILLON", "Les Salles-de-Castillon"),
    ("Flaujagues", "Flaujagues"),
    ("Bergerac", "Bergerac"),  # not a Lidoire station: unchanged
    (None, None),
    ("", ""),
])
def test_followups_canonical_city(raw, expected):
    assert netatmo.canonical_city(raw) == expected


def test_followups_canonical_city_alias_map(monkeypatch):
    monkeypatch.setitem(netatmo.NETATMO_CITY_ALIASES, "St-Antoine", "Saint-Antoine-de-Breuilh")
    assert netatmo.canonical_city("st antoine") == "Saint-Antoine-de-Breuilh"


async def test_followups_live_store_uses_config_spelling_in_one_batch(batch_writes, forbid_single_writes):
    basin = {
        "rain_1h_avg_mm": 1.0,
        "rain_24h_avg_mm": 4.0,
        "lidoire_stations": [
            {"city": "Saint-Remy", "rain_1h_mm": 2.0, "rain_24h_mm": 8.0, "lat": 44.95, "lon": 0.18},
            {"city": "Castillon", "config_city": "Castillon-la-Bataille",
             "rain_1h_mm": 0.5, "rain_24h_mm": 3.0},
        ],
        "stations": [{"city": "Bergerac", "rain_1h_mm": 0.0, "rain_24h_mm": 1.0}],
    }
    stored = await netatmo.store_rainfall_to_influx(basin)
    assert len(batch_writes) == 1
    points = batch_writes[0]
    assert stored == len(points) == 8  # 4 series x (snapshot + hourly)
    lidoire = {p["tags"]["city"] for p in points if p["tags"]["source"] == "netatmo-lidoire"}
    assert lidoire == {"Saint-Rémy", "Castillon-la-Bataille"}
    others = {p["tags"]["city"] for p in points if p["tags"]["source"] == "netatmo"}
    assert others == {"basin_average", "Bergerac"}
    # hourly slots are marked written only after the batch succeeded
    assert netatmo._last_hourly_write[("netatmo-lidoire", "Saint-Rémy")] == NOW.replace(minute=0)


async def test_followups_failed_batch_does_not_mark_hourly_slot(monkeypatch):
    from app.services import influx_client

    async def failing(points, chunk_size=5000):
        return 0

    monkeypatch.setattr(influx_client, "write_points_batch", failing)
    await netatmo.store_rainfall_to_influx({"lidoire_stations": [], "stations": []})
    assert netatmo._last_hourly_write == {}


async def test_followups_historical_backfill_uses_config_spelling(monkeypatch, batch_writes, forbid_single_writes):
    async def fake_devices():
        return {
            "Saint-Remy": {"device_id": "d1", "rain_module_id": "m1", "city": "Saint-Remy",
                           "config_city": "Saint-Rémy", "lat": 44.95, "lon": 0.18},
            # entry cached before config_city existed: folded to the config name
            "LAMOTHE MONTRAVEL": {"device_id": "d2", "rain_module_id": "m2", "city": "LAMOTHE MONTRAVEL"},
        }

    async def fake_history(device_id, module_id, start, end, scale="1hour"):
        return [{"timestamp": NOW - timedelta(hours=h), "rain_mm": 0.2 * h} for h in (1, 2)]

    monkeypatch.setattr(netatmo, "_discover_lidoire_device_ids", fake_devices)
    monkeypatch.setattr(netatmo, "fetch_netatmo_station_history", fake_history)

    total = await netatmo.fetch_and_store_netatmo_historical_range(NOW - timedelta(days=1), NOW)
    assert total == 4
    assert len(batch_writes) == 2  # one batched write per station
    cities = [{p["tags"]["city"] for p in batch} for batch in batch_writes]
    assert cities == [{"Saint-Rémy"}, {"Lamothe-Montravel"}]
    assert all("precipitation_mm_h" in p["fields"] for batch in batch_writes for p in batch)


async def test_followups_basin_reads_history_under_canonical_and_legacy_names(monkeypatch):
    station = {
        "station_id": "70:ee:50:00:00:01", "rain_module_id": "05:00:00:00:00:01",
        "lat": 44.9479, "lon": 0.1847, "distance_km": 20.0, "city": "Saint-Remy",
        "rain": {"rain_60min_mm": 1.0, "rain_24h_mm": 6.0},
    }

    async def fake_stations(lat, lon, radius_km=75.0):
        return [dict(station)]

    seen = {}

    async def fake_history(cities, aliases=None):
        seen["cities"], seen["aliases"] = cities, aliases
        hist = {"Saint-Rémy": {"rain_24h_mm": 6.0, "rain_72h_mm": 15.0, "rain_7d_mm": 30.0}}
        return hist, {"Saint-Rémy": 7}, None

    monkeypatch.setattr(netatmo, "fetch_public_stations_data", fake_stations)
    monkeypatch.setattr(netatmo, "_get_lidoire_history", fake_history)

    basin = await netatmo.get_basin_rainfall()
    assert seen == {"cities": ["Saint-Rémy"], "aliases": {"Saint-Rémy": ["Saint-Remy"]}}
    lidoire = basin["lidoire_stations"][0]
    assert lidoire["city"] == "Saint-Remy"  # display keeps the Netatmo name
    assert lidoire["config_city"] == "Saint-Rémy"
    assert (lidoire["rain_72h_mm"], lidoire["rain_7d_mm"]) == (15.0, 30.0)


async def test_followups_coverage_counts_alias_samples(fake_query):
    _, set_tables = fake_query

    def row(city, hours_ago):
        return {"city": city, "_time": (NOW - timedelta(hours=hours_ago)).replace(tzinfo=timezone.utc)}

    # day 0 under the canonical tag, days 1-2 under the legacy raw tag
    set_tables([[row("Saint-Rémy", 0), row("Saint-Remy", 24), row("Saint-Remy", 48), row("Other", 72)]])
    coverage = await netatmo._query_daily_coverage(
        ["Saint-Rémy"], aliases={"Saint-Rémy": ["Saint-Remy"]},
    )
    assert coverage == {"Saint-Rémy": 3}
