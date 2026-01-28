"""Episode detection service.

Analyzes backfilled historical data to detect potential flood episodes
based on the v2.5 risk model thresholds.

Detection criteria:
- Risk score >= 55 (RED level) for at least 2 consecutive hours
- OR risk score >= 35 (ORANGE level) for at least 6 consecutive hours
- OR maximum river level exceeds station red thresholds

Episodes are merged if they're within 24h of each other.
"""

from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, asdict

from app.config import get_settings
from app.services import influx_client


@dataclass
class DetectedEpisode:
    """A detected flood episode."""

    start_time: datetime
    end_time: datetime
    peak_time: datetime
    peak_score: float
    peak_level: str  # green/yellow/orange/red
    duration_hours: float
    max_river_levels: dict[str, float]  # station_id -> max height
    total_rain_mm: float
    max_tide_coef: Optional[int]
    detection_method: str  # "risk_score" or "river_threshold"
    severity_estimate: int  # 1-5 scale
    description: str

    def to_dict(self):
        return {
            **asdict(self),
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "peak_time": self.peak_time.isoformat(),
        }


# Risk level thresholds (v2.5 calibration)
RISK_RED_THRESHOLD = 55
RISK_ORANGE_THRESHOLD = 35
RISK_YELLOW_THRESHOLD = 15


async def detect_episodes_from_risk_scores(
    start_date: datetime,
    end_date: datetime,
    min_red_hours: int = 2,
    min_orange_hours: int = 6,
    merge_gap_hours: int = 24,
) -> list[DetectedEpisode]:
    """
    Detect flood episodes from stored risk scores.

    Args:
        start_date: Start of analysis period
        end_date: End of analysis period
        min_red_hours: Minimum consecutive RED hours to trigger detection
        min_orange_hours: Minimum consecutive ORANGE hours to trigger detection
        merge_gap_hours: Merge episodes that are within this gap

    Returns:
        List of detected episodes
    """
    print(f"\n--- Detecting episodes from risk scores ---")
    print(f"Period: {start_date.date()} -> {end_date.date()}")

    # Query risk scores from InfluxDB
    risk_scores = await influx_client.query_risk_scores(start_date, end_date)

    if not risk_scores:
        print("No risk score data found")
        return []

    print(f"Found {len(risk_scores)} risk score records")

    # Sort by timestamp
    risk_scores.sort(key=lambda x: x["timestamp"])

    # Find periods where risk exceeds thresholds
    episodes_raw = []
    current_episode = None

    for rs in risk_scores:
        score = rs.get("score")
        level = rs.get("level", "green")
        ts = rs["timestamp"]

        if score is None:
            continue

        # Check if this point should be part of an episode
        is_significant = score >= RISK_ORANGE_THRESHOLD

        if is_significant:
            if current_episode is None:
                # Start new episode
                current_episode = {
                    "start": ts,
                    "end": ts,
                    "peak_score": score,
                    "peak_time": ts,
                    "peak_level": level,
                    "scores": [(ts, score, level)],
                }
            else:
                # Extend current episode
                current_episode["end"] = ts
                current_episode["scores"].append((ts, score, level))
                if score > current_episode["peak_score"]:
                    current_episode["peak_score"] = score
                    current_episode["peak_time"] = ts
                    current_episode["peak_level"] = level
        else:
            # Not significant - close episode if gap is too large
            if current_episode is not None:
                gap = (ts - current_episode["end"]).total_seconds() / 3600
                if gap > merge_gap_hours:
                    episodes_raw.append(current_episode)
                    current_episode = None

    # Don't forget the last episode
    if current_episode is not None:
        episodes_raw.append(current_episode)

    # Filter episodes by duration criteria
    filtered_episodes = []
    for ep in episodes_raw:
        duration_hours = (ep["end"] - ep["start"]).total_seconds() / 3600

        # Count hours at each level
        red_hours = sum(1 for _, s, _ in ep["scores"] if s >= RISK_RED_THRESHOLD)
        orange_hours = sum(1 for _, s, _ in ep["scores"] if s >= RISK_ORANGE_THRESHOLD)

        # Check if episode meets criteria
        if red_hours >= min_red_hours or orange_hours >= min_orange_hours:
            filtered_episodes.append({
                **ep,
                "duration_hours": duration_hours,
                "red_hours": red_hours,
                "orange_hours": orange_hours,
            })

    print(f"Found {len(filtered_episodes)} significant episodes")

    # Enrich episodes with context data
    enriched_episodes = []
    for ep in filtered_episodes:
        episode = await _enrich_episode(ep)
        if episode:
            enriched_episodes.append(episode)

    return enriched_episodes


