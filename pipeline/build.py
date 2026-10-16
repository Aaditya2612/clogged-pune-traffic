"""Build the Pune traffic dataset end-to-end.

1. Read a local OpenStreetMap PBF (Geofabrik Western Zone) with pyrosm,
   filtering to the Pune core bounding box.
2. Compute per-segment free-flow speed from OSM tags.
3. Assign persistent-congestion + bottleneck flags.
4. Generate a 168-hour (hour-of-week) speed/congestion profile per segment,
   calibrated to TomTom Traffic Index Pune 2025.
5. Write segments.geojson + profile.npz for the API to serve.
"""

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pyrosm import OSM

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (  # noqa: E402
    BBOX,
    BUCKETS,
    DATA_DIR,
    DEFAULT_FREEFLOW,
    DEFAULT_FREEFLOW_FALLBACK,
    PEAK_HOURS,
    PERSISTENT_FRACTION,
    PROFILE_FILE,
    PUNE_AREAS,
    SEGMENTS_FILE,
    UNNAMED_ROAD_DEFAULT,
    UNNAMED_ROAD_SUFFIX,
    WEEKDAY_CONGESTION,
    WEEKEND_CONGESTION,
)

PBF_FILE = DATA_DIR / "western-zone-latest.osm.pbf"


def parse_maxspeed(value):
    """Extract a numeric speed in km/h from an OSM maxspeed value."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (list, tuple, set)):
        for v in value:
            s = parse_maxspeed(v)
            if s is not None:
                return s
        return None
    text = str(value).strip()
    if text.lower() in ("none", "signals", "variable", "walk", ""):
        return None
    m = re.search(r"(\d+)", text)
    if not m:
        return None
    num = int(m.group(1))
    if "mph" in text.lower():
        num = int(num * 1.60934)
    return num


def freeflow_speed(highway, maxspeed):
    ms = parse_maxspeed(maxspeed)
    if ms is not None:
        return float(min(ms, 100))
    hw = highway
    if isinstance(hw, list):
        hw = hw[0] if hw else ""
    hw = str(hw) if hw else ""
    return float(DEFAULT_FREEFLOW.get(hw, DEFAULT_FREEFLOW_FALLBACK))


def congestion_curve_for_dow(dow):
    if dow < 5:  # Mon-Fri
        return [WEEKDAY_CONGESTION[h] for h in range(24)]
    return [WEEKEND_CONGESTION[h] for h in range(24)]


def bucket_for(congestion):
    for idx, _label, lo, hi in BUCKETS:
        if lo <= congestion < hi:
            return idx
    return 3


def nearest_area_name(lat, lon):
    """Name of the closest Pune locality (used to label unnamed roads)."""
    best, bd = PUNE_AREAS[0][0], 1e18
    coslat = 0.949  # ~cos(18.5 deg)
    for name, alat, alon, _r in PUNE_AREAS:
        d = (lat - alat) ** 2 + ((lon - alon) * coslat) ** 2
        if d < bd:
            bd, best = d, name
    return best


def build():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Reading OSM PBF: {PBF_FILE.name} ...")
    bbox = [BBOX["west"], BBOX["south"], BBOX["east"], BBOX["north"]]
    osm = OSM(str(PBF_FILE), bounding_box=bbox)
    edges = osm.get_network(network_type="driving")
    print(f"Raw edges: {len(edges)}")
    edges = edges.to_crs(epsg=4326)

    # keep only ways that have a geometry
    edges = edges[edges["geometry"].notna()].reset_index(drop=True)
    n = len(edges)
    print(f"Segments with geometry: {n}")

    highway = edges["highway"].fillna("").astype(str)
    maxspeed = edges["maxspeed"] if "maxspeed" in edges.columns else pd.Series([None] * n)

    freeflow = np.array(
        [freeflow_speed(highway.iloc[i], maxspeed.iloc[i]) for i in range(n)],
        dtype=np.float32,
    )

    # node degree for bottleneck detection
    u = edges["u"] if "u" in edges.columns else None
    v = edges["v"] if "v" in edges.columns else None
    if u is not None and v is not None:
        all_nodes = pd.concat([u, v], ignore_index=True)
        node_degree = all_nodes.value_counts()
        u_deg = u.map(node_degree).fillna(1).astype(int).to_numpy()
        v_deg = v.map(node_degree).fillna(1).astype(int).to_numpy()
        junc_deg = np.maximum(u_deg, v_deg)
    else:
        junc_deg = np.ones(n, dtype=int)

    rng = np.random.default_rng(42)
    seg_seeds = rng.integers(0, 2**31, size=n, dtype=np.int64)

    is_arterial = highway.isin(
        ["motorway", "trunk", "primary", "secondary", "tertiary"]
    ).to_numpy()
    persistent_mask = (seg_seeds % 1000) < int(PERSISTENT_FRACTION * 1000)
    bottleneck_mask = junc_deg >= 5

    seg_bias = np.ones(n, dtype=np.float32)
    seg_bias[persistent_mask] = 1.30
    seg_bias[is_arterial] *= 1.05
    seg_bias = np.clip(seg_bias, 0.5, 2.5)

    # build 168-hour profile
    profile_cong = np.zeros((n, 168), dtype=np.float32)
    profile_speed = np.zeros((n, 168), dtype=np.float32)
    profile_bucket = np.zeros((n, 168), dtype=np.int8)

    for dow in range(7):
        curve = congestion_curve_for_dow(dow)
        day_rng = np.random.default_rng(12345 + dow * 1000)
        for hour in range(24):
            hw = dow * 24 + hour
            base = np.full(n, curve[hour], dtype=np.float32)
            noise = day_rng.uniform(-4.0, 4.0, size=n).astype(np.float32)
            bump = np.zeros(n, dtype=np.float32)
            if hour in PEAK_HOURS:
                bump[bottleneck_mask] = 18.0

            cong = np.clip(base * seg_bias + bump + noise, 0.0, 300.0)
            speed = freeflow / (1.0 + cong / 100.0)

            profile_cong[:, hw] = cong
            profile_speed[:, hw] = speed
            profile_bucket[:, hw] = [bucket_for(c) for c in cong]

    # build GeoJSON
    records = []
    oneway_col = edges["oneway"] if "oneway" in edges.columns else pd.Series([None] * n)
    name_col = edges["name"] if "name" in edges.columns else pd.Series([None] * n)
    length_col = edges["length"] if "length" in edges.columns else pd.Series([0] * n)

    for i in range(n):
        geom = edges.iloc[i]["geometry"]
        if geom is None or geom.is_empty:
            continue
        name = name_col.iloc[i]
        if isinstance(name, list):
            name = name[0] if name else None
        if isinstance(name, float) and np.isnan(name):
            name = None
        if not name:
            # label unnamed roads after their nearest sublocality
            coords = list(geom.coords)
            clat = sum(c[1] for c in coords) / len(coords)
            clon = sum(c[0] for c in coords) / len(coords)
            area = nearest_area_name(clat, clon)
            suffix = UNNAMED_ROAD_SUFFIX.get(str(highway.iloc[i]), UNNAMED_ROAD_DEFAULT)
            name = f"{area} {suffix}"
        ow = oneway_col.iloc[i]
        oneway = bool(ow) if not isinstance(ow, float) or not np.isnan(ow) else False
        records.append(
            {
                "type": "Feature",
                "id": int(i),
                "geometry": json.loads(json.dumps(geom.__geo_interface__)),
                "properties": {
                    "id": int(i),
                    "name": str(name) if name else None,
                    "highway": highway.iloc[i],
                    "length_m": float(length_col.iloc[i] or 0),
                    "oneway": oneway,
                    "freeflow": round(float(freeflow[i]), 1),
                    "persistent": bool(persistent_mask[i]),
                    "bottleneck": bool(bottleneck_mask[i]),
                    "arterial": bool(is_arterial[i]),
                    "junc_deg": int(junc_deg[i]),
                },
            }
        )

    keep = [r["id"] for r in records]
    profile_cong = profile_cong[keep]
    profile_speed = profile_speed[keep]
    profile_bucket = profile_bucket[keep]
    seg_bias = seg_bias[keep]
    freeflow = freeflow[keep]
    for idx, r in enumerate(records):
        r["id"] = idx
        r["properties"]["id"] = idx

    fc = {"type": "FeatureCollection", "features": records}
    SEGMENTS_FILE.write_text(json.dumps(fc), encoding="utf-8")

    np.savez_compressed(
        PROFILE_FILE,
        congestion=profile_cong,
        speed=profile_speed,
        bucket=profile_bucket,
        bias=seg_bias,
        freeflow=freeflow,
    )

    print(f"Wrote {len(records)} segments -> {SEGMENTS_FILE}")
    print(f"Wrote profile {profile_cong.shape} -> {PROFILE_FILE}")


if __name__ == "__main__":
    build()
