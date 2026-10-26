"""Episode detection service.

Analyzes backfilled historical data to detect potential flood episodes
based on the risk model level thresholds.

Detection criteria:
- Risk score >= 55 (RED level) for at least 2 consecutive HOURS
- OR risk score >= 35 (ORANGE level) for at least 6 consecutive HOURS
- OR river level exceeds a (non-tidal) station red threshold

Durations are measured in hours, not samples: a run of consecutive samples
(no gap larger than 1.5 x the series step) lasts (last - first) + step, so a
6-hourly backfill sample counts as 6h. Libourne (weight 0, tidal) is
excluded. Episodes are merged if they're within 24h of each other.

Memory is bounded whatever the range: Influx is read in windows of
``DETECTION_CHUNK_DAYS`` with the episode state carried across windows, so
the result is the one of a single pass. Only the current model version (and
untagged legacy points) of ``risk_score`` is read.
"""

import logging
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from dataclasses import dataclass, asdict

from app.config import get_settings
from app.services import influx_client
from app.services.risk_core import floor_hour, score_to_level, to_utc_naive

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


# Risk level thresholds (v2.5 calibration, unchanged in v3.3)
RISK_RED_THRESHOLD = 55
RISK_ORANGE_THRESHOLD = 35
RISK_YELLOW_THRESHOLD = 15


def _series_step_hours(timestamps: list[datetime]) -> float:
    """Nominal sampling step (hours) of a sorted series: median spacing."""
    diffs = [
        (b - a).total_seconds() / 3600
        for a, b in zip(timestamps, timestamps[1:])
        if b > a
    ]
    return statistics.median(diffs) if diffs else 1.0


def longest_run_hours(
    samples: list[tuple[datetime, float]],
    threshold: float,
    step_hours: Optional[float] = None,
) -> float:
    """Longest run (hours) of CONSECUTIVE samples with value >= threshold.

    Samples are (timestamp, value) sorted by time. Two samples are
    consecutive when their gap is <= 1.5 x step. A run lasts
    (last - first) + step hours.
    """
    if not samples:
        return 0.0
    if step_hours is None:
        step_hours = _series_step_hours([ts for ts, _ in samples])
    max_gap_h = 1.5 * step_hours

    best = 0.0
    run_start: Optional[datetime] = None
    prev: Optional[datetime] = None
    for ts, value in samples:
        above = value >= threshold
        contiguous = prev is not None and (ts - prev).total_seconds() / 3600 <= max_gap_h
        if above:
            if run_start is None or not contiguous:
                run_start = ts
            best = max(best, (ts - run_start).total_seconds() / 3600 + step_hours)
        else:
            run_start = None
        prev = ts
    return best


def hourly_rain_total(rows: list[dict], start: datetime, end: datetime) -> float:
    """Total rain (mm) over [start, end]: mean per hour across sources, then sum."""
    from app.services.risk_v2 import rain_hourly_from_records

    s = to_utc_naive(start)
    e = to_utc_naive(end)
    hourly = rain_hourly_from_records(rows)
    # Hour buckets (hour_end - 1h, hour_end] overlapping [start, end]
    return sum(
        mm for hour_end, mm in hourly.items()
        if hour_end > s and hour_end - timedelta(hours=1) < e
    )


def _active_station_ids() -> set[str]:
    """Stations with weight > 0 (Libourne, tidal, excluded)."""
    return {s["id"] for s in get_settings().hubeau_stations if s.get("weight", 0) > 0}


# Long ranges (years) are scanned in chunks of this many days, the episode
# state being carried from one chunk to the next: memory is bounded by one
# chunk whatever the range, and the result is identical to a single pass.
# A single 2022-11 -> 2026-10 query (~800k points at full resolution)
# OOM-killed a 1 GiB backend.
DETECTION_CHUNK_DAYS = 30


def detection_chunks(
    start: datetime,
    end: datetime,
    chunk_days: float = DETECTION_CHUNK_DAYS,
) -> list[tuple[datetime, datetime]]:
    """Contiguous half-open windows [a, b) covering [start, end), naive UTC.

    Inner bounds are whole hours, so an hour bucket (used to prefer realtime
    over backfill risk points) never straddles two chunks.
    """
    step = timedelta(days=chunk_days)
    if step < timedelta(hours=1):
        raise ValueError("chunk_days must cover at least one hour")
    s, e = to_utc_naive(start), to_utc_naive(end)
    if e <= s:
        return []
    bounds = [s]
    inner = floor_hour(s) + step
    while inner < e:
        bounds.append(inner)
        inner += step
    bounds.append(e)
    return list(zip(bounds, bounds[1:]))


