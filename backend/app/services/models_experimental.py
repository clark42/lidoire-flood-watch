"""Experimental candidate flood models (NOT used by any production path).

Nothing in ``app/`` imports this module: realtime, backfill, forecast and
TimeMachine only use :mod:`app.services.risk_core`. The candidates here are
evaluated offline by ``scripts/eval/candidates.py`` (hourly causal replay,
event-based contingency, leave-one-event-out / blocked cross-validation).

Physical idea (research 2026-10-25): the mill sits on the Lidoire just
upstream of its confluence with the tidal Dordogne. The water level at the
mill depends on (1) the Lidoire's own discharge, forced by recent rain on
its 231 km2 catchment, and (2) the BACKWATER imposed by the Dordogne at the
confluence, approximated by ``H_aval`` = 12.5h rolling max (points <= t) of
Pessac-sur-Dordogne (P555001001, ~7 km upstream of the confluence; the
rolling max removes the tide / hydropower sub-daily cycle).

Candidates (every model follows the ``scripts.eval`` Model protocol:
``name``, ``kind``, ``thresholds``, ``reset()``, ``score(row)``, where
``row`` has ``timestamp``, ``values`` and ``features``):

- :class:`BackwaterRuleModel`: standalone 2-variable rule, alert when the
  backwater AND the Lidoire forcing are both high (score = min of the two
  per-variable scores). Forcing = antecedent precipitation index (API),
  basin rain 24h, or the GR4H discharge.
- :class:`V33BackwaterModel`: production v3.3 score multiplied by a
  backwater gate (0.5 below the Pessac yellow threshold, 1.0 from orange).
- :class:`LogisticModel`: penalised logistic regression on 2-3 features
  ("flood within the next 24h" probability), fitted by
  :func:`fit_logistic_l2` (numpy Newton/IRLS, strong L2 on standardised
  coefficients, intercept not penalised).
- :class:`GR4H`: minimal GR4H conceptual reservoir model (production store,
  two unit hydrographs, routing store) of the Lidoire, parameters fixed from
  the literature (no calibration: there is no discharge gauge on the
  Lidoire) and a climatological potential evapotranspiration.

Every stateful quantity (API, GR4H stores) is updated from ``rain_1h`` in
chronological order, so a model only ever uses current and past rows. A
timestamp that goes backwards resets the state (new replay).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional, Sequence

import numpy as np

LIDOIRE_AREA_KM2 = 231.0
PRIMARY_STATION_ID = "P555001001"  # Pessac-sur-Dordogne

# Antecedent precipitation index decays (per day)
API_K_FAST = 0.88  # ~8 days e-folding: Lidoire event forcing
API_K_SLOW = 0.97  # ~33 days: soil wetness proxy

# Pessac thresholds (config.hubeau_stations, P555001001) - used as the
# label-free backwater thresholds
PESSAC_THRESHOLDS_M = (3.0, 3.8, 4.5)

# yellow/orange and red/orange ratios of the Pessac thresholds: used to
# derive the yellow and red steps from a single fitted orange threshold
YELLOW_RATIO = PESSAC_THRESHOLDS_M[0] / PESSAC_THRESHOLDS_M[1]
RED_RATIO = PESSAC_THRESHOLDS_M[2] / PESSAC_THRESHOLDS_M[1]

DEFAULT_SCORE_THRESHOLDS = {"orange": 35.0, "red": 55.0}
DEFAULT_PROBABILITY_THRESHOLDS = {"orange": 0.05, "red": 0.20}


def piecewise_score(value: Optional[float], thresholds: Sequence[float]) -> Optional[float]:
    """Monotone map (yellow, orange, red) -> (15, 35, 55), linear beyond red, capped 100.

    Same mapping as ``scripts.eval.models.piecewise_score`` (duplicated so that
    ``app/`` never imports ``scripts/``).
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    yellow, orange, red = thresholds
    if value <= 0:
        return 0.0
    if value < yellow:
        return 15.0 * value / yellow
    if value < orange:
        return 15.0 + 20.0 * (value - yellow) / (orange - yellow)
    if value < red:
        return 35.0 + 20.0 * (value - orange) / (red - orange)
    return min(100.0, 55.0 + 20.0 * (value - red) / (red - orange))


def steps_from_orange(orange: float) -> tuple[float, float, float]:
    """(yellow, orange, red) with the Pessac threshold ratios."""
    return (orange * YELLOW_RATIO, orange, orange * RED_RATIO)


