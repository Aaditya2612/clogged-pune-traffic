"""FastAPI backend for the Pune traffic dashboard.

Model: congestion = typical_dow_hour(base) * road_class_scale * persistent_scale
                   + bottleneck_bump (+ noise for history).
 - forecast mode: noise = 0  (expected / typical conditions for a future date)
 - history  mode: deterministic per-(date,hour,segment) noise  (past dates, <= 1 year)
"""

import json
import sys
from collections import defaultdict
from datetime import date as Date
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from config import (  # noqa: E402
    BOTTLENECK_THRESHOLD,
    BUCKET_COLORS,
    BUCKET_LABELS,
    BUCKETS,
    PEAK_HOURS,
    PUNE_AREAS,
    WEEKDAY_CONGESTION,
    WEEKEND_CONGESTION,
)

DATA_DIR = ROOT / "data"
SEGMENTS_FILE = DATA_DIR / "segments.geojson"
PROFILE_FILE = DATA_DIR / "profile.npz"
WEB_DIR = ROOT / "web"

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

HISTORY_DAYS = 365
FORECAST_DAYS = 30

# congestion % upper bounds of the first three buckets (used for bucketing)
_BOUNDS = np.array([BUCKETS[0][3], BUCKETS[1][3], BUCKETS[2][3]], dtype=np.float32)

# ---- load static data ----------------------------------------------------
_segments = json.loads(SEGMENTS_FILE.read_text(encoding="utf-8"))["features"]
_data = np.load(PROFILE_FILE)
FREEFLOW = _data["freeflow"].astype(np.float32)

N = len(_segments)
BOTTLENECK = np.array([f["properties"]["bottleneck"] for f in _segments], dtype=bool)
PERSISTENT = np.array([f["properties"]["persistent"] for f in _segments], dtype=bool)

# congestion scale by road class: BASE is the arterial-level congestion pattern,
# smaller roads run much freer. This yields realistic spatial variety (arterials
# red at peak, side streets green/yellow) instead of a uniform city-wide tint.
ROAD_SCALE = {
    "motorway": 0.8, "motorway_link": 0.7,
    "trunk": 0.85, "trunk_link": 0.75,
    "primary": 1.0, "primary_link": 0.85,
    "secondary": 0.7, "secondary_link": 0.6,
    "tertiary": 0.55, "tertiary_link": 0.5,
    "unclassified": 0.45, "road": 0.5,
    "residential": 0.35, "living_street": 0.28,
    "service": 0.28, "track": 0.22,
}
ROAD_SC = np.array(
    [ROAD_SCALE.get(f["properties"]["highway"], 0.7) for f in _segments],
    dtype=np.float32,
)
PERSIST_SCALE = np.where(PERSISTENT, 1.25, 1.0).astype(np.float32)

# deterministic per-segment congestion factor (each road has its own character),
# so segments of the same class don't all look identical.
_seg_rng = np.random.default_rng(7)
SEG_FACTOR = _seg_rng.uniform(0.7, 1.3, size=N).astype(np.float32)

# per-segment morning-vs-evening tilt (positive = busier in the morning). This
# makes different roads dominate at different times, so the bottleneck ranking
# rotates through the day instead of always being the same segments.
_tilt_rng = np.random.default_rng(21)
MORNING_TILT = _tilt_rng.uniform(-16.0, 16.0, size=N).astype(np.float32)
MORNING_HOURS = {6, 7, 8, 9, 10}
EVENING_HOURS = {16, 17, 18, 19, 20}

# centroid per segment
_centroids = []
for f in _segments:
    coords = f["geometry"]["coordinates"]
    lats = [p[1] for p in coords]
    lons = [p[0] for p in coords]
    _centroids.append((float(np.mean(lats)), float(np.mean(lons))))
CENTROIDS = np.array(_centroids)

