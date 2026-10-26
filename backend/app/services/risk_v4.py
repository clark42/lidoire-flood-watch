"""Model v4-alpha ("4.0a"): mechanism-based flood risk at the mill (pure, no I/O, no utcnow()).

NOT wired into production: no route, job or service imports this module
yet (a test enforces it; only the offline candidate wrapper does). It is
meant to be stored later as a shadow series ``4.0a`` once the hydro
collectors (Eyraud discharge, Mouliets piezometer) feed its inputs.

Physical model (research 2026-10-26): the mill sits on the Lidoire just
upstream of its confluence with the tidal Dordogne. It floods through:

1. **backwater + runoff (joint)**: the Dordogne holds the confluence high
   (backwater) WHILE the Lidoire carries a flood (runoff). Neither alone is
   enough: a high Dordogne without rain (2024-05-09) or a Lidoire flood on
   a low Dordogne does not flood the mill. Score = min(backwater, runoff).
2. **overbank**: a rare Dordogne flood reaches the mill directly, whatever
   the Lidoire does (2026-02-14, Pessac record 7.88 m).
3. **groundwater** (optional): the alluvial water table rises through the
   floor (2023-03-11 infiltration flood, since sealed: off by default).

Inputs (:class:`V4Features`), all causal (points <= t, after the
publication delay of each source):

- ``backwater_level_m``: H_aval, max of Pessac over the trailing 12.5 h
  (``risk_core.backwater_level``, scale m), or ``confluence_stage_ngf``
  (:func:`confluence_stage`, m NGF) when ``params.backwater_source ==
  "confluence"``;
- ``runoff_q_m3s``: Lidoire runoff proxy, by default the 12.5 h max of the
  Eyraud discharge (P540401002, 73.5 km2 right-bank tributary 28 km
  upstream, no gauge on the Lidoire itself) ending 7 h before t;
- ``groundwater_ngf``: Mouliets-et-Villemartin alluvial piezometer
  (08048X0088/P), daily, m NGF.

Each mechanism maps its input to 0-100 with :func:`graded_score` (0 at the
``zero`` anchor, 15 / 35 / 55 at the yellow / orange / red thresholds,
linear in between and beyond red, capped at 100). The final score is
``max(min(backwater, runoff), overbank[, groundwater])`` over the available
mechanisms; levels green < 15 <= yellow < 35 <= orange < 55 <= red.

Pre-registered thresholds (fixed 2026-10-26 BEFORE any v4 evaluation
metric was computed; no label is used anywhere):

- backwater: Pessac config thresholds 3.0 / 3.8 / 4.5 m (zero 0 m), the
  same constants as the v3.4 tide gate and the 3.4-gate shadow;
- runoff: 0.4 / 0.7 / 1.0 x Q2 (zero 0), Q2 = median of the
  hydrological-year (Sep-Aug) maxima of the daily max discharge (Hub'Eau
  QIXnJ, years with >= 300 days) ending 2022-08, i.e. strictly before the
  evaluation period; 0.7 Q2 ~ bankfull (1-1.5 year flood);
- overbank: Pessac T-year levels from the HIXnJ hydrological-year maxima
  ending 2022-08 (empirical quantile 1 - 1/T): yellow T = 2, orange T = 5,
  red T = 10 (zero 0 m), on H_aval;
- groundwater: percentiles 50 (zero) / 90 / 97 / 99.5 of the daily level
  2007-06 -> 2022-09 (same 90 / 97 / 99.5 convention as the other
  label-free candidates);
- confluence variant: Z_c = 0.8 (Pessac + 3.33) + 0.2 (Libourne - 1.17)
  m NGF (alpha = 0.2 from the river distances, Hub'Eau datums), thresholds
  = the Z_c values at the same hourly quantile ranks as the Pessac
  thresholds in the H_aval distribution (quantile mapping, label-free).

The numbers below are computed by ``scripts/eval/climatology.py`` from the
cached archives (a test checks them when the cache is present).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from app.models.schemas import RiskLevel

MODEL_VERSION_V4 = "4.0a"

# Hub'Eau referentiel datums (m NGF) and the confluence weighting
PESSAC_ZERO_NGF = 3.33
LIBOURNE_ZERO_NGF = -1.17
CONFLUENCE_ALPHA = 0.2  # ~7 km Pessac -> confluence out of ~37 km Pessac -> Libourne

LEVEL_BOUNDS = (15.0, 35.0, 55.0)
MECHANISMS = ("backwater", "runoff", "overbank", "groundwater")


@dataclass(frozen=True)
class GradedThresholds:
    """(zero, yellow, orange, red) anchors of :func:`graded_score`."""

    zero: float
    yellow: float
    orange: float
    red: float

    def __post_init__(self):
        if not self.zero < self.yellow < self.orange < self.red:
            raise ValueError(f"thresholds must increase: {self}")

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.zero, self.yellow, self.orange, self.red)


def graded_score(value: Optional[float], th: GradedThresholds) -> Optional[float]:
    """0 at/below ``zero``, 15 / 35 / 55 at yellow / orange / red, linear, capped 100."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if value <= th.zero:
        return 0.0
    if value < th.yellow:
        return 15.0 * (value - th.zero) / (th.yellow - th.zero)
    if value < th.orange:
        return 15.0 + 20.0 * (value - th.yellow) / (th.orange - th.yellow)
    if value < th.red:
        return 35.0 + 20.0 * (value - th.orange) / (th.red - th.orange)
    return min(100.0, 55.0 + 20.0 * (value - th.red) / (th.red - th.orange))


