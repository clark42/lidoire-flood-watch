"""Shadow model "3.3-gate": v3.3 score x backwater gate (pure, no I/O).

Production port of the offline candidate ``V33BackwaterModel`` ("v3.3 +
gate remous", evaluation 2026-10-25: orange alerts 241 -> 74, POD 5/5). The
mill floods when the Lidoire's own discharge meets the backwater imposed by
the Dordogne at the confluence; ``H_aval`` = max of the primary station
(Pessac-sur-Dordogne) over the trailing 12.5 h (points <= t, the window
removes the tide / hydropower sub-daily cycle) stands for that backwater.

gate(H_aval) = 0.5 at H_aval <= 3.0 m (Pessac yellow), 1.0 at H_aval >=
3.8 m (Pessac orange), linear in between; no fitted parameter. A missing
H_aval leaves the v3.3 score unchanged; an hour without any station is not
scored (as the replay).

The shadow score is only computed and stored next to v3.3 (dedicated
measurement, see ``risk_v2.store_shadow_risk_score``): it never feeds the
displayed score, the API, the forecasts or the episode detection.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional, Sequence

from app.models.schemas import RiskLevel
from app.services.risk_core import (
    MODEL_VERSION,
    LevelPoint,
    ModelParams,
    RiskFeatures,
    _index_after,
    _index_from,
    score_to_level,
)

# Stored in the dedicated ``risk_score_shadow`` measurement
# (influx_client.RISK_SHADOW_MEASUREMENT), never in ``risk_score``.
SHADOW_MODEL_VERSION = f"{MODEL_VERSION}-gate"

BACKWATER_WINDOW_H = 12.5
# Pessac thresholds (config.hubeau_stations, P555001001), fixed as in the
# offline evaluation (label-free gate)
BACKWATER_GATE_LOW_M = 3.0
BACKWATER_GATE_FULL_M = 3.8
BACKWATER_GATE_MIN_FACTOR = 0.5


def backwater_level(
    points: Sequence[LevelPoint],
    t: datetime,
    window_h: float = BACKWATER_WINDOW_H,
) -> Optional[float]:
    """H_aval: max height of sorted ``(naive UTC, height_m)`` points in [t - window_h, t]."""
    lo = _index_from(points, t - timedelta(hours=window_h))
    hi = _index_after(points, t)
    window = points[lo:hi]
    return max(h for _, h in window) if window else None


def backwater_gate(
    h_aval: Optional[float],
    h_low: float = BACKWATER_GATE_LOW_M,
    h_full: float = BACKWATER_GATE_FULL_M,
    min_factor: float = BACKWATER_GATE_MIN_FACTOR,
) -> float:
    """Gate factor in [min_factor, 1]; 1.0 when H_aval is unknown."""
    if h_aval is None or math.isnan(h_aval):
        return 1.0
    if h_aval <= h_low:
        return min_factor
    if h_aval >= h_full:
        return 1.0
    frac = (h_aval - h_low) / (h_full - h_low)
    return min_factor + (1.0 - min_factor) * frac


def gated_score(
    features: RiskFeatures,
    base_score: float,
    h_aval: Optional[float],
) -> Optional[float]:
    """v3.3 score x gate, None when no station has a valid level."""
    if not features.stations:
        return None
    return base_score * backwater_gate(h_aval)


def shadow_level(score: float, params: Optional[ModelParams] = None) -> RiskLevel:
    """Level of a shadow score (same 15 / 35 / 55 boundaries as v3.3)."""
    return score_to_level(score, params)