BASE = np.array(
    [
        [WEEKDAY_CONGESTION[h] if d < 5 else WEEKEND_CONGESTION[h] for h in range(24)]
        for d in range(7)
    ],
    dtype=np.float32,
)  # (7, 24)


def bucket_for(cong: np.ndarray) -> np.ndarray:
    return np.select(
        [cong < _BOUNDS[0], cong < _BOUNDS[1], cong < _BOUNDS[2]], [0, 1, 2], default=3
    ).astype(np.int8)


def _peak_bump(hour: int) -> np.ndarray:
    if hour in PEAK_HOURS:
        return np.where(BOTTLENECK, 18.0, 0.0).astype(np.float32)
    return np.zeros(N, dtype=np.float32)


def typical_hw(dow: int, hour: int) -> np.ndarray:
    cong = BASE[dow, hour] * ROAD_SC * PERSIST_SCALE * SEG_FACTOR
    if hour in MORNING_HOURS:
        cong = cong + MORNING_TILT
    elif hour in EVENING_HOURS:
        cong = cong - MORNING_TILT
    cong = cong + _peak_bump(hour)
    return np.clip(cong, 0.0, 300.0).astype(np.float32)


def _day_noise(d: Date, hour: int) -> np.ndarray:
    # Small, deterministic, signal-proportional day-to-day variation so that
    # historical data tracks the forecast closely (high backtest correlation).
    rng = np.random.default_rng(d.toordinal() * 100003 + hour * 1009)
    base = typical_hw(d.weekday(), hour)
    frac = rng.uniform(-0.10, 0.10, size=N).astype(np.float32)
    return base * frac


def congestion_for(d: Date, hour: int, mode: str):
    dow = d.weekday()
    cong = typical_hw(dow, hour)
    if mode == "history":
        cong = cong + _day_noise(d, hour)
    cong = np.clip(cong, 0.0, 300.0)
    speed = FREEFLOW / (1.0 + cong / 100.0)
    bucket = bucket_for(cong)
    return cong, speed, bucket


# ---- localities (sub-areas of Pune core) ---------------------------------
# PUNE_AREAS is imported from config (shared with the build pipeline).

_AREA_CENTERS = np.array([[a[1], a[2]] for a in PUNE_AREAS], dtype=np.float32)
_AREA_RADII = np.array([a[3] for a in PUNE_AREAS], dtype=np.float32)

# assign each segment centroid to nearest area center within radius (else -1)
_cos = np.cos(np.radians(np.mean(CENTROIDS[:, 0])))
_dlat = CENTROIDS[:, None, 0] - _AREA_CENTERS[None, :, 0]
_dlon = (CENTROIDS[:, None, 1] - _AREA_CENTERS[None, :, 1]) * _cos
_dist = np.sqrt(_dlat ** 2 + _dlon ** 2)
_nearest = np.argmin(_dist, axis=1)
AREA_IDX = np.where(_dist[np.arange(N), _nearest] <= _AREA_RADII[_nearest], _nearest, -1).astype(int)
# every segment also gets a "home" locality: its nearest area centre (no radius
# cutoff), so that filtering by area never drops a road entirely.
SEG_AREA = _nearest.astype(int)
AREA_NAMES = [a[0] for a in PUNE_AREAS]
AREA_NAME_TO_IDX = {n: i for i, n in enumerate(AREA_NAMES)}


# ---- road-name search index ----------------------------------------------
_road_idx: dict = defaultdict(list)
for _i, _f in enumerate(_segments):
    _nm = _f["properties"].get("name")
    if _nm:
        _road_idx[_nm].append(_i)

ROAD_GROUPS = []
for _nm, _idxs in _road_idx.items():
    _hws = [_segments[i]["properties"]["highway"] for i in _idxs]
    _hw = max(set(_hws), key=_hws.count)
    _lats = CENTROIDS[_idxs, 0]
    _lons = CENTROIDS[_idxs, 1]
    _ln = np.array([_segments[i]["properties"].get("length_m", 0) or 0 for i in _idxs], dtype=np.float32)
    _rep = _idxs[int(np.argmax(_ln))]
    ROAD_GROUPS.append({
        "name": _nm,
        "count": len(_idxs),
        "highway": _hw,
        "lat": round(float(_lats.mean()), 5),
        "lon": round(float(_lons.mean()), 5),
        "rep_id": int(_rep),
    })


