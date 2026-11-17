
"""
Priority 12 Topology-Safe Boundary Synchronization
==============================================

Synchronizes two or more overlapping locality GeoJSON files together.

Safety design
-------------
- Dry run by default.
- Original files are never modified.
- All supplied original polygons are processed in one operation.
- REVIEW files are ignored and already-synced files are rejected.
- Shared edges follow the competing original polygon edges, not anchor bisectors.
- Duplicate, nested, different-level, and dangerously overlapping areas
  are blocked.
- The original combined union is preserved.
- Final pairwise overlap must be almost zero.
- Every synchronized polygon must remain valid and retain its locality
  anchor.
- Applied results are written to a new timestamped folder.

Install:
    python -m pip install shapely pyproj

Dry run:
    python sync_multiple_boundaries.py area1.geojson area2.geojson area3.geojson

Apply:
    python sync_multiple_boundaries.py area1.geojson area2.geojson area3.geojson --apply
"""

from __future__ import annotations

import argparse
import json
import math
import re
import zipfile
import sys

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

try:
    from pyproj import CRS, Transformer
    from shapely.geometry import (
        GeometryCollection,
        LineString,
        MultiLineString,
        MultiPoint,
        MultiPolygon,
        Point,
        Polygon,
        box,
        mapping,
        shape,
    )
    from shapely.ops import linemerge, polygonize, split, transform, unary_union
    from shapely.validation import make_valid
except ImportError as exc:
    print(
        "ERROR: This tool requires Shapely and pyproj.\n"
        "Install them with:\n"
        "python -m pip install shapely pyproj"
    )
    raise SystemExit(1) from exc


# -------------------------------------------------------------------------
# Safety thresholds
# -------------------------------------------------------------------------

MIN_MEANINGFUL_OVERLAP_M2 = 50.0
MAX_SMALLER_POLYGON_OVERLAP_RATIO = 0.40

DUPLICATE_IOU_THRESHOLD = 0.82
DUPLICATE_SMALLER_OVERLAP_THRESHOLD = 0.92

MAX_AREA_CHANGE_RATIO = 0.25
MAX_UNION_CHANGE_RATIO = 0.0005
MAX_REMAINING_PAIR_OVERLAP_M2 = 2.0
MAX_PERIMETER_INCREASE_RATIO = 1.00

MIN_SHARED_EDGE_LENGTH_M = 5.0
NUMERICAL_SLIVER_M2 = 0.25

# Priority 11 - source-boundary following partition.
RASTER_TARGET_LONG_SIDE_PX = 1400
RASTER_MAX_CELLS = 4_000_000
RASTER_MIN_RESOLUTION_M = 0.75
RASTER_MAX_RESOLUTION_M = 6.0
RASTER_PADDING_PX = 8

# The final common edge must stay approximately midway between the two
# original competing edges instead of cutting diagonally across the overlap.
MAX_SHARED_EDGE_BALANCE_ERROR = 0.38
MAX_SHARED_EDGE_MEAN_SOURCE_DISTANCE_M = 300.0
MIN_SHARED_EDGE_SOURCE_LENGTH_RATIO = 0.35
SHARED_EDGE_SAMPLE_SPACING_M = 8.0

# Priority 12 - topology classification before synchronization.
#
# Only genuine same-level neighboring areas may be synchronized.
TOUCH_DISTANCE_M = 6.0
MIN_EXACT_SHARED_BOUNDARY_LENGTH_M = 25.0
MIN_BOUNDARY_PROXIMITY_LENGTH_M = 35.0
SOURCE_RAIL_BUFFER_M = 24.0

PARENT_CHILD_MIN_SMALLER_OVERLAP_RATIO = 0.55
PARENT_CHILD_MIN_ANCHOR_OVERLAP_RATIO = 0.35
PARENT_CHILD_MIN_ASYMMETRY_RATIO = 2.25

TRUE_NEIGHBOR_MAX_SMALLER_OVERLAP_RATIO = 0.40
TRUE_NEIGHBOR_MAX_AVERAGE_OVERLAP_WIDTH_M = 240.0
TRUE_NEIGHBOR_MAX_RELATIVE_OVERLAP_WIDTH = 0.34
TRUE_NEIGHBOR_MAX_OVERLAP_COMPONENTS = 3

TRIPLE_OVERLAP_BLOCK_AREA_M2 = 25.0

SIGNIFICANT_COMPONENT_AREA_M2 = 10.0
MAX_FINAL_SIGNIFICANT_COMPONENTS = 1
MAX_FINAL_INTERIOR_HOLES = 0


# -------------------------------------------------------------------------
# General helpers
# -------------------------------------------------------------------------

def _polygonal(geometry: Any) -> Any:
    if geometry is None or geometry.is_empty:
        return Polygon()

    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry

    if isinstance(geometry, GeometryCollection):
        polygon_parts = [
            item
            for item in geometry.geoms
            if isinstance(item, (Polygon, MultiPolygon))
            and not item.is_empty
        ]

        return (
            unary_union(polygon_parts)
            if polygon_parts
            else Polygon()
        )

    return Polygon()


def _linear(geometry: Any) -> Any:
    if geometry is None or geometry.is_empty:
        return LineString()

    if isinstance(geometry, (LineString, MultiLineString)):
        return geometry

    if isinstance(geometry, GeometryCollection):
        line_parts = [
            item
            for item in geometry.geoms
            if isinstance(item, (LineString, MultiLineString))
            and not item.is_empty
        ]

        return (
            unary_union(line_parts)
            if line_parts
            else LineString()
        )

    return LineString()


def _normalise_name(value: str) -> str:
    ignored = {
        "locality",
        "neighbourhood",
        "neighborhood",
        "new",
        "delhi",
        "district",
        "state",
        "india",
    }

    tokens = [
        token
        for token in re.sub(
            r"[^a-z0-9]+",
            " ",
            str(value or "").lower(),
        ).split()
        if token not in ignored
    ]

    return " ".join(tokens)


def _safe_stem(value: str) -> str:
    value = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        value,
    )

    value = re.sub(
        r"_+",
        "_",
        value,
    ).strip("_")

    return value or "boundary"


def _display_name(path: Path, properties: dict) -> str:
    return str(
        properties.get("display_name")
        or properties.get("name")
        or path.stem
    )


def _boundary_level(properties: dict) -> str:
    return str(
        properties.get("boundary_level")
        or "unknown"
    ).strip().lower()


def _load_geojson(path: Path) -> tuple[Any, dict]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError(
            f"{path.name}: invalid GeoJSON object"
        )

    payload_type = payload.get("type")

    if payload_type == "FeatureCollection":
        features = payload.get("features") or []

        if not features:
            raise ValueError(
                f"{path.name}: no GeoJSON features"
            )

        geometries = []
        merged_properties = {}

        for feature in features:
            if not isinstance(feature, dict):
                continue

            geometry_payload = feature.get("geometry")

            if not isinstance(geometry_payload, dict):
                continue

            geometry = _polygonal(
                make_valid(
                    shape(geometry_payload)
                )
            )

            if not geometry.is_empty:
                geometries.append(geometry)

            properties = feature.get("properties")

            if (
                isinstance(properties, dict)
                and not merged_properties
            ):
                merged_properties = dict(properties)

        if not geometries:
            raise ValueError(
                f"{path.name}: no polygon geometry"
            )

        geometry = _polygonal(
            make_valid(
                unary_union(geometries)
            )
        )

        properties = merged_properties

    elif payload_type == "Feature":
        geometry_payload = payload.get("geometry")

        if not isinstance(geometry_payload, dict):
            raise ValueError(
                f"{path.name}: missing geometry"
            )

        geometry = _polygonal(
            make_valid(
                shape(geometry_payload)
            )
        )

        properties = payload.get("properties")

        if not isinstance(properties, dict):
            properties = {}

    elif payload_type in {"Polygon", "MultiPolygon"}:
        geometry = _polygonal(
            make_valid(
                shape(payload)
            )
        )

        properties = {}

    else:
        raise ValueError(
            f"{path.name}: unsupported GeoJSON type "
            f"{payload_type!r}"
        )

    if geometry.is_empty:
        raise ValueError(
            f"{path.name}: empty polygon geometry"
        )

    return geometry, dict(properties)


def _utm_crs_for_geometries(
    geometries: list[Any],
) -> CRS:
    centroid = unary_union(
        geometries
    ).centroid

    longitude = float(
        centroid.x
    )

    latitude = float(
        centroid.y
    )

    zone = max(
        1,
        min(
            60,
            int(
                math.floor(
                    (
                        longitude
                        + 180.0
                    )
                    / 6.0
                )
                + 1
            ),
        ),
    )

    epsg = (
        32600
        + zone
        if latitude >= 0.0
        else 32700
        + zone
    )

    return CRS.from_epsg(
        epsg
    )


def _project_geometries(
    geometries: list[Any],
) -> tuple[
    list[Any],
    Transformer,
    CRS,
]:
    local_crs = _utm_crs_for_geometries(
        geometries
    )

    forward = Transformer.from_crs(
        "EPSG:4326",
        local_crs,
        always_xy=True,
    )

    reverse = Transformer.from_crs(
        local_crs,
        "EPSG:4326",
        always_xy=True,
    )

    projected = [
        _polygonal(
            make_valid(
                transform(
                    forward.transform,
                    geometry,
                )
            )
        )
        for geometry
        in geometries
    ]

    return (
        projected,
        reverse,
        local_crs,
    )


# -------------------------------------------------------------------------
# Voronoi-like partition helpers
# -------------------------------------------------------------------------

