"""A-priori thresholds of the v4-alpha candidates, from climatology only (no label).

Run from ``backend/``::

    python -m scripts.eval.climatology [--offline]

Every series ends on 2022-09-30, before the evaluation period (2022-11-01
onwards), except the confluence quantile mapping, which only uses the
unlabelled hourly distribution of the evaluation dataset. The values are
hard-coded in :mod:`app.services.risk_v4` (pre-registered thresholds) and
checked against this computation by a test when the cache is present.

- Q2 (Eyraud, Caudeau): median of the hydrological-year (Sep-Aug) maxima of
  Hub'Eau QIXnJ (daily max discharge), years with >= ``MIN_DAYS`` days.
- Pessac T-year levels: empirical quantile ``1 - 1/T`` of the
  hydrological-year maxima of HIXnJ (daily max height), T = 2 / 5 / 10.
- GloFAS Q2: same on the consolidated_v4 daily discharge of the Lidoire cell.
- Mouliets piezometer: percentiles 50 / 90 / 97 / 99.5 of the daily level.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Optional, Sequence

import numpy as np

from scripts.eval import sources
from scripts.eval.dataset import GLOFAS_CELL, HYDRO_Q_SERIES, PIEZO_BSS

CLIM_END = date(2022, 9, 30)
EYRAUD_CLIM_START = date(2009, 12, 1)
CAUDEAU_CLIM_START = date(2010, 6, 1)
PESSAC_CLIM_START = date(1993, 1, 1)
GLOFAS_CLIM_START = date(1984, 1, 1)
PIEZO_CLIM_START = date(2007, 1, 1)
MIN_DAYS = 300
T_YEARS = (2, 5, 10)
PIEZO_PERCENTILES = (50.0, 90.0, 97.0, 99.5)


def hydro_year(d: date) -> int:
    """Hydrological year (Sep -> Aug) labelled by its starting calendar year."""
    return d.year if d.month >= 9 else d.year - 1


def hydro_year_maxima(daily: Sequence[tuple[date, Optional[float]]], min_days: int = MIN_DAYS) -> dict[int, float]:
    by: dict[int, list[float]] = {}
    for d, v in daily:
        if v is not None and not np.isnan(v):
            by.setdefault(hydro_year(d), []).append(float(v))
    return {y: max(v) for y, v in sorted(by.items()) if len(v) >= min_days}


def t_year_value(maxima: Sequence[float], t_years: float) -> float:
    """Empirical T-year value: quantile 1 - 1/T of the annual maxima (linear interpolation)."""
    return float(np.percentile(np.asarray(maxima, dtype=float), 100.0 * (1.0 - 1.0 / t_years)))


def quantile_mapped_thresholds(
    reference: np.ndarray, target: np.ndarray, thresholds: Sequence[float],
) -> tuple[float, ...]:
    """Values of ``target`` at the quantile ranks of ``thresholds`` in ``reference``.

    Hours where either series is missing are dropped (paired distribution).
    """
    ok = np.isfinite(reference) & np.isfinite(target)
    ref, tgt = reference[ok], target[ok]
    ranks = [100.0 * float(np.mean(ref < th)) for th in thresholds]
    return tuple(float(np.percentile(tgt, r)) for r in ranks)


def download_series(cache: Optional[sources.RawCache] = None, client=None, refresh: bool = False) -> dict:
    """Climatology series (cached; ``client=None`` = offline)."""
    cache = cache or sources.RawCache()
    eyraud_id = HYDRO_Q_SERIES["eyraud"][0]
    caudeau_id = HYDRO_Q_SERIES["caudeau"][0]
    pessac_id = HYDRO_Q_SERIES["pessac"][0]
    return {
        "eyraud_qixnj": sources.download_daily_elab(eyraud_id, "QIXnJ", EYRAUD_CLIM_START, CLIM_END,
                                                    cache, client, refresh),
        "caudeau_qixnj": sources.download_daily_elab(caudeau_id, "QIXnJ", CAUDEAU_CLIM_START, CLIM_END,
                                                     cache, client, refresh),
        "pessac_hixnj": sources.download_daily_elab(pessac_id, "HIXnJ", PESSAC_CLIM_START, CLIM_END,
                                                    cache, client, refresh),
        "glofas_consolidated": [
            (d, v) for d, v in sources.download_glofas("consolidated_v4", *GLOFAS_CELL, GLOFAS_CLIM_START,
                                                       CLIM_END, cache, client, refresh)
            if d <= CLIM_END
        ],
        "piezo": [(d, v) for d, v in sources.download_piezo(PIEZO_BSS, PIEZO_CLIM_START, CLIM_END, cache, client,
                                                             refresh) if d <= CLIM_END],
    }


def compute(series: dict) -> dict:
    """Pre-registered thresholds from the climatology series."""
    out: dict = {"clim_end": CLIM_END.isoformat(), "min_days_per_year": MIN_DAYS}
    for key, name in (("eyraud_qixnj", "eyraud"), ("caudeau_qixnj", "caudeau"), ("glofas_consolidated", "glofas")):
        maxima = hydro_year_maxima(series.get(key) or [])
        out[f"{name}_annual_maxima"] = maxima
        out[f"{name}_q2_m3s"] = float(np.median(list(maxima.values()))) if maxima else None
    pmax = hydro_year_maxima(series.get("pessac_hixnj") or [])
    out["pessac_annual_maxima_m"] = pmax
    out["pessac_t_year_levels_m"] = (
        tuple(t_year_value(list(pmax.values()), t) for t in T_YEARS) if pmax else None
    )
    piezo = [v for _, v in series.get("piezo") or []]
    out["piezo_n_days"] = len(piezo)
    out["piezo_first"] = series["piezo"][0][0].isoformat() if series.get("piezo") else None
    out["piezo_percentiles_ngf"] = (
        tuple(float(np.percentile(piezo, q)) for q in PIEZO_PERCENTILES) if piezo else None
    )
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.eval.climatology", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--offline", action="store_true", help="cache only, no download")
    args = p.parse_args(argv)
    client = None if args.offline else sources.PoliteClient()
    try:
        res = compute(download_series(client=client))
    finally:
        if client is not None:
            client.close()
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