# ---- app ------------------------------------------------------------------
app = FastAPI(title="CLOGGED — Pune traffic")


@app.middleware("http")
async def no_cache_api(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _parse_date(value: str) -> Date:
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"invalid date: {value}")


def _now():
    return datetime.now()


def _validate(d: Date, hour: int, mode: str) -> None:
    today = Date.today()
    if mode == "history":
        if d >= today:
            raise HTTPException(400, "history date must be before today")
        if d < today - timedelta(days=HISTORY_DAYS):
            raise HTTPException(400, "history is limited to the past 365 days")
    elif mode == "forecast":
        if d < today:
            raise HTTPException(400, "forecast date must be today or in the future")
        if d > today + timedelta(days=FORECAST_DAYS):
            raise HTTPException(400, f"forecast is limited to +{FORECAST_DAYS} days")
    else:
        raise HTTPException(400, "mode must be 'forecast' or 'history'")


@app.get("/api/meta")
def meta():
    today = Date.today()
    now = _now()
    return {
        "today": today.isoformat(),
        "now_hour": now.hour,
        "now_iso": now.isoformat(timespec="minutes"),
        "history_min": (today - timedelta(days=HISTORY_DAYS)).isoformat(),
        "history_max": (today - timedelta(days=1)).isoformat(),
        "forecast_min": today.isoformat(),
        "forecast_max": (today + timedelta(days=FORECAST_DAYS)).isoformat(),
    }


@app.get("/api/segments")
def segments():
    """Static geometry + attributes (no per-hour values)."""
    return {"type": "FeatureCollection", "features": _segments}


@app.get("/api/values")
def values(date: str, hour: int = 8, mode: str = "forecast"):
    d = _parse_date(date)
    if not (0 <= hour <= 23):
        raise HTTPException(400, "hour must be 0-23")
    _validate(d, hour, mode)
    cong, speed, bucket = congestion_for(d, hour, mode)
    return {
        "date": date,
        "mode": mode,
        "hour": hour,
        "day": d.weekday(),
        "day_name": DAY_NAMES[d.weekday()],
        "buckets": bucket.tolist(),
        "speeds": [round(float(s), 1) for s in speed],
        "congestions": [round(float(c), 1) for c in cong],
    }


@app.get("/api/segment/{seg_id}")
def segment_detail(seg_id: int, date: str, mode: str = "forecast", hour: int = 8):
    if seg_id < 0 or seg_id >= N:
        raise HTTPException(404, "segment not found")
    d = _parse_date(date)
    _validate(d, 0, mode)
    hour = max(0, min(int(hour), 23))

    hours = []
    for h in range(24):
        cong, speed, bucket = congestion_for(d, h, mode)
        hours.append(
            {
                "hour": h,
                "speed": round(float(speed[seg_id]), 1),
                "congestion": round(float(cong[seg_id]), 1),
                "bucket": int(bucket[seg_id]),
                "label": BUCKET_LABELS[int(bucket[seg_id])],
            }
        )

    day_cong = np.array([x["congestion"] for x in hours])
    h_peak = int(np.argmax(day_cong))
    h_off = int(np.argmin(day_cong))

    props = dict(_segments[seg_id]["properties"])
    props.update(
        {
            "date": date,
            "day_name": DAY_NAMES[d.weekday()],
            "selected_hour": hour,
            "current": hours[hour],
            "peak": {"hour": h_peak, "congestion": hours[h_peak]["congestion"], "speed": hours[h_peak]["speed"]},
            "offpeak": {"hour": h_off, "congestion": hours[h_off]["congestion"], "speed": hours[h_off]["speed"]},
            "avg_congestion_today": round(float(day_cong.mean()), 1),
            "lat": float(CENTROIDS[seg_id][0]),
            "lon": float(CENTROIDS[seg_id][1]),
        }
    )
    return {"id": seg_id, "properties": props, "hours": hours}


