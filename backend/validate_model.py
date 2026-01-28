"""Validate v3.0 model scores against annotated episodes.

Simulates the risk score for each episode using the actual data from InfluxDB,
comparing old (stored) scores with new v3.0 scores.
"""

import asyncio
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/Users/clark/perso/lidoire-flood-watch/backend")

from app.config import get_settings
from app.services import influx_client
from app.services.risk_v2 import (
    _normalize_score,
    _calculate_tendency_score,
    _calculate_momentum_score,
    _calculate_conjunction_multiplier,
    _calculate_stability_dampener,
)
from app.models.schemas import Tendency


async def simulate_v3_score(peak_time: datetime) -> dict:
    """Simulate the v3.0 score for a given peak time using InfluxDB data."""
    settings = get_settings()
    start = peak_time - timedelta(hours=6)
    end = peak_time + timedelta(hours=6)

    # 1. River levels for all non-tidal stations
    weighted_score_sum = 0.0
    total_weight = 0.0
    n_rising = 0
    n_total = 0

    for station_cfg in settings.hubeau_stations:
        w = station_cfg.get("weight", 0)
        if w <= 0:
            continue

        sid = station_cfg["id"]
        th = station_cfg.get("thresholds", {})
        yellow = th.get("yellow", 3.0)
        orange = th.get("orange", 4.0)
        red = th.get("red", 5.0)

        levels = await influx_client.query_river_levels(sid, start, end)
        if not levels:
            continue

        closest = min(levels, key=lambda l: abs(
            (l["timestamp"].replace(tzinfo=None) if l["timestamp"].tzinfo else l["timestamp"]) - peak_time
        ).total_seconds())

        height = closest["height_m"]
        score = _normalize_score(height, (yellow, orange, red))

        weighted_score_sum += score * w
        total_weight += w
        n_total += 1

        # Rate
        rate_start = peak_time - timedelta(hours=2)
        before_pts = [
            l for l in levels
            if rate_start <= (l["timestamp"].replace(tzinfo=None) if l["timestamp"].tzinfo else l["timestamp"]) <= peak_time
        ]
        rate_cm_h = 0.0
        if len(before_pts) >= 2:
            p0, p1 = before_pts[0], before_pts[-1]
            t0 = p0["timestamp"].replace(tzinfo=None) if p0["timestamp"].tzinfo else p0["timestamp"]
            t1 = p1["timestamp"].replace(tzinfo=None) if p1["timestamp"].tzinfo else p1["timestamp"]
            dt_h = (t1 - t0).total_seconds() / 3600
            if dt_h > 0.1:
                rate_cm_h = (p1["height_m"] - p0["height_m"]) * 100 / dt_h

        if rate_cm_h > 1.0:
            n_rising += 1

    river_score = weighted_score_sum / total_weight if total_weight > 0 else 0

    # 2. Rainfall
    rain_start = peak_time - timedelta(days=7)
    rainfall = await influx_client.query_rainfall(rain_start, peak_time)
    rain_by_hour = {}
    for r in rainfall:
        precip = r.get("precipitation_mm_h")
        if precip is None:
            continue
        ts = r["timestamp"]
        if hasattr(ts, "tzinfo") and ts.tzinfo:
            ts = ts.replace(tzinfo=None)
        bucket = ts.replace(minute=0, second=0, microsecond=0)
        rain_by_hour.setdefault(bucket, []).append(precip)
    rain_hourly = {b: sum(v)/len(v) for b, v in rain_by_hour.items()}

    rain_24h = sum(v for ts, v in rain_hourly.items() if (peak_time - ts).total_seconds() <= 24*3600)
    rain_72h = sum(v for ts, v in rain_hourly.items() if (peak_time - ts).total_seconds() <= 72*3600)
    rain_7d = sum(v for ts, v in rain_hourly.items())

    rain_24h_score = _normalize_score(
        rain_24h,
        (settings.risk_rain_24h_yellow, settings.risk_rain_24h_orange, settings.risk_rain_24h_red),
    )
    rain_72h_score = _normalize_score(
        rain_72h,
        (settings.risk_rain_24h_yellow * 2, settings.risk_rain_24h_orange * 2, settings.risk_rain_24h_red * 2),
    )
    rain_7d_score = _normalize_score(rain_7d, (60.0, 100.0, 150.0))

    # 3. Tide
    tide_phase_score = 0.1  # Default
    tide_score = 0.0
    for port in ["BORDEAUX", "LIBOURNE"]:
        tides = await influx_client.query_tides(
            port, peak_time - timedelta(hours=12), peak_time + timedelta(hours=12)
        )
        if not tides:
            continue
        heights = [t for t in tides if t.get("height_m") is not None]
        if heights:
            sorted_h = sorted(heights, key=lambda t: t["timestamp"].replace(tzinfo=None) if t["timestamp"].tzinfo else t["timestamp"])
            high_tides = []
            for i in range(1, len(sorted_h) - 1):
                if (sorted_h[i]["height_m"] >= sorted_h[i-1]["height_m"] and
                    sorted_h[i]["height_m"] >= sorted_h[i+1]["height_m"]):
                    ht_ts = sorted_h[i]["timestamp"]
                    if hasattr(ht_ts, "tzinfo") and ht_ts.tzinfo:
                        ht_ts = ht_ts.replace(tzinfo=None)
                    high_tides.append(ht_ts)

            if high_tides:
                closest_ht = min(high_tides, key=lambda ht: abs((ht - peak_time).total_seconds()))
                hours_to_ht = abs((closest_ht - peak_time).total_seconds()) / 3600
                if hours_to_ht <= 1:
                    tide_phase_score = 1.0
                elif hours_to_ht <= 2:
                    tide_phase_score = 0.8
                elif hours_to_ht <= 3:
                    tide_phase_score = 0.5
                elif hours_to_ht <= 4:
                    tide_phase_score = 0.3
                else:
                    tide_phase_score = 0.1
            break

    # 4. Tendency
    if n_rising >= 3:
        tendency_score = 0.7
    elif n_rising >= 1:
        tendency_score = 0.5
    else:
        tendency_score = 0.0
    tendency = Tendency.RISING if n_rising >= 2 else (Tendency.STABLE if n_rising == 1 else Tendency.FALLING)

    # 5. Momentum (gated by river level)
    momentum_score = _calculate_momentum_score(
        n_rising=n_rising, n_total=max(n_total, 1), river_score=river_score,
    )

    # 6. Conjunction
    conjunction = _calculate_conjunction_multiplier(
        river_score, tide_phase_score, tendency,
        rain_72h_score=rain_72h_score, rain_24h_score=rain_24h_score,
        n_rising=n_rising,
    )

    # 7. Stability dampener
    dampener = _calculate_stability_dampener(
        river_score, tendency, n_rising=n_rising,
    )

    # 8. Calculate final score (v3.0 weights)
    weights = {
        "river": 0.28,
        "rain_24h": 0.10,
        "rain_72h": 0.12,
        "rain_7d": 0.05,
        "tide": 0.05,
        "tide_phase": 0.05,
        "tendency": 0.12,
        "momentum": 0.08,
        "wind_west": 0.03,
        "soil_sat": 0.05,
        "video": 0.05,
    }

    total_w = sum(weights.values()) - weights["video"]  # No video
    weighted = (
        river_score * weights["river"] +
        rain_24h_score * weights["rain_24h"] +
        rain_72h_score * weights["rain_72h"] +
        rain_7d_score * weights["rain_7d"] +
        tide_score * weights["tide"] +
        tide_phase_score * weights["tide_phase"] +
        tendency_score * weights["tendency"] +
        momentum_score * weights["momentum"]
    )

    weighted *= conjunction
    weighted *= dampener
    final_score = min((weighted / total_w) * 100, 100)

    return {
        "score": round(final_score, 1),
        "river": round(river_score, 3),
        "rain_24h": round(rain_24h, 0),
        "rain_72h": round(rain_72h, 0),
        "n_rising": n_rising,
        "n_total": n_total,
        "momentum": round(momentum_score, 2),
        "conjunction": round(conjunction, 2),
        "dampener": round(dampener, 2),
        "tendency": tendency.value,
    }