# ---------------------------------------------------------------------------
# Forcing: antecedent precipitation index, climatological PET, GR4H
# ---------------------------------------------------------------------------

class AntecedentPrecipitationIndex:
    """API_t = API_{t-dt} * k^(dt/24h) + P_t (mm), hourly steps."""

    def __init__(self, k_per_day: float, initial: float = 0.0):
        if not 0.0 < k_per_day < 1.0:
            raise ValueError("k_per_day must be in (0, 1)")
        self.k_per_day = k_per_day
        self.initial = initial
        self.value = initial

    def reset(self) -> None:
        self.value = self.initial

    def update(self, rain_mm: float, dt_h: float = 1.0) -> float:
        self.value = self.value * self.k_per_day ** (dt_h / 24.0) + max(0.0, rain_mm)
        return self.value


def climatological_pet_mm_h(t: datetime) -> float:
    """Potential evapotranspiration (mm/h), annual sinusoid for SW France.

    0.2 mm/day mid-January to 3.8 mm/day mid-July (~730 mm/year, the order
    of the Oudin PE in Gironde), spread evenly over the day. No archived
    ET0 is used: this is a fixed climatology.
    """
    doy = t.timetuple().tm_yday
    daily = 2.0 + 1.8 * math.sin(2.0 * math.pi * (doy - 105) / 365.25)
    return max(0.0, daily) / 24.0


def _sh_curve(t: float, x4: float, d: float, half: bool) -> float:
    """S-curves of the GR4 unit hydrographs (UH1 base x4, UH2 base 2*x4)."""
    if t <= 0:
        return 0.0
    if not half:
        return 1.0 if t >= x4 else (t / x4) ** d
    if t >= 2 * x4:
        return 1.0
    if t < x4:
        return 0.5 * (t / x4) ** d
    return 1.0 - 0.5 * (2.0 - t / x4) ** d


@dataclass
class GR4HParams:
    """GR4H parameters (hourly step). Fixed, NOT calibrated.

    x1 = 350 mm (production store, median of GR4J regional sets, Perrin
    2003); x2 = 0 mm/h (no groundwater exchange); x3 = 90 mm (routing store,
    median GR4J); x4 = 16 h (unit hydrograph base, from the 12-20h
    concentration time of the Lidoire at the confluence, Kirpich/Giandotti).
    d = 1.25 and the 21/4 percolation factor are the airGR GR4H constants.
    """

    x1: float = 350.0
    x2: float = 0.0
    x3: float = 90.0
    x4: float = 16.0
    d: float = 1.25
    perc_factor: float = 21.0 / 4.0
    s0_ratio: float = 0.5
    r0_ratio: float = 0.5


class GR4H:
    """Minimal GR4H (Mathevet 2005 / airGR) for the Lidoire: step(P, E) -> Q (mm/h)."""

    def __init__(self, params: Optional[GR4HParams] = None):
        self.p = params or GR4HParams()
        n1 = int(math.ceil(self.p.x4))
        n2 = int(math.ceil(2 * self.p.x4))
        self.uh1 = np.array([
            _sh_curve(i, self.p.x4, self.p.d, False) - _sh_curve(i - 1, self.p.x4, self.p.d, False)
            for i in range(1, n1 + 1)
        ])
        self.uh2 = np.array([
            _sh_curve(i, self.p.x4, self.p.d, True) - _sh_curve(i - 1, self.p.x4, self.p.d, True)
            for i in range(1, n2 + 1)
        ])
        self.reset()

    def reset(self) -> None:
        self.s = self.p.s0_ratio * self.p.x1
        self.r = self.p.r0_ratio * self.p.x3
        self.buf1 = np.zeros(len(self.uh1))
        self.buf2 = np.zeros(len(self.uh2))
        self.q = 0.0

    @property
    def soil_filling(self) -> float:
        """Production store filling S/X1 (0-1): a soil wetness proxy."""
        return self.s / self.p.x1

    def step(self, rain_mm: float, pet_mm: float) -> float:
        p = self.p
        x1 = p.x1
        rain_mm = max(0.0, rain_mm)
        if rain_mm >= pet_mm:
            pn = rain_mm - pet_mm
            ratio = self.s / x1
            th = math.tanh(min(pn / x1, 13.0))
            ps = x1 * (1 - ratio ** 2) * th / (1 + ratio * th)
            es = 0.0
        else:
            pn = 0.0
            en = pet_mm - rain_mm
            ratio = self.s / x1
            th = math.tanh(min(en / x1, 13.0))
            es = self.s * (2 - ratio) * th / (1 + (1 - ratio) * th)
            ps = 0.0
        self.s = min(x1, max(0.0, self.s + ps - es))
        perc = self.s * (1 - (1 + (self.s / (p.perc_factor * x1)) ** 4) ** -0.25)
        self.s -= perc
        pr = perc + (pn - ps)

        # unit hydrographs (90% slow branch via UH1, 10% direct via UH2)
        self.buf1 = np.roll(self.buf1, -1)
        self.buf1[-1] = 0.0
        self.buf1 += 0.9 * pr * self.uh1
        self.buf2 = np.roll(self.buf2, -1)
        self.buf2[-1] = 0.0
        self.buf2 += 0.1 * pr * self.uh2
        q9 = self.buf1[0]
        q1 = self.buf2[0]
        self.buf1[0] = 0.0
        self.buf2[0] = 0.0

        exch = p.x2 * (self.r / p.x3) ** 3.5 if self.r > 0 else 0.0
        self.r = max(0.0, self.r + q9 + exch)
        qr = self.r * (1 - (1 + (self.r / p.x3) ** 4) ** -0.25)
        self.r -= qr
        qd = max(0.0, q1 + exch)
        self.q = qr + qd
        return self.q


