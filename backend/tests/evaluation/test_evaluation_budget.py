"""Alert budget study (scripts.eval.budget): combinations and notifier simulation."""

from datetime import datetime, timedelta

from app.models.schemas import EpisodeOutcome
from scripts.eval.budget import above, all_of, any_of, evaluate_point, indicator, simulate_notifier
from scripts.eval.truth import EvalEvent, LabelPeriod

T0 = datetime(2024, 1, 1)


def _times(n):
    return [T0 + timedelta(hours=i) for i in range(n)]


def test_budget_combinations_and_missing_inputs():
    series = {"a": [None, 10.0, 60.0, 10.0], "b": [None, 40.0, None, 10.0]}
    assert indicator(above("a", 55), series, 4) == [None, 0.0, 100.0, 0.0]
    either = any_of(above("a", 55), above("b", 35))
    assert indicator(either, series, 4) == [None, 100.0, 100.0, 0.0]
    both = all_of(above("a", 5), above("b", 35))
    assert indicator(both, series, 4) == [None, 100.0, None, 0.0]
    assert either.name == "a >= 55 OR b >= 35"


def test_budget_notifier_hold_merges_oscillations():
    # on 2 h, off 2 h, on 2 h, then off for good: 2 rises with a 1 h hold, 1 with a 3 h hold
    fired = [100, 100, 0, 0, 100, 100, 0, 0, 0, 0, 0]
    times = _times(len(fired))
    assert simulate_notifier(times, fired, hold_h=1) == (2, 4)
    assert simulate_notifier(times, fired, hold_h=3) == (1, 2)
    # hours without a stored score are skipped, not read as a drop
    assert simulate_notifier(times, [100, None, None, None, 100, 0], hold_h=1) == (1, 1)


def test_budget_point_lead_against_precise_peak():
    times = _times(200)
    scores = [0.0] * 200
    for i in range(100, 110):
        scores[i] = 80.0
    peak = T0 + timedelta(hours=112, minutes=15)
    flood = EvalEvent("2024-01-05", peak, EpisodeOutcome.FLOODED, True, 5, peak_time_precision_hours=0.5)
    res = evaluate_point(above("s", 55), {"s": scores}, times, [flood], LabelPeriod(times[0], times[-1]))
    row = res.row()
    assert row["hits"] == 1 and row["n_floods"] == 1
    assert row["leads_h"]["2024-01-05"] == 12.25
    assert row["lead_min_minus_precision_h"] == 11.75
    assert row["pod_ci95"][1] == 1.0
