"""v4-alpha eval inputs: parsing, Q downloads, publication lags (no leakage), climatology helpers."""

from datetime import date, datetime, timedelta

import httpx
import numpy as np
import pytest
import respx

from app.services import risk_v4 as v4
from app.services.risk_core import StationConfig
from scripts.eval import climatology, sources
from scripts.eval.dataset import (
    RawInputs,
    build_rows,
    daily_value_at,
)
from scripts.eval.sources import PoliteClient, RawCache, download_station_levels

PESSAC = StationConfig("P555001001", "Pessac", "Dordogne", 0.30, (3.0, 3.8, 4.5))
START = datetime(2024, 2, 1)
END = datetime(2024, 2, 4)


@pytest.fixture
def client():
    c = PoliteClient(min_interval_s={}, sleep=lambda s: None, retries=2)
    yield c
    c.close()


def test_evaluation_parse_discharge_piezo_and_daily():
    q = sources.parse_hydroportail({"series": {"unit": "l", "data": [{"v": 26600, "t": "2024-02-10T03:00:00Z"}]}})
    assert q == [(datetime(2024, 2, 10, 3), 26.6)]
    pz = sources.parse_hubeau_piezo({"data": [
        {"date_mesure": "2024-02-11", "niveau_nappe_eau": 7.1},
        {"date_mesure": "2024-02-10", "niveau_nappe_eau": 6.9},
        {"date_mesure": "2024-02-12", "niveau_nappe_eau": None},
    ]})
    assert pz == [(date(2024, 2, 10), 6.9), (date(2024, 2, 11), 7.1)]
    gl = sources.parse_open_meteo_daily({"daily": {"time": ["2024-02-10", "2024-02-11"],
                                                   "river_discharge": [3.5, None]}}, "river_discharge")
    assert gl == [(date(2024, 2, 10), 3.5), (date(2024, 2, 11), None)]
    assert sources.parse_hubeau_piezo(None) == [] and sources.parse_open_meteo_daily(None, "x") == []


@respx.mock
def test_evaluation_discharge_download_key_and_qixnj_fallback(tmp_path, client):
    sid = "P540401002"
    hydro = respx.get(sources.HYDROPORTAIL_URL.format(station_id=sid)).mock(side_effect=[
        httpx.Response(200, json={"series": {"unit": "l", "data": [{"v": 5000, "t": "2024-02-20T00:00:00Z"}]}}),
        httpx.Response(200, json={"series": {"data": []}}),
    ])
    hubeau = respx.get(sources.HUBEAU_ELAB_URL).mock(return_value=httpx.Response(200, json={
        "data": [{"date_obs_elab": "2024-04-02", "resultat_obs_elab": 12000}],
    }))
    cache = RawCache(tmp_path)
    raw = download_station_levels(sid, date(2024, 2, 1), date(2024, 4, 30), cache, client, variable="Q")
    assert hydro.calls[0].request.url.params["hydro_series[simpleAndInterpolatedAndHourlyVariable]"] == "Q"
    assert hubeau.calls[0].request.url.params["grandeur_hydro_elab"] == "QIXnJ"
    assert raw.points == [(datetime(2024, 2, 20), 5.0)] and raw.daily_max == [(date(2024, 4, 2), 12.0)]
    assert cache.path("hydroportail", f"{sid}_Q_2024-01-01_2024-03-31").exists()
    assert cache.path("hubeau_qixnj", f"{sid}_Q_2024-04-01_2024-06-30").exists()
    # heights keep their historical cache keys
    assert not cache.path("hydroportail", f"{sid}_2024-01-01_2024-03-31").exists()
    with pytest.raises(ValueError):
        download_station_levels(sid, date(2024, 2, 1), date(2024, 2, 2), cache, None, variable="T")


def test_evaluation_daily_value_publication_lag():
    daily = [(date(2024, 2, 1), 6.5), (date(2024, 2, 2), 7.0)]
    # day 2024-02-02 published at 2024-02-03 12:00 with a 12 h lag
    assert daily_value_at(daily, datetime(2024, 2, 3, 11), 12.0) == 6.5
    assert daily_value_at(daily, datetime(2024, 2, 3, 12), 12.0) == 7.0
    assert daily_value_at(daily, datetime(2024, 2, 3, 0), 0.0) == 7.0
    assert daily_value_at(daily, datetime(2024, 2, 2, 23), 0.0) == 6.5
    assert daily_value_at(daily, datetime(2024, 2, 1, 12), 0.0) is None
    assert daily_value_at(daily, datetime(2024, 2, 9), 0.0) is None  # too old


def _raw(shift: float = 0.0, cut: datetime | None = None) -> RawInputs:
    def bump(t, v):
        return v + shift if cut is not None and t > cut else v

    t = START - timedelta(days=2)
    pessac, eyraud, lib = [], [], []
    k = 0
    while t <= END + timedelta(days=1):
        pessac.append((t, bump(t, 3.0 + 0.01 * k)))
        eyraud.append((t, bump(t, 1.0 + 0.1 * k)))
        lib.append((t, bump(t, 4.0)))
        t += timedelta(minutes=30)
        k += 1
    days = [date(2024, 1, 28) + timedelta(days=i) for i in range(12)]
    return RawInputs(
        station_points={PESSAC.station_id: pessac},
        discharge={"eyraud": eyraud, "pessac": [(a, 300.0) for a, _ in pessac]},
        libourne_points=lib,
        piezo_daily=[(d, bump(datetime(d.year, d.month, d.day), 6.5)) for d in days],
        glofas_daily=[(d, bump(datetime(d.year, d.month, d.day), 2.0)) for d in days],
    )