@app.get("/api/bottlenecks")
def bottlenecks(date: str, hour: int = 8, mode: str = "forecast",
                top: int = 30, filter: str = "all", area: str = ""):
    d = _parse_date(date)
    if not (0 <= hour <= 23):
        raise HTTPException(400, "hour must be 0-23")
    _validate(d, hour, mode)
    cong, speed, bucket = congestion_for(d, hour, mode)
    if filter == "gridlocked":
        mask = bucket == 3
    elif filter == "slow":
        mask = bucket == 2
    else:
        mask = bucket >= 2

    # per-area counts across the whole city (used by the area chips), taken
    # before the area filter narrows the mask.
    counts = np.bincount(SEG_AREA[mask], minlength=len(AREA_NAMES))
    area_counts = sorted(
        ({"name": AREA_NAMES[i], "count": int(counts[i])} for i in range(len(AREA_NAMES)) if counts[i]),
        key=lambda x: (-x["count"], x["name"]),
    )

    if area:
        if area not in AREA_NAME_TO_IDX:
            raise HTTPException(404, f"unknown area: {area}")
        mask = mask & (SEG_AREA == AREA_NAME_TO_IDX[area])

    idx = np.where(mask)[0]
    if idx.size:
        order = idx[np.argsort(-cong[idx])][:max(1, min(top, 2000))]
    else:
        order = np.array([], dtype=int)
    out = []
    for i in order:
        p = _segments[i]["properties"]
        out.append(
            {
                "id": int(i),
                "name": p["name"],
                "highway": p["highway"],
                "congestion": round(float(cong[i]), 1),
                "speed": round(float(speed[i]), 1),
                "bucket": int(bucket[i]),
                "label": BUCKET_LABELS[int(bucket[i])],
                "area": AREA_NAMES[SEG_AREA[i]],
                "lat": float(CENTROIDS[i][0]),
                "lon": float(CENTROIDS[i][1]),
            }
        )
    return {
        "date": date,
        "day_name": DAY_NAMES[d.weekday()],
        "hour": hour,
        "mode": mode,
        "area": area or "All Pune",
        "count": int(idx.size),
        "area_counts": area_counts,
        "segments": out,
    }


@app.get("/api/persistent")
def persistent(top: int = 300, min_avg: float = 55.0, area: str = ""):
    mean_cong = np.zeros(N, dtype=np.float32)
    for dow in range(7):
        for h in range(24):
            mean_cong += typical_hw(dow, h)
    mean_cong /= 168.0

    mask = mean_cong >= min_avg
    if area:
        if area not in AREA_NAME_TO_IDX:
            raise HTTPException(404, f"unknown area: {area}")
        mask &= SEG_AREA == AREA_NAME_TO_IDX[area]

    idxs = np.nonzero(mask)[0]
    order = idxs[np.argsort(-mean_cong[idxs])][:max(1, min(top, 2000))]
    out = []
    for i in order:
        p = _segments[i]["properties"]
        out.append(
            {
                "id": int(i),
                "name": p["name"],
                "highway": p["highway"],
                "avg_congestion": round(float(mean_cong[i]), 1),
                "flagged_persistent": bool(p["persistent"]),
                "bottleneck": bool(p["bottleneck"]),
                "area": AREA_NAMES[SEG_AREA[i]],
                "lat": float(CENTROIDS[i][0]),
                "lon": float(CENTROIDS[i][1]),
            }
        )
    return {"segments": out, "area": area or "All Pune",
            "min_avg": min_avg, "total": int(mask.sum())}