def mm_h_to_m3s(q_mm_h: float, area_km2: float = LIDOIRE_AREA_KM2) -> float:
    return q_mm_h * area_km2 * 1e3 / 3600.0


# ---------------------------------------------------------------------------
# Shared causal feature extractor
# ---------------------------------------------------------------------------

FEATURE_NAMES = (
    "h_aval", "api_fast", "api_slow", "rain_24h", "rain_72h", "q_gr4h", "gr4h_soil",
)


class CandidateFeatures:
    """Causal candidate features, updated row by row (chronological order).

    - ``h_aval``: 12.5h rolling max of Pessac (``values['pessac_max_12h']``);
    - ``api_fast`` / ``api_slow``: API of basin rain, k = 0.88 / 0.97 per day;
    - ``rain_24h`` / ``rain_72h``: basin rain windows from the dataset;
    - ``q_gr4h`` (m3/s) and ``gr4h_soil`` (S/X1): GR4H driven by ``rain_1h``
      and the climatological PET.

    A missing ``rain_1h`` counts as 0 mm. A timestamp at or before the last
    one resets every state (a new replay started).
    """

    def __init__(self, gr4h_params: Optional[GR4HParams] = None, with_gr4h: bool = True):
        self.api_fast = AntecedentPrecipitationIndex(API_K_FAST)
        self.api_slow = AntecedentPrecipitationIndex(API_K_SLOW)
        self.gr4h = GR4H(gr4h_params) if with_gr4h else None
        self.last_ts: Optional[datetime] = None

    def reset(self) -> None:
        self.api_fast.reset()
        self.api_slow.reset()
        if self.gr4h is not None:
            self.gr4h.reset()
        self.last_ts = None

    def update(self, timestamp: datetime, values: Mapping[str, Optional[float]]) -> dict[str, Optional[float]]:
        dt_h = 1.0
        if self.last_ts is not None:
            dt_h = (timestamp - self.last_ts).total_seconds() / 3600.0
            if dt_h <= 0:
                self.reset()
                dt_h = 1.0
        self.last_ts = timestamp
        rain = values.get("rain_1h") or 0.0
        out: dict[str, Optional[float]] = {
            "h_aval": values.get("pessac_max_12h"),
            "api_fast": self.api_fast.update(rain, dt_h),
            "api_slow": self.api_slow.update(rain, dt_h),
            "rain_24h": values.get("rain_24h"),
            "rain_72h": values.get("rain_72h"),
            "q_gr4h": None,
            "gr4h_soil": None,
        }
        if self.gr4h is not None:
            # hours skipped since the last row: zero rain, PET only
            for _ in range(max(0, int(round(dt_h)) - 1)):
                self.gr4h.step(0.0, climatological_pet_mm_h(timestamp))
            q = self.gr4h.step(rain, climatological_pet_mm_h(timestamp))
            out["q_gr4h"] = mm_h_to_m3s(q)
            out["gr4h_soil"] = self.gr4h.soil_filling
        return out


