"""Event-based contingency: alert episodes, hits, misses, false alarms, lead time."""

from datetime import datetime, timedelta

import pytest

from app.models.schemas import EpisodeOutcome
from scripts.eval.contingency import (
    AlertEpisode,
    ContingencyConfig,
    alert_episodes,
    evaluate_alerts,
    evaluate_scores,
    season_of,
)
from scripts.eval.truth import EvalEvent, LabelPeriod, binary_outcome

T0 = datetime(2024, 1, 1)
H = timedelta(hours=1)


def _event(key_time: datetime, outcome: EpisodeOutcome) -> EvalEvent:
    return EvalEvent(
        key=key_time.date().isoformat(),
        peak_time=key_time,
        outcome=outcome,
        positive=binary_outcome(outcome),
        severity=3,
    )


def _alert(start: datetime, hours: int, peak: float = 60.0) -> AlertEpisode:
    return AlertEpisode(start=start, end=start + (hours - 1) * H, peak_time=start,
                        peak_score=peak, n_hours=hours)


def test_evaluation_alert_episodes_contiguous_runs():
    times = [T0 + i * H for i in range(10)]
    scores = [10, 40, 50, 20, None, 36, 36, 34, 35, 80]
    eps = alert_episodes(times, scores, 35)
    assert [(e.start, e.end, e.n_hours) for e in eps] == [
        (T0 + 1 * H, T0 + 2 * H, 2),
        (T0 + 5 * H, T0 + 6 * H, 2),
        (T0 + 8 * H, T0 + 9 * H, 2),
    ]
    assert eps[0].peak_score == 50 and eps[0].peak_time == T0 + 2 * H
    assert eps[2].peak_score == 80


def test_evaluation_alert_episodes_threshold_inclusive_and_none_breaks():
    times = [T0 + i * H for i in range(4)]
    assert len(alert_episodes(times, [35, None, 35, 35], 35)) == 2
    assert alert_episodes(times, [None] * 4, 35) == []


def test_evaluation_alert_episodes_missing_hour_breaks_run():
    times = [T0, T0 + H, T0 + 3 * H]  # hour 2 missing from the grid
    eps = alert_episodes(times, [50, 50, 50], 35)
    assert len(eps) == 2


def test_evaluation_alert_episodes_merge_gap():
    times = [T0 + i * H for i in range(12)]
    scores = [50, 50, 0, 0, 50, 0, 0, 0, 0, 50, 0, 0]
    assert len(alert_episodes(times, scores, 35, merge_gap_h=0)) == 3
    merged = alert_episodes(times, scores, 35, merge_gap_h=2)
    assert [(e.start, e.end) for e in merged] == [(T0, T0 + 4 * H), (T0 + 9 * H, T0 + 9 * H)]
    assert merged[0].n_hours == 3  # only hours above threshold are counted
    assert len(alert_episodes(times, scores, 35, merge_gap_h=4)) == 1


def test_evaluation_hit_miss_false_alarm_and_lead_time():
    flood1 = _event(datetime(2024, 1, 10, 12), EpisodeOutcome.FLOODED)
    flood2 = _event(datetime(2024, 2, 10, 12), EpisodeOutcome.FLOODED)  # missed
    negative = _event(datetime(2024, 3, 1, 12), EpisodeOutcome.NEAR_MISS)
    alerts = [
        _alert(datetime(2024, 1, 9, 0), 40),    # starts 36h before flood1 -> hit
        _alert(datetime(2024, 3, 1, 0), 5),     # on the near miss -> labelled negative
        _alert(datetime(2024, 4, 15, 0), 3),    # nothing labelled -> unlabelled
    ]
    res = evaluate_alerts(alerts, [flood1, flood2, negative], 35)
    assert (res.hits, res.misses) == (1, 1)
    assert res.false_alarms_labelled == 1
    assert res.false_alarms_raw == 2
    assert res.pod == pytest.approx(0.5)
    assert res.far_raw == pytest.approx(2 / 3)
    assert res.far_labelled == pytest.approx(1 / 2)
    assert res.csi_raw == pytest.approx(1 / 4)
    assert res.csi_labelled == pytest.approx(1 / 3)
    assert res.lead_times_h == [36.0]
    assert res.median_lead_h == 36.0 and res.min_lead_h == 36.0
    assert [a.category for a in res.alerts] == ["hit", "labelled_negative", "unlabelled"]
    assert res.alerts[1].matched_keys == ["2024-03-01"]
    assert [a.alert.start for a in res.unlabelled_alerts] == [datetime(2024, 4, 15)]
    by_key = {e.key: e for e in res.events}
    assert by_key["2024-01-10"].matched and not by_key["2024-02-10"].matched