def test_evaluation_hydro_values_lagged_and_causal():
    rows = build_rows(_raw(), START, END, (PESSAC,), PESSAC.station_id)
    r = next(x for x in rows if x.timestamp == datetime(2024, 2, 2, 12))
    v = r.values
    # Eyraud: last point published at t - 7h, 12.5 h max ending there
    eyraud = dict(_raw().discharge["eyraud"])
    assert v["q_eyraud_m3s"] == pytest.approx(eyraud[datetime(2024, 2, 2, 5)])
    assert v["q_eyraud_max_12h"] == pytest.approx(eyraud[datetime(2024, 2, 2, 5)])  # increasing series
    assert v["q_eyraud_lag0_m3s"] == pytest.approx(eyraud[datetime(2024, 2, 2, 12)])
    assert v["q_pessac_m3s"] == 300.0 and v["q_caudeau_m3s"] is None
    assert v["z_conf_ngf"] == pytest.approx(0.8 * (v["pessac_max_12h"] + 3.33) + 0.2 * (4.0 - 1.17))
    assert v["piezo_ngf"] == 6.5 and v["glofas_lidoire_m3s"] == 2.0

    cut = datetime(2024, 2, 2, 12)
    rows_p = build_rows(_raw(shift=5.0, cut=cut), START, END, (PESSAC,), PESSAC.station_id)
    lag_keys = ("q_eyraud_m3s", "q_eyraud_max_12h", "piezo_ngf", "glofas_lidoire_m3s")
    for a, b in zip(rows, rows_p):
        if a.timestamp <= cut:
            assert a.values == b.values, a.timestamp
        elif a.timestamp < cut + timedelta(hours=7):
            # lagged inputs still blind to the perturbation
            assert all(a.values[k] == b.values[k] for k in lag_keys), a.timestamp
    after = next(b for b in rows_p if b.timestamp == cut + timedelta(hours=8))
    before = next(a for a in rows if a.timestamp == cut + timedelta(hours=8))
    assert after.values["q_eyraud_m3s"] == pytest.approx(before.values["q_eyraud_m3s"] + 5.0)


def test_evaluation_climatology_helpers():
    assert climatology.hydro_year(date(2023, 9, 1)) == 2023 and climatology.hydro_year(date(2024, 8, 31)) == 2023
    daily = [(date(2020, 9, 1) + timedelta(days=i), float(i % 400)) for i in range(800)]
    maxima = climatology.hydro_year_maxima(daily, min_days=300)
    assert set(maxima) == {2020, 2021}
    assert climatology.hydro_year_maxima(daily[:100], min_days=300) == {}
    assert climatology.t_year_value([1.0, 2.0, 3.0, 4.0, 5.0], 2) == pytest.approx(3.0)
    assert climatology.t_year_value([1.0, 2.0, 3.0, 4.0, 5.0], 5) == pytest.approx(4.2)
    ref = np.arange(100, dtype=float)
    tgt = 2 * ref + 1
    tgt[3] = np.nan
    th = climatology.quantile_mapped_thresholds(ref, tgt, (50.0, 90.0))
    assert th[0] == pytest.approx(101.0, abs=2.5) and th[1] == pytest.approx(181.0, abs=2.5)
    res = climatology.compute({"eyraud_qixnj": daily, "piezo": [(date(2020, 1, 1), 6.0), (date(2020, 1, 2), 7.0)]})
    assert res["eyraud_q2_m3s"] == pytest.approx(np.median(list(maxima.values())))
    assert res["pessac_t_year_levels_m"] is None and res["piezo_percentiles_ngf"][0] == pytest.approx(6.5)


def test_evaluation_v4_climatology_matches_constants_when_cached():
    if not sources.RawCache().path("hubeau_elab_clim", "P540401002_QIXnJ_2009-12-01_2022-09-30").exists():
        pytest.skip("climatology cache not present (run python -m scripts.eval.climatology)")
    res = climatology.compute(climatology.download_series(client=None))
    assert res["eyraud_q2_m3s"] == pytest.approx(v4.EYRAUD_Q2_M3S, abs=0.01)
    assert res["caudeau_q2_m3s"] == pytest.approx(v4.CAUDEAU_Q2_M3S, abs=0.01)
    assert res["glofas_q2_m3s"] == pytest.approx(v4.GLOFAS_Q2_M3S, abs=0.01)
    assert res["pessac_t_year_levels_m"] == pytest.approx(v4.PESSAC_T_YEAR_LEVELS_M, abs=0.01)
    assert res["piezo_percentiles_ngf"] == pytest.approx(v4.PIEZO_PERCENTILES_NGF, abs=0.01)
