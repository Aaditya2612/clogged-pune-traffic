# CLOGGED — Pune Traffic Dashboard

Complete technical documentation of the project: its goal, workflow, data,
algorithms, API, UI and requirements. Written so that a separate LLM can turn
this into a slide deck (a suggested slide outline is at the end).

---

## 1. One-line summary

**CLOGGED** is a web dashboard that shows road-by-road traffic congestion across
the Pune city core as a coloured map, for any hour of any date in the **past
365 days** (historical view) or the **next 30 days** (forecast view), built from
OpenStreetMap road geometry plus a congestion model calibrated to **TomTom
Traffic Index — Pune 2025**.

---

## 2. Problem & approach

- **Problem:** Pune has no free, public, road-level congestion map you can
  scrub through time. Real traffic APIs (TomTom, Google) are paid and
  rate-limited.
- **Approach:** Use a free, openly licensed road network (OSM), attach a
  physically-plausible *free-flow speed* to every road, then overlay a
  *congestion model* whose shape in time and space is calibrated to published
  TomTom Traffic Index statistics for Pune. The result is a fully synthetic but
  realistic, explorable traffic map with **zero API keys and zero recurring
  cost**.
- **Explicitly a simulation:** the numbers are modelled, not measured. See
  §12 (Limitations) — this is stated honestly in the UI (the accuracy card is
  labelled "backtest", not "ground truth").

---

## 3. What the user can do (functional overview)

| Feature | Description |
|---|---|
| **Coloured road map** | 28,535 road segments drawn over Pune; colour = current congestion bucket (green/yellow/orange/red). |
| **Time control** | Draggable 24-hour clock (00:00–23:00) + prev/next-day buttons + a date picker. |
| **Animate** | Play button steps the hour forward ~2×/second to animate the day. |
| **Now** | Jumps to the live current local date + hour. |
| **Historical / Forecast toggle** | iOS-style glass slider switches the whole app between the past-365-day window and the 30-day future window. |
| **Peaks tab** | City-wide peak/off-peak hour + weekday & weekend congestion curves + forecast-accuracy (backtest) card. |
| **Bottlenecks tab** | Ranked list of the most-congested roads at the selected date/hour (filter: slow, gridlocked, or both). |
| **Persistent tab** | Roads that are congested almost *all* the time (long-run weekly average ≥ 55%), filterable by locality with searchable chips. |
| **Forecast tab** | Per-locality (18 Pune localities) 24-hour congestion chart + next-24-hours table. |
| **Search tab** | Type-ahead search over 18 localities and 1,655 road names; click to fly/highlight. |
| **Hover tooltip** | Move the cursor near a road → floating tooltip with name, bucket, speed, congestion, free-flow. |
| **Detail popup** | Click a road → panel with full 24-hour chart, peak/off-peak, average, flags. Stays in sync when you change date/hour/mode. |

---