def _half_plane_closer_to(
    first_anchor: Point,
    second_anchor: Point,
    expanded_box: Polygon,
) -> Polygon:
    first_x = float(
        first_anchor.x
    )

    first_y = float(
        first_anchor.y
    )

    second_x = float(
        second_anchor.x
    )

    second_y = float(
        second_anchor.y
    )

    direction_x = (
        second_x
        - first_x
    )

    direction_y = (
        second_y
        - first_y
    )

    distance = math.hypot(
        direction_x,
        direction_y,
    )

    if distance < 1.0:
        raise ValueError(
            "Two locality anchors are almost identical."
        )

    midpoint_x = (
        first_x
        + second_x
    ) / 2.0

    midpoint_y = (
        first_y
        + second_y
    ) / 2.0

    perpendicular_x = (
        -direction_y
        / distance
    )

    perpendicular_y = (
        direction_x
        / distance
    )

    min_x, min_y, max_x, max_y = (
        expanded_box.bounds
    )

    diagonal = max(
        math.hypot(
            max_x
            - min_x,
            max_y
            - min_y,
        ),
        1.0,
    )

    for multiplier in (
        4.0,
        8.0,
        16.0,
        32.0,
    ):
        extension = max(
            diagonal
            * multiplier,
            50000.0,
        )

        splitter = LineString([
            (
                midpoint_x
                - perpendicular_x
                * extension,
                midpoint_y
                - perpendicular_y
                * extension,
            ),
            (
                midpoint_x
                + perpendicular_x
                * extension,
                midpoint_y
                + perpendicular_y
                * extension,
            ),
        ])

        pieces = split(
            expanded_box,
            splitter,
        )

        polygon_pieces = [
            item
            for item
            in pieces.geoms
            if isinstance(
                item,
                Polygon,
            )
            and not item.is_empty
        ]

        if len(
            polygon_pieces
        ) != 2:
            continue

        selected = next(
            (
                piece
                for piece
                in polygon_pieces
                if piece.covers(
                    first_anchor
                )
            ),
            None,
        )

        if selected is not None:
            return selected

    raise ValueError(
        "Could not build a reliable multi-area split plane."
    )


def _build_local_cells(
    coverer_indexes: list[int],
    anchors: list[Point],
    expanded_box: Polygon,
) -> dict[int, Any]:
    cells = {}

    for index in coverer_indexes:
        cell = expanded_box

        for other_index in coverer_indexes:
            if other_index == index:
                continue

            half_plane = _half_plane_closer_to(
                anchors[index],
                anchors[other_index],
                expanded_box,
            )

            cell = _polygonal(
                make_valid(
                    cell.intersection(
                        half_plane
                    )
                )
            )

            if cell.is_empty:
                break

        cells[index] = cell

    return cells


def _assign_residual_by_nearest_anchor(
    residual: Any,
    coverer_indexes: list[int],
    anchors: list[Point],
    assigned_parts: dict[int, list[Any]],
) -> None:
    residual = _polygonal(
        make_valid(
            residual
        )
    )

    if residual.is_empty:
        return

    components = (
        [residual]
        if isinstance(
            residual,
            Polygon,
        )
        else list(
            residual.geoms
        )
    )

    for component in components:
        if component.is_empty:
            continue

        representative = (
            component.representative_point()
        )

        nearest_index = min(
            coverer_indexes,
            key=lambda index: (
                representative.distance(
                    anchors[index]
                )
            ),
        )

        assigned_parts[
            nearest_index
        ].append(
            component
        )



def _iter_polygons(
    geometry: Any,
):
    geometry = _polygonal(
        geometry
    )

    if isinstance(
        geometry,
        Polygon,
    ):
        yield geometry

    elif isinstance(
        geometry,
        MultiPolygon,
    ):
        yield from geometry.geoms


def _iter_lines(
    geometry: Any,
):
    if geometry is None or geometry.is_empty:
        return

    if isinstance(
        geometry,
        LineString,
    ):
        yield geometry

    elif isinstance(
        geometry,
        MultiLineString,
    ):
        yield from geometry.geoms

    elif isinstance(
        geometry,
        GeometryCollection,
    ):
        for part in geometry.geoms:
            yield from _iter_lines(
                part
            )


def _raster_context(
    face: Any,
) -> dict:
    min_x, min_y, max_x, max_y = (
        face.bounds
    )

    width_m = max(
        max_x - min_x,
        1.0,
    )

    height_m = max(
        max_y - min_y,
        1.0,
    )

    long_side_m = max(
        width_m,
        height_m,
    )

    resolution_m = min(
        RASTER_MAX_RESOLUTION_M,
        max(
            RASTER_MIN_RESOLUTION_M,
            long_side_m
            / float(
                RASTER_TARGET_LONG_SIDE_PX
            ),
        ),
    )

    while True:
        width_px = (
            int(
                math.ceil(
                    width_m
                    / resolution_m
                )
            )
            + 2
            * RASTER_PADDING_PX
            + 3
        )

        height_px = (
            int(
                math.ceil(
                    height_m
                    / resolution_m
                )
            )
            + 2
            * RASTER_PADDING_PX
            + 3
        )

        if (
            width_px
            * height_px
            <= RASTER_MAX_CELLS
        ):
            break

        resolution_m *= 1.20

    return {
        "min_x": float(
            min_x
        ),
        "max_y": float(
            max_y
        ),
        "resolution_m": float(
            resolution_m
        ),
        "width_px": int(
            width_px
        ),
        "height_px": int(
            height_px
        ),
        "padding_px": int(
            RASTER_PADDING_PX
        ),
    }


def _metric_coords_to_pixels(
    coordinates,
    context: dict,
) -> np.ndarray:
    min_x = context[
        "min_x"
    ]

    max_y = context[
        "max_y"
    ]

    resolution_m = context[
        "resolution_m"
    ]

    padding_px = context[
        "padding_px"
    ]

    points = []

    for x_value, y_value in coordinates:
        pixel_x = int(
            round(
                (
                    float(
                        x_value
                    )
                    - min_x
                )
                / resolution_m
            )
        ) + padding_px

        pixel_y = int(
            round(
                (
                    max_y
                    - float(
                        y_value
                    )
                )
                / resolution_m
            )
        ) + padding_px

        points.append([
            pixel_x,
            pixel_y,
        ])

    return np.asarray(
        points,
        dtype=np.int32,
    ).reshape(
        -1,
        1,
        2,
    )


def _rasterize_polygon(
    geometry: Any,
    context: dict,
) -> np.ndarray:
    mask = np.zeros(
        (
            context[
                "height_px"
            ],
            context[
                "width_px"
            ],
        ),
        dtype=np.uint8,
    )

    for polygon in _iter_polygons(
        geometry
    ):
        exterior = _metric_coords_to_pixels(
            polygon.exterior.coords,
            context,
        )

        if len(
            exterior
        ) >= 3:
            cv2.fillPoly(
                mask,
                [
                    exterior
                ],
                255,
            )

        for interior in polygon.interiors:
            hole = _metric_coords_to_pixels(
                interior.coords,
                context,
            )

            if len(
                hole
            ) >= 3:
                cv2.fillPoly(
                    mask,
                    [
                        hole
                    ],
                    0,
                )

    return mask


def _rasterize_lines(
    geometry: Any,
    context: dict,
) -> np.ndarray:
    mask = np.zeros(
        (
            context[
                "height_px"
            ],
            context[
                "width_px"
            ],
        ),
        dtype=np.uint8,
    )

    for line in _iter_lines(
        geometry
    ):
        if len(
            line.coords
        ) < 2:
            continue

        pixels = _metric_coords_to_pixels(
            line.coords,
            context,
        )

        cv2.polylines(
            mask,
            [
                pixels
            ],
            isClosed=False,
            color=255,
            thickness=1,
            lineType=cv2.LINE_8,
        )

    return mask


def _pixel_ring_to_metric(
    contour: np.ndarray,
    context: dict,
) -> list[
    tuple[
        float,
        float,
    ]
]:
    min_x = context[
        "min_x"
    ]

    max_y = context[
        "max_y"
    ]

    resolution_m = context[
        "resolution_m"
    ]

    padding_px = context[
        "padding_px"
    ]

    coordinates = []

    for pixel_x, pixel_y in contour.reshape(
        -1,
        2,
    ):
        metric_x = (
            min_x
            + (
                float(
                    pixel_x
                )
                - padding_px
            )
            * resolution_m
        )

        metric_y = (
            max_y
            - (
                float(
                    pixel_y
                )
                - padding_px
            )
            * resolution_m
        )

        coordinates.append((
            metric_x,
            metric_y,
        ))

    return coordinates