def feature_table(rows: Sequence, gr4h_params: Optional[GR4HParams] = None) -> dict[str, np.ndarray]:
    """Candidate features for every row (NaN when missing), one causal pass."""
    ext = CandidateFeatures(gr4h_params)
    cols: dict[str, list[float]] = {k: [] for k in FEATURE_NAMES}
    for row in rows:
        f = ext.update(row.timestamp, row.values)
        for k in FEATURE_NAMES:
            v = f.get(k)
            cols[k].append(float("nan") if v is None else float(v))
    return {k: np.asarray(v, dtype=float) for k, v in cols.items()}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class _StatefulModel:
    """Base: owns a CandidateFeatures extractor, reset before each replay."""

    with_gr4h = False

    def _init_extractor(self) -> None:
        self._ext = CandidateFeatures(with_gr4h=self.with_gr4h)

    def reset(self) -> None:
        self._ext.reset()

    def _features(self, row) -> dict[str, Optional[float]]:
        return self._ext.update(row.timestamp, row.values)


@dataclass
class BackwaterRuleModel(_StatefulModel):
    """Two-variable rule: score = min(score(H_aval), score(forcing)).

    Each variable is mapped with its own (yellow, orange, red) thresholds to
    15/35/55, so the alert at a level fires when BOTH variables exceed that
    level (joint exceedance: backwater AND Lidoire forcing). ``forcing`` is
    one of ``api_fast``, ``rain_24h``, ``rain_72h`` or ``q_gr4h``.
    """

    f_thresholds: tuple[float, float, float]
    h_thresholds: tuple[float, float, float] = PESSAC_THRESHOLDS_M
    forcing: str = "api_fast"
    name: str = "bw_rule"
    kind: str = "score"
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORE_THRESHOLDS))

    def __post_init__(self):
        if self.forcing not in FEATURE_NAMES:
            raise ValueError(f"unknown forcing {self.forcing!r}")
        self.with_gr4h = self.forcing == "q_gr4h"
        self._init_extractor()

    def score_features(self, f: Mapping[str, Optional[float]]) -> Optional[float]:
        sh = piecewise_score(f.get("h_aval"), self.h_thresholds)
        sf = piecewise_score(f.get(self.forcing), self.f_thresholds)
        if sh is None or sf is None:
            return None
        return min(sh, sf)

    def score(self, row) -> Optional[float]:
        return self.score_features(self._features(row))


@dataclass
class V33BackwaterModel(_StatefulModel):
    """v3.3 score x backwater gate g(H_aval).

    g = ``min_factor`` at H_aval <= ``h_low`` (Pessac yellow, 3.0 m), 1 at
    H_aval >= ``h_full`` (Pessac orange, 3.8 m), linear in between; no
    fitted parameter. A missing H_aval leaves the v3.3 score unchanged.
    Hours without any station are not scored (as V33Model).
    """

    h_low: float = PESSAC_THRESHOLDS_M[0]
    h_full: float = PESSAC_THRESHOLDS_M[1]
    min_factor: float = 0.5
    name: str = "v3.3+backwater"
    kind: str = "score"
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORE_THRESHOLDS))

    def __post_init__(self):
        self._init_extractor()

    def gate(self, h_aval: Optional[float]) -> float:
        if h_aval is None or math.isnan(h_aval):
            return 1.0
        if h_aval <= self.h_low:
            return self.min_factor
        if h_aval >= self.h_full:
            return 1.0
        frac = (h_aval - self.h_low) / (self.h_full - self.h_low)
        return self.min_factor + (1.0 - self.min_factor) * frac

    def score(self, row) -> Optional[float]:
        from app.services.risk_core import compute_risk, get_model_params

        f = self._features(row)
        if not row.features.stations:
            return None
        base = compute_risk(row.features, get_model_params()).score
        return base * self.gate(f.get("h_aval"))


def _transform(name: str, x: np.ndarray) -> np.ndarray:
    """Feature transform used by the logistic models (log1p for rain/flow)."""
    if name in ("api_fast", "api_slow", "rain_24h", "rain_72h", "q_gr4h"):
        return np.log1p(np.maximum(x, 0.0))
    return x


