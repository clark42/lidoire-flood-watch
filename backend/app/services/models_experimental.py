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
- :class:`V4AlphaModel`: the v4-alpha mechanism model (backwater AND
  runoff, OR overbank, optional groundwater) of
  :mod:`app.services.risk_v4`, fed with the candidate features; variants
  change the runoff proxy (Eyraud / Caudeau discharge, GR4H, GloFAS), the
  backwater input (Pessac H_aval or confluence stage Z_c) and the switches.
  :func:`v4_alpha_scores` is an independent vectorised implementation
  (equivalence test).
- :class:`V4BetaModel`: the runoff-led v4-beta (pre-registered 2026-10-27,
  ``scripts/eval/preregistration_v4beta.md``): Eyraud-equivalent runoff =
  max(published Eyraud Q, climatology-fitted nowcast from ERA5 basin rain),
  Pessac only as an overbank branch above the 3.1 km step crest; variants
  with archived forecast rain, a Pessac modulator, or no Pessac. Not
  promoted to ``app/services`` proper: it did not clear the pre-registered
  rule (see CLAUDE.md).

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

from app.services.risk_v4 import (
    EYRAUD_Q2_M3S,
    GradedThresholds,
    V4Features,
    V4Params,
    compute_risk_v4,
    graded_score,
)
from app.services.risk_shadow import (
    BACKWATER_GATE_FULL_M,
    BACKWATER_GATE_LOW_M,
    BACKWATER_GATE_MIN_FACTOR,
    backwater_gate,
)

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
    "q_eyraud", "q_caudeau", "q_glofas", "z_conf", "piezo", "q_eyraud_lag0",
)

# dataset value -> candidate feature (pass-through, already causal and lagged)
PASSTHROUGH_FEATURES = {
    "q_eyraud": "q_eyraud_max_12h",  # 12.5 h max of the Eyraud Q ending t - 7 h
    "q_caudeau": "q_caudeau_max_12h",
    "q_glofas": "glofas_lidoire_m3s",
    "z_conf": "z_conf_ngf",
    "piezo": "piezo_ngf",
    "q_eyraud_lag0": "q_eyraud_lag0_max_12h",  # sensitivity only: no publication lag
}


class CandidateFeatures:
    """Causal candidate features, updated row by row (chronological order).

    - ``h_aval``: 12.5h rolling max of Pessac (``values['pessac_max_12h']``);
    - ``api_fast`` / ``api_slow``: API of basin rain, k = 0.88 / 0.97 per day;
    - ``rain_24h`` / ``rain_72h``: basin rain windows from the dataset;
    - ``q_gr4h`` (m3/s) and ``gr4h_soil`` (S/X1): GR4H driven by ``rain_1h``
      and the climatological PET;
    - v4-alpha pass-through inputs (:data:`PASSTHROUGH_FEATURES`): Eyraud /
      Caudeau discharge (12.5 h max, publication lag applied by the
      dataset), GloFAS Lidoire discharge, confluence stage, groundwater.

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
        for name, key in PASSTHROUGH_FEATURES.items():
            out[name] = values.get(key)
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
    Hours without any station are not scored (as V33Model). The gate is the
    production one (:mod:`app.services.risk_shadow`, shadow series
    "3.3-gate"), so the replay scores exactly what the scheduler stores.
    """

    h_low: float = BACKWATER_GATE_LOW_M
    h_full: float = BACKWATER_GATE_FULL_M
    min_factor: float = BACKWATER_GATE_MIN_FACTOR
    name: str = "v3.3+backwater"
    kind: str = "score"
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORE_THRESHOLDS))

    def __post_init__(self):
        self._init_extractor()

    def gate(self, h_aval: Optional[float]) -> float:
        return backwater_gate(h_aval, self.h_low, self.h_full, self.min_factor)

    def score(self, row) -> Optional[float]:
        from app.services.risk_core import compute_risk, get_model_params

        f = self._features(row)
        if not row.features.stations:
            return None
        base = compute_risk(row.features, get_model_params()).score
        return base * self.gate(f.get("h_aval"))


