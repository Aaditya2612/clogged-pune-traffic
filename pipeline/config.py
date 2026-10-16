"""Configuration for the Pune traffic demo."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"

SEGMENTS_FILE = DATA_DIR / "segments.geojson"
PROFILE_FILE = DATA_DIR / "profile.npz"

# Pune city core bounding box (lat north/south, lon east/west)
# extended south to include Katraj / Bharati Vidyapeeth.
BBOX = {
    "north": 18.62,
    "south": 18.42,
    "east": 73.94,
    "west": 73.77,
}

# Congestion bucket thresholds (congestion % = extra travel time vs free-flow).
# Tuned so "fast moving" (green) appears on side streets during normal hours,
# while arterials still reach slow/gridlocked at peak.
BUCKETS = [
    (0, "fast", 0.0, 35.0),        # fast moving
    (1, "moderate", 35.0, 60.0),   # moderate
    (2, "slow", 60.0, 90.0),       # slow
    (3, "gridlocked", 90.0, 1e9),  # gridlocked
]

# congestion % at/above which a segment counts as a "bottleneck" (slow or worse)
BOTTLENECK_THRESHOLD = 60.0

BUCKET_COLORS = {
    0: "#2ecc71",  # green
    1: "#f1c40f",  # yellow
    2: "#e67e22",  # orange
    3: "#c0392b",  # red
}

BUCKET_LABELS = {0: "Fast moving", 1: "Moderate", 2: "Slow", 3: "Gridlocked"}

# Default free-flow speed (km/h) by OSM highway type for an Indian city.
DEFAULT_FREEFLOW = {
    "motorway": 90,
    "motorway_link": 60,
    "trunk": 70,
    "trunk_link": 50,
    "primary": 55,
    "primary_link": 40,
    "secondary": 45,
    "secondary_link": 35,
    "tertiary": 40,
    "tertiary_link": 30,
    "residential": 30,
    "unclassified": 35,
    "living_street": 20,
    "service": 20,
    "track": 20,
    "road": 30,
}
DEFAULT_FREEFLOW_FALLBACK = 30

# Calibration curves derived from TomTom Traffic Index Pune 2025.
# Congestion level (%) by hour of day. Real anchors: morning rush 91.8%,
# evening rush 110.9%, annual average 71.1%, free-flow ~30 km/h at night.
WEEKDAY_CONGESTION = {
    0: 3, 1: 2, 2: 2, 3: 2, 4: 3, 5: 6,
    6: 15, 7: 28, 8: 45, 9: 70, 10: 85, 11: 80,
    12: 82, 13: 75, 14: 72, 15: 75, 16: 85, 17: 90,
    18: 108, 19: 95, 20: 62, 21: 42, 22: 27, 23: 12,
}

WEEKEND_CONGESTION = {
    0: 3, 1: 2, 2: 2, 3: 2, 4: 3, 5: 5,
    6: 8, 7: 14, 8: 25, 9: 38, 10: 48, 11: 55,
    12: 60, 13: 58, 14: 55, 15: 58, 16: 65, 17: 70,
    18: 75, 19: 68, 20: 52, 21: 38, 22: 22, 23: 10,
}

# Peak-hour windows used for bottleneck amplification (24h)
PEAK_HOURS = set(list(range(8, 12)) + list(range(17, 21)))

# Fraction of segments flagged as persistently congested
PERSISTENT_FRACTION = 0.08

# Pune localities within the core bbox. Used for the Forecast locality list,
# the search index, and to label otherwise-unnamed roads.
# (name, lat, lon, radius in degrees)
PUNE_AREAS = [
    ("Shivajinagar", 18.5315, 73.8487, 0.014),
    ("Swargate", 18.5018, 73.8635, 0.014),
    ("Camp", 18.5106, 73.8773, 0.013),
    ("Koregaon Park", 18.5425, 73.8972, 0.014),
    ("Kalyani Nagar", 18.5509, 73.9040, 0.013),
    ("Kothrud", 18.5043, 73.8121, 0.015),
    ("Aundh", 18.5586, 73.8082, 0.012),
    ("Baner", 18.5599, 73.7899, 0.012),
    ("Viman Nagar", 18.5647, 73.9188, 0.013),
    ("Hadapsar", 18.5054, 73.9276, 0.015),
    ("Pashan", 18.5485, 73.7910, 0.013),
    ("Erandwane", 18.5074, 73.8330, 0.012),
    ("Katraj", 18.4550, 73.8580, 0.016),
    ("Bharati Vidyapeeth", 18.4565, 73.8625, 0.012),
    ("Pimpri-Chinchwad", 18.6050, 73.8000, 0.020),
    ("Warje", 18.4850, 73.8000, 0.014),
    ("Wagholi", 18.5800, 73.9180, 0.016),
    ("Balewadi", 18.5750, 73.7720, 0.013),
]

# descriptor suffix for unnamed roads, by OSM highway class
UNNAMED_ROAD_SUFFIX = {
    "residential": "residential lane",
    "living_street": "lane",
    "service": "service road",
    "unclassified": "local road",
    "road": "local road",
    "tertiary": "road",
    "track": "track",
}
UNNAMED_ROAD_DEFAULT = "local road"
