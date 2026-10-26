"""Synthetic forecasts and an in-memory run store for outlook / archive tests (no network, no database)."""

from datetime import datetime, timedelta

from app.services.forecast import ForecastInputs, build_forecast, forecast_inputs_summary, upcoming_high_tides
from app.services.outlook import summarize_horizons
from app.services.risk_core import MODEL_VERSION, StationFeature

T0 = datetime(2024, 2, 10, 12, 0)
PESSAC = "P555001001"


def storm_inputs(t0: datetime = T0, rain_mm_h: float = 5.0, rain_hours: int = 12,
                 tide_reliable: bool = True) -> ForecastInputs:
    """Pessac in yellow and rising, heavy rain after t0, high tides every 12.4 h."""
    stations = (
        StationFeature(PESSAC, "Pessac", "Dordogne", 3.3, 3.0, 0.30, (3.0, 3.8, 4.5)),
        StationFeature("P726151001", "Abzac", "Isle", 2.4, 2.0, 0.25, (2.2, 3.0, 3.5)),
        StationFeature("P514001001", "Bergerac", "Dordogne", 2.0, 0.0, 0.15, (2.5, 3.5, 4.5)),
    )
    rain = {t0 - timedelta(hours=k): 0.5 for k in range(168)}
    rain.update({t0 + timedelta(hours=k): rain_mm_h for k in range(1, rain_hours + 1)})
    highs = [(t0 + timedelta(hours=2 + 12.42 * i), 95) for i in range(-1, 16)]
    return ForecastInputs(
        t0=t0,
        stations=stations,
        primary_station_id=PESSAC,
        station_delays={PESSAC: 4.0, "P726151001": 2.0, "P514001001": 8.0},
        rain_hourly=rain,
        tide_highs=highs,
        tide_reliable=tide_reliable,
        soil_saturation_factor=1.3,
        westerly_wind_kmh=20.0,
        primary_levels=[(t0 - timedelta(hours=k), 3.3 - 0.03 * k) for k in range(13)],
    )


def rising_inputs(t0: datetime = T0, tide_reliable: bool = True) -> ForecastInputs:
    """Yellow now, rain from H+10 to H+21: orange then red later (6 h < 24 h < 72 h)."""
    stations = (
        StationFeature(PESSAC, "Pessac", "Dordogne", 2.6, 0.0, 0.30, (3.0, 3.8, 4.5)),
        StationFeature("P726151001", "Abzac", "Isle", 1.8, 0.0, 0.25, (2.2, 3.0, 3.5)),
        StationFeature("P514001001", "Bergerac", "Dordogne", 2.0, 0.0, 0.15, (2.5, 3.5, 4.5)),
    )
    rain = {t0 - timedelta(hours=k): 0.05 for k in range(168)}
    rain.update({t0 + timedelta(hours=k): 4.0 for k in range(10, 22)})
    base = storm_inputs(t0, tide_reliable=tide_reliable)
    return ForecastInputs(
        t0=t0,
        stations=stations,
        primary_station_id=PESSAC,
        station_delays=base.station_delays,
        rain_hourly=rain,
        tide_highs=base.tide_highs,
        tide_reliable=tide_reliable,
        soil_saturation_factor=1.3,
        westerly_wind_kmh=20.0,
        primary_levels=[(t0 - timedelta(hours=k), 2.6) for k in range(13)],
    )


def forecast_result(fi: ForecastInputs, hours: int = 168, data_sources=None) -> dict:
    """Same shape as forecast.get_risk_forecast_v2 (minus the summary fields)."""
    breakdowns: list = []
    forecasts = build_forecast(fi, hours, breakdowns)
    return {
        "generated_at": fi.t0.isoformat() + "+00:00",
        "hours_ahead": hours,
        "forecasts": forecasts,
        "data_sources": data_sources or {"river": {"status": "ok"}, "rain": {"status": "ok"}},
        "model_version": MODEL_VERSION,
        "horizons": summarize_horizons(forecasts, breakdowns),
        "high_tides": upcoming_high_tides(fi, 85),
        "inputs_summary": forecast_inputs_summary(fi, forecasts, breakdowns),
    }, breakdowns


class MemoryRunStore:
    def __init__(self, fail: bool = False):
        self.rows: list[dict] = []
        self.fail = fail

    async def save(self, row: dict) -> bool:
        if self.fail:
            raise RuntimeError("postgres down")
        if any(r["run_at"] == row["run_at"] and r["model_version"] == row["model_version"] for r in self.rows):
            return False
        self.rows.append(row)
        return True

    async def list_runs(self, start, end, model_version):
        return [r for r in self.rows if start <= r["run_at"] <= end and r["model_version"] == model_version]