def _transform(name: str, x: np.ndarray) -> np.ndarray:
    """Feature transform used by the logistic models (log1p for rain/flow)."""
    if name in ("api_fast", "api_slow", "rain_24h", "rain_72h", "q_gr4h", "q_eyraud", "q_caudeau", "q_glofas",
                "q_eyraud_lag0"):
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


# ---------------------------------------------------------------------------
# v4-alpha (app.services.risk_v4)
# ---------------------------------------------------------------------------

RUNOFF_FEATURES = ("q_eyraud", "q_caudeau", "q_gr4h", "q_glofas", "q_eyraud_lag0")


@dataclass
class V4AlphaModel(_StatefulModel):
    """v4-alpha score of :func:`app.services.risk_v4.compute_risk_v4` on the candidate features.

    ``runoff_feature`` picks the runoff proxy (one of :data:`RUNOFF_FEATURES`;
    ``params.runoff`` must hold thresholds on that proxy's scale). No
    parameter is fitted: every threshold is in ``params`` (pre-registered).
    """

    params: V4Params
    runoff_feature: str = "q_eyraud"
    name: str = "v4a_joint"
    kind: str = "score"
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORE_THRESHOLDS))

    def __post_init__(self):
        if self.runoff_feature not in RUNOFF_FEATURES:
            raise ValueError(f"unknown runoff feature {self.runoff_feature!r}")
        self.with_gr4h = self.runoff_feature == "q_gr4h"
        self._init_extractor()

    def v4_features(self, f: Mapping[str, Optional[float]]) -> V4Features:
        def val(k: str) -> Optional[float]:
            v = f.get(k)
            return None if v is None or math.isnan(v) else float(v)

        return V4Features(
            backwater_level_m=val("h_aval"),
            confluence_stage_ngf=val("z_conf"),
            runoff_q_m3s=val(self.runoff_feature),
            groundwater_ngf=val("piezo"),
        )

    def breakdown(self, row):
        return compute_risk_v4(self.v4_features(self._features(row)), self.params)

    def score(self, row) -> Optional[float]:
        return self.breakdown(row).score


def _graded_vec(x: np.ndarray, th) -> np.ndarray:
    """Vectorised risk_v4.graded_score (NaN stays NaN)."""
    z, y, o, r = th.as_tuple()
    x = np.asarray(x, dtype=float)
    out = np.zeros_like(x)
    out = np.where((x > z) & (x < y), 15.0 * (x - z) / (y - z), out)
    out = np.where((x >= y) & (x < o), 15.0 + 20.0 * (x - y) / (o - y), out)
    out = np.where((x >= o) & (x < r), 35.0 + 20.0 * (x - o) / (r - o), out)
    out = np.where(x >= r, np.minimum(100.0, 55.0 + 20.0 * (x - r) / (r - o)), out)
    return np.where(np.isnan(x), np.nan, out)


def v4_alpha_scores(table: Mapping[str, np.ndarray], params: V4Params, runoff_feature: str = "q_eyraud") -> np.ndarray:
    """Independent vectorised v4-alpha score over a :func:`feature_table` (NaN = not scored)."""
    h = np.asarray(table["h_aval"], dtype=float)
    if params.backwater_source == "confluence":
        sb = _graded_vec(table["z_conf"], params.confluence)
    else:
        sb = _graded_vec(h, params.backwater)
    sr = _graded_vec(table[runoff_feature], params.runoff) if params.runoff is not None else np.full(len(h), np.nan)
    joint = np.where(np.isnan(sb) | np.isnan(sr), np.nan, np.minimum(sb, sr))
    branches = [joint]
    if params.use_overbank and params.overbank is not None:
        branches.append(_graded_vec(h, params.overbank))
    if params.use_groundwater and params.groundwater is not None:
        branches.append(_graded_vec(table["piezo"], params.groundwater))
    stacked = np.vstack(branches)
    with np.errstate(all="ignore"):
        filled = np.where(np.isnan(stacked), -1.0, stacked)
        best = np.where(np.all(np.isnan(stacked), axis=0), np.nan, np.max(filled, axis=0))
    return np.round(np.minimum(100.0, best), 2)


