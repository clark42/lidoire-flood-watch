# Pre-registration: v4-beta, a runoff-led flood score for the mill

Registered 2026-10-27, branch `feature/model-v4-beta`, committed on its own BEFORE any v4-beta score is
computed on the evaluation period (2022-11-01 -> 2026-10-25) and before any v4-beta metric. Everything
below is fixed; any later change is an amendment, dated, listed at the end and reported as such.

## 0. What was known before (contamination disclosure)

- The structure is motivated by analyses that used the labels (CLAUDE.md, "Controle physique du remous"
  and `added_value.py`): every flood coincides with the Eyraud discharge peak (median +2 h); the Dordogne
  backwater stops ~3 km below the mill; Pessac adds no out-of-sample skill once Eyraud Q and rain are
  known; per event, the Eyraud max separates the floods (>= 25.9 m3/s) from the labelled negatives
  (<= 25.3 m3/s). The choice of the Eyraud as runoff analogue also came from label-based screening
  (selection bias, the Caudeau did worse). The analyst knows these numbers.
- No threshold, coefficient or switch below is derived from a label. Their sources are: climatology
  strictly before the evaluation period (hydrological years 2010-09 -> 2022-08), the Lidoire long profile
  (RGE ALTI / BD TOPAGE, topography only), Hub'Eau datums and existing config constants.
- Computed before this commit: the climatology regressions and the climatological operating points of
  section 2 (training period only, `python -m scripts.eval.runoff_nowcast --offline`), and the input
  coverage over the evaluation dataset (share of hours with each input, no score, no label).

## 1. Inputs at hour t (all causal)

| Name | Definition | Source, lag |
|---|---|---|
| `Q_obs` | max of the Eyraud discharge points in [t - 7 h - 12.5 h, t - 7 h] (`q_eyraud_max_12h`) | Hydroportail variable-step Q, P540401002, 7 h publication lag |
| `q7` | log1p(last Eyraud point <= t - 7 h, max age 12 h) | same |
| `d7` | q7 - log1p(last Eyraud point <= t - 10 h, max age 12 h) (rise) | same |
| `r6`, `r24`, `r72`, `w30` | log1p of ERA5 basin rain over (t-6, t], (t-24, t-6], (t-72, t-24], (t-720, t-72] | Open-Meteo archive (ERA5), mean of the 3 `lidoire_basin_points`, labels <= t |
| `H_aval` | max of Pessac P555001001 over the 12.5 h ending at t | Hydroportail H, no lag |
| `f24` (variant V1 only) | log1p of the forecast basin rain over (t, t + 24 h] issued before t | Open-Meteo Previous Runs, `meteofrance_seamless`, see section 5 |

ERA5 stands for observed basin rain (radar or gauges in production). It is used in the fit AND in the
evaluation so that both see the same rain product.

## 2. Model structure (primary, `v4b`)

### 2.1 Runoff nowcast (Eyraud-equivalent discharge over the next 6 h)

`Qhat_now = expm1(b0 + b . x)`, `x = (q7, d7, r6, r24, r72, w30, q7*r6, q7*r24)`, ordinary least squares
of `log1p(max Eyraud Q over [t, t + 6 h])` on the hours 2010-09-01 00:00 -> 2022-08-31 23:00 with
complete inputs (n = 100 894, R2 = 0.912, residual sd 0.126 log units). Coefficients (fixed):

| term | coef |
|---|---|
| intercept | -0.030654 |
| q7 | 0.932038 |
| d7 | 0.743835 |
| r6 | 0.033733 |
| r24 | 0.033321 |
| r72 | -0.002936 |
| w30 | 0.008958 |
| q7*r6 | 0.177589 |
| q7*r24 | -0.040915 |

Fallback when `q7` or `d7` is missing (Eyraud gauge down): rain-only regression on the same target and
hours, `x = (r6, r24, r72, w30)` (n = 104 025, R2 = 0.304): intercept -0.455674, r6 0.087107,
r24 0.121413, r72 0.101706, w30 0.141890.

Antecedent wetness enters through `q7` (Eyraud baseflow, the catchment state) and `w30` (30-day rain).

### 2.2 Runoff hazard and score