def _mask_to_metric_polygon(
    mask: np.ndarray,
    context: dict,
) -> Any:
    contours, hierarchy = cv2.findContours(
        mask,
        cv2.RETR_CCOMP,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    if (
        hierarchy is None
        or not contours
    ):
        return Polygon()

    hierarchy = hierarchy[
        0
    ]

    polygons = []

    for contour_index, contour in enumerate(
        contours
    ):
        parent_index = int(
            hierarchy[
                contour_index
            ][
                3
            ]
        )

        if parent_index != -1:
            continue

        if cv2.contourArea(
            contour
        ) < 2.0:
            continue

        simplified = cv2.approxPolyDP(
            contour,
            epsilon=1.25,
            closed=True,
        )

        outer = _pixel_ring_to_metric(
            simplified,
            context,
        )

        if len(
            outer
        ) < 3:
            continue

        holes = []

        child_index = int(
            hierarchy[
                contour_index
            ][
                2
            ]
        )

        while child_index != -1:
            child_contour = contours[
                child_index
            ]

            if cv2.contourArea(
                child_contour
            ) >= 2.0:
                child_simplified = cv2.approxPolyDP(
                    child_contour,
                    epsilon=1.25,
                    closed=True,
                )

                hole = _pixel_ring_to_metric(
                    child_simplified,
                    context,
                )

                if len(
                    hole
                ) >= 3:
                    holes.append(
                        hole
                    )

            child_index = int(
                hierarchy[
                    child_index
                ][
                    0
                ]
            )

        polygon = _polygonal(
            make_valid(
                Polygon(
                    outer,
                    holes,
                )
            )
        )

        if not polygon.is_empty:
            polygons.append(
                polygon
            )

    return (
        _polygonal(
            make_valid(
                unary_union(
                    polygons
                )
            )
        )
        if polygons
        else Polygon()
    )


def _keep_source_connected_parts(
    geometry: Any,
    source_boundary: Any,
    tolerance_m: float,
) -> Any:
    geometry = _polygonal(
        make_valid(
            geometry
        )
    )

    if geometry.is_empty:
        return geometry

    components = (
        [
            geometry
        ]
        if isinstance(
            geometry,
            Polygon,
        )
        else list(
            geometry.geoms
        )
    )

    touching = [
        component
        for component
        in components
        if component.distance(
            source_boundary
        )
        <= tolerance_m
    ]

    if touching:
        # One overlap face should produce one source-connected side. Keeping
        # only the largest touching component removes tiny raster islands that
        # otherwise create zig-zag shared edges.
        return max(
            touching,
            key=lambda component: (
                component.area
            ),
        )

    return max(
        components,
        key=lambda component: (
            component.area
        ),
    )


def _longest_line(
    geometry: Any,
) -> LineString:
    geometry = _linear(
        geometry
    )

    if geometry.is_empty:
        return LineString()

    merged = linemerge(
        geometry
    )

    lines = list(
        _iter_lines(
            merged
        )
    )

    return (
        max(
            lines,
            key=lambda line: (
                line.length
            ),
        )
        if lines
        else LineString()
    )


def _source_rail_for_face(
    index: int,
    other_index: int,
    face: Any,
    geometries: list[Any],
    tolerance_m: float,
) -> LineString:
    face_band = face.boundary.buffer(
        tolerance_m
        * 3.0
    )

    source_candidate = _linear(
        geometries[
            index
        ].boundary.intersection(
            face_band
        )
    )

    unique_source = _linear(
        source_candidate.difference(
            geometries[
                other_index
            ].boundary.buffer(
                tolerance_m
                * 1.75
            )
        )
    )

    selected = (
        unique_source
        if not unique_source.is_empty
        and unique_source.length
        >= tolerance_m
        * 6.0
        else source_candidate
    )

    return _longest_line(
        selected
    )


def _normalised_line_samples(
    line: LineString,
    sample_count: int,
    reverse: bool = False,
) -> list[
    tuple[
        float,
        float,
    ]
]:
    distances = np.linspace(
        0.0,
        float(
            line.length
        ),
        sample_count,
    )

    if reverse:
        distances = distances[
            ::-1
        ]

    return [
        (
            float(
                point.x
            ),
            float(
                point.y
            ),
        )
        for point
        in (
            line.interpolate(
                float(
                    distance
                )
            )
            for distance
            in distances
        )
    ]


def _extended_midline_from_rails(
    first_rail: LineString,
    second_rail: LineString,
    face: Any,
    resolution_m: float,
) -> tuple[
    LineString,
    LineString,
    dict,
]:
    first_start = Point(
        first_rail.coords[
            0
        ]
    )

    first_end = Point(
        first_rail.coords[
            -1
        ]
    )

    second_start = Point(
        second_rail.coords[
            0
        ]
    )

    second_end = Point(
        second_rail.coords[
            -1
        ]
    )

    same_direction_cost = (
        first_start.distance(
            second_start
        )
        + first_end.distance(
            second_end
        )
    )

    reversed_direction_cost = (
        first_start.distance(
            second_end
        )
        + first_end.distance(
            second_start
        )
    )

    reverse_second = (
        reversed_direction_cost
        < same_direction_cost
    )

    sample_count = max(
        24,
        min(
            1600,
            int(
                math.ceil(
                    max(
                        first_rail.length,
                        second_rail.length,
                    )
                    / max(
                        resolution_m
                        * 2.0,
                        2.0,
                    )
                )
            )
            + 1,
        ),
    )

    first_samples = _normalised_line_samples(
        first_rail,
        sample_count,
        reverse=False,
    )

    second_samples = _normalised_line_samples(
        second_rail,
        sample_count,
        reverse=reverse_second,
    )

    midpoint_coordinates = [
        (
            (
                first_point[
                    0
                ]
                + second_point[
                    0
                ]
            )
            / 2.0,
            (
                first_point[
                    1
                ]
                + second_point[
                    1
                ]
            )
            / 2.0,
        )
        for first_point, second_point
        in zip(
            first_samples,
            second_samples,
        )
    ]

    midline = LineString(
        midpoint_coordinates
    ).simplify(
        max(
            resolution_m
            * 0.80,
            0.75,
        ),
        preserve_topology=True,
    )

    coordinates = list(
        midline.coords
    )

    if len(
        coordinates
    ) < 2:
        raise ValueError(
            "Could not build a reliable shared centreline."
        )

    start_x, start_y = coordinates[
        0
    ]

    next_x, next_y = coordinates[
        1
    ]

    end_x, end_y = coordinates[
        -1
    ]

    previous_x, previous_y = coordinates[
        -2
    ]

    start_dx = (
        start_x
        - next_x
    )

    start_dy = (
        start_y
        - next_y
    )

    end_dx = (
        end_x
        - previous_x
    )

    end_dy = (
        end_y
        - previous_y
    )

    start_length = max(
        math.hypot(
            start_dx,
            start_dy,
        ),
        0.001,
    )

    end_length = max(
        math.hypot(
            end_dx,
            end_dy,
        ),
        0.001,
    )

    min_x, min_y, max_x, max_y = (
        face.bounds
    )

    extension_m = max(
        math.hypot(
            max_x
            - min_x,
            max_y
            - min_y,
        )
        * 2.0,
        500.0,
    )

    extended_coordinates = [
        (
            start_x
            + start_dx
            / start_length
            * extension_m,
            start_y
            + start_dy
            / start_length
            * extension_m,
        ),
        *coordinates,
        (
            end_x
            + end_dx
            / end_length
            * extension_m,
            end_y
            + end_dy
            / end_length
            * extension_m,
        ),
    ]

    extended_line = LineString(
        extended_coordinates
    )

    diagnostics = {
        "sample_count": sample_count,
        "first_rail_length_m": round(
            first_rail.length,
            3,
        ),
        "second_rail_length_m": round(
            second_rail.length,
            3,
        ),
        "midline_length_m": round(
            midline.length,
            3,
        ),
        "second_rail_reversed": bool(
            reverse_second
        ),
    }

    return (
        midline,
        extended_line,
        diagnostics,
    )


def _partition_face_by_source_boundaries(
    face: Any,
    coverer_indexes: list[int],
    geometries: list[Any],
) -> tuple[
    dict[int, Any],
    dict,
]:
    """
    Split a two-polygon overlap face along the averaged source rails.

    The exact same split geometry is used for both output polygons. A
    three-way overlap is blocked because automatic ownership would be
    ambiguous and could create a shortcut at the junction.
    """
    if len(
        coverer_indexes
    ) != 2:
        raise ValueError(
            "A three-way overlap face was detected. Priority 11 blocks "
            "this junction for manual review instead of drawing diagonal "
            "shortcuts."
        )

    first_index, second_index = (
        coverer_indexes
    )

    context = _raster_context(
        face
    )

    tolerance_m = max(
        context[
            "resolution_m"
        ],
        1.0,
    )

    first_rail = _source_rail_for_face(
        first_index,
        second_index,
        face,
        geometries,
        tolerance_m,
    )

    second_rail = _source_rail_for_face(
        second_index,
        first_index,
        face,
        geometries,
        tolerance_m,
    )

    if (
        first_rail.is_empty
        or second_rail.is_empty
        or first_rail.length
        < MIN_SHARED_EDGE_LENGTH_M
        or second_rail.length
        < MIN_SHARED_EDGE_LENGTH_M
    ):
        raise ValueError(
            "Could not isolate both original overlap edges."
        )

    (
        midline,
        extended_line,
        midline_diagnostics,
    ) = _extended_midline_from_rails(
        first_rail,
        second_rail,
        face,
        tolerance_m,
    )

    split_result = split(
        face,
        extended_line,
    )

    pieces = [
        _polygonal(
            make_valid(
                geometry
            )
        )
        for geometry
        in split_result.geoms
        if isinstance(
            geometry,
            (Polygon, MultiPolygon),
        )
        and not geometry.is_empty
        and geometry.area
        > NUMERICAL_SLIVER_M2
    ]

    if len(
        pieces
    ) != 2:
        raise ValueError(
            "The source-following centreline did not split the overlap "
            "into exactly two clean sides."
        )

    first_piece, second_piece = (
        pieces
    )

    first_exclusive = _polygonal(
        make_valid(
            geometries[
                first_index
            ].difference(
                geometries[
                    second_index
                ]
            )
        )
    )

    second_exclusive = _polygonal(
        make_valid(
            geometries[
                second_index
            ].difference(
                geometries[
                    first_index
                ]
            )
        )
    )

    if (
        first_exclusive.is_empty
        or second_exclusive.is_empty
    ):
        raise ValueError(
            "The overlap sides could not be connected to both original "
            "locality areas."
        )

    direct_cost = (
        first_piece.representative_point().distance(
            first_exclusive
        )
        + second_piece.representative_point().distance(
            second_exclusive
        )
    )

    swapped_cost = (
        first_piece.representative_point().distance(
            second_exclusive
        )
        + second_piece.representative_point().distance(
            first_exclusive
        )
    )

    if direct_cost <= swapped_cost:
        assignments = {
            first_index: first_piece,
            second_index: second_piece,
        }
    else:
        assignments = {
            first_index: second_piece,
            second_index: first_piece,
        }

    shared_inside_face = _linear(
        midline.intersection(
            face
        )
    )

    diagnostics = {
        "method": (
            "averaged_source_rail_shared_edge"
        ),
        "coverer_count": 2,
        "raster_resolution_m": round(
            context[
                "resolution_m"
            ],
            3,
        ),
        "shared_edge_length_m": round(
            shared_inside_face.length,
            3,
        ),
        **midline_diagnostics,
    }

    return (
        assignments,
        diagnostics,
    )


def _partition_union(
    geometries: list[Any],
    anchors: list[Point],
) -> tuple[
    list[Any],
    list[dict],
]:
    """
    Atomic overlay partition using the competing source boundaries.

    `anchors` remains in the signature for backward compatibility and final
    anchor validation, but it is not used to draw the shared edge.
    """
    del anchors

    merged_boundaries = unary_union(
        [
            geometry.boundary
            for geometry
            in geometries
        ]
    )

    faces = [
        face
        for face
        in polygonize(
            merged_boundaries
        )
        if not face.is_empty
        and face.area
        > NUMERICAL_SLIVER_M2
    ]

    assigned_parts: dict[
        int,
        list[Any],
    ] = {
        index: []
        for index in range(
            len(
                geometries
            )
        )
    }

    partition_diagnostics = []

    for face_index, face in enumerate(
        faces,
        start=1,
    ):
        representative = (
            face.representative_point()
        )

        coverer_indexes = [
            index
            for index, geometry
            in enumerate(
                geometries
            )
            if geometry.covers(
                representative
            )
        ]

        if not coverer_indexes:
            continue

        if len(
            coverer_indexes
        ) == 1:
            assigned_parts[
                coverer_indexes[
                    0
                ]
            ].append(
                face
            )

            continue

        assignments, diagnostics = (
            _partition_face_by_source_boundaries(
                face=face,
                coverer_indexes=coverer_indexes,
                geometries=geometries,
            )
        )

        diagnostics[
            "face_index"
        ] = face_index

        diagnostics[
            "face_area_m2"
        ] = round(
            face.area,
            3,
        )

        partition_diagnostics.append(
            diagnostics
        )

        for index, piece in assignments.items():
            if (
                not piece.is_empty
                and piece.area
                > NUMERICAL_SLIVER_M2
            ):
                assigned_parts[
                    index
                ].append(
                    piece
                )

    final_geometries = []

    for index in range(
        len(
            geometries
        )
    ):
        parts = assigned_parts[
            index
        ]

        if not parts:
            final_geometries.append(
                Polygon()
            )

            continue

        final_geometry = _polygonal(
            make_valid(
                unary_union(
                    parts
                )
            )
        )

        final_geometries.append(
            final_geometry
        )

    return (
        final_geometries,
        partition_diagnostics,
    )


# -------------------------------------------------------------------------
# Validation
# -------------------------------------------------------------------------

def _pair_metrics(
    first: Any,
    second: Any,
) -> dict:
    intersection_area = float(
        first.intersection(
            second
        ).area
    )

    first_area = float(
        first.area
    )

    second_area = float(
        second.area
    )

    union_area = float(
        first.union(
            second
        ).area
    )

    return {
        "intersection_area_m2": (
            intersection_area
        ),
        "first_overlap_ratio": (
            intersection_area
            / max(
                first_area,
                1.0,
            )
        ),
        "second_overlap_ratio": (
            intersection_area
            / max(
                second_area,
                1.0,
            )
        ),
        "smaller_overlap_ratio": (
            intersection_area
            / max(
                min(
                    first_area,
                    second_area,
                ),
                1.0,
            )
        ),
        "iou": (
            intersection_area
            / max(
                union_area,
                1.0,
            )
        ),
    }



def _sample_line_points(
    geometry: Any,
    spacing_m: float,
) -> list[Point]:
    points = []

    for line in _iter_lines(
        _linear(
            geometry
        )
    ):
        length = float(
            line.length
        )

        if length <= 0.0:
            continue

        sample_count = max(
            2,
            int(
                math.ceil(
                    length
                    / max(
                        spacing_m,
                        1.0,
                    )
                )
            )
            + 1,
        )

        for sample_index in range(
            sample_count
        ):
            distance = (
                length
                * sample_index
                / max(
                    sample_count
                    - 1,
                    1,
                )
            )

            points.append(
                line.interpolate(
                    distance
                )
            )

    return points


def _shared_edge_source_metrics(
    shared_edge: Any,
    first_geometry: Any,
    second_geometry: Any,
    original_overlap: Any,
) -> dict:
    samples = _sample_line_points(
        shared_edge,
        SHARED_EDGE_SAMPLE_SPACING_M,
    )

    if not samples:
        return {
            "sample_count": 0,
            "mean_balance_error": 1.0,
            "mean_source_distance_m": float(
                "inf"
            ),
            "max_source_distance_m": float(
                "inf"
            ),
            "source_length_ratio": 0.0,
        }

    first_distances = np.asarray(
        [
            point.distance(
                first_geometry.boundary
            )
            for point
            in samples
        ],
        dtype=np.float64,
    )

    second_distances = np.asarray(
        [
            point.distance(
                second_geometry.boundary
            )
            for point
            in samples
        ],
        dtype=np.float64,
    )

    combined_distances = (
        first_distances
        + second_distances
    )

    balance_errors = (
        np.abs(
            first_distances
            - second_distances
        )
        / np.maximum(
            combined_distances,
            1.0,
        )
    )

    source_distances = (
        combined_distances
        / 2.0
    )

    rail_buffer = max(
        5.0,
        float(
            np.percentile(
                combined_distances,
                75,
            )
        )
        * 1.50,
    )

    first_rail = _linear(
        first_geometry.boundary.intersection(
            original_overlap.buffer(
                rail_buffer
            )
        )
    )

    second_rail = _linear(
        second_geometry.boundary.intersection(
            original_overlap.buffer(
                rail_buffer
            )
        )
    )

    reference_length = max(
        min(
            float(
                first_rail.length
            ),
            float(
                second_rail.length
            ),
        ),
        1.0,
    )

    return {
        "sample_count": len(
            samples
        ),
        "mean_balance_error": float(
            np.mean(
                balance_errors
            )
        ),
        "mean_source_distance_m": float(
            np.mean(
                source_distances
            )
        ),
        "max_source_distance_m": float(
            np.max(
                source_distances
            )
        ),
        "source_length_ratio": (
            float(
                shared_edge.length
            )
            / reference_length
        ),
        "first_source_rail_length_m": float(
            first_rail.length
        ),
        "second_source_rail_length_m": float(
            second_rail.length
        ),
    }


def _is_review_input(
    path: Path,
    properties: dict,
) -> bool:
    lowered_name = path.name.lower()

    return (
        "_review_" in lowered_name
        or bool(
            properties.get(
                "review_candidate",
                False,
            )
        )
        or bool(
            properties.get(
                "review_type",
                "",
            )
        )
    )


def _is_already_synced_input(
    path: Path,
    properties: dict,
) -> bool:
    lowered_name = path.name.lower()

    return (
        "_synced" in lowered_name
        or bool(
            properties.get(
                "priority5_multi_synced",
                False,
            )
        )
        or bool(
            properties.get(
                "priority11_shared_edge_synced",
                False,
            )
        )
    )




def _significant_component_count(
    geometry: Any,
) -> int:
    geometry = _polygonal(
        make_valid(
            geometry
        )
    )

    if geometry.is_empty:
        return 0

    if isinstance(
        geometry,
        Polygon,
    ):
        return int(
            geometry.area
            >= SIGNIFICANT_COMPONENT_AREA_M2
        )

    return sum(
        1
        for polygon
        in geometry.geoms
        if polygon.area
        >= SIGNIFICANT_COMPONENT_AREA_M2
    )


def _interior_hole_count(
    geometry: Any,
) -> int:
    geometry = _polygonal(
        make_valid(
            geometry
        )
    )

    if geometry.is_empty:
        return 0

    polygons = (
        [
            geometry
        ]
        if isinstance(
            geometry,
            Polygon,
        )
        else list(
            geometry.geoms
        )
    )

    return sum(
        len(
            polygon.interiors
        )
        for polygon
        in polygons
        if polygon.area
        >= SIGNIFICANT_COMPONENT_AREA_M2
    )


def _polygon_component_count(
    geometry: Any,
) -> int:
    geometry = _polygonal(
        make_valid(
            geometry
        )
    )

    if geometry.is_empty:
        return 0

    if isinstance(
        geometry,
        Polygon,
    ):
        return 1

    return len(
        geometry.geoms
    )


def _topology_metrics(
    first: Any,
    second: Any,
    first_anchor: Point,
    second_anchor: Point,
) -> dict:
    pair = _pair_metrics(
        first,
        second,
    )

    overlap = _polygonal(
        make_valid(
            first.intersection(
                second
            )
        )
    )

    first_area = max(
        float(
            first.area
        ),
        1.0,
    )

    second_area = max(
        float(
            second.area
        ),
        1.0,
    )

    smaller_area = min(
        first_area,
        second_area,
    )

    exact_shared_boundary = _linear(
        first.boundary.intersection(
            second.boundary
        )
    )

    exact_shared_length = float(
        exact_shared_boundary.length
    )

    geometry_distance = float(
        first.distance(
            second
        )
    )

    first_near_second = _linear(
        first.boundary.intersection(
            second.boundary.buffer(
                TOUCH_DISTANCE_M
            )
        )
    )

    second_near_first = _linear(
        second.boundary.intersection(
            first.boundary.buffer(
                TOUCH_DISTANCE_M
            )
        )
    )

    proximity_length = min(
        float(
            first_near_second.length
        ),
        float(
            second_near_first.length
        ),
    )

    source_rail_length = 0.0
    average_overlap_width = 0.0
    relative_overlap_width = 0.0
    overlap_component_count = 0
    overlap_compactness = 0.0

    if not overlap.is_empty:
        overlap_component_count = (
            _polygon_component_count(
                overlap
            )
        )

        overlap_perimeter = max(
            float(
                overlap.length
            ),
            1.0,
        )

        overlap_compactness = (
            4.0
            * math.pi
            * float(
                overlap.area
            )
            / (
                overlap_perimeter
                * overlap_perimeter
            )
        )

        overlap_buffer = overlap.buffer(
            SOURCE_RAIL_BUFFER_M
        )

        first_source_rail = _linear(
            first.boundary.intersection(
                overlap_buffer
            )
        )

        second_source_rail = _linear(
            second.boundary.intersection(
                overlap_buffer
            )
        )

        source_rail_length = min(
            float(
                first_source_rail.length
            ),
            float(
                second_source_rail.length
            ),
        )

        average_overlap_width = (
            float(
                overlap.area
            )
            / max(
                source_rail_length,
                1.0,
            )
        )

        equivalent_smaller_radius = math.sqrt(
            smaller_area
            / math.pi
        )

        relative_overlap_width = (
            average_overlap_width
            / max(
                equivalent_smaller_radius,
                1.0,
            )
        )

    first_anchor_inside_second = bool(
        second.covers(
            first_anchor
        )
    )

    second_anchor_inside_first = bool(
        first.covers(
            second_anchor
        )
    )

    minimum_overlap_ratio = min(
        pair[
            "first_overlap_ratio"
        ],
        pair[
            "second_overlap_ratio"
        ],
    )

    maximum_overlap_ratio = max(
        pair[
            "first_overlap_ratio"
        ],
        pair[
            "second_overlap_ratio"
        ],
    )

    overlap_asymmetry_ratio = (
        maximum_overlap_ratio
        / max(
            minimum_overlap_ratio,
            0.000001,
        )
    )

    return {
        **pair,
        "geometry_distance_m": geometry_distance,
        "exact_shared_boundary_length_m": (
            exact_shared_length
        ),
        "boundary_proximity_length_m": (
            proximity_length
        ),
        "source_rail_length_m": (
            source_rail_length
        ),
        "average_overlap_width_m": (
            average_overlap_width
        ),
        "relative_overlap_width": (
            relative_overlap_width
        ),
        "overlap_component_count": (
            overlap_component_count
        ),
        "overlap_compactness": (
            overlap_compactness
        ),
        "first_anchor_inside_second": (
            first_anchor_inside_second
        ),
        "second_anchor_inside_first": (
            second_anchor_inside_first
        ),
        "overlap_asymmetry_ratio": (
            overlap_asymmetry_ratio
        ),
    }


def _classify_topology_pair(
    metrics: dict,
) -> tuple[
    str,
    str,
]:
    intersection_area = float(
        metrics[
            "intersection_area_m2"
        ]
    )

    smaller_overlap = float(
        metrics[
            "smaller_overlap_ratio"
        ]
    )

    duplicate_shape = (
        metrics[
            "iou"
        ]
        >= DUPLICATE_IOU_THRESHOLD
        and smaller_overlap
        >= DUPLICATE_SMALLER_OVERLAP_THRESHOLD
    )

    if duplicate_shape:
        return (
            "duplicate_shape",
            "The two boundaries appear to represent the same area.",
        )

    if (
        intersection_area
        < MIN_MEANINGFUL_OVERLAP_M2
    ):
        if (
            metrics[
                "exact_shared_boundary_length_m"
            ]
            >= MIN_EXACT_SHARED_BOUNDARY_LENGTH_M
        ):
            return (
                "already_aligned_neighbor",
                "The areas already share an exact boundary and need no cut.",
            )

        if (
            metrics[
                "geometry_distance_m"
            ]
            <= TOUCH_DISTANCE_M
            and metrics[
                "boundary_proximity_length_m"
            ]
            >= MIN_BOUNDARY_PROXIMITY_LENGTH_M
        ):
            return (
                "small_gap_between_neighbors",
                "The outlines look neighboring but have a physical gap.",
            )

        return (
            "separate_not_neighbor",
            "The two selected areas do not share a usable outside edge.",
        )

    anchor_containment = (
        metrics[
            "first_anchor_inside_second"
        ]
        or metrics[
            "second_anchor_inside_first"
        ]
    )

    parent_child = (
        smaller_overlap
        >= PARENT_CHILD_MIN_SMALLER_OVERLAP_RATIO
        and (
            anchor_containment
            or metrics[
                "overlap_asymmetry_ratio"
            ]
            >= PARENT_CHILD_MIN_ASYMMETRY_RATIO
        )
    ) or (
        anchor_containment
        and smaller_overlap
        >= PARENT_CHILD_MIN_ANCHOR_OVERLAP_RATIO
    )

    if parent_child:
        return (
            "parent_child_overlap",
            "One boundary appears to be a broad parent area or an internal sub-locality.",
        )

    broad_overlap = (
        smaller_overlap
        > TRUE_NEIGHBOR_MAX_SMALLER_OVERLAP_RATIO
        or metrics[
            "average_overlap_width_m"
        ]
        > TRUE_NEIGHBOR_MAX_AVERAGE_OVERLAP_WIDTH_M
        or metrics[
            "relative_overlap_width"
        ]
        > TRUE_NEIGHBOR_MAX_RELATIVE_OVERLAP_WIDTH
        or metrics[
            "overlap_component_count"
        ]
        > TRUE_NEIGHBOR_MAX_OVERLAP_COMPONENTS
    )

    if broad_overlap:
        return (
            "broad_overlapping_areas",
            "The overlap is too wide or complex to be treated as a shared outside edge.",
        )

    true_neighbor = (
        not anchor_containment
        and smaller_overlap
        <= TRUE_NEIGHBOR_MAX_SMALLER_OVERLAP_RATIO
        and metrics[
            "source_rail_length_m"
        ]
        >= MIN_BOUNDARY_PROXIMITY_LENGTH_M
        and metrics[
            "average_overlap_width_m"
        ]
        <= TRUE_NEIGHBOR_MAX_AVERAGE_OVERLAP_WIDTH_M
        and metrics[
            "relative_overlap_width"
        ]
        <= TRUE_NEIGHBOR_MAX_RELATIVE_OVERLAP_WIDTH
        and metrics[
            "overlap_component_count"
        ]
        <= TRUE_NEIGHBOR_MAX_OVERLAP_COMPONENTS
    )

    if true_neighbor:
        return (
            "true_neighbor_overlap",
            "Same-level neighboring outlines overlap as a narrow edge strip and may be synchronized.",
        )

    return (
        "uncertain_topology",
        "The relationship is unclear, so automatic synchronization is blocked.",
    )


def _triple_overlap_checks(
    geometries: list[Any],
    input_records: list[dict],
) -> list[dict]:
    checks = []

    for first_index in range(
        len(
            geometries
        )
    ):
        for second_index in range(
            first_index
            + 1,
            len(
                geometries
            ),
        ):
            for third_index in range(
                second_index
                + 1,
                len(
                    geometries
                ),
            ):
                triple = _polygonal(
                    make_valid(
                        geometries[
                            first_index
                        ].intersection(
                            geometries[
                                second_index
                            ]
                        ).intersection(
                            geometries[
                                third_index
                            ]
                        )
                    )
                )

                area = float(
                    triple.area
                )

                checks.append({
                    "first": input_records[
                        first_index
                    ][
                        "name"
                    ],
                    "second": input_records[
                        second_index
                    ][
                        "name"
                    ],
                    "third": input_records[
                        third_index
                    ][
                        "name"
                    ],
                    "triple_overlap_area_m2": round(
                        area,
                        3,
                    ),
                    "classification": (
                        "unsafe_three_way_overlap"
                        if area
                        >= TRIPLE_OVERLAP_BLOCK_AREA_M2
                        else "safe_point_or_no_junction"
                    ),
                    "blocked": (
                        area
                        >= TRIPLE_OVERLAP_BLOCK_AREA_M2
                    ),
                })

    return checks



def synchronize_multiple(
    geojson_paths: list[Path],
) -> dict:
    input_records = []
    ignored_review_inputs = []
    already_synced_inputs = []

    for path in geojson_paths:
        geometry, properties = (
            _load_geojson(
                path
            )
        )

        if _is_review_input(
            path,
            properties,
        ):
            ignored_review_inputs.append(
                str(
                    path
                )
            )
            continue

        if _is_already_synced_input(
            path,
            properties,
        ):
            already_synced_inputs.append(
                str(
                    path
                )
            )
            continue

        input_records.append({
            "path": path,
            "geometry_wgs84": geometry,
            "properties": properties,
            "name": _display_name(
                path,
                properties,
            ),
            "level": _boundary_level(
                properties
            ),
        })

    report: dict[str, Any] = {
        "priority": 12,
        "mode": "topology_safe_shared_edge",
        "created_at_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "decision": "blocked",
        "safe_to_apply": False,
        "supplied_input_count": len(
            geojson_paths
        ),
        "input_count": len(
            input_records
        ),
        "ignored_review_inputs": (
            ignored_review_inputs
        ),
        "rejected_already_synced_inputs": (
            already_synced_inputs
        ),
        "inputs": [
            {
                "file": str(
                    record[
                        "path"
                    ]
                ),
                "name": record[
                    "name"
                ],
                "boundary_level": record[
                    "level"
                ],
            }
            for record
            in input_records
        ],
        "reasons": [],
        "warnings": [],
        "pair_checks": [],
        "junction_checks": [],
        "topology_summary": {},
        "final_checks": {},
    }

    if already_synced_inputs:
        report[
            "reasons"
        ].append(
            "Already-synchronized files were selected. Use the original "
            "unsynced GeoJSON files so Priority 11 can rebuild the common "
            "edge from the real source boundaries."
        )

        return report

    if len(
        input_records
    ) < 2:
        report[
            "reasons"
        ].append(
            "At least two GeoJSON files are required."
        )

        return report

    resolved_paths = [
        record[
            "path"
        ].resolve()
        for record
        in input_records
    ]

    if len(
        set(
            resolved_paths
        )
    ) != len(
        resolved_paths
    ):
        report[
            "reasons"
        ].append(
            "The same GeoJSON file was supplied more than once."
        )

        return report

    normalised_names = [
        _normalise_name(
            record[
                "name"
            ]
        )
        for record
        in input_records
    ]

    duplicate_names = sorted({
        name
        for name
        in normalised_names
        if name
        and normalised_names.count(
            name
        ) > 1
    })

    if duplicate_names:
        report[
            "reasons"
        ].append(
            "Duplicate locality names detected: "
            + ", ".join(
                duplicate_names
            )
        )

        return report

    allowed_levels = {
        "locality",
        "neighbourhood",
        "neighborhood",
        "unknown",
    }

    invalid_levels = [
        record[
            "level"
        ]
        for record
        in input_records
        if record[
            "level"
        ] not in allowed_levels
    ]

    if invalid_levels:
        report[
            "reasons"
        ].append(
            "This tool accepts locality/neighbourhood "
            "boundaries only."
        )

        return report

    known_levels = {
        record[
            "level"
        ]
        for record
        in input_records
        if record[
            "level"
        ] != "unknown"
    }

    if len(
        known_levels
    ) > 1:
        report[
            "reasons"
        ].append(
            "The supplied boundaries have different levels."
        )

        return report

    original_wgs84 = [
        record[
            "geometry_wgs84"
        ]
        for record
        in input_records
    ]

    (
        original_metric,
        reverse_transformer,
        local_crs,
    ) = _project_geometries(
        original_wgs84
    )

    anchors = [
        geometry.representative_point()
        for geometry
        in original_metric
    ]

    meaningful_overlap_pairs = []
    aligned_neighbor_pairs = []
    topology_neighbor_pairs = []
    input_neighbor_degrees = [
        0
        for _ in input_records
    ]

    classification_counts: dict[
        str,
        int,
    ] = {}

    blocking_classifications = {
        "duplicate_shape",
        "parent_child_overlap",
        "broad_overlapping_areas",
        "small_gap_between_neighbors",
        "uncertain_topology",
    }

    for first_index in range(
        len(
            input_records
        )
    ):
        for second_index in range(
            first_index
            + 1,
            len(
                input_records
            ),
        ):
            first_record = input_records[
                first_index
            ]

            second_record = input_records[
                second_index
            ]

            metrics = _topology_metrics(
                first=original_metric[
                    first_index
                ],
                second=original_metric[
                    second_index
                ],
                first_anchor=anchors[
                    first_index
                ],
                second_anchor=anchors[
                    second_index
                ],
            )

            (
                classification,
                explanation,
            ) = _classify_topology_pair(
                metrics
            )

            classification_counts[
                classification
            ] = (
                classification_counts.get(
                    classification,
                    0,
                )
                + 1
            )

            pair_report = {
                "first": first_record[
                    "name"
                ],
                "second": second_record[
                    "name"
                ],
                "classification": classification,
                "explanation": explanation,
                "blocked": (
                    classification
                    in blocking_classifications
                ),
                "overlap_area_m2": round(
                    metrics[
                        "intersection_area_m2"
                    ],
                    3,
                ),
                "first_overlap_percent": round(
                    metrics[
                        "first_overlap_ratio"
                    ]
                    * 100.0,
                    3,
                ),
                "second_overlap_percent": round(
                    metrics[
                        "second_overlap_ratio"
                    ]
                    * 100.0,
                    3,
                ),
                "smaller_polygon_overlap_percent": round(
                    metrics[
                        "smaller_overlap_ratio"
                    ]
                    * 100.0,
                    3,
                ),
                "intersection_over_union_percent": round(
                    metrics[
                        "iou"
                    ]
                    * 100.0,
                    3,
                ),
                "geometry_distance_m": round(
                    metrics[
                        "geometry_distance_m"
                    ],
                    3,
                ),
                "exact_shared_boundary_length_m": round(
                    metrics[
                        "exact_shared_boundary_length_m"
                    ],
                    3,
                ),
                "boundary_proximity_length_m": round(
                    metrics[
                        "boundary_proximity_length_m"
                    ],
                    3,
                ),
                "source_rail_length_m": round(
                    metrics[
                        "source_rail_length_m"
                    ],
                    3,
                ),
                "average_overlap_width_m": round(
                    metrics[
                        "average_overlap_width_m"
                    ],
                    3,
                ),
                "relative_overlap_width": round(
                    metrics[
                        "relative_overlap_width"
                    ],
                    4,
                ),
                "overlap_component_count": int(
                    metrics[
                        "overlap_component_count"
                    ]
                ),
                "overlap_compactness": round(
                    metrics[
                        "overlap_compactness"
                    ],
                    4,
                ),
                "first_anchor_inside_second": bool(
                    metrics[
                        "first_anchor_inside_second"
                    ]
                ),
                "second_anchor_inside_first": bool(
                    metrics[
                        "second_anchor_inside_first"
                    ]
                ),
                "overlap_asymmetry_ratio": round(
                    metrics[
                        "overlap_asymmetry_ratio"
                    ],
                    3,
                ),
            }

            if (
                classification
                == "true_neighbor_overlap"
            ):
                meaningful_overlap_pairs.append(
                    (
                        first_index,
                        second_index,
                    )
                )

                topology_neighbor_pairs.append(
                    (
                        first_index,
                        second_index,
                    )
                )

                input_neighbor_degrees[
                    first_index
                ] += 1

                input_neighbor_degrees[
                    second_index
                ] += 1

            elif (
                classification
                == "already_aligned_neighbor"
            ):
                aligned_neighbor_pairs.append(
                    (
                        first_index,
                        second_index,
                    )
                )

                topology_neighbor_pairs.append(
                    (
                        first_index,
                        second_index,
                    )
                )

                input_neighbor_degrees[
                    first_index
                ] += 1

                input_neighbor_degrees[
                    second_index
                ] += 1

            elif (
                classification
                in blocking_classifications
            ):
                report[
                    "reasons"
                ].append(
                    f"{first_record['name']} <-> "
                    f"{second_record['name']}: "
                    f"{explanation}"
                )

            report[
                "pair_checks"
            ].append(
                pair_report
            )

    junction_checks = _triple_overlap_checks(
        original_metric,
        input_records,
    )

    report[
        "junction_checks"
    ] = junction_checks

    for junction in junction_checks:
        if junction[
            "blocked"
        ]:
            report[
                "reasons"
            ].append(
                "Unsafe three-way overlap: "
                f"{junction['first']}, "
                f"{junction['second']} and "
                f"{junction['third']} overlap across "
                f"{junction['triple_overlap_area_m2']} m²."
            )

    isolated_inputs = [
        input_records[
            index
        ][
            "name"
        ]
        for index, degree
        in enumerate(
            input_neighbor_degrees
        )
        if degree == 0
    ]

    if isolated_inputs:
        report[
            "reasons"
        ].append(
            "These selected areas are not confirmed neighbors "
            "of any other selected area: "
            + ", ".join(
                isolated_inputs
            )
        )

    report[
        "topology_summary"
    ] = {
        "classification_counts": (
            classification_counts
        ),
        "true_neighbor_overlap_pair_count": len(
            meaningful_overlap_pairs
        ),
        "already_aligned_neighbor_pair_count": len(
            aligned_neighbor_pairs
        ),
        "confirmed_neighbor_pair_count": len(
            topology_neighbor_pairs
        ),
        "isolated_inputs": isolated_inputs,
        "decision_rule": (
            "Only true_neighbor_overlap and "
            "already_aligned_neighbor pairs are allowed."
        ),
    }

    if report[
        "reasons"
    ]:
        return report

    if not topology_neighbor_pairs:
        report[
            "reasons"
        ].append(
            "No genuine neighboring relationships were found "
            "among the supplied boundaries."
        )

        return report

    original_union = _polygonal(
        make_valid(
            unary_union(
                original_metric
            )
        )
    )

    (
        final_metric,
        partition_diagnostics,
    ) = _partition_union(
        original_metric,
        anchors,
    )

    for index, geometry in enumerate(
        final_metric
    ):
        if geometry.is_empty:
            report[
                "reasons"
            ].append(
                f"{input_records[index]['name']} became empty."
            )

            continue

        if not geometry.is_valid:
            report[
                "reasons"
            ].append(
                f"{input_records[index]['name']} is invalid "
                "after synchronization."
            )

        if not geometry.covers(
            anchors[index]
        ):
            report[
                "reasons"
            ].append(
                f"{input_records[index]['name']} lost its "
                "locality anchor."
            )

        significant_component_count = (
            _significant_component_count(
                geometry
            )
        )

        interior_hole_count = (
            _interior_hole_count(
                geometry
            )
        )

        input_records[
            index
        ][
            "significant_component_count"
        ] = significant_component_count

        input_records[
            index
        ][
            "interior_hole_count"
        ] = interior_hole_count

        if (
            significant_component_count
            > MAX_FINAL_SIGNIFICANT_COMPONENTS
        ):
            report[
                "reasons"
            ].append(
                f"{input_records[index]['name']} split into "
                "multiple significant polygon pieces."
            )

        if (
            interior_hole_count
            > MAX_FINAL_INTERIOR_HOLES
        ):
            report[
                "reasons"
            ].append(
                f"{input_records[index]['name']} developed "
                "an internal loop/hole."
            )

        area_change_ratio = (
            abs(
                geometry.area
                - original_metric[
                    index
                ].area
            )
            / max(
                original_metric[
                    index
                ].area,
                1.0,
            )
        )

        perimeter_increase_ratio = (
            max(
                0.0,
                geometry.length
                - original_metric[
                    index
                ].length,
            )
            / max(
                original_metric[
                    index
                ].length,
                1.0,
            )
        )

        input_records[
            index
        ][
            "area_change_ratio"
        ] = area_change_ratio

        input_records[
            index
        ][
            "perimeter_increase_ratio"
        ] = perimeter_increase_ratio

        if (
            area_change_ratio
            > MAX_AREA_CHANGE_RATIO
        ):
            report[
                "reasons"
            ].append(
                f"{input_records[index]['name']} changed "
                "too much in area."
            )

        if (
            perimeter_increase_ratio
            > MAX_PERIMETER_INCREASE_RATIO
        ):
            report[
                "reasons"
            ].append(
                f"{input_records[index]['name']} became "
                "excessively complex."
            )

    final_union = _polygonal(
        make_valid(
            unary_union(
                final_metric
            )
        )
    )

    union_change_area = float(
        original_union.symmetric_difference(
            final_union
        ).area
    )

    union_change_ratio = (
        union_change_area
        / max(
            original_union.area,
            1.0,
        )
    )

    maximum_remaining_overlap = 0.0
    shared_edges = []

    for first_index in range(
        len(
            input_records
        )
    ):
        for second_index in range(
            first_index
            + 1,
            len(
                input_records
            ),
        ):
            overlap_after = float(
                final_metric[
                    first_index
                ].intersection(
                    final_metric[
                        second_index
                    ]
                ).area
            )

            maximum_remaining_overlap = max(
                maximum_remaining_overlap,
                overlap_after,
            )

            original_overlap = float(
                original_metric[
                    first_index
                ].intersection(
                    original_metric[
                        second_index
                    ]
                ).area
            )

            if (
                (
                    first_index,
                    second_index,
                )
                in meaningful_overlap_pairs
            ):
                shared_edge = _linear(
                    final_metric[
                        first_index
                    ].boundary.intersection(
                        final_metric[
                            second_index
                        ].boundary
                    )
                )

                shared_edge_length = float(
                    shared_edge.length
                )

                source_metrics = (
                    _shared_edge_source_metrics(
                        shared_edge=shared_edge,
                        first_geometry=original_metric[
                            first_index
                        ],
                        second_geometry=original_metric[
                            second_index
                        ],
                        original_overlap=original_metric[
                            first_index
                        ].intersection(
                            original_metric[
                                second_index
                            ]
                        ),
                    )
                )

                shared_edges.append({
                    "first_index": (
                        first_index
                    ),
                    "second_index": (
                        second_index
                    ),
                    "geometry_metric": (
                        shared_edge
                    ),
                    "length_m": (
                        shared_edge_length
                    ),
                    "source_metrics": (
                        source_metrics
                    ),
                })

                if (
                    shared_edge_length
                    < MIN_SHARED_EDGE_LENGTH_M
                ):
                    report[
                        "reasons"
                    ].append(
                        f"{input_records[first_index]['name']} "
                        "and "
                        f"{input_records[second_index]['name']} "
                        "did not receive a meaningful shared edge."
                    )

                if (
                    source_metrics[
                        "mean_balance_error"
                    ]
                    > MAX_SHARED_EDGE_BALANCE_ERROR
                ):
                    report[
                        "reasons"
                    ].append(
                        f"{input_records[first_index]['name']} and "
                        f"{input_records[second_index]['name']} received "
                        "a shared edge that is not centred between the "
                        "two original source boundaries."
                    )

                if (
                    source_metrics[
                        "mean_source_distance_m"
                    ]
                    > MAX_SHARED_EDGE_MEAN_SOURCE_DISTANCE_M
                ):
                    report[
                        "reasons"
                    ].append(
                        f"{input_records[first_index]['name']} and "
                        f"{input_records[second_index]['name']} received "
                        "a shared edge too far from the original outlines."
                    )

                if (
                    source_metrics[
                        "source_length_ratio"
                    ]
                    < MIN_SHARED_EDGE_SOURCE_LENGTH_RATIO
                ):
                    report[
                        "reasons"
                    ].append(
                        f"{input_records[first_index]['name']} and "
                        f"{input_records[second_index]['name']} produced "
                        "a suspicious shortcut instead of a full common edge."
                    )

    shared_edge_crossing_checks = []

    for first_edge_index in range(
        len(
            shared_edges
        )
    ):
        for second_edge_index in range(
            first_edge_index
            + 1,
            len(
                shared_edges
            ),
        ):
            first_edge_record = shared_edges[
                first_edge_index
            ]

            second_edge_record = shared_edges[
                second_edge_index
            ]

            crossing = first_edge_record[
                "geometry_metric"
            ].intersection(
                second_edge_record[
                    "geometry_metric"
                ]
            )

            crossing_line_length = float(
                _linear(
                    crossing
                ).length
            )

            blocked_crossing = (
                crossing_line_length
                > NUMERICAL_SLIVER_M2
            )

            shared_edge_crossing_checks.append({
                "first_pair": [
                    input_records[
                        first_edge_record[
                            "first_index"
                        ]
                    ][
                        "name"
                    ],
                    input_records[
                        first_edge_record[
                            "second_index"
                        ]
                    ][
                        "name"
                    ],
                ],
                "second_pair": [
                    input_records[
                        second_edge_record[
                            "first_index"
                        ]
                    ][
                        "name"
                    ],
                    input_records[
                        second_edge_record[
                            "second_index"
                        ]
                    ][
                        "name"
                    ],
                ],
                "overlapping_line_length_m": round(
                    crossing_line_length,
                    3,
                ),
                "blocked": blocked_crossing,
            })

            if blocked_crossing:
                report[
                    "reasons"
                ].append(
                    "Two generated shared edges overlap each other "
                    "as lines, creating an unsafe junction."
                )

    if (
        maximum_remaining_overlap
        > MAX_REMAINING_PAIR_OVERLAP_M2
    ):
        report[
            "reasons"
        ].append(
            "Some synchronized polygons still overlap."
        )

    if (
        union_change_ratio
        > MAX_UNION_CHANGE_RATIO
    ):
        report[
            "reasons"
        ].append(
            "The synchronization changed the combined "
            "covered area."
        )

    report[
        "final_checks"
    ] = {
        "projected_crs": (
            local_crs.to_string()
        ),
        "original_union_area_m2": round(
            original_union.area,
            3,
        ),
        "final_union_area_m2": round(
            final_union.area,
            3,
        ),
        "union_change_area_m2": round(
            union_change_area,
            6,
        ),
        "union_change_percent": round(
            union_change_ratio
            * 100.0,
            6,
        ),
        "maximum_remaining_pair_overlap_m2": round(
            maximum_remaining_overlap,
            6,
        ),
        "meaningful_overlap_pair_count": len(
            meaningful_overlap_pairs
        ),
        "already_aligned_neighbor_pair_count": len(
            aligned_neighbor_pairs
        ),
        "confirmed_neighbor_pair_count": len(
            topology_neighbor_pairs
        ),
        "shared_edge_crossing_checks": (
            shared_edge_crossing_checks
        ),
        "gap_area_m2": round(
            float(
                original_union.difference(
                    final_union
                ).area
            ),
            6,
        ),
        "partition_method": (
            "source_boundary_distance_raster_partition"
        ),
        "partition_faces": partition_diagnostics,
        "shared_edges": [
            {
                "first": input_records[
                    edge[
                        "first_index"
                    ]
                ][
                    "name"
                ],
                "second": input_records[
                    edge[
                        "second_index"
                    ]
                ][
                    "name"
                ],
                "length_m": round(
                    edge[
                        "length_m"
                    ],
                    3,
                ),
                "exact_same_coordinates": True,
                "mean_balance_error": round(
                    edge.get(
                        "source_metrics",
                        {},
                    ).get(
                        "mean_balance_error",
                        0.0,
                    ),
                    4,
                ),
                "mean_source_distance_m": round(
                    edge.get(
                        "source_metrics",
                        {},
                    ).get(
                        "mean_source_distance_m",
                        0.0,
                    ),
                    3,
                ),
                "max_source_distance_m": round(
                    edge.get(
                        "source_metrics",
                        {},
                    ).get(
                        "max_source_distance_m",
                        0.0,
                    ),
                    3,
                ),
                "source_length_ratio": round(
                    edge.get(
                        "source_metrics",
                        {},
                    ).get(
                        "source_length_ratio",
                        0.0,
                    ),
                    4,
                ),
            }
            for edge
            in shared_edges
        ],
        "areas": [
            {
                "name": input_records[
                    index
                ][
                    "name"
                ],
                "original_area_m2": round(
                    original_metric[
                        index
                    ].area,
                    3,
                ),
                "final_area_m2": round(
                    final_metric[
                        index
                    ].area,
                    3,
                ),
                "area_change_percent": round(
                    input_records[
                        index
                    ][
                        "area_change_ratio"
                    ]
                    * 100.0,
                    3,
                ),
                "perimeter_increase_percent": round(
                    input_records[
                        index
                    ][
                        "perimeter_increase_ratio"
                    ]
                    * 100.0,
                    3,
                ),
                "significant_component_count": int(
                    input_records[
                        index
                    ].get(
                        "significant_component_count",
                        0,
                    )
                ),
                "interior_hole_count": int(
                    input_records[
                        index
                    ].get(
                        "interior_hole_count",
                        0,
                    )
                ),
            }
            for index
            in range(
                len(
                    input_records
                )
            )
        ],
    }

    if report[
        "reasons"
    ]:
        return report

    report[
        "decision"
    ] = "safe_to_apply"

    report[
        "safe_to_apply"
    ] = True

    report[
        "_input_records"
    ] = input_records

    report[
        "_final_wgs84"
    ] = [
        _polygonal(
            make_valid(
                transform(
                    reverse_transformer.transform,
                    geometry,
                )
            )
        )
        for geometry
        in final_metric
    ]

    report[
        "_shared_edges_wgs84"
    ] = [
        {
            "first_index": edge[
                "first_index"
            ],
            "second_index": edge[
                "second_index"
            ],
            "length_m": edge[
                "length_m"
            ],
            "geometry": transform(
                reverse_transformer.transform,
                edge[
                    "geometry_metric"
                ],
            ),
        }
        for edge
        in shared_edges
    ]

    return report


# -------------------------------------------------------------------------
# Writers
# -------------------------------------------------------------------------

def _public_report(
    report: dict,
) -> dict:
    return {
        key: value
        for key, value
        in report.items()
        if not key.startswith(
            "_"
        )
    }


def _write_report(
    path: Path,
    report: dict,
) -> None:
    with path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            _public_report(
                report
            ),
            file,
            ensure_ascii=False,
            indent=2,
        )


