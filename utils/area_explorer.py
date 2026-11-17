"""
area_explorer.py
----------------
Exhaustive Nationwide Area & Locality Harvester for India & Worldwide.
Implements all 20 requirements from test.txt:
  1.  Real state boundary polygons (not just bbox) — OSM relation → cached → bbox fallback
  2.  True checkpoint resume — PENDING/PROCESSING/COMPLETED/SPLIT/FAILED/DENSITY_LIMITED
  3.  Configurable adaptive quadtree with SAFE_RESULT_THRESHOLD / DENSITY_LIMITED status
  4.  Decoupled Stage-A discovery (broad) + Stage-B classification
  5.  Layered Overpass queries (place, admin, landuse, named ways)
  6.  Spatially-scored polygon matching with confidence (HIGH/MEDIUM/LOW/REJECTED)
  7.  Multi-source hierarchy resolution (tags → spatial → Nominatim)
  8.  Global deduplication (OSM ID → geometry → name+state+district+proximity)
  9.  State-leakage prevention with CROSS_BOUNDARY flag
  10. Honest coverage reporting (geographic_tile_coverage_pct, locality_completeness=UNKNOWN)
  11. Correct failure handling (timeout ≠ empty; FAILED checkpoint, no silent zero)
  12. Full multilingual preservation (hi, mr, ta, te, bn, gu, kn, ml, pa, ur, old_name…)
  13. No fake polygon fabrication
  14. All existing functionality preserved
  15–19. See inline comments
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

# ─────────────────────────────────────────────────────────────
#  SAFE PRINT
# ─────────────────────────────────────────────────────────────

def _safe_print(msg: str):
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
            print(str(msg).encode(enc, errors="replace").decode(enc))
        except Exception:
            print(str(msg).encode("ascii", errors="replace").decode("ascii"))

# ─────────────────────────────────────────────────────────────
#  CONFIGURABLE CONSTANTS (Req 3)
# ─────────────────────────────────────────────────────────────

SAFE_RESULT_THRESHOLD = 2500   # split tile if results >= this (below API limit of ~3000)
MAX_TILE_DEPTH = 6             # maximum quadtree recursion depth
MIN_TILE_SIZE_DEGREES = 0.02   # do not subdivide below ~2.2km tiles

# ─────────────────────────────────────────────────────────────
#  OVERPASS & NOMINATIM
# ─────────────────────────────────────────────────────────────

OVERPASS_SERVERS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_URL = OVERPASS_SERVERS[0]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "").strip().lower()

HEADERS = {
    "User-Agent": "JarvisMapsAI/3.1 (Exhaustive Indian Spatial Engine; mailto:support@jarvismaps.ai)"
}

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jarvis_maps.db")

# ─────────────────────────────────────────────────────────────
#  36 STATES & UNION TERRITORIES — with OSM relation IDs (Req 1)
# ─────────────────────────────────────────────────────────────

INDIA_STATES_REGISTRY: Dict[str, Dict[str, Any]] = {
    "AN": {"name": "Andaman and Nicobar Islands", "type": "UT",    "osm_relation": 1993168,  "bbox": [6.75, 92.2, 13.7, 94.3]},
    "AP": {"name": "Andhra Pradesh",               "type": "State", "osm_relation": 2022095,  "bbox": [12.6, 76.7, 19.1, 84.8]},
    "AR": {"name": "Arunachal Pradesh",             "type": "State", "osm_relation": 1989755,  "bbox": [26.6, 91.5, 29.5, 97.4]},
    "AS": {"name": "Assam",                         "type": "State", "osm_relation": 1993149,  "bbox": [24.1, 89.7, 28.0, 96.0]},
    "BR": {"name": "Bihar",                         "type": "State", "osm_relation": 1958982,  "bbox": [24.2, 83.3, 27.6, 88.3]},
    "CH": {"name": "Chandigarh",                    "type": "UT",    "osm_relation": 1993158,  "bbox": [30.68, 76.72, 30.79, 76.85]},
    "CT": {"name": "Chhattisgarh",                  "type": "State", "osm_relation": 1993172,  "bbox": [17.7, 80.2, 24.1, 84.4]},
    "DH": {"name": "Dadra and Nagar Haveli and Daman and Diu", "type": "UT", "osm_relation": 1993171, "bbox": [20.0, 72.8, 20.5, 73.2]},
    "DL": {"name": "Delhi",                         "type": "UT",    "osm_relation": 1942586,  "bbox": [28.40, 76.83, 28.89, 77.35]},
    "GA": {"name": "Goa",                           "type": "State", "osm_relation": 1993153,  "bbox": [14.9, 73.6, 15.8, 74.4]},
    "GJ": {"name": "Gujarat",                       "type": "State", "osm_relation": 1949080,  "bbox": [20.1, 68.1, 24.7, 74.5]},
    "HR": {"name": "Haryana",                       "type": "State", "osm_relation": 1942601,  "bbox": [27.6, 74.4, 30.9, 77.6]},
    "HP": {"name": "Himachal Pradesh",              "type": "State", "osm_relation": 1993152,  "bbox": [30.3, 75.7, 33.3, 79.0]},
    "JK": {"name": "Jammu and Kashmir",             "type": "UT",    "osm_relation": 1993163,  "bbox": [32.2, 73.7, 35.5, 77.8]},
    "JH": {"name": "Jharkhand",                     "type": "State", "osm_relation": 1993175,  "bbox": [21.9, 83.3, 25.4, 87.9]},
    "KA": {"name": "Karnataka",                     "type": "State", "osm_relation": 2019939,  "bbox": [11.5, 74.0, 18.5, 78.6]},
    "KL": {"name": "Kerala",                        "type": "State", "osm_relation": 2018151,  "bbox": [8.1, 74.8, 12.8, 77.4]},
    "LA": {"name": "Ladakh",                        "type": "UT",    "osm_relation": 12491691, "bbox": [32.0, 75.5, 36.0, 80.5]},
    "LD": {"name": "Lakshadweep",                   "type": "UT",    "osm_relation": 1993174,  "bbox": [8.0, 71.0, 12.5, 74.0]},
    "MP": {"name": "Madhya Pradesh",                "type": "State", "osm_relation": 1950071,  "bbox": [21.0, 74.0, 26.9, 82.8]},
    "MH": {"name": "Maharashtra",                   "type": "State", "osm_relation": 1950884,  "bbox": [15.6, 72.6, 22.0, 80.9]},
    "MN": {"name": "Manipur",                       "type": "State", "osm_relation": 1993157,  "bbox": [23.8, 93.0, 25.7, 94.8]},
    "ML": {"name": "Meghalaya",                     "type": "State", "osm_relation": 1993159,  "bbox": [25.0, 89.8, 26.1, 92.8]},
    "MZ": {"name": "Mizoram",                       "type": "State", "osm_relation": 1993161,  "bbox": [21.9, 92.2, 24.5, 93.4]},
    "NL": {"name": "Nagaland",                      "type": "State", "osm_relation": 1993154,  "bbox": [25.1, 93.3, 27.0, 95.2]},
    "OD": {"name": "Odisha",                        "type": "State", "osm_relation": 1984022,  "bbox": [17.8, 81.3, 22.6, 87.5]},
    "PB": {"name": "Punjab",                        "type": "State", "osm_relation": 1942686,  "bbox": [29.5, 73.8, 32.5, 76.9]},
    "PY": {"name": "Puducherry",                    "type": "UT",    "osm_relation": 1993164,  "bbox": [11.8, 79.7, 12.1, 79.9]},
    "RJ": {"name": "Rajasthan",                     "type": "State", "osm_relation": 1942920,  "bbox": [23.0, 69.5, 30.2, 78.3]},
    "SK": {"name": "Sikkim",                        "type": "State", "osm_relation": 1993150,  "bbox": [27.0, 88.0, 28.1, 88.9]},
    "TN": {"name": "Tamil Nadu",                    "type": "State", "osm_relation": 96905,    "bbox": [8.0, 76.2, 13.6, 80.4]},
    "TG": {"name": "Telangana",                     "type": "State", "osm_relation": 3250963,  "bbox": [15.8, 77.2, 19.9, 81.8]},
    "TR": {"name": "Tripura",                       "type": "State", "osm_relation": 1993162,  "bbox": [22.9, 91.1, 24.5, 92.4]},
    "UP": {"name": "Uttar Pradesh",                 "type": "State", "osm_relation": 1942587,  "bbox": [23.8, 77.0, 30.4, 84.6]},
    "UK": {"name": "Uttarakhand",                   "type": "State", "osm_relation": 1993160,  "bbox": [28.7, 77.5, 31.5, 81.1]},
    "WB": {"name": "West Bengal",                   "type": "State", "osm_relation": 1960177,  "bbox": [21.5, 85.8, 27.3, 89.9]},
}

# Alias map for state code lookups
STATE_CODE_MAP: Dict[str, Tuple[str, ...]] = {}
for _sc, _si in INDIA_STATES_REGISTRY.items():
    STATE_CODE_MAP[_sc.lower()] = (_si["name"].lower(),)
STATE_CODE_MAP["dl"] = ("delhi", "nct of delhi", "national capital territory of delhi")
STATE_CODE_MAP["or"] = ("odisha", "orissa")
STATE_CODE_MAP["ut"] = ("uttarakhand",)
STATE_CODE_MAP["cg"] = ("chhattisgarh",)
STATE_CODE_MAP["ts"] = ("telangana",)

# In-memory cache for fetched state boundary polygons (Req 1, Req 16)
_STATE_BOUNDARY_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}
_STATE_DISTRICTS_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}


def normalize_state_code(val: str) -> Tuple[str, str]:
    """
    Normalizes any state string, ISO code (e.g. 'IN-TN', 'IN-DL'),
    abbreviation (e.g. 'TN', 'DL'), or full name (e.g. 'Tamil Nadu', 'Delhi')
    to (canonical_2letter_code, canonical_full_name).
    """
    if not val:
        return ("", "")
    s = str(val).strip()
    s_upper = s.upper()
    if s_upper.startswith("IN-") or s_upper.startswith("IN_"):
        s_upper = s_upper[3:]
    if s_upper in INDIA_STATES_REGISTRY:
        return (s_upper, INDIA_STATES_REGISTRY[s_upper]["name"])
    s_lower = s.lower()
    if s_lower.startswith("in-") or s_lower.startswith("in_"):
        s_lower = s_lower[3:]
    alias_map = {"ut": "UK", "cg": "CT", "or": "OD", "ts": "TG"}
    for code, aliases in STATE_CODE_MAP.items():
        if s_lower == code or s_lower in aliases:
            target = alias_map.get(code, code.upper())
            if target in INDIA_STATES_REGISTRY:
                return (target, INDIA_STATES_REGISTRY[target]["name"])
    for code, info in INDIA_STATES_REGISTRY.items():
        if s_lower == info["name"].lower():
            return (code, info["name"])
    return ("", val)


def load_state_boundary(state_code: str, log: Optional[Callable] = None) -> Optional[Dict[str, Any]]:
    """
    Loads actual state boundary polygon. (Req 1)
    Hierarchy: Local cached GeoJSON → OSM relation lookup → bbox fallback.
    """
    norm_code, _ = normalize_state_code(state_code)
    code = norm_code or (state_code.upper().strip() if state_code else "")
    if not code:
        return None
    if code in _STATE_BOUNDARY_CACHE:
        return _STATE_BOUNDARY_CACHE[code]

    info = INDIA_STATES_REGISTRY.get(code)
    if not info:
        _STATE_BOUNDARY_CACHE[code] = None
        return None

    # 1. Local disk cache check (preferred and instant)
    local_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "boundaries", "states", f"{code}.json")
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                _STATE_BOUNDARY_CACHE[code] = data
                if log:
                    log(f"[BOUNDARY] {info['name']}: loaded from local verified cache", "info")
                return data
        except Exception:
            pass

    # 2. OSM relation lookup via Nominatim
    osm_rel_id = info.get("osm_relation")
    if osm_rel_id:
        try:
            params = {
                "osm_ids": f"R{osm_rel_id}",
                "format": "json",
                "polygon_geojson": 1,
            }
            resp = requests.get(
                "https://nominatim.openstreetmap.org/lookup",
                params=params,
                headers=HEADERS,
                timeout=20,
            )
            if resp.status_code == 200:
                items = resp.json()
                if items:
                    geojson = items[0].get("geojson")
                    if geojson and geojson.get("type") in ("Polygon", "MultiPolygon"):
                        result = {
                            "polygon_geom": geojson,
                            "source": f"OSM_relation_{osm_rel_id}",
                            "verified": True,
                            "relation_id": osm_rel_id,
                        }
                        _STATE_BOUNDARY_CACHE[code] = result
                        try:
                            os.makedirs(os.path.dirname(local_path), exist_ok=True)
                            with open(local_path, "w", encoding="utf-8") as f:
                                json.dump(result, f)
                        except Exception:
                            pass
                        if log:
                            log(f"[BOUNDARY] {info['name']}: verified via OSM relation {osm_rel_id}", "info")
                        return result
        except Exception as e:
            if log:
                log(f"[BOUNDARY] {info['name']}: OSM lookup failed ({e}), using bbox fallback", "warn")

    # 3. Bbox fallback
    bbox = info.get("bbox")
    if bbox:
        s, w, n, e = bbox
        fallback = {
            "polygon_geom": {
                "type": "Polygon",
                "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]
            },
            "source": "bbox_fallback",
            "verified": False,
            "relation_id": None,
        }
        _STATE_BOUNDARY_CACHE[code] = fallback
        if log:
            log(f"[BOUNDARY] {info['name']}: using bbox fallback (unverified)", "warn")
        return fallback

    _STATE_BOUNDARY_CACHE[code] = None
    return None


def load_state_districts(state_code: str) -> Dict[str, Dict[str, Any]]:
    """Loads all district boundary polygons for a given state code from disk or cache."""
    norm_code, _ = normalize_state_code(state_code)
    code = norm_code or (state_code.upper().strip() if state_code else "")
    if not code:
        return {}
    if code in _STATE_DISTRICTS_CACHE:
        return _STATE_DISTRICTS_CACHE[code]
    dist_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "boundaries", "districts", f"{code}.json")
    if os.path.exists(dist_file):
        try:
            with open(dist_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                _STATE_DISTRICTS_CACHE[code] = data
                return data
        except Exception:
            pass
    _STATE_DISTRICTS_CACHE[code] = {}
    return {}


def tile_intersects_polygon(south: float, west: float, north: float, east: float,
                             polygon_geom: Dict[str, Any]) -> bool:
    """
    Quick bbox-vs-polygon intersection check. (Req 1, 9)
    Returns True if the tile's bbox may overlap the polygon.
    Uses corner-point sampling + centroid for speed.
    """
    if not polygon_geom:
        return True
    mid_lat = (south + north) / 2
    mid_lon = (west + east) / 2
    test_points = [
        (south, west), (south, east), (north, west), (north, east),
        (mid_lat, mid_lon),
        (south, mid_lon), (north, mid_lon), (mid_lat, west), (mid_lat, east),
    ]
    for lat, lon in test_points:
        if is_point_inside_boundary(lat, lon, polygon_geom):
            return True
    return False


def verify_object_state(lat: float, lon: float, expected_state_code: str,
                         state_boundary: Optional[Dict[str, Any]]) -> str:
    """
    Verifies that a discovered object belongs to the expected state. (Req 9)
    Returns: 'IN_STATE' | 'CROSS_BOUNDARY' | 'OUTSIDE_STATE' | 'UNVERIFIED'
    """
    if not state_boundary or not state_boundary.get("polygon_geom"):
        return "UNVERIFIED"
    inside = is_point_inside_boundary(lat, lon, state_boundary["polygon_geom"])
    if inside:
        return "IN_STATE"
    if not state_boundary.get("verified"):
        return "UNVERIFIED"
    return "OUTSIDE_STATE"


# ─────────────────────────────────────────────────────────────
#  EXPANDED INDIAN LOCALITY TAXONOMY (40+ types)
# ─────────────────────────────────────────────────────────────

SETTLEMENT_PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    ("colony",         "Colony",              re.compile(r"\b(colony|cly)\b", re.I)),
    ("nagar",          "Nagar",               re.compile(r"\b(nagar|ngr)\b", re.I)),
    ("vihar",          "Vihar",               re.compile(r"\b(vihar|vihare)\b", re.I)),
    ("enclave",        "Enclave",             re.compile(r"\b(enclave|encl)\b", re.I)),
    ("extension",      "Extension",           re.compile(r"\b(extension|extn|ext)\b", re.I)),
    ("puram_puri",     "Puram / Puri",        re.compile(r"\b(puram|puri|pooram)\b", re.I)),
    ("kunj",           "Kunj",                re.compile(r"\b(kunj)\b", re.I)),
    ("bagh",           "Bagh",                re.compile(r"\b(bagh|baug)\b", re.I)),
    ("niketan",        "Niketan",             re.compile(r"\b(niketan)\b", re.I)),
    ("layout",         "Layout",              re.compile(r"\b(layout|lyt)\b", re.I)),
    ("housing_board",  "Housing Board",       re.compile(r"\b(housing board|rhb|khb|mhada|hudco)\b", re.I)),
    ("awas",           "Awas / Parisar",      re.compile(r"\b(awas|avas|parisar)\b", re.I)),
    ("basti_mohalla",  "Basti / Mohalla",     re.compile(r"\b(basti|mohalla)\b", re.I)),
    ("tola_para",      "Tola / Para / Pally", re.compile(r"\b(tola|para|pally|palli)\b", re.I)),
    ("gaon_gram",      "Gaon / Gram",         re.compile(r"\b(gaon|gram|gramam)\b", re.I)),
    ("village",        "Village",             re.compile(r"\b(village|vill)\b", re.I)),
    ("dhani",          "Dhani",               re.compile(r"\b(dhani)\b", re.I)),
    ("pada_wadi",      "Pada / Wadi / Chawl", re.compile(r"\b(pada|wadi|vadi|chawl)\b", re.I)),
    ("kheda_hamlet",   "Kheda / Hamlet",      re.compile(r"\b(kheda|hamlet|mauza|majra)\b", re.I)),
    ("tanda_cheri",    "Tanda / Cheri",       re.compile(r"\b(tanda|cheri|hatti|falia|khera|nagla|dera)\b", re.I)),
    ("gali_lane",      "Gali / Lane",         re.compile(r"\b(gali|lane|rasta)\b", re.I)),
    ("ward",           "Ward",                re.compile(r"\bward[\s\-_]*([0-9]{1,3}|[a-z])\b", re.I)),
    ("block",          "Block",               re.compile(r"\b(block[\s\-_]*[a-z0-9]+|[a-z0-9]+[\s\-_]*block)\b", re.I)),
    ("pocket",         "Pocket",              re.compile(r"\bpocket[\s\-_]*([0-9a-z\-]+)\b|\bpkt[\s\-_]*([0-9a-z\-]+)\b", re.I)),
    ("phase",          "Phase",               re.compile(r"\bphase[\s\-_]*([0-9ivx]+)\b", re.I)),
    ("sector",         "Sector",              re.compile(r"\bsec(?:tor)?[\s\-_]*([0-9]{1,4}[a-z]?)\b", re.I)),
    ("scheme",         "Scheme",              re.compile(r"\bscheme[\s\-_]*([0-9a-z]+)\b", re.I)),
    ("society",        "Society / Apartment", re.compile(r"\b(society|soc|apartments?|apt|heights|residency|greens|towers?)\b", re.I)),
    ("township",       "Township",            re.compile(r"\b(township)\b", re.I)),
    ("industrial_area","Industrial Area",     re.compile(r"\b(industrial area|industrial estate|midc|gidc|riico|sez|sidco|sipcot|kiadb|upsidc|idpl)\b", re.I)),
    ("market_mandi",   "Market / Mandi",      re.compile(r"\b(market|mandi|bazaar|bazar|haat|chowk|chawk|shopping center)\b", re.I)),
    ("institutional",  "Institutional Area",  re.compile(r"\b(institutional area|knowledge park|campus|it park|tech park|cyber city)\b", re.I)),
    ("cantt",          "Cantonment",          re.compile(r"\b(cantonment|cantt|military camp)\b", re.I)),
    ("suburb",         "Suburb",              re.compile(r"\b(suburb)\b", re.I)),
    ("locality",       "Locality",            re.compile(r"\b(locality)\b", re.I)),
    ("tehsil_taluk",   "Tehsil / Taluk",      re.compile(r"\b(tehsil|taluk|taluka|mandal)\b", re.I)),
]

SUPPORTED_TYPES = {t[0] for t in SETTLEMENT_PATTERNS}
SUPPORTED_TYPES.update({"named_area", "neighbourhood", "pincode_zone", "zone_circle", "town"})

AREA_TYPE_KEY_MAP = {
    "locality": "locality", "suburb": "suburb", "major_area": "suburb",
    "sector": "sector", "colony": "colony", "nagar_/_vihar": "nagar",
    "nagar_vihar": "nagar", "nagar": "nagar", "vihar": "vihar",
    "neighbourhood": "neighbourhood", "neighborhood": "neighbourhood",
    "block": "block", "phase": "phase", "society": "society",
    "township": "township", "village": "village", "ward": "ward",
    "industrial_area": "industrial_area", "industrial": "industrial_area",
    "commercial_market": "market_mandi", "commercial": "market_mandi", "market": "market_mandi",
    "tehsil_taluk": "tehsil_taluk", "taluk": "tehsil_taluk", "tehsil": "tehsil_taluk",
    "zone_circle": "zone_circle", "zone": "zone_circle", "town": "town",
    "hamlet_basti": "basti_mohalla", "basti": "basti_mohalla",
    "institutional": "institutional", "pincode_zone": "pincode_zone", "pincode": "pincode_zone",
}


# ─────────────────────────────────────────────────────────────
#  DATA CLASSES — AreaRecord & CoverageReport (Req 10)
# ─────────────────────────────────────────────────────────────

@dataclass
class AreaRecord:
    osm_id: Optional[str] = None
    osm_type: str = "node"
    name: str = ""
    name_en: str = ""
    name_native: str = ""
    vernacular_names: Dict[str, str] = field(default_factory=dict)
    settlement_type: str = "named_area"
    display_type: str = "Named Area"
    parent_area: str = ""
    country: str = "India"
    state: str = ""
    state_code: str = ""
    district: str = ""
    subdistrict: str = ""
    tehsil: str = ""
    city: str = ""
    town: str = ""
    village: str = ""
    postal_code: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    discovery_status: str = "FOUND"
    polygon_status: str = "NOT_FOUND"
    polygon_match_confidence: str = ""        # HIGH / MEDIUM / LOW / REJECTED (Req 6)
    geometry_type: str = "Point"
    polygon_points: int = 0
    geometry: Optional[Dict[str, Any]] = None
    geojson: str = ""
    tile_id: str = ""
    source: str = "OpenStreetMap"
    maps_url: str = ""
    state_membership: str = "UNVERIFIED"     # IN_STATE / CROSS_BOUNDARY / OUTSIDE_STATE / UNVERIFIED (Req 9)
    hierarchy_confidence: str = "LOW"        # HIGH / MEDIUM / LOW (Req 7)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["vernacular_names"] = json.dumps(self.vernacular_names, ensure_ascii=False)
        return d


@dataclass
class CoverageReport:
    """Honest coverage metrics — tile coverage ≠ locality completeness. (Req 10)"""
    region_name: str = ""
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0
    # Tile metrics
    total_tiles_planned: int = 0
    total_tiles_processed: int = 0
    total_tiles_subdivided: int = 0
    completed_tiles: int = 0
    failed_tiles: int = 0
    density_limited_tiles: int = 0
    pending_tiles: int = 0
    # Query metrics
    total_queries_executed: int = 0
    failed_queries: int = 0
    retried_queries: int = 0
    # Discovery and funnel metrics (Req 8, 10 in test.txt)
    total_raw_elements: int = 0
    unique_named_areas: int = 0
    raw_objects_received: int = 0
    objects_after_state_validation: int = 0
    objects_after_classification: int = 0
    objects_after_deduplication: int = 0
    polygons_found: int = 0
    polygon_missing_count: int = 0
    points_found: int = 0
    state_boundary_verified_count: int = 0
    state_boundary_uncertain_count: int = 0
    in_state_count: int = 0
    outside_state_count: int = 0
    cross_boundary_count: int = 0
    unverified_state_count: int = 0
    hierarchy_resolved: int = 0
    hierarchy_unknown: int = 0
    successful_query_count: int = 0
    failed_query_count: int = 0
    duplicates_removed: int = 0
    state_breakdown: Dict[str, int] = field(default_factory=dict)
    settlement_type_breakdown: Dict[str, int] = field(default_factory=dict)
    # Honest completeness (Req 10)
    geographic_tile_coverage_pct: float = 0.0
    locality_completeness: str = "UNKNOWN"
    completeness_note: str = "Completeness depends on OSM data density, which varies by region. Tile coverage ≠ locality coverage."
    coverage_limitations: List[str] = field(default_factory=list)

    def finish(self) -> Dict[str, Any]:
        self.end_time = time.time()
        self.raw_objects_received = self.total_raw_elements
        self.objects_after_deduplication = self.unique_named_areas
        if self.total_tiles_planned > 0:
            self.geographic_tile_coverage_pct = round(
                self.completed_tiles / self.total_tiles_planned * 100.0, 2
            )
        else:
            self.geographic_tile_coverage_pct = 100.0 if self.unique_named_areas > 0 else 0.0
        self.locality_completeness = "UNKNOWN"
        return asdict(self)


# ─────────────────────────────────────────────────────────────
#  SQLITE CHECKPOINT & PERSISTENCE (Req 2)
# ─────────────────────────────────────────────────────────────

def init_area_harvest_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Initialize SQLite tables. Schema includes parent_tile_id and PROCESSING/DENSITY_LIMITED. (Req 2)"""
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS area_harvest_checkpoints (
                tile_id TEXT PRIMARY KEY,
                parent_tile_id TEXT,
                region_name TEXT,
                bbox_json TEXT,
                status TEXT DEFAULT 'PENDING',
                items_count INTEGER DEFAULT 0,
                depth INTEGER DEFAULT 0,
                created_at REAL,
                updated_at REAL
            )
        """)
        # Migration: add parent_tile_id if old schema
        try:
            conn.execute("ALTER TABLE area_harvest_checkpoints ADD COLUMN parent_tile_id TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE area_harvest_checkpoints ADD COLUMN created_at REAL")
        except Exception:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS area_explorer_results (
                osm_id TEXT PRIMARY KEY,
                name TEXT,
                name_en TEXT,
                name_native TEXT,
                vernacular_json TEXT,
                settlement_type TEXT,
                display_type TEXT,
                parent_area TEXT,
                country TEXT,
                state TEXT,
                state_code TEXT,
                district TEXT,
                subdistrict TEXT,
                tehsil TEXT,
                city TEXT,
                town TEXT,
                village TEXT,
                postal_code TEXT,
                latitude REAL,
                longitude REAL,
                discovery_status TEXT,
                polygon_status TEXT,
                polygon_match_confidence TEXT,
                geometry_type TEXT,
                polygon_points INTEGER,
                geojson TEXT,
                tile_id TEXT,
                source TEXT,
                maps_url TEXT,
                state_membership TEXT,
                hierarchy_confidence TEXT,
                created_at REAL
            )
        """)
        # Migrations for new columns
        for col, coltype in [
            ("polygon_match_confidence", "TEXT"),
            ("state_membership", "TEXT"),
            ("hierarchy_confidence", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE area_explorer_results ADD COLUMN {col} {coltype}")
            except Exception:
                pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_area_state ON area_explorer_results (state_code);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_area_district ON area_explorer_results (district);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_area_city ON area_explorer_results (city);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_area_type ON area_explorer_results (settlement_type);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_checkpoint_status ON area_harvest_checkpoints (status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_checkpoint_region ON area_harvest_checkpoints (region_name);")
    return conn


def load_checkpoint_tiles(conn: sqlite3.Connection, region_name: str) -> Dict[str, Dict[str, Any]]:
    """
    Loads existing tile checkpoints for a region. (Req 2)
    Returns dict keyed by tile_id with status and bbox info.
    PROCESSING tiles (from a previous crash) are treated as PENDING to re-execute.
    """
    tiles: Dict[str, Dict[str, Any]] = {}
    try:
        cursor = conn.execute(
            "SELECT tile_id, bbox_json, status, depth, items_count, parent_tile_id FROM area_harvest_checkpoints WHERE region_name = ?",
            (region_name,)
        )
        for row in cursor.fetchall():
            tile_id, bbox_json, status, depth, items_count, parent_tile_id = row
            # Recover crashed PROCESSING tiles as PENDING (Req 2)
            if status == "PROCESSING":
                status = "PENDING"
            tiles[tile_id] = {
                "bbox": json.loads(bbox_json) if bbox_json else None,
                "status": status,
                "depth": depth or 0,
                "items_count": items_count or 0,
                "parent_tile_id": parent_tile_id,
            }
    except Exception as e:
        _safe_print(f"Warning: Could not load checkpoint tiles: {e}")
    return tiles


def save_checkpoint(conn: sqlite3.Connection, tile_id: str, region_name: str, bbox: List[float],
                    status: str, items_count: int, depth: int, parent_tile_id: str = ""):
    """Atomic checkpoint write using WAL journal. (Req 2, 11)"""
    now = time.time()
    try:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO area_harvest_checkpoints
                    (tile_id, parent_tile_id, region_name, bbox_json, status, items_count, depth, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(
                    (SELECT created_at FROM area_harvest_checkpoints WHERE tile_id = ?), ?
                ), ?)
            """, (tile_id, parent_tile_id, region_name, json.dumps(bbox), status, items_count, depth,
                  tile_id, now, now))
    except Exception as e:
        _safe_print(f"Warning: Failed to save checkpoint for {tile_id}: {e}")


def save_area_records_batch(conn: sqlite3.Connection, records: List[AreaRecord]):
    """Stream AreaRecord objects into SQLite via batch insert."""
    if not records:
        return
    now = time.time()
    rows = [(
        r.osm_id or f"{r.latitude:.6f}_{r.longitude:.6f}",
        r.name, r.name_en, r.name_native,
        json.dumps(r.vernacular_names, ensure_ascii=False),
        r.settlement_type, r.display_type, r.parent_area,
        r.country, r.state, r.state_code, r.district, r.subdistrict, r.tehsil,
        r.city, r.town, r.village, r.postal_code,
        r.latitude, r.longitude, r.discovery_status,
        r.polygon_status, r.polygon_match_confidence,
        r.geometry_type, r.polygon_points, r.geojson,
        r.tile_id, r.source, r.maps_url,
        r.state_membership, r.hierarchy_confidence, now
    ) for r in records]
    try:
        with conn:
            conn.executemany("""
                INSERT OR REPLACE INTO area_explorer_results (
                    osm_id, name, name_en, name_native, vernacular_json, settlement_type, display_type, parent_area,
                    country, state, state_code, district, subdistrict, tehsil, city, town, village, postal_code,
                    latitude, longitude, discovery_status, polygon_status, polygon_match_confidence,
                    geometry_type, polygon_points, geojson, tile_id, source, maps_url,
                    state_membership, hierarchy_confidence, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
    except Exception as e:
        _safe_print(f"Warning: Failed to insert area records batch: {e}")


# ─────────────────────────────────────────────────────────────
#  GEOMETRY UTILITIES
# ─────────────────────────────────────────────────────────────

def calculate_polygon_centroid(coords: List[List[float]]) -> Tuple[float, float]:
    if not coords:
        return (0.0, 0.0)
    return (round(sum(p[1] for p in coords) / len(coords), 7),
            round(sum(p[0] for p in coords) / len(coords), 7))


def is_point_inside_boundary(lat: float, lon: float, polygon_geom: Dict[str, Any]) -> bool:
    """Ray-casting point-in-polygon for GeoJSON Polygon/MultiPolygon."""
    if not polygon_geom or "coordinates" not in polygon_geom:
        return True

    def ray_cast(px, py, poly):
        inside = False
        n = len(poly)
        if n < 3:
            return False
        x1, y1 = poly[0]
        for i in range(1, n + 1):
            x2, y2 = poly[i % n]
            if py > min(y1, y2) and py <= max(y1, y2) and px <= max(x1, x2):
                if y1 != y2:
                    xi = (py - y1) * (x2 - x1) / (y2 - y1) + x1
                if x1 == x2 or px <= xi:
                    inside = not inside
            x1, y1 = x2, y2
        return inside

    gtype = polygon_geom.get("type")
    coords = polygon_geom.get("coordinates", [])
    if gtype == "Polygon":
        if not coords:
            return True
        if not ray_cast(lon, lat, coords[0]):
            return False
        for hole in coords[1:]:
            if ray_cast(lon, lat, hole):
                return False
        return True
    elif gtype == "MultiPolygon":
        for poly in coords:
            if not poly:
                continue
            if ray_cast(lon, lat, poly[0]):
                in_hole = any(ray_cast(lon, lat, h) for h in poly[1:])
                if not in_hole:
                    return True
        return False
    return True


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Returns distance in metres between two WGS84 coordinates."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def stitch_osm_ways_to_polygon(members: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str, int]:
    """Stitches relation outer way members into GeoJSON Polygon / MultiPolygon."""
    outer_ways = [
        [[p["lon"], p["lat"]] for p in m.get("geometry", [])]
        for m in members
        if m.get("type") == "way" and m.get("role") in ("outer", "") and len(m.get("geometry", [])) >= 2
    ]
    if not outer_ways:
        return None, "Point", 0

    rings, used = [], [False] * len(outer_ways)
    for i in range(len(outer_ways)):
        if used[i]:
            continue
        ring = list(outer_ways[i])
        used[i] = True
        for _ in range(len(outer_ways) * 2):
            if math.isclose(ring[0][0], ring[-1][0], abs_tol=1e-5) and math.isclose(ring[0][1], ring[-1][1], abs_tol=1e-5):
                break
            matched = False
            for j in range(len(outer_ways)):
                if used[j]:
                    continue
                c = outer_ways[j]
                if math.isclose(ring[-1][0], c[0][0], abs_tol=1e-5) and math.isclose(ring[-1][1], c[0][1], abs_tol=1e-5):
                    ring.extend(c[1:]); used[j] = True; matched = True; break
                elif math.isclose(ring[-1][0], c[-1][0], abs_tol=1e-5) and math.isclose(ring[-1][1], c[-1][1], abs_tol=1e-5):
                    ring.extend(reversed(c[:-1])); used[j] = True; matched = True; break
            if not matched:
                break
        if len(ring) >= 4:
            if not (math.isclose(ring[0][0], ring[-1][0], abs_tol=1e-5) and math.isclose(ring[0][1], ring[-1][1], abs_tol=1e-5)):
                ring.append(ring[0])
            rings.append(ring)

    if not rings:
        return None, "Point", 0
    if len(rings) == 1:
        return {"type": "Polygon", "coordinates": rings}, "Polygon", len(rings[0])
    return {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}, "MultiPolygon", sum(len(r) for r in rings)


def extract_osm_element_geometry(element: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str, int]:
    """Extracts authentic geometry from an OSM element. Never fabricates a polygon from bbox. (Req 13)"""
    etype = element.get("type")
    if etype == "way":
        geom = element.get("geometry", [])
        if geom and len(geom) >= 3:
            coords = [[round(p["lon"], 7), round(p["lat"], 7)] for p in geom]
            if math.isclose(coords[0][0], coords[-1][0], abs_tol=1e-5) and math.isclose(coords[0][1], coords[-1][1], abs_tol=1e-5):
                return {"type": "Polygon", "coordinates": [coords]}, "Polygon", len(coords)
    elif etype == "relation":
        members = element.get("members", [])
        if members:
            pd, gt, pts = stitch_osm_ways_to_polygon(members)
            if pd:
                return pd, gt, pts

    lat = element.get("lat") or (element.get("center") or {}).get("lat")
    lon = element.get("lon") or (element.get("center") or {}).get("lon")
    if lat is not None and lon is not None:
        return {"type": "Point", "coordinates": [round(float(lon), 7), round(float(lat), 7)]}, "Point", 0
    return None, "None", 0


# ─────────────────────────────────────────────────────────────
#  MULTILINGUAL NAME EXTRACTION (Req 12)
# ─────────────────────────────────────────────────────────────

VERNACULAR_LANG_KEYS = [
    "name:en", "name:hi", "name:mr", "name:ta", "name:te", "name:kn", "name:ml",
    "name:gu", "name:bn", "name:pa", "name:ur", "name:or", "name:as",
    "official_name", "alt_name", "loc_name", "short_name", "int_name", "old_name",
]

def extract_multilingual_names(tags: Dict[str, Any]) -> Tuple[str, str, str, Dict[str, str]]:
    """Preserves ALL language name variants. Never drops area if name:en is absent."""
    vernacular: Dict[str, str] = {k: str(v).strip() for k, v in tags.items()
                                   if k in VERNACULAR_LANG_KEYS and str(v).strip()}
    raw_name = tags.get("name") or tags.get("official_name") or tags.get("name:en")
    if not raw_name:
        for k in ("name:hi", "name:mr", "name:ta", "name:te", "name:bn", "name:gu", "name:kn", "name:ml"):
            if tags.get(k):
                raw_name = tags.get(k)
                break
    canonical = str(raw_name).strip() if raw_name else ""
    english = tags.get("name:en") or canonical
    native = (tags.get("name:hi") or tags.get("name:mr") or tags.get("name:ta") or
              tags.get("name:te") or canonical)
    return canonical, english, native, vernacular


# ─────────────────────────────────────────────────────────────
#  STAGE-B CLASSIFICATION (Req 4)
# ─────────────────────────────────────────────────────────────

def classify_settlement(name: str, tags: Dict[str, Any]) -> Tuple[str, str]:
    """
    Stage-B: classify a named entity. If unrecognised, return ('named_area', 'Named Area').
    Never drops an area. (Req 4)
    """
    nc = f" {name} "
    place = tags.get("place", "").lower()
    landuse = tags.get("landuse", "").lower()
    boundary = tags.get("boundary", "").lower()

    if place in ("suburb", "neighbourhood", "quarter"):
        for key, disp, pat in SETTLEMENT_PATTERNS:
            if pat.search(nc):
                return key, disp
        return ("suburb" if place == "suburb" else "neighbourhood"), place.title()
    if place in ("village", "hamlet", "isolated_dwelling"):
        for key, disp, pat in SETTLEMENT_PATTERNS:
            if pat.search(nc):
                return key, disp
        return "village", "Village"
    if place in ("town", "city"):
        return "town", place.title()
    if landuse == "residential":
        for key, disp, pat in SETTLEMENT_PATTERNS:
            if pat.search(nc):
                return key, disp
        return "colony", "Residential Area"
    if landuse in ("industrial", "commercial", "retail"):
        return "industrial_area" if landuse == "industrial" else "market_mandi", landuse.title()
    if boundary == "administrative":
        al = tags.get("admin_level", "")
        if al in ("6", "7"):
            return "tehsil_taluk", "Tehsil / Taluk"
        elif al in ("8", "9", "10"):
            return "ward", "Municipal Ward"
    for key, disp, pat in SETTLEMENT_PATTERNS:
        if pat.search(nc):
            return key, disp
    return "named_area", "Named Area"


# ─────────────────────────────────────────────────────────────
#  ADMINISTRATIVE HIERARCHY RESOLUTION (Req 7)
# ─────────────────────────────────────────────────────────────

def resolve_administrative_hierarchy(tags: Dict[str, Any], state_hint: str = "",
                                     lat: Optional[float] = None, lon: Optional[float] = None,
                                     name: str = "") -> Dict[str, str]:
    """
    Preferred hierarchy:
    1. Spatial administrative containment (cached district polygons)
    2. OSM administrative relations / tags
    3. OSM addr/is_in tags
    4. Nominatim fallback (done via enrich_parent_areas)
    Returns dict with country, state, state_code, district, subdistrict, tehsil, city, parent_area. (Req 2, 3, 7)
    """
    raw_state = tags.get("addr:state") or tags.get("is_in:state") or state_hint or ""
    state_code, state_name = normalize_state_code(raw_state)
    state = state_name if state_name else raw_state

    district = tags.get("addr:district") or tags.get("is_in:district") or tags.get("is_in:county") or ""
    subdistrict = tags.get("addr:subdistrict") or tags.get("is_in:subdistrict") or tags.get("addr:mandal") or ""
    tehsil = (tags.get("addr:tehsil") or tags.get("is_in:tehsil") or
              tags.get("addr:taluk") or tags.get("is_in:taluka") or
              tags.get("addr:subdistrict") or tags.get("is_in:subdistrict") or "")
    city = tags.get("addr:city") or tags.get("is_in:city") or ""
    parent_area = (tags.get("addr:suburb") or tags.get("is_in:suburb") or
                   tags.get("addr:locality") or tags.get("is_in:locality") or
                   tags.get("addr:quarter") or "")

    # Level 1: Spatial District Containment (Req 2, 7)
    if not district and lat is not None and lon is not None and state_code:
        dist_map = load_state_districts(state_code)
        for dname, dinfo in dist_map.items():
            poly = dinfo.get("polygon_geom")
            if poly and is_point_inside_boundary(lat, lon, poly):
                district = dname
                break

    # Self-parent protection: an object must not identify itself as its own parent (Req 4)
    if parent_area and name:
        p_clean = parent_area.strip().lower()
        n_clean = name.strip().lower()
        if p_clean == n_clean or p_clean in n_clean or n_clean in p_clean:
            parent_area = ""

    confidence = "HIGH" if (state_code and district) else ("MEDIUM" if state_code else "LOW")
    return {
        "country": tags.get("addr:country") or "India",
        "state": state, "state_code": state_code,
        "district": district, "subdistrict": subdistrict,
        "tehsil": tehsil, "city": city, "parent_area": parent_area,
        "hierarchy_confidence": confidence,
    }


# ─────────────────────────────────────────────────────────────
#  NOMINATIM REVERSE-GEOCODE PARENT AREA ENRICHER (Req 7)
# ─────────────────────────────────────────────────────────────

_REVERSE_CACHE: Dict[Tuple[float, float], Dict[str, str]] = {}

def _nominatim_reverse_single(lat: float, lon: float) -> Dict[str, str]:
    """Single Nominatim reverse-geocode call with cache."""
    key = (round(lat, 3), round(lon, 3))
    if key in _REVERSE_CACHE:
        return _REVERSE_CACHE[key]
    result: Dict[str, str] = {}
    try:
        resp = requests.get(
            NOMINATIM_REVERSE_URL,
            params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1, "zoom": 14},
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            addr = resp.json().get("address", {})
            result["suburb"] = (addr.get("suburb") or addr.get("neighbourhood") or
                                 addr.get("quarter") or addr.get("village") or "")
            result["city"] = (addr.get("city") or addr.get("town") or
                               addr.get("municipality") or "")
            result["district"] = (addr.get("state_district") or addr.get("district") or
                                   addr.get("county") or "")
            result["subdistrict"] = (addr.get("city_district") or addr.get("subdistrict") or
                                      addr.get("subcounty") or "")
            result["tehsil"] = (addr.get("tehsil") or addr.get("taluk") or
                                addr.get("city_district") or addr.get("subdistrict") or "")
            result["state"] = addr.get("state") or ""
            result["postcode"] = addr.get("postcode") or ""
    except Exception:
        pass
    _REVERSE_CACHE[key] = result
    return result


def enrich_parent_areas(records: List[AreaRecord], log: Optional[Callable] = None,
                         max_enrich: int = 500) -> List[AreaRecord]:
    """
    Enriches parent_area, tehsil, district, city, state for records missing those fields
    via spatial district polygons + cached Nominatim reverse-geocoding. (Req 2, 3, 4, 7)
    """
    # 1. First pass: Spatial District & Self-Parent check for ALL records
    for rec in records:
        if not rec.district and rec.state_code and rec.latitude and rec.longitude:
            dist_map = load_state_districts(rec.state_code)
            for dname, dinfo in dist_map.items():
                poly = dinfo.get("polygon_geom")
                if poly and is_point_inside_boundary(rec.latitude, rec.longitude, poly):
                    rec.district = dname
                    if rec.hierarchy_confidence == "LOW":
                        rec.hierarchy_confidence = "HIGH"
                    break

        # Sanitize existing self-parent
        if rec.parent_area and rec.name:
            p_clean = rec.parent_area.strip().lower()
            n_clean = rec.name.strip().lower()
            if p_clean == n_clean or p_clean in n_clean or n_clean in p_clean:
                rec.parent_area = ""

    # 2. Second pass: Nominatim enrichment for records needing parent_area or tehsil
    needs = [r for r in records if (not r.parent_area or not r.tehsil or not r.district) and r.latitude and r.longitude]
    if not needs:
        return records

    seen_keys: Set[Tuple[float, float]] = set()
    to_call: List[AreaRecord] = []
    for r in needs:
        k = (round(r.latitude, 3), round(r.longitude, 3))
        if k not in seen_keys:
            to_call.append(r)
            seen_keys.add(k)
    to_call = to_call[:max_enrich]

    if log:
        log(f"[HIERARCHY] Reverse-geocoding {len(to_call)} unique locations for hierarchy & parent area...", "info")

    for idx, r in enumerate(to_call):
        addr = _nominatim_reverse_single(r.latitude, r.longitude)
        time.sleep(1.05)
        ck = (round(r.latitude, 3), round(r.longitude, 3))
        for rec in records:
            if (round(rec.latitude, 3), round(rec.longitude, 3)) == ck:
                # District
                if not rec.district and addr.get("district"):
                    rec.district = addr["district"]
                    if rec.hierarchy_confidence in ("LOW", "MEDIUM"):
                        rec.hierarchy_confidence = "HIGH"
                # Tehsil & Subdistrict (Req 3)
                if not rec.tehsil and addr.get("tehsil"):
                    rec.tehsil = addr["tehsil"]
                if not rec.subdistrict and addr.get("subdistrict"):
                    rec.subdistrict = addr["subdistrict"]
                if not rec.city and addr.get("city"):
                    rec.city = addr["city"]
                if not rec.state and addr.get("state"):
                    rec.state = addr["state"]
                if not rec.postal_code and addr.get("postcode"):
                    rec.postal_code = addr["postcode"]

                # Parent Area with self-parent and circular protection (Req 4)
                cand_parent = (addr.get("suburb") or "").strip()
                if cand_parent and not rec.parent_area:
                    cand_clean = cand_parent.lower()
                    name_clean = rec.name.strip().lower()
                    # Reject candidate if candidate_parent == current_area or vice-versa
                    if (cand_clean != name_clean and
                        cand_clean not in name_clean and
                        name_clean not in cand_clean):
                        rec.parent_area = cand_parent
                        if rec.hierarchy_confidence == "LOW":
                            rec.hierarchy_confidence = "MEDIUM"

        if log and (idx + 1) % 50 == 0:
            log(f"[HIERARCHY] Enriched {idx + 1}/{len(to_call)} locations...", "info")

    if log:
        enriched_p = sum(1 for r in records if r.parent_area)
        enriched_d = sum(1 for r in records if r.district)
        enriched_t = sum(1 for r in records if r.tehsil)
        log(f"[HIERARCHY] Hierarchy summary: {enriched_d}/{len(records)} districts, {enriched_t}/{len(records)} tehsils, {enriched_p}/{len(records)} parent areas.", "success")
    return records


# ─────────────────────────────────────────────────────────────
#  OVERPASS API CLIENT — mirror rotation + correct failure handling (Req 11)
# ─────────────────────────────────────────────────────────────

def fetch_overpass_with_backoff(
    query: str, max_retries: int = 4, initial_delay: float = 3.0,
    log: Optional[Callable] = None,
) -> Tuple[Optional[requests.Response], str]:
    """
    Queries Overpass with mirror rotation and exponential backoff.
    Returns (response, failure_reason).
    Distinguishes timeout/HTTP error from genuine empty result. (Req 11)
    """
    delay = initial_delay
    for attempt in range(max_retries):
        server = OVERPASS_SERVERS[attempt % len(OVERPASS_SERVERS)]
        try:
            resp = requests.post(server, data={"data": query}, headers=HEADERS, timeout=120)
            if resp.status_code == 200:
                return resp, ""
            elif resp.status_code in (429, 504, 502, 503):
                if log:
                    log(f"[RETRY] attempt {attempt+1}/{max_retries} — mirror {server} returned {resp.status_code}. Waiting {delay:.1f}s...", "warn")
                time.sleep(delay)
                delay *= 1.8
            else:
                if log:
                    log(f"[FAILED] Overpass HTTP {resp.status_code} from {server}", "error")
                return None, f"HTTP_{resp.status_code}"
        except Exception as e:
            if log:
                log(f"[RETRY] attempt {attempt+1}/{max_retries} — {server} error: {e}. Waiting {delay:.1f}s...", "warn")
            time.sleep(delay)
            delay *= 1.8
    return None, "EXHAUSTED"


# ─────────────────────────────────────────────────────────────
#  ADAPTIVE QUADTREE TILING (Req 3 — configurable, DENSITY_LIMITED)
# ─────────────────────────────────────────────────────────────

@dataclass
class QuadTile:
    tile_id: str
    south: float
    west: float
    north: float
    east: float
    depth: int = 0
    parent_tile_id: str = ""

    @property
    def bbox(self) -> List[float]:
        return [self.south, self.west, self.north, self.east]

    @property
    def width_deg(self) -> float:
        return self.east - self.west

    @property
    def height_deg(self) -> float:
        return self.north - self.south

    def too_small(self) -> bool:
        return self.width_deg < MIN_TILE_SIZE_DEGREES or self.height_deg < MIN_TILE_SIZE_DEGREES

    def split_quadrants(self) -> List["QuadTile"]:
        ml = (self.south + self.north) / 2.0
        mn = (self.west + self.east) / 2.0
        d = self.depth + 1
        return [
            QuadTile(f"{self.tile_id}_NW", ml, self.west, self.north, mn, d, self.tile_id),
            QuadTile(f"{self.tile_id}_NE", ml, mn, self.north, self.east, d, self.tile_id),
            QuadTile(f"{self.tile_id}_SW", self.south, self.west, ml, mn, d, self.tile_id),
            QuadTile(f"{self.tile_id}_SE", self.south, mn, ml, self.east, d, self.tile_id),
        ]


def build_stage_a_query(tile: QuadTile, timeout_sec: int = 90) -> str:
    """
    Stage-A (Req 4, 5): Broad layered discovery query — place=*, admin boundaries,
    named landuse, named ways/relations. Does NOT filter by classification.
    Layer 1: place + admin | Layer 2: landuse residential/commercial | Layer 3: named ways/relations.
    """
    s, w, n, e = tile.south, tile.west, tile.north, tile.east
    bf = f"({s},{w},{n},{e})"
    return f"""
    [out:json][timeout:{timeout_sec}];
    (
      node["place"]{bf};
      way["place"]{bf};
      relation["place"]{bf};

      relation["boundary"="administrative"]["admin_level"~"^(5|6|7|8|9|10)$"]{bf};

      node["landuse"~"^(residential|industrial|commercial|retail)$"]["name"]{bf};
      way["landuse"~"^(residential|industrial|commercial|retail)$"]["name"]{bf};
      relation["landuse"~"^(residential|industrial|commercial|retail)$"]["name"]{bf};

      way["name"]["landuse"]{bf};
      way["name"]["place"]{bf};
      relation["name"]["landuse"]{bf};

      node["postal_code"]["name"]{bf};
      node["amenity"~"^(marketplace|bus_station)$"]["name"]{bf};
    );
    out geom qt;
    """


# ─────────────────────────────────────────────────────────────
#  SPATIALLY-SCORED POLYGON MATCHING (Req 6)
# ─────────────────────────────────────────────────────────────

def match_polygon_spatially(
    area: AreaRecord,
    candidates: List[Dict[str, Any]],
    max_distance_m: float = 5000.0,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Selects the best polygon candidate using multi-factor scoring. (Req 6)
    NEVER matches on name alone. Returns (best_geom_dict, confidence).
    Confidence: HIGH / MEDIUM / LOW / REJECTED
    """
    if not candidates:
        return None, "REJECTED"

    def normalize_name(n: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", n.lower().strip()))

    target_name = normalize_name(area.name)
    target_lat, target_lon = area.latitude, area.longitude

    best_candidate = None
    best_score = -1
    best_conf = "REJECTED"

    for cand in candidates:
        tags = cand.get("tags", {})
        cand_name = normalize_name(tags.get("name") or tags.get("name:en") or "")
        if not cand_name:
            continue

        score = 0
        conf = "REJECTED"

        # Factor 1: OSM ID match — definitive
        cand_osm_id = f"{cand.get('type', 'node')}/{cand.get('id')}"
        if area.osm_id and area.osm_id == cand_osm_id:
            score += 1000
            conf = "HIGH"

        # Factor 2: Normalized name match
        if cand_name == target_name:
            score += 50
        elif target_name in cand_name or cand_name in target_name:
            score += 25

        # Factor 3: Same state (from tags or is_in)
        cand_state = (tags.get("addr:state") or tags.get("is_in:state") or "").lower()
        if area.state and cand_state and area.state.lower() in cand_state:
            score += 30

        # Factor 4: Same district
        cand_district = (tags.get("addr:district") or tags.get("is_in:district") or "").lower()
        if area.district and cand_district and area.district.lower() in cand_district:
            score += 20

        # Factor 5: Geographic proximity
        geom_dict, geom_type, _ = extract_osm_element_geometry(cand)
        cand_lat, cand_lon = 0.0, 0.0
        if cand.get("type") == "node":
            cand_lat = float(cand.get("lat", 0))
            cand_lon = float(cand.get("lon", 0))
        elif geom_dict and geom_type in ("Polygon", "MultiPolygon"):
            ring = geom_dict["coordinates"][0] if geom_type == "Polygon" else geom_dict["coordinates"][0][0]
            cand_lat, cand_lon = calculate_polygon_centroid(ring)

        if cand_lat and cand_lon:
            dist_m = haversine_m(target_lat, target_lon, cand_lat, cand_lon)
            if dist_m > max_distance_m:
                continue  # Reject geographically distant candidates (Req 6)
            elif dist_m < 500:
                score += 40
            elif dist_m < 2000:
                score += 20
            else:
                score += 5

        # Factor 6: Polygon containment
        if geom_dict and geom_type in ("Polygon", "MultiPolygon"):
            if is_point_inside_boundary(target_lat, target_lon, geom_dict):
                score += 30

        if score > best_score:
            best_score = score
            best_candidate = cand
            if score >= 100:
                best_conf = "HIGH"
            elif score >= 60:
                best_conf = "MEDIUM"
            elif score >= 30:
                best_conf = "LOW"
            else:
                best_conf = "REJECTED"

    if best_conf == "REJECTED" or best_candidate is None:
        return None, "REJECTED"

    geom_dict, _, _ = extract_osm_element_geometry(best_candidate)
    if not geom_dict or geom_dict.get("type") not in ("Polygon", "MultiPolygon"):
        return None, "REJECTED"

    return geom_dict, best_conf


# ─────────────────────────────────────────────────────────────
#  GLOBAL DEDUPLICATION (Req 8)
# ─────────────────────────────────────────────────────────────

def deduplicate_global_results(records: List[AreaRecord]) -> Tuple[List[AreaRecord], int]:
    """
    Three-level global deduplication. (Req 8)
    Level 1: Same source+osm_type+osm_id
    Level 2: Same source+name+geometry
    Level 3: Cross-source: same state+district+name+proximity (<100m)
    NEVER deduplicates by name alone.
    """
    removed = 0
    seen_l1: Set[str] = set()
    seen_l2: Set[Tuple[str, str, int, int]] = set()
    output: List[AreaRecord] = []

    for r in records:
        # Level 1: Exact OSM identity
        l1_key = f"{r.source}|{r.osm_type}|{r.osm_id}" if r.osm_id else None
        if l1_key and l1_key in seen_l1:
            removed += 1
            continue
        if l1_key:
            seen_l1.add(l1_key)

        # Level 2: Same source + name + rounded coords
        l2_key = (r.source, r.name.lower(), int(r.latitude * 10000), int(r.longitude * 10000))
        if l2_key in seen_l2:
            removed += 1
            continue
        seen_l2.add(l2_key)

        output.append(r)

    # Level 3: Cross-source (compare against output so far)
    final: List[AreaRecord] = []
    geo_groups: Dict[Tuple[str, str, str], List[AreaRecord]] = {}
    for r in output:
        group_key = (r.name.lower(), r.state.lower(), r.district.lower())
        geo_groups.setdefault(group_key, []).append(r)

    for group in geo_groups.values():
        if len(group) == 1:
            final.extend(group)
            continue
        # Keep the one with best polygon or highest confidence; remove within 100m
        kept: List[AreaRecord] = []
        for r in sorted(group, key=lambda x: (x.polygon_status != "FOUND", x.osm_id is None)):
            duplicate = False
            for k in kept:
                if haversine_m(r.latitude, r.longitude, k.latitude, k.longitude) < 100:
                    removed += 1
                    duplicate = True
                    break
            if not duplicate:
                kept.append(r)
        final.extend(kept)

    return final, removed


# ─────────────────────────────────────────────────────────────
#  EXHAUSTIVE HARVEST PIPELINE — with resume (Req 2, 3, 9, 10, 11, 15, 18)
# ─────────────────────────────────────────────────────────────

def harvest_region_exhaustive(
    region_name: str,
    bbox: List[float],
    max_depth: int = MAX_TILE_DEPTH,
    target_types: Optional[Set[str]] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    db_conn: Optional[sqlite3.Connection] = None,
    resume: bool = True,
    state_code: str = "",
) -> Tuple[List[AreaRecord], CoverageReport]:
    """
    Exhaustive regional harvest with:
    - Real checkpoint resume (Req 2)
    - Configurable SAFE_RESULT_THRESHOLD + DENSITY_LIMITED status (Req 3)
    - Stage-A broad discovery (Req 4, 5)
    - State boundary filtering (Req 1, 9)
    - Correct failure handling (Req 11)
    - Honest coverage metrics (Req 10)
    - Structured logging (Req 18)
    """
    def log(msg: str, level: str = "info"):
        if log_callback:
            log_callback(msg, level)
        else:
            _safe_print(f"[{level.upper()}] {msg}")

    report = CoverageReport(region_name=region_name)
    conn = db_conn or init_area_harvest_db()

    # Load actual state boundary polygon (Req 1)
    norm_sc, norm_sn = normalize_state_code(state_code)
    if norm_sc:
        state_code = norm_sc
    state_boundary: Optional[Dict[str, Any]] = None
    if state_code:
        state_boundary = load_state_boundary(state_code, log=log)

    # Resume: load existing checkpoints (Req 2)
    existing_checkpoints: Dict[str, Dict[str, Any]] = {}
    if resume:
        existing_checkpoints = load_checkpoint_tiles(conn, region_name)
        completed_ids = {tid for tid, t in existing_checkpoints.items() if t["status"] == "COMPLETED"}
        report.completed_tiles = len(completed_ids)
        if completed_ids:
            log(f"[CHECKPOINT] Resuming — {len(completed_ids)} tiles already completed, skipping them.", "info")

    # Initialize tile queue — skip already COMPLETED or SPLIT tiles
    root_tile = QuadTile("T0", bbox[0], bbox[1], bbox[2], bbox[3], depth=0)
    tile_queue: List[QuadTile] = []
    if root_tile.tile_id in existing_checkpoints:
        cp = existing_checkpoints[root_tile.tile_id]
        if cp["status"] not in ("COMPLETED", "SPLIT"):
            tile_queue.append(root_tile)
        elif cp["status"] == "SPLIT":
            # Re-queue sub-tiles that aren't completed
            for tid, t in existing_checkpoints.items():
                if t.get("parent_tile_id") == root_tile.tile_id and t["status"] not in ("COMPLETED", "SPLIT"):
                    b = t["bbox"]
                    tile_queue.append(QuadTile(tid, b[0], b[1], b[2], b[3], t["depth"], root_tile.tile_id))
    else:
        tile_queue.append(root_tile)
        save_checkpoint(conn, root_tile.tile_id, region_name, root_tile.bbox, "PENDING", 0, 0)

    report.total_tiles_planned = max(1, len(tile_queue) + report.completed_tiles)

    discovered_records: List[AreaRecord] = []
    seen_osm_ids: Set[str] = set()
    seen_geo_keys: Set[Tuple[str, int, int]] = set()

    log(f"[STATE] {region_name} | Tiles queued: {len(tile_queue)} | Existing checkpoints: {len(existing_checkpoints)}", "info")

    while tile_queue:
        if stop_check and stop_check():
            log("[STOPPED] Harvest stopped by user.", "warn")
            break

        tile = tile_queue.pop(0)

        # Skip already completed tiles (Req 2)
        if tile.tile_id in existing_checkpoints and existing_checkpoints[tile.tile_id]["status"] == "COMPLETED":
            report.completed_tiles += 1
            continue

        # Tile vs state polygon intersection check (Req 1, 9)
        if state_boundary and state_boundary.get("polygon_geom"):
            if not tile_intersects_polygon(tile.south, tile.west, tile.north, tile.east, state_boundary["polygon_geom"]):
                save_checkpoint(conn, tile.tile_id, region_name, tile.bbox, "COMPLETED", 0, tile.depth, tile.parent_tile_id)
                report.completed_tiles += 1
                continue

        # Mark PROCESSING (Req 2 — crash recovery)
        save_checkpoint(conn, tile.tile_id, region_name, tile.bbox, "PROCESSING", 0, tile.depth, tile.parent_tile_id)
        report.total_queries_executed += 1

        log(f"[TILE] id={tile.tile_id} depth={tile.depth} bbox={[round(x,4) for x in tile.bbox]}", "info")

        query = build_stage_a_query(tile)
        resp, failure_reason = fetch_overpass_with_backoff(query, max_retries=3, log=log)

        if resp is None:
            # Genuine API failure — NOT zero result (Req 11)
            report.failed_queries += 1
            report.failed_query_count += 1
            if tile.depth < max_depth and not tile.too_small():
                log(f"[SPLIT] tile {tile.tile_id} failed ({failure_reason}). Subdividing into 4 quadrants...", "warn")
                quads = tile.split_quadrants()
                for q in quads:
                    tile_queue.insert(0, q)
                    save_checkpoint(conn, q.tile_id, region_name, q.bbox, "PENDING", 0, q.depth, tile.tile_id)
                report.total_tiles_subdivided += 1
                report.total_tiles_planned += 4
                save_checkpoint(conn, tile.tile_id, region_name, tile.bbox, "SPLIT", 0, tile.depth, tile.parent_tile_id)
            else:
                log(f"[FAILED] tile={tile.tile_id} — max depth/size reached, cannot subdivide. reason={failure_reason}", "error")
                save_checkpoint(conn, tile.tile_id, region_name, tile.bbox, "FAILED", 0, tile.depth, tile.parent_tile_id)
                report.failed_tiles += 1
            continue

        try:
            elements = resp.json().get("elements", [])
        except Exception as e:
            log(f"[FAILED] JSON parse error for tile {tile.tile_id}: {e}", "error")
            save_checkpoint(conn, tile.tile_id, region_name, tile.bbox, "FAILED", 0, tile.depth, tile.parent_tile_id)
            report.failed_tiles += 1
            continue

        report.successful_query_count += 1
        log(f"[QUERY] tile={tile.tile_id} returned {len(elements)} objects", "info")

        # Adaptive splitting: split if at/above SAFE_RESULT_THRESHOLD (Req 3)
        if len(elements) >= SAFE_RESULT_THRESHOLD and not tile.too_small() and tile.depth < max_depth:
            log(f"[SPLIT] tile={tile.tile_id} has {len(elements)} >= threshold {SAFE_RESULT_THRESHOLD}. Subdividing...", "info")
            quads = tile.split_quadrants()
            for q in quads:
                tile_queue.insert(0, q)
                save_checkpoint(conn, q.tile_id, region_name, q.bbox, "PENDING", 0, q.depth, tile.tile_id)
            report.total_tiles_subdivided += 1
            report.total_tiles_planned += 4
            save_checkpoint(conn, tile.tile_id, region_name, tile.bbox, "SPLIT", len(elements), tile.depth, tile.parent_tile_id)
            continue

        # DENSITY_LIMITED: still overloaded but cannot subdivide further (Req 3)
        if len(elements) >= SAFE_RESULT_THRESHOLD and (tile.too_small() or tile.depth >= max_depth):
            log(f"[WARNING] tile={tile.tile_id} reached max depth/size while still overloaded ({len(elements)} objects). Marked DENSITY_LIMITED.", "warn")
            save_checkpoint(conn, tile.tile_id, region_name, tile.bbox, "DENSITY_LIMITED", len(elements), tile.depth, tile.parent_tile_id)
            report.density_limited_tiles += 1
            report.coverage_limitations.append(f"Tile {tile.tile_id}: DENSITY_LIMITED ({len(elements)} objects)")

        report.total_raw_elements += len(elements)
        batch_records: List[AreaRecord] = []

        for el in elements:
            tags = el.get("tags", {})
            # Stage-A: extract name (multilingual). Never drop if no name:en. (Req 4, 12)
            name, name_en, name_native, vernacular = extract_multilingual_names(tags)
            if not name:
                continue

            # Stage-B: classify (Req 4)
            settle_key, settle_disp = classify_settlement(name, tags)
            if target_types and settle_key not in target_types and "named_area" not in target_types:
                continue

            el_type = el.get("type", "node")
            el_id = str(el.get("id", ""))
            osm_full_id = f"{el_type}/{el_id}"

            # Level-1 dedup by OSM ID
            if osm_full_id in seen_osm_ids:
                report.duplicates_removed += 1
                continue
            seen_osm_ids.add(osm_full_id)

            geom_dict, geom_type, pts = extract_osm_element_geometry(el)
            lat, lon = 0.0, 0.0
            if el_type == "node":
                lat, lon = float(el.get("lat", 0)), float(el.get("lon", 0))
            else:
                center = el.get("center", {})
                lat = float(center.get("lat", 0)) if center else 0.0
                lon = float(center.get("lon", 0)) if center else 0.0
                if (not lat or not lon) and geom_dict and geom_type in ("Polygon", "MultiPolygon"):
                    ring = geom_dict["coordinates"][0] if geom_type == "Polygon" else geom_dict["coordinates"][0][0]
                    lat, lon = calculate_polygon_centroid(ring)

            lat, lon = round(lat, 7), round(lon, 7)
            if not lat or not lon:
                continue

            # Geo-key dedup (Req 8 Level-2)
            geo_key = (name.lower(), int(lat * 1000), int(lon * 1000))
            if geo_key in seen_geo_keys:
                report.duplicates_removed += 1
                continue
            seen_geo_keys.add(geo_key)

            # State leakage check (Req 1, 9)
            membership = verify_object_state(lat, lon, state_code, state_boundary)
            if membership == "OUTSIDE_STATE" and state_boundary and state_boundary.get("verified"):
                report.outside_state_count += 1
                continue  # Reject confirmed out-of-state object from target state scan
            elif membership == "IN_STATE":
                report.in_state_count += 1
                report.state_boundary_verified_count += 1
            elif membership == "CROSS_BOUNDARY":
                report.cross_boundary_count += 1
            else:
                report.unverified_state_count += 1
                report.state_boundary_uncertain_count += 1

            report.objects_after_state_validation += 1
            report.objects_after_classification += 1

            poly_status = "FOUND" if geom_type in ("Polygon", "MultiPolygon") else "NOT_FOUND"
            poly_conf = "HIGH" if poly_status == "FOUND" else "NONE"
            if poly_status == "FOUND":
                report.polygons_found += 1
            else:
                report.polygon_missing_count += 1
                report.points_found += 1

            admin = resolve_administrative_hierarchy(tags, state_hint=state_code or region_name, lat=lat, lon=lon, name=name)
            if admin.get("district") or admin.get("parent_area"):
                report.hierarchy_resolved += 1
            else:
                report.hierarchy_unknown += 1

            # Do not label an object as Delhi merely because it was discovered during a Delhi/NCR scan (Req 1)
            final_state = admin["state"] or region_name
            if membership == "OUTSIDE_STATE" and (not admin["state"] or admin["state"].lower() == region_name.lower()):
                final_state = "Neighboring State"

            rec = AreaRecord(
                osm_id=osm_full_id, osm_type=el_type,
                name=name, name_en=name_en, name_native=name_native, vernacular_names=vernacular,
                settlement_type=settle_key, display_type=settle_disp,
                parent_area=admin["parent_area"], country=admin["country"],
                state=final_state, state_code=admin["state_code"] or state_code,
                district=admin["district"], subdistrict=admin["subdistrict"],
                tehsil=admin["tehsil"], city=admin["city"],
                postal_code=tags.get("postal_code", "") or tags.get("addr:postcode", ""),
                latitude=lat, longitude=lon,
                discovery_status="FOUND",
                polygon_status=poly_status, polygon_match_confidence=poly_conf,
                geometry_type=geom_type, polygon_points=pts,
                geometry=geom_dict,
                geojson=json.dumps(geom_dict) if geom_dict else "",
                tile_id=tile.tile_id, source="OpenStreetMap",
                maps_url=f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
                state_membership=membership,
                hierarchy_confidence=admin["hierarchy_confidence"],
            )
            batch_records.append(rec)
            discovered_records.append(rec)

            st_key = rec.state or "Other"
            report.state_breakdown[st_key] = report.state_breakdown.get(st_key, 0) + 1
            report.settlement_type_breakdown[settle_key] = report.settlement_type_breakdown.get(settle_key, 0) + 1

        # Enrich parent areas via Nominatim (capped per tile)
        if batch_records:
            batch_records = list(enrich_parent_areas(batch_records, log=log, max_enrich=min(80, len(batch_records))))

        log(f"[CHECKPOINT] tile={tile.tile_id} saved | discovered={len(batch_records)}", "info")
        save_area_records_batch(conn, batch_records)
        report.completed_tiles += 1
        save_checkpoint(conn, tile.tile_id, region_name, tile.bbox, "COMPLETED", len(batch_records), tile.depth, tile.parent_tile_id)

    report.unique_named_areas = len(discovered_records)
    rep_dict = report.finish()

    try:
        with open("coverage_report.json", "w", encoding="utf-8") as f:
            json.dump(rep_dict, f, indent=2, ensure_ascii=False)
        log(f"[COVERAGE] Report saved. Tile coverage: {report.geographic_tile_coverage_pct}% | Locality completeness: UNKNOWN (source-dependent)", "success")
    except Exception as e:
        log(f"Warning: Failed to write coverage_report.json: {e}", "warn")

    return discovered_records, report


# ─────────────────────────────────────────────────────────────
#  CITY BOUNDARY RESOLUTION
# ─────────────────────────────────────────────────────────────

def fetch_city_boundary(city: str, country_code: str = DEFAULT_COUNTRY_CODE,
                         state_code: str = "",
                         log_callback: Optional[Callable] = None) -> Dict[str, Any]:
    """Resolves city boundary polygon and bounding box via Nominatim."""
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)

    clean_city = city.strip()
    if clean_city.lower() in ("delhi", "nct of delhi", "new delhi"):
        clean_city = "National Capital Territory of Delhi"
        country_code = "in"

    params: Dict[str, Any] = {
        "q": clean_city, "format": "json",
        "polygon_geojson": 1, "limit": 5, "addressdetails": 1,
    }
    if country_code:
        params["countrycodes"] = country_code

    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        items = resp.json()
        if not items:
            return {"bbox": None, "area_id": None, "polygon_geom": None, "display": clean_city}

        best = items[0]
        for it in items:
            if it.get("osm_type") == "relation":
                best = it
                break

        bbox_raw = [float(x) for x in best.get("boundingbox", [0, 0, 0, 0])]
        bbox = [bbox_raw[0], bbox_raw[2], bbox_raw[1], bbox_raw[3]]
        osm_id = best.get("osm_id")
        area_id = int(osm_id) + 3600000000 if best.get("osm_type") == "relation" else None

        addr = best.get("address", {})
        raw_sc = addr.get("ISO3166-2-lvl4") or state_code
        canonical_sc, canonical_sn = normalize_state_code(raw_sc)
        if not canonical_sc and addr.get("state"):
            canonical_sc, canonical_sn = normalize_state_code(addr["state"])

        return {
            "bbox": bbox, "area_id": area_id,
            "polygon_geom": best.get("geojson"),
            "display": best.get("display_name", clean_city),
            "state": canonical_sn or addr.get("state", ""),
            "state_code": canonical_sc,
            "district": addr.get("state_district") or addr.get("district") or "",
        }
    except Exception as e:
        log(f"⚠️ Error resolving boundary for '{city}': {e}", "warn")
        return {"bbox": None, "area_id": None, "polygon_geom": None, "display": clean_city, "state": "", "state_code": "", "district": ""}


# ─────────────────────────────────────────────────────────────
#  STAGE-2 POLYGON RESOLVER (Req 6 — spatially scored)
# ─────────────────────────────────────────────────────────────

def resolve_polygons_stage2(areas: List[Dict[str, Any]],
                             log: Optional[Callable] = None) -> List[Dict[str, Any]]:
    """Resolves polygon boundaries using spatially-scored matching. (Req 6)"""
    unresolved = [a for a in areas if a.get("polygon_status") != "FOUND" and a.get("name")]
    if not unresolved:
        return areas

    batch_size = 20
    for i in range(0, len(unresolved), batch_size):
        chunk = unresolved[i:i + batch_size]
        escaped = "|".join(re.escape(str(a["name"]).strip()) for a in chunk)
        q = f"""
        [out:json][timeout:60];
        (
          way["name"~"^({escaped})$",i];
          relation["name"~"^({escaped})$",i];
        );
        out geom qt;
        """
        try:
            resp, _ = fetch_overpass_with_backoff(q, max_retries=2, log=log)
            if not resp:
                continue
            candidates = resp.json().get("elements", [])
            for a in chunk:
                ar = AreaRecord(
                    name=a.get("name", ""),
                    latitude=float(a.get("latitude", 0)),
                    longitude=float(a.get("longitude", 0)),
                    state=a.get("state", ""),
                    district=a.get("district", ""),
                    osm_id=a.get("osm_id"),
                )
                best_geom, conf = match_polygon_spatially(ar, candidates)
                if best_geom and conf != "REJECTED":
                    a["geometry"] = best_geom
                    a["geometry_type"] = best_geom.get("type", "Polygon")
                    a["polygon_status"] = "FOUND"
                    a["polygon_match_confidence"] = conf
                    a["geojson"] = json.dumps(best_geom)
        except Exception:
            pass

    for a in areas:
        if a.get("polygon_status") != "FOUND":
            a["polygon_status"] = "NOT_FOUND"
            a.setdefault("polygon_match_confidence", "REJECTED")
    return areas


# ─────────────────────────────────────────────────────────────
#  PRIMARY PUBLIC INTERFACE (Req 14 — backward compatible)
# ─────────────────────────────────────────────────────────────

def fetch_areas(
    city: str,
    area_type: Any = "all",
    limit: Optional[int] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    source: str = "hybrid",
    headless: bool = True,
    state_code: str = "",
    strict_boundary: bool = True,
    location_type: str = "City",
    stop_check: Optional[Callable[[], bool]] = None,
    resume: bool = True,
) -> List[Dict[str, Any]]:
    """
    Main entry point. Dispatches to:
    - Nationwide mode (India / Bharat / All India)
    - State mode (any state name or code)
    - City / locality mode (single city with boundary)
    Preserves all existing GUI parameters. (Req 14)
    """
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            _safe_print(f"[{level.upper()}] {msg}")

    # Normalize area type filter
    target_types: Set[str] = set()
    if isinstance(area_type, (list, set, tuple)):
        for t in area_type:
            cleaned = str(t).lower().replace(" ", "_")
            target_types.add(AREA_TYPE_KEY_MAP.get(cleaned, cleaned))
    elif isinstance(area_type, str) and area_type.lower() not in ("all", "all categories"):
        cleaned = area_type.lower().replace(" ", "_")
        target_types.add(AREA_TYPE_KEY_MAP.get(cleaned, cleaned))
    is_all_types = not target_types

    city_clean = city.strip().lower()

    # Nationwide mode (Req 15)
    if city_clean in ("india", "whole india", "all india", "bharat"):
        log("🇮🇳 [STATE] Initiating Nationwide Harvest across all 36 States and UTs...", "info")
        all_results: List[AreaRecord] = []
        for sc, info in INDIA_STATES_REGISTRY.items():
            if stop_check and stop_check():
                break
            log(f"[STATE] {info['name']} ({sc})", "info")
            records, _ = harvest_region_exhaustive(
                region_name=info["name"], bbox=info["bbox"], max_depth=3,
                target_types=None if is_all_types else target_types,
                log_callback=log, stop_check=stop_check, state_code=sc, resume=resume,
            )
            all_results.extend(records)

        final, removed = deduplicate_global_results(all_results)
        log(f"[DEDUP] removed {removed} global duplicates. Final: {len(final)} unique areas.", "info")
        return [r.to_dict() for r in final]

    # State mode
    matched_sc: Optional[str] = None
    for sc, info in INDIA_STATES_REGISTRY.items():
        if (city_clean == info["name"].lower() or
                city_clean == f"{info['name'].lower()} state" or
                city_clean == sc.lower()):
            matched_sc = sc
            break

    if matched_sc:
        info = INDIA_STATES_REGISTRY[matched_sc]
        log(f"[STATE] {info['name']} ({matched_sc}) — Exhaustive Quadtree Harvest", "info")
        records, _ = harvest_region_exhaustive(
            region_name=info["name"], bbox=info["bbox"], max_depth=4,
            target_types=None if is_all_types else target_types,
            log_callback=log, stop_check=stop_check, state_code=matched_sc, resume=resume,
        )
        return [r.to_dict() for r in records]

    # City / locality mode
    boundary = fetch_city_boundary(city, state_code=state_code, log_callback=log)
    if not boundary.get("bbox"):
        log(f"❌ Could not resolve boundary for '{city}'.", "error")
        return []

    # Exhaustive mode for city when limit is large/unset
    if limit is None or limit > 500:
        effective_state_code = state_code or boundary.get("state_code", "")
        log(f"🔍 Performing exhaustive regional harvest for '{city}'...", "info")
        records, _ = harvest_region_exhaustive(
            region_name=boundary.get("display", city), bbox=boundary["bbox"],
            max_depth=3, target_types=None if is_all_types else target_types,
            log_callback=log, stop_check=stop_check, state_code=effective_state_code, resume=resume,
        )
        res_dicts = [r.to_dict() for r in records]
        if strict_boundary and boundary.get("polygon_geom"):
            res_dicts = [r for r in res_dicts if is_point_inside_boundary(r["latitude"], r["longitude"], boundary["polygon_geom"])]
        return res_dicts

    # Single-pass bounded query for small limits
    s, w, n, e = boundary["bbox"]
    bf = f"({s},{w},{n},{e})"
    query = f"""
    [out:json][timeout:60];
    (
      node["place"]{bf}; way["place"]{bf}; relation["place"]{bf};
      node["landuse"~"^(residential|industrial|commercial)$"]["name"]{bf};
      way["landuse"~"^(residential|industrial|commercial)$"]["name"]{bf};
      relation["boundary"="administrative"]["admin_level"~"^(8|9|10)$"]{bf};
    );
    out geom qt {limit};
    """
    log(f"🔍 Searching up to {limit} areas in {city}...", "info")
    resp, fail_reason = fetch_overpass_with_backoff(query, max_retries=3, log=log)
    if not resp:
        log(f"❌ Query failed ({fail_reason}) for '{city}'.", "error")
        return []

    elements = resp.json().get("elements", [])
    results: List[AreaRecord] = []
    seen: Set[Tuple] = set()

    for el in elements:
        tags = el.get("tags", {})
        name, name_en, name_native, vernacular = extract_multilingual_names(tags)
        if not name:
            continue
        settle_key, settle_disp = classify_settlement(name, tags)
        if not is_all_types and settle_key not in target_types and "named_area" not in target_types:
            continue

        geom_dict, geom_type, pts = extract_osm_element_geometry(el)
        el_type = el.get("type", "node")
        lat = float(el.get("lat", 0)) if el_type == "node" else float((el.get("center") or {}).get("lat", 0))
        lon = float(el.get("lon", 0)) if el_type == "node" else float((el.get("center") or {}).get("lon", 0))
        if (not lat or not lon) and geom_dict and geom_type in ("Polygon", "MultiPolygon"):
            ring = geom_dict["coordinates"][0] if geom_type == "Polygon" else geom_dict["coordinates"][0][0]
            lat, lon = calculate_polygon_centroid(ring)
        lat, lon = round(lat, 7), round(lon, 7)
        if not lat or not lon:
            continue

        if strict_boundary and boundary.get("polygon_geom"):
            if not is_point_inside_boundary(lat, lon, boundary["polygon_geom"]):
                continue

        dup_key = (name.lower(), round(lat, 4), round(lon, 4))
        if dup_key in seen:
            continue
        seen.add(dup_key)

        poly_status = "FOUND" if geom_type in ("Polygon", "MultiPolygon") else "NOT_FOUND"
        poly_conf = "HIGH" if poly_status == "FOUND" else "NONE"
        admin = resolve_administrative_hierarchy(tags, state_hint=state_code, lat=lat, lon=lon, name=name)

        rec = AreaRecord(
            osm_id=f"{el_type}/{el.get('id')}", osm_type=el_type,
            name=name, name_en=name_en, name_native=name_native, vernacular_names=vernacular,
            settlement_type=settle_key, display_type=settle_disp,
            parent_area=admin["parent_area"], country=admin["country"],
            state=admin["state"] or state_code, state_code=admin["state_code"],
            district=admin["district"], subdistrict=admin["subdistrict"],
            tehsil=admin["tehsil"], city=admin["city"] or city,
            latitude=lat, longitude=lon, discovery_status="FOUND",
            polygon_status=poly_status, polygon_match_confidence=poly_conf,
            geometry_type=geom_type, polygon_points=pts, geometry=geom_dict,
            geojson=json.dumps(geom_dict) if geom_dict else "",
            source="OpenStreetMap",
            maps_url=f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
            hierarchy_confidence=admin["hierarchy_confidence"],
        )
        results.append(rec)

    if results:
        results = list(enrich_parent_areas(results, log=log, max_enrich=min(200, len(results))))
        log(f"✅ Discovered {len(results)} areas in {city}.", "success")

    return [r.to_dict() for r in results]


# ─────────────────────────────────────────────────────────────
#  CALCULATE COVERAGE METRICS (Req 10)
# ─────────────────────────────────────────────────────────────

def calculate_coverage_metrics(report: CoverageReport) -> Dict[str, Any]:
    """Returns honest coverage metrics. Never claims locality_completeness = guaranteed %. (Req 10)"""
    return {
        "geographic_tile_coverage_pct": report.geographic_tile_coverage_pct,
        "locality_completeness": "UNKNOWN",
        "completeness_note": report.completeness_note,
        "completed_tiles": report.completed_tiles,
        "failed_tiles": report.failed_tiles,
        "density_limited_tiles": report.density_limited_tiles,
        "total_tiles_planned": report.total_tiles_planned,
        "objects_discovered": report.total_raw_elements,
        "unique_objects": report.unique_named_areas,
        "polygon_found_count": report.polygons_found,
        "polygon_missing_count": report.polygon_missing_count,
        "state_boundary_verified_count": report.state_boundary_verified_count,
        "state_boundary_uncertain_count": report.state_boundary_uncertain_count,
        "successful_query_count": report.successful_query_count,
        "failed_query_count": report.failed_query_count,
        "duplicates_removed": report.duplicates_removed,
    }


def generate_validation_summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Produces complete audit validation summary before/after export. (Req 8, 10 in test.txt)
    Tracks unique IDs, state memberships, missing hierarchy, self-parents, polygon statuses.
    """
    total = len(records)
    osm_ids = [r.get("osm_id", "") for r in records if r.get("osm_id")]
    unique_osm = len(set(osm_ids))
    dup_osm = total - unique_osm

    states: Dict[str, int] = {}
    memberships: Dict[str, int] = {}
    polygon_statuses: Dict[str, int] = {}
    polygon_confs: Dict[str, int] = {}
    hierarchy_confs: Dict[str, int] = {}
    sources: Dict[str, int] = {}
    discovery_statuses: Dict[str, int] = {}

    missing_district = 0
    missing_tehsil = 0
    missing_parent = 0
    self_parent_count = 0

    for r in records:
        st = r.get("state") or "BLANK"
        states[st] = states.get(st, 0) + 1

        mem = r.get("state_membership") or "BLANK"
        memberships[mem] = memberships.get(mem, 0) + 1

        ps = r.get("polygon_status") or "BLANK"
        polygon_statuses[ps] = polygon_statuses.get(ps, 0) + 1

        pc = r.get("polygon_match_confidence") or "BLANK"
        polygon_confs[pc] = polygon_confs.get(pc, 0) + 1

        hc = r.get("hierarchy_confidence") or "BLANK"
        hierarchy_confs[hc] = hierarchy_confs.get(hc, 0) + 1

        src = r.get("source") or "BLANK"
        sources[src] = sources.get(src, 0) + 1

        ds = r.get("discovery_status") or "BLANK"
        discovery_statuses[ds] = discovery_statuses.get(ds, 0) + 1

        if not r.get("district"):
            missing_district += 1
        if not r.get("tehsil"):
            missing_tehsil += 1

        pa = (r.get("parent_area") or "").strip()
        name = (r.get("name") or "").strip()
        if not pa:
            missing_parent += 1
        elif pa.lower() == name.lower():
            self_parent_count += 1

    summary = {
        "Total records": total,
        "Unique OSM IDs": unique_osm,
        "Duplicate OSM IDs": dup_osm,
        "States represented": states,
        "UNVERIFIED state memberships": memberships.get("UNVERIFIED", 0),
        "OUTSIDE_STATE records": memberships.get("OUTSIDE_STATE", 0),
        "CROSS_BOUNDARY records": memberships.get("CROSS_BOUNDARY", 0),
        "IN_STATE records": memberships.get("IN_STATE", 0),
        "Missing District": missing_district,
        "Missing Tehsil/Taluk": missing_tehsil,
        "Missing Parent Area": missing_parent,
        "Self-parent records": self_parent_count,
        "Polygon FOUND": polygon_statuses.get("FOUND", 0),
        "Polygon NOT_FOUND": polygon_statuses.get("NOT_FOUND", 0),
        "Polygon confidence distribution": polygon_confs,
        "Hierarchy confidence distribution": hierarchy_confs,
        "Discovery status distribution": discovery_statuses,
        "Source distribution": sources,
    }
    return summary


# ─────────────────────────────────────────────────────────────
#  EXPORTERS (Req 14 — preserved)
# ─────────────────────────────────────────────────────────────

def save_area_to_excel(areas: List[Dict[str, Any]], filepath: str):
    """Exports discovered areas to Excel spreadsheet with validation summary."""
    import pandas as pd
    summary = generate_validation_summary(areas)
    _safe_print("\n" + "=" * 60)
    _safe_print("           EXPORT VALIDATION SUMMARY (test.txt)")
    _safe_print("=" * 60)
    for k, v in summary.items():
        _safe_print(f"  {k:<32}: {v}")
    _safe_print("=" * 60 + "\n")

    rows = [{
        "OSM ID": a.get("osm_id", ""),
        "Area Name": a.get("name", ""),
        "English Name": a.get("name_en", ""),
        "Native Name": a.get("name_native", ""),
        "Settlement Type": a.get("display_type", a.get("settlement_type", "")),
        "Parent Area": a.get("parent_area", ""),
        "District": a.get("district", ""),
        "Tehsil / Taluk": a.get("tehsil", ""),
        "State": a.get("state", ""),
        "State Code": a.get("state_code", ""),
        "Country": a.get("country", "India"),
        "Postal Code": a.get("postal_code", ""),
        "Discovery Status": a.get("discovery_status", "FOUND"),
        "Polygon Status": a.get("polygon_status", "NOT_FOUND"),
        "Polygon Confidence": a.get("polygon_match_confidence", ""),
        "State Membership": a.get("state_membership", ""),
        "Hierarchy Confidence": a.get("hierarchy_confidence", ""),
        "Geometry Type": a.get("geometry_type", "Point"),
        "Polygon Points": a.get("polygon_points", 0),
        "Latitude": a.get("latitude", ""),
        "Longitude": a.get("longitude", ""),
        "Google Maps URL": a.get("maps_url", ""),
        "Source": a.get("source", "OpenStreetMap"),
    } for a in areas]
    pd.DataFrame(rows).to_excel(filepath, index=False)


def save_area_to_kml(areas: List[Dict[str, Any]], filepath: str):
    """Exports discovered areas to KML format."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
             '  <name>Discovered Areas - Jarvis Maps AI</name>']
    for a in areas:
        name = a.get("name", "Unnamed")
        lat, lon = a.get("latitude", 0), a.get("longitude", 0)
        stype = a.get("display_type", "")
        geom = a.get("geometry")
        gtype = a.get("geometry_type", "Point")
        lines.append('  <Placemark>')
        lines.append(f'    <name><![CDATA[{name}]]></name>')
        lines.append(f'    <description><![CDATA[Type: {stype}<br>State: {a.get("state", "")}<br>Parent: {a.get("parent_area", "")}]]></description>')
        if geom and gtype == "Polygon" and geom.get("coordinates"):
            ring = geom["coordinates"][0]
            coords_str = " ".join(f"{p[0]},{p[1]},0" for p in ring)
            lines.append(f'    <Polygon><outerBoundaryIs><LinearRing><coordinates>{coords_str}</coordinates></LinearRing></outerBoundaryIs></Polygon>')
        else:
            lines.append(f'    <Point><coordinates>{lon},{lat},0</coordinates></Point>')
        lines.append('  </Placemark>')
    lines.extend(['</Document></kml>'])
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def save_area_to_geojson(areas: List[Dict[str, Any]], filepath: str):
    """Exports discovered areas to GeoJSON FeatureCollection."""
    features = []
    for a in areas:
        geom = a.get("geometry")
        if not geom:
            lat, lon = a.get("latitude"), a.get("longitude")
            if lat is not None and lon is not None:
                geom = {"type": "Point", "coordinates": [lon, lat]}
        if not geom:
            continue
        props = {k: v for k, v in a.items() if k not in ("geometry", "geojson")}
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, indent=2, ensure_ascii=False)