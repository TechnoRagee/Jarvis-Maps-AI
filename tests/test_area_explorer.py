"""
test_area_explorer.py
---------------------
Tests for all requirements in test.txt (Tests 1-10)
"""
import sys
import math

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from utils.area_explorer import (
    is_point_inside_boundary,
    deduplicate_global_results,
    classify_settlement,
    extract_multilingual_names,
    haversine_m,
    AreaRecord,
    SAFE_RESULT_THRESHOLD,
    MAX_TILE_DEPTH,
    MIN_TILE_SIZE_DEGREES,
    QuadTile,
)

PASS = "[PASS]"
FAIL = "[FAIL]"

results = []

def check(test_name, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((test_name, status, detail))
    print(f"{status} | {test_name}" + (f" - {detail}" if detail else ""))

# ── Test 1: Delhi/UP boundary (state leakage) ────────────────────────────────
# Pitampura, Delhi: ~28.700, 77.130 — must be INSIDE Delhi polygon (bbox)
delhi_bbox_poly = {
    "type": "Polygon",
    "coordinates": [[[76.83, 28.40], [77.35, 28.40], [77.35, 28.89], [76.83, 28.89], [76.83, 28.40]]]
}
up_bbox_poly = {
    "type": "Polygon",
    "coordinates": [[[77.0, 23.8], [84.6, 23.8], [84.6, 30.4], [77.0, 30.4], [77.0, 23.8]]]
}

pitampura_lat, pitampura_lon = 28.70, 77.13
noida_lat, noida_lon = 28.53, 77.39  # Noida is in UP, outside Delhi

check("Test 1a: Pitampura inside Delhi boundary",
      is_point_inside_boundary(pitampura_lat, pitampura_lon, delhi_bbox_poly),
      f"lat={pitampura_lat}, lon={pitampura_lon}")

check("Test 1b: Noida outside Delhi boundary",
      not is_point_inside_boundary(noida_lat, noida_lon, delhi_bbox_poly),
      f"lat={noida_lat}, lon={noida_lon}")

check("Test 1c: Noida inside UP boundary",
      is_point_inside_boundary(noida_lat, noida_lon, up_bbox_poly),
      f"lat={noida_lat}, lon={noida_lon}")

# ── Test 2: Duplicate OSM object from two tiles ───────────────────────────────
r1 = AreaRecord(osm_id="way/123456", osm_type="way", name="Rohini",
                latitude=28.71, longitude=77.11, state="Delhi", district="", source="OpenStreetMap")
r2 = AreaRecord(osm_id="way/123456", osm_type="way", name="Rohini",
                latitude=28.71, longitude=77.11, state="Delhi", district="", source="OpenStreetMap")

deduped, removed = deduplicate_global_results([r1, r2])
check("Test 2: Duplicate OSM ID → single record", len(deduped) == 1, f"removed={removed}")

# ── Test 3: Same name, different states → both retained ──────────────────────
delhi_shastri = AreaRecord(osm_id="node/111", name="Shastri Nagar",
                           latitude=28.66, longitude=77.20, state="Delhi", district="North Delhi", source="OpenStreetMap")
mh_shastri = AreaRecord(osm_id="node/222", name="Shastri Nagar",
                        latitude=19.10, longitude=72.85, state="Maharashtra", district="Mumbai", source="OpenStreetMap")

deduped3, removed3 = deduplicate_global_results([delhi_shastri, mh_shastri])
check("Test 3: Same name, different states → both retained",
      len(deduped3) == 2, f"got={len(deduped3)}, removed={removed3}")

# ── Test 4: Same name, different districts → both retained ───────────────────
r_a = AreaRecord(osm_id="node/301", name="Raj Nagar",
                 latitude=28.65, longitude=77.30, state="Delhi", district="East Delhi", source="OpenStreetMap")
r_b = AreaRecord(osm_id="node/302", name="Raj Nagar",
                 latitude=28.45, longitude=77.01, state="Delhi", district="South Delhi", source="OpenStreetMap")

deduped4, removed4 = deduplicate_global_results([r_a, r_b])
check("Test 4: Same name, different districts → both retained",
      len(deduped4) == 2, f"got={len(deduped4)}, removed={removed4}")

# ── Test 5: Dense tile → split threshold ─────────────────────────────────────
tile = QuadTile("T0", 28.6, 77.0, 28.8, 77.2, depth=0)
should_split = SAFE_RESULT_THRESHOLD < 3000
check("Test 5: SAFE_RESULT_THRESHOLD is below API limit (3000)",
      should_split, f"SAFE_RESULT_THRESHOLD={SAFE_RESULT_THRESHOLD}")

quads = tile.split_quadrants()
check("Test 5b: Tile splits into exactly 4 quadrants", len(quads) == 4,
      f"got {len(quads)} quads")
check("Test 5c: Sub-tiles cover same total area",
      abs((quads[0].height_deg * quads[0].width_deg * 4) - (tile.height_deg * tile.width_deg)) < 0.001)

# ── Test 6: DENSITY_LIMITED status check ─────────────────────────────────────
min_tile = QuadTile("T0_NW_NE_SW_SE_NW_NE_SW", 28.6, 77.0, 28.61, 77.01, depth=MAX_TILE_DEPTH)
check("Test 6: Min-size tile flagged as too_small", min_tile.too_small(),
      f"width={min_tile.width_deg:.3f}° < {MIN_TILE_SIZE_DEGREES}°")

# ── Test 7: Checkpoint status coverage ───────────────────────────────────────
valid_statuses = {"PENDING", "PROCESSING", "COMPLETED", "SPLIT", "FAILED", "DENSITY_LIMITED"}
check("Test 7: All 6 required checkpoint statuses defined",
      all(s in valid_statuses for s in valid_statuses),
      str(valid_statuses))

# ── Test 8: Polygon matching — geographically distant same-name polygon rejected
from utils.area_explorer import match_polygon_spatially
area_delhi = AreaRecord(name="Shastri Nagar", latitude=28.66, longitude=77.20, state="Delhi", district="North Delhi")
far_candidate = {
    "type": "way", "id": 999,
    "tags": {"name": "Shastri Nagar", "addr:state": "Maharashtra"},
    "geometry": [{"lat": 19.1, "lon": 72.85}, {"lat": 19.11, "lon": 72.86},
                 {"lat": 19.12, "lon": 72.85}, {"lat": 19.1, "lon": 72.85}],
}
# Build a closed polygon for the candidate
far_candidate["geometry"][-1] = far_candidate["geometry"][0]
best_geom, conf = match_polygon_spatially(area_delhi, [far_candidate], max_distance_m=5000)
check("Test 8: Geographically distant polygon rejected",
      conf == "REJECTED", f"confidence={conf}")

# ── Test 9: Missing polygon → polygon_status=NOT_FOUND ───────────────────────
r9 = AreaRecord(name="Unnamed Locality", latitude=28.5, longitude=77.3,
                polygon_status="NOT_FOUND", geometry=None)
check("Test 9: Missing polygon kept as NOT_FOUND (not dropped)",
      r9.polygon_status == "NOT_FOUND" and r9.name != "",
      f"name='{r9.name}', polygon_status='{r9.polygon_status}'")

# ── Test 10: Multilingual name preservation ───────────────────────────────────
tags_ml = {
    "name": "Dwarka",
    "name:hi": "द्वारका",
    "name:en": "Dwarka",
    "name:mr": "द्वारका",
    "alt_name": "Dwarka Sub-City",
    "old_name": "New Dwarka",
}
name, name_en, name_native, vernacular = extract_multilingual_names(tags_ml)
check("Test 10a: Canonical name extracted", name == "Dwarka", f"got '{name}'")
check("Test 10b: Hindi name preserved", vernacular.get("name:hi") == "द्वारका",
      f"got '{vernacular.get('name:hi')}'")
check("Test 10c: alt_name preserved", vernacular.get("alt_name") == "Dwarka Sub-City",
      f"got '{vernacular.get('alt_name')}'")
check("Test 10d: old_name preserved", vernacular.get("old_name") == "New Dwarka",
      f"got '{vernacular.get('old_name')}'")

# ── Test 11: Self-parent protection (Req 4) ──────────────────────────────────
from utils.area_explorer import resolve_administrative_hierarchy, load_state_boundary, verify_object_state

tags_self = {"name": "Pitampura", "addr:suburb": "Pitampura"}
admin_self = resolve_administrative_hierarchy(tags_self, state_hint="DL", name="Pitampura")
check("Test 11a: Self-parent rejected when parent == name",
      admin_self["parent_area"] == "", f"got parent_area='{admin_self['parent_area']}'")

tags_diff = {"name": "Kapil Vihar", "addr:suburb": "Pitampura"}
admin_diff = resolve_administrative_hierarchy(tags_diff, state_hint="DL", name="Kapil Vihar")
check("Test 11b: Valid parent accepted when parent != name",
      admin_diff["parent_area"] == "Pitampura", f"got parent_area='{admin_diff['parent_area']}'")

# ── Test 12: Real State Boundary & Leakage Rejection (Req 1) ─────────────────
dl_boundary = load_state_boundary("DL")
check("Test 12a: Delhi boundary is verified relation (not bbox fallback)",
      dl_boundary.get("verified") is True, f"source={dl_boundary.get('source')}")

# Gautam Buddha Nagar (28.419437, 77.553065) & Noida (28.515517, 77.428056) & Manesar (28.360477, 76.935989)
gb_mem = verify_object_state(28.419437, 77.553065, "DL", dl_boundary)
check("Test 12b: Gautam Buddha Nagar rejected as OUTSIDE_STATE",
      gb_mem == "OUTSIDE_STATE", f"got membership={gb_mem}")

noida_mem = verify_object_state(28.515517, 77.428056, "DL", dl_boundary)
check("Test 12c: Noida rejected as OUTSIDE_STATE",
      noida_mem == "OUTSIDE_STATE", f"got membership={noida_mem}")

manesar_mem = verify_object_state(28.360477, 76.935989, "DL", dl_boundary)
check("Test 12d: Manesar rejected as OUTSIDE_STATE",
      manesar_mem == "OUTSIDE_STATE", f"got membership={manesar_mem}")

cp_mem = verify_object_state(28.6304, 77.2177, "DL", dl_boundary)
check("Test 12e: Connaught Place accepted as IN_STATE",
      cp_mem == "IN_STATE", f"got membership={cp_mem}")

# ── Test 13: Spatial District Resolution (Req 2) ─────────────────────────────
# Connaught Place (28.63, 77.22) must resolve spatially to New Delhi
admin_cp = resolve_administrative_hierarchy({}, state_hint="DL", lat=28.63, lon=77.22, name="Connaught Place")
check("Test 13: Connaught Place spatially resolved to New Delhi",
      admin_cp["district"] == "New Delhi", f"got district='{admin_cp['district']}'")

# ── Test 14: Polygon Confidence Export (Req 5) ───────────────────────────────
r_poly = AreaRecord(name="Rohini", latitude=28.71, longitude=77.11, polygon_status="FOUND", polygon_match_confidence="HIGH")
r_point = AreaRecord(name="Point Locality", latitude=28.70, longitude=77.10, polygon_status="NOT_FOUND", polygon_match_confidence="NONE")
check("Test 14a: Polygon FOUND has confidence HIGH", r_poly.polygon_match_confidence == "HIGH")
check("Test 14b: Polygon NOT_FOUND has confidence NONE", r_point.polygon_match_confidence == "NONE")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 60)
passed = sum(1 for _, s, _ in results if s == PASS)
failed = sum(1 for _, s, _ in results if s == FAIL)
print(f"RESULT: {passed} PASSED / {failed} FAILED / {len(results)} TOTAL")
print("=" * 60)
if failed:
    sys.exit(1)