async def main():
    episodes = [
        {"id": 14, "peak": "2023-12-01T06:00:00", "sev": 5, "flood": True, "old": 90.0,
         "notes": "25cm eau interieur"},
        {"id": 12, "peak": "2023-03-11T12:00:00", "sev": 3, "flood": True, "old": 46.9,
         "notes": "infiltration"},
        {"id": 3, "peak": "2023-12-11T12:00:00", "sev": 3, "flood": True, "old": 94.9,
         "notes": "continuation dec"},
        {"id": 2, "peak": "2024-02-26T12:00:00", "sev": 5, "flood": True, "old": 70.5,
         "notes": "25cm eau"},
        {"id": 1, "peak": "2025-04-23T16:00:00", "sev": 4, "flood": True, "old": 51.6,
         "notes": "inonde"},
        {"id": 13, "peak": "2023-01-18T12:00:00", "sev": 1, "flood": False, "old": 60.5,
         "notes": "pas inonde"},
        {"id": 4, "peak": "2024-03-30T12:00:00", "sev": 2, "flood": False, "old": 51.4,
         "notes": "bien monte"},
        {"id": 5, "peak": "2024-05-09T06:00:00", "sev": 1, "flood": False, "old": 60.6,
         "notes": "Pessac RED"},
        {"id": 6, "peak": "2024-10-18T06:00:00", "sev": 2, "flood": False, "old": 58.9,
         "notes": "pas inonde"},
        {"id": 7, "peak": "2024-11-27T06:00:00", "sev": 1, "flood": False, "old": 36.1,
         "notes": "pas inonde"},
        {"id": 8, "peak": "2024-12-11T06:00:00", "sev": 1, "flood": False, "old": 33.2,
         "notes": "pas inonde"},
        {"id": 9, "peak": "2025-01-12T06:00:00", "sev": 1, "flood": False, "old": 48.6,
         "notes": "Pessac ORANGE"},
        {"id": 10, "peak": "2025-11-25T06:00:00", "sev": 3, "flood": False, "old": 83.5,
         "notes": "pas inonde"},
        {"id": 11, "peak": "2026-01-28T06:00:00", "sev": 4, "flood": False, "old": 42.6,
         "notes": "en cours"},
    ]

    print("=" * 110)
    print("VALIDATION MODELE v3.0 — Comparaison ancien/nouveau score")
    print("=" * 110)

    header = (f"{'ID':>3s} {'Date':>10s} {'F?':>3s} {'Sev':>3s} | "
              f"{'OLD':>5s} {'NEW':>5s} {'Delta':>6s} | "
              f"{'River':>5s} {'Rise':>4s} {'Mom':>4s} {'Conj':>5s} {'Damp':>5s} | "
              f"{'R24':>4s} {'R72':>4s} {'Notes'}")
    print(header)
    print("-" * 110)

    flood_scores = []
    noflood_scores = []

    for ep in episodes:
        peak = datetime.fromisoformat(ep["peak"])
        d = await simulate_v3_score(peak)

        delta = d["score"] - ep["old"]
        sign = "+" if delta > 0 else ""
        flood_str = "OUI" if ep["flood"] else "non"

        if ep["flood"]:
            flood_scores.append(d["score"])
        else:
            noflood_scores.append(d["score"])

        print(f"{ep['id']:>3d} {ep['peak'][:10]:>10s} {flood_str:>3s} {ep['sev']:>3d} | "
              f"{ep['old']:>5.1f} {d['score']:>5.1f} {sign}{delta:>5.1f} | "
              f"{d['river']:>5.3f} {d['n_rising']:>4d} {d['momentum']:>4.2f} {d['conjunction']:>5.2f} {d['dampener']:>5.2f} | "
              f"{d['rain_24h']:>4.0f} {d['rain_72h']:>4.0f} {ep['notes']}")

    print()
    print("=" * 110)
    print("SEPARATION")
    print(f"  INONDE : min={min(flood_scores):.1f}  max={max(flood_scores):.1f}  avg={sum(flood_scores)/len(flood_scores):.1f}")
    print(f"  OK     : min={min(noflood_scores):.1f}  max={max(noflood_scores):.1f}  avg={sum(noflood_scores)/len(noflood_scores):.1f}")

    gap = min(flood_scores) - max(noflood_scores)
    if gap > 0:
        print(f"  ==> GAP de {gap:.1f} pts entre le pire flood et le meilleur non-flood. MODELE DISCRIMINE BIEN.")
    else:
        overlap = max(noflood_scores) - min(flood_scores)
        print(f"  ==> Overlap de {overlap:.1f} pts. Le modele ne separe pas parfaitement.")

    print()
    # Target scores
    print("OBJECTIFS:")
    print("  Floods:    ID14 ~90, ID12 ~55, ID3 ~90, ID2 ~85, ID1 ~70")
    print("  No-floods: ID5 <40, ID9 <45, ID10 <50, ID13 <40, ID4 <50")


if __name__ == "__main__":
    asyncio.run(main())