def test_evaluation_window_bounds():
    flood = _event(datetime(2024, 1, 10, 12), EpisodeOutcome.FLOODED)
    peak = flood.peak_time
    inside_early = _alert(peak - 72 * H, 2)
    too_early = _alert(peak - 73 * H, 1)  # ends before the window opens
    inside_late = _alert(peak + 6 * H, 2)
    too_late = _alert(peak + 7 * H, 2)
    assert evaluate_alerts([inside_early], [flood], 35).hits == 1
    assert evaluate_alerts([inside_late], [flood], 35).hits == 1
    assert evaluate_alerts([inside_late], [flood], 35).lead_times_h == [-6.0]
    res = evaluate_alerts([too_early, too_late], [flood], 35)
    assert res.hits == 0 and res.misses == 1 and res.false_alarms_raw == 2


def test_evaluation_earliest_matching_alert_gives_lead_time():
    flood = _event(datetime(2024, 1, 10, 12), EpisodeOutcome.FLOODED)
    a1 = _alert(flood.peak_time - 48 * H, 3)
    a2 = _alert(flood.peak_time - 10 * H, 3)
    res = evaluate_alerts([a1, a2], [flood], 35)
    assert res.hits == 1
    assert res.lead_times_h == [48.0]
    assert res.false_alarms_raw == 0  # both alerts matched the flood
    assert [a.category for a in res.alerts] == ["hit", "hit"]


def test_evaluation_ongoing_alert_matches_unless_strict():
    first = _event(datetime(2023, 12, 1, 6), EpisodeOutcome.FLOODED)
    second = _event(datetime(2023, 12, 11, 12), EpisodeOutcome.FLOODED)
    long_alert = _alert(datetime(2023, 11, 29, 0), 24 * 14)  # covers both floods
    res = evaluate_alerts([long_alert], [first, second], 35)
    assert res.hits == 2
    second_res = next(e for e in res.events if e.key == "2023-12-11")
    assert second_res.ongoing and second_res.lead_time_h == 72.0

    strict = evaluate_alerts([long_alert], [first, second], 35,
                             ContingencyConfig(allow_ongoing=False))
    assert (strict.hits, strict.misses) == (1, 1)


def test_evaluation_unknown_event_excluded():
    unknown = _event(datetime(2026, 1, 28, 6), EpisodeOutcome.UNKNOWN)
    alert = _alert(datetime(2026, 1, 27, 0), 10)
    res = evaluate_alerts([alert], [unknown], 35)
    assert res.alerts[0].category == "excluded"
    assert res.false_alarms_raw == 0 and res.hits == 0 and res.misses == 0
    assert res.pod is None and res.far_raw is None


def test_evaluation_flood_overrides_negative_when_both_match():
    flood = _event(datetime(2024, 1, 10, 12), EpisodeOutcome.FLOODED)
    neg = _event(datetime(2024, 1, 11, 12), EpisodeOutcome.FALSE_POSITIVE)
    res = evaluate_alerts([_alert(datetime(2024, 1, 10, 0), 5)], [flood, neg], 35)
    assert res.alerts[0].category == "hit"
    assert res.false_alarms_raw == 0


def test_evaluation_period_excludes_alerts_and_events_outside():
    period = LabelPeriod(datetime(2024, 1, 1), datetime(2024, 6, 30, 23))
    flood_out = _event(datetime(2024, 8, 1, 12), EpisodeOutcome.FLOODED)
    alert_out = _alert(datetime(2024, 9, 1), 3)
    res = evaluate_alerts([alert_out], [flood_out], 35, period=period)
    assert res.alerts[0].category == "outside_period"
    assert res.misses == 0 and res.false_alarms_raw == 0
    assert res.n_alerts == 1 and res.n_alerts_judged == 0


def test_evaluation_scores_end_to_end_and_seasons():
    times = [T0 + i * H for i in range(24 * 20)]
    flood = _event(T0 + 24 * 5 * H, EpisodeOutcome.FLOODED)
    scores = [0.0] * len(times)
    for i in range(24 * 4, 24 * 5 + 3):  # alert from 24h before the peak
        scores[i] = 60.0
    scores[24 * 15] = 40.0  # isolated orange hour, unlabelled
    orange = evaluate_scores(times, scores, [flood], 35)
    red = evaluate_scores(times, scores, [flood], 55)
    assert orange.hits == 1 and orange.false_alarms_raw == 1
    assert red.hits == 1 and red.false_alarms_raw == 0
    assert orange.lead_times_h == [24.0]
    assert orange.alerts_per_season == {"2023-24": 2}
    assert season_of(datetime(2023, 10, 1)) == "2023-24"
    assert season_of(datetime(2023, 9, 30)) == "2022-23"
    d = orange.to_dict()
    assert d["alerts"][0]["category"] == "hit" and d["events"][0]["matched"] is True
