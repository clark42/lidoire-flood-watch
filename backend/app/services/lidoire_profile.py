"""Lidoire long profile, confluence stage estimate and backwater reach (PURE).

The mill stands on the Lidoire about 3.9 km (as the crow flies) above its
confluence with the Dordogne. Its ground is ~12 m NGF, the confluence
~2.4-3.4 m NGF, so a Dordogne level at the confluence is never compared
with the mill floor directly: the honest picture is the Lidoire long profile
from the mill down to the confluence, and how far up it the Dordogne
backwater reaches.

- Profile: ``app/data/lidoire_profile.json``, ground z (IGN RGE ALTI)
  sampled every ~100 m along the Sandre BD TOPAGE line of the Lidoire, from
  ~1 km above the mill to the confluence (``scripts/build_lidoire_profile.py``).
  ``d_m`` = distance along the river from the confluence (0) upstream.
- Confluence stage Z_c (ESTIMATED, no gauge there): weighted mean of the
  Pessac (7 km upstream) and Libourne (~30 km downstream) water levels in
  m NGF, ``(1 - alpha) * (zero_pessac + H_pessac) + alpha * (zero_libourne +
  H_libourne)``. NGF zeros and alpha come from ``config.py``.
- Backwater reach (APPROXIMATION): flat water at Z_c from the confluence,
  i.e. the furthest profile point upstream whose ground z <= Z_c, with a
  linear interpolation to the next sample above Z_c (and the reach
  contiguous from the confluence, which stops at the first rise). It ignores the
  Lidoire's own flow, the water slope, bridges, weirs and the channel bed
  (z is the terrain on the TOPAGE line, often the bank or the water surface
  of the survey day, not the bed).

Display only: the risk model v3.4 never reads this module (guard test).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Sequence

from app.geo import haversine_km

PROFILE_FILE = Path(__file__).resolve().parents[1] / "data" / "lidoire_profile.json"

ESTIMATE_LABEL = "estime"
APPROX_LABEL = "approximation plan d'eau horizontal"


# ---------------------------------------------------------------------------
# Polyline helpers (also used by scripts/build_lidoire_profile.py)
# ---------------------------------------------------------------------------


def cumulative_m(coords: Sequence[tuple[float, float]]) -> list[float]:
    """Cumulative distance (m) along (lon, lat) vertices, 0 at the first one."""
    out = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        out.append(out[-1] + 1000.0 * haversine_km(lat1, lon1, lat2, lon2))
    return out


def _local_xy(lon: float, lat: float, lat0: float) -> tuple[float, float]:
    """Equirectangular metres around ``lat0`` (enough over a few km)."""
    k = 1000.0 * math.pi * 6371.0 / 180.0
    return lon * k * math.cos(math.radians(lat0)), lat * k


def project_onto(coords: Sequence[tuple[float, float]], lon: float, lat: float) -> tuple[float, float]:
    """(distance along the line from its first vertex, offset to the line), metres."""
    cum = cumulative_m(coords)
    px, py = _local_xy(lon, lat, lat)
    best = (math.inf, 0.0)
    for i in range(len(coords) - 1):
        ax, ay = _local_xy(*coords[i], lat)
        bx, by = _local_xy(*coords[i + 1], lat)
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        u = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        off = math.hypot(px - (ax + u * dx), py - (ay + u * dy))
        if off < best[0]:
            best = (off, cum[i] + u * (cum[i + 1] - cum[i]))
    return best[1], best[0]


def point_at(coords: Sequence[tuple[float, float]], cum: Sequence[float], d: float) -> tuple[float, float]:
    """(lon, lat) at distance ``d`` (m) along the line (clamped to its ends)."""
    if d <= 0:
        return coords[0]
    if d >= cum[-1]:
        return coords[-1]
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cum[mid] <= d:
            lo = mid
        else:
            hi = mid
    span = cum[hi] - cum[lo]
    u = 0.0 if span == 0 else (d - cum[lo]) / span
    (x1, y1), (x2, y2) = coords[lo], coords[hi]
    return x1 + u * (x2 - x1), y1 + u * (y2 - y1)


# ---------------------------------------------------------------------------
# Profile file
# ---------------------------------------------------------------------------


@lru_cache
def load_profile(path: Optional[str] = None) -> dict:
    """The committed profile (``metadata`` + ``points`` sorted by ``d_m``)."""
    with Path(path or PROFILE_FILE).open(encoding="utf-8") as f:
        data = json.load(f)
    data["points"] = sorted(data["points"], key=lambda p: p["d_m"])
    return data


# ---------------------------------------------------------------------------
# Confluence stage and backwater reach
# ---------------------------------------------------------------------------


def envelope_max(points: Iterable[tuple[datetime, float]], t: datetime, hours: float) -> Optional[float]:
    """Max of the values with timestamp in (t - hours, t] (causal), None if none."""
    lo = t - timedelta(hours=hours)
    vals = [v for ts, v in points if v is not None and lo < ts <= t]
    return max(vals) if vals else None


def latest_value(points: Iterable[tuple[datetime, float]], t: datetime,
                 max_age_hours: float) -> Optional[tuple[datetime, float]]:
    """Newest (timestamp, value) at or before ``t``, not older than ``max_age_hours``."""
    best = None
    for ts, v in points:
        if v is None or ts > t or t - ts > timedelta(hours=max_age_hours):
            continue
        if best is None or ts > best[0]:
            best = (ts, v)
    return best


def confluence_stage(
    pessac_h: Optional[float],
    libourne_h: Optional[float],
    pessac_zero: float,
    libourne_zero: float,
    alpha: float,
) -> tuple[Optional[float], list[str]]:
    """Z_c in m NGF from two station heights (m, station datum), with notes.

    Libourne missing: Pessac alone (alpha = 0), noted. Pessac missing: None
    (Pessac is the backwater proxy; Libourne alone is tidal).
    """
    notes: list[str] = []
    if pessac_h is None:
        return None, ["Pessac indisponible : Z_c non estime"]
    z_pessac = pessac_zero + pessac_h
    if libourne_h is None:
        notes.append("Libourne indisponible : Z_c = niveau NGF de Pessac seul (alpha = 0)")
        return round(z_pessac, 3), notes
    z = (1.0 - alpha) * z_pessac + alpha * (libourne_zero + libourne_h)
    return round(z, 3), notes


def backwater_reach(points: Sequence[dict], z_c: Optional[float]) -> dict:
    """Furthest point upstream (from the confluence) whose ground z <= Z_c.

    ``points``: ``{d_m, z_m, lon, lat}`` sorted by ``d_m`` (0 = confluence).
    The crossing is interpolated linearly between the last sample at or
    below Z_c and the next one upstream (above Z_c by construction).
    ``status``: ``none`` (no sample under Z_c), ``partial`` or
    ``whole_profile`` (the last upstream sample is under Z_c: the reach is
    at least the profile end).

    ``distance_m`` is that furthest point (it may lie beyond a rise above
    Z_c, e.g. a weir: an upper bound). ``connected_distance_m`` stops at the
    first sample above Z_c walking up from the confluence (water connected
    to the Dordogne); ``disconnected_low_points`` flags the difference.
    """
    base = {"label": APPROX_LABEL, "status": "unknown", "distance_m": None,
            "lon": None, "lat": None, "z_m": None,
            "connected_distance_m": None, "disconnected_low_points": None}
    pts = [p for p in points if p.get("z_m") is not None]
    if z_c is None or not pts:
        return base
    idx = [i for i, p in enumerate(pts) if p["z_m"] <= z_c]
    if not idx:
        return {**base, "status": "none", "distance_m": 0.0,
                "connected_distance_m": 0.0, "disconnected_low_points": False}
    # Contiguous from the confluence: stops at the first sample above Z_c
    j = 0 if pts[0]["z_m"] <= z_c else None
    while j is not None and j + 1 < len(pts) and pts[j + 1]["z_m"] <= z_c:
        j += 1
    connected = 0.0 if j is None else _crossing(pts, j, z_c)["d_m"]
    i = idx[-1]
    cross = _crossing(pts, i, z_c)
    return {
        **base,
        "status": "whole_profile" if i == len(pts) - 1 else "partial",
        "distance_m": cross["d_m"],
        "lon": cross["lon"],
        "lat": cross["lat"],
        "z_m": cross["z_m"],
        "connected_distance_m": connected,
        "disconnected_low_points": bool(cross["d_m"] > connected + 1e-6),
    }


def _crossing(pts: Sequence[dict], i: int, z_c: float) -> dict:
    """Interpolated point where the ground rises above Z_c just upstream of sample ``i``."""
    p = pts[i]
    if i == len(pts) - 1:
        return {"d_m": round(p["d_m"], 1), "lon": p["lon"], "lat": p["lat"], "z_m": p["z_m"]}
    q = pts[i + 1]
    dz = q["z_m"] - p["z_m"]
    u = 0.0 if dz <= 0 else max(0.0, min(1.0, (z_c - p["z_m"]) / dz))
    return {
        "d_m": round(p["d_m"] + u * (q["d_m"] - p["d_m"]), 1),
        "lon": round(p["lon"] + u * (q["lon"] - p["lon"]), 6),
        "lat": round(p["lat"] + u * (q["lat"] - p["lat"]), 6),
        "z_m": round(z_c, 3),
    }


def reach_relative_to_mill(reach: dict, mill_d_m: float) -> dict:
    """Distance (m) between the backwater front and the mill along the river.

    Positive = the front is downstream of the mill (dry ground between).
    """
    d = reach.get("distance_m")
    c = reach.get("connected_distance_m")
    return {
        "distance_below_mill_m": None if d is None else round(mill_d_m - d, 1),
        "reaches_mill": None if d is None else bool(d >= mill_d_m),
        "connected_distance_below_mill_m": None if c is None else round(mill_d_m - c, 1),
    }