def confluence_stage(
    pessac_env_m: Optional[float],
    libourne_env_m: Optional[float],
    alpha: float = CONFLUENCE_ALPHA,
) -> Optional[float]:
    """Z_c (m NGF) from the 12.5 h envelopes of Pessac and Libourne (scale m)."""
    if pessac_env_m is None or libourne_env_m is None:
        return None
    return (1.0 - alpha) * (pessac_env_m + PESSAC_ZERO_NGF) + alpha * (libourne_env_m + LIBOURNE_ZERO_NGF)


def score_level(score: float) -> RiskLevel:
    yellow, orange, red = LEVEL_BOUNDS
    if score < yellow:
        return RiskLevel.GREEN
    if score < orange:
        return RiskLevel.YELLOW
    if score < red:
        return RiskLevel.ORANGE
    return RiskLevel.RED


# ---------------------------------------------------------------------------
# Pre-registered thresholds (see module docstring; scripts/eval/climatology.py)
# ---------------------------------------------------------------------------

BACKWATER_PESSAC_M = GradedThresholds(0.0, 3.0, 3.8, 4.5)
# Q2 Eyraud (P540401002, QIXnJ, 12 hydrological years 2010-11 -> 2021-22)
EYRAUD_Q2_M3S = 18.89
# Q2 Caudeau (P524401001, QIXnJ, 12 hydrological years 2010-11 -> 2021-22)
CAUDEAU_Q2_M3S = 9.64
# Q2 GloFAS v4 Lidoire cell (consolidated_v4 daily, 25 hydrological years 1997-98 -> 2021-22)
GLOFAS_Q2_M3S = 7.22
# Pessac HIXnJ T-year levels (T = 2, 5, 10), 26 hydrological years 1996-97 -> 2021-22
PESSAC_T_YEAR_LEVELS_M = (5.13, 6.25, 6.54)
# Mouliets 08048X0088/P daily level percentiles 50 / 90 / 97 / 99.5, 2007-06-27 -> 2022-09-30
PIEZO_PERCENTILES_NGF = (6.31, 6.76, 6.99, 7.24)
# Z_c at the hourly quantile ranks of 3.0 / 3.8 / 4.5 m in H_aval (2022-11-01 -> 2026-10-25)
CONFLUENCE_THRESHOLDS_NGF = (5.79, 6.46, 7.06)


def runoff_thresholds(q2: float) -> GradedThresholds:
    """0.4 / 0.7 / 1.0 x Q2 (zero 0)."""
    return GradedThresholds(0.0, 0.4 * q2, 0.7 * q2, q2)


@dataclass(frozen=True)
class V4Params:
    """Mechanism thresholds and switches (defaults = the pre-registered v4-alpha)."""

    backwater_source: str = "pessac"  # "pessac" (H_aval) | "confluence" (Z_c)
    backwater: GradedThresholds = BACKWATER_PESSAC_M
    confluence: Optional[GradedThresholds] = None
    runoff_source: str = "eyraud"  # provenance label of runoff_q_m3s
    runoff: Optional[GradedThresholds] = None
    overbank: Optional[GradedThresholds] = None
    groundwater: Optional[GradedThresholds] = None
    use_overbank: bool = True
    use_groundwater: bool = False
    model_version: str = MODEL_VERSION_V4

    def __post_init__(self):
        if self.backwater_source not in ("pessac", "confluence"):
            raise ValueError(f"unknown backwater_source {self.backwater_source!r}")