@dataclass
class LogisticModel(_StatefulModel):
    """P(flood within next 24h) = sigmoid(b0 + sum b_i z_i), z standardised features."""

    feature_names: tuple[str, ...]
    mean: tuple[float, ...]
    std: tuple[float, ...]
    coef: tuple[float, ...]
    intercept: float
    name: str = "logistic"
    kind: str = "probability"
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PROBABILITY_THRESHOLDS))

    def __post_init__(self):
        self.with_gr4h = any(n in ("q_gr4h", "gr4h_soil") for n in self.feature_names)
        self._init_extractor()

    def predict_matrix(self, table: Mapping[str, np.ndarray]) -> np.ndarray:
        """Vectorised probabilities from a :func:`feature_table` (NaN if a feature is missing)."""
        z = np.column_stack([
            (_transform(n, np.asarray(table[n], dtype=float)) - m) / s
            for n, m, s in zip(self.feature_names, self.mean, self.std)
        ])
        return _sigmoid(self.intercept + z @ np.asarray(self.coef))

    def score_features(self, f: Mapping[str, Optional[float]]) -> Optional[float]:
        vals = [f.get(n) for n in self.feature_names]
        if any(v is None or math.isnan(v) for v in vals):
            return None
        table = {n: np.asarray([v], dtype=float) for n, v in zip(self.feature_names, vals)}
        return float(self.predict_matrix(table)[0])

    def score(self, row) -> Optional[float]:
        return self.score_features(self._features(row))


@dataclass
class LogisticFit:
    intercept: float
    coef: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    n_iter: int
    converged: bool


def fit_logistic_l2(
    x: np.ndarray,
    y: np.ndarray,
    weights: Optional[np.ndarray] = None,
    l2: float = 1.0,
    max_iter: int = 100,
    tol: float = 1e-8,
) -> LogisticFit:
    """Weighted L2-penalised logistic regression (Newton/IRLS, numpy only).

    Columns are standardised (mean 0, sd 1) first; the penalty
    ``l2/2 * ||beta||^2`` applies to the standardised slopes (a N(0, 1/l2)
    prior), never to the intercept. With few events, a strong ``l2`` shrinks
    the slopes towards 0 (Firth-like small-sample protection).
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    n, k = x.shape
    w = np.ones(n) if weights is None else np.asarray(weights, dtype=float)
    mean = np.average(x, axis=0, weights=w)
    std = np.sqrt(np.average((x - mean) ** 2, axis=0, weights=w))
    std = np.where(std > 0, std, 1.0)
    z = np.column_stack([np.ones(n), (x - mean) / std])
    penalty = np.full(k + 1, l2)
    penalty[0] = 0.0
    ybar = np.clip(np.average(y, weights=w), 1e-6, 1 - 1e-6)
    beta = np.zeros(k + 1)
    beta[0] = math.log(ybar / (1 - ybar))

    def objective(b: np.ndarray) -> float:
        eta = z @ b
        # weighted log-likelihood, stable form: y*eta - log(1 + e^eta)
        return float(np.sum(w * (y * eta - np.logaddexp(0.0, eta))) - 0.5 * np.sum(penalty * b * b))

    current = objective(beta)
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        p = _sigmoid(z @ beta)
        grad = z.T @ (w * (y - p)) - penalty * beta
        hess = (z * (w * p * (1 - p))[:, None]).T @ z + np.diag(penalty)
        step = np.linalg.solve(hess + 1e-12 * np.eye(k + 1), grad)
        # step halving: the penalised log-likelihood never decreases
        t = 1.0
        while t > 1e-6:
            candidate = objective(beta + t * step)
            if candidate >= current - 1e-12:
                break
            t /= 2.0
        beta = beta + t * step
        improvement = objective(beta) - current
        current += improvement
        if np.max(np.abs(t * step)) < tol or abs(improvement) < tol * (1.0 + abs(current)):
            converged = True
            break
    return LogisticFit(float(beta[0]), beta[1:], mean, std, it, converged)


def _sigmoid(eta: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(eta, -500.0, 500.0)))


def logistic_model_from_fit(
    fit: LogisticFit,
    feature_names: Sequence[str],
    name: str = "logistic",
    thresholds: Optional[Mapping[str, float]] = None,
) -> LogisticModel:
    return LogisticModel(
        feature_names=tuple(feature_names),
        mean=tuple(float(v) for v in fit.mean),
        std=tuple(float(v) for v in fit.std),
        coef=tuple(float(v) for v in fit.coef),
        intercept=fit.intercept,
        name=name,
        thresholds=dict(thresholds or DEFAULT_PROBABILITY_THRESHOLDS),
    )


def design_matrix(table: Mapping[str, np.ndarray], feature_names: Sequence[str]) -> np.ndarray:
    """Transformed (unstandardised) feature columns, NaN kept."""
    return np.column_stack([_transform(n, np.asarray(table[n], dtype=float)) for n in feature_names])