def _polygon_kml_xml(
    geometry: Any,
) -> str:
    def polygon_xml(
        polygon: Polygon,
    ) -> str:
        outer_coordinates = " ".join(
            f"{longitude:.8f},{latitude:.8f},0"
            for longitude, latitude
            in polygon.exterior.coords
        )

        inner_xml = []

        for interior in polygon.interiors:
            inner_coordinates = " ".join(
                f"{longitude:.8f},{latitude:.8f},0"
                for longitude, latitude
                in interior.coords
            )

            inner_xml.append(
                "<innerBoundaryIs><LinearRing>"
                f"<coordinates>{inner_coordinates}</coordinates>"
                "</LinearRing></innerBoundaryIs>"
            )

        return (
            "<Polygon><tessellate>1</tessellate>"
            "<outerBoundaryIs><LinearRing>"
            f"<coordinates>{outer_coordinates}</coordinates>"
            "</LinearRing></outerBoundaryIs>"
            + "".join(
                inner_xml
            )
            + "</Polygon>"
        )

    if isinstance(
        geometry,
        Polygon,
    ):
        return polygon_xml(
            geometry
        )

    if isinstance(
        geometry,
        MultiPolygon,
    ):
        return (
            "<MultiGeometry>"
            + "".join(
                polygon_xml(
                    polygon
                )
                for polygon
                in geometry.geoms
            )
            + "</MultiGeometry>"
        )

    raise ValueError(
        "KML output requires Polygon or MultiPolygon geometry."
    )


