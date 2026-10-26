"""Runoff nowcast of the v4-beta candidate: Eyraud-equivalent discharge from rain (climatology fit).

Run from ``backend/``::

    python -m scripts.eval.runoff_nowcast [--offline]

The mill floods at the Lidoire's own runoff peak, which the Eyraud
(P540401002, 73.5 km2, the closest gauged analogue) tracks, but the Eyraud
discharge is published ~7 h late. The nowcast fills the gap with observed
basin rain: a log-linear regression of the Eyraud discharge ahead of t on
the last published discharge (t - 7 h), its 3 h rise, and the ERA5 basin
rain windows, FITTED ON THE CLIMATOLOGY ONLY (hydrological years
2010-09 -> 2022-08, strictly before the evaluation period and without any
label). See ``preregistration_v4beta.md`` for the pre-registered structure.

Inputs at hour t (all causal):

- ``q7`` = log1p(Q Eyraud latest point <= t - 7 h, 12 h max age);
- ``d7`` = q7 - log1p(Q Eyraud latest point <= t - 10 h) (rise at t - 7 h);
- ``r6``, ``r24``, ``r72``, ``w30`` = log1p(ERA5 basin rain over (t-6, t],
  (t-24, t-6], (t-72, t-24], (t-720, t-72]) (mean of the 3 Lidoire basin
  points of ``settings.lidoire_basin_points``, label = hour end);
- forecast model only: ``f24`` = log1p(rain over (t, t+24]): observed ERA5
  in the fit (perfect prognosis), the archived forecast in application.

Targets: log1p(max Eyraud Q over [t, t + 6 h]) (nowcast) and over
[t, t + 24 h] (forecast). Ordinary least squares, numpy only; the residual
90th percentile (log scale) of the fit is kept for the upper variant.

The coefficients are copied into :mod:`app.services.models_experimental`
(``V4B_NOWCAST_COEF`` ...); a test checks them against this fit when the
cache is present.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Mapping, Optional, Sequence

import numpy as np

# Climatology (training) period: hydrological years 2010-11 -> 2021-22, as the Eyraud Q2
TRAIN_START = datetime(2010, 9, 1)
TRAIN_END = datetime(2022, 8, 31, 23)
# downloads start early enough for the 30-day antecedent window
TRAIN_FETCH_START = date(2010, 7, 1)
ERA5_FETCH_START = date(2010, 1, 1)

EYRAUD_ID = "P540401002"
Q_LAG_H = 7.0  # Eyraud publication lag (research 2026-10-26)
Q_RISE_LAG_H = 10.0  # second point for the rise d7
Q_MAX_AGE_H = 12.0  # = dataset.CAUSAL_MAX_AGE_HOURS
NOWCAST_HORIZON_H = 6
FORECAST_HORIZON_H = 24

NOWCAST_FEATURES = ("q7", "d7", "r6", "r24", "r72", "w30", "q7_r6", "q7_r24")
FORECAST_FEATURES = NOWCAST_FEATURES + ("f24", "q7_f24")
# fallback when no Eyraud point is published (gauge down): rain only
RAIN_ONLY_FEATURES = ("r6", "r24", "r72", "w30")

# previous-runs forecast: hour labels (t, t + DAY1_MAX_LEAD_H] from the run of
# the previous day (initialised <= label - 24 h, published <= label - 20 h with
# a 4 h latency), the rest of the 24 h window from the run of 2 days before
PREV_RUNS_MODEL = "meteofrance_seamless"
DAY1_MAX_LEAD_H = 20


# ---------------------------------------------------------------------------
# Hourly arrays (vectorised, causal)
# ---------------------------------------------------------------------------

def hourly_grid(start: datetime, end: datetime) -> np.ndarray:
    n = int((end - start).total_seconds() // 3600) + 1
    return np.array([np.datetime64(start, "s") + np.timedelta64(3600 * i, "s") for i in range(n)])


def basin_rain_array(by_point: Mapping[str, Mapping[datetime, float]], grid: np.ndarray) -> np.ndarray:
    """Hourly basin mean (mm, label = hour end) on ``grid``; NaN where no point has data."""
    n = len(grid)
    total = np.zeros(n)
    count = np.zeros(n)
    t0 = grid[0]
    for series in by_point.values():
        for ts, v in series.items():
            if v is None:
                continue
            i = int((np.datetime64(ts, "s") - t0) // np.timedelta64(3600, "s"))
            if 0 <= i < n:
                total[i] += float(v)
                count[i] += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.maximum(count, 1), np.nan)


def past_window_sums(rain: np.ndarray, hours: int) -> np.ndarray:
    """Sum of labels in (t - hours, t] (NaN if any hour of the window is missing or out of range)."""
    filled = np.nan_to_num(rain, nan=0.0)
    miss = np.isnan(rain).astype(np.int64)
    c = np.concatenate(([0.0], np.cumsum(filled)))
    m = np.concatenate(([0], np.cumsum(miss)))
    n = len(rain)
    out = np.full(n, np.nan)
    i = np.arange(n)
    ok = i + 1 - hours >= 0
    lo = np.where(ok, i + 1 - hours, 0)
    s = c[i + 1] - c[lo]
    missing = m[i + 1] - m[lo]
    out[ok & (missing[...] == 0)] = s[ok & (missing == 0)]
    return out


def latest_values(points: Sequence[tuple[datetime, float]], times: np.ndarray, max_age_h: float) -> np.ndarray:
    """Last point value with timestamp <= each time (NaN if none within ``max_age_h``)."""
    if not points:
        return np.full(len(times), np.nan)
    pt = np.array([np.datetime64(p[0], "s") for p in points])
    pv = np.array([p[1] for p in points], dtype=float)
    idx = np.searchsorted(pt, times, side="right") - 1
    out = np.full(len(times), np.nan)
    ok = idx >= 0
    age = np.where(ok, (times - pt[np.maximum(idx, 0)]) / np.timedelta64(1, "h"), np.inf)
    good = ok & (age <= max_age_h)
    out[good] = pv[idx[good]]
    return out


def forward_max(points: Sequence[tuple[datetime, float]], times: np.ndarray, horizon_h: int,
                max_age_h: float = Q_MAX_AGE_H) -> np.ndarray:
    """max Q over [t, t + horizon]: points in the window and the value held at t (training target)."""
    held = latest_values(points, times, max_age_h)
    if not points:
        return held
    pt = np.array([np.datetime64(p[0], "s") for p in points])
    pv = np.array([p[1] for p in points], dtype=float)
    lo = np.searchsorted(pt, times, side="left")
    hi = np.searchsorted(pt, times + np.timedelta64(3600 * horizon_h, "s"), side="right")
    out = held.copy()
    for k in np.flatnonzero(hi > lo):
        w = pv[lo[k]:hi[k]].max()
        out[k] = w if np.isnan(out[k]) else max(out[k], w)
    return out


@dataclass
class NowcastInputs:
    """Raw (untransformed) nowcast inputs per hour of a grid."""

    times: np.ndarray
    q_lag7: np.ndarray
    q_lag10: np.ndarray
    rain_6h: np.ndarray
    rain_24h: np.ndarray
    rain_72h: np.ndarray
    rain_720h: np.ndarray

    def as_values(self, i: int) -> dict[str, Optional[float]]:
        def f(a):
            v = a[i]
            return None if np.isnan(v) else float(v)

        return {"q_eyraud_m3s": f(self.q_lag7), "q_eyraud_lag10_m3s": f(self.q_lag10),
                "era5_rain_6h": f(self.rain_6h), "era5_rain_24h": f(self.rain_24h),
                "era5_rain_72h": f(self.rain_72h), "era5_rain_720h": f(self.rain_720h)}


def nowcast_inputs(q_points: Sequence[tuple[datetime, float]], rain: np.ndarray, grid: np.ndarray) -> NowcastInputs:
    lag7 = grid - np.timedelta64(int(Q_LAG_H * 3600), "s")
    lag10 = grid - np.timedelta64(int(Q_RISE_LAG_H * 3600), "s")
    return NowcastInputs(
        times=grid,
        q_lag7=latest_values(q_points, lag7, Q_MAX_AGE_H),
        q_lag10=latest_values(q_points, lag10, Q_MAX_AGE_H),
        rain_6h=past_window_sums(rain, 6), rain_24h=past_window_sums(rain, 24),
        rain_72h=past_window_sums(rain, 72), rain_720h=past_window_sums(rain, 720),
    )


def forward_rain_sums(rain: np.ndarray, hours: int) -> np.ndarray:
    """Sum of labels in (t, t + hours] (perfect prognosis in the fit)."""
    past = past_window_sums(rain, hours)
    out = np.full(len(rain), np.nan)
    out[:-hours] = past[hours:]
    return out


def previous_runs_forward_sum(
    day1: Mapping[datetime, float], day2: Mapping[datetime, float], t: datetime, hours: int = FORECAST_HORIZON_H,
) -> Optional[float]:
    """Forecast rain over (t, t + hours] issued before t (None if an hour is missing).

    Labels t+1 .. t+DAY1_MAX_LEAD_H from ``day1`` (run >= 24 h before the
    label, published by label - 20 h <= t), later labels from ``day2``.
    """
    total = 0.0
    for k in range(1, hours + 1):
        lab = t + timedelta(hours=k)
        src = day1 if k <= DAY1_MAX_LEAD_H else day2
        v = src.get(lab)
        if v is None:
            return None
        total += v
    return total


# ---------------------------------------------------------------------------
# Design matrix (shared with app.services.models_experimental, which re-implements it)
# ---------------------------------------------------------------------------

def transformed_features(
    q_lag7, q_lag10, rain_6h, rain_24h, rain_72h, rain_720h, fc_24h=None,
) -> dict[str, np.ndarray]:
    """log1p features of the pre-registered regressions (arrays or scalars)."""
    q7 = np.log1p(np.maximum(np.asarray(q_lag7, dtype=float), 0.0))
    q10 = np.log1p(np.maximum(np.asarray(q_lag10, dtype=float), 0.0))
    r6 = np.log1p(np.maximum(np.asarray(rain_6h, dtype=float), 0.0))
    r24 = np.log1p(np.maximum(np.asarray(rain_24h, dtype=float) - np.asarray(rain_6h, dtype=float), 0.0))
    r72 = np.log1p(np.maximum(np.asarray(rain_72h, dtype=float) - np.asarray(rain_24h, dtype=float), 0.0))
    w30 = np.log1p(np.maximum(np.asarray(rain_720h, dtype=float) - np.asarray(rain_72h, dtype=float), 0.0))
    out = {"q7": q7, "d7": q7 - q10, "r6": r6, "r24": r24, "r72": r72, "w30": w30,
           "q7_r6": q7 * r6, "q7_r24": q7 * r24}
    if fc_24h is not None:
        f24 = np.log1p(np.maximum(np.asarray(fc_24h, dtype=float), 0.0))
        out["f24"] = f24
        out["q7_f24"] = q7 * f24
    return out


@dataclass
class OLSFit:
    features: tuple[str, ...]
    intercept: float
    coef: tuple[float, ...]
    resid_q90: float
    resid_sd: float
    r2: float
    n: int

    def to_dict(self) -> dict:
        return {"features": list(self.features), "intercept": self.intercept, "coef": list(self.coef),
                "resid_q90": self.resid_q90, "resid_sd": self.resid_sd, "r2": self.r2, "n": self.n}


def fit_ols(feats: Mapping[str, np.ndarray], y: np.ndarray, names: Sequence[str], mask: np.ndarray) -> OLSFit:
    x = np.column_stack([feats[n] for n in names])
    ok = mask & np.isfinite(y) & np.all(np.isfinite(x), axis=1)
    a = np.column_stack([np.ones(int(ok.sum())), x[ok]])
    beta, *_ = np.linalg.lstsq(a, y[ok], rcond=None)
    pred = a @ beta
    resid = y[ok] - pred
    r2 = 1.0 - float(np.sum(resid ** 2)) / float(np.sum((y[ok] - y[ok].mean()) ** 2))
    return OLSFit(tuple(names), float(beta[0]), tuple(float(b) for b in beta[1:]),
                  float(np.percentile(resid, 90)), float(resid.std()), r2, int(ok.sum()))


# ---------------------------------------------------------------------------
# Climatology fit
# ---------------------------------------------------------------------------

def training_series(cache=None, client=None, refresh: bool = False, settings=None):
    """(Eyraud variable-step Q points, ERA5 basin rain by point) over the training period."""
    from app.config import get_settings
    from scripts.eval import sources

    settings = settings or get_settings()
    cache = cache or sources.RawCache()
    sq = sources.download_station_levels(EYRAUD_ID, TRAIN_FETCH_START, TRAIN_END.date(), cache, client, refresh,
                                         variable="Q")
    era5 = sources.download_era5_rain(settings.lidoire_basin_points, ERA5_FETCH_START, TRAIN_END.date(),
                                      cache, client, refresh)
    return sorted(sq.points), era5, {"q_chunks_daily_fallback": sq.chunks_daily_fallback,
                                     "q_chunks_missing": sq.chunks_missing}


def fit_climatology(cache=None, client=None, refresh: bool = False) -> dict:
    q_points, era5, meta = training_series(cache, client, refresh)
    # the training series stop on 2022-09-30 at the latest: no evaluation hour is read
    q_points = [p for p in q_points if p[0] <= TRAIN_END + timedelta(days=30)]
    grid = hourly_grid(datetime(ERA5_FETCH_START.year, ERA5_FETCH_START.month, ERA5_FETCH_START.day),
                       TRAIN_END + timedelta(hours=FORECAST_HORIZON_H))
    rain = basin_rain_array(era5, grid)
    inp = nowcast_inputs(q_points, rain, grid)
    fc = forward_rain_sums(rain, FORECAST_HORIZON_H)
    feats = transformed_features(inp.q_lag7, inp.q_lag10, inp.rain_6h, inp.rain_24h, inp.rain_72h,
                                 inp.rain_720h, fc)
    y6 = np.log1p(forward_max(q_points, grid, NOWCAST_HORIZON_H))
    y24 = np.log1p(forward_max(q_points, grid, FORECAST_HORIZON_H))
    in_train = (grid >= np.datetime64(TRAIN_START, "s")) & (grid <= np.datetime64(TRAIN_END, "s"))
    now = fit_ols(feats, y6, NOWCAST_FEATURES, in_train)
    fcst = fit_ols(feats, y24, FORECAST_FEATURES, in_train)
    rain_only = fit_ols(feats, y6, RAIN_ONLY_FEATURES, in_train)
    persistence = fit_ols(feats, y6, ("q7",), in_train)
    return {"train": [TRAIN_START.isoformat(), TRAIN_END.isoformat()], "meta": meta,
            "n_q_points": len(q_points), "nowcast": now.to_dict(), "forecast": fcst.to_dict(),
            "rain_only": rain_only.to_dict(), "persistence_q7_only": persistence.to_dict(),
            "_arrays": (grid, inp, feats, y6, y24, in_train, q_points)}


# ---------------------------------------------------------------------------
# Label-free operating points (climatology 2010-09 -> 2022-08)
# ---------------------------------------------------------------------------

# Hazard thresholds tried, as fractions of the Eyraud Q2 (18.89 m3/s)
FRACTION_GRID = (0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.7, 1.0)
# Climatological budgets (merged alerts per year) of the yellow / orange / red anchors
LEVEL_BUDGETS = {"yellow": 10.0, "orange": 5.0, "red": 2.5}
EYRAUD_Q2_M3S = 18.89  # = app.services.risk_v4.EYRAUD_Q2_M3S (QIXnJ 2010-2022)
MERGE_GAP_H = 12
HOURS_PER_YEAR = 8766.0


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    m = np.concatenate(([False], mask, [False])).astype(np.int8)
    d = np.diff(m)
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1))


def _merge(runs: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in runs:
        if out and s - out[-1][1] <= gap + 1:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def observed_lag7_max(q_points, grid: np.ndarray, window_h: float = 12.5) -> np.ndarray:
    """12.5 h max of the Eyraud points ending t - 7 h (= dataset ``q_eyraud_max_12h``)."""
    out = np.full(len(grid), np.nan)
    if not q_points:
        return out
    pt = np.array([np.datetime64(p[0], "s") for p in q_points])
    pv = np.array([p[1] for p in q_points], dtype=float)
    lag = grid - np.timedelta64(int(Q_LAG_H * 3600), "s")
    hi = np.searchsorted(pt, lag, side="right")
    lo = np.searchsorted(pt, lag - np.timedelta64(int(window_h * 3600), "s"), side="left")
    for k in np.flatnonzero(hi > lo):
        out[k] = pv[lo[k]:hi[k]].max()
    return out


def predict(fit: Mapping, feats: Mapping[str, np.ndarray]) -> np.ndarray:
    x = np.column_stack([feats[n] for n in fit["features"]])
    return np.expm1(fit["intercept"] + x @ np.asarray(fit["coef"], dtype=float))


def operating_points(res: dict) -> dict:
    """Merged alert rate and lead before the Eyraud >= Q2 peaks for each hazard fraction.

    Hazard = max(observed Eyraud 12.5 h max at t - 7 h, nowcast). Alerts =
    runs of hazard >= f x Q2 merged at 12 h; pseudo-events = runs of the
    instantaneous Eyraud Q >= Q2 merged at 72 h (peak = their max); lead =
    peak - start of the first alert starting in [peak - 72 h, peak + 6 h]
    (or ongoing). Anchors: the LOWEST fraction whose rate fits each budget.
    """
    grid, inp, feats, _, _, in_train, q_points = res["_arrays"]
    hazard = np.fmax(observed_lag7_max(q_points, grid), predict(res["nowcast"], feats))
    qinst = latest_values(q_points, grid, Q_MAX_AGE_H)
    years = float(in_train.sum()) / HOURS_PER_YEAR
    ev = _merge(_runs(in_train & (np.nan_to_num(qinst) >= EYRAUD_Q2_M3S)), 72)
    peaks = [s + int(np.nanargmax(qinst[s:e + 1])) for s, e in ev]
    rows = []
    for f in FRACTION_GRID:
        al = _merge(_runs(in_train & (np.nan_to_num(hazard) >= f * EYRAUD_Q2_M3S)), MERGE_GAP_H)
        leads = []
        for p in peaks:
            cand = [s for s, e in al if p - 72 <= s <= p + 6 or s < p - 72 <= e]
            if cand:
                leads.append(float(p - cand[0]))
        rows.append({"fraction": f, "threshold_m3s": round(f * EYRAUD_Q2_M3S, 2),
                     "alerts_per_year": len(al) / years, "q2_events": len(peaks), "q2_hits": len(leads),
                     "lead_median_h": float(np.median(leads)) if leads else None,
                     "lead_min_h": min(leads) if leads else None})
    anchors = {}
    for level, budget in LEVEL_BUDGETS.items():
        ok = [r["fraction"] for r in rows if r["alerts_per_year"] <= budget]
        anchors[level] = min(ok) if ok else None
    return {"years": years, "rows": rows, "anchors_fraction_of_q2": anchors}


def main(argv: Optional[list[str]] = None) -> int:
    from scripts.eval import sources

    p = argparse.ArgumentParser(prog="python -m scripts.eval.runoff_nowcast", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--offline", action="store_true")
    args = p.parse_args(argv)
    client = None if args.offline else sources.PoliteClient()
    try:
        res = fit_climatology(client=client)
    finally:
        if client is not None:
            client.close()
    res["operating_points"] = operating_points(res)
    res.pop("_arrays")
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