def default_v4_params(**overrides) -> V4Params:
    """Pre-registered v4-alpha parameters (Eyraud runoff, Pessac backwater, overbank, no groundwater)."""
    base = dict(
        runoff=runoff_thresholds(EYRAUD_Q2_M3S),
        overbank=GradedThresholds(0.0, *PESSAC_T_YEAR_LEVELS_M),
        groundwater=GradedThresholds(*PIEZO_PERCENTILES_NGF),
        confluence=GradedThresholds(0.0, *CONFLUENCE_THRESHOLDS_NGF),
    )
    base.update(overrides)
    return V4Params(**base)


# ---------------------------------------------------------------------------
# Features and breakdown
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V4Features:
    """Causal inputs of the v4-alpha score at one hour (None = unavailable)."""

    backwater_level_m: Optional[float] = None
    confluence_stage_ngf: Optional[float] = None
    runoff_q_m3s: Optional[float] = None
    groundwater_ngf: Optional[float] = None


@dataclass(frozen=True)
class MechanismScore:
    name: str
    input_value: Optional[float]
    score: Optional[float]
    thresholds: Optional[tuple[float, float, float, float]]
    enabled: bool = True

    @property
    def available(self) -> bool:
        return self.enabled and self.score is not None


@dataclass(frozen=True)
class V4Breakdown:
    score: Optional[float]
    level: Optional[RiskLevel]
    joint_score: Optional[float]
    dominant: Optional[str]  # "joint" | "overbank" | "groundwater"
    mechanisms: dict[str, MechanismScore]
    unavailable: tuple[str, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)
    model_version: str = MODEL_VERSION_V4

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "level": self.level.value if self.level else None,
            "joint_score": self.joint_score,
            "dominant": self.dominant,
            "mechanisms": {
                k: {"input": m.input_value, "score": m.score, "thresholds": m.thresholds, "enabled": m.enabled}
                for k, m in self.mechanisms.items()
            },
            "unavailable": list(self.unavailable),
            "notes": list(self.notes),
            "model_version": self.model_version,
        }


def _mechanism(
    name: str, value: Optional[float], th: Optional[GradedThresholds], enabled: bool = True,
) -> MechanismScore:
    if not enabled or th is None:
        return MechanismScore(name, value, None, th.as_tuple() if th else None, enabled=False)
    return MechanismScore(name, value, graded_score(value, th), th.as_tuple())


def compute_risk_v4(features: V4Features, params: Optional[V4Params] = None) -> V4Breakdown:
    """v4-alpha score and per-mechanism breakdown (pure).

    The joint branch needs both backwater and runoff; the final score is the
    max of the available enabled branches, None when none is available.
    """
    p = params or default_v4_params()
    notes: list[str] = []
    if p.backwater_source == "confluence":
        if p.confluence is None:
            raise ValueError("confluence thresholds required for backwater_source='confluence'")
        bw = _mechanism("backwater", features.confluence_stage_ngf, p.confluence)
        notes.append("backwater = confluence stage Z_c (m NGF)")
    else:
        bw = _mechanism("backwater", features.backwater_level_m, p.backwater)
    ro = _mechanism("runoff", features.runoff_q_m3s, p.runoff)
    ob = _mechanism("overbank", features.backwater_level_m, p.overbank, p.use_overbank)
    gw = _mechanism("groundwater", features.groundwater_ngf, p.groundwater, p.use_groundwater)
    mechanisms = {"backwater": bw, "runoff": ro, "overbank": ob, "groundwater": gw}
    unavailable = tuple(k for k, m in mechanisms.items() if m.enabled and m.score is None)

    joint = min(bw.score, ro.score) if bw.available and ro.available else None
    if joint is None and (bw.available or ro.available):
        notes.append("joint branch unavailable: " + ", ".join(k for k in ("backwater", "runoff")
                                                              if not mechanisms[k].available))
    branches = [("joint", joint), ("overbank", ob.score if ob.available else None),
                ("groundwater", gw.score if gw.available else None)]
    present = [(k, v) for k, v in branches if v is not None]
    if not present:
        return V4Breakdown(None, None, joint, None, mechanisms, unavailable, tuple(notes), p.model_version)
    dominant, score = max(present, key=lambda kv: kv[1])
    score = round(min(100.0, score), 2)
    return V4Breakdown(score, score_level(score), joint, dominant, mechanisms, unavailable, tuple(notes),
                       p.model_version)
