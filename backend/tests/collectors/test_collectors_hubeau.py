"""Hub'Eau collector: units, regression rate helper, grandeurs, concurrent fetch."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.models.schemas import RiverLevel, Tendency
from app.services.collectors import hubeau

T = datetime(2026, 10, 25, 12, 0)


def _levels(heights_m, step_min=15, end=T, aware=False):
    n = len(heights_m)
    out = []
    for i, h in enumerate(heights_m):
        ts = end - timedelta(minutes=step_min * (n - 1 - i))
        if aware:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(RiverLevel(timestamp=ts, station_id="P555001001", station_name="Pessac",
                              river="Dordogne", height_m=h))
    return out


def test_parse_observations_tr_mm_to_m():
    payload = {"data": [
        {"date_obs": "2026-10-25T22:25:00Z", "resultat_obs": 441.0},
        {"date_obs": "2026-10-25T22:20:00Z", "resultat_obs": 440.0},
        {"date_obs": "2026-10-25T22:15:00Z", "resultat_obs": None},
    ]}
    levels = hubeau.parse_observations_tr(payload, "P555001001")
    assert [lv.height_m for lv in levels] == [0.44, 0.441]
    assert levels[0].timestamp == datetime(2026, 10, 25, 22, 20, tzinfo=timezone.utc)


def test_rate_linear_rise():
    # +2 cm every 15 min = 8 cm/h, over 3h
    heights = [3.0 + 0.02 * i for i in range(13)]
    assert hubeau.rate_cm_per_hour(_levels(heights), T) == pytest.approx(8.0)


def test_rate_ignores_points_after_t():
    heights = [3.0] * 13 + [4.0, 5.0]  # big jump after T
    levels = _levels(heights, end=T + timedelta(minutes=30))
    assert hubeau.rate_cm_per_hour(levels, T) == pytest.approx(0.0)


def test_rate_robust_to_single_spike():
    heights = [3.0] * 13
    heights[6] = 3.10  # one noisy 10 cm spike mid-window
    assert abs(hubeau.rate_cm_per_hour(_levels(heights), T)) < 0.5


def test_rate_insufficient_data():
    assert hubeau.rate_cm_per_hour([], T) == 0.0
    assert hubeau.rate_cm_per_hour(_levels([3.0, 3.5]), T) == 0.0  # 2 points over 15 min < min span
    # 3 points over 10 min < min span
    assert hubeau.rate_cm_per_hour(_levels([3.0, 3.1, 3.2], step_min=5), T) == 0.0


def test_rate_accepts_tuples_dicts_and_aware():
    tuples = [(T - timedelta(hours=2), 1.0), (T - timedelta(hours=1), 1.1), (T, 1.2)]
    assert hubeau.rate_cm_per_hour(tuples, T) == pytest.approx(10.0)
    dicts = [{"timestamp": ts.isoformat() + "Z", "height_m": h} for ts, h in tuples]
    assert hubeau.rate_cm_per_hour(dicts, T) == pytest.approx(10.0)
    aware = _levels([1.0, 1.1, 1.2], step_min=60, aware=True)
    assert hubeau.rate_cm_per_hour(aware, T.replace(tzinfo=timezone.utc)) == pytest.approx(10.0)
    assert hubeau.rate_cm_per_hour(aware, T) == pytest.approx(10.0)


def test_rate_window():
    # flat for 3h then rising the last hour: 1h window sees the rise only
    heights = [3.0] * 9 + [3.05, 3.10, 3.15, 3.20]
    levels = _levels(heights)
    assert hubeau.rate_cm_per_hour(levels, T, window_h=1) == pytest.approx(20.0)
    assert 0 < hubeau.rate_cm_per_hour(levels, T, window_h=3) < 20.0


def test_tendency_and_rate_wrappers():
    rising = _levels([3.0 + 0.02 * i for i in range(13)])  # 24 cm in 3h
    assert hubeau._calculate_tendency(rising, t=T) == Tendency.RISING
    assert hubeau._calculate_rate_of_change(rising, t=T) == 8.0
    flat = _levels([3.0] * 13)
    assert hubeau._calculate_tendency(flat, t=T) == Tendency.STABLE


async def test_daily_min_discharge_rejected():
    # No HTTP call must be made for unsupported (non-height) granularities
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=hubeau.HUBEAU_API_BASE)
        result = await hubeau.fetch_historical_data("P555001001", T - timedelta(days=3), T, granularity="daily_min")
        assert result == []
        assert route.call_count == 0


@respx.mock
async def test_fetch_all_stations_concurrent_single_client_and_cache(monkeypatch):
    monkeypatch.setattr(hubeau, "_stations_cache_by_hours", {})
    from app.config import get_settings
    n_stations = len(get_settings().hubeau_stations)

    def handler(request):
        code = request.url.params["code_entite"]
        return httpx.Response(200, json={"data": [
            {"date_obs": "2026-10-25T11:45:00Z", "resultat_obs": 1000.0, "code_station": code},
        ]})

    route = respx.get(url__startswith=f"{hubeau.HUBEAU_API_BASE}/observations_tr").mock(side_effect=handler)
    data = await hubeau.fetch_all_stations_realtime(hours_back=24)
    assert len(data) == n_stations
    assert route.call_count == n_stations
    assert all(levels[0].height_m == 1.0 for levels in data.values())

    await hubeau.fetch_all_stations_realtime(hours_back=24)
    assert route.call_count == n_stations  # cached
    await hubeau.fetch_all_stations_realtime(hours_back=48)
    assert route.call_count == 2 * n_stations  # cache keyed by hours_back
