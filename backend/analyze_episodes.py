"""Analyse exhaustive des épisodes pour recalibration du modèle.

Interroge InfluxDB pour chaque épisode confirmé/rejeté :
- 7 stations rivières (niveaux + tendances)
- Pluie cumulative (24h, 72h, 7j)
- Marée (coefficient + phase)
- Sol (humidité, température)
- Vent (composante ouest)
- Score de risque stocké

Produit un tableau comparatif pour recalibrer risk_v2.py.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta

# Add parent to path for imports
sys.path.insert(0, "/Users/clark/perso/lidoire-flood-watch/backend")

from app.config import get_settings
from app.services import influx_client


async def analyze_episode_conditions(peak_time: datetime, window_h: int = 6) -> dict:
    """Fetch all available data around an episode peak time."""
    settings = get_settings()
    start = peak_time - timedelta(hours=window_h)
    end = peak_time + timedelta(hours=window_h)

    result = {
        "peak_time": peak_time.isoformat(),
        "stations": {},
        "rain_24h": 0.0,
        "rain_72h": 0.0,
        "rain_7d": 0.0,
        "tide_coef": None,
        "tide_height_at_peak": None,
        "hours_to_high_tide": None,
        "soil_moisture_shallow": None,
        "soil_moisture_deep": None,
        "soil_temp": None,
        "wind_speed": None,
        "wind_westerly": None,
        "risk_score_stored": None,
    }

    # 1. River levels for ALL 7 stations
    for station_cfg in settings.hubeau_stations:
        sid = station_cfg["id"]
        sname = station_cfg["name"]
        river = station_cfg["river"]
        weight = station_cfg.get("weight", 0)
        thresholds = station_cfg.get("thresholds", {})

        levels = await influx_client.query_river_levels(sid, start, end)
        if not levels:
            result["stations"][sid] = {
                "name": sname, "river": river, "weight": weight,
                "height_m": None, "rate_cm_h": None, "rising": None,
                "thresholds": thresholds,
            }
            continue

        # Find closest to peak
        closest = min(levels, key=lambda l: abs(
            (l["timestamp"].replace(tzinfo=None) if l["timestamp"].tzinfo else l["timestamp"]) - peak_time
        ).total_seconds())

        height = closest["height_m"]

        # Calculate rate (use points 2h before and at peak)
        rate_start = peak_time - timedelta(hours=2)
        before_pts = [
            l for l in levels
            if rate_start <= (l["timestamp"].replace(tzinfo=None) if l["timestamp"].tzinfo else l["timestamp"]) <= peak_time
        ]
        rate_cm_h = 0.0
        if len(before_pts) >= 2:
            p0 = before_pts[0]
            p1 = before_pts[-1]
            t0 = p0["timestamp"].replace(tzinfo=None) if p0["timestamp"].tzinfo else p0["timestamp"]
            t1 = p1["timestamp"].replace(tzinfo=None) if p1["timestamp"].tzinfo else p1["timestamp"]
            dt_h = (t1 - t0).total_seconds() / 3600
            if dt_h > 0.1:
                rate_cm_h = (p1["height_m"] - p0["height_m"]) * 100 / dt_h

        result["stations"][sid] = {
            "name": sname,
            "river": river,
            "weight": weight,
            "height_m": round(height, 3),
            "rate_cm_h": round(rate_cm_h, 1),
            "rising": rate_cm_h > 1.0,
            "thresholds": thresholds,
            "above_yellow": height >= thresholds.get("yellow", 3.0),
            "above_orange": height >= thresholds.get("orange", 4.0),
            "above_red": height >= thresholds.get("red", 5.0),
        }

    # 2. Rainfall cumulative (24h, 72h, 7d)
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

    result["rain_24h"] = round(rain_24h, 1)
    result["rain_72h"] = round(rain_72h, 1)
    result["rain_7d"] = round(rain_7d, 1)

    # 3. Tide data
    for port in ["BORDEAUX", "LIBOURNE"]:
        tides = await influx_client.query_tides(
            port, peak_time - timedelta(hours=12), peak_time + timedelta(hours=12)
        )
        if not tides:
            continue

        # Find coefficient
        coeffs = [t for t in tides if t.get("coefficient") is not None]
        if coeffs:
            closest_coef = min(coeffs, key=lambda t: abs(
                (t["timestamp"].replace(tzinfo=None) if t["timestamp"].tzinfo else t["timestamp"]) - peak_time
            ).total_seconds())
            result["tide_coef"] = int(closest_coef["coefficient"])

        # Find tide height at peak + closest high tide
        heights = [t for t in tides if t.get("height_m") is not None]
        if heights:
            # Height at peak
            closest_h = min(heights, key=lambda t: abs(
                (t["timestamp"].replace(tzinfo=None) if t["timestamp"].tzinfo else t["timestamp"]) - peak_time
            ).total_seconds())
            result["tide_height_at_peak"] = round(closest_h["height_m"], 2)

            # Find high tides (local maxima)
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
                result["hours_to_high_tide"] = round((closest_ht - peak_time).total_seconds() / 3600, 1)

        if result["tide_coef"] is not None:
            break  # Got data from this port

    # 4. Soil moisture
    soil = await influx_client.query_soil_timeline(
        peak_time - timedelta(hours=6), peak_time + timedelta(hours=1),
        resolution="1h",
        fields=["moisture_shallow", "moisture_deep", "soil_temp"],
    )

    for field_name, points in soil.items():
        if not points:
            continue
        # Take the one closest to peak
        closest_pt = min(points, key=lambda p: abs(
            (p["timestamp"].replace(tzinfo=None) if p["timestamp"].tzinfo else p["timestamp"]) - peak_time
        ).total_seconds())
        if field_name == "moisture_shallow":
            result["soil_moisture_shallow"] = round(closest_pt["value"], 1)
        elif field_name == "moisture_deep":
            result["soil_moisture_deep"] = round(closest_pt["value"], 1)
        elif field_name == "soil_temp":
            result["soil_temp"] = round(closest_pt["value"], 1)

    # 5. Wind
    wind = await influx_client.query_wind_timeline(
        peak_time - timedelta(hours=6), peak_time + timedelta(hours=1),
        resolution="1h",
    )

    for field_name, points in wind.items():
        if not points:
            continue
        closest_pt = min(points, key=lambda p: abs(
            (p["timestamp"].replace(tzinfo=None) if p["timestamp"].tzinfo else p["timestamp"]) - peak_time
        ).total_seconds())
        if field_name == "speed_kmh":
            result["wind_speed"] = round(closest_pt["value"], 1)
        elif field_name == "westerly_kmh":
            result["wind_westerly"] = round(closest_pt["value"], 1)

    # 6. Stored risk score
    scores = await influx_client.query_risk_scores(
        peak_time - timedelta(hours=3), peak_time + timedelta(hours=3)
    )
    score_vals = [s["score"] for s in scores if s.get("score") is not None]
    if score_vals:
        result["risk_score_stored"] = round(max(score_vals), 1)

    # 7. Derived metrics
    active_stations = [s for s in result["stations"].values() if s["weight"] > 0 and s["height_m"] is not None]
    result["n_stations_data"] = len(active_stations)
    result["n_rising"] = sum(1 for s in active_stations if s.get("rising"))
    result["n_above_yellow"] = sum(1 for s in active_stations if s.get("above_yellow"))
    result["n_above_orange"] = sum(1 for s in active_stations if s.get("above_orange"))

    # Weighted river score (same as model)
    total_w = 0.0
    weighted_river = 0.0
    for s in active_stations:
        th = s["thresholds"]
        h = s["height_m"]
        yellow = th.get("yellow", 3.0)
        orange = th.get("orange", 4.0)
        red = th.get("red", 5.0)

        if h <= 0:
            score = 0.0
        elif h < yellow:
            score = (h / yellow) * 0.33
        elif h < orange:
            score = 0.33 + ((h - yellow) / (orange - yellow)) * 0.33
        elif h < red:
            score = 0.66 + ((h - orange) / (red - orange)) * 0.34
        else:
            score = 1.0
        # Apply exponential
        if score > 0:
            score = score ** 0.6

        weighted_river += score * s["weight"]
        total_w += s["weight"]

    result["weighted_river_score"] = round(weighted_river / total_w, 3) if total_w > 0 else 0

    return result


async def main():
    """Run analysis on all episodes."""
    # Episode data (from database - hardcoded for offline analysis)
    episodes = [
        # CONFIRMED FLOODED
        {"id": 14, "peak": "2023-12-01T06:00:00", "severity": 5, "flooded": True,
         "notes": "Grosse inondation 25cm eau interieur"},
        {"id": 12, "peak": "2023-03-11T12:00:00", "severity": 3, "flooded": True,
         "notes": "Premiere inondation infiltration (depuis colmate)"},
        {"id": 3, "peak": "2023-12-11T12:00:00", "severity": 3, "flooded": True,
         "notes": "Continuation decembre 2023"},
        {"id": 2, "peak": "2024-02-26T12:00:00", "severity": 5, "flooded": True,
         "notes": "20-25cm eau interieur moulin"},
        {"id": 1, "peak": "2025-04-23T16:00:00", "severity": 4, "flooded": True,
         "notes": "Inonde"},
        # CONFIRMED NOT FLOODED
        {"id": 13, "peak": "2023-01-18T12:00:00", "severity": 1, "flooded": False,
         "notes": "pas inondé"},
        {"id": 4, "peak": "2024-03-30T12:00:00", "severity": 2, "flooded": False,
         "notes": "ca a bien monte mais pas inondé"},
        {"id": 5, "peak": "2024-05-09T06:00:00", "severity": 1, "flooded": False,
         "notes": "pas inondé"},
        {"id": 6, "peak": "2024-10-18T06:00:00", "severity": 2, "flooded": False,
         "notes": "pas inondé"},
        {"id": 7, "peak": "2024-11-27T06:00:00", "severity": 1, "flooded": False,
         "notes": "pas inondé"},
        {"id": 8, "peak": "2024-12-11T06:00:00", "severity": 1, "flooded": False,
         "notes": "pas inondé"},
        {"id": 9, "peak": "2025-01-12T06:00:00", "severity": 1, "flooded": False,
         "notes": "pas inondé"},
        {"id": 10, "peak": "2025-11-25T06:00:00", "severity": 3, "flooded": False,
         "notes": "pas inondé, haut"},
        {"id": 11, "peak": "2026-01-28T06:00:00", "severity": 4, "flooded": False,
         "notes": "en cours, pas encore inondé"},
    ]

    print("=" * 120)
    print("ANALYSE EXHAUSTIVE DES EPISODES - DONNEES MULTI-SOURCE")
    print("=" * 120)

    all_results = []

    for ep in episodes:
        peak = datetime.fromisoformat(ep["peak"])
        print(f"\n--- Episode {ep['id']}: {ep['peak'][:10]} {'INONDE' if ep['flooded'] else 'OK'} (sev={ep['severity']}) ---")

        data = await analyze_episode_conditions(peak)
        data["episode_id"] = ep["id"]
        data["flooded"] = ep["flooded"]
        data["severity"] = ep["severity"]
        data["notes"] = ep["notes"]
        all_results.append(data)

        # Print station details
        print(f"  Rivières ({data['n_stations_data']} stations, {data['n_rising']} en hausse, "
              f"{data['n_above_yellow']} >jaune, {data['n_above_orange']} >orange):")
        for sid, s in data["stations"].items():
            if s["height_m"] is not None:
                flag = ""
                if s.get("above_red"): flag = " *** RED ***"
                elif s.get("above_orange"): flag = " ** ORANGE **"
                elif s.get("above_yellow"): flag = " * YELLOW *"
                rise = f"+{s['rate_cm_h']}cm/h" if s["rate_cm_h"] > 0 else f"{s['rate_cm_h']}cm/h"
                print(f"    {s['name']:20s} ({s['river']:10s}) w={s['weight']:.2f}  "
                      f"H={s['height_m']:.2f}m  {rise:>10s}{flag}")

        print(f"  Score rivière pondéré: {data['weighted_river_score']:.3f}")
        print(f"  Pluie: 24h={data['rain_24h']:.0f}mm  72h={data['rain_72h']:.0f}mm  7j={data['rain_7d']:.0f}mm")
        print(f"  Marée: coef={data['tide_coef']}  hauteur={data['tide_height_at_peak']}m  "
              f"heures_PM={data['hours_to_high_tide']}")
        print(f"  Sol: shallow={data['soil_moisture_shallow']}%  deep={data['soil_moisture_deep']}%  "
              f"temp={data['soil_temp']}°C")
        print(f"  Vent: vitesse={data['wind_speed']}km/h  ouest={data['wind_westerly']}km/h")
        print(f"  Score risque stocké: {data['risk_score_stored']}")

    # Summary comparison table
    print("\n" + "=" * 120)
    print("TABLEAU COMPARATIF")
    print("=" * 120)

    header = (f"{'ID':>3s} {'Date':>10s} {'Flood':>5s} {'Sev':>3s} | "
              f"{'WRiver':>6s} {'Rise':>4s} {'>Y':>2s} {'>O':>2s} | "
              f"{'R24h':>5s} {'R72h':>5s} {'R7d':>5s} | "
              f"{'Coef':>4s} {'hPM':>5s} {'Tide':>5s} | "
              f"{'SoilS':>5s} {'SoilD':>5s} {'Temp':>5s} | "
              f"{'Wind':>5s} {'WstW':>5s} | "
              f"{'Score':>5s}")
    print(header)
    print("-" * len(header))

    for d in all_results:
        flood_str = "OUI" if d["flooded"] else "non"
        coef_str = str(d["tide_coef"]) if d["tide_coef"] else "-"
        hpm_str = f"{d['hours_to_high_tide']:.1f}" if d["hours_to_high_tide"] is not None else "-"
        tide_str = f"{d['tide_height_at_peak']:.1f}" if d["tide_height_at_peak"] is not None else "-"
        soil_s = f"{d['soil_moisture_shallow']:.0f}" if d["soil_moisture_shallow"] is not None else "-"
        soil_d = f"{d['soil_moisture_deep']:.0f}" if d["soil_moisture_deep"] is not None else "-"
        temp = f"{d['soil_temp']:.0f}" if d["soil_temp"] is not None else "-"
        wind = f"{d['wind_speed']:.0f}" if d["wind_speed"] is not None else "-"
        wstw = f"{d['wind_westerly']:.0f}" if d["wind_westerly"] is not None else "-"
        score = f"{d['risk_score_stored']:.0f}" if d["risk_score_stored"] is not None else "-"

        print(f"{d['episode_id']:>3d} {d['peak_time'][:10]:>10s} {flood_str:>5s} {d['severity']:>3d} | "
              f"{d['weighted_river_score']:>6.3f} {d['n_rising']:>4d} {d['n_above_yellow']:>2d} {d['n_above_orange']:>2d} | "
              f"{d['rain_24h']:>5.0f} {d['rain_72h']:>5.0f} {d['rain_7d']:>5.0f} | "
              f"{coef_str:>4s} {hpm_str:>5s} {tide_str:>5s} | "
              f"{soil_s:>5s} {soil_d:>5s} {temp:>5s} | "
              f"{wind:>5s} {wstw:>5s} | "
              f"{score:>5s}")

    # Statistical separation analysis
    print("\n" + "=" * 120)
    print("ANALYSE DE SEPARATION")
    print("=" * 120)

    flooded = [d for d in all_results if d["flooded"]]
    not_flooded = [d for d in all_results if not d["flooded"]]

    metrics = [
        ("weighted_river_score", "Score rivière pondéré"),
        ("n_rising", "Stations en hausse"),
        ("n_above_yellow", "Stations > jaune"),
        ("n_above_orange", "Stations > orange"),
        ("rain_24h", "Pluie 24h (mm)"),
        ("rain_72h", "Pluie 72h (mm)"),
        ("rain_7d", "Pluie 7j (mm)"),
    ]

    for key, label in metrics:
        f_vals = [d[key] for d in flooded if d[key] is not None]
        nf_vals = [d[key] for d in not_flooded if d[key] is not None]

        if f_vals and nf_vals:
            f_min, f_max, f_avg = min(f_vals), max(f_vals), sum(f_vals)/len(f_vals)
            nf_min, nf_max, nf_avg = min(nf_vals), max(nf_vals), sum(nf_vals)/len(nf_vals)
            overlap = max(0, min(f_max, nf_max) - max(f_min, nf_min))
            total_range = max(f_max, nf_max) - min(f_min, nf_min)
            overlap_pct = (overlap / total_range * 100) if total_range > 0 else 0

            print(f"  {label:30s}: INONDE [{f_min:.1f} - {f_max:.1f}] avg={f_avg:.1f} | "
                  f"OK [{nf_min:.1f} - {nf_max:.1f}] avg={nf_avg:.1f} | "
                  f"overlap={overlap_pct:.0f}%")

    # Export full JSON for reference
    with open("/tmp/episode_analysis.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nDonnées complètes exportées dans /tmp/episode_analysis.json")


if __name__ == "__main__":
    asyncio.run(main())
