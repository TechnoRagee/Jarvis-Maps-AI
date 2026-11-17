"""
verify_harvest_benchmark.py
---------------------------
Test script to run sample benchmarks across urban, rural, and regional test cases:
Delhi, Mumbai, Bengaluru, Jaipur, rural Uttar Pradesh, rural Rajasthan, Northeast India.
"""

import sys
import time
from utils.area_explorer import harvest_region_exhaustive, fetch_areas, INDIA_STATES_REGISTRY

test_cases = [
    # Small test bboxes or regions to verify speed and accuracy without hitting rate limits
    ("Delhi (North-West / Pitampura)", [28.67, 77.10, 28.73, 77.16]),
    ("Jaipur (Walled City & Surroundings)", [26.90, 75.80, 26.94, 75.84]),
    ("Rural Uttar Pradesh (Aligarh District Cluster)", [27.85, 78.02, 27.92, 78.10]),
    ("Rural Rajasthan (Churu / Shekhawati Cluster)", [28.25, 74.90, 28.32, 74.98]),
    ("Northeast India (Guwahati Metro)", [26.12, 91.70, 26.18, 91.78]),
]

print("=" * 60)
print("STARTING EXHAUSTIVE HARVEST BENCHMARK TEST (test.txt)")
print("=" * 60)

for name, bbox in test_cases:
    print(f"\n--- Testing: {name} ---")
    start = time.time()
    records, report = harvest_region_exhaustive(
        region_name=name,
        bbox=bbox,
        max_depth=2,
    )
    elapsed = time.time() - start

    print(f"Results for: {report.region_name}")
    print(f"  - Queries Executed       : {report.total_queries_executed}")
    print(f"  - Objects Discovered     : {report.total_raw_elements}")
    print(f"  - Unique Named Areas     : {report.unique_named_areas}")
    print(f"  - Polygons Found         : {report.polygons_found}")
    print(f"  - Points Found           : {report.points_found}")
    print(f"  - Failed Queries         : {report.failed_queries}")
    print(f"  - Retried Queries        : {report.retried_queries}")
    print(f"  - Duplicates Removed     : {report.duplicates_removed}")
    print(f"  - Time Elapsed           : {elapsed:.2f}s")
    
    # Sample 3 discovered items
    sample = records[:3]
    for idx, r in enumerate(sample, 1):
        print(f"    [{idx}] {r.name} | Type: {r.display_type} | Geometry: {r.geometry_type} | State: {r.state}")

print("\n" + "=" * 60)
print("BENCHMARK COMPLETED SUCCESSFULLY")
print("=" * 60)