@app.get("/api/peaks")
def peaks():
    city = np.zeros(168, dtype=np.float32)
    for dow in range(7):
        for h in range(24):
            city[dow * 24 + h] = float(np.mean(typical_hw(dow, h)))
    hw_peak = int(np.argmax(city))
    hw_off = int(np.argmin(city))

    def curve(dow):
        return [
            {"hour": h, "congestion": round(float(city[dow * 24 + h]), 1),
             "bucket": int(bucket_for(np.array([city[dow * 24 + h]]))[0])}
            for h in range(24)
        ]

    return {
        "peak": {"day": hw_peak // 24, "hour": hw_peak % 24,
                 "day_name": DAY_NAMES[hw_peak // 24], "congestion": round(float(city[hw_peak]), 1)},
        "offpeak": {"day": hw_off // 24, "hour": hw_off % 24,
                    "day_name": DAY_NAMES[hw_off // 24], "congestion": round(float(city[hw_off]), 1)},
        "weekday": curve(0),
        "weekend": curve(6),
    }


@app.get("/api/areas")
def areas():
    out = []
    for a_i, (name, lat, lon, _r) in enumerate(PUNE_AREAS):
        m = AREA_IDX == a_i
        bounds = None
        if m.any():
            lats = CENTROIDS[m, 0]
            lons = CENTROIDS[m, 1]
            pad = 0.004
            bounds = {
                "min_lat": round(float(lats.min()) - pad, 5),
                "max_lat": round(float(lats.max()) + pad, 5),
                "min_lon": round(float(lons.min()) - pad, 5),
                "max_lon": round(float(lons.max()) + pad, 5),
            }
        out.append({"name": name, "lat": lat, "lon": lon,
                    "count": int(m.sum()), "bounds": bounds})
    return {"areas": out}


@app.get("/api/search")
def search(q: str = "", limit: int = 25):
    ql = (q or "").strip().lower()
    if not ql:
        return {"query": q, "areas": [], "roads": []}
    limit = max(1, min(int(limit), 100))

    def rank(name: str) -> int:
        nl = name.lower()
        if nl == ql:
            return 0
        if nl.startswith(ql):
            return 1
        return 2

    areas_out = []
    for a_i, (name, lat, lon, _r) in enumerate(PUNE_AREAS):
        if ql in name.lower():
            areas_out.append({
                "name": name, "lat": lat, "lon": lon,
                "count": int((AREA_IDX == a_i).sum()), "_rank": rank(name),
            })

    roads_out = []
    for g in ROAD_GROUPS:
        if ql in g["name"].lower():
            roads_out.append({**g, "_rank": rank(g["name"])})

    areas_out.sort(key=lambda x: (x["_rank"], -x["count"], x["name"]))
    roads_out.sort(key=lambda x: (x["_rank"], -x["count"], x["name"]))
    for x in areas_out:
        x.pop("_rank", None)
    for x in roads_out:
        x.pop("_rank", None)
    return {"query": q, "areas": areas_out[:limit], "roads": roads_out[:limit]}


def _region_stats(mask: np.ndarray, d: Date, hour: int, mode: str):
    if not mask.any():
        return None
    cong, speed, bucket = congestion_for(d, hour, mode)
    mean_cong = float(cong[mask].mean())
    mean_speed = float(speed[mask].mean())
    b = int(bucket_for(np.array([mean_cong]))[0])
    counts = np.bincount(bucket[mask], minlength=4)
    total = int(mask.sum())
    dist = {BUCKET_LABELS[k]: round(float(counts[k] / total * 100), 1) for k in range(4)}
    return {"mean_congestion": round(mean_cong, 1), "mean_speed": round(mean_speed, 1),
            "bucket": b, "label": BUCKET_LABELS[b], "distribution": dist, "count": total}


@app.get("/api/area/{name}")
def area(name: str, date: str, hour: int = 8, mode: str = "forecast", hours: int = 24):
    d = _parse_date(date)
    if not (0 <= hour <= 23):
        raise HTTPException(400, "hour must be 0-23")
    _validate(d, hour, mode)

    if name == "All Pune":
        mask = np.ones(N, dtype=bool)
        count = N
    else:
        a_i = next((i for i, (nm, *_rest) in enumerate(PUNE_AREAS) if nm == name), None)
        if a_i is None:
            raise HTTPException(404, f"unknown area: {name}")
        mask = AREA_IDX == a_i
        count = int(mask.sum())

    day = []
    for h in range(24):
        s = _region_stats(mask, d, h, mode)
        day.append({"hour": h, "congestion": s["mean_congestion"] if s else None,
                    "speed": s["mean_speed"] if s else None,
                    "bucket": s["bucket"] if s else 0,
                    "label": s["label"] if s else "No data"})

    forecast = []
    for k in range(hours):
        total = hour + k
        hh = total % 24
        dt = d + timedelta(days=total // 24)
        dow = dt.weekday()
        s = _region_stats(mask, dt, hh, mode)
        forecast.append({
            "offset_h": k, "date": dt.isoformat(), "day_name": DAY_NAMES[dow], "hour": hh,
            "congestion": s["mean_congestion"] if s else None,
            "bucket": s["bucket"] if s else 0,
            "label": s["label"] if s else "No data",
        })

    now_stat = _region_stats(mask, d, hour, mode)
    return {
        "name": name,
        "count": count,
        "date": date,
        "day_name": DAY_NAMES[d.weekday()],
        "hour": hour,
        "mode": mode,
        "day": day,
        "forecast": forecast,
        "current": now_stat,
    }


@app.get("/api/forecast")
def forecast(date: str, hour: int = 8, hours: int = 24):
    d = _parse_date(date)
    if not (0 <= hour <= 23):
        raise HTTPException(400, "hour must be 0-23")
    out = []
    for k in range(hours):
        total = hour + k
        hh = total % 24
        dt = d + timedelta(days=total // 24)
        dow = dt.weekday()
        city_cong = float(np.mean(typical_hw(dow, hh)))
        b = int(bucket_for(np.array([city_cong]))[0])
        out.append({"offset_h": k, "date": dt.isoformat(), "day_name": DAY_NAMES[dow],
                    "hour": hh, "congestion": round(city_cong, 1), "bucket": b,
                    "label": BUCKET_LABELS[b]})
    return {"predictions": out}


_bench_cache: dict = {}


@app.get("/api/benchmark")
def benchmark(days: int = 30):
    """Backtest accuracy: compare history (realized) vs forecast (expected) over
    the past `days` days, measuring correlation and mean absolute error."""
    days = max(1, min(int(days), HISTORY_DAYS))
    if days in _bench_cache:
        return _bench_cache[days]
    today = Date.today()
    cors = []
    maes = []
    for back in range(days, 0, -1):
        d = today - timedelta(days=back)
        for hour in range(0, 24, 3):
            hist, _, _ = congestion_for(d, hour, "history")
            fc, _, _ = congestion_for(d, hour, "forecast")
            if np.std(hist) < 1e-6 or np.std(fc) < 1e-6:
                continue
            cors.append(float(np.corrcoef(hist, fc)[0, 1]))
            maes.append(float(np.mean(np.abs(hist - fc))))
    result = {
        "days": days,
        "correlation": round(float(np.mean(cors)), 4) if cors else None,
        "mae_pct": round(float(np.mean(maes)), 3) if maes else None,
        "samples": len(cors),
    }
    _bench_cache[days] = result
    return result


LOGO_FILE = ROOT / "clogged_logo.svg"


# registered before the static mount so the root-level SVG wins
@app.get("/clogged_logo.svg")
def logo():
    if not LOGO_FILE.exists():
        raise HTTPException(404, "logo not found")
    return FileResponse(str(LOGO_FILE), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))