async def detect_episodes_from_river_levels(
    start_date: datetime,
    end_date: datetime,
) -> list[DetectedEpisode]:
    """
    Detect flood episodes from river level thresholds.

    Looks for periods where any station exceeds its red threshold.
    """
    print(f"\n--- Detecting episodes from river levels ---")

    settings = get_settings()
    episodes_raw = []

    # Query river levels for all stations
    all_levels = await influx_client.query_all_stations_levels(start_date, end_date)

    if not all_levels:
        print("No river level data found")
        return []

    # Check each station for threshold exceedances
    for station in settings.hubeau_stations:
        station_id = station["id"]
        thresholds = station.get("thresholds", {})
        red_threshold = thresholds.get("red", 999)  # Default high to avoid false positives

        levels = all_levels.get(station_id, [])
        if not levels:
            continue

        # Find periods above red threshold
        current_episode = None
        for lv in levels:
            height = lv.get("height_m")
            ts = lv["timestamp"]

            if height is None:
                continue

            if height >= red_threshold:
                if current_episode is None:
                    current_episode = {
                        "start": ts,
                        "end": ts,
                        "peak_height": height,
                        "peak_time": ts,
                        "station_id": station_id,
                        "station_name": station["name"],
                    }
                else:
                    current_episode["end"] = ts
                    if height > current_episode["peak_height"]:
                        current_episode["peak_height"] = height
                        current_episode["peak_time"] = ts
            else:
                if current_episode is not None:
                    # Check gap
                    gap = (ts - current_episode["end"]).total_seconds() / 3600
                    if gap > 12:  # 12 hour gap to end episode
                        episodes_raw.append(current_episode)
                        current_episode = None

        if current_episode is not None:
            episodes_raw.append(current_episode)

    print(f"Found {len(episodes_raw)} river threshold exceedances")

    # Convert to DetectedEpisode format
    episodes = []
    for ep in episodes_raw:
        duration = (ep["end"] - ep["start"]).total_seconds() / 3600

        episode = DetectedEpisode(
            start_time=ep["start"],
            end_time=ep["end"],
            peak_time=ep["peak_time"],
            peak_score=0,  # Unknown
            peak_level="red",
            duration_hours=duration,
            max_river_levels={ep["station_id"]: ep["peak_height"]},
            total_rain_mm=0,  # To be enriched
            max_tide_coef=None,
            detection_method="river_threshold",
            severity_estimate=_estimate_severity_from_height(ep["peak_height"]),
            description=f"Niveau {ep['station_name']} > seuil rouge ({ep['peak_height']:.2f}m)",
        )
        episodes.append(episode)

    return episodes


async def _enrich_episode(ep: dict) -> Optional[DetectedEpisode]:
    """Enrich a raw episode with context data."""
    settings = get_settings()

    start = ep["start"]
    end = ep["end"]
    peak_time = ep["peak_time"]

    # Query context data around the episode
    context_start = start - timedelta(hours=24)
    context_end = end + timedelta(hours=6)

    # Get river levels
    river_data = await influx_client.query_all_stations_levels(context_start, context_end)
    max_levels = {}
    for station_id, levels in river_data.items():
        if levels:
            max_height = max(lv.get("height_m", 0) or 0 for lv in levels)
            if max_height > 0:
                max_levels[station_id] = max_height

    # Get rainfall
    rainfall_data = await influx_client.query_rainfall(context_start, end)
    total_rain = 0
    for rp in rainfall_data:
        if rp.get("precipitation_mm_h"):
            total_rain += rp["precipitation_mm_h"]

    # Get tide coefficients
    tide_data = await influx_client.query_tides("BORDEAUX", context_start, context_end)
    max_coef = None
    for td in tide_data:
        coef = td.get("coefficient")
        if coef and (max_coef is None or coef > max_coef):
            max_coef = coef

    # Estimate severity
    severity = _estimate_severity(
        peak_score=ep["peak_score"],
        duration_hours=ep["duration_hours"],
        max_levels=max_levels,
        total_rain=total_rain,
        max_coef=max_coef,
    )

    # Generate description
    description = _generate_description(
        ep, max_levels, total_rain, max_coef, settings.hubeau_stations
    )

    return DetectedEpisode(
        start_time=start,
        end_time=end,
        peak_time=peak_time,
        peak_score=ep["peak_score"],
        peak_level=ep["peak_level"],
        duration_hours=ep["duration_hours"],
        max_river_levels=max_levels,
        total_rain_mm=round(total_rain, 1),
        max_tide_coef=max_coef,
        detection_method="risk_score",
        severity_estimate=severity,
        description=description,
    )


def _estimate_severity(
    peak_score: float,
    duration_hours: float,
    max_levels: dict,
    total_rain: float,
    max_coef: Optional[int],
) -> int:
    """Estimate episode severity on 1-5 scale."""
    severity = 1

    # Score-based
    if peak_score >= 75:
        severity = max(severity, 4)
    elif peak_score >= 55:
        severity = max(severity, 3)
    elif peak_score >= 35:
        severity = max(severity, 2)

    # Duration bonus
    if duration_hours >= 48:
        severity = min(5, severity + 1)
    elif duration_hours >= 24:
        severity = min(5, severity + 0.5)

    # Rain bonus
    if total_rain >= 100:
        severity = min(5, severity + 1)
    elif total_rain >= 50:
        severity = min(5, severity + 0.5)

    # Tide bonus
    if max_coef and max_coef >= 105:
        severity = min(5, severity + 0.5)

    return int(round(severity))