def _write_geojson(
    path: Path,
    geometry: Any,
    properties: dict,
    synchronized_group: list[str],
    report_name: str,
) -> None:
    output_properties = dict(
        properties
    )

    output_properties.update({
        "priority12_topology_safe_synced": True,
        "synchronized_group": synchronized_group,
        "sync_report": report_name,
        "sync_method": (
            "source_boundary_distance_raster_partition"
        ),
    })

    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": (
                output_properties
            ),
            "geometry": mapping(
                geometry
            ),
        }],
    }

    with path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            payload,
            file,
            ensure_ascii=False,
            indent=2,
        )


def _write_kml(
    path: Path,
    geometry: Any,
    name: str,
    line_color: str,
    fill_color: str,
) -> None:
    geometry_xml = (
        _polygon_kml_xml(
            geometry
        )
    )

    safe_name = escape(
        name
    )

    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        '<Document>\n'
        f'  <name>{safe_name}</name>\n'
        '  <Style id="multiSyncedBoundaryStyle">\n'
        '    <LineStyle>\n'
        f'      <color>{line_color}</color>\n'
        '      <width>3</width>\n'
        '    </LineStyle>\n'
        '    <PolyStyle>\n'
        f'      <color>{fill_color}</color>\n'
        '      <fill>1</fill>\n'
        '      <outline>1</outline>\n'
        '    </PolyStyle>\n'
        '  </Style>\n'
        '  <Placemark>\n'
        f'    <name>{safe_name}</name>\n'
        '    <styleUrl>#multiSyncedBoundaryStyle</styleUrl>\n'
        f'    {geometry_xml}\n'
        '  </Placemark>\n'
        '</Document>\n'
        '</kml>\n'
    )

    path.write_text(
        kml,
        encoding="utf-8",
    )