`Q_R = max(Q_obs, Qhat_now)` (one of them if the other is missing; none if both are missing).

`R = graded(Q_R; zero 0, yellow 0.10 Q2, orange 0.25 Q2, red 0.70 Q2)` with
`graded` = `app.services.risk_v4.graded_score` (0 / 15 / 35 / 55 at the anchors, linear, capped 100)
and Q2 = 18.89 m3/s (Eyraud, median of the hydrological-year maxima of QIXnJ 2010-11 -> 2021-22, the
pre-registered v4-alpha constant). Anchors in m3/s: 1.89 / 4.72 / 13.22.

Anchor derivation (label-free, climatology 2010-09 -> 2022-08, 12.0 years): for each fraction f of Q2
in the fixed grid {0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.70, 1.00}, alerts = runs of
`Q_R >= f Q2` merged at 12 h; the yellow / orange / red anchor is the LOWEST fraction whose
climatological alert rate is <= 10 / 5 / 2.5 per year (upper end, lower end and half the lower end of
the owner's 5-10 alerts/yr budget; lowest = longest lead and fewest misses within the budget).
Pseudo-events for reading only: runs of the instantaneous Eyraud Q >= Q2 merged at 72 h (15 events).

| f x Q2 | m3/s | alerts/yr | Q2 events hit | lead median / min before the Eyraud peak (h) |
|---|---|---|---|---|
| 0.10 | 1.89 | 8.2 | 15/15 | 80 / 4 |
| 0.15 | 2.83 | 7.0 | 15/15 | 53 / 0 |
| 0.20 | 3.78 | 5.8 | 15/15 | 30 / -1 |
| 0.25 | 4.72 | 4.9 | 15/15 | 24 / -1 |
| 0.30 | 5.67 | 4.4 | 15/15 | 19 / -1 |
| 0.35 | 6.61 | 4.3 | 15/15 | 13 / -1 |
| 0.40 | 7.56 | 4.0 | 15/15 | 13 / -2 |
| 0.50 | 9.45 | 3.5 | 15/15 | 6 / -2 |
| 0.70 | 13.22 | 2.4 | 15/15 | 2 / -2 |
| 1.00 | 18.89 | 1.9 | 14/15 | 1.5 / -2 |

### 2.3 Overbank branch (Pessac only at very high stage)

`O = graded(H_aval; zero 5.67, yellow 6.67, orange 7.67, red 8.67 m)` (Pessac scale, zero 3.33 m NGF;
Pessac taken as an upper bound of the confluence stage). Physics: 5.67 m = 9.0 m NGF, crest of the step
at 3.1 km from the confluence, below which the backwater is held; 8.67 m = 12.0 m NGF, the mill ground
(a flat backwater reaches the mill); 7.67 m = 11.0 m NGF, the foot of the step at 5.3 km just below
the mill; yellow halfway. Below 5.67 m Pessac does not enter the score at all.

### 2.4 Score

`score = max(R, O)` over the available branches (unscored hour if none), levels green < 15 <= yellow
< 35 <= orange < 55 <= red. Hazard meaning: an Eyraud-class runoff peak expected within ~6 h (the
target window), i.e. ~6-13 h after the last published Eyraud point.

## 3. Variants (3, each changes one thing)

- **V1 `v4b_fc`** (lead from forecast rain): adds `F = graded(Qhat_fc; same anchors)`, `Qhat_fc` = the
  same regression form with `f24` and `q7*f24`, target `log1p(max Eyraud Q over [t, t + 24 h])`, fitted
  on the same hours with the OBSERVED ERA5 rain over (t, t + 24 h] (perfect prognosis; n = 101 327,
  R2 = 0.832): intercept -0.062638, q7 0.918883, d7 0.754779, r6 0.060571, r24 0.045927, r72 -0.004478,
  w30 0.014704, q7*r6 0.163133, q7*r24 -0.095765, f24 0.035738, q7*f24 0.128150. Applied with the
  archived forecast; `score = max(R, F, O)`; where the archive does not cover the window (before
  2024-01-19 15:00 UTC or a missing hour) F is absent and the score equals the primary. Also reported on
  the sub-period 2024-01-20 -> 2026-10-25 (3 floods) against the primary and v3.4 red.
- **V2 `v4b_mod`** (Pessac modulator): `Q_R` replaced by `Q_R / (1 - g)`,
  `g = clip((H_aval - 5.67) / (8.67 - 5.67), 0, 0.95)`: the runoff anchors shrink linearly as the drowned
  backwater uses up the freeboard between the 3.1 km crest and the mill ground. Overbank kept.
- **V3 `v4b_runoff`** (no Pessac at all): `score = R`.

Diagnostics, reported but NOT eligible for the decision: Eyraud observed only (`Q_R = Q_obs`), nowcast
only (`Q_R = Qhat_now`), Eyraud without publication lag (optimistic bound).

## 4. Primary metric, operating point and decision rule

Operating point: v4-beta **orange** (score >= 35, i.e. `Q_R >= 4.72 m3/s` or `H_aval >= 7.67 m`), the
level meant for ntfy, compared with **v3.4 red** (>= 55), the current recommendation. Alert runs merged
at 12 h, labels v2 (5 floods, 9 labelled negatives), lead = peak - alert start against the owner's
hour-precise peaks, harness windows (alert start in [peak - 72 h, peak + 6 h], or ongoing).

Runoff floods: 2023-11-30, 2024-02-26, 2025-04-21, 2026-02-12. The 2023-03-11 flood (infiltration
through the floor, sealed since) is reported but not required.

Primary metric: median lead over the 4 runoff floods, a miss counting as a failure.

**v4-beta beats v3.4 red** if and only if ALL hold for the primary `v4b` at orange:

1. Budget: <= 10 alerts per year (merged at 12 h).
2. No miss: the 4 runoff floods are detected.
3. Useful lead: lead >= 6 h on each of the 4 runoff floods.
4. Trade-off against v3.4 red recomputed in the same run (published values: 6.3 alerts/yr, leads
   12 / 19 / 18 / 51 h, median 18.5 h on the 4 runoff floods, 20 raw false alarms merged):
   (a) median lead >= v3.4 red's AND raw false alarms (merged) <= v3.4 red's, OR
   (b) median lead >= v3.4 red's - 6 h AND raw false alarms (merged) <= 0.75 x v3.4 red's.

Consequences: pass -> a pure `app/services/risk_v4b.py`, NOT wired into production (guard test), candidate
for a shadow series; never a production switch on 4-5 floods. Fail -> no module. A variant that
passes while the primary fails is an exploratory hypothesis for a new pre-registration, not a pass.

Also reported (not decisive): every column of the CLAUDE.md tables (orange and red, unmerged and merged,
POD with Clopper-Pearson 95% CI, FA labelled / raw, FAR, CSI, alerts/yr, AUC with block-bootstrap 90%
CI), lead per flood, and the budget study rows `v4b >= 15 / 25 / 35 / 45 / 55`, `v4b_fc >= 35`,
`v3.4 >= 55 OR v4b >= 35`, `v3.4 >= 35 AND v4b >= 35`, with the ntfy simulation (hold 3 h / 12 h).

## 5. Leakage controls and known residual leakage

- Eyraud: points <= t - 7 h only (publication lag), training and evaluation alike.
- ERA5: labels <= t; in reality ERA5 is published ~5 days late, so the evaluation assumes an observed
  basin rain of ERA5 quality in real time (OPERA radar or gauges in production; distribution shift).
- Forecast rain (V1): `previous_dayN` at label T comes from a run initialised >= N x 24 h before T.
  Labels t+1 .. t+20 use day 1 (published by T - 20 h <= t if the run is out within 4 h, true for
  AROME/ARPEGE on Open-Meteo), labels t+21 .. t+24 use day 2. A run published more than 4 h after its
  initialisation would leak a few hours; no observed future rain is ever used outside the fit.
- Fit: training hours stop on 2022-08-31 23:00 (targets and the perfect-prognosis rain are read up to
  2022-09-01 23:00, a month before the evaluation period); the climatology alert rates use the same
  hours. No label anywhere.
- Residual, not removable: the structure and the Eyraud were chosen after label-based analyses
  (section 0); the long-profile anchors were drawn by the analyst who knew the flood stages of Pessac;
  5 floods (4 runoff floods): one flood = 20-25 points of POD, nothing here is significant.