# ---------------------------------------------------------------------------
# v4-beta (runoff-led, pre-registered 2026-10-27: scripts/eval/preregistration_v4beta.md)
# ---------------------------------------------------------------------------

# OLS on the climatology 2010-09 -> 2022-08 (scripts/eval/runoff_nowcast.py, no label):
# log1p(max Eyraud Q over [t, t + 6 h]) ~ q7, d7, r6, r24, r72, w30, q7*r6, q7*r24
V4B_NOWCAST_INTERCEPT = -0.030654
V4B_NOWCAST_COEF = {"q7": 0.932038, "d7": 0.743835, "r6": 0.033733, "r24": 0.033321, "r72": -0.002936,
                    "w30": 0.008958, "q7_r6": 0.177589, "q7_r24": -0.040915}
# fallback without any Eyraud point (gauge down): rain only
V4B_RAIN_ONLY_INTERCEPT = -0.455674
V4B_RAIN_ONLY_COEF = {"r6": 0.087107, "r24": 0.121413, "r72": 0.101706, "w30": 0.141890}
# variant V1: max over [t, t + 24 h], + forecast rain f24 (perfect prognosis in the fit)
V4B_FORECAST_INTERCEPT = -0.062638
V4B_FORECAST_COEF = {"q7": 0.918883, "d7": 0.754779, "r6": 0.060571, "r24": 0.045927, "r72": -0.004478,
                     "w30": 0.014704, "q7_r6": 0.163133, "q7_r24": -0.095765, "f24": 0.035738,
                     "q7_f24": 0.128150}
# runoff anchors: lowest fractions of Q2 with <= 10 / 5 / 2.5 climatological alerts per year
V4B_RUNOFF_FRACTIONS = (0.10, 0.25, 0.70)
V4B_RUNOFF = GradedThresholds(0.0, *(round(f * EYRAUD_Q2_M3S, 2) for f in V4B_RUNOFF_FRACTIONS))
# overbank on H_aval (Pessac scale, zero 3.33 m NGF): 3.1 km step crest 9.0 m NGF -> mill ground 12.0 m NGF
V4B_STEP_CREST_M = 5.67
V4B_MILL_GROUND_M = 8.67
V4B_OVERBANK = GradedThresholds(V4B_STEP_CREST_M, 6.67, 7.67, V4B_MILL_GROUND_M)
V4B_MOD_MAX_G = 0.95


def _log1p_pos(v: float) -> float:
    return math.log1p(max(0.0, v))


def v4b_regressors(values: Mapping[str, Optional[float]], forecast_rain_mm: Optional[float] = None
                   ) -> dict[str, Optional[float]]:
    """Transformed inputs of the v4-beta regressions (None where a raw input is missing).

    Same transforms as ``scripts.eval.runoff_nowcast.transformed_features``
    (an equivalence test checks it).
    """
    def g(k):
        v = values.get(k)
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

    q_lag7, q_lag10 = g("q_eyraud_m3s"), g("q_eyraud_lag10_m3s")
    r6h, r24h, r72h, r720h = (g(f"era5_rain_{h}h") for h in (6, 24, 72, 720))
    out: dict[str, Optional[float]] = dict.fromkeys(
        ("q7", "d7", "r6", "r24", "r72", "w30", "q7_r6", "q7_r24", "f24", "q7_f24"))
    if None not in (r6h, r24h, r72h, r720h):
        out["r6"] = _log1p_pos(r6h)
        out["r24"] = _log1p_pos(r24h - r6h)
        out["r72"] = _log1p_pos(r72h - r24h)
        out["w30"] = _log1p_pos(r720h - r72h)
    if q_lag7 is not None:
        out["q7"] = _log1p_pos(q_lag7)
        if q_lag10 is not None:
            out["d7"] = out["q7"] - _log1p_pos(q_lag10)
        if out["r6"] is not None:
            out["q7_r6"] = out["q7"] * out["r6"]
            out["q7_r24"] = out["q7"] * out["r24"]
    if forecast_rain_mm is not None:
        out["f24"] = _log1p_pos(forecast_rain_mm)
        if out["q7"] is not None:
            out["q7_f24"] = out["q7"] * out["f24"]
    return out


