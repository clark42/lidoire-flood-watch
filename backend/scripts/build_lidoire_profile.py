"""Build ``app/data/lidoire_profile.json`` (offline, network required).

    python -m scripts.build_lidoire_profile [--gml FILE] [--step 100] [--upstream 1000]

1. Lidoire line (Sandre BD TOPAGE 2019, CdOH 05C0000002000889946) from the
   Sandre WFS (GML 3.1.1, EPSG:4326), or ``--gml`` for a saved response. The
   line runs from the source to the confluence with the Dordogne.
2. Distances along the line from the confluence (haversine), mill projected
   onto the line.
3. Samples every ``--step`` m from ``--upstream`` m above the mill down to the
   confluence; ground z from the IGN Geoplateforme altimetry API (keyless,
   resource ``ign_rge_alti_wld``) on a short cross-section (default +/- 30 m,
   7 points): ``z_m`` is the section minimum (low ground: the wetted channel
   or the low bank), ``z_line_m`` the value on the line. Plus the mill point
   and a 60 m ring around it.

The committed file is the output: the backend never calls these APIs.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import date
from pathlib import Path

import httpx

from app.config import get_settings
from app.services.lidoire_profile import PROFILE_FILE, cumulative_m, point_at, project_onto

CD_OH = "05C0000002000889946"
WFS_URL = (
    "https://services.sandre.eaufrance.fr/geo/topage2019?SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature"
    "&TYPENAME=sa:CoursEau_FXX_Topage2019&SRSNAME=EPSG:4326&FILTER=%3CFilter%3E%3CPropertyIsEqualTo%3E"
    "%3CPropertyName%3ECdOH%3C/PropertyName%3E%3CLiteral%3E" + CD_OH +
    "%3C/Literal%3E%3C/PropertyIsEqualTo%3E%3C/Filter%3E"
)
ALTI_URL = "https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json"
ALTI_RESOURCE = "ign_rge_alti_wld"
BATCH = 50
NODATA = -100.0  # IGN no-data is -99999


def valid_min(values):
    vals = [v for v in values if v is not None and v > NODATA]
    return min(vals) if vals else None


def cross_section(line, cum, d, half_width, n):
    """``n`` points (odd) across the line at ``d``, from -half_width to +half_width metres."""
    lon, lat = point_at(line, cum, d)
    a = point_at(line, cum, max(0.0, d - 20.0))
    b = point_at(line, cum, min(cum[-1], d + 20.0))
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110574.0
    tx, ty = (b[0] - a[0]) * kx, (b[1] - a[1]) * ky
    norm = math.hypot(tx, ty) or 1.0
    nx, ny = -ty / norm, tx / norm  # unit normal, metres
    offsets = [half_width * (2 * k / (n - 1) - 1) for k in range(n)]
    return [(lon + o * nx / kx, lat + o * ny / ky) for o in offsets]


def parse_line(gml: str) -> list[tuple[float, float]]:
    """(lon, lat) vertices of the Lidoire LineString (GML posList is lat lon)."""
    for feature in gml.split("<gml:featureMember>")[1:]:
        if f"<sa:CdOH>{CD_OH}</sa:CdOH>" not in feature:
            continue
        lists = re.findall(r"<gml:posList[^>]*>(.*?)</gml:posList>", feature, re.S)
        if len(lists) != 1:
            raise ValueError(f"expected one LineString, got {len(lists)}")
        v = [float(x) for x in lists[0].split()]
        return [(v[i + 1], v[i]) for i in range(0, len(v), 2)]
    raise ValueError(f"CdOH {CD_OH} not found")


def elevations(client: httpx.Client, pts: list[tuple[float, float]]) -> list[float]:
    """Ground z of each point, in batches, paced and retried on 429 (rate limit)."""
    out: list[float] = []
    for i in range(0, len(pts), BATCH):
        chunk = pts[i:i + BATCH]
        for attempt in range(6):
            time.sleep(1.0 if attempt == 0 else 5.0 * attempt)
            resp = client.get(ALTI_URL, params={
                "lon": "|".join(f"{lon:.6f}" for lon, _ in chunk),
                "lat": "|".join(f"{lat:.6f}" for _, lat in chunk),
                "resource": ALTI_RESOURCE,
                "zonly": "true",
            }, timeout=60.0)
            if resp.status_code != 429:
                break
        resp.raise_for_status()
        out.extend(float(z) for z in resp.json()["elevations"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gml", help="saved WFS GetFeature response (default: fetch it)")
    parser.add_argument("--step", type=float, default=100.0)
    parser.add_argument("--upstream", type=float, default=1000.0, help="metres above the mill")
    parser.add_argument("--half-width", type=float, default=30.0, help="cross-section half width (m)")
    parser.add_argument("--section-points", type=int, default=7, help="points per cross-section (odd)")
    parser.add_argument("--out", default=str(PROFILE_FILE))
    args = parser.parse_args()

    settings = get_settings()
    with httpx.Client(follow_redirects=True) as client:
        gml = Path(args.gml).read_text(encoding="utf-8") if args.gml else client.get(WFS_URL, timeout=60.0).text
        line = list(reversed(parse_line(gml)))  # confluence first
        cum = cumulative_m(line)
        mill_d, mill_offset = project_onto(line, settings.moulin_lon, settings.moulin_lat)
        end = min(cum[-1], mill_d + args.upstream)
        n = int(math.floor(end / args.step))
        ds = [k * args.step for k in range(n + 1)]
        if ds[-1] < end:
            ds.append(end)
        samples = [point_at(line, cum, d) for d in ds]
        sections = [cross_section(line, cum, d, args.half_width, args.section_points) for d in ds]
        flat = [pt for sec in sections for pt in sec]
        zflat = elevations(client, flat)
        per = len(sections[0])
        z_sections = [zflat[i * per:(i + 1) * per] for i in range(len(sections))]
        zs = [valid_min(zsec) for zsec in z_sections]
        z_line = [z_sections[i][per // 2] for i in range(len(sections))]

        ring = [(settings.moulin_lon, settings.moulin_lat)]
        for k in range(8):
            a = 2 * math.pi * k / 8
            ring.append((
                settings.moulin_lon + 60.0 * math.sin(a) / (111320.0 * math.cos(math.radians(settings.moulin_lat))),
                settings.moulin_lat + 60.0 * math.cos(a) / 110574.0,
            ))
        ring_z = elevations(client, ring)

    points = [
        {"d_m": round(d, 1), "z_m": round(z, 2), "z_line_m": round(zl, 2) if zl > NODATA else None,
         "lon": round(lon, 6), "lat": round(lat, 6)}
        for d, (lon, lat), z, zl in zip(ds, samples, zs, z_line)
        if z is not None
    ]
    payload = {
        "metadata": {
            "name": "Profil en long de la Lidoire, du moulin a la confluence avec la Dordogne",
            "distance_origin": "confluence (0 m), distance le long du trace TOPAGE vers l'amont",
            "line_source": "Sandre, BD TOPAGE 2019 (IGN/OFB), couche CoursEau_FXX_Topage2019, CdOH " + CD_OH,
            "line_source_url": WFS_URL,
            "line_length_m": round(cum[-1], 1),
            "elevation_source": "IGN, RGE ALTI (API altimetrie de la Geoplateforme, ressource " + ALTI_RESOURCE + ")",
            "elevation_source_url": ALTI_URL,
            "licence": "Licence Ouverte / Open Licence v2.0 (Etalab) : Sandre BD TOPAGE et IGN RGE ALTI",
            "attribution": "Sandre - BD TOPAGE ; IGN - RGE ALTI",
            "vertical_datum": "NGF-IGN69 (m)",
            "retrieved": date.today().isoformat(),
            "step_m": args.step,
            "upstream_of_mill_m": args.upstream,
            "z_definition": (
                f"z_m = minimum du terrain sur une section de +/- {args.half_width:g} m perpendiculaire au trace "
                f"({args.section_points} points) : point bas (lit en eau ou berge basse) ; z_line_m = terrain sur le trace"
            ),
            "section_half_width_m": args.half_width,
            "section_points": args.section_points,
            "caveats": [
                "z = altitude du terrain (surface de l'eau au jour du leve LiDAR la ou il y a de l'eau), pas le fond du lit",
                "le trace TOPAGE peut s'ecarter du lit reel de quelques dizaines de metres",
                "ponts, seuils et remblais ne sont pas representes comme obstacles",
            ],
            "confluence": {"lon": round(line[0][0], 6), "lat": round(line[0][1], 6), "z_m": points[0]["z_m"]},
            "mill": {
                "lon": settings.moulin_lon,
                "lat": settings.moulin_lat,
                "d_m": round(mill_d, 1),
                "offset_from_line_m": round(mill_offset, 1),
                "ground_z_m": round(ring_z[0], 2),
                "ground_z_ring60m_min_m": round(min(ring_z), 2),
                "ground_z_ring60m_max_m": round(max(ring_z), 2),
            },
        },
        "points": points,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(points)} points, mill at {mill_d:.0f} m (offset {mill_offset:.0f} m), "
          f"z {points[0]['z_m']} -> {points[-1]['z_m']} m, mill ground {ring_z[0]} m")


if __name__ == "__main__":
    main()