def _write_kmz(
    kml_path: Path,
) -> Path:
    kmz_path = kml_path.with_suffix(
        ".kmz"
    )

    with zipfile.ZipFile(
        kmz_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.write(
            kml_path,
            arcname="doc.kml",
        )

    return kmz_path


def _write_shared_edges(
    path: Path,
    shared_edges: list[dict],
    input_records: list[dict],
) -> None:
    features = []

    for edge in shared_edges:
        features.append({
            "type": "Feature",
            "properties": {
                "first": input_records[
                    edge[
                        "first_index"
                    ]
                ][
                    "name"
                ],
                "second": input_records[
                    edge[
                        "second_index"
                    ]
                ][
                    "name"
                ],
                "length_m": round(
                    edge[
                        "length_m"
                    ],
                    3,
                ),
                "priority12_shared_edge": True,
            },
            "geometry": mapping(
                edge[
                    "geometry"
                ]
            ),
        })

    payload = {
        "type": "FeatureCollection",
        "features": features,
    }

    with path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            payload,
            file,
            ensure_ascii=False,
            indent=2,
        )


# -------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------

def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely synchronize two or more overlapping "
            "locality GeoJSON boundaries in one operation."
        )
    )

    parser.add_argument(
        "geojson_files",
        nargs="+",
        type=Path,
        help=(
            "Two or more locality GeoJSON files"
        ),
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Create new synchronized GeoJSON/KML/KMZ files. "
            "Original files are never modified."
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Root output folder. Default: the first "
            "GeoJSON file's folder."
        ),
    )

    args = parser.parse_args(argv)

    if len(
        args.geojson_files
    ) < 2:
        parser.error(
            "Provide at least two GeoJSON files."
        )

    paths = [
        path.resolve()
        for path
        in args.geojson_files
    ]

    for path in paths:
        if not path.exists():
            print(
                f"ERROR: File not found: {path}"
            )

            return 1

        if path.suffix.lower() not in {
            ".geojson",
            ".json",
        }:
            print(
                "ERROR: Use GeoJSON files, not KML files: "
                f"{path.name}"
            )

            return 1

    output_root = (
        args.output_dir.resolve()
        if args.output_dir
        else paths[0].parent
    )

    output_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    run_timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    report_path = output_root / (
        "priority12_topology_report_"
        f"{run_timestamp}.json"
    )

    try:
        report = synchronize_multiple(
            paths
        )
    except Exception as exc:
        report = {
            "priority": 12,
            "mode": "topology_safe_shared_edge",
            "decision": "error",
            "safe_to_apply": False,
            "reasons": [
                str(
                    exc
                )
            ],
        }

    _write_report(
        report_path,
        report,
    )

    print(
        "="
        * 72
    )

    print(
        "PRIORITY 12 - TOPOLOGY-SAFE SYNCHRONIZATION"
    )

    print(
        "="
        * 72
    )

    print(
        f"Areas supplied: {len(paths)}"
    )

    for index, path in enumerate(
        paths,
        start=1,
    ):
        print(
            f"  {index}. {path.name}"
        )

    print(
        f"Report: {report_path}"
    )

    ignored_review_inputs = report.get(
        "ignored_review_inputs",
        [],
    )

    if ignored_review_inputs:
        print(
            "Ignored REVIEW files:"
        )

        for ignored_file in ignored_review_inputs:
            print(
                f"  - {Path(ignored_file).name}"
            )

        print()

    pair_checks = report.get(
        "pair_checks",
        [],
    )

    if pair_checks:
        print(
            "Topology classification:"
        )

        for pair in pair_checks:
            print(
                f"  {pair['first']}  <->  {pair['second']}: "
                f"{pair['classification']}"
            )

        print()

    junction_checks = report.get(
        "junction_checks",
        [],
    )

    blocked_junctions = [
        junction
        for junction
        in junction_checks
        if junction.get(
            "blocked",
            False,
        )
    ]

    if blocked_junctions:
        print(
            "Unsafe junctions:"
        )

        for junction in blocked_junctions:
            print(
                f"  {junction['first']} / "
                f"{junction['second']} / "
                f"{junction['third']}: "
                f"{junction['triple_overlap_area_m2']} m²"
            )

        print()

    final_checks = report.get(
        "final_checks"
    ) or {}

    if final_checks:
        print(
            "Meaningful overlap pairs: "
            f"{final_checks.get('meaningful_overlap_pair_count', 0)}"
        )

        print(
            "Maximum remaining overlap: "
            f"{final_checks.get('maximum_remaining_pair_overlap_m2', 0)} m²"
        )

        print(
            "Combined-area change: "
            f"{final_checks.get('union_change_percent', 0)}%"
        )

        print()

        for area in final_checks.get(
            "areas",
            [],
        ):
            print(
                f"{area['name']}: "
                f"area change {area['area_change_percent']}%"
            )

        print()

    if not report.get(
        "safe_to_apply"
    ):
        print(
            "RESULT: BLOCKED FOR SAFETY"
        )

        for reason in report.get(
            "reasons"
        ) or []:
            print(
                f"  - {reason}"
            )

        return 2

    print(
        "RESULT: SAFE TO SYNCHRONIZE"
    )

    if not args.apply:
        print(
            "Dry run only. No boundary files were changed or created."
        )

        print(
            "Run the same command with --apply after "
            "reviewing the report."
        )

        return 0

    run_dir = output_root / (
        "priority12_synced_"
        f"{run_timestamp}"
    )

    run_dir.mkdir(
        parents=True,
        exist_ok=False,
    )

    input_records = report[
        "_input_records"
    ]

    final_wgs84 = report[
        "_final_wgs84"
    ]

    shared_edges = report[
        "_shared_edges_wgs84"
    ]

    group_names = [
        record[
            "name"
        ]
        for record
        in input_records
    ]

    line_colors = [
        "ff0000ff",
        "ffff0000",
        "ff00aa00",
        "ff00a5ff",
        "ffff00ff",
        "ffffff00",
        "ff8844ff",
        "ff44ff88",
    ]

    fill_colors = [
        "330000ff",
        "33ff0000",
        "3300aa00",
        "3300a5ff",
        "33ff00ff",
        "33ffff00",
        "338844ff",
        "3344ff88",
    ]

    print()

    print(
        f"Output folder: {run_dir}"
    )

    for index, record in enumerate(
        input_records
    ):
        base_stem = _safe_stem(
            record[
                "path"
            ].stem
        )

        geojson_output = run_dir / (
            f"{base_stem}_synced.geojson"
        )

        kml_output = run_dir / (
            f"{base_stem}_synced.kml"
        )

        _write_geojson(
            geojson_output,
            final_wgs84[
                index
            ],
            record[
                "properties"
            ],
            synchronized_group=group_names,
            report_name=report_path.name,
        )

        _write_kml(
            kml_output,
            final_wgs84[
                index
            ],
            (
                record[
                    "name"
                ]
                + " - Synced"
            ),
            line_color=line_colors[
                index
                % len(
                    line_colors
                )
            ],
            fill_color=fill_colors[
                index
                % len(
                    fill_colors
                )
            ],
        )

        kmz_output = _write_kmz(
            kml_output
        )

        print(
            f"  Created: {geojson_output.name}"
        )

        print(
            f"  Created: {kml_output.name}"
        )

        print(
            f"  Created: {kmz_output.name}"
        )

    shared_edge_path = run_dir / (
        "shared_edges.geojson"
    )

    _write_shared_edges(
        shared_edge_path,
        shared_edges,
        input_records,
    )

    final_report_path = run_dir / (
        report_path.name
    )

    _write_report(
        final_report_path,
        report,
    )

    print(
        f"  Created: {shared_edge_path.name}"
    )

    print(
        f"  Created: {final_report_path.name}"
    )

    print()

    print(
        "Original files were not modified."
    )

    print(
        "In Google Earth, open only the new *_synced.kml files "
        "from the output folder."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
