"""Event-based contingency: alert episodes vs labelled flood peaks.

- Alert episode: maximal run of consecutive hours with score >= threshold
  (optionally merging runs separated by <= ``merge_gap_h`` hours).
- Match: an alert matches an event when it starts within
  [peak - lead_window_h, peak + lag_window_h] (default 72h / 6h). With
  ``allow_ongoing`` (default) an alert that started earlier and is still on
  when the window opens also matches (lead time then capped at
  ``lead_window_h`` and flagged ``ongoing``): otherwise one long alert
  covering two close floods would count one of them as a miss.
- Hit: flooded event with >= 1 matching alert. Miss: flooded event without.
- Alerts are classified: ``hit`` (matches a flooded event), ``excluded``
  (matches only an unknown event), ``labelled_negative`` (matches only
  near_miss/false_positive events), ``unlabelled`` (matches nothing) and
  ``outside_period`` (starts outside the annotated period: not judged).
- FAR_raw = (labelled_negative + unlabelled) / (hits + those);
  FAR_labelled = labelled_negative / (hits + labelled_negative): the labels
  are not exhaustive, so unlabelled alerts may be unannotated floods.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from scripts.eval.truth import EvalEvent, LabelPeriod, event_window

HOUR = timedelta(hours=1)
HOURS_PER_YEAR = 8766.0


@dataclass(frozen=True)
class AlertEpisode:
    start: datetime
    end: datetime  # last hour at/above threshold
    peak_time: datetime
    peak_score: float
    n_hours: int

    @property
    def duration_h(self) -> float:
        return (self.end - self.start).total_seconds() / 3600 + 1


def alert_episodes(
    times: Sequence[datetime],
    scores: Sequence[Optional[float]],
    threshold: float,
    merge_gap_h: int = 0,
) -> list[AlertEpisode]:
    """Alert episodes from an hourly score series (None = no score = no alert)."""
    runs: list[list[tuple[datetime, float]]] = []
    current: list[tuple[datetime, float]] = []
    for t, s in zip(times, scores):
        if s is not None and s >= threshold:
            # a gap longer than merge_gap_h hours (or a missing hour) ends the run
            if current and t - current[-1][0] > HOUR * (merge_gap_h + 1):
                runs.append(current)
                current = []
            current.append((t, float(s)))
        elif current and merge_gap_h == 0:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    episodes = []
    for run in runs:
        peak_t, peak_s = max(run, key=lambda x: x[1])
        episodes.append(AlertEpisode(
            start=run[0][0], end=run[-1][0], peak_time=peak_t, peak_score=peak_s, n_hours=len(run),
        ))
    return episodes


@dataclass(frozen=True)
class ContingencyConfig:
    lead_window_h: float = 72.0
    lag_window_h: float = 6.0
    allow_ongoing: bool = True
    merge_gap_h: int = 0


@dataclass
class EventResult:
    key: str
    outcome: str
    positive: Optional[bool]
    peak_time: datetime
    matched: bool
    first_alert_start: Optional[datetime] = None
    lead_time_h: Optional[float] = None  # peak - alert start (capped if ongoing)
    ongoing: bool = False
    max_alert_score: Optional[float] = None


@dataclass
class AlertResult:
    alert: AlertEpisode
    category: str  # hit | excluded | labelled_negative | unlabelled | outside_period
    matched_keys: list[str] = field(default_factory=list)


def season_of(t: datetime) -> str:
    """Hydrological season Oct->Sep, e.g. 2023-12-01 -> '2023-24'."""
    y = t.year if t.month >= 10 else t.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


@dataclass
class ContingencyResult:
    threshold: float
    hits: int
    misses: int
    false_alarms_raw: int
    false_alarms_labelled: int
    n_alerts: int
    n_alerts_judged: int
    pod: Optional[float]
    far_raw: Optional[float]
    far_labelled: Optional[float]
    csi_raw: Optional[float]
    csi_labelled: Optional[float]
    median_lead_h: Optional[float]
    min_lead_h: Optional[float]
    lead_times_h: list[float]
    alerts_per_season: dict[str, int]
    alerts_per_year: Optional[float]
    alert_hours_share: Optional[float]  # share of replayed hours at/above threshold
    events: list[EventResult]
    alerts: list[AlertResult]
    config: ContingencyConfig

    def summary(self) -> dict:
        """Scalar metrics only."""
        keys = (
            "threshold", "hits", "misses", "false_alarms_raw", "false_alarms_labelled",
            "n_alerts", "n_alerts_judged", "pod", "far_raw", "far_labelled", "csi_raw",
            "csi_labelled", "median_lead_h", "min_lead_h", "alerts_per_year", "alert_hours_share",
        )
        out = {k: getattr(self, k) for k in keys}
        out["alerts_per_season"] = dict(self.alerts_per_season)
        return out

    def to_dict(self) -> dict:
        out = self.summary()
        out["lead_times_h"] = list(self.lead_times_h)
        out["config"] = asdict(self.config)
        out["events"] = [
            {**asdict(e), "peak_time": e.peak_time.isoformat(),
             "first_alert_start": e.first_alert_start.isoformat() if e.first_alert_start else None}
            for e in self.events
        ]
        out["alerts"] = [
            {
                "start": a.alert.start.isoformat(),
                "end": a.alert.end.isoformat(),
                "peak_time": a.alert.peak_time.isoformat(),
                "peak_score": a.alert.peak_score,
                "n_hours": a.alert.n_hours,
                "category": a.category,
                "matched_keys": a.matched_keys,
            }
            for a in self.alerts
        ]
        return out

    @property
    def unlabelled_alerts(self) -> list[AlertResult]:
        return [a for a in self.alerts if a.category == "unlabelled"]


def _ratio(num: float, den: float) -> Optional[float]:
    return num / den if den > 0 else None


def match_alert(alert: AlertEpisode, event: EvalEvent, config: ContingencyConfig) -> Optional[tuple[float, bool]]:
    """(lead time h, ongoing) when ``alert`` matches ``event``, else None."""
    w_start, w_end = event_window(event, config.lead_window_h, config.lag_window_h)
    if w_start <= alert.start <= w_end:
        return (event.peak_time - alert.start).total_seconds() / 3600, False
    if config.allow_ongoing and alert.start < w_start <= alert.end:
        return config.lead_window_h, True
    return None


def evaluate_alerts(
    alerts: Sequence[AlertEpisode],
    events: Sequence[EvalEvent],
    threshold: float,
    config: ContingencyConfig = ContingencyConfig(),
    period: Optional[LabelPeriod] = None,
    seasons: Sequence[str] = (),
    n_hours: Optional[int] = None,
) -> ContingencyResult:
    """Contingency table from alert episodes and labelled events.

    ``seasons`` pre-fills the per-season alert counts (seasons without any
    alert then show 0); ``n_hours`` (replayed hours) gives alerts_per_year.
    """
    events_in = [e for e in events if period is None or period.contains(e.peak_time)]

    event_results: dict[str, EventResult] = {
        e.key: EventResult(e.key, e.outcome.value, e.positive, e.peak_time, matched=False)
        for e in events_in
    }
    alert_results: list[AlertResult] = []
    for alert in alerts:
        matched: list[tuple[EvalEvent, float, bool]] = []
        for ev in events_in:
            m = match_alert(alert, ev, config)
            if m is not None:
                matched.append((ev, m[0], m[1]))
        for ev, lead, ongoing in matched:
            er = event_results[ev.key]
            if not er.matched or (er.first_alert_start and alert.start < er.first_alert_start):
                er.matched = True
                er.first_alert_start = alert.start
                er.lead_time_h = lead
                er.ongoing = ongoing
            er.max_alert_score = max(er.max_alert_score or 0.0, alert.peak_score)

        keys = [ev.key for ev, _, _ in matched]
        if any(ev.positive is True for ev, _, _ in matched):
            category = "hit"
        elif any(ev.positive is None for ev, _, _ in matched):
            category = "excluded"
        elif matched:
            category = "labelled_negative"
        elif period is not None and not period.contains(alert.start):
            category = "outside_period"
        else:
            category = "unlabelled"
        alert_results.append(AlertResult(alert, category, keys))

    positives = [event_results[e.key] for e in events_in if e.positive is True]
    hits = sum(1 for r in positives if r.matched)
    misses = len(positives) - hits
    fa_lab = sum(1 for a in alert_results if a.category == "labelled_negative")
    fa_unl = sum(1 for a in alert_results if a.category == "unlabelled")
    fa_raw = fa_lab + fa_unl
    leads = sorted(r.lead_time_h for r in positives if r.matched and r.lead_time_h is not None)

    per_season: dict[str, int] = {k: 0 for k in seasons}
    for a in alerts:
        per_season[season_of(a.start)] = per_season.get(season_of(a.start), 0) + 1

    return ContingencyResult(
        threshold=threshold,
        hits=hits,
        misses=misses,
        false_alarms_raw=fa_raw,
        false_alarms_labelled=fa_lab,
        n_alerts=len(alerts),
        n_alerts_judged=sum(1 for a in alert_results if a.category != "outside_period"),
        pod=_ratio(hits, hits + misses),
        far_raw=_ratio(fa_raw, hits + fa_raw),
        far_labelled=_ratio(fa_lab, hits + fa_lab),
        csi_raw=_ratio(hits, hits + misses + fa_raw),
        csi_labelled=_ratio(hits, hits + misses + fa_lab),
        median_lead_h=statistics.median(leads) if leads else None,
        min_lead_h=leads[0] if leads else None,
        lead_times_h=leads,
        alerts_per_season=dict(sorted(per_season.items())),
        alerts_per_year=len(alerts) * HOURS_PER_YEAR / n_hours if n_hours else None,
        alert_hours_share=sum(a.n_hours for a in alerts) / n_hours if n_hours else None,
        events=list(event_results.values()),
        alerts=alert_results,
        config=config,
    )


def evaluate_scores(
    times: Sequence[datetime],
    scores: Sequence[Optional[float]],
    events: Sequence[EvalEvent],
    threshold: float,
    config: ContingencyConfig = ContingencyConfig(),
    period: Optional[LabelPeriod] = None,
) -> ContingencyResult:
    """Alert episodes from hourly scores, then :func:`evaluate_alerts`."""
    alerts = alert_episodes(times, scores, threshold, config.merge_gap_h)
    seasons = sorted({season_of(t) for t in times})
    return evaluate_alerts(alerts, events, threshold, config, period, seasons, len(times))