def _linear(intercept: float, coef: Mapping[str, float], x: Mapping[str, Optional[float]]) -> Optional[float]:
    if any(x.get(k) is None for k in coef):
        return None
    return math.expm1(intercept + sum(c * x[k] for k, c in coef.items()))


def v4b_nowcast_q(x: Mapping[str, Optional[float]]) -> tuple[Optional[float], str]:
    """(Qhat over the next 6 h in Eyraud m3/s, "eyraud" | "rain_only" | "none")."""
    q = _linear(V4B_NOWCAST_INTERCEPT, V4B_NOWCAST_COEF, x)
    if q is not None:
        return q, "eyraud"
    q = _linear(V4B_RAIN_ONLY_INTERCEPT, V4B_RAIN_ONLY_COEF, x)
    return (q, "rain_only") if q is not None else (None, "none")


def v4b_forecast_q(x: Mapping[str, Optional[float]]) -> Optional[float]:
    return _linear(V4B_FORECAST_INTERCEPT, V4B_FORECAST_COEF, x)


def _max_opt(*vals: Optional[float]) -> Optional[float]:
    present = [v for v in vals if v is not None]
    return max(present) if present else None


@dataclass
class V4BetaModel:
    """v4-beta: runoff-led score = max(runoff, [forecast runoff], overbank) (pre-registered).

    ``variant``: ``"primary"`` (v4b), ``"fc"`` (V1, + forecast branch from the
    archived forecast ``fc_rain_24h_prev``), ``"mod"`` (V2, Pessac modulator
    above the 3.1 km step crest), ``"runoff"`` (V3, no Pessac); diagnostics
    ``"obs_only"`` (Eyraud observed with its lag only), ``"nowcast_only"``,
    ``"lag0"`` (Eyraud without publication lag, optimistic). Stateless.
    """

    variant: str = "primary"
    name: str = "v4b"
    kind: str = "score"
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORE_THRESHOLDS))

    VARIANTS = ("primary", "fc", "mod", "runoff", "obs_only", "nowcast_only", "lag0")

    def __post_init__(self):
        if self.variant not in self.VARIANTS:
            raise ValueError(f"unknown v4-beta variant {self.variant!r}")

    def reset(self) -> None:  # stateless
        return None

    def breakdown(self, values: Mapping[str, Optional[float]]) -> dict:
        v = self.variant
        if v == "lag0":
            values = {**values, "q_eyraud_m3s": values.get("q_eyraud_lag0_m3s"),
                      "q_eyraud_max_12h": values.get("q_eyraud_lag0_max_12h"),
                      "q_eyraud_lag10_m3s": values.get("q_eyraud_lag3_m3s")}
        x = v4b_regressors(values, values.get("fc_rain_24h_prev") if v == "fc" else None)
        q_obs = values.get("q_eyraud_max_12h")
        q_now, source = v4b_nowcast_q(x)
        if v == "obs_only":
            q_r = q_obs
        elif v == "nowcast_only":
            q_r = q_now
        else:
            q_r = _max_opt(q_obs, q_now)
        h = values.get("pessac_max_12h")
        g = None
        if v == "mod" and q_r is not None and h is not None:
            g = min(V4B_MOD_MAX_G, max(0.0, (h - V4B_STEP_CREST_M) / (V4B_MILL_GROUND_M - V4B_STEP_CREST_M)))
            q_r = q_r / (1.0 - g)
        runoff = graded_score(q_r, V4B_RUNOFF)
        q_fc = v4b_forecast_q(x) if v == "fc" else None
        forecast = graded_score(q_fc, V4B_RUNOFF)
        overbank = None if v in ("runoff", "obs_only", "nowcast_only") else graded_score(h, V4B_OVERBANK)
        score = _max_opt(runoff, forecast, overbank)
        return {"score": None if score is None else round(min(100.0, score), 2), "runoff": runoff,
                "forecast": forecast, "overbank": overbank, "q_obs": q_obs, "q_nowcast": q_now,
                "q_runoff": q_r, "q_forecast": q_fc, "nowcast_source": source, "modulator_g": g}

    def score(self, row) -> Optional[float]:
        return self.breakdown(row.values)["score"]
