"""Hourly evaluation dataset for the mill, rebuilt from cached public archives.

Every row holds, at hour ``t`` (naive UTC):

- ``features``: the :class:`risk_core.RiskFeatures` built with the production
  historical builder (:func:`risk_core.build_historical_features`, same as
  the backfill/TimeMachine), i.e. the exact input of ``compute_risk``;
- ``values``: flat causal features for candidate models (station heights and
  rates, rolling Pessac level, rain windows, tide, and the hydrological
  inputs of the v4-alpha candidates: discharge of the Eyraud / Caudeau /
  Pessac, Libourne level and confluence stage, Mouliets groundwater level,
  GloFAS Lidoire discharge).

Real-world latencies (research 2026-10-26, hydro report) are applied as
lags, so a value is only used once it would have been published:

- Eyraud and Caudeau Q: Hub'Eau/Vigicrues publish them ~7 h late outside
  crises -> points with timestamp <= t - 7 h only (``HYDRO_Q_SERIES``);
- Pessac Q and Libourne H: ~10-20 min -> points <= t;
- Mouliets piezometer (daily): day D used from D+1 12:00 UTC (real-time
  feed ~12 h late);
- GloFAS (daily): day D used from D+1 00:00 UTC (the end of day D under
  either date convention; production would read the forecast day 0).

No future leakage (default ``level_mode="causal"``): observed series only
contribute points with timestamp <= t (river levels, rain buckets ending at
or before t). Tides are astronomical predictions, so the nearest high tide
may be in the future (as in production). Two opt-in sensitivity modes are
flagged in ``meta["leakage"]``:

- ``level_mode="interp"`` linearly interpolates the variable-step archive on
  a 15-min grid; the value at t then depends on the NEXT archived point
  (a reconstruction of what a 5-min real-time sensor read at t, since the
  Banque Hydro stores compressed series meant to be interpolated);
- ``forecast_rain="oracle"`` feeds the OBSERVED next 24h/72h rain as the
  "forecast" (perfect forecast, upper bound of the production forecast
  blend). Archived forecasts are not available for the whole period
  (Open-Meteo previous-runs API starts in 2024).

Deviations from the live realtime score (documented in ``meta``): soil
saturation, westerly wind and video are not archived (unavailable inputs,
excluded from the denominator, as in the production backfill); rain is the
Open-Meteo basin mean (no Netatmo/Infoclimat archive); tide coefficients are
estimated from the model sea-level range. Tide times get the production phase
lag (``settings.tide_phase_lag_hours``, overridable by ``tide_lag_hours``).
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Iterator, Mapping, Optional, Sequence

import numpy as np

from app.config import get_settings
from app.services import risk_core
from app.services.risk_core import (
    LevelPoint,
    RiskFeatures,
    StationConfig,
    StationFeature,
)

logger = logging.getLogger(__name__)

# Default evaluation period (task: 2022-11-01 -> 2026-10-25)
DEFAULT_START = date(2022, 11, 1)
DEFAULT_END = date(2026, 10, 25)

# Variable-step archives have multi-hour gaps when the level is flat; the
# last archived value is held for up to this age (points <= t only).
CAUSAL_MAX_AGE_HOURS = 12.0
# interp mode: interpolate only across gaps up to this length
INTERP_MAX_GAP_HOURS = 12.0
INTERP_STEP_MINUTES = 15
# Daily-max fallback: the max of day D is known at D+1 00:00 UTC
DAILY_MAX_AGE_DAYS = 2

ROLLING_WINDOW_H = 12.5
RAIN_WINDOWS_H = (1, 3, 6, 12, 24, 48, 72, 168)

# v4-alpha hydrological inputs: name -> (Hub'Eau station, publication lag h)
HYDRO_Q_SERIES = {
    "eyraud": ("P540401002", 7.0),  # bras de l'Eyraud a La Force, 73.5 km2
    "caudeau": ("P524401001", 7.0),  # Caudeau a Lembras, 253 km2
    "pessac": ("P555001001", 0.0),  # Dordogne a Pessac-sur-Dordogne
}
# series also exposed without the publication lag (flagged sensitivity only)
LAG0_SENSITIVITY = ("eyraud",)
LIBOURNE_STATION_ID = "P577001001"
PIEZO_BSS = "08048X0088/P"  # Mouliets-et-Villemartin, Dordogne alluvium, 5.9 km
PIEZO_LAG_AFTER_DAY_H = 12.0
GLOFAS_CELL = (44.875, -0.025)  # Lidoire cell of the GloFAS v4 0.05 deg grid
GLOFAS_LAG_AFTER_DAY_H = 0.0
# Daily values older than this (days) are not used
DAILY_INPUT_MAX_AGE_DAYS = 3


@dataclass(frozen=True)
class FeatureRow:
    """Inputs available at one hour (everything is causal unless flagged)."""

    timestamp: datetime
    features: RiskFeatures
    values: Mapping[str, Optional[float]]


@dataclass
class RawInputs:
    """Parsed raw series (independent of where they came from)."""

    # Variable-step level points per station (naive UTC, m), sorted
    station_points: dict[str, list[LevelPoint]] = field(default_factory=dict)
    # Daily maxima per station where the variable-step series is missing
    station_daily_max: dict[str, list[tuple[date, float]]] = field(default_factory=dict)
    # Hourly rain per basin point: {hour label (end of hour): mm}
    rain_by_point: dict[str, dict[datetime, float]] = field(default_factory=dict)
    # Hourly model sea level (naive UTC, m or None)
    sea_level: list[tuple[datetime, Optional[float]]] = field(default_factory=list)
    # v4-alpha: variable-step discharge per HYDRO_Q_SERIES name (naive UTC, m3/s)
    discharge: dict[str, list[LevelPoint]] = field(default_factory=dict)
    # v4-alpha: Libourne variable-step level (scale m), tidal, not scored by v3.4
    libourne_points: list[LevelPoint] = field(default_factory=list)
    # v4-alpha: daily groundwater level (m NGF) and GloFAS discharge (m3/s)
    piezo_daily: list[tuple[date, float]] = field(default_factory=list)
    glofas_daily: list[tuple[date, float]] = field(default_factory=list)
    # v4-beta: ERA5 hourly rain per basin point (observed-rain stand-in of the nowcast)
    rain_era5_by_point: dict[str, dict[datetime, float]] = field(default_factory=dict)
    # v4-beta: archived forecasts (Previous Runs API), {"day1"|"day2": {point: {label: mm}}}
    rain_prev_runs: dict[str, dict[str, dict[datetime, float]]] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildOptions:
    level_mode: str = "causal"  # "causal" | "interp"
    forecast_rain: str = "none"  # "none" | "oracle"
    use_tide: bool = True
    tide_lag_hours: Optional[float] = None  # default: production Open-Meteo lag
    max_age_hours: Optional[float] = None  # default depends on level_mode
    rate_window_h: Optional[float] = None  # default: settings (3h)

    def resolved_max_age(self, settings) -> float:
        if self.max_age_hours is not None:
            return self.max_age_hours
        if self.level_mode == "interp":
            return settings.risk_level_max_age_hours
        return CAUSAL_MAX_AGE_HOURS

    def resolved_tide_lag(self) -> float:
        if self.tide_lag_hours is not None:
            return self.tide_lag_hours
        from app.services.collectors.tide import SOURCE_OPEN_METEO, tide_phase_lag_hours

        return tide_phase_lag_hours(SOURCE_OPEN_METEO)

    def resolved_rate_window(self, settings) -> float:
        return self.rate_window_h if self.rate_window_h is not None else settings.risk_rate_window_hours


@dataclass
class EvalDataset:
    """Hourly rows over [start, end] plus provenance metadata."""

    rows: list[FeatureRow]
    station_configs: tuple[StationConfig, ...]
    primary_station_id: str
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[FeatureRow]:
        return iter(self.rows)

    @property
    def times(self) -> list[datetime]:
        return [r.timestamp for r in self.rows]

    def index_of(self, t: datetime) -> Optional[int]:
        times = self.times
        i = bisect.bisect_left(times, t)
        return i if i < len(times) and times[i] == t else None

    def hour_index(self, t: datetime) -> Optional[int]:
        """Index of the row of the hour containing ``t`` (label peaks may carry minutes)."""
        return self.index_of(risk_core.floor_hour(t))

    def subset(self, start: datetime, end: datetime) -> "EvalDataset":
        rows = [r for r in self.rows if start <= r.timestamp <= end]
        return EvalDataset(rows, self.station_configs, self.primary_station_id, dict(self.meta))


# ---------------------------------------------------------------------------
# Level preprocessing
# ---------------------------------------------------------------------------

def interpolate_points(
    points: Sequence[LevelPoint],
    step_minutes: int = INTERP_STEP_MINUTES,
    max_gap_hours: float = INTERP_MAX_GAP_HOURS,
) -> list[LevelPoint]:
    """Linear interpolation of a variable-step series on a regular grid.

    LEAKS the next archived point into values between two points: only for
    the flagged ``interp`` sensitivity mode.
    """
    if len(points) < 2:
        return list(points)
    step = timedelta(minutes=step_minutes)
    out: list[LevelPoint] = []
    for (t0, h0), (t1, h1) in zip(points, points[1:]):
        out.append((t0, h0))
        gap = (t1 - t0).total_seconds()
        if gap <= step.total_seconds() or gap > max_gap_hours * 3600:
            continue
        # grid times strictly between t0 and t1
        g = risk_core.floor_hour(t0)
        while g <= t0:
            g += step
        while g < t1:
            frac = (g - t0).total_seconds() / gap
            out.append((g, h0 + (h1 - h0) * frac))
            g += step
    out.append(points[-1])
    return out


def daily_station_feature(
    cfg: StationConfig,
    daily: Sequence[tuple[date, float]],
    t: datetime,
) -> Optional[StationFeature]:
    """Degraded station feature from daily maxima (no leakage).

    At t, only days D whose max is complete (D+1 00:00 <= t) are used: the
    height is the last complete day's max, the rate the day-to-day change
    of the maxima spread over 24h. Older than DAILY_MAX_AGE_DAYS -> None.
    """
    if not daily:
        return None
    last_day = (t - timedelta(days=1)).date()  # last complete day at t
    days = [d for d, _ in daily]
    i = bisect.bisect_right(days, last_day)
    if i == 0:
        return None
    d1, h1 = daily[i - 1]
    if (last_day - d1).days > DAILY_MAX_AGE_DAYS:
        return None
    rate = 0.0
    if i >= 2:
        d0, h0 = daily[i - 2]
        span_h = (d1 - d0).days * 24
        if 0 < span_h <= 48:
            rate = (h1 - h0) * 100.0 / span_h
    return StationFeature(
        station_id=cfg.station_id,
        station_name=cfg.station_name,
        river=cfg.river,
        height_m=h1,
        rate_cm_h=rate,
        weight=cfg.weight,
        thresholds=cfg.thresholds,
    )


# ---------------------------------------------------------------------------
# Tide
# ---------------------------------------------------------------------------

def high_tides_from_sea_level(
    sea_level: Sequence[tuple[datetime, Optional[float]]],
    lag_hours: Optional[float] = None,
) -> list[tuple[datetime, Optional[int]]]:
    """High tides (time, estimated coefficient) with the production extractor.

    Times are shifted to the Lidoire confluence by ``lag_hours`` (default:
    the production Open-Meteo lag), exactly as the collector does.
    """
    from app.models.schemas import TideType
    from app.services.collectors.tide import _extract_tides_from_levels

    if not sea_level:
        return []
    times = [ts.isoformat() for ts, _ in sea_level]
    levels = [v for _, v in sea_level]
    tides = _extract_tides_from_levels(times, levels, lag_hours)
    return sorted(
        (risk_core.to_utc_naive(td.timestamp), td.coefficient)
        for td in tides if td.tide_type == TideType.HIGH
    )


class _TideIndex:
    """Fast tide_at(): passes only the high tides within +-30h to risk_core."""

    WINDOW = timedelta(hours=30)

    def __init__(self, highs: Sequence[tuple[datetime, Optional[int]]]):
        self.highs = list(highs)
        self.times = [h[0] for h in self.highs]

    def at(self, t: datetime) -> risk_core.TideState:
        lo = bisect.bisect_left(self.times, t - self.WINDOW)
        hi = bisect.bisect_right(self.times, t + self.WINDOW)
        return risk_core.tide_at(t, self.highs[lo:hi])


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _window_values(points: Sequence[LevelPoint], t: datetime, hours: float) -> list[float]:
    lo = bisect.bisect_left(points, (t - timedelta(hours=hours), float("-inf")))
    hi = bisect.bisect_right(points, (t, float("inf")))
    return [h for _, h in points[lo:hi]]


def _latest_value(points: Sequence[LevelPoint], t: datetime, max_age_h: float) -> Optional[float]:
    """Last value with timestamp <= t, None if older than ``max_age_h``."""
    i = bisect.bisect_right(points, (t, float("inf")))
    if i == 0:
        return None
    ts, v = points[i - 1]
    return v if (t - ts).total_seconds() <= max_age_h * 3600 else None


def _window_max(points: Sequence[LevelPoint], t: datetime, hours: float) -> Optional[float]:
    win = _window_values(points, t, hours)
    return max(win) if win else None


def daily_value_at(
    daily: Sequence[tuple[date, float]],
    t: datetime,
    lag_after_day_h: float,
    max_age_days: int = DAILY_INPUT_MAX_AGE_DAYS,
) -> Optional[float]:
    """Value of the last day D published at t: D+1 00:00 + ``lag_after_day_h`` <= t."""
    if not daily:
        return None
    last_day = (t - timedelta(hours=lag_after_day_h) - timedelta(days=1)).date()
    i = bisect.bisect_right(daily, (last_day, float("inf")))
    if i == 0:
        return None
    d, v = daily[i - 1]
    return v if (last_day - d).days <= max_age_days else None


def hydro_values(raw: RawInputs, t: datetime, primary_pts: Sequence[LevelPoint]) -> dict[str, Optional[float]]:
    """v4-alpha raw inputs at t, with the publication lags (see module docstring)."""
    from app.services.risk_v4 import confluence_stage

    out: dict[str, Optional[float]] = {}
    for name, (_, lag_h) in HYDRO_Q_SERIES.items():
        pts = raw.discharge.get(name) or []
        t_pub = t - timedelta(hours=lag_h)
        out[f"q_{name}_m3s"] = _latest_value(pts, t_pub, CAUSAL_MAX_AGE_HOURS)
        out[f"q_{name}_max_12h"] = _window_max(pts, t_pub, ROLLING_WINDOW_H)
        if name in LAG0_SENSITIVITY and lag_h:
            # sensitivity / timing analysis only: no publication lag (optimistic)
            out[f"q_{name}_lag0_m3s"] = _latest_value(pts, t, CAUSAL_MAX_AGE_HOURS)
            out[f"q_{name}_lag0_max_12h"] = _window_max(pts, t, ROLLING_WINDOW_H)
            out[f"q_{name}_lag3_m3s"] = _latest_value(pts, t - timedelta(hours=3), CAUSAL_MAX_AGE_HOURS)
    out["libourne_h"] = _latest_value(raw.libourne_points, t, CAUSAL_MAX_AGE_HOURS)
    lib_env = _window_max(raw.libourne_points, t, ROLLING_WINDOW_H)
    out["libourne_max_12h"] = lib_env
    out["z_conf_ngf"] = confluence_stage(_window_max(primary_pts, t, ROLLING_WINDOW_H), lib_env)
    out["piezo_ngf"] = daily_value_at(raw.piezo_daily, t, PIEZO_LAG_AFTER_DAY_H)
    out["glofas_lidoire_m3s"] = daily_value_at(raw.glofas_daily, t, GLOFAS_LAG_AFTER_DAY_H)
    # v4-beta nowcast: second Eyraud point (rise between t - 10 h and t - 7 h)
    eyraud_lag = HYDRO_Q_SERIES["eyraud"][1]
    out["q_eyraud_lag10_m3s"] = _latest_value(
        raw.discharge.get("eyraud") or [], t - timedelta(hours=eyraud_lag + V4B_RISE_EXTRA_LAG_H),
        CAUSAL_MAX_AGE_HOURS,
    )
    return out


# v4-beta: the Eyraud rise is read between t - 10 h and t - 7 h
V4B_RISE_EXTRA_LAG_H = 3.0
# Open-Meteo Previous Runs archive starts on 2024-01-19 (previous_day1)
V4B_PREV_RUNS_START = date(2024, 1, 1)
V4B_ERA5_WINDOWS_H = (6, 24, 72, 720)


def v4b_rain_values(raw: RawInputs, times: Sequence[datetime]) -> list[dict[str, Optional[float]]]:
    """ERA5 basin rain windows and the archived 24 h forecast per row (v4-beta inputs).

    Past windows only use labels <= t (NaN -> None if an hour is missing).
    ``fc_rain_24h_prev`` = rain over (t, t + 24 h] forecast BEFORE t
    (:func:`scripts.eval.runoff_nowcast.previous_runs_forward_sum`), None
    where the archive does not cover the window (before 2024-01-20).
    """
    from scripts.eval import runoff_nowcast as rn

    if not times:
        return []
    out: list[dict[str, Optional[float]]] = [dict() for _ in times]
    if raw.rain_era5_by_point:
        start = times[0] - timedelta(hours=max(V4B_ERA5_WINDOWS_H))
        grid = rn.hourly_grid(start, times[-1])
        rain = rn.basin_rain_array(raw.rain_era5_by_point, grid)
        sums = {h: rn.past_window_sums(rain, h) for h in V4B_ERA5_WINDOWS_H}
        idx = {t: i for i, t in enumerate(grid.astype("datetime64[s]").astype(object))}
        for k, t in enumerate(times):
            i = idx.get(t)
            for h in V4B_ERA5_WINDOWS_H:
                v = sums[h][i] if i is not None else float("nan")
                out[k][f"era5_rain_{h}h"] = None if np.isnan(v) else float(v)
    day1 = _basin_mean(raw.rain_prev_runs.get("day1") or {})
    day2 = _basin_mean(raw.rain_prev_runs.get("day2") or {})
    for k, t in enumerate(times):
        out[k]["fc_rain_24h_prev"] = rn.previous_runs_forward_sum(day1, day2, t) if day1 and day2 else None
    return out


def _basin_mean(by_point: Mapping[str, Mapping[datetime, float]]) -> dict[datetime, float]:
    """Mean across the points present at each label."""
    acc: dict[datetime, list[float]] = {}
    for series in by_point.values():
        for ts, v in series.items():
            acc.setdefault(ts, []).append(v)
    n = len(by_point)
    # a label counts only when every point has it (same basin mean as the fit)
    return {ts: sum(v) / len(v) for ts, v in acc.items() if len(v) == n}


def build_rows(
    raw: RawInputs,
    start: datetime,
    end: datetime,
    station_configs: Sequence[StationConfig],
    primary_station_id: str,
    options: BuildOptions = BuildOptions(),
    settings=None,
) -> list[FeatureRow]:
    """One FeatureRow per hour in [start, end] (inclusive)."""
    settings = settings or get_settings()
    max_age = options.resolved_max_age(settings)
    rate_window = options.resolved_rate_window(settings)

    if options.level_mode == "interp":
        points = {sid: interpolate_points(p) for sid, p in raw.station_points.items()}
    elif options.level_mode == "causal":
        points = {sid: list(p) for sid, p in raw.station_points.items()}
    else:
        raise ValueError(f"unknown level_mode {options.level_mode!r}")
    if options.forecast_rain not in ("none", "oracle"):
        raise ValueError(f"unknown forecast_rain {options.forecast_rain!r}")

    rain_records = (
        (ts, mm, pid) for pid, series in raw.rain_by_point.items() for ts, mm in series.items()
    )
    rain_hourly = risk_core.aggregate_hourly_rain(rain_records)
    tide_index = _TideIndex(
        high_tides_from_sea_level(raw.sea_level, options.resolved_tide_lag()) if options.use_tide else []
    )
    cfg_by_id = {c.station_id: c for c in station_configs}
    primary_pts = points.get(primary_station_id, [])
    t_first = risk_core.floor_hour(start)
    n_hours = int((end - t_first).total_seconds() // 3600) + 1 if end >= t_first else 0
    v4b_values = v4b_rain_values(raw, [t_first + timedelta(hours=k) for k in range(n_hours)])

    rows: list[FeatureRow] = []
    t = t_first
    while t <= end:
        tide = tide_index.at(t) if options.use_tide else risk_core.TideState(False, None, None)
        rain_7d_ok = risk_core.rain_window_count(rain_hourly, t, 168) >= 150
        feats = risk_core.build_historical_features(
            t, station_configs, points, primary_station_id, rain_hourly, tide,
            max_age_hours=max_age, rate_window_h=rate_window, rain_7d_available=rain_7d_ok,
        )

        # Daily-max fallback for stations without a recent variable-step point
        present = {s.station_id for s in feats.stations}
        degraded = []
        extra = []
        for sid, daily in raw.station_daily_max.items():
            cfg = cfg_by_id.get(sid)
            if cfg is None or sid in present:
                continue
            feat = daily_station_feature(cfg, daily, t)
            if feat is not None:
                extra.append(feat)
                degraded.append(sid)
        if extra:
            ordered = sorted(
                list(feats.stations) + extra,
                key=lambda s: [c.station_id for c in station_configs].index(s.station_id),
            )
            feats = replace(feats, stations=tuple(ordered),
                            notes=feats.notes + (f"daily-max fallback: {','.join(degraded)}",))

        if options.forecast_rain == "oracle":
            feats = replace(
                feats,
                rain_forecast_24h_mm=risk_core.rain_window_sum(rain_hourly, t, 24, forward=True),
                rain_forecast_72h_mm=risk_core.rain_window_sum(rain_hourly, t, 72, forward=True),
            )

        values: dict[str, Optional[float]] = {}
        by_id = {s.station_id: s for s in feats.stations}
        for cfg in station_configs:
            s = by_id.get(cfg.station_id)
            values[f"h_{cfg.station_id}"] = s.height_m if s else None
            values[f"rate_{cfg.station_id}"] = s.rate_cm_h if s else None
        p = by_id.get(primary_station_id)
        values["pessac_h"] = p.height_m if p else None
        values["pessac_rate_cm_h"] = p.rate_cm_h if p else None
        win = _window_values(primary_pts, t, ROLLING_WINDOW_H)
        values["pessac_max_12h"] = max(win) if win else None
        values["pessac_mean_12h"] = sum(win) / len(win) if win else None
        active = [s for s in feats.stations if s.weight > 0]
        values["n_stations"] = float(len(active))
        values["n_rising"] = float(sum(1 for s in active if s.rate_cm_h > risk_core.RISING_RATE_CM_H))
        values["n_above_yellow"] = float(sum(1 for s in active if s.height_m >= s.thresholds[0]))
        values["n_above_orange"] = float(sum(1 for s in active if s.height_m >= s.thresholds[1]))
        values["n_daily_fallback"] = float(len(degraded))
        for hours in RAIN_WINDOWS_H:
            values[f"rain_{hours}h"] = risk_core.rain_window_sum(rain_hourly, t, hours)
        values["rain_hours_72h"] = float(risk_core.rain_window_count(rain_hourly, t, 72))
        values["tide_available"] = 1.0 if feats.tide_available else 0.0
        values["tide_coefficient"] = (
            float(feats.tide_coefficient) if feats.tide_coefficient is not None else None
        )
        values["hours_to_high_tide"] = feats.hours_to_high_tide
        values.update(hydro_values(raw, t, primary_pts))
        values.update(v4b_values[len(rows)])

        rows.append(FeatureRow(timestamp=t, features=feats, values=values))
        t += timedelta(hours=1)
    return rows


def leakage_flags(options: BuildOptions) -> dict:
    return {
        "causal": options.level_mode == "causal" and options.forecast_rain == "none",
        "level_mode": options.level_mode,
        "forecast_rain": options.forecast_rain,
        "notes": [
            n for n in (
                "interp: values between archived points use the next archived point"
                if options.level_mode == "interp" else None,
                "oracle: observed future rain used as the rain forecast"
                if options.forecast_rain == "oracle" else None,
            ) if n
        ],
    }


def default_station_configs(settings=None) -> tuple[StationConfig, ...]:
    """Scored stations (weight > 0), as in the production backfill."""
    settings = settings or get_settings()
    return tuple(
        StationConfig.from_settings_dict(c) for c in settings.hubeau_stations if c.get("weight", 0) > 0
    )


# ---------------------------------------------------------------------------
# Download + build
# ---------------------------------------------------------------------------

def collect_raw_inputs(
    start: date,
    end: date,
    offline: bool = False,
    refresh: bool = False,
    cache=None,
    client=None,
    settings=None,
) -> tuple[RawInputs, dict]:
    """Download (or read from cache) every raw series; returns (raw, sources meta).

    The download starts 8 days before ``start`` so that the 7-day rain window
    and the rate/rolling windows are complete on the first evaluated hour.
    """
    from scripts.eval import sources

    settings = settings or get_settings()
    cache = cache or sources.RawCache()
    own_client = None
    if client is None and not offline:
        own_client = client = sources.PoliteClient()
    fetch_start = start - timedelta(days=8)

    raw = RawInputs()
    meta: dict = {"stations": {}, "rain": {}, "tide": {}, "hydro": {}}
    try:
        for cfg in default_station_configs(settings):
            sr = sources.download_station_levels(cfg.station_id, fetch_start, end, cache, client, refresh)
            raw.station_points[cfg.station_id] = sr.points
            if sr.daily_max:
                raw.station_daily_max[cfg.station_id] = sr.daily_max
            meta["stations"][cfg.station_id] = {
                "name": cfg.station_name,
                "n_points": len(sr.points),
                "first": sr.points[0][0].isoformat() if sr.points else None,
                "last": sr.points[-1][0].isoformat() if sr.points else None,
                "chunks_ok": sr.chunks_ok,
                "chunks_daily_max_fallback": sr.chunks_daily_fallback,
                "chunks_missing": sr.chunks_missing,
                "source": "Hydroportail variable-step H (most_valid)",
            }
            logger.info("levels %s: %d points", cfg.station_name, len(sr.points))

        rain = sources.download_rain(
            settings.lidoire_basin_points, fetch_start, end, cache, client, refresh,
        )
        raw.rain_by_point = rain.by_point
        meta["rain"] = {
            "source": "Open-Meteo historical-forecast (best_match), ERA5 archive for gaps",
            "points": [p["id"] for p in settings.lidoire_basin_points],
            "aggregation": "mean across points per hour (risk_core.aggregate_hourly_rain)",
            "n_hours": {pid: len(v) for pid, v in rain.by_point.items()},
            "era5_filled_hours": rain.era5_filled,
        }

        hydro_meta: dict = {}
        for name, (sid, lag_h) in HYDRO_Q_SERIES.items():
            sq = sources.download_station_levels(sid, fetch_start, end, cache, client, refresh, variable="Q")
            # chunks without variable-step data: daily maxima (QIXnJ) stamped at the
            # end of the day, so that the max of day D is never used before D+1 00:00
            fallback = [(datetime(d.year, d.month, d.day) + timedelta(days=1), q) for d, q in sq.daily_max]
            raw.discharge[name] = sorted(sq.points + fallback)
            hydro_meta[f"q_{name}"] = {
                "station": sid, "n_points": len(sq.points), "lag_h": lag_h,
                "chunks_daily_max_fallback": sq.chunks_daily_fallback, "chunks_missing": sq.chunks_missing,
                "source": "Hydroportail variable-step Q (most_valid), m3/s",
            }
        lib = sources.download_station_levels(LIBOURNE_STATION_ID, fetch_start, end, cache, client, refresh)
        raw.libourne_points = sorted(lib.points)
        hydro_meta["libourne_h"] = {"station": LIBOURNE_STATION_ID, "n_points": len(lib.points),
                                    "chunks_missing": lib.chunks_missing}
        raw.piezo_daily = sources.download_piezo(PIEZO_BSS, fetch_start, end, cache, client, refresh)
        hydro_meta["piezo"] = {"bss": PIEZO_BSS, "n_days": len(raw.piezo_daily),
                               "lag": f"day D used from D+1 + {PIEZO_LAG_AFTER_DAY_H:g} h",
                               "source": "Hub'Eau niveaux_nappes chroniques (m NGF)"}
        glofas = sources.download_glofas("seamless_v4", *GLOFAS_CELL, fetch_start, end, cache, client, refresh)
        raw.glofas_daily = [(d, v) for d, v in glofas if v is not None]
        hydro_meta["glofas"] = {"cell": GLOFAS_CELL, "n_days": len(raw.glofas_daily),
                                "lag": f"day D used from D+1 + {GLOFAS_LAG_AFTER_DAY_H:g} h",
                                "source": "Open-Meteo Flood seamless_v4 (consolidated reanalysis to 2025-05, "
                                          "then archived forecast_v4)"}
        # v4-beta: ERA5 basin rain (30-day antecedent window) and archived rain forecasts
        from scripts.eval import runoff_nowcast

        raw.rain_era5_by_point = sources.download_era5_rain(
            settings.lidoire_basin_points, fetch_start - timedelta(days=31), end, cache, client, refresh,
        )
        prev = sources.download_previous_runs_rain(
            settings.lidoire_basin_points, runoff_nowcast.PREV_RUNS_MODEL, max(fetch_start, V4B_PREV_RUNS_START),
            end + timedelta(days=2), cache, client, refresh,
        ) if end >= V4B_PREV_RUNS_START else {}
        raw.rain_prev_runs = {
            "day1": prev.get("precipitation_previous_day1") or {},
            "day2": prev.get("precipitation_previous_day2") or {},
        }
        hydro_meta["era5_rain"] = {
            "points": [p["id"] for p in settings.lidoire_basin_points],
            "n_hours": {k: len(v) for k, v in raw.rain_era5_by_point.items()},
            "source": "Open-Meteo archive (ERA5), v4-beta observed-rain stand-in",
        }
        hydro_meta["prev_runs_rain"] = {
            "model": runoff_nowcast.PREV_RUNS_MODEL,
            "n_hours_day1": {k: len(v) for k, v in raw.rain_prev_runs["day1"].items()},
            "rule": f"labels t+1..t+{runoff_nowcast.DAY1_MAX_LEAD_H} from previous_day1, then previous_day2",
        }
        meta["hydro"] = hydro_meta
        # warm the cache of the climatology series (a-priori v4 thresholds)
        from scripts.eval import climatology

        climatology.download_series(cache, client, refresh)
        runoff_nowcast.training_series(cache, client, refresh, settings)

        from app.services.collectors.tide import MARINE_LAT, MARINE_LON

        raw.sea_level = sources.download_sea_level(
            MARINE_LAT, MARINE_LON, fetch_start, end + timedelta(days=2), cache, client, refresh,
        )
        meta["tide"] = {
            "source": "Open-Meteo Marine sea_level_height_msl (Gironde mouth), coefficients estimated, "
                      "times shifted by the phase lag to the Lidoire confluence",
            "n_hours": sum(1 for _, v in raw.sea_level if v is not None),
        }
    finally:
        if own_client is not None:
            meta["n_requests"] = own_client.n_requests
            own_client.close()
    return raw, meta


def build_dataset(
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    options: BuildOptions = BuildOptions(),
    offline: bool = False,
    refresh: bool = False,
    raw: Optional[RawInputs] = None,
    sources_meta: Optional[dict] = None,
    settings=None,
) -> EvalDataset:
    """Full pipeline: raw series (cache/download) -> hourly FeatureRows."""
    settings = settings or get_settings()
    if raw is None:
        raw, sources_meta = collect_raw_inputs(start, end, offline=offline, refresh=refresh, settings=settings)
    configs = default_station_configs(settings)
    t0 = datetime(start.year, start.month, start.day)
    t1 = datetime(end.year, end.month, end.day, 23)
    rows = build_rows(raw, t0, t1, configs, settings.hubeau_primary_station, options, settings)
    meta = {
        "start": t0.isoformat(),
        "end": t1.isoformat(),
        "n_hours": len(rows),
        "model_version": risk_core.MODEL_VERSION,
        "options": {
            "level_mode": options.level_mode,
            "forecast_rain": options.forecast_rain,
            "use_tide": options.use_tide,
            "tide_lag_hours": options.resolved_tide_lag(),
            "max_age_hours": options.resolved_max_age(settings),
            "rate_window_h": options.resolved_rate_window(settings),
        },
        "leakage": leakage_flags(options),
        "sources": sources_meta or {},
        "unavailable_inputs": ["soil_sat", "wind_west", "video", "rain_forecast (unless oracle)"],
        "coverage": coverage_summary(rows, configs),
    }
    return EvalDataset(rows, tuple(configs), settings.hubeau_primary_station, meta)


def coverage_summary(rows: Sequence[FeatureRow], configs: Sequence[StationConfig]) -> dict:
    """Share of hours with each input present."""
    n = len(rows) or 1
    out = {
        cfg.station_name: round(sum(1 for r in rows if r.values.get(f"h_{cfg.station_id}") is not None) / n, 4)
        for cfg in configs
    }
    out["any_station"] = round(sum(1 for r in rows if r.features.stations) / n, 4)
    out["tide_available"] = round(sum(1 for r in rows if r.features.tide_available) / n, 4)
    out["rain_72h_complete"] = round(sum(1 for r in rows if (r.values.get("rain_hours_72h") or 0) >= 72) / n, 4)
    out["daily_max_fallback_hours"] = sum(1 for r in rows if (r.values.get("n_daily_fallback") or 0) > 0)
    for key in ("q_eyraud_m3s", "q_caudeau_m3s", "q_pessac_m3s", "z_conf_ngf", "piezo_ngf", "glofas_lidoire_m3s",
                "q_eyraud_lag10_m3s", "era5_rain_720h", "fc_rain_24h_prev"):
        out[key] = round(sum(1 for r in rows if r.values.get(key) is not None) / n, 4)
    return out
