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


def _synthetic_scores(seed: int, n: int = 24 * 120) -> list:
    """Slow flood-like waves + semi-diurnal tide oscillation + noise + gaps."""
    import math
    import random

    rng = random.Random(seed)
    out, base = [], 10.0
    for i in range(n):
        if rng.random() < 0.01:
            base = rng.choice([5.0, 25.0, 40.0, 50.0, 60.0, 80.0])   # new regime
        tide = 8.0 * math.sin(2 * math.pi * i / 12.42)
        s = min(100.0, max(0.0, base + tide + rng.gauss(0, 4)))
        out.append(None if rng.random() < 0.03 else round(s, 1))
    return out


def test_budget_red_policy_matches_real_notifier():
    """simulate_notifier at >= 55 == the real level_event with NOTIFY_MIN_LEVEL=red."""
    from scripts.eval.budget import NotifierPolicy, replay_notifier

    for seed in range(6):
        scores = _synthetic_scores(seed)
        times = _times(len(scores))
        fired = indicator(above("v3.4", 55), {"v3.4": scores}, len(scores))
        for hold in (3.0, 12.0):
            real = replay_notifier(times, scores, NotifierPolicy("red", "red", hold).settings())
            assert real == simulate_notifier(times, fired, hold), (seed, hold)
            assert real[0] > 0


def test_budget_orange_policy_counts_escalations_to_red():
    """Under the orange minimum the real notifier also alerts on orange -> red."""
    from scripts.eval.budget import NotifierPolicy, replay_notifier

    settings = NotifierPolicy("orange", "orange", 3.0).settings()
    # never red: identical to the simulation at >= 35
    scores = [min(s, 54.0) if s is not None else None for s in _synthetic_scores(1)]
    times = _times(len(scores))
    fired = indicator(above("v3.4", 35), {"v3.4": scores}, len(scores))
    assert replay_notifier(times, scores, settings) == simulate_notifier(times, fired, 3.0)
    # green -> orange -> red: 2 alerts for 1 run above 35
    times = _times(6)
    assert replay_notifier(times, [5.0, 40.0, 60.0, 60.0, 5.0, 5.0], settings)[0] == 2
    assert simulate_notifier(times, [0, 100, 100, 100, 0, 0], 3.0)[0] == 1


def test_budget_policy_rows_per_year():
    from scripts.eval.budget import POLICIES, policy_rows

    assert [p.min_level for p in POLICIES][-1] == "red" and POLICIES[-1].hold_h == 12.0
    times = _times(8766)                                 # one year
    scores = [5.0] * 8766
    for start in (100, 3000, 6000):
        for i in range(start, start + 10):
            scores[i] = 70.0
    rows = {r["policy"]: r for r in policy_rows(times, scores)}
    prod = rows["red, hold 12 h (production)"]
    assert prod["alerts_per_year"] == 3.0 and prod["messages_per_year"] == 6.0
    assert rows["orange, hold 3 h (default)"]["alerts_per_year"] == 3.0   # straight to red: 1 rise


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