## 4. System architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  OFFLINE DATA PIPELINE  (run once, or when you want fresh data)     │
│                                                                     │
│  Geofabrik PBF  ──►  build.py (pyrosm)  ──►  segments.geojson       │
│  (western-zone)        + config.py           profile.npz            │
└────────────────────────────────────────────────────────────────────┘
                              │  (served at runtime)
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│  BACKEND   api/main.py  (FastAPI + numpy)                           │
│   • loads segments.geojson + profile.npz once at start-up           │
│   • recomputes congestion on demand from a closed-form model        │
│   • ~14 JSON endpoints under /api/*  + serves the web/ static site  │
└────────────────────────────────────────────────────────────────────┘
                              │  HTTP (localhost:8000)
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│  FRONTEND  web/index.html + style.css + app.js  (vanilla JS)        │
│   • Leaflet map on a <canvas> renderer, 28,535 polylines            │
│   • Clock / mode switch / tabs / detail popup / search / hover      │
└────────────────────────────────────────────────────────────────────┘
```

- **No build step for the frontend** — plain HTML/CSS/JS, Leaflet from a CDN.
- **No database** — two flat files (`segments.geojson`, `profile.npz`).
- **Single process** — the API process also serves the static frontend, so the
  whole app is one `uvicorn` command.

---

## 5. Repository layout

```
reserachPaper/                     (project root — note spelling "reserach")
├─ clogged_logo.svg                header logo (root-level, served by a route)
├─ DOCUMENTATION.md                this file
├─ api/
│   └─ main.py                     FastAPI app: model + all endpoints (615 lines)
├─ pipeline/
│   ├─ config.py                   all tunable constants (121 lines)
│   ├─ build.py                    OSM → dataset builder (244 lines)
│   └─ download_pbf.py             one-off Geofabrik downloader (22 lines)
├─ web/
│   ├─ index.html                  page skeleton (85 lines)
│   ├─ style.css                   all styling incl. iOS mode switch (142 lines)
│   └─ app.js                      all frontend logic (748 lines)
└─ data/                           (generated + source data)
    ├─ western-zone-latest.osm.pbf source OSM extract (~211 MB)
    ├─ segments.geojson            28,535 road segments (~12.6 MB)
    └─ profile.npz                 168-hour per-segment profile (~)
```

There is **no `README`, no `requirements.txt`, no tests** in the repo. This
document is the single source of truth.

---

## 6. Data pipeline (offline)

### 6.1 Download — `pipeline/download_pbf.py`
Streams the Geofabrik **India → Western Zone** extract:
`https://download.geofabrik.de/asia/india/western-zone-latest.osm.pbf`
(~211 MB) into `data/`. Uses `requests` streaming with a progress printout and
a custom User-Agent.

### 6.2 Build — `pipeline/build.py`
Run with `python pipeline\build.py`. Steps:

1. **Read PBF** with `pyrosm.OSM`, clipped to the Pune-core bounding box
   (`config.BBOX`), using `get_network(network_type="driving")` and reprojected
   to EPSG:4326.
2. **Free-flow speed per segment** (`freeflow_speed`):
   - Take numeric `maxspeed` from OSM when present (handles `mph`, lists, and
     junk values like `none`/`signals`; caps at 100 km/h).
   - Otherwise fall back to a per-class default (`config.DEFAULT_FREEFLOW`,
     fallback 30 km/h).
3. **Flags:**
   - `persistent` = `rng(seed 42) % 1000 < 8%` → ~8% of segments
     (`PERSISTENT_FRACTION = 0.08`).
   - `bottleneck` = node-degree ≥ 5 (a junction where ≥5 edges meet).
     ⚠️ See §12 — **this currently yields 0 flags** because the PBF edge table
     has no `u`/`v` columns, so the code falls back to `junc_deg = 1`.
   - `arterial` = highway ∈ {motorway, trunk, primary, secondary, tertiary}.
4. **Per-segment bias** `seg_bias`: 1.30 if persistent, ×1.05 if arterial,
   clipped to [0.5, 2.5].
5. **168-hour profile** per segment (7 days × 24 hours): congestion =
   `curve[hour] * seg_bias + noise(±4) + peak_bump(18 if bottleneck)`,
   speed = `freeflow / (1 + congestion/100)`, bucket from thresholds.
6. **Unnamed-road labelling:** roads with no OSM `name` are named
   `"<nearest locality> <class suffix>"`, e.g. `"Kothrud residential lane"`
   (see §10.1).
7. **Write outputs:** `data/segments.geojson` (geometry + properties) and
   `data/profile.npz` (arrays `congestion`, `speed`, `bucket`, `bias`,
   `freeflow`, all shaped `(28535, 168)` except the last two `(28535,)`).

> **Architectural note:** at serve time the API only reads `freeflow` from
> `profile.npz`. The congestion the dashboard displays is **recomputed live** by
> the API's own model (§8), which is a *separate* formula from the build-time
> profile. They are deliberately similar in spirit but not identical.

### 6.3 Output facts (measured on the current build)
- Segments: **28,535**
- Distinct road names: **1,655** (0 null — all named)
- Arterial segments: **4,698**
- Segments flagged `persistent`: **2,300**
- Segments flagged `bottleneck`: **0** (see limitation)
- Free-flow speed: min **10**, mean **32.8**, max **85** km/h
- Highway classes: residential 22,286 · tertiary 1,928 · primary 1,079 ·
  living_street 978 · secondary 915 · trunk 776 · trunk_link 140 ·
  primary_link 112 · unclassified 102 · busway 95 · tertiary_link 63 ·
  secondary_link 58 · bus_stop 2 · road 1
- Bounding box: N 18.62, S 18.42, E 73.94, W 73.77

---

## 7. Calibration & the congestion concept

### 7.1 What "congestion %" means
Congestion % = **extra travel time vs free-flow**, as a percentage
(the same definition TomTom uses). Speed is then derived:

```
speed = freeflow_speed / (1 + congestion / 100)
```

i.e. congestion 0% → free-flow speed; 100% → half the free-flow speed.

### 7.2 TomTom anchors (config comments)
From *TomTom Traffic Index — Pune 2025*:
- Morning rush: **91.8%** congestion
- Evening rush: **110.9%**
- Annual average: **71.1%**
- Free-flow (~night) ≈ **30 km/h**

### 7.3 Hour-of-day congestion curves (`config.py`)
Two 24-value lookup tables define the typical city-wide congestion by hour:

`WEEKDAY_CONGESTION` — two peaks: a morning shoulder (~8–11 h) and a strong
evening peak at **18 h = 108%**, tapering to ~12% by 23 h.

`WEEKEND_CONGESTION` — one broad midday/afternoon hump peaking ~18 h = 75%.

### 7.4 Congestion buckets (the colour scale)
| Bucket | Name | Congestion % | Colour |
|---|---|---|---|
| 0 | Fast moving | 0 – 35 | green `#2ecc71` |
| 1 | Moderate | 35 – 60 | yellow `#f1c40f` |
| 2 | Slow | 60 – 90 | orange `#e67e22` |
| 3 | Gridlocked | ≥ 90 | red `#c0392b` |

`BOTTLENECK_THRESHOLD = 60%` (slow or worse). `PEAK_HOURS = {8,9,10,11,17,18,19,20}`.

---

## 8. The live congestion model (`api/main.py`)

For a given date `d` and hour `h`, with day-of-week `dow = d.weekday()`:

```
typical_hw(dow,h) = BASE[dow,h] * ROAD_SCALE[class]
                            * PERSIST_SCALE * SEG_FACTOR
                    + morning/evening TILT
                    + peak_bump
                    (clipped to [0, 300])

congestion(d,h,mode) = typical_hw(dow,h)                        # forecast
                     = typical_hw(dow,h) + day_noise(d,h)       # history
speed     = FREEFLOW / (1 + congestion/100)
bucket    = threshold(congestion)
```

Terms explained:

- **`BASE[dow,h]`** — the weekday/weekend congestion curve (7×24 table built
  from the config curves). Base is the *arterial-level* pattern.
- **`ROAD_SCALE`** — per-highway multiplier so smaller roads run freer, giving
  realistic spatial variety (arterials red at peak, side streets green). Range:
  motorway 0.8 … residential 0.35 … track 0.22. Default 0.7.
- **`PERSIST_SCALE`** — 1.25 for persistently-flagged segments, else 1.0.
- **`SEG_FACTOR`** — per-segment random character factor, uniform(0.7, 1.3),
  seeded (seed 7) so it is stable run-to-run. Prevents all roads of one class
  looking identical.
- **Morning/evening `TILT`** — per-segment value uniform(−16, +16) (seed 21),
  *added* during morning hours {6–10} and *subtracted* during evening hours
  {16–20}. This rotates which roads dominate at different times of day.
- **`peak_bump`** — +18 for `bottleneck`-flagged segments during `PEAK_HOURS`
  (currently inert because 0 segments are flagged).
- **`day_noise`** — history mode only. A deterministic random ±10% of the base
  value, seeded from `(date.toordinal(), hour)` so the same past date always
  yields the same values. Makes history look "real" (day-to-day wobble) while
  staying close to the forecast — which is why the backtest scores high.
  Forecast mode has **no noise** (returns the smooth expected value).

**Why this design:** it is fully deterministic (same request → same answer),
needs no database, and lets the history and forecast live in one code path.

---

## 9. Backend API reference (`api/main.py`)

Loaded once at start-up: `segments.geojson` features + `profile.npz["freeflow"]`.
Derived arrays: `ROAD_SC`, `PERSIST_SCALE`, `SEG_FACTOR`, `MORNING_TILT`,
`CENTROIDS`, `AREA_IDX`, `SEG_AREA`, `ROAD_GROUPS`, `BOTTLENECK`, `PERSISTENT`.

All `/api/*` responses get `Cache-Control: no-store` via middleware.

| Method & path | Params | Returns |
|---|---|---|
| `GET /api/meta` | — | `today`, `now_hour`, `now_iso`, history min/max, forecast min/max |
| `GET /api/segments` | — | Full GeoJSON FeatureCollection (static geometry + properties) |
| `GET /api/values` | `date, hour=8, mode=forecast` | Per-segment `buckets[]`, `speeds[]`, `congestions[]` + day info |
| `GET /api/segment/{id}` | `date, mode, hour` | One segment: 24-h series, current/peak/off-peak, avg, flags |
| `GET /api/bottlenecks` | `date, hour, mode, top=30, filter=all\|slow\|gridlocked` | Ranked congested segments |
| `GET /api/persistent` | `top=300, min_avg=55.0, area=""` | Long-run congested roads + per-row area; `total`; 404 on unknown area |
| `GET /api/peaks` | — | City peak/off-peak + weekday/weekend 24-h curves |
| `GET /api/areas` | — | 18 localities w/ centre, segment count, lat/lon bounds |
| `GET /api/search` | `q, limit=25` | `{areas[], roads[]}` ranked; limit clamp 1–100 |
| `GET /api/area/{name}` | `date, hour, mode, hours=24` | Locality 24-h day curve + sliding `hours` forecast + distribution |
| `GET /api/forecast` | `date, hour=8, hours=24` | City-wide next-N-hours predictions |
| `GET /api/benchmark` | `days=30` | Backtest `correlation`, `mae_pct`, `samples` (cached) |
| `GET /clogged_logo.svg` | — | The header logo (registered before the static mount) |
| `GET /` | — | Serves `web/index.html` |
| mounted at `/` | — | `StaticFiles(web/, html=True)` — serves CSS/JS |

### Validation rules (`_validate`)
- `mode` must be `history` or `forecast` (else 400).
- `history`: date must be **strictly before today** and **within the last 365
  days** (`HISTORY_DAYS = 365`).
- `forecast`: date must be **today or later** and within
  **+30 days** (`FORECAST_DAYS = 30`).
- `hour` must be 0–23. Unknown area/segment → 404.

### Measured live values (current run)
- `/api/meta`: today `2026-10-16`; history `2025-10-16 … 2026-10-15`;
  forecast `2026-10-16 … 2026-11-15`.
- `/api/benchmark`: **correlation 0.9915**, **MAE 0.988%**, 240 samples
  (30 days × 8 sampled hours at 3-hour steps).

---

## 10. Key algorithms

### 10.1 Nearest-locality labelling & spatial grouping
- **At build:** each unnamed road is named after its **nearest** Pune locality
  centre, using a squared-distance metric with a latitude correction
  `coslat = 0.949` (≈ cos 18.5°): `d = (Δlat)² + (Δlon·0.949)²`.
- **At serve:** every segment gets a "home" locality `SEG_AREA` = nearest area
  centre **with no radius cutoff**, so filtering by locality always finds roads
  (100% coverage). A separate `AREA_IDX` *does* apply a radius cutoff and is
  used for bounds/counts.
- **Why:** an earlier strict-radius mapping dropped ~60% of persistently
  congested roads (many, e.g. Sinhagad Road, Ganeshkhind Rd, Kharadi Bypass,
  fell outside every locality circle). Nearest-area grouping fixes coverage;
  when grouped this way all **176** roads above 55% map to all **18** localities
  (Pimpri-Chinchwad 28, Shivajinagar 27, Warje 22, Erandwane 19, Aundh 12,
  Koregaon Park 11, Bharati Vidyapeeth 10, Kothrud 8, …).

### 10.2 Search ranking
- `ROAD_GROUPS` is precomputed once: for each distinct name — segment `count`,
  modal `highway` class, mean-centroid `lat`/`lon`, and `rep_id` (longest
  segment) for navigation.
- Query is lower-cased substring match over area names and road names.
- Rank: **exact (0) > prefix (1) > substring (2)**, then by **descending
  count**, then name. Limit clamped 1–100. Empty query → empty lists.

### 10.3 Hover/click picking (custom spatial index)
28,535 thin polylines are drawn with `interactive: false` (Leaflet's own
per-stroke hit-testing is too fiddly for 0.75–3 px lines and O(n) per move).
Instead:
- **Build a uniform grid** (`CELL = 0.01°` ≈ 1.1 km) mapping cell → segment ids;
  precompute each segment's bounding box.
- On cursor move, compute a pixel-accurate grab radius (`HIT_PX = 11`) in
  degrees, look up only the covering cells, reject by bbox, then compute exact
  point-to-polyline distance for the survivors; nearest segment within the
  radius wins.
- One **shared, sticky Leaflet tooltip** is driven from the picked id; a
  `requestAnimationFrame` throttle coalesces rapid mousemoves; map click opens
  the detail popup for the picked segment.
- Validated with a temporary Node harness: 1600/1600 exact picks across zooms
  10 / 12.2 / 14 / 17, ~40 µs per pick (~25k picks/s).

### 10.4 Backtest / benchmark
Compares **history vs forecast** over the past `days` days at 3-hour steps:
mean Pearson correlation across all segments and mean absolute error in
percentage points. Because history = forecast + small noise, the score is high
(r ≈ 0.99, MAE ≈ 1%). **This measures self-consistency of the model, not
accuracy against real-world measurements.** Results cached per `days`.

---

## 11. Frontend details (`web/`)

### 11.1 Layout
- Top bar: logo + tagline, mode slider, date controls, clock, Now, Animate.
- Summary strip: selected date/day/hour + a 4-bucket city-wide distribution.
- Main: full-height Leaflet map (left) + 344 px tabbed sidebar (right).
- Floating detail popup (top-right of the map) toggled per segment.

### 11.2 Map
- Leaflet 1.9.4 (CDN), `L.canvas` renderer for 28k lines, OSM raster tiles,
  initial view `[18.52, 73.855]` zoom 12.2, `maxBounds` = Pune bbox.
- Line weight scales with zoom: `WEIGHT_BASE = {arterial: 3.0, normal: 1.8}`
  × `zoomScale(z)` = 0.42 at z ≤ 11 → 1.0 at z ≥ 15 (thins out when zoomed out
  so colours don't smear into bands). Opacity 0.8, round caps/joins.

### 11.3 Clock
- Custom 24-tick SVG dial (major ticks every 6 h, labels 00/06/12/18); drag the
  hand (pointer capture) to set the hour; angle → `round(angle/15) % 24`.
- History mode, when viewing *today*, clamps the hour to the current hour.

### 11.4 Mode switch
- Frosted iOS-glass slider (`backdrop-filter: blur(14px) saturate(160%)`),
  animated green when in Forecast, greyscale in Historical; clicking a side
  label or the switch toggles mode and resets the date window.

### 11.5 "Now" behaviour (recent fix)
`applyNow()` sets mode = forecast, date = today, hour = current local hour,
applies date limits, and **clamps the hour before first render**. It is called
once during `init()` (after `/api/meta` + `setDateLimits`) and reused by the
Now button. This fixed a latent bug where opening in history mode clamped
"today" back to yesterday.

### 11.6 Sidebar tabs
1. **Peaks** — backtest card, peak/off-peak card, weekday & weekend 24-h bar
   charts (hand-built SVG; bars hover-highlight).
2. **Bottlenecks** — dropdown filter (slow+gridlocked / slow / gridlocked),
   ranked clickable rows (colour-coded left border), "no bottlenecks" empty
   state.
3. **Persistent** — summary card + **area filter** (free-text input driving
   **searchable chips** with per-locality counts) + filtered list. Clicking a
   row flies to the segment.
4. **Forecast** — locality dropdown (All Pune + 18), 24-h bar chart, bucket
   distribution chips, "next 24 hours" table.
5. **Search** — type-ahead input; results grouped into **Areas** and **Roads**;
   clicking an area flies to its bounds, clicking a road highlights all its
   segments (cyan, up to 500) and opens the first segment's detail.

### 11.7 Detail popup
Shows name, date/day, road class, free-flow speed, value at the selected hour,
average for the date, peak and off-peak hours, persistent/bottleneck flags, and
a 24-hour SVG chart. A module-level `detailId` tracks the open segment;
`refresh()` (called on every hour/date/mode change and during Animate) re-fetches
and redraws it so it never goes stale. Closing resets `detailId = -1`.

### 11.8 Logo
`clogged_logo.svg` sits in the project root and is served by an explicit API
route (registered **before** the `StaticFiles` mount, which only covers `web/`).
The header renders it at 48 px with the tagline "Pune traffic" beside it,
vertically centred.

---

## 12. Requirements, dependencies & how to run

### Runtime / system
- Windows (developed on win32), Python 3.x, a modern browser.
- Internet only for the OSM map tiles (CDN) and, optionally, the initial PBF
  download. Everything else is local.

### Python packages (installed globally; no requirements file)
`fastapi`, `uvicorn`, `numpy`, `pandas`, `pyrosm`, `requests`

### Run the app
```powershell
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000
```

### Rebuild the data (optional, ~30 s after the PBF is present)
```powershell
python pipeline\download_pbf.py   # only if data\*.osm.pbf is missing
python pipeline\build.py
```

### Dev tips
- Static file edits need a hard refresh (`Ctrl+Shift+R`).
- `node --check web/app.js` is used as a quick syntax gate.
- No lint/test suite exists.

---

## 13. Known limitations & caveats (state these honestly in any deck)

1. **Synthetic data.** Congestion is modelled from published Pune averages and
   a formula — it is **not** measured from sensors or vehicles (the app can't
   claim real probe data).
2. **Benchmark is self-consistency, not accuracy.** The r ≈ 0.99 / MAE ≈ 1%
   figure compares the model's history mode against its own forecast mode, so
   it validates internal behaviour, not real-world correctness.
3. **Bottleneck (junction) flag is dead.** The PBF edge table has **no `u`/`v`
   node columns**, so `junc_deg = 1` for every segment and **0 segments** are
   flagged as junction bottlenecks; the peak bump therefore never fires.
   (`/api/bottlenecks` is unaffected — it ranks by congestion, not the flag.)
4. **Free-flow speeds are mostly class defaults.** Only ~1–2% of segments carry
   an explicit OSM `maxspeed`; the rest use per-class defaults, so free-flow
   accuracy depends on those defaults.
5. **Unnamed roads share generated names.** 22,286 residential segments produce
   large same-named groups (e.g. a locality's "residential lane"), so search
   results for those names can aggregate thousands of segments.
6. **Locality set is 18 hand-picked Pune-core localities** — not exhaustive;
   roads far from any centre clamp to the nearest one instead of "unknown".
7. **Two congestion code paths.** `build.py`'s stored 168-h profile and the
   API's live model differ (different bias/scale formulas); the API ignores the
   stored profile except for `freeflow`.
8. `config.BOTTLENECK_THRESHOLD` is imported by the API but ununsed (the
   effective threshold is the bucket boundary at 60%).

---

## 14. Numbers cheat-sheet (for slides)

- 28,535 road segments · 1,655 distinct names · 4,698 arterials
- 18 Pune localities · 211 MB source PBF · 12.6 MB geojson
- Past window: 365 days · Future window: 30 days · 24 hourly steps
- Buckets: green < 35% · yellow 35–60% · orange 60–90% · red ≥ 90%
- TomTom Pune 2025 anchors: AM 91.8% · PM 110.9% · avg 71.1% · free-flow ~30 km/h
- Peak evening hour in model: 18:00 ≈ 108%
- Backtest: r ≈ 0.99, MAE ≈ 1% (self-consistency)
- Persistent roads (≥55% weekly avg): 176, across all 18 localities
- Hover picking: ~40 µs/pick, ~25k picks/s, 11 px grab radius
- Free-flow: 10 / 32.8 / 85 km/h (min/mean/max)

---

## 15. Suggested slide outline (for the PPT-generating LLM)

1. **Title** — CLOGGED: a free, explorable congestion map for Pune.
2. **Problem** — no free road-level congestion map; paid APIs.
3. **Idea / approach** — OSM geometry + TomTom-calibrated model, no API keys.
4. **Architecture** — the 3-layer diagram from §4.
5. **Data pipeline** — download → build → outputs (§6), with the fact list.
6. **How congestion is defined** — extra-time %, speed formula (§7.1).
7. **Calibration** — TomTom anchors + weekday/weekend curves (§7.2–7.3).
8. **Colour scale** — the bucket table (§7.4).
9. **The model** — the formula and what each term does (§8).
10. **API** — endpoint table (§9), note validation windows.
11. **Key algorithms** — nearest-locality, search ranking, spatial-index hover
    picking, backtest (§10).
12. **The interface** — map, clock, mode switch, 5 tabs, detail popup, logo
    (§3, §11) — best presented with screenshots.
13. **Requirements & how to run** (§12).
14. **Limitations / honesty** (§13) — synthetic, self-consistency benchmark,
    dead junction flag.
15. **Highlights & numbers** (§14).
16. **Future work** — real probe data, fix junction detection (add u/v),
    fewer/smarter unnamed-road groups, more localities, routing.
