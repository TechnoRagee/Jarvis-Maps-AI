"""
check_area.py

Ask what boundary Jarvis will actually use for a given search area, before
running a job. Coordinates of results are always exact; what varies by area
type is the gate that decides which results are inside it.

Usage:
    +
    python check_area.py "Ghaziabad" --district "Uttar Pradesh"
    python check_area.py "201301"
    python check_area.py "Mumbai" "Pune" "201301" "Sector 62"
"""

from __future__ import annotations

import argparse
import sys

try:
    import amenity_finder as af
except ImportError:
    from utils import amenity_finder as af


def _describe(area: str, district: str | None) -> dict:
    lines: list[str] = []

    anchor = af._resolve_area_anchor(
        area,
        district=district,
        log=lambda message, tag="info": lines.append(message),
    )

    if not anchor:
        return {
            "area": area,
            "mode": "NONE",
            "grade": "BROKEN",
            "note": "Nominatim could not resolve this area. The distance "
                    "gate is disabled and every result Google returns will "
                    "be accepted, however far away it is.",
            "lines": lines,
        }

    if anchor.get("polygon"):
        vertices = sum(
            len(ring) for rings in anchor["polygon"] for ring in rings
        )
        mode, grade = "polygon", "BEST"
        note = (
            f"Real administrative outline, {vertices} points. Enclaves are "
            "excluded correctly and only genuine border cases rely on the "
            f"{af.BOUNDARY_BUFFER_KM:.0f} km buffer."
        )
    elif anchor.get("bbox"):
        south, north, west, east = anchor["bbox"]
        span_ns = af._haversine_km(south, west, north, west)
        span_ew = af._haversine_km(south, west, south, east)
        mode, grade = "bounding box", "OK"
        note = (
            f"Rectangle {span_ns:.0f} x {span_ew:.0f} km. Fine for compact "
            "areas. For an L-shaped or concave area it will also accept "
            "places in the notch that are not really inside."
        )
        if span_ns > 200 or span_ew > 200:
            grade = "WEAK"
            note += (
                " This box is large enough to swallow neighbouring "
                "districts — pass a district or use the polygon-backed "
                "parent area instead."
            )
    else:
        mode, grade = "radius", "WEAK"
        note = (
            f"No usable shape, falling back to a {anchor['radius_km']:.0f} km "
            "circle around the centre point. A circle is a poor model of any "
            "administrative area; expect both misses and over-reach. Set "
            "radius_km explicitly to control it."
        )

    return {
        "area": area,
        "mode": mode,
        "grade": grade,
        "lat": anchor["lat"],
        "lng": anchor["lng"],
        "note": note,
        "lines": lines,
    }


GRADE_MARK = {"BEST": "✅", "OK": "🟡", "WEAK": "⚠️ ", "BROKEN": "❌"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report the boundary quality Jarvis will use per area."
    )
    parser.add_argument("areas", nargs="+", help="one or more search areas")
    parser.add_argument(
        "--district",
        default=None,
        help="disambiguating district or state, applied to every area",
    )
    args = parser.parse_args()

    print()
    weak: list[str] = []

    for area in args.areas:
        report = _describe(area, args.district)
        mark = GRADE_MARK.get(report["grade"], "  ")

        print(f"{mark} {area}")
        print(f"     mode  : {report['mode']} ({report['grade']})")
        if "lat" in report:
            print(f"     centre: {report['lat']:.5f}, {report['lng']:.5f}")
        for line in report["lines"]:
            print(f"     {line}")
        print(f"     {report['note']}")
        print()

        if report["grade"] in ("WEAK", "BROKEN"):
            weak.append(area)

    if weak:
        print("Areas needing a district or a manual radius_km:")
        for area in weak:
            print(f"  - {area}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())