def _median_from_counts(counts: Counter) -> float:
    """``statistics.median`` of a multiset given as {value: count} (1.0 if empty)."""
    n = sum(counts.values())
    if n == 0:
        return 1.0

    def nth(k: int) -> float:
        seen = 0
        for value in sorted(counts):
            seen += counts[value]
            if k < seen:
                return value
        raise IndexError(k)

    if n % 2:
        return nth(n // 2)
    return (nth(n // 2 - 1) + nth(n // 2)) / 2


class _RiskEpisodeScan:
    """Streaming episode builder over time-ordered (timestamp, score) samples.

    A point >= threshold opens or extends an episode; a point below it closes
    the open episode once it is more than ``merge_gap_hours`` after the
    episode's last significant point. Every sample seen while an episode is
    open is kept (trimmed to the episode span on close) so the consecutive
    run lengths can be measured afterwards. The sampling step (median
    spacing of the whole series) is accumulated as a histogram.
    """

    def __init__(self, threshold: float, merge_gap_hours: float):
        self.threshold = threshold
        self.merge_gap_hours = merge_gap_hours
        self.episodes: list[dict] = []
        self.current: Optional[dict] = None
        self.spacings: Counter = Counter()
        self.prev_ts: Optional[datetime] = None
        self.count = 0

    def feed(self, ts: datetime, score: float) -> None:
        if self.prev_ts is not None and ts > self.prev_ts:
            self.spacings[(ts - self.prev_ts).total_seconds() / 3600] += 1
        self.prev_ts = ts
        self.count += 1

        cur = self.current
        if score >= self.threshold:
            # Level derived from the score (legacy points may lack the level)
            level = score_to_level(score).value
            if cur is None:
                self.current = {
                    "start": ts,
                    "end": ts,
                    "peak_score": score,
                    "peak_time": ts,
                    "peak_level": level,
                    "samples": [(ts, score)],
                }
            else:
                cur["end"] = ts
                cur["samples"].append((ts, score))
                if score > cur["peak_score"]:
                    cur["peak_score"] = score
                    cur["peak_time"] = ts
                    cur["peak_level"] = level
        elif cur is not None:
            cur["samples"].append((ts, score))
            if (ts - cur["end"]).total_seconds() / 3600 > self.merge_gap_hours:
                self._close()

    def _close(self) -> None:
        cur = self.current
        cur["samples"] = [s for s in cur["samples"] if s[0] <= cur["end"]]
        self.episodes.append(cur)
        self.current = None

    def finish(self) -> list[dict]:
        if self.current is not None:
            self._close()
        return self.episodes

    @property
    def step_hours(self) -> float:
        return _median_from_counts(self.spacings)


async def detect_episodes_from_risk_scores(
    start_date: datetime,
    end_date: datetime,
    min_red_hours: int = 2,
    min_orange_hours: int = 6,
    threshold: float = RISK_ORANGE_THRESHOLD,
    merge_gap_hours: int = 24,
    chunk_days: float = DETECTION_CHUNK_DAYS,
) -> list[DetectedEpisode]:
    """
    Detect flood episodes from stored risk scores.

    Args:
        start_date: Start of analysis period
        end_date: End of analysis period
        min_red_hours: Minimum consecutive RED hours to trigger detection
        min_orange_hours: Minimum consecutive ORANGE hours to trigger detection
        merge_gap_hours: Merge episodes that are within this gap
        chunk_days: Influx is read in windows of this size (memory bound)

    Returns:
        List of detected episodes
    """
    logger.info("Detecting episodes from risk scores: %s -> %s", start_date.date(), end_date.date())

    # Current model version (+ legacy points) only, one row per timestamp
    scan = _RiskEpisodeScan(threshold, merge_gap_hours)
    for chunk_start, chunk_end in detection_chunks(start_date, end_date, chunk_days):
        rows = await influx_client.query_risk_score_values(chunk_start, chunk_end)
        points = sorted(
            ((to_utc_naive(rs["timestamp"]), rs["score"]) for rs in rows if rs.get("score") is not None),
            key=lambda p: p[0],
        )
        del rows
        for ts, score in points:
            scan.feed(ts, score)

    if scan.count == 0:
        logger.info("No risk score data found")
        return []

    logger.info("Found %d risk score records", scan.count)
    episodes_raw = scan.finish()
    step_hours = scan.step_hours

    # Filter episodes by duration criteria
    filtered_episodes = []
    for ep in episodes_raw:
        duration_hours = (ep["end"] - ep["start"]).total_seconds() / 3600 + step_hours

        # Longest CONSECUTIVE run, in hours (not a count of samples).
        # Every sample of the episode span (including sub-threshold ones)
        # is used so a dip below the threshold breaks the run.
        span = ep.pop("samples")
        red_hours = longest_run_hours(span, RISK_RED_THRESHOLD, step_hours)
        orange_hours = longest_run_hours(span, RISK_ORANGE_THRESHOLD, step_hours)

        # Check if episode meets criteria
        if red_hours >= min_red_hours or orange_hours >= min_orange_hours:
            filtered_episodes.append({
                **ep,
                "duration_hours": duration_hours,
                "red_hours": red_hours,
                "orange_hours": orange_hours,
            })

    logger.info("Found %d significant episodes", len(filtered_episodes))

    # Enrich episodes with context data
    enriched_episodes = []
    for ep in filtered_episodes:
        episode = await _enrich_episode(ep)
        if episode:
            enriched_episodes.append(episode)

    return enriched_episodes


# A river exceedance ends at the first below-red point more than this many
# hours after the last above-red point.
RIVER_EPISODE_GAP_HOURS = 12


async def detect_episodes_from_river_levels(
    start_date: datetime,
    end_date: datetime,
    chunk_days: float = DETECTION_CHUNK_DAYS,
) -> list[DetectedEpisode]:
    """
    Detect flood episodes from river level thresholds.

    Looks for periods where any station exceeds its red threshold. Levels
    are read at full resolution, one chunk at a time, the per-station state
    being carried across chunks.
    """
    logger.info("Detecting episodes from river levels")

    settings = get_settings()
    # Libourne (weight 0): tidal oscillation masks the flood signal
    stations = [s for s in settings.hubeau_stations if s.get("weight", 0) > 0]
    station_ids = [s["id"] for s in stations]
    current: dict[str, Optional[dict]] = {sid: None for sid in station_ids}
    closed: dict[str, list[dict]] = {sid: [] for sid in station_ids}

    for chunk_start, chunk_end in detection_chunks(start_date, end_date, chunk_days):
        heights = await influx_client.query_station_heights(chunk_start, chunk_end, station_ids)
        for station in stations:
            station_id = station["id"]
            thresholds = station.get("thresholds", {})
            red_threshold = thresholds.get("red", 999)  # Default high to avoid false positives
            episode = current[station_id]

            for ts, height in heights.get(station_id, []):
                if height is None:
                    continue
                ts = to_utc_naive(ts)

                if height >= red_threshold:
                    if episode is None:
                        episode = {
                            "start": ts,
                            "end": ts,
                            "peak_height": height,
                            "peak_time": ts,
                            "station_id": station_id,
                            "station_name": station["name"],
                        }
                    else:
                        episode["end"] = ts
                        if height > episode["peak_height"]:
                            episode["peak_height"] = height
                            episode["peak_time"] = ts
                elif episode is not None:
                    gap = (ts - episode["end"]).total_seconds() / 3600
                    if gap > RIVER_EPISODE_GAP_HOURS:
                        closed[station_id].append(episode)
                        episode = None

            current[station_id] = episode
        del heights

    # Station order, then time order (as a single pass over each station)
    episodes_raw = []
    for station_id in station_ids:
        episodes_raw.extend(closed[station_id])
        if current[station_id] is not None:
            episodes_raw.append(current[station_id])

    logger.info("Found %d river threshold exceedances", len(episodes_raw))

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
    active_ids = _active_station_ids()
    max_levels = {}
    for station_id, levels in river_data.items():
        if station_id not in active_ids:
            continue  # Libourne excluded (tidal)
        if levels:
            max_height = max(lv.get("height_m", 0) or 0 for lv in levels)
            if max_height > 0:
                max_levels[station_id] = max_height

    # Get rainfall: mean per hour across sources, then sum over hours
    rainfall_data = await influx_client.query_rainfall(context_start, end)
    total_rain = hourly_rain_total(rainfall_data, context_start, end)

    # Get tide coefficients
    tide_data = await influx_client.query_tides(get_settings().tide_port_confluence, context_start, context_end)
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
    threshold: float = RISK_ORANGE_THRESHOLD,
    chunk_days: float = DETECTION_CHUNK_DAYS,
) -> dict:
    """
    Run full episode detection on the backfilled data.

    ``threshold`` is the risk score above which a point belongs to an episode.
    ``chunk_days`` only bounds memory (Influx read window), not the result.

    Returns summary with all detected episodes for user validation.
    """
    if start_date is None:
        start_date = datetime(2022, 11, 1)
    if end_date is None:
        end_date = _utcnow()

    logger.info("Starting episode detection: %s -> %s", start_date.date(), end_date.date())

    # Detect from risk scores
    risk_episodes = await detect_episodes_from_risk_scores(
        start_date, end_date, threshold=threshold, chunk_days=chunk_days,
    )

    # Detect from river thresholds
    river_episodes = await detect_episodes_from_river_levels(start_date, end_date, chunk_days=chunk_days)

    # Merge overlapping episodes
    all_episodes = risk_episodes + river_episodes
    merged = _merge_episodes(all_episodes)

    # Sort by peak time
    merged.sort(key=lambda e: e.peak_time, reverse=True)

    logger.info("Episode detection complete: %d potential flood episodes", len(merged))
    for i, ep in enumerate(merged, 1):
        logger.info(
            "%d. %s - severity %s/5 - %s",
            i, ep.peak_time.strftime("%Y-%m-%d %H:%M"), ep.severity_estimate, ep.description,
        )

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