def _estimate_severity_from_height(height_m: float) -> int:
    """Estimate severity from max river height."""
    if height_m >= 6.0:
        return 5
    elif height_m >= 5.0:
        return 4
    elif height_m >= 4.0:
        return 3
    elif height_m >= 3.5:
        return 2
    else:
        return 1


def _generate_description(
    ep: dict,
    max_levels: dict,
    total_rain: float,
    max_coef: Optional[int],
    stations_config: list,
) -> str:
    """Generate human-readable description of the episode."""
    parts = []

    # Peak info
    peak_time = ep["peak_time"]
    peak_score = ep["peak_score"]
    parts.append(f"Pic de risque {peak_score:.0f}/100 le {peak_time.strftime('%d/%m/%Y à %Hh')}")

    # Duration
    duration = ep["duration_hours"]
    if duration >= 24:
        parts.append(f"durée {duration:.0f}h")
    else:
        parts.append(f"durée {duration:.1f}h")

    # Max river levels
    station_names = {s["id"]: s["name"] for s in stations_config}
    for station_id, height in sorted(max_levels.items(), key=lambda x: x[1], reverse=True)[:2]:
        name = station_names.get(station_id, station_id)
        parts.append(f"{name}: {height:.2f}m")

    # Rain
    if total_rain >= 20:
        parts.append(f"pluie cumulée: {total_rain:.0f}mm")

    # Tide
    if max_coef and max_coef >= 90:
        parts.append(f"coef marée: {max_coef}")

    return " | ".join(parts)


async def run_episode_detection(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> dict:
    """
    Run full episode detection on the backfilled data.

    Returns summary with all detected episodes for user validation.
    """
    if start_date is None:
        start_date = datetime(2022, 11, 1)
    if end_date is None:
        end_date = datetime.utcnow()

    print(f"\n{'='*60}")
    print(f"STARTING EPISODE DETECTION")
    print(f"Period: {start_date.date()} -> {end_date.date()}")
    print(f"{'='*60}")

    # Detect from risk scores
    risk_episodes = await detect_episodes_from_risk_scores(start_date, end_date)

    # Detect from river thresholds
    river_episodes = await detect_episodes_from_river_levels(start_date, end_date)

    # Merge overlapping episodes
    all_episodes = risk_episodes + river_episodes
    merged = _merge_episodes(all_episodes)

    # Sort by peak time
    merged.sort(key=lambda e: e.peak_time, reverse=True)

    print(f"\n{'='*60}")
    print(f"DETECTION COMPLETE")
    print(f"Found {len(merged)} potential flood episodes")
    print(f"{'='*60}")

    for i, ep in enumerate(merged, 1):
        print(f"\n{i}. {ep.peak_time.strftime('%Y-%m-%d %H:%M')} - Severity {ep.severity_estimate}/5")
        print(f"   {ep.description}")

    return {
        "period": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        "episodes_count": len(merged),
        "episodes": [ep.to_dict() for ep in merged],
    }


def _merge_episodes(episodes: list[DetectedEpisode]) -> list[DetectedEpisode]:
    """Merge overlapping or adjacent episodes."""
    if not episodes:
        return []

    # Sort by start time
    sorted_eps = sorted(episodes, key=lambda e: e.start_time)
    merged = [sorted_eps[0]]

    for ep in sorted_eps[1:]:
        last = merged[-1]

        # Check if episodes overlap or are within 24h
        gap = (ep.start_time - last.end_time).total_seconds() / 3600

        if gap <= 24:
            # Merge
            merged[-1] = DetectedEpisode(
                start_time=last.start_time,
                end_time=max(last.end_time, ep.end_time),
                peak_time=last.peak_time if last.peak_score >= ep.peak_score else ep.peak_time,
                peak_score=max(last.peak_score, ep.peak_score),
                peak_level=last.peak_level if last.peak_score >= ep.peak_score else ep.peak_level,
                duration_hours=(max(last.end_time, ep.end_time) - last.start_time).total_seconds() / 3600,
                max_river_levels={**last.max_river_levels, **ep.max_river_levels},
                total_rain_mm=max(last.total_rain_mm, ep.total_rain_mm),
                max_tide_coef=max(last.max_tide_coef or 0, ep.max_tide_coef or 0) or None,
                detection_method=f"{last.detection_method}+{ep.detection_method}" if last.detection_method != ep.detection_method else last.detection_method,
                severity_estimate=max(last.severity_estimate, ep.severity_estimate),
                description=last.description if last.peak_score >= ep.peak_score else ep.description,
            )
        else:
            merged.append(ep)

    return merged
