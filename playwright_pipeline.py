
"""
screenshot_pipeline.py
======================
Full pipeline: User Location → Google Maps → Screenshot → GeoTIFF
               → Boundary Detection (Google visual outline) → Polygon
               → Exact Google Original GeoJSON/KML/KMZ
               → Optional derived synchronization

FIXES vs original
-----------------
BUG 1  fetch_osm_boundary   filtered=[] block was at wrong indent (4-space),
                             ran even when parsed.locality was falsy and
                             `elements` was undefined → moved inside locality block.
BUG 2  fetch_osm_boundary   `elements = all_elements` was set inside the inner
                             for-loop; moved after the loop so it's set once.
BUG 3  fetch_osm_boundary   `node_fallback = None` declared inside
                             `if parsed.locality:` but `if node_fallback:` check
                             was outside → NameError when no locality.
                             Fix: declare node_fallback = None at function top.
BUG 4  _best_element        `q_tokens` / `n_tokens` assigned only inside
                             `if name_q:` but `overlap` used them outside that
                             block → silent NameError (swallowed by try/except),
                             causing every element to be skipped when name_q=''.
                             Fix: guard overlap inside the same if name_q block,
                             skip element when name_q is empty.
BUG 5  fetch_osm_boundary   `continue` statements had 20-space indent (5 levels)
                             inside a 2-level block — fragile and unreadable.
                             Fix: normalised all indentation to 4-space multiples.
BUG 6  choose_polygon       locality osm_node_buffer branch logged a message
                             but never returned → fell through, ring was lost.
                             Fix: added explicit return after extracting the ring.
BUG 7  parse_query           Dead state-code rescan inside the 3-part regex branch
                             (copy-paste from generic branch, could never match).
                             Fix: removed dead block.
BUG 8  _write_geotiff_raw   tag_offset for GeoKeyDirectoryTag (34736) used
                             type 3 (SHORT) with count=16, but the packed struct
                             is already 32 bytes (16 x uint16).  The TIFF spec
                             requires type=3 SHORT here so count must equal the
                             number of uint16 values (16) — this was actually
                             correct; no change needed (documented for clarity).
"""

from __future__ import annotations
import time
import sys

# Windows CMD may use cp1252 when stdout/stderr are redirected to a file.
# Force UTF-8 so symbols such as arrows and status icons do not crash logging.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(
        encoding="utf-8",
        errors="replace",
    )

import heapq
import json
import math
import os
import re
import struct
import urllib.parse
import urllib.request
import zipfile
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np



HTTP_HEADERS   = {"User-Agent": "JarvisBoundaryPipeline/2.0 (educational)"}

# Keep False until Google Maps screenshots render correctly.
# After a successful visible test, change this to True and test again.
BOUNDARY_HEADLESS = True

# Satellite boundary mode.
#
# Google draws the locality outline as a nearly continuous line over
# satellite imagery, while the default map view uses widely spaced dots that
# vanish under highways and label text. Measured on the same north edge:
# map view covered 32% of the edge with a 257px hole; satellite covered 51%
# with a worst gap of 5px.
#
# Satellite imagery is red-brown in much of India, so a much stricter colour
# rule is required. If satellite capture fails to produce a boundary, the
# pipeline automatically falls back to the proven map view.
SATELLITE_BOUNDARY_MODE = True
SATELLITE_AUTO_FALLBACK_TO_MAP = True
ACTIVE_SATELLITE_VIEW = False

# Satellite URLs encode the camera as @lat,lng,<metres>m instead of
# @lat,lng,<zoom>z. Converting metres to zoom needs the rendered map height,
# which is measured on every capture.
LAST_MAP_VIEWPORT_HEIGHT_CSS = 732.0

# Derived from Google Maps satellite pixels of the locality outline:
# BGR around (53, 109, 231), saturation ~196, value ~231.
SATELLITE_RED_MIN_RED = 190.0
SATELLITE_RED_MIN_RED_MINUS_GREEN = 70.0
SATELLITE_RED_MIN_RED_MINUS_BLUE = 90.0
SATELLITE_RED_MIN_SATURATION = 110
SATELLITE_RED_MIN_VALUE = 190

# Satellite selected-place outlines are now often rendered as vivid yellow
# rather than only orange/red. Keep this rule strict enough to avoid normal
# roads and roofs while preserving the solid centre of the selected outline.
SATELLITE_YELLOW_MIN_RED = 220.0
SATELLITE_YELLOW_MIN_GREEN = 170.0
SATELLITE_YELLOW_MAX_BLUE = 150.0
SATELLITE_YELLOW_MIN_HUE = 15
SATELLITE_YELLOW_MAX_HUE = 45
SATELLITE_YELLOW_MIN_SATURATION = 100
SATELLITE_YELLOW_MIN_VALUE = 220

# Priority 15.5.4.8 — satellite label/POI occlusion repair.
# Google draws road names and POI markers above the selected boundary. A local
# gap may use those overlay pixels as support only when its endpoint directions,
# radial position, path directness and final polygon geometry are all safe.
SATELLITE_LABEL_OCCLUSION_REPAIR_ENABLED = True
SATELLITE_LABEL_OCCLUSION_MAX_GAP_PX = {
    "small": 85.0,
    "medium": 125.0,
    "large": 145.0,
}
SATELLITE_LABEL_OCCLUSION_MIN_RATIO = 0.16
SATELLITE_LABEL_OCCLUSION_MIN_COMBINED_SUPPORT_RATIO = 0.70
SATELLITE_LABEL_OCCLUSION_MAX_COMBINED_MEAN_DISTANCE_PX = 8.0
SATELLITE_LABEL_OCCLUSION_MAX_PATH_LENGTH_RATIO = 1.60
SATELLITE_LABEL_OCCLUSION_MIN_ENDPOINT_DIRECTION_COS = 0.24
SATELLITE_LABEL_OCCLUSION_MIN_DIRECTION_SUM = 0.95
SATELLITE_LABEL_OCCLUSION_MAX_RADIAL_DIFFERENCE_RATIO = 0.78
SATELLITE_LABEL_OCCLUSION_MAX_RADIAL_DIFFERENCE_PX = 62.0
SATELLITE_LABEL_OCCLUSION_MIN_PATH_RADIUS_RATIO = 0.68
SATELLITE_LABEL_OCCLUSION_PATH_COST = 0.90
SATELLITE_LABEL_OCCLUSION_SUPPORT_DISTANCE_PX = 8.0

# Satellite frames carry more warm terrain, so the flood guard needs headroom.
SATELLITE_RED_FLOOD_MAX_MAP_RED_RATIO = 0.020

# Priority 4: compare a new locality polygon with existing GeoJSON boundaries.
OVERLAP_VALIDATION_ENABLED = True

# Priority 13 — Exact Google Mode.
#
# Accepted Google visual polygons are exported unchanged even when two Google
# locality results overlap. Overlap is recorded as a warning only. Geometry
# validity, target containment, multi-zoom agreement and self-crossing checks
# remain strict and can still block export.
EXACT_GOOGLE_MODE = True
EXACT_GOOGLE_OVERLAP_POLICY = "warning_only"

# Keep the source Google traces separate from review and derived outputs.
EXACT_GOOGLE_ORIGINAL_DIRNAME = "google_original"
EXACT_GOOGLE_REVIEW_DIRNAME = "review_candidates"
EXACT_GOOGLE_REPORT_DIRNAME = "reports"

# Priority 14 — georeference correction.
#
# The old converter used a simple linear latitude/longitude scale from the
# camera centre. Google Maps is Web Mercator, so image pixels must be converted
# through Web Mercator world pixels. This is especially important when Windows
# display scaling creates dpr=1.25 screenshots and css/image=0.8.
GEOREFERENCE_MODE = "web_mercator_dpr_corrected"
GEOREFERENCE_REPORT_ENABLED = True
KML_COORDINATE_DECIMALS = 7

# Priority 15 — India-wide adaptive locality detection.
#
# Small colonies, medium localities and large sectors cannot use one fixed
# zoom plan, ROI and area threshold. The active profile is chosen from
# Google's selected zoom and updated from actual CV diagnostics.
ADAPTIVE_LOCALITY_MODE = True
ADAPTIVE_MAX_CAPTURE_COUNT = 5

LOCALITY_PROFILES = {
    "small": {
        "initial_zoom": 16,
        "zoom_plan": [16, 15, 14, 13],
        "single_min_coverage": 0.70,
        "single_min_close_to_red": 0.94,
        "single_max_mean_red_distance_px": 4.5,
    },
    "medium": {
        "initial_zoom": 15,
        "zoom_plan": [15, 14, 13],
        "single_min_coverage": 0.78,
        "single_min_close_to_red": 0.93,
        "single_max_mean_red_distance_px": 4.5,
    },
    "large": {
        "initial_zoom": 14,
        "zoom_plan": [14, 13, 12, 15],
        "single_min_coverage": 0.62,   # relaxed: large localities have broken outlines
        "single_min_close_to_red": 0.78,  # relaxed: map labels occlude the red line
        "single_max_mean_red_distance_px": 6.0,  # relaxed for large profile
    },
}

ACTIVE_LOCALITY_PROFILE = "medium"
ACTIVE_LOCALITY_PROFILE_REASON = "default"
ACTIVE_CAPTURE_ZOOM = 15.0

# Priority 15.1 profile-specific exact-repair safety limits.
#
# Large boundaries can lose more red-supported vertices while safely repairing
# long dotted-line gaps. The lower large-profile threshold is used only when
# the visible outline has very high angular coverage and all geometry safety
# checks pass.
PROFILE_RETAINED_SUPPORT_LIMITS = {
    "small": 0.72,
    "medium": 0.70,
    "large": 0.68,
}

PROFILE_MIN_COVERAGE_FOR_LOW_SUPPORT = {
    "small": 0.60,
    "medium": 0.58,
    "large": 0.52,
}

PROFILE_MAX_EXACT_AREA_CHANGE = {
    "small": 0.055,
    "medium": 0.050,
    "large": 0.045,
}

PROFILE_MAX_MEAN_SNAP_DISTANCE_PX = {
    "small": 6.5,
    "medium": 5.5,
    "large": 5.0,
}

# Priority 15.2 — cross-zoom confirmation for max-step-only candidates.
# A candidate can enter exact repair when all checks pass except max_step and
# its visible Google-red evidence is excellent. It remains PROVISIONAL and can
# never be exported without a strongly matching independent zoom.
GAP_PROVISIONAL_ENABLED = True
GAP_PROVISIONAL_MIN_COVERAGE = {
    "small": 0.50,
    "medium": 0.48,
    "large": 0.45,
}
GAP_PROVISIONAL_MIN_CLOSE_TO_RED = {
    "small": 0.75,
    "medium": 0.70,
    "large": 0.65,
}
GAP_PROVISIONAL_MAX_MEAN_RED_DISTANCE_PX = {
    "small": 8.0,
    "medium": 9.5,
    "large": 12.0,
}
GAP_PROVISIONAL_MAX_PRE_REPAIR_STEP_PX = {
    "small": 110.0,
    "medium": 125.0,
    "large": 145.0,
}
GAP_PROVISIONAL_MIN_SNAPPED_BINS = {
    "small": 80,
    "medium": 75,
    "large": 70,
}
GAP_PROVISIONAL_MAX_RAW_OUTLIERS = 6
GAP_CONFIRMATION_MIN_IOU = 0.78
GAP_CONFIRMATION_MIN_SMALLER_OVERLAP = 0.90
GAP_CONFIRMATION_MAX_AREA_RATIO_DIFFERENCE = 0.22

# Priority 15.3 — detailed framing and final-zoom lock.
#
# Low zooms are useful for confirming the overall shape, but they must not
# become the final source when Google originally opened a compact urban
# locality near zoom 15. This prevents a tiny zoom-13 outline from producing
# an oversized/incorrect final KML.
INITIAL_GOOGLE_LOCALITY_ZOOM = 15.0
LAST_MULTI_ZOOM_SELECTION = {}

FRAMING_MIN_MAX_SPAN_RATIO = 0.19
FRAMING_MIN_BBOX_AREA_RATIO = 0.024
FRAMING_MAX_MAX_SPAN_RATIO = 0.92
FRAMING_EDGE_MARGIN_RATIO = 0.012

# For a locality Google originally opens at zoom 15, zoom 12 is too far out
# for normal urban-sector confirmation. Zoom 13 remains confirmation-only.
SKIP_ZOOM12_WHEN_INITIAL_ZOOM_AT_LEAST = 14.75

# Detailed final source floor derived from Google's own selected framing.
FINAL_ZOOM_FLOOR_BY_INITIAL_VIEW = (
    (14.75, 14.0),
    (13.75, 13.0),
    (0.00, 12.0),
)

# Priority 15.4 — target-centred detailed recapture.
#
# Google may keep the selected locality far from the visual map centre after
# the side panel closes. The old function only printed a warning. This version
# pans the existing map canvas with real pointer actions, without reloading or
# changing the selected place URL.
TARGET_CENTERING_ENABLED = True
TARGET_CENTER_TOLERANCE_X_PX = 65.0
TARGET_CENTER_TOLERANCE_Y_PX = 55.0
TARGET_CENTER_PREFERRED_RESIDUAL_X_PX = 42.0
TARGET_CENTER_PREFERRED_RESIDUAL_Y_PX = 38.0
TARGET_CENTER_MAX_DRAG_X_PX = 220.0
TARGET_CENTER_MAX_DRAG_Y_PX = 150.0
TARGET_CENTER_MAX_ATTEMPTS = 3
TARGET_CENTER_PAN_SETTLE_SECONDS = 1.8
TARGET_CENTER_CAMERA_WAIT_SECONDS = 8.0
TARGET_CENTER_ZOOM_TOLERANCE = 0.18
TARGET_CENTER_MIN_IMPROVEMENT_PX = 8.0

# Priority 15.5.1 — delayed-camera lock and capture safety.
TARGET_CENTER_STABLE_SAMPLES = 4
TARGET_CENTER_STABLE_INTERVAL_SECONDS = 0.45
TARGET_CENTER_DELAYED_GRACE_SECONDS = 14.0
TARGET_CENTER_DIRECT_LOCK_THRESHOLD_PX = 145.0
TARGET_CENTER_URL_LOCK_WAIT_SECONDS = 20.0
TARGET_CENTER_URL_LOCK_SETTLE_SECONDS = 3.0
TARGET_CENTER_CAMERA_EPSILON_DEGREES = 0.00000030
# Google keeps the selected place off-centre when the locality is large and
# the URL camera lock cannot move further. The capture is still usable as
# long as the target stays well inside the frame, and the ROI window is now
# near-full-frame at zoom 15, so allow a larger residual offset.
TARGET_CENTER_CAPTURE_MAX_OFFSET_X_PX = 240.0
TARGET_CENTER_CAPTURE_MAX_OFFSET_Y_PX = 240.0

# Priority 15.5.1 — graph-specific red preservation.
RED_PRESERVATION_ENABLED = True
RED_PRESERVATION_TRIGGER_RATIO = 0.20
RED_PRESERVATION_MIN_COMPONENT_AREA = 2
RED_PRESERVATION_MAX_COMPONENT_AREA = 180
RED_PRESERVATION_NEIGHBOR_DISTANCE_PX = 34.0
RED_PRESERVATION_CLEAN_SUPPORT_DILATION_PX = 13
RED_PRESERVATION_MAX_RESTORED_RATIO = 0.72

LAST_RECENTER_DIAGNOSTICS = {}

# Priority 15.5 — Directional Boundary Graph Reconstruction.
DIRECTIONAL_GRAPH_ENABLED = True
DIRECTIONAL_GRAPH_LOCAL_JOIN_LENGTH = {"small": 11, "medium": 15, "large": 19}
DIRECTIONAL_GRAPH_MAX_GAP_PX = {"small": 105.0, "medium": 145.0, "large": 185.0}
DIRECTIONAL_GRAPH_GAP_LEVELS = {
    "small": [35.0, 55.0, 80.0, 105.0],
    "medium": [40.0, 70.0, 105.0, 145.0],
    "large": [45.0, 80.0, 120.0, 155.0, 185.0],
}
DIRECTIONAL_GRAPH_MIN_COMPONENT_PIXELS = 8
DIRECTIONAL_GRAPH_MIN_ENDPOINT_DIRECTION_COS = 0.08
DIRECTIONAL_GRAPH_MIN_AVERAGE_DIRECTION_COS = 0.30
DIRECTIONAL_GRAPH_MIN_TARGET_CLEARANCE_RATIO = 0.30
DIRECTIONAL_GRAPH_MAX_RADIAL_DIFFERENCE_RATIO = 0.62
DIRECTIONAL_GRAPH_BRIDGE_THICKNESS_PX = 3
DIRECTIONAL_GRAPH_MAX_BRIDGES = 18
DIRECTIONAL_GRAPH_MAX_BRIDGE_FRACTION = 0.30
DIRECTIONAL_GRAPH_MIN_RED_CLOSE_RATIO = {"small": 0.88, "medium": 0.84, "large": 0.80}
DIRECTIONAL_GRAPH_MAX_MEAN_RED_DISTANCE_PX = {"small": 6.0, "medium": 7.0, "large": 8.5}
DIRECTIONAL_GRAPH_CLOSE_DISTANCE_PX = {"small": 8.0, "medium": 9.0, "large": 11.0}
GRAPH_CONFIRMATION_MIN_IOU = 0.72
GRAPH_CONFIRMATION_MIN_SMALLER_OVERLAP = 0.86
GRAPH_CONFIRMATION_MAX_AREA_RATIO_DIFFERENCE = 0.28

# Priority 15.5.3 — Local Gap Path Repair.
#
# When the polar contour is already strong and fails only because of one or
# two oversized adjacent steps, preserve every valid section and replace only
# those local gaps with red-supported A* paths. A repaired result remains
# provisional until another zoom confirms the geographic shape.
LOCAL_GAP_PATH_ENABLED = True
LOCAL_GAP_PATH_MAX_GAPS = 2
LOCAL_GAP_PATH_TARGET_STEP_PX = 24.0
LOCAL_GAP_PATH_MIN_COVERAGE = {
    "small": 0.74,
    "medium": 0.70,
    "large": 0.66,
}
LOCAL_GAP_PATH_MIN_CLOSE_TO_RED = {
    "small": 0.90,
    "medium": 0.88,
    "large": 0.88,
}
LOCAL_GAP_PATH_MAX_MEAN_RED_DISTANCE_PX = {
    "small": 8.0,
    "medium": 8.8,
    "large": 9.0,
}
LOCAL_GAP_PATH_MAX_ORIGINAL_GAP_PX = {
    "small": 105.0,
    "medium": 125.0,
    "large": 135.0,
}
LOCAL_GAP_PATH_SEARCH_MARGIN_PX = {
    "small": 60,
    "medium": 75,
    "large": 90,
}
LOCAL_GAP_PATH_MAX_LENGTH_RATIO = 2.75
LOCAL_GAP_PATH_MIN_RED_SUPPORT_RATIO = 0.55
LOCAL_GAP_PATH_MAX_PATH_MEAN_RED_DISTANCE_PX = 9.5
LOCAL_GAP_PATH_MAX_AREA_CHANGE_RATIO = 0.065
LOCAL_GAP_PATH_MAX_FINAL_STEP_PX = 26.0
LOCAL_GAP_PATH_MAX_EXPANSIONS = 260000
LOCAL_GAP_PATH_VARIANTS = (
    {"name": "balanced", "distance_weight": 0.34, "line_weight": 0.018, "inward_weight": 1.4},
    {"name": "red_strict", "distance_weight": 0.48, "line_weight": 0.010, "inward_weight": 1.7},
    {"name": "shape_strict", "distance_weight": 0.28, "line_weight": 0.030, "inward_weight": 2.0},
    {"name": "wide_red", "distance_weight": 0.40, "line_weight": 0.006, "inward_weight": 1.2},
)

# Priority 15.5.2 — target-guided graph corridor and multi-path matching.
# The preserved mask can contain red labels/icons. A rough polar radius guide
# now limits graph pixels to a boundary corridor around the searched target.
GRAPH_CORRIDOR_ENABLED = True
GRAPH_CORRIDOR_BIN_COUNT = 180
GRAPH_CORRIDOR_MIN_GUIDE_COVERAGE = 0.30
GRAPH_CORRIDOR_MIN_FILTERED_PIXELS = 90
GRAPH_CORRIDOR_MIN_FILTERED_RETENTION = 0.10
GRAPH_CORRIDOR_INNER_RATIO = {"small": 0.18, "medium": 0.22, "large": 0.26}
GRAPH_CORRIDOR_OUTER_RATIO = {"small": 0.20, "medium": 0.24, "large": 0.28}
GRAPH_CORRIDOR_MIN_HALF_WIDTH_PX = {"small": 22.0, "medium": 26.0, "large": 30.0}
GRAPH_CORRIDOR_MAX_HALF_WIDTH_PX = {"small": 72.0, "medium": 88.0, "large": 108.0}
GRAPH_CORRIDOR_FALLBACK_EXPANSION = 1.40
GRAPH_CORRIDOR_COMPONENT_MAX_FILL_RATIO = 0.80
GRAPH_CORRIDOR_COMPONENT_MAX_THICKNESS_PX = 18.0
GRAPH_CORRIDOR_SUPPORT_DILATION_PX = 9

# Explore several non-conflicting endpoint matchings instead of one greedy
# solution. Every surviving loop still needs the existing cross-zoom checks.
DIRECTIONAL_GRAPH_MAX_PATH_VARIANTS = 14
DIRECTIONAL_GRAPH_TOP_CANDIDATES_PER_LEVEL = 72
DIRECTIONAL_GRAPH_SEEDED_VARIANTS = 8

# Priority 15.5.4 — preserved-mask polar bootstrap and zoom guard.
#
# When normal cleaning removes almost every Google-red dot, build a safe
# target-centred angular guide from strict + preserved red evidence. The dotted
# observations are converted into a temporary continuous red line. This line
# is only a reconstruction guide; final geometry must still agree with the
# original red pixels at another zoom.
PRESERVED_POLAR_BOOTSTRAP_ENABLED = True
PRESERVED_POLAR_BOOTSTRAP_TRIGGER_RATIO = 0.08
PRESERVED_POLAR_BOOTSTRAP_MIN_CLEAN_PIXELS = 45
PRESERVED_POLAR_BOOTSTRAP_BIN_COUNT = 180
PRESERVED_POLAR_BOOTSTRAP_MIN_COVERAGE = 0.22
PRESERVED_POLAR_BOOTSTRAP_STRICT_PERCENTILE = 72.0
PRESERVED_POLAR_BOOTSTRAP_PRESERVED_PERCENTILE = 66.0
PRESERVED_POLAR_BOOTSTRAP_LINE_THICKNESS_PX = 3
PRESERVED_POLAR_BOOTSTRAP_SUPPORT_DILATION_PX = {
    "small": 17,
    "medium": 23,
    "large": 29,
}
PRESERVED_POLAR_BOOTSTRAP_MAX_RADIAL_JUMP_RATIO = 0.30
PRESERVED_POLAR_BOOTSTRAP_MAX_RADIAL_JUMP_PX = 62.0

# Priority 15.5.4.1 bootstrap error hotfix.
#
# The old bootstrap used the normal short-side radius ceiling. Large sector
# outlines can extend beyond that ceiling even while remaining inside the
# target ROI. The bootstrap now expands to the actual ROI corners.
PRESERVED_POLAR_BOOTSTRAP_EXPAND_TO_ROI = True
PRESERVED_POLAR_BOOTSTRAP_ROI_RADIUS_MARGIN = 0.985
PRESERVED_POLAR_BOOTSTRAP_MINIMUM_RADIUS_PX = 7.0

# When direct angular sampling is sparse, connect Google-red dots in polar
# space. In polar coordinates, angle is vertical and radius is horizontal, so
# a vertical closing kernel converts dotted angular observations into a
# continuous radius guide without drawing arbitrary image-space shortcuts.
PRESERVED_POLAR_BOOTSTRAP_POLAR_FALLBACK_ENABLED = True
PRESERVED_POLAR_BOOTSTRAP_POLAR_CLOSE_ANGLE_BINS = 15
PRESERVED_POLAR_BOOTSTRAP_POLAR_CLOSE_RADIUS_PX = 5
PRESERVED_POLAR_BOOTSTRAP_POLAR_RADIUS_PERCENTILE = 68.0

# Priority 15.5.4.2 — full-frame bootstrap ROI fix.
#
# Keep the original full-frame Google-red masks before the normal CV ROI is
# applied. The bootstrap gets its own large evidence window and only the final
# candidate is validated against a bounded target-centred ROI.
FULL_FRAME_BOOTSTRAP_ENABLED = True
FULL_FRAME_BOOTSTRAP_EDGE_MARGIN_RATIO = 0.006
FULL_FRAME_BOOTSTRAP_RED_BBOX_PADDING_RATIO = 0.055
FULL_FRAME_BOOTSTRAP_MIN_WIDTH_RATIO = 0.90
FULL_FRAME_BOOTSTRAP_MIN_HEIGHT_RATIO = 0.88
FULL_FRAME_BOOTSTRAP_VALIDATION_MARGIN_PX = 28
FULL_FRAME_BOOTSTRAP_MIN_SOURCE_PIXELS = 60
FULL_FRAME_BOOTSTRAP_RADIUS_EXPANSION_ENABLED = True
FULL_FRAME_BOOTSTRAP_RADIUS_MARGIN = 0.995

# Compact localities initially opened by Google near zoom 15 use only the
# detailed/confirmation sequence 15 -> 14 -> 13. Zoom 16 and zoom 12 are
# excluded from adaptive queue rebuilds.
COMPACT_URBAN_ZOOM_GUARD_ENABLED = True
COMPACT_URBAN_INITIAL_ZOOM_THRESHOLD = 14.75
COMPACT_URBAN_ALLOWED_RETRY_ZOOMS = (14, 13)

# Google can render quarter zooms after wheel/keyboard input. Use the actual
# rendered zoom for georeferencing when it is close enough to the request.
FRACTIONAL_ZOOM_ACCEPTANCE_TOLERANCE = 0.30

# Warn when at least this fraction of the smaller polygon overlaps.
OVERLAP_WARN_SMALLER_RATIO = 0.12

# In Clean Coverage Mode this would block different same-level localities.
# In Exact Google Mode it is retained only to classify strong warnings.
OVERLAP_BLOCK_SMALLER_RATIO = 0.30

# Treat very similar shapes with different filenames as duplicate boundaries.
OVERLAP_DUPLICATE_IOU = 0.80
OVERLAP_DUPLICATE_SMALLER_RATIO = 0.90

# Priority 7: preserve more of the visible Google-red outline.
#
# Exact mode never replaces the stable contour unless all exact-shape safety
# checks pass. When the red line is too incomplete or noisy, the pipeline
# automatically keeps the Priority 3 stable contour.
EXACT_SHAPE_MODE = True
EXACT_SHAPE_MAX_SNAP_DISTANCE_PX = 20.0
EXACT_SHAPE_MIN_RED_SUPPORT_RATIO = 0.42
EXACT_SHAPE_MAX_AREA_CHANGE_RATIO = 0.045
EXACT_SHAPE_MAX_VERTEX_COUNT = 720
EXACT_SHAPE_MIN_VERTEX_COUNT = 240

# Priority 10: accept exact repaired contours when they miss the final
# integer-rounded max-step check by a tiny amount only.
EXACT_SHAPE_MAX_STEP_TOLERANCE_PX = 1.25

# Priority 10: exact repaired candidates may lose a few red-supported
# vertices while removing spikes/long jumps. 90% retained support is still
# strong enough when all other exact checks pass.
EXACT_SHAPE_MIN_RETAINED_SUPPORT_AFTER_REPAIR = 0.90

# Reject contours that appear to cut through the inner locality instead of
# following the outer visible boundary.
INNER_SHORTCUT_MIN_FRACTION = 0.15
INNER_SHORTCUT_RADIUS_RATIO = 0.22

# Priority 10.1: when no final boundary can be exported, save inspectable
# CV diagnostics for every zoom capture.
CV_FAILURE_DEBUG_EXPORT_ENABLED = True

# Priority 10.2: safe gap closing.
#
# A strong but broken Google-red outline can have one large endpoint gap.
# Instead of rejecting it immediately, allow it to enter exact-repair. The
# final export is still blocked unless exact-repair removes the unsafe gap.
SAFE_GAP_CLOSE_ENABLED = True
SAFE_GAP_CLOSE_MIN_COVERAGE = 0.78
SAFE_GAP_CLOSE_MIN_CLOSE_TO_RED = 0.90
SAFE_GAP_CLOSE_MAX_MEAN_RED_DISTANCE_PX = 6.50
SAFE_GAP_CLOSE_PRE_REPAIR_MAX_STEP_PX = 115.0

# Multiplier applied to the pre-repair step limit for contours that qualify
# for exact repair. The repair stage removes long steps and is validated
# separately, so this pre-filter does not need to be the deciding check.
SAFE_GAP_CLOSE_PRE_REPAIR_STEP_TOLERANCE = 1.0

# A repair whose gap vertices all land on Google's rendered red pixels is
# following the boundary through a rendering gap rather than guessing, so a
# larger area change is acceptable for it alone.
EXACT_REPAIR_FULL_GAP_SUPPORT_RATIO = 1.0
EXACT_REPAIR_FULL_GAP_SUPPORT_MAX_DISTANCE_PX = 18.0
EXACT_REPAIR_FULL_GAP_SUPPORT_AREA_CHANGE_FACTOR = 1.0
SAFE_GAP_CLOSE_REQUIRES_EXACT_ABOVE_STEP_PX = 60.0

# Priority 10.3: if a repaired exact contour is geometrically clean but is
# blocked only because too much of it was interpolated, export it as REVIEW.
REPAIRED_REVIEW_EXPORT_ENABLED = True
REPAIRED_REVIEW_ALLOWED_FAILED_CHECKS = {
    "retained_red_support",
}

# Filled by detect_boundary_cv when a repaired contour is safe enough for
# manual review but not safe enough for automatic final export.
LAST_CV_REVIEW_CANDIDATES: list[dict] = []

# Priority 9: reliable camera positioning and cross-zoom validation.
MULTI_ZOOM_VALIDATION_ENABLED = True

# Two zoom results are considered compatible when most of their combined
# shape agrees and the smaller polygon is largely contained in that agreement.
MULTI_ZOOM_MIN_IOU = 0.74
MULTI_ZOOM_MIN_SMALLER_OVERLAP = 0.86

# A single result is accepted without a matching second zoom only when it is
# exceptionally strong.
MULTI_ZOOM_SINGLE_MIN_COVERAGE = 0.82
MULTI_ZOOM_SINGLE_MIN_CLOSE_TO_RED = 0.88
MULTI_ZOOM_SINGLE_MAX_MEAN_RED_DISTANCE_PX = 7.0

# Permit a slightly larger pre-repair jump only when the candidate already
# has excellent red support. Priority 8 exact repair must still reduce all
# final steps to its strict limit before export.
HIGH_SUPPORT_PRE_REPAIR_MAX_STEP_PX = 85.0

# Priority 9.2: wait until Google's red dotted outline is actually rendered
# before saving each comparison screenshot.
RED_RENDER_WAIT_SECONDS = 14.0
RED_RENDER_POLL_SECONDS = 1.25
RED_RENDER_MIN_PIXELS = 220
RED_RENDER_MIN_HORIZONTAL_SPAN_PX = 180
RED_RENDER_MIN_VERTICAL_SPAN_PX = 120

# Priority 15.5.4.3 — abnormal Google-red flood guard.
#
# A locality outline should occupy only a small fraction of the map image.
# Large red overlays, stale feedback layers, route rendering or corrupted map
# frames can make tens of thousands of pixels pass the red colour filter.
RED_FLOOD_GUARD_ENABLED = True
RED_FLOOD_MAX_MAP_RED_RATIO = 0.015
RED_FLOOD_RELATIVE_MULTIPLIER = 4.0
RED_FLOOD_MIN_RELATIVE_EXCESS_PIXELS = 5000
RED_FLOOD_MIN_ABSOLUTE_PIXELS = 12000
RED_FLOOD_RECAPTURE_ATTEMPTS = 2
RED_FLOOD_SETTLE_SECONDS = 2.5

# A capture whose camera lock failed or whose red render remains flooded is
# diagnostic-only. It cannot change the adaptive locality profile or enter
# final multi-zoom validation.
ACTIVE_CAPTURE_PROFILE_TRUSTED = True

# Priority 15.5.4.4 — Boundary Presence Gate + Selected-Place Lock.
ACTIVE_EXPECTED_SELECTED_LABEL = ""
ACTIVE_SELECTED_PLACE_TRUSTED = True
LAST_SELECTED_PLACE_LOCK = {}

SELECTED_PLACE_LOCK_ENABLED = True
SELECTED_PLACE_LOCK_MAX_ATTEMPTS = 2
SELECTED_PLACE_LOCK_WAIT_SECONDS = 7.0
SELECTED_PLACE_LOCK_POLL_SECONDS = 0.45

BOUNDARY_PRESENCE_GATE_ENABLED = True
BOUNDARY_PRESENCE_ANGLE_BINS = 72
BOUNDARY_PRESENCE_MIN_COMPONENTS = 20
BOUNDARY_PRESENCE_MIN_ANGULAR_COVERAGE = 0.22
BOUNDARY_PRESENCE_MIN_QUADRANTS = 3
BOUNDARY_PRESENCE_MIN_COHERENT_LINKS = 8
BOUNDARY_PRESENCE_MAX_ANGULAR_GAP_BINS = 2
BOUNDARY_PRESENCE_MIN_SCORE = 0.68
BOUNDARY_PRESENCE_MIN_CLEAN_PIXELS = 45
BOUNDARY_PRESENCE_MIN_CLEAN_ANGULAR_COVERAGE = 0.18
BOUNDARY_PRESENCE_MIN_LINE_ARC_PX = 90.0
BOUNDARY_PRESENCE_MIN_TOTAL_LINE_ARC_PX = 170.0
BOUNDARY_PRESENCE_MAX_LINE_THICKNESS_PX = 9.0
BOUNDARY_PRESENCE_UI_TOP_RATIO = 0.075
BOUNDARY_PRESENCE_UI_BOTTOM_RATIO = 0.045

# Priority 15.5.4.5 — strict ring evidence.
BOUNDARY_PRESENCE_MAX_COMPONENTS_PER_BIN = 12.0
BOUNDARY_PRESENCE_MIN_TRACK_COVERAGE = 0.20
BOUNDARY_PRESENCE_MIN_TRACK_QUADRANTS = 3
BOUNDARY_PRESENCE_MAX_TRACK_GAP_BINS = 2
BOUNDARY_PRESENCE_MAX_TRACK_JUMP_RATIO = 0.20
BOUNDARY_PRESENCE_MAX_TRACK_JUMP_PX = 52.0
BOUNDARY_PRESENCE_MAX_REJECTED_STRICT_RATIO = 0.55
BOUNDARY_PRESENCE_MIN_LINE_ANGULAR_COVERAGE = 0.20
BOUNDARY_PRESENCE_MIN_LINE_QUADRANTS = 3

# A retry capture is valid only when the actual rendered zoom still matches
# the requested zoom after place restoration, camera centring and panel close.
ZOOM_INTEGRITY_ENABLED = True
ZOOM_INTEGRITY_TOLERANCE = 0.30
ZOOM_INTEGRITY_MAX_ATTEMPTS = 2

# Priority 15.5.4.6 — comparison zoom failures are non-fatal.
ZOOM_INTEGRITY_DIRECT_URL_FALLBACK = True
ZOOM_INTEGRITY_SKIP_CAPTURE_ON_FAILURE = True
ZOOM_INTEGRITY_NEAREST_STEP_DIAGNOSTIC_LIMIT = 0.55

# Exact-repair confirmation. A repaired contour that is geometrically clean
# but loses global red support may enter multi-zoom validation provisionally.
# It can never export without a geographically compatible second zoom.
EXACT_REPAIR_CONFIRMATION_ENABLED = True
EXACT_REPAIR_CONFIRMATION_ALLOWED_FAILED_CHECKS = {
    "retained_red_support",
    "no_inner_shortcut",
}
EXACT_REPAIR_CONFIRMATION_MIN_COVERAGE = 0.55
EXACT_REPAIR_CONFIRMATION_MAX_REPAIRED_FRACTION = 0.35
EXACT_REPAIR_CONFIRMATION_MIN_UNREPAIRED_SUPPORT = 0.64
EXACT_REPAIR_CONFIRMATION_GAP_SUPPORT_DISTANCE_PX = 30.0
EXACT_REPAIR_CONFIRMATION_MIN_GAP_SUPPORT = 0.30
EXACT_REPAIR_CONFIRMATION_MAX_GAP_MEAN_DISTANCE_PX = 30.0
EXACT_REPAIR_CONFIRMATION_ALLOW_SHORTCUT_DEFERRED = True
EXACT_REPAIR_CONFIRMATION_SHORTCUT_FRACTION_TOLERANCE = 0.018
EXACT_REPAIR_CONFIRMATION_SHORTCUT_RADIUS_TOLERANCE_PX = 12.0

# Priority 9.3: if zoom candidates disagree, export each candidate separately
# for manual review instead of failing with no inspectable boundary output.
REVIEW_CANDIDATE_EXPORT_ENABLED = True

# Updated by detect_boundary_cv after each capture. This avoids changing the
# existing public return type used elsewhere in the project.
LAST_CV_DIAGNOSTICS: dict = {}

INDIA_BBOX = (6.5, 68.0, 37.5, 97.5)

STATE_CODES: dict[str, str] = {
    "AP": "Andhra Pradesh",    "AR": "Arunachal Pradesh", "AS": "Assam",
    "BR": "Bihar",             "CG": "Chhattisgarh",      "GA": "Goa",
    "GJ": "Gujarat",           "HR": "Haryana",           "HP": "Himachal Pradesh",
    "JH": "Jharkhand",         "KA": "Karnataka",         "KL": "Kerala",
    "MP": "Madhya Pradesh",    "MH": "Maharashtra",       "MN": "Manipur",
    "ML": "Meghalaya",         "MZ": "Mizoram",           "NL": "Nagaland",
    "OD": "Odisha",            "PB": "Punjab",            "RJ": "Rajasthan",
    "SK": "Sikkim",            "TN": "Tamil Nadu",        "TG": "Telangana",
    "TR": "Tripura",           "UP": "Uttar Pradesh",     "UK": "Uttarakhand",
    "WB": "West Bengal",       "AN": "Andaman and Nicobar Islands",
    "CH": "Chandigarh",        "DN": "Dadra and Nagar Haveli and Daman and Diu",
    "DL": "Delhi",             "JK": "Jammu and Kashmir", "LA": "Ladakh",
    "LD": "Lakshadweep",       "PY": "Puducherry",
}



def _set_active_locality_profile(
    profile_name: str,
    reason: str,
    log=print,
) -> str:
    global ACTIVE_LOCALITY_PROFILE
    global ACTIVE_LOCALITY_PROFILE_REASON

    reason_text = str(
        reason
        or ""
    )

    if (
        not ACTIVE_CAPTURE_PROFILE_TRUSTED
        and not reason_text.startswith(
            "Google selected zoom"
        )
    ):
        log(
            "   [Adaptive] Profile update ignored because "
            "the current capture is diagnostic-only"
        )

        return ACTIVE_LOCALITY_PROFILE

    if profile_name not in LOCALITY_PROFILES:
        profile_name = "medium"

    changed = (
        profile_name != ACTIVE_LOCALITY_PROFILE
        or reason != ACTIVE_LOCALITY_PROFILE_REASON
    )

    ACTIVE_LOCALITY_PROFILE = profile_name
    ACTIVE_LOCALITY_PROFILE_REASON = reason

    if changed:
        log(
            "   [Adaptive] Locality profile: "
            f"{profile_name.upper()} ({reason})"
        )

    return profile_name


def _profile_from_google_zoom(
    google_zoom: float,
) -> tuple[str, str]:
    zoom_value = float(google_zoom)

    if zoom_value >= 15.75:
        return (
            "small",
            f"Google selected zoom {zoom_value:.1f}",
        )

    if zoom_value <= 14.25:
        return (
            "large",
            f"Google selected zoom {zoom_value:.1f}",
        )

    return (
        "medium",
        f"Google selected zoom {zoom_value:.1f}",
    )


def _adaptive_area_limits(
    profile_name: str,
    zoom: float,
) -> tuple[float, float]:
    zoom_value = float(zoom)

    if profile_name == "small":
        if zoom_value >= 15.5:
            return 0.0010, 0.45
        if zoom_value >= 14.5:
            return 0.0008, 0.42
        return 0.0005, 0.35

    if profile_name == "large":
        if zoom_value >= 14.5:
            return 0.0040, 0.52
        if zoom_value >= 13.5:
            return 0.0025, 0.48
        return 0.0015, 0.42

    if zoom_value >= 14.5:
        return 0.0030, 0.42
    if zoom_value >= 13.5:
        return 0.0018, 0.26
    return 0.0010, 0.22


def _adaptive_roi_ratios(
    profile_name: str,
    zoom: float,
) -> tuple[float, float]:
    zoom_value = float(zoom)

    if profile_name == "small":
        if zoom_value >= 15.5:
            return 0.48, 0.49
        if zoom_value >= 14.5:
            return 0.46, 0.48
        return 0.42, 0.46

    if profile_name == "large":
        if zoom_value >= 14.5:
            return 0.49, 0.49
        if zoom_value >= 13.5:
            return 0.43, 0.48
        return 0.36, 0.44

    # Zoom 15 renders the dotted line with the widest pixel spacing, so it is
    # the most accurate source. A medium locality often fills more than the
    # old 0.38/0.46 window there and was rejected for inside_roi alone. Use a
    # near-full-frame window at zoom 15 so the whole outline can be traced.
    if zoom_value >= 14.5:
        return 0.48, 0.49
    if zoom_value >= 13.5:
        return 0.30, 0.40
    return 0.23, 0.35


def _adaptive_radius_limits(
    profile_name: str,
    width: int,
    height: int,
    roi_half_width: int,
    roi_half_height: int,
) -> tuple[float, float]:
    short_side = float(min(width, height))

    minimum_ratio = {
        "small": 0.012,
        "medium": 0.020,
        "large": 0.022,
    }.get(profile_name, 0.020)

    maximum_ratio = {
        "small": 0.52,
        "medium": 0.64,
        "large": 0.78,
    }.get(profile_name, 0.64)

    minimum_radius = max(
        10.0,
        short_side * minimum_ratio,
    )

    maximum_radius = min(
        math.hypot(
            roi_half_width,
            roi_half_height,
        ),
        short_side * maximum_ratio,
    )

    return minimum_radius, maximum_radius


def _candidate_metric(
    diagnostics: dict,
    key: str,
    default=None,
):
    if not isinstance(diagnostics, dict):
        return default

    if key in diagnostics:
        return diagnostics.get(key, default)

    for nested_key in (
        "best_rejection",
        "exact_shape",
        "exact_shape_fallback",
    ):
        nested = diagnostics.get(nested_key)

        if isinstance(nested, dict):
            result = _candidate_metric(
                nested,
                key,
                None,
            )

            if result is not None:
                return result

    return default


def _infer_profile_from_attempt(
    current_profile: str,
    diagnostics: dict,
    georef: "GeoReference",
) -> tuple[str, str]:
    area_ratio = float(
        _candidate_metric(
            diagnostics,
            "area_ratio",
            0.0,
        )
        or 0.0
    )

    inside_roi = bool(
        _candidate_metric(
            diagnostics,
            "inside_roi",
            True,
        )
    )

    close_to_red = float(
        _candidate_metric(
            diagnostics,
            "close_to_red_ratio",
            0.0,
        )
        or 0.0
    )

    maximum_step = float(
        _candidate_metric(
            diagnostics,
            "max_step_px",
            0.0,
        )
        or 0.0
    )

    coverage_ratio = float(
        _candidate_metric(
            diagnostics,
            "coverage",
            0.0,
        )
        or 0.0
    )

    target_x, target_y = georef.target_pixel

    offset_x_ratio = abs(
        float(target_x)
        - float(georef.camera_px_x)
    ) / max(float(georef.img_w), 1.0)

    offset_y_ratio = abs(
        float(target_y)
        - float(georef.camera_px_y)
    ) / max(float(georef.img_h), 1.0)

    target_offset_ratio = max(
        offset_x_ratio,
        offset_y_ratio,
    )

    if (
        not inside_roi
        or area_ratio >= 0.14
        or maximum_step >= 80.0
        or target_offset_ratio >= 0.22
        or (
            area_ratio >= 0.050
            and coverage_ratio >= 0.85
        )
    ):
        return (
            "large",
            (
                "large/off-centre outline "
                f"(area={area_ratio:.4f}, "
                f"inside_roi={inside_roi}, "
                f"coverage={coverage_ratio:.3f}, "
                f"max_step={maximum_step:.1f}, "
                f"target_offset={target_offset_ratio:.3f})"
            ),
        )

    if (
        0.0 < area_ratio < 0.010
        and close_to_red >= 0.80
    ):
        return (
            "small",
            (
                "small high-quality outline "
                f"(area={area_ratio:.4f}, "
                f"red_match={close_to_red:.3f})"
            ),
        )

    return (
        current_profile,
        f"evidence supports {current_profile} profile",
    )



def _adaptive_exact_repair_limits(
    profile_name: str,
    coverage_ratio: float,
) -> dict:
    profile_name = (
        profile_name
        if profile_name in PROFILE_RETAINED_SUPPORT_LIMITS
        else "medium"
    )

    base_support = float(
        PROFILE_RETAINED_SUPPORT_LIMITS[
            profile_name
        ]
    )

    minimum_coverage = float(
        PROFILE_MIN_COVERAGE_FOR_LOW_SUPPORT[
            profile_name
        ]
    )

    coverage_value = float(
        coverage_ratio
        or 0.0
    )

    # Do not use the relaxed large-profile threshold when the visible dotted
    # outline itself has weak angular coverage.
    if coverage_value < minimum_coverage:
        required_support = max(
            base_support,
            0.70
            if profile_name == "large"
            else 0.75,
        )
    else:
        required_support = base_support

    return {
        "profile": profile_name,
        "coverage_ratio": coverage_value,
        "minimum_coverage_for_relaxed_support": (
            minimum_coverage
        ),
        "required_retained_support": (
            required_support
        ),
        "maximum_area_change_ratio": float(
            PROFILE_MAX_EXACT_AREA_CHANGE[
                profile_name
            ]
        ),
        "maximum_mean_snap_distance_px": float(
            PROFILE_MAX_MEAN_SNAP_DISTANCE_PX[
                profile_name
            ]
        ),
    }


def _rejection_strength(
    diagnostics: dict,
) -> tuple:
    if not isinstance(
        diagnostics,
        dict,
    ):
        return (
            0,
            0.0,
            0.0,
            0.0,
        )

    failed_checks = diagnostics.get(
        "failed_checks",
        [],
    )

    return (
        int(
            bool(
                diagnostics
            )
        ),
        float(
            diagnostics.get(
                "coverage",
                0.0,
            )
            or 0.0
        ),
        float(
            diagnostics.get(
                "close_to_red_ratio",
                0.0,
            )
            or 0.0
        ),
        float(
            diagnostics.get(
                "area_ratio",
                0.0,
            )
            or 0.0
        )
        + min(
            float(
                diagnostics.get(
                    "max_step_px",
                    0.0,
                )
                or 0.0
            )
            / 1000.0,
            0.5,
        )
        + min(
            len(
                failed_checks
            )
            * 0.001,
            0.01,
        ),
    )


def _preserve_strongest_rejection(
    existing_diagnostics: dict,
    new_diagnostics: dict,
) -> dict:
    existing = (
        existing_diagnostics
        if isinstance(
            existing_diagnostics,
            dict,
        )
        else {}
    )

    new = (
        new_diagnostics
        if isinstance(
            new_diagnostics,
            dict,
        )
        else {}
    )

    if _rejection_strength(
        new
    ) > _rejection_strength(
        existing
    ):
        return dict(
            new
        )

    return dict(
        existing
    )




def _minimum_final_zoom() -> float:
    initial_zoom = float(
        INITIAL_GOOGLE_LOCALITY_ZOOM
    )

    for (
        minimum_initial_zoom,
        final_zoom_floor,
    ) in FINAL_ZOOM_FLOOR_BY_INITIAL_VIEW:
        if initial_zoom >= float(
            minimum_initial_zoom
        ):
            return float(
                final_zoom_floor
            )

    return 12.0


def _contour_framing_diagnostics(
    contour,
    image_width: int,
    image_height: int,
    zoom: float,
) -> dict:
    if (
        contour is None
        or image_width <= 0
        or image_height <= 0
    ):
        return {
            "status": "missing",
            "final_eligible": False,
            "reason": "missing_contour_or_image_dimensions",
        }

    points = np.asarray(
        contour,
        dtype=np.int32,
    ).reshape(
        -1,
        1,
        2,
    )

    if len(
        points
    ) < 3:
        return {
            "status": "missing",
            "final_eligible": False,
            "reason": "too_few_contour_points",
        }

    (
        box_x,
        box_y,
        box_width,
        box_height,
    ) = cv2.boundingRect(
        points
    )

    width_ratio = float(
        box_width
    ) / max(
        float(
            image_width
        ),
        1.0,
    )

    height_ratio = float(
        box_height
    ) / max(
        float(
            image_height
        ),
        1.0,
    )

    bbox_area_ratio = (
        float(
            box_width
        )
        * float(
            box_height
        )
        / max(
            float(
                image_width
                * image_height
            ),
            1.0,
        )
    )

    max_span_ratio = max(
        width_ratio,
        height_ratio,
    )

    minimum_edge_margin = min(
        float(
            box_x
        ) / max(
            float(
                image_width
            ),
            1.0,
        ),
        float(
            box_y
        ) / max(
            float(
                image_height
            ),
            1.0,
        ),
        float(
            image_width
            - (
                box_x
                + box_width
            )
        ) / max(
            float(
                image_width
            ),
            1.0,
        ),
        float(
            image_height
            - (
                box_y
                + box_height
            )
        ) / max(
            float(
                image_height
            ),
            1.0,
        ),
    )

    touches_edge = (
        minimum_edge_margin
        <= FRAMING_EDGE_MARGIN_RATIO
    )

    too_small = (
        max_span_ratio
        < FRAMING_MIN_MAX_SPAN_RATIO
        or bbox_area_ratio
        < FRAMING_MIN_BBOX_AREA_RATIO
    )

    too_large_or_clipped = (
        max_span_ratio
        > FRAMING_MAX_MAX_SPAN_RATIO
        or touches_edge
    )

    zoom_floor = _minimum_final_zoom()

    if too_small:
        status = "confirmation_only_too_small"
        reason = (
            "boundary occupies too little of the screenshot"
        )

    elif too_large_or_clipped:
        status = "confirmation_only_clipped_or_too_large"
        reason = (
            "boundary is too close to an image edge or too large"
        )

    elif float(
        zoom
    ) < zoom_floor:
        status = "confirmation_only_low_zoom"
        reason = (
            f"zoom {float(zoom):.2f} is below final floor "
            f"{zoom_floor:.2f}"
        )

    else:
        status = "detailed_final_candidate"
        reason = "complete boundary has useful detailed framing"

    final_eligible = (
        status
        == "detailed_final_candidate"
    )

    return {
        "status": status,
        "reason": reason,
        "final_eligible": bool(
            final_eligible
        ),
        "zoom": round(
            float(
                zoom
            ),
            3,
        ),
        "minimum_final_zoom": round(
            float(
                zoom_floor
            ),
            3,
        ),
        "bbox_x": int(
            box_x
        ),
        "bbox_y": int(
            box_y
        ),
        "bbox_width_px": int(
            box_width
        ),
        "bbox_height_px": int(
            box_height
        ),
        "width_ratio": round(
            width_ratio,
            4,
        ),
        "height_ratio": round(
            height_ratio,
            4,
        ),
        "bbox_area_ratio": round(
            bbox_area_ratio,
            4,
        ),
        "max_span_ratio": round(
            max_span_ratio,
            4,
        ),
        "minimum_edge_margin_ratio": round(
            minimum_edge_margin,
            4,
        ),
        "touches_edge": bool(
            touches_edge
        ),
    }


def _candidate_framing(
    candidate: dict,
) -> dict:
    if not isinstance(
        candidate,
        dict,
    ):
        return {}

    diagnostics = candidate.get(
        "diagnostics",
        {},
    )

    if not isinstance(
        diagnostics,
        dict,
    ):
        return {}

    framing = diagnostics.get(
        "framing",
        {},
    )

    if isinstance(
        framing,
        dict,
    ):
        return framing

    exact_shape = diagnostics.get(
        "exact_shape",
        {},
    )

    if isinstance(
        exact_shape,
        dict,
    ):
        framing = exact_shape.get(
            "framing",
            {},
        )

        if isinstance(
            framing,
            dict,
        ):
            return framing

    exact_fallback = diagnostics.get(
        "exact_shape_fallback",
        {},
    )

    if isinstance(
        exact_fallback,
        dict,
    ):
        framing = exact_fallback.get(
            "framing",
            {},
        )

        if isinstance(
            framing,
            dict,
        ):
            return framing

    return {}


def _candidate_is_final_eligible(
    candidate: dict,
) -> bool:
    framing = _candidate_framing(
        candidate
    )

    if not framing:
        return (
            float(
                candidate.get(
                    "zoom",
                    0.0,
                )
                or 0.0
            )
            >= _minimum_final_zoom()
            and not _candidate_is_gap_provisional(
                candidate
            )
        )

    return bool(
        framing.get(
            "final_eligible",
            False,
        )
    )


def _candidate_detail_preference(
    candidate: dict,
) -> tuple:
    framing = _candidate_framing(
        candidate
    )
    diagnostics = candidate.get("diagnostics", {}) if isinstance(candidate.get("diagnostics"), dict) else {}
    method = str(diagnostics.get("method", ""))
    is_exact = int("exact" in method or candidate.get("source") in {"exact_repair_confirmation", "google_red_exact_shape"})
    is_polar = int("polar" in method)
    point_count = int(diagnostics.get("point_count", len(candidate.get("ring", []))))

    return (
        int(
            _candidate_is_final_eligible(
                candidate
            )
        ),
        is_exact - is_polar,
        min(point_count, 1000),
        float(
            candidate.get(
                "zoom",
                0.0,
            )
            or 0.0
        ),
        float(
            framing.get(
                "bbox_area_ratio",
                0.0,
            )
            or 0.0
        ),
        _boundary_candidate_score(
            candidate
        ),
    )



def _adaptive_zoom_plan(
    profile_name: str,
    current_zoom: float,
) -> list[int]:
    if (
        COMPACT_URBAN_ZOOM_GUARD_ENABLED
        and float(
            INITIAL_GOOGLE_LOCALITY_ZOOM
        )
        >= COMPACT_URBAN_INITIAL_ZOOM_THRESHOLD
    ):
        return [
            int(
                zoom_value
            )
            for zoom_value
            in COMPACT_URBAN_ALLOWED_RETRY_ZOOMS
            if abs(
                float(
                    zoom_value
                )
                - float(
                    current_zoom
                )
            )
            >= 0.25
        ]

    profile = LOCALITY_PROFILES.get(
        profile_name,
        LOCALITY_PROFILES["medium"],
    )

    plan = []

    for zoom_value in profile["zoom_plan"]:
        zoom_integer = int(round(zoom_value))

        if (
            profile_name == "large"
            and float(
                INITIAL_GOOGLE_LOCALITY_ZOOM
            )
            >= SKIP_ZOOM12_WHEN_INITIAL_ZOOM_AT_LEAST
            and zoom_integer <= 12
        ):
            continue

        if abs(
            float(zoom_integer)
            - float(current_zoom)
        ) < 0.25:
            continue

        if zoom_integer not in plan:
            plan.append(zoom_integer)

    return plan



def _gap_provisional_limits(
    profile_name: str,
) -> dict:
    profile_name = (
        profile_name
        if profile_name in GAP_PROVISIONAL_MIN_COVERAGE
        else "medium"
    )
    return {
        "profile": profile_name,
        "minimum_coverage": float(GAP_PROVISIONAL_MIN_COVERAGE[profile_name]),
        "minimum_close_to_red": float(GAP_PROVISIONAL_MIN_CLOSE_TO_RED[profile_name]),
        "maximum_mean_red_distance_px": float(GAP_PROVISIONAL_MAX_MEAN_RED_DISTANCE_PX[profile_name]),
        "maximum_pre_repair_step_px": float(GAP_PROVISIONAL_MAX_PRE_REPAIR_STEP_PX[profile_name]),
        "minimum_snapped_bins": int(GAP_PROVISIONAL_MIN_SNAPPED_BINS[profile_name]),
        "maximum_raw_outliers": int(GAP_PROVISIONAL_MAX_RAW_OUTLIERS),
    }


def _max_step_only_candidate_is_provisional_safe(
    failed_checks: list[str],
    coverage_ratio: float,
    close_to_red_ratio: float,
    mean_red_distance_px: float,
    maximum_step_px: float,
    snapped_bins: int,
    raw_snap_outliers: int,
    inside_roi: bool,
    contains_target: bool,
    profile_name: str,
) -> tuple[bool, dict]:
    limits = _gap_provisional_limits(profile_name)
    checks = {
        "enabled": bool(GAP_PROVISIONAL_ENABLED),
        "failed_only_max_step": set(failed_checks or []) == {"max_step"},
        "inside_roi": bool(inside_roi),
        "contains_target": bool(contains_target),
        "coverage": float(coverage_ratio) >= limits["minimum_coverage"],
        "close_to_red": float(close_to_red_ratio) >= limits["minimum_close_to_red"],
        "mean_red_distance": float(mean_red_distance_px) <= limits["maximum_mean_red_distance_px"],
        "maximum_step": float(maximum_step_px) <= limits["maximum_pre_repair_step_px"],
        "snapped_bins": int(snapped_bins) >= limits["minimum_snapped_bins"],
        "raw_snap_outliers": int(raw_snap_outliers) <= limits["maximum_raw_outliers"],
    }
    diagnostics = {
        "profile": limits["profile"],
        "limits": limits,
        "values": {
            "failed_checks": list(failed_checks or []),
            "coverage": round(float(coverage_ratio), 4),
            "close_to_red_ratio": round(float(close_to_red_ratio), 4),
            "mean_red_distance_px": round(float(mean_red_distance_px), 3),
            "maximum_step_px": round(float(maximum_step_px), 3),
            "snapped_bins": int(snapped_bins),
            "raw_snap_outliers": int(raw_snap_outliers),
            "inside_roi": bool(inside_roi),
            "contains_target": bool(contains_target),
        },
        "checks": checks,
        "accepted_as_provisional": all(checks.values()),
    }
    return bool(diagnostics["accepted_as_provisional"]), diagnostics


def _exact_diagnostics_are_geometry_safe(
    exact_diagnostics: dict,
) -> bool:
    if not isinstance(exact_diagnostics, dict):
        return False
    repair = exact_diagnostics.get("repair", {})
    shortcut = exact_diagnostics.get("shortcut", {})
    if not isinstance(repair, dict):
        repair = {}
    if not isinstance(shortcut, dict):
        shortcut = {}
    max_step = float(exact_diagnostics.get("max_step_px", 999.0) or 999.0)
    step_limit = float(exact_diagnostics.get(
        "max_step_tolerance_limit_px",
        exact_diagnostics.get("max_allowed_step_px", 0.0),
    ) or 0.0)
    area_change = float(exact_diagnostics.get("area_change_ratio", 1.0) or 1.0)
    area_limit = float(exact_diagnostics.get(
        "maximum_area_change_ratio",
        PROFILE_MAX_EXACT_AREA_CHANGE.get(ACTIVE_LOCALITY_PROFILE, 0.04),
    ) or PROFILE_MAX_EXACT_AREA_CHANGE.get(ACTIVE_LOCALITY_PROFILE, 0.04))
    mean_snap = float(exact_diagnostics.get("mean_snap_distance_px", 999.0) or 999.0)
    mean_snap_limit = float(exact_diagnostics.get(
        "maximum_mean_snap_distance_px",
        PROFILE_MAX_MEAN_SNAP_DISTANCE_PX.get(ACTIVE_LOCALITY_PROFILE, 5.0),
    ) or PROFILE_MAX_MEAN_SNAP_DISTANCE_PX.get(ACTIVE_LOCALITY_PROFILE, 5.0))
    return (
        bool(exact_diagnostics.get("contains_target", False))
        and bool(exact_diagnostics.get("inside_roi", False))
        and int(repair.get("self_crossings_after_repair", 1) or 0) == 0
        and not bool(shortcut.get("suspected", False))
        and max_step <= step_limit
        and area_change <= area_limit
        and mean_snap <= mean_snap_limit
    )



def _exact_repair_confirmation_is_safe(
    diagnostics: dict,
) -> bool:
    if not isinstance(
        diagnostics,
        dict,
    ):
        return False

    exact = diagnostics.get(
        "exact_shape",
        diagnostics.get(
            "exact_shape_fallback",
            {},
        ),
    )

    if not isinstance(
        exact,
        dict,
    ):
        return False

    confirmation = exact.get(
        "confirmation",
        {},
    )

    if not isinstance(
        confirmation,
        dict,
    ):
        return False

    failed_checks = set(
        exact.get(
            "failed_checks",
            [],
        )
        or []
    )

    repair = exact.get(
        "repair",
        {},
    )

    if not isinstance(
        repair,
        dict,
    ):
        repair = {}

    return bool(
        confirmation.get(
            "accepted_as_provisional",
            False,
        )
        and failed_checks
        and failed_checks.issubset(
            EXACT_REPAIR_CONFIRMATION_ALLOWED_FAILED_CHECKS
        )
        and bool(
            exact.get(
                "inside_roi",
                False,
            )
        )
        and bool(
            exact.get(
                "contains_target",
                False,
            )
        )
        and int(
            repair.get(
                "self_crossings_after_repair",
                1,
            )
            or 0
        )
        == 0
        and float(
            exact.get(
                "max_step_px",
                999.0,
            )
            or 999.0
        )
        <= float(
            exact.get(
                "max_step_tolerance_limit_px",
                0.0,
            )
            or 0.0
        )
        and float(
            exact.get(
                "area_change_ratio",
                1.0,
            )
            or 1.0
        )
        <= float(
            exact.get(
                "maximum_area_change_ratio",
                0.0,
            )
            or 0.0
        )
    )


def _candidate_is_gap_provisional(candidate: dict) -> bool:
    if not isinstance(candidate, dict):
        return False
    diagnostics = candidate.get("diagnostics", {})
    return bool(
        candidate.get("gap_provisional", False)
        or (
            isinstance(diagnostics, dict)
            and diagnostics.get("requires_cross_zoom_confirmation", False)
            and diagnostics.get("provisional_reason", "") in {
                "max_step_only_gap_repair",
                "local_gap_path_repair",
                "exact_repair_confirmation",
            }
        )
    )


def _provisional_candidate_is_safe(
    candidate: dict,
) -> bool:
    if not candidate.get("provisional", False):
        return True
    diagnostics = candidate.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        return False

    if diagnostics.get("requires_cross_zoom_confirmation", False):
        if diagnostics.get("provisional_reason") == "directional_graph_reconstruction":
            return _graph_candidate_is_safe(diagnostics)
        if diagnostics.get("provisional_reason") == "local_gap_path_repair":
            return _local_gap_candidate_is_safe(diagnostics)

        if diagnostics.get(
            "provisional_reason"
        ) == "exact_repair_confirmation":
            return _exact_repair_confirmation_is_safe(
                diagnostics
            )

        if (
            diagnostics.get(
                "provisional_reason"
            )
            == "preserved_mask_polar_bootstrap"
        ):
            return _polar_bootstrap_candidate_is_safe(
                diagnostics
            )
        admission = diagnostics.get("gap_provisional_admission", {})
        exact_shape = diagnostics.get("exact_shape", {})
        return (
            isinstance(admission, dict)
            and admission.get("accepted_as_provisional", False)
            and isinstance(exact_shape, dict)
            and bool(exact_shape.get("accepted", False))
            and _exact_diagnostics_are_geometry_safe(exact_shape)
        )

    exact_fallback = diagnostics.get("exact_shape_fallback", {})
    if not isinstance(exact_fallback, dict):
        return False
    failed_checks = set(exact_fallback.get("failed_checks", []) or [])
    allowed_failed_checks = {"retained_red_support"}
    stable_shortcut = exact_fallback.get("stable_shortcut", {})
    if (
        isinstance(stable_shortcut, dict)
        and not stable_shortcut.get("suspected", False)
        and float(exact_fallback.get("area_change_ratio", 1.0) or 1.0) <= 0.03
    ):
        # Stable contour is shortcut-free and the repair barely changed the
        # area, so a suspected shortcut is the shape's genuine concavity.
        allowed_failed_checks.add("no_inner_shortcut")
    return (
        failed_checks
        and failed_checks.issubset(allowed_failed_checks)
        and float(exact_fallback.get("repair", {}).get("retained_red_support_ratio", 0.0) or 0.0) >= 0.65
        and _exact_diagnostics_are_geometry_safe(exact_fallback)
    )



# ─────────────────────────────────────────────────────────────────────────────
#  DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedQuery:
    raw:          str
    google_query: str
    display_name: str
    locality:     Optional[str] = None
    district:     Optional[str] = None
    state_code:   Optional[str] = None
    state_full:   Optional[str] = None


@dataclass
class GeoReference:
    center_lat:  float
    center_lng:  float
    zoom:        float
    img_w:       int
    img_h:       int
    camera_px_x: Optional[float] = None
    camera_px_y: Optional[float] = None
    target_lat: Optional[float] = None
    target_lng: Optional[float] = None

    # CSS pixels per screenshot image pixel. Example:
    # 1540 CSS px / 1925 screenshot px = 0.8 when DPR = 1.25.
    css_per_image_x: float = 1.0
    css_per_image_y: float = 1.0

    meters_per_pixel: float = field(init=False)
    deg_per_px_lng:   float = field(init=False)
    deg_per_px_lat:   float = field(init=False)
    world_size:       float = field(init=False)
    center_world_x:   float = field(init=False)
    center_world_y:   float = field(init=False)

    @staticmethod
    def _clamp_latitude(
        latitude: float,
    ) -> float:
        return max(
            -85.05112878,
            min(
                85.05112878,
                float(latitude),
            ),
        )

    @staticmethod
    def _lat_to_mercator_fraction(
        latitude: float,
    ) -> float:
        latitude = GeoReference._clamp_latitude(
            latitude
        )

        sine_value = math.sin(
            math.radians(
                latitude
            )
        )

        return (
            0.5
            - math.log(
                (
                    1.0
                    + sine_value
                )
                / (
                    1.0
                    - sine_value
                )
            )
            / (
                4.0
                * math.pi
            )
        )

    @staticmethod
    def _mercator_fraction_to_lat(
        fraction_y: float,
    ) -> float:
        mercator_n = (
            math.pi
            - 2.0
            * math.pi
            * float(
                fraction_y
            )
        )

        return math.degrees(
            math.atan(
                math.sinh(
                    mercator_n
                )
            )
        )

    def _lng_to_world_x(
        self,
        longitude: float,
    ) -> float:
        return (
            (
                float(
                    longitude
                )
                + 180.0
            )
            / 360.0
            * self.world_size
        )

    def _lat_to_world_y(
        self,
        latitude: float,
    ) -> float:
        return (
            self._lat_to_mercator_fraction(
                latitude
            )
            * self.world_size
        )

    def _world_x_to_lng(
        self,
        world_x: float,
    ) -> float:
        return (
            float(
                world_x
            )
            / self.world_size
            * 360.0
            - 180.0
        )

    def _world_y_to_lat(
        self,
        world_y: float,
    ) -> float:
        return self._mercator_fraction_to_lat(
            float(
                world_y
            )
            / self.world_size
        )

    def __post_init__(
        self,
    ):
        if self.camera_px_x is None:
            self.camera_px_x = self.img_w / 2.0

        if self.camera_px_y is None:
            self.camera_px_y = self.img_h / 2.0

        self.css_per_image_x = max(
            float(
                self.css_per_image_x
            ),
            1e-9,
        )

        self.css_per_image_y = max(
            float(
                self.css_per_image_y
            ),
            1e-9,
        )

        self.world_size = (
            256.0
            * (
                2.0
                ** float(
                    self.zoom
                )
            )
        )

        self.center_world_x = (
            self._lng_to_world_x(
                self.center_lng
            )
        )

        self.center_world_y = (
            self._lat_to_world_y(
                self.center_lat
            )
        )

        lat_rad = math.radians(
            self.center_lat
        )

        css_meters_per_pixel = (
            156543.03392
            * math.cos(
                lat_rad
            )
            / (
                2.0
                ** float(
                    self.zoom
                )
            )
        )

        self.meters_per_pixel = (
            css_meters_per_pixel
            * (
                self.css_per_image_x
                + self.css_per_image_y
            )
            / 2.0
        )

        self.deg_per_px_lng = (
            360.0
            / self.world_size
            * self.css_per_image_x
        )

        one_image_pixel_north_lat = (
            self._world_y_to_lat(
                self.center_world_y
                - self.css_per_image_y
            )
        )

        self.deg_per_px_lat = abs(
            one_image_pixel_north_lat
            - float(
                self.center_lat
            )
        )

    def pixel_to_latlng(
        self,
        px: float,
        py: float,
    ) -> tuple[float, float]:
        """
        Convert screenshot-image pixels to latitude/longitude using Google's
        Web Mercator world-pixel grid.

        Image pixels are first converted back to CSS map pixels using
        css_per_image_x/y. This fixes the dpr=1.25 / css-image=0.8 offset
        visible in Google Earth overlays.
        """
        delta_world_x = (
            float(
                px
            )
            - float(
                self.camera_px_x
            )
        ) * self.css_per_image_x

        delta_world_y = (
            float(
                py
            )
            - float(
                self.camera_px_y
            )
        ) * self.css_per_image_y

        world_x = (
            self.center_world_x
            + delta_world_x
        )

        world_y = (
            self.center_world_y
            + delta_world_y
        )

        lat = self._world_y_to_lat(
            world_y
        )

        lng = self._world_x_to_lng(
            world_x
        )

        return (
            round(
                max(
                    -90.0,
                    min(
                        90.0,
                        lat,
                    ),
                ),
                7,
            ),
            round(
                max(
                    -180.0,
                    min(
                        180.0,
                        lng,
                    ),
                ),
                7,
            ),
        )

    def latlng_to_pixel(
        self,
        latitude: float,
        longitude: float,
    ) -> tuple[float, float]:
        """
        Convert a nearby geographic coordinate into screenshot-image pixels
        using the same Web Mercator transform as pixel_to_latlng.
        """
        world_x = self._lng_to_world_x(
            longitude
        )

        world_y = self._lat_to_world_y(
            latitude
        )

        px = float(
            self.camera_px_x
        ) + (
            world_x
            - self.center_world_x
        ) / self.css_per_image_x

        py = float(
            self.camera_px_y
        ) + (
            world_y
            - self.center_world_y
        ) / self.css_per_image_y

        return px, py

    @property
    def target_pixel(self) -> tuple[float, float]:
        latitude = (
            self.target_lat
            if self.target_lat is not None
            else self.center_lat
        )
        longitude = (
            self.target_lng
            if self.target_lng is not None
            else self.center_lng
        )

        return self.latlng_to_pixel(
            latitude=float(
                latitude
            ),
            longitude=float(
                longitude
            ),
        )

    @property
    def bbox(self) -> dict[str, float]:
        tl = self.pixel_to_latlng(
            0,
            0,
        )

        br = self.pixel_to_latlng(
            self.img_w,
            self.img_h,
        )

        return {
            "north": tl[0],
            "south": br[0],
            "west": tl[1],
            "east": br[1],
        }

    def to_debug_dict(
        self,
    ) -> dict:
        bbox = self.bbox
        target_x, target_y = self.target_pixel

        return {
            "mode": GEOREFERENCE_MODE,
            "center_lat": round(
                float(
                    self.center_lat
                ),
                8,
            ),
            "center_lng": round(
                float(
                    self.center_lng
                ),
                8,
            ),
            "zoom": round(
                float(
                    self.zoom
                ),
                4,
            ),
            "image_width_px": int(
                self.img_w
            ),
            "image_height_px": int(
                self.img_h
            ),
            "camera_px_x": round(
                float(
                    self.camera_px_x
                ),
                3,
            ),
            "camera_px_y": round(
                float(
                    self.camera_px_y
                ),
                3,
            ),
            "css_per_image_x": round(
                float(
                    self.css_per_image_x
                ),
                6,
            ),
            "css_per_image_y": round(
                float(
                    self.css_per_image_y
                ),
                6,
            ),
            "target_lat": (
                None
                if self.target_lat is None
                else round(
                    float(
                        self.target_lat
                    ),
                    8,
                )
            ),
            "target_lng": (
                None
                if self.target_lng is None
                else round(
                    float(
                        self.target_lng
                    ),
                    8,
                )
            ),
            "target_px_x": round(
                float(
                    target_x
                ),
                3,
            ),
            "target_px_y": round(
                float(
                    target_y
                ),
                3,
            ),
            "meters_per_image_pixel": round(
                float(
                    self.meters_per_pixel
                ),
                6,
            ),
            "deg_per_image_px_lng": round(
                float(
                    self.deg_per_px_lng
                ),
                10,
            ),
            "deg_per_image_px_lat_at_center": round(
                float(
                    self.deg_per_px_lat
                ),
                10,
            ),
            "bbox": bbox,
        }


@dataclass
class PipelineResult:
    query:           str
    status:          str   = "pending"
    error:           str   = ""
    screenshot_path: str   = ""
    geotiff_path:    str   = ""
    geojson_path:    str   = ""
    kml_path:        str   = ""
    kmz_path:        str   = ""
    display_name:    str   = ""
    polygon_source:  str   = ""
    polygon_points:  int   = 0
    center_lat:      float = 0.0
    center_lng:      float = 0.0
    overlap_report_path: str = ""


# ─────────────────────────────────────────────────────────────────────────────
#  QUERY PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_query(raw: str) -> ParsedQuery:
    raw = raw.strip()

    # "Locality, District, ST"
    m = re.match(r"^(.+?)\s*,\s*(.+?)\s*,\s*([A-Za-z]{2})\s*$", raw)
    if m and m.group(3).upper() in STATE_CODES:
        locality = m.group(1).strip()
        district = _strip_admin_suffix(m.group(2).strip())
        code     = m.group(3).upper()
        state    = STATE_CODES[code]
        # FIX BUG 7: removed dead state-rescan block that was copy-pasted
        #            from the generic branch and could never match here.
        return ParsedQuery(
            raw          = raw,
            google_query = f"{locality}, {district}, {state}",
            display_name = f"{locality}, {district}, {code}",
            locality     = locality,
            district     = district,
            state_code   = code,
            state_full   = state,
        )

    # "District, ST"
    m = re.match(r"^(.+?)\s*,\s*([A-Za-z]{2})\s*$", raw)
    if m and m.group(2).upper() in STATE_CODES:
        district = _strip_admin_suffix(m.group(1).strip())
        code     = m.group(2).upper()
        state    = STATE_CODES[code]
        return ParsedQuery(
            raw          = raw,
            google_query = f"{district} District, {state}",
            display_name = f"{district}, {code}",
            district     = district,
            state_code   = code,
            state_full   = state,
        )

    # Plain state name
    clean = raw.lower().replace("india", "").replace("state", "").strip()
    for scode, sname in STATE_CODES.items():
        if clean == sname.lower():
            return ParsedQuery(
                raw          = raw,
                google_query = sname,
                display_name = sname,
                state_code   = scode,
                state_full   = sname,
            )

    return ParsedQuery(
        raw          = raw,
        google_query = raw,
        display_name = raw,
    )


def _strip_admin_suffix(text: str) -> str:
    return re.sub(
        r"\s+(district|zila|jila|tehsil|taluk)$", "",
        text, flags=re.IGNORECASE
    ).strip()


# ─────────────────────────────────────────────────────────────────────────────
#  SELENIUM / MAPS
# ─────────────────────────────────────────────────────────────────────────────


def _normalise_place_label(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    ).casefold()
    value = re.sub(
        r"\bsec(?:tor)?\s*[-:]?\s*(\d+)\b",
        r"sector \1",
        value,
    )
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _expected_place_tokens(expected_label: str) -> list[str]:
    ignored = {"india", "uttar", "pradesh", "district", "city"}
    return [
        token
        for token in _normalise_place_label(expected_label).split()
        if token and token not in ignored
    ]


def _place_text_matches(expected_label: str, candidate_text: str) -> bool:
    expected_tokens = _expected_place_tokens(expected_label)
    candidate_tokens = set(_normalise_place_label(candidate_text).split())
    return bool(
        expected_tokens
        and candidate_tokens
        and all(token in candidate_tokens for token in expected_tokens)
    )


def _read_selected_place_identity(driver) -> dict:
    identity = {
        "primary_title": "",
        "headings": [],
        "place_path": "",
        "document_title": "",
        "search_value": "",
        "url": "",
    }
    try:
        raw = driver.execute_script(
            """
            const headings = [];
            const selectors = [
                'h1.DUwDvf',
                'h1.fontHeadlineLarge',
                '#pane h1',
                '.widget-pane h1',
                '[role="main"] h1',
                '[role="main"] [role="heading"][aria-level="1"]'
            ];
            for (const selector of selectors) {
                for (const element of document.querySelectorAll(selector)) {
                    const text = String(
                        element.innerText || element.textContent || ''
                    ).trim();
                    if (text && !headings.includes(text)) headings.push(text);
                }
            }
            const searchInput =
                document.querySelector('#searchboxinput') ||
                document.querySelector('input[aria-label*="Search"]');
            return {
                headings,
                documentTitle: document.title || '',
                searchValue: searchInput
                    ? String(searchInput.value || '').trim()
                    : '',
                url: location.href
            };
            """
        ) or {}
        headings = [
            str(item).strip()
            for item in raw.get("headings", [])
            if str(item).strip()
        ]
        identity.update({
            "headings": headings,
            "primary_title": headings[0] if headings else "",
            "document_title": str(raw.get("documentTitle", "") or "").strip(),
            "search_value": str(raw.get("searchValue", "") or "").strip(),
            "url": str(raw.get("url", "") or ""),
        })
    except Exception:
        try:
            identity["url"] = str(driver.current_url)
        except Exception:
            pass

    place_match = re.search(
        r"/maps/place/([^/@?]+)",
        identity.get("url", ""),
        flags=re.IGNORECASE,
    )
    if place_match:
        identity["place_path"] = urllib.parse.unquote_plus(place_match.group(1))
    return identity


def _selected_place_matches(expected_label: str, identity: dict) -> bool:
    headings = [
        str(value)
        for value in identity.get("headings", [])
        if str(value).strip()
    ]
    # A matching place heading is the strongest evidence, but a non-matching
    # heading must NOT end the check: Google Maps keeps a hidden
    # accessibility h1 ("Google Maps") in the DOM even when the place panel
    # is collapsed, which would otherwise force a false mismatch and stop
    # the URL/search-box fallbacks from ever running.
    for heading in headings:
        if _place_text_matches(
            expected_label,
            heading,
        ):
            return True

    place_path = str(identity.get("place_path", "") or "")
    if place_path and _place_text_matches(expected_label, place_path):
        return True

    # The side panel is collapsed before capture and zoom/pan can strip the
    # /maps/place/ segment from the URL, so heading and path evidence often
    # vanish for a still-selected place. The search box keeps the exact query
    # this pipeline typed, so it is trustworthy identity evidence.
    search_value = str(identity.get("search_value", "") or "")
    if search_value and _place_text_matches(expected_label, search_value):
        return True

    title = re.sub(
        r"\s*[-|]\s*Google Maps.*$",
        "",
        str(identity.get("document_title", "") or ""),
        flags=re.IGNORECASE,
    )
    return _place_text_matches(expected_label, title)


def _wait_for_selected_place_match(
    driver,
    expected_label: str,
    timeout_seconds: float,
) -> tuple[bool, dict]:
    deadline = time.time() + max(1.0, float(timeout_seconds))
    identity = {}
    while time.time() < deadline:
        identity = _read_selected_place_identity(driver)
        if _selected_place_matches(expected_label, identity):
            return True, identity
        time.sleep(SELECTED_PLACE_LOCK_POLL_SECONDS)
    return False, identity


def ensure_selected_place_lock(
    driver,
    expected_label: str,
    google_query: str,
    view: dict,
    target_zoom: Optional[float] = None,
    log=print,
) -> tuple[dict, dict]:
    global LAST_SELECTED_PLACE_LOCK
    global ACTIVE_SELECTED_PLACE_TRUSTED

    diagnostics = {
        "enabled": bool(
            SELECTED_PLACE_LOCK_ENABLED
        ),
        "expected_label": str(
            expected_label
            or ""
        ),
        "trusted": False,
        "restored": False,
        "attempts": [],
    }

    if (
        not SELECTED_PLACE_LOCK_ENABLED
        or not str(
            expected_label
            or ""
        ).strip()
    ):
        diagnostics.update({
            "trusted": True,
            "status": "not_required",
        })

        ACTIVE_SELECTED_PLACE_TRUSTED = True
        LAST_SELECTED_PLACE_LOCK = diagnostics

        return (
            dict(
                view
            ),
            diagnostics,
        )

    identity = _read_selected_place_identity(
        driver
    )

    diagnostics[
        "initial_identity"
    ] = identity

    current_view = (
        _parse_maps_url(
            driver.current_url
        )
        or dict(
            view
        )
    )

    current_view = _merge_camera_with_place(
        current_view,
        view,
    )

    if _selected_place_matches(
        expected_label,
        identity,
    ):
        diagnostics.update({
            "trusted": True,
            "status": "already_locked",
            "final_identity": identity,
            "actual_zoom": float(
                current_view.get(
                    "zoom",
                    0.0,
                )
            ),
            "requested_zoom": (
                float(
                    target_zoom
                )
                if target_zoom
                is not None
                else None
            ),
        })

        ACTIVE_SELECTED_PLACE_TRUSTED = True
        LAST_SELECTED_PLACE_LOCK = diagnostics

        log(
            "   [Selected Place] Verified: "
            f"{identity.get('primary_title') or identity.get('place_path') or expected_label}"
        )

        return (
            current_view,
            diagnostics,
        )


    for attempt_index in range(
        SELECTED_PLACE_LOCK_MAX_ATTEMPTS
    ):
        attempt = {
            "attempt": int(
                attempt_index
                + 1
            ),
            "method": "",
        }

        try:
            search_input = driver.find_element(
                By.CSS_SELECTOR,
                "#searchboxinput",
            )

            search_input.click()
            search_input.send_keys(
                Keys.CONTROL,
                "a",
            )
            search_input.send_keys(
                Keys.BACKSPACE
            )
            search_input.send_keys(
                google_query
            )
            search_input.send_keys(
                Keys.ENTER
            )

            attempt[
                "method"
            ] = "search_box"

        except Exception:
            try:
                driver.get(
                    "https://www.google.com/maps/place/"
                    + urllib.parse.quote_plus(
                        google_query
                    )
                )

                attempt[
                    "method"
                ] = "search_url"

            except Exception as exc:
                attempt[
                    "error"
                ] = str(
                    exc
                )

        matched, identity = (
            _wait_for_selected_place_match(
                driver,
                expected_label,
                SELECTED_PLACE_LOCK_WAIT_SECONDS,
            )
        )

        attempt.update({
            "matched": bool(
                matched
            ),
            "identity": identity,
        })

        diagnostics[
            "attempts"
        ].append(
            attempt
        )

        if not matched:
            continue

        parsed_camera = _parse_maps_url(
            driver.current_url
        )

        if parsed_camera:
            if parsed_camera.get(
                "place_lat"
            ) is None:
                parsed_camera[
                    "place_lat"
                ] = float(
                    parsed_camera[
                        "lat"
                    ]
                )

                parsed_camera[
                    "place_source"
                ] = (
                    "verified_selected_camera_fallback"
                )

            if parsed_camera.get(
                "place_lng"
            ) is None:
                parsed_camera[
                    "place_lng"
                ] = float(
                    parsed_camera[
                        "lng"
                    ]
                )

                parsed_camera[
                    "place_source"
                ] = (
                    "verified_selected_camera_fallback"
                )

            current_view = parsed_camera

            current_view[
                "name"
            ] = (
                identity.get(
                    "primary_title"
                )
                or expected_label
            )

        diagnostics.update({
            "trusted": True,
            "restored": True,
            "status": "restored_and_locked",
            "final_identity": identity,
            "actual_zoom": float(
                current_view.get(
                    "zoom",
                    0.0,
                )
            ),
            "requested_zoom": (
                float(
                    target_zoom
                )
                if target_zoom
                is not None
                else None
            ),
        })

        ACTIVE_SELECTED_PLACE_TRUSTED = True
        LAST_SELECTED_PLACE_LOCK = diagnostics

        log(
            "   [Selected Place] Restored exact result: "
            f"{identity.get('primary_title') or identity.get('place_path') or expected_label}"
        )

        return (
            current_view,
            diagnostics,
        )

    diagnostics.update({
        "trusted": False,
        "status": "selected_place_lock_failed",
        "final_identity": (
            _read_selected_place_identity(
                driver
            )
        ),
    })

    ACTIVE_SELECTED_PLACE_TRUSTED = False
    LAST_SELECTED_PLACE_LOCK = diagnostics

    actual_title = (
        diagnostics[
            "final_identity"
        ].get(
            "primary_title"
        )
        or diagnostics[
            "final_identity"
        ].get(
            "place_path"
        )
        or "unknown"
    )

    log(
        "   [Selected Place] WARNING: expected "
        f"'{expected_label}' but Google shows "
        f"'{actual_title}'. This capture is diagnostic-only."
    )

    return (
        current_view,
        diagnostics,
    )


def _zoom_matches_requested(
    view: dict,
    requested_zoom: float,
) -> bool:
    # Satellite URLs report metres, so the recovered zoom carries a small
    # rounding error from the measured viewport height. Allow for it.
    tolerance = (
        0.45
        if str(
            view.get(
                "zoom_source",
                "",
            )
        )
        == "metres_token"
        else ZOOM_INTEGRITY_TOLERANCE
    )

    try:
        return (
            abs(
                float(
                    view.get(
                        "zoom"
                    )
                )
                - float(
                    requested_zoom
                )
            )
            <= tolerance
        )

    except (
        TypeError,
        ValueError,
        AttributeError,
    ):
        return False


def lock_selected_place_at_requested_zoom(
    driver,
    expected_label: str,
    google_query: str,
    view: dict,
    requested_zoom: float,
    log=print,
) -> tuple[dict, dict, dict]:
    requested_zoom = float(
        requested_zoom
    )

    current_view = dict(
        view
    )

    final_place_lock = {}

    diagnostics = {
        "enabled": bool(
            ZOOM_INTEGRITY_ENABLED
        ),
        "requested_zoom": requested_zoom,
        "trusted": False,
        "attempts": [],
    }

    for attempt_number in range(
        1,
        max(
            1,
            int(
                ZOOM_INTEGRITY_MAX_ATTEMPTS
            ),
        )
        + 1,
    ):
        attempt_record = {
            "attempt": int(
                attempt_number
            ),
            "requested_zoom": requested_zoom,
            "visual_zoom_error": "",
            "direct_url_fallback": {},
        }

        try:
            (
                current_view,
                final_place_lock,
            ) = ensure_selected_place_lock(
                driver=driver,
                expected_label=expected_label,
                google_query=google_query,
                view=current_view,
                target_zoom=None,
                log=log,
            )

        except Exception as exc:
            attempt_record[
                "place_lock_error"
            ] = str(
                exc
            )

            diagnostics[
                "attempts"
            ].append(
                attempt_record
            )

            log(
                "   [Zoom Integrity] Selected-place verification "
                f"failed safely: {exc}"
            )

            continue

        if not final_place_lock.get(
            "trusted",
            False,
        ):
            attempt_record[
                "place_lock_failed"
            ] = True

            diagnostics[
                "attempts"
            ].append(
                attempt_record
            )

            continue

        try:
            current_view = set_actual_map_zoom(
                driver=driver,
                view=current_view,
                target_zoom=requested_zoom,
                log=log,
            )

        except RuntimeError as exc:
            attempt_record[
                "visual_zoom_error"
            ] = str(
                exc
            )

            parsed_after_failure = _parse_maps_url(
                driver.current_url
            )

            if parsed_after_failure:
                current_view = (
                    _merge_camera_with_place(
                        parsed_after_failure,
                        current_view,
                    )
                )

            log(
                "   [Zoom Integrity] Visual zoom controller "
                "could not land on the exact level; switching "
                "to direct selected-place URL fallback"
            )

        except Exception as exc:
            attempt_record[
                "visual_zoom_error"
            ] = str(
                exc
            )

            log(
                "   [Zoom Integrity] Visual zoom failed safely; "
                "switching to direct URL fallback"
            )

        if not _zoom_matches_requested(
            current_view,
            requested_zoom,
        ):
            if ZOOM_INTEGRITY_DIRECT_URL_FALLBACK:
                forced_view = {
                    **current_view,
                    "zoom": requested_zoom,
                }

                try:
                    (
                        locked_view,
                        lock_diagnostics,
                    ) = _url_lock_camera_on_target(
                        driver=driver,
                        view=forced_view,
                        log=log,
                    )

                    current_view = (
                        _merge_camera_with_place(
                            locked_view,
                            forced_view,
                        )
                    )

                    attempt_record[
                        "direct_url_fallback"
                    ] = dict(
                        lock_diagnostics
                    )

                except Exception as exc:
                    attempt_record[
                        "direct_url_fallback"
                    ] = {
                        "attempted": True,
                        "successful": False,
                        "error": str(
                            exc
                        ),
                    }

            close_maps_ui(
                driver,
                log=log,
            )

            stable_view, fallback_stability = (
                _read_stable_maps_camera(
                    driver=driver,
                    reference_view=current_view,
                    timeout_seconds=5.0,
                    require_change=False,
                    stable_samples=3,
                )
            )

            if stable_view is not None:
                current_view = (
                    _merge_camera_with_place(
                        stable_view,
                        current_view,
                    )
                )

            attempt_record[
                "fallback_stability"
            ] = fallback_stability

        if _zoom_matches_requested(
            current_view,
            requested_zoom,
        ):
            try:
                current_view = recenter_map_on_place(
                    driver=driver,
                    view=current_view,
                    log=log,
                )

            except Exception as exc:
                attempt_record[
                    "centering_error"
                ] = str(
                    exc
                )

            close_maps_ui(
                driver,
                log=log,
            )

            stable_view, final_stability = (
                _read_stable_maps_camera(
                    driver=driver,
                    reference_view=current_view,
                    timeout_seconds=5.0,
                    require_change=False,
                    stable_samples=3,
                )
            )

            if stable_view is not None:
                current_view = (
                    _merge_camera_with_place(
                        stable_view,
                        current_view,
                    )
                )

        else:
            final_stability = {
                "stable": False,
                "reason": (
                    "requested_zoom_not_rendered"
                ),
            }

        selected_match, identity = (
            _wait_for_selected_place_match(
                driver,
                expected_label,
                3.0,
            )
        )

        actual_zoom = float(
            current_view.get(
                "zoom",
                0.0,
            )
        )

        zoom_error = abs(
            actual_zoom
            - requested_zoom
        )

        zoom_match = bool(
            zoom_error
            <= ZOOM_INTEGRITY_TOLERANCE
        )

        camera_safe = bool(
            zoom_match
            and _capture_camera_is_safe(
                current_view
            )
        )

        attempt_record.update({
            "actual_zoom": actual_zoom,
            "zoom_error": round(
                zoom_error,
                4,
            ),
            "zoom_match": zoom_match,
            "nearest_half_step": bool(
                not zoom_match
                and zoom_error
                <= (
                    ZOOM_INTEGRITY_NEAREST_STEP_DIAGNOSTIC_LIMIT
                )
            ),
            "selected_place_match": bool(
                selected_match
            ),
            "camera_safe": camera_safe,
            "identity": identity,
            "final_stability": final_stability,
        })

        diagnostics[
            "attempts"
        ].append(
            attempt_record
        )

        log(
            "   [Zoom Integrity] "
            f"requested={requested_zoom:.2f}, "
            f"actual={actual_zoom:.2f}, "
            f"place={'ok' if selected_match else 'mismatch'}, "
            f"camera={'safe' if camera_safe else 'unsafe'}"
        )

        if (
            selected_match
            and zoom_match
            and camera_safe
        ):
            diagnostics.update({
                "trusted": True,
                "status": (
                    "selected_place_zoom_locked"
                ),
                "actual_zoom": actual_zoom,
                "final_identity": identity,
            })

            final_place_lock = {
                **final_place_lock,
                "trusted": True,
                "final_identity": identity,
                "zoom_integrity": dict(
                    diagnostics
                ),
            }

            return (
                current_view,
                final_place_lock,
                diagnostics,
            )

    diagnostics.update({
        "trusted": False,
        "status": "zoom_integrity_failed",
        "actual_zoom": float(
            current_view.get(
                "zoom",
                0.0,
            )
        ),
        "skip_capture": bool(
            ZOOM_INTEGRITY_SKIP_CAPTURE_ON_FAILURE
        ),
    })

    final_place_lock = {
        **final_place_lock,
        "trusted": False,
        "zoom_integrity": dict(
            diagnostics
        ),
    }

    log(
        "   [Zoom Integrity] WARNING: requested zoom "
        f"{requested_zoom:.2f} could not be rendered exactly. "
        "The comparison capture will be skipped safely."
    )

    return (
        current_view,
        final_place_lock,
        diagnostics,
    )


def set_satellite_view(
    driver,
    enabled: bool,
    log=print,
) -> bool:
    """
    Switch the Google Maps base layer between map and satellite.

    Google exposes the layer switcher as a hover flyout at the bottom left,
    so the satellite control is absent from the DOM until the thumbnail is
    hovered. The hover is performed first, and a URL rewrite is used only as
    a fallback. The page is never navigated unless a layer change is actually
    required, because navigation reopens the place panel and would shrink the
    usable map area of the next capture.
    """
    global ACTIVE_SATELLITE_VIEW


    def layer_is_satellite() -> bool:
        try:
            return bool(
                driver.execute_script(
                    """
                    const canvases = Array.from(
                        document.querySelectorAll('canvas')
                    );
                    if (/!1e3/.test(location.href)) return true;
                    const label = document.querySelector(
                        '[aria-label*=\"Show street map\"],'
                        + '[aria-label*=\"street map\"]'
                    );
                    return Boolean(label);
                    """
                )
            )
        except Exception:
            return False

    if layer_is_satellite() == bool(enabled):
        ACTIVE_SATELLITE_VIEW = bool(enabled)
        return True

    # Step 1: hover the layer thumbnail so its options are rendered.
    hovered = False

    for selector in (
        "button[aria-label*='Layers']",
        "[jsaction*='minimap']",
        ".gm-style-mtc",
        "#minimap",
    ):
        try:
            elements = [
                element
                for element in driver.find_elements(
                    By.CSS_SELECTOR,
                    selector,
                )
                if element.is_displayed()
            ]

            if not elements:
                continue

            elements[0].click()
            hovered = True
            time.sleep(1.5)
            break

        except Exception:
            continue

    if hovered:
        try:
            clicked = bool(
                driver.execute_script(
                    """
                    const wanted = arguments[0];
                    const nodes = Array.from(
                        document.querySelectorAll(
                            'button, [role=\"button\"], [role=\"menuitem\"],'
                            + '[role=\"menuitemradio\"], [jsaction]'
                        )
                    );
                    for (const element of nodes) {
                        const rect = element.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const text = [
                            element.getAttribute('aria-label') || '',
                            element.getAttribute('title') || '',
                            element.textContent || ''
                        ].join(' ').toLowerCase();
                        const isSatellite = (
                            text.includes('satellite') ||
                            text.includes('imagery')
                        );
                        const isMap = (
                            text.includes('street map') ||
                            text.includes('default')
                        );
                        if (
                            (wanted && isSatellite && !isMap) ||
                            (!wanted && isMap)
                        ) {
                            element.click();
                            return true;
                        }
                    }
                    return false;
                    """,
                    bool(enabled),
                )
            )
        except Exception:
            clicked = False

        if clicked:
            time.sleep(7)

            active = layer_is_satellite()
            ACTIVE_SATELLITE_VIEW = bool(active)

            log(
                "   [Satellite] Base layer now: "
                f"{'satellite' if active else 'map'} (hover control)"
            )

            if active == bool(enabled):
                return True

    # Step 2: URL fallback. Only attempted when the control was not usable.
    try:
        current_url = str(
            driver.current_url
        )
    except Exception:
        current_url = ""

    if not current_url:
        log(
            "   [Satellite] Layer switch unavailable; "
            "keeping the current base map"
        )
        return False

    cleaned_url = re.sub(
        r"!5m1!1e[0-9]+",
        "",
        current_url,
    )

    if enabled:
        # !3m1!4b1 is the base-layer element of a place URL; !3m1!1e3
        # selects satellite imagery in the same slot.
        if "!3m1!4b1" in cleaned_url:
            target_url = cleaned_url.replace(
                "!3m1!4b1",
                "!3m1!1e3",
                1,
            )
        elif "/data=" in cleaned_url:
            target_url = cleaned_url + "!5m1!1e3"
        else:
            target_url = ""
    else:
        target_url = cleaned_url.replace(
            "!3m1!1e3",
            "!3m1!4b1",
            1,
        )

    if not target_url or target_url == current_url:
        log(
            "   [Satellite] Layer switch unavailable; "
            "keeping the current base map"
        )
        return False

    try:
        driver.get(
            target_url
        )
    except Exception as exc:
        log(
            "   [Satellite] Layer navigation failed safely: "
            f"{exc}"
        )
        return False

    time.sleep(8)

    active = layer_is_satellite()
    ACTIVE_SATELLITE_VIEW = bool(active)

    log(
        "   [Satellite] Base layer now: "
        f"{'satellite' if active else 'map'} (url fallback)"
    )

    return active == bool(enabled)


class By:
    CSS_SELECTOR = "css"
    XPATH = "xpath"
    TAG_NAME = "tag"
    ID = "id"
    NAME = "name"
    CLASS_NAME = "class"


class PlaywrightElement:
    def __init__(self, locator):
        self.locator = locator

    @property
    def text(self) -> str:
        try:
            return self.locator.text_content(timeout=1500) or ""
        except Exception:
            return ""

    def is_displayed(self) -> bool:
        try:
            return self.locator.is_visible(timeout=1500)
        except Exception:
            return False

    @property
    def size(self) -> dict:
        try:
            box = self.locator.bounding_box(timeout=1500)
            if box:
                return {"width": float(box["width"]), "height": float(box["height"])}
        except Exception:
            pass
        return {"width": 0.0, "height": 0.0}

    @property
    def rect(self) -> dict:
        try:
            box = self.locator.bounding_box(timeout=1500)
            if box:
                return box
        except Exception:
            pass
        return {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}

    def click(self):
        try:
            self.locator.click(timeout=3000)
        except Exception:
            pass

    def send_keys(self, *keys):
        try:
            for k in keys:
                self.locator.press(str(k))
        except Exception:
            pass

    def get_attribute(self, name: str) -> str:
        try:
            return self.locator.get_attribute(name, timeout=1500) or ""
        except Exception:
            return ""


class PlaywrightDriver:
    """
    Drop-in Playwright browser wrapper providing full compatibility with
    the existing pipeline's DOM evaluation, screenshotting, URL tracking,
    and native mouse/keyboard controls without Selenium session crashes.
    """
    def __init__(self, playwright_inst, browser, context, page):
        self._playwright = playwright_inst
        self._browser = browser
        self._context = context
        self.page = page

    @property
    def current_url(self) -> str:
        try:
            return self.page.evaluate("() => window.location.href") or self.page.url
        except Exception:
            return self.page.url if hasattr(self, "page") else ""

    @property
    def title(self) -> str:
        try:
            return self.page.title()
        except Exception:
            return ""

    def get(self, url: str):
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass

    def execute_script(self, script: str, *args):
        try:
            clean = script.strip()
            if args:
                js_wrapper = f"""(args) => {{
                    const arguments = args;
                    {clean}
                }}"""
                return self.page.evaluate(js_wrapper, list(args))
            else:
                if "return " in clean or clean.startswith("const ") or clean.startswith("let ") or clean.startswith("var "):
                    return self.page.evaluate(f"() => {{ {clean} }}")
                return self.page.evaluate(clean)
        except Exception:
            return None

    def save_screenshot(self, filename: str):
        try:
            self.page.screenshot(path=filename)
            return True
        except Exception:
            return False

    def get_screenshot_as_png(self) -> bytes:
        return self.page.screenshot(type="png")

    def get_screenshot_as_base64(self) -> str:
        import base64
        return base64.b64encode(self.page.screenshot(type="png")).decode("ascii")

    def screenshot(self, path: str = None, **kwargs):
        return self.page.screenshot(path=path, **kwargs)

    def find_element(self, by=None, selector: str = ""):
        try:
            loc = self.page.locator(selector).first
            if loc.count() > 0:
                return PlaywrightElement(loc)
        except Exception:
            pass
        raise RuntimeError(f"Element not found: {selector}")

    def find_elements(self, by=None, selector: str = ""):
        try:
            locs = self.page.locator(selector)
            return [PlaywrightElement(locs.nth(i)) for i in range(locs.count())]
        except Exception:
            return []

    def set_window_size(self, width: int, height: int):
        pass

    def set_page_load_timeout(self, timeout: int):
        try:
            self.page.set_default_navigation_timeout(timeout * 1000)
        except Exception:
            pass

    def set_script_timeout(self, timeout: int):
        try:
            self.page.set_default_timeout(timeout * 1000)
        except Exception:
            pass

    def close(self):
        self.quit()

    def quit(self):
        try:
            self.page.close()
        except Exception:
            pass
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass


def open_maps_playwright(
    google_query: str,
    log=print,
) -> tuple[object, dict]:
    """
    Open Google Maps using Microsoft Playwright and wait for the visual map canvas to render.
    """
    from playwright.sync_api import sync_playwright

    log(f"   [Maps] Opening (Playwright): {google_query}")

    p = sync_playwright().start()
    launch_args = [
        "--disable-notifications",
        "--disable-popup-blocking",
        "--no-first-run",
        "--enable-webgl",
        "--ignore-gpu-blocklist",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    browser = None
    try:
        browser = p.chromium.launch(
            headless=BOUNDARY_HEADLESS,
            args=launch_args,
        )
    except Exception as launch_err:
        # Fallback to system-installed Microsoft Edge (pre-installed on Windows 10/11)
        try:
            browser = p.chromium.launch(
                headless=BOUNDARY_HEADLESS,
                channel="msedge",
                args=launch_args,
            )
        except Exception:
            # Fallback to system-installed Google Chrome
            try:
                browser = p.chromium.launch(
                    headless=BOUNDARY_HEADLESS,
                    channel="chrome",
                    args=launch_args,
                )
            except Exception:
                raise launch_err
    context = browser.new_context(
        viewport={"width": 1540, "height": 732},
        device_scale_factor=1.25,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    page = context.new_page()
    page.set_default_navigation_timeout(45000)
    page.set_default_timeout(30000)

    driver = PlaywrightDriver(p, browser, context, page)

    url = (
        "https://www.google.com/maps/place/"
        + urllib.parse.quote_plus(google_query)
    )

    try:
        page.goto(url, wait_until="commit", timeout=45000)
    except Exception:
        pass

    # Dismiss Google consent screen when it appears.
    consent_selectors = [
        "button[aria-label='Accept all']",
        "button[jsname='higCR']",
        "form[action*='consent'] button",
    ]
    for selector in consent_selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                loc.click(timeout=2000)
                time.sleep(1)
                break
        except Exception:
            continue

    # Wait for coordinates in URL.
    view = None
    for _ in range(60):
        time.sleep(0.5)
        live_url = driver.current_url
        parsed = _parse_maps_url(live_url)
        if parsed and parsed.get("lat") is not None and parsed.get("zoom") is not None:
            view = parsed
            break

    if not view:
        # Fallback: extract center from Google Maps client state
        try:
            eval_view = page.evaluate("""() => {
                const center = window.APP_INITIALIZATION_STATE?.[0]?.[0]?.[1];
                if (Array.isArray(center) && center.length >= 3) {
                    return { lat: center[2], lng: center[1], zoom: 15.0 };
                }
                return null;
            }""")
            if eval_view:
                view = eval_view
        except Exception:
            pass

    if not view:
        current_url = page.url
        debug_path = os.path.abspath("maps_url_error_debug.png")
        driver.save_screenshot(debug_path)
        driver.quit()
        raise RuntimeError(
            "Could not extract coordinates from Maps URL: "
            f"{current_url[:180]}. Debug: {debug_path}"
        )

    # WebGL tiles rendering settle.
    time.sleep(3)

    name = google_query
    try:
        h1 = page.locator("h1.DUwDvf, h1.fontHeadlineLarge").first
        if h1.count() > 0:
            text = h1.text_content().strip()
            if text:
                name = text
    except Exception:
        pass

    view["name"] = name

    log(
        f"   [Maps] Center: {view['lat']:.5f}, "
        f"{view['lng']:.5f}  Zoom: {view['zoom']}"
    )

    return driver, view


# Backward-compatible alias
open_maps_selenium = open_maps_playwright


def _parse_maps_url(
    url: str,
) -> Optional[dict]:
    """
    Read only the Google Maps camera centre from @lat,lng,zoom.

    The !3d/!4d pair identifies a place marker and may be far from the actual
    rendered viewport centre when a side panel is open. It must not be used
    for screenshot georeferencing.
    """
    match = re.search(
        r"@(-?\d+(?:\.\d+)?),"
        r"(-?\d+(?:\.\d+)?),"
        r"(\d+(?:\.\d+)?)([zm])",
        url or "",
    )

    if not match:
        return None

    latitude = float(
        match.group(1)
    )

    camera_value = float(
        match.group(3)
    )

    camera_unit = match.group(4)

    if camera_unit == "z":
        zoom_value = camera_value
        zoom_source = "zoom_token"

    else:
        # Satellite view reports the vertical extent of the viewport in
        # metres. Web Mercator fixes metres-per-pixel at zoom 0, so the zoom
        # follows directly from the rendered map height.
        metres = max(
            camera_value,
            1.0,
        )

        viewport_height = max(
            float(
                LAST_MAP_VIEWPORT_HEIGHT_CSS
            ),
            1.0,
        )

        zoom_value = math.log2(
            156543.03392
            * math.cos(
                math.radians(
                    latitude
                )
            )
            * viewport_height
            / metres
        )

        zoom_value = max(
            0.0,
            min(
                22.0,
                zoom_value,
            ),
        )

        zoom_source = "metres_token"

    result = {
        "lat": latitude,
        "lng": float(
            match.group(2)
        ),
        "zoom": zoom_value,
        "center_source": "viewport_at",
        "zoom_source": zoom_source,
    }

    # Google Maps place URLs normally retain the selected place coordinates
    # in !3d<latitude>!4d<longitude>. These are different from the viewport
    # camera whenever the left information panel offsets the selected place.
    place_match = re.search(
        r"!3d(-?\d+(?:\.\d+)?)"
        r"!4d(-?\d+(?:\.\d+)?)",
        url or "",
    )

    if place_match:
        result["place_lat"] = float(
            place_match.group(1)
        )
        result["place_lng"] = float(
            place_match.group(2)
        )
        result["place_source"] = "url_3d4d"

    return result

def close_maps_ui(driver, log=print):
    """
    Close or hide the Google Maps left information panel and dismiss small
    popups before measuring the viewport and taking a screenshot.
    """

    panel_selectors = (
        'button[aria-label*="Collapse side panel"]',
        'button[aria-label*="Hide side panel"]',
        'button[jsaction*="drawer.close"]',
        'button[jsaction*="pane.collapse"]',
        'button[title*="Collapse side panel"]',
        'button[aria-label*="Collapse"]',
        'button[aria-label*="collapse"]',
    )

    panel_button_clicked = False

    for selector in panel_selectors:
        try:
            if hasattr(driver, "page"):
                loc = driver.page.locator(selector).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=1500)
                    panel_button_clicked = True
                    log("   [Maps UI] Side panel collapse control clicked")
                    time.sleep(2)
                    break
        except Exception:
            continue

    if not panel_button_clicked:
        try:
            panel_button_clicked = bool(driver.execute_script("""
                const btn = document.querySelector('button[aria-label*="Collapse side panel"], button[aria-label*="Hide side panel"], button[jsaction*="drawer.close"], button[jsaction*="pane.collapse"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            """))
            if panel_button_clicked:
                log("   [Maps UI] Side panel collapse control clicked via JS")
                time.sleep(2)
        except Exception:
            pass

    def read_panel_width() -> float:
        try:
            return float(
                driver.execute_script(
                    """
                    const collapseBtn = document.querySelector('button[aria-label*="Collapse side panel"], button[aria-label*="Hide side panel"], button[jsaction*="drawer.close"]');
                    if (collapseBtn && collapseBtn.getBoundingClientRect().width > 0) {
                        return collapseBtn.getBoundingClientRect().left > 0 ? collapseBtn.getBoundingClientRect().left : 480;
                    }

                    const expandBtn = document.querySelector('button[aria-label*="Expand side panel"], button[aria-label*="Show side panel"], button[jsaction*="drawer.open"]');
                    if (expandBtn && expandBtn.getBoundingClientRect().width > 0) {
                        return 0;
                    }

                    const candidates = [
                        document.querySelector('#pane'),
                        document.querySelector('.widget-pane'),
                        document.querySelector('.w6VYqd')
                    ].filter(Boolean);

                    for (const pane of candidates) {
                        const rect = pane.getBoundingClientRect();
                        const style = getComputedStyle(pane);

                        const visible = (
                            rect.width > 200 &&
                            rect.height > 200 &&
                            rect.right > 0 &&
                            rect.left < 50 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            Number(style.opacity || 1) > 0
                        );

                        if (visible) {
                            return rect.width;
                        }
                    }

                    return 0;
                    """
                )
                or 0.0
            )

        except Exception:
            return 0.0

    panel_width = read_panel_width()

    if panel_width > 50:
        try:
            driver.execute_script(
                """
                const candidates = [
                    document.querySelector('#pane'),
                    document.querySelector('.widget-pane'),
                    document.querySelector('.w6VYqd'),
                    document.querySelector('div.m6QErb.DxyBCb'),
                    ...document.querySelectorAll('div[role="region"], div[role="main"]')
                ].filter(Boolean);

                for (const pane of candidates) {
                    const rect = pane.getBoundingClientRect();
                    if (rect.left < 50 && rect.width > 200) {
                        pane.style.setProperty(
                            'display',
                            'none',
                            'important'
                        );
                        pane.style.setProperty(
                            'visibility',
                            'hidden',
                            'important'
                        );
                        pane.style.setProperty(
                            'width',
                            '0px',
                            'important'
                        );
                        pane.style.setProperty(
                            'min-width',
                            '0px',
                            'important'
                        );
                        pane.style.setProperty(
                            'max-width',
                            '0px',
                            'important'
                        );
                        pane.style.setProperty(
                            'pointer-events',
                            'none',
                            'important'
                        );
                    }
                }

                window.dispatchEvent(
                    new Event('resize')
                );
                """
            )

            time.sleep(3)
            panel_width = read_panel_width()

            log(
                "   [Maps UI] Side panel force-hidden"
            )

        except Exception as exc:
            log(
                "   [Maps UI] Could not force-hide "
                f"side panel: {exc}"
            )

    panel_closed = panel_width <= 50

    try:
        driver.execute_script(
            """
            window.__jarvis_panel_verified_closed =
                arguments[0];
            """,
            panel_closed,
        )
    except Exception:
        pass

    log(
        "   [Maps UI] Final panel width: "
        f"{panel_width:.1f}"
    )

    if panel_closed:
        log(
            "   [Maps UI] Side panel verified closed"
        )
    else:
        log(
            "   [Maps UI] WARNING: side panel is still visible"
        )

    try:
        buttons = driver.find_elements(
            By.TAG_NAME,
            "button",
        )

        for button in buttons:
            text_value = (
                button.text
                or ""
            ).strip().lower()

            aria_label = (
                button.get_attribute(
                    "aria-label"
                )
                or ""
            ).strip().lower()

            should_click = (
                text_value in {
                    "no thanks",
                    "not now",
                }
                or (
                    aria_label == "close"
                    and button.is_displayed()
                )
            )

            if not should_click:
                continue

            driver.execute_script(
                "arguments[0].click();",
                button,
            )

            log(
                "   [Maps UI] Feedback popup dismissed"
            )

            time.sleep(1)
            break

    except Exception:
        pass





def _mercator_fraction_y(
    latitude: float,
) -> float:
    latitude = max(
        -85.05112878,
        min(
            85.05112878,
            float(
                latitude
            ),
        ),
    )

    sine_value = math.sin(
        math.radians(
            latitude
        )
    )

    return (
        0.5
        - math.log(
            (
                1.0
                + sine_value
            )
            / (
                1.0
                - sine_value
            )
        )
        / (
            4.0
            * math.pi
        )
    )


def _target_offset_from_camera(
    view: dict,
) -> Optional[tuple[float, float]]:
    place_latitude = view.get(
        "place_lat"
    )

    place_longitude = view.get(
        "place_lng"
    )

    if (
        place_latitude is None
        or place_longitude is None
    ):
        return None

    camera_latitude = float(
        view.get(
            "lat",
            place_latitude,
        )
    )

    camera_longitude = float(
        view.get(
            "lng",
            place_longitude,
        )
    )

    zoom = float(
        view.get(
            "zoom",
            15.0,
        )
    )

    world_size = (
        256.0
        * (
            2.0
            ** zoom
        )
    )

    offset_x = (
        (
            float(
                place_longitude
            )
            - camera_longitude
        )
        / 360.0
        * world_size
    )

    offset_y = (
        _mercator_fraction_y(
            float(
                place_latitude
            )
        )
        - _mercator_fraction_y(
            camera_latitude
        )
    ) * world_size

    return (
        float(
            offset_x
        ),
        float(
            offset_y
        ),
    )


def _target_offset_is_centered(
    offset_x: float,
    offset_y: float,
) -> bool:
    return (
        abs(
            float(
                offset_x
            )
        )
        <= TARGET_CENTER_TOLERANCE_X_PX
        and abs(
            float(
                offset_y
            )
        )
        <= TARGET_CENTER_TOLERANCE_Y_PX
    )


def _axis_pan_delta(
    offset: float,
    preferred_residual: float,
    maximum_drag: float,
) -> float:
    offset = float(
        offset
    )

    if abs(
        offset
    ) <= preferred_residual:
        return 0.0

    desired_residual = math.copysign(
        preferred_residual,
        offset,
    )

    # Dragging the map in the opposite direction of target-camera offset
    # moves the target toward the viewport centre.
    drag_delta = -(
        offset
        - desired_residual
    )

    return max(
        -float(
            maximum_drag
        ),
        min(
            float(
                maximum_drag
            ),
            drag_delta,
        ),
    )


def _planned_target_pan(
    offset_x: float,
    offset_y: float,
) -> tuple[int, int]:
    drag_x = _axis_pan_delta(
        offset=offset_x,
        preferred_residual=(
            TARGET_CENTER_PREFERRED_RESIDUAL_X_PX
        ),
        maximum_drag=(
            TARGET_CENTER_MAX_DRAG_X_PX
        ),
    )

    drag_y = _axis_pan_delta(
        offset=offset_y,
        preferred_residual=(
            TARGET_CENTER_PREFERRED_RESIDUAL_Y_PX
        ),
        maximum_drag=(
            TARGET_CENTER_MAX_DRAG_Y_PX
        ),
    )

    return (
        int(
            round(
                drag_x
            )
        ),
        int(
            round(
                drag_y
            )
        ),
    )


def _merge_camera_with_place(
    camera: Optional[dict],
    previous_view: dict,
) -> dict:
    if not camera:
        return dict(
            previous_view
        )

    merged = dict(
        camera
    )

    for key in (
        "place_lat",
        "place_lng",
        "place_source",
        "name",
    ):
        if previous_view.get(
            key
        ) is not None:
            merged[
                key
            ] = previous_view[
                key
            ]

    return merged



def _maps_url_with_camera(
    current_url: str,
    latitude: float,
    longitude: float,
    zoom: float,
) -> str:
    """
    Return the same Google Maps place URL with a different @lat,lng,zoom
    camera segment. The selected place/data section is preserved.
    """
    camera_segment = (
        f"@{float(latitude):.7f},"
        f"{float(longitude):.7f},"
        f"{float(zoom):.2f}z"
    )

    # Satellite URLs carry a metres token, so both forms must be replaceable.
    camera_pattern = re.compile(
        r"@[-+]?\d+(?:\.\d+)?,"
        r"[-+]?\d+(?:\.\d+)?,"
        r"\d+(?:\.\d+)?[zm]"
    )

    if camera_pattern.search(
        current_url
    ):
        return camera_pattern.sub(
            camera_segment,
            current_url,
            count=1,
        )

    data_marker = "/data="

    if data_marker in current_url:
        prefix, suffix = current_url.split(
            data_marker,
            1,
        )

        return (
            prefix.rstrip("/")
            + "/"
            + camera_segment
            + data_marker
            + suffix
        )

    query_marker = "?"

    if query_marker in current_url:
        prefix, suffix = current_url.split(
            query_marker,
            1,
        )

        return (
            prefix.rstrip("/")
            + "/"
            + camera_segment
            + query_marker
            + suffix
        )

    return (
        current_url.rstrip("/")
        + "/"
        + camera_segment
    )




def _camera_change_distance(
    first_view: dict,
    second_view: dict,
) -> float:
    if not first_view or not second_view:
        return float("inf")

    latitude_scale = 111320.0
    mean_latitude = math.radians(
        (
            float(first_view.get("lat", 0.0))
            + float(second_view.get("lat", 0.0))
        )
        / 2.0
    )
    longitude_scale = max(
        1.0,
        latitude_scale * math.cos(mean_latitude),
    )

    latitude_delta = (
        float(second_view.get("lat", 0.0))
        - float(first_view.get("lat", 0.0))
    ) * latitude_scale
    longitude_delta = (
        float(second_view.get("lng", 0.0))
        - float(first_view.get("lng", 0.0))
    ) * longitude_scale

    return float(
        math.hypot(
            latitude_delta,
            longitude_delta,
        )
    )


def _read_stable_maps_camera(
    driver,
    reference_view: dict,
    timeout_seconds: float,
    require_change: bool = False,
    stable_samples: int = TARGET_CENTER_STABLE_SAMPLES,
) -> tuple[Optional[dict], dict]:
    deadline = time.time() + max(1.0, float(timeout_seconds))
    last_camera = None
    stable_count = 0
    observed_count = 0
    changed = False

    while time.time() < deadline:
        parsed = _parse_maps_url(driver.current_url)

        if parsed:
            camera = _merge_camera_with_place(parsed, reference_view)
            observed_count += 1

            changed = (
                _camera_change_distance(reference_view, camera) >= 0.08
                or abs(
                    float(camera.get("zoom", 0.0))
                    - float(reference_view.get("zoom", 0.0))
                ) >= 0.02
            )

            if require_change and not changed:
                stable_count = 0
                last_camera = camera
            else:
                if last_camera is not None:
                    same_camera = (
                        abs(float(camera["lat"]) - float(last_camera["lat"]))
                        <= TARGET_CENTER_CAMERA_EPSILON_DEGREES
                        and abs(float(camera["lng"]) - float(last_camera["lng"]))
                        <= TARGET_CENTER_CAMERA_EPSILON_DEGREES
                        and abs(
                            float(camera.get("zoom", 0.0))
                            - float(last_camera.get("zoom", 0.0))
                        ) <= 0.02
                    )
                else:
                    same_camera = False

                stable_count = stable_count + 1 if same_camera else 1
                last_camera = camera

                if stable_count >= max(2, int(stable_samples)):
                    return camera, {
                        "stable": True,
                        "changed": bool(changed),
                        "samples": int(observed_count),
                        "stable_samples": int(stable_count),
                    }

        time.sleep(TARGET_CENTER_STABLE_INTERVAL_SECONDS)

    return last_camera, {
        "stable": False,
        "changed": bool(changed),
        "samples": int(observed_count),
        "stable_samples": int(stable_count),
    }


def _capture_camera_is_safe(view: dict) -> bool:
    offset = _target_offset_from_camera(view)
    if offset is None:
        return False

    offset_x, offset_y = offset
    return (
        abs(float(offset_x)) <= TARGET_CENTER_CAPTURE_MAX_OFFSET_X_PX
        and abs(float(offset_y)) <= TARGET_CENTER_CAPTURE_MAX_OFFSET_Y_PX
    )


def _url_lock_camera_on_target(
    driver,
    view: dict,
    log=print,
) -> tuple[dict, dict]:
    place_latitude = view.get("place_lat")
    place_longitude = view.get("place_lng")

    if place_latitude is None or place_longitude is None:
        return view, {
            "attempted": False,
            "successful": False,
            "reason": "missing_place_coordinates",
        }

    zoom = float(view.get("zoom", 15.0))
    lock_url = _maps_url_with_camera(
        current_url=driver.current_url,
        latitude=float(place_latitude),
        longitude=float(place_longitude),
        zoom=zoom,
    )

    log(
        "   [Camera Lock] Applying selected-place camera lock "
        f"at zoom {zoom:.2f}"
    )

    try:
        driver.get(lock_url)
    except Exception as exc:
        return view, {
            "attempted": True,
            "successful": False,
            "reason": "navigation_failed",
            "error": str(exc),
        }

    time.sleep(TARGET_CENTER_URL_LOCK_SETTLE_SECONDS)

    locked_view, stability = _read_stable_maps_camera(
        driver=driver,
        reference_view=view,
        timeout_seconds=TARGET_CENTER_URL_LOCK_WAIT_SECONDS,
        require_change=False,
    )

    if locked_view is None:
        return view, {
            "attempted": True,
            "successful": False,
            "reason": "camera_not_readable_after_lock",
            "stability": stability,
        }

    locked_view = _merge_camera_with_place(locked_view, view)
    offset = _target_offset_from_camera(locked_view)
    safe = _capture_camera_is_safe(locked_view)

    diagnostics = {
        "attempted": True,
        "successful": bool(safe and stability.get("stable", False)),
        "stability": stability,
        "camera": {
            "lat": float(locked_view["lat"]),
            "lng": float(locked_view["lng"]),
            "zoom": float(locked_view["zoom"]),
        },
        "offset_px": (
            {
                "x": round(float(offset[0]), 3),
                "y": round(float(offset[1]), 3),
                "distance": round(float(math.hypot(*offset)), 3),
            }
            if offset is not None
            else None
        ),
    }

    if diagnostics["successful"]:
        log(
            "   [Camera Lock] Verified stable selected-place camera: "
            f"offset=({offset[0]:.1f}, {offset[1]:.1f})px"
        )
    else:
        log(
            "   [Camera Lock] WARNING: lock did not reach a stable "
            "capture-safe camera"
        )

    return locked_view, diagnostics


def recenter_map_on_place(
    driver,
    view: dict,
    log=print,
    maximum_attempts: int = TARGET_CENTER_MAX_ATTEMPTS,
) -> dict:
    """
    Reconcile delayed Google Maps camera movement and produce a stable,
    capture-safe camera around the selected locality.

    Large offsets use a deterministic camera-segment lock that preserves the
    selected-place URL/data. Smaller offsets may use a real pointer pan, but
    the result is accepted only after multiple stable camera samples.
    """
    global LAST_RECENTER_DIAGNOSTICS

    original_view = dict(view)

    observed_view, observed_stability = _read_stable_maps_camera(
        driver=driver,
        reference_view=original_view,
        timeout_seconds=3.0,
        require_change=False,
        stable_samples=3,
    )

    current_view = _merge_camera_with_place(
        observed_view or original_view,
        original_view,
    )

    if observed_view and _camera_change_distance(original_view, current_view) >= 0.08:
        log(
            "   [Camera Lock] Reconciled a delayed camera update before "
            "centering"
        )

    initial_offset = _target_offset_from_camera(current_view)

    LAST_RECENTER_DIAGNOSTICS = {
        "enabled": bool(TARGET_CENTERING_ENABLED),
        "status": "started",
        "initial_camera": {
            "lat": float(current_view.get("lat", 0.0)),
            "lng": float(current_view.get("lng", 0.0)),
            "zoom": float(current_view.get("zoom", 0.0)),
        },
        "initial_stability": observed_stability,
        "attempts": [],
    }

    if not TARGET_CENTERING_ENABLED:
        LAST_RECENTER_DIAGNOSTICS.update({
            "status": "disabled",
            "successful": _capture_camera_is_safe(current_view),
        })
        return current_view

    if initial_offset is None:
        LAST_RECENTER_DIAGNOSTICS.update({
            "status": "missing_selected_place_coordinates",
            "successful": False,
        })
        log(
            "   [Target Center] Selected-place coordinates are unavailable; "
            "actual camera retained"
        )
        return current_view

    initial_x, initial_y = initial_offset
    initial_distance = float(math.hypot(initial_x, initial_y))
    LAST_RECENTER_DIAGNOSTICS["initial_offset_px"] = {
        "x": round(initial_x, 3),
        "y": round(initial_y, 3),
        "distance": round(initial_distance, 3),
    }

    if _capture_camera_is_safe(current_view):
        LAST_RECENTER_DIAGNOSTICS.update({
            "status": "already_capture_safe",
            "successful": True,
            "attempt_count": 0,
            "final_camera": {
                "lat": float(current_view["lat"]),
                "lng": float(current_view["lng"]),
                "zoom": float(current_view["zoom"]),
            },
            "final_offset_px": LAST_RECENTER_DIAGNOSTICS["initial_offset_px"],
        })
        log(
            "   [Target Center] Camera already capture-safe; "
            f"target offset=({initial_x:.1f}, {initial_y:.1f})px"
        )
        return current_view

    # Large offsets are locked directly. This avoids Selenium drag inertia
    # being applied several seconds later during the next zoom operation.
    if initial_distance >= TARGET_CENTER_DIRECT_LOCK_THRESHOLD_PX:
        locked_view, lock_diagnostics = _url_lock_camera_on_target(
            driver=driver,
            view=current_view,
            log=log,
        )
        LAST_RECENTER_DIAGNOSTICS["url_lock"] = lock_diagnostics
        current_view = _merge_camera_with_place(locked_view, original_view)

        scene = None
        for selector in (
            "#scene",
            ".widget-scene",
            ".widget-scene-canvas",
            "canvas",
        ):
            try:
                visible = [
                    element
                    for element in driver.find_elements(By.CSS_SELECTOR, selector)
                    if element.is_displayed()
                    and element.size.get("width", 0) >= 500
                    and element.size.get("height", 0) >= 300
                ]
                if visible:
                    scene = max(
                        visible,
                        key=lambda element: (
                            element.size.get("width", 0)
                            * element.size.get("height", 0)
                        ),
                    )
                    break
            except Exception:
                continue

        previous_distance = initial_distance

        if scene is not None:
            for attempt_number in range(1, max(1, int(maximum_attempts)) + 1):
                offset = _target_offset_from_camera(current_view)
                if offset is None or _capture_camera_is_safe(current_view):
                    break

                offset_x, offset_y = offset
                drag_x, drag_y = _planned_target_pan(offset_x, offset_y)
                if drag_x == 0 and drag_y == 0:
                    break

                before_view = dict(current_view)
                log(
                    f"   [Target Center] Pan attempt {attempt_number}: "
                    f"offset=({offset_x:.1f}, {offset_y:.1f})px, "
                    f"drag=({drag_x}, {drag_y})px"
                )

                attempt_record = {
                    "attempt": int(attempt_number),
                    "offset_before_px": {
                        "x": round(float(offset_x), 3),
                        "y": round(float(offset_y), 3),
                    },
                    "drag_px": {"x": int(drag_x), "y": int(drag_y)},
                }

                try:
                    if hasattr(driver, "page"):
                        start_x, start_y = 800, 400
                        driver.page.mouse.move(start_x, start_y)
                        driver.page.mouse.down()
                        time.sleep(0.1)
                        driver.page.mouse.move(start_x + int(drag_x), start_y + int(drag_y), steps=7)
                        time.sleep(0.1)
                        driver.page.mouse.up()
                except Exception as exc:
                    attempt_record.update({
                        "status": "drag_failed",
                        "error": str(exc),
                    })
                    LAST_RECENTER_DIAGNOSTICS["attempts"].append(attempt_record)
                    break

                time.sleep(TARGET_CENTER_PAN_SETTLE_SECONDS)

                changed_view, stability = _read_stable_maps_camera(
                    driver=driver,
                    reference_view=before_view,
                    timeout_seconds=TARGET_CENTER_DELAYED_GRACE_SECONDS,
                    require_change=True,
                )
                attempt_record["stability"] = stability

                if changed_view is None or not stability.get("stable", False):
                    attempt_record["status"] = "unconfirmed_or_delayed_camera"
                    LAST_RECENTER_DIAGNOSTICS["attempts"].append(attempt_record)
                    log(
                        "   [Target Center] Pan was not stably confirmed; "
                        "switching to deterministic camera lock"
                    )
                    locked_view, lock_diagnostics = _url_lock_camera_on_target(
                        driver=driver,
                        view=before_view,
                        log=log,
                    )
                    LAST_RECENTER_DIAGNOSTICS["url_lock"] = lock_diagnostics
                    current_view = _merge_camera_with_place(
                        locked_view,
                        original_view,
                    )
                    break

                current_view = _merge_camera_with_place(changed_view, original_view)
                updated_offset = _target_offset_from_camera(current_view)
                updated_distance = (
                    float(math.hypot(*updated_offset))
                    if updated_offset is not None
                    else float("inf")
                )
                improvement = previous_distance - updated_distance

                attempt_record.update({
                    "status": "stable_camera_changed",
                    "camera_after": {
                        "lat": float(current_view["lat"]),
                        "lng": float(current_view["lng"]),
                        "zoom": float(current_view["zoom"]),
                    },
                    "offset_after_px": (
                        {
                            "x": round(float(updated_offset[0]), 3),
                            "y": round(float(updated_offset[1]), 3),
                            "distance": round(updated_distance, 3),
                        }
                        if updated_offset is not None
                        else None
                    ),
                    "improvement_px": round(float(improvement), 3),
                })
                LAST_RECENTER_DIAGNOSTICS["attempts"].append(attempt_record)

                log(
                    "   [Target Center] Stable camera after pan: "
                    f"target offset=({updated_offset[0]:.1f}, "
                    f"{updated_offset[1]:.1f})px"
                )

                if abs(
                    float(current_view.get("zoom", 0.0))
                    - float(before_view.get("zoom", 0.0))
                ) > TARGET_CENTER_ZOOM_TOLERANCE:
                    log(
                        "   [Target Center] Pan changed zoom unexpectedly; "
                        "using deterministic camera lock"
                    )
                    locked_view, lock_diagnostics = _url_lock_camera_on_target(
                        driver=driver,
                        view={
                            **current_view,
                            "zoom": float(before_view.get("zoom", 15.0)),
                        },
                        log=log,
                    )
                    LAST_RECENTER_DIAGNOSTICS["url_lock"] = lock_diagnostics
                    current_view = _merge_camera_with_place(
                        locked_view,
                        original_view,
                    )
                    break

                if improvement < TARGET_CENTER_MIN_IMPROVEMENT_PX:
                    locked_view, lock_diagnostics = _url_lock_camera_on_target(
                        driver=driver,
                        view=current_view,
                        log=log,
                    )
                    LAST_RECENTER_DIAGNOSTICS["url_lock"] = lock_diagnostics
                    current_view = _merge_camera_with_place(
                        locked_view,
                        original_view,
                    )
                    break

                previous_distance = updated_distance

        if not _capture_camera_is_safe(current_view):
            locked_view, lock_diagnostics = _url_lock_camera_on_target(
                driver=driver,
                view=current_view,
                log=log,
            )
            LAST_RECENTER_DIAGNOSTICS["url_lock"] = lock_diagnostics
            current_view = _merge_camera_with_place(locked_view, original_view)

    # Final reconciliation catches any camera motion that arrives after a
    # pointer timeout. Never return the stale pre-drag camera.
    final_view, final_stability = _read_stable_maps_camera(
        driver=driver,
        reference_view=current_view,
        timeout_seconds=5.0,
        require_change=False,
        stable_samples=3,
    )
    if final_view is not None:
        current_view = _merge_camera_with_place(final_view, original_view)

    final_offset = _target_offset_from_camera(current_view)
    successful = bool(
        final_stability.get("stable", False)
        and _capture_camera_is_safe(current_view)
    )

    LAST_RECENTER_DIAGNOSTICS.update({
        "status": "camera_locked" if successful else "camera_lock_failed",
        "successful": successful,
        "attempt_count": len(LAST_RECENTER_DIAGNOSTICS.get("attempts", [])),
        "final_stability": final_stability,
        "final_camera": {
            "lat": float(current_view.get("lat", 0.0)),
            "lng": float(current_view.get("lng", 0.0)),
            "zoom": float(current_view.get("zoom", 0.0)),
        },
        "final_offset_px": (
            {
                "x": round(float(final_offset[0]), 3),
                "y": round(float(final_offset[1]), 3),
                "distance": round(float(math.hypot(*final_offset)), 3),
            }
            if final_offset is not None
            else None
        ),
    })

    if successful:
        log(
            "   [Camera Lock] Capture-safe camera verified: "
            f"offset=({final_offset[0]:.1f}, {final_offset[1]:.1f})px"
        )
    else:
        log(
            "   [Camera Lock] WARNING: camera is not capture-safe; "
            "this zoom must not be trusted as a final boundary source"
        )

    return current_view



def set_actual_map_zoom(
    driver,
    view: dict,
    target_zoom: int = 15,
    log=print,
) -> dict:
    """
    Change the actually rendered Google Maps zoom.

    Methods are attempted in this order for each one-level zoom step:
    1. scan and click Google's native zoom control;
    2. focus the map scene and send + or -;
    3. perform a real mouse-wheel action over the map centre;
    4. dispatch a wheel event to the map scene as a final fallback.

    A step counts as successful only after @lat,lng,zoom in the current URL
    changes. This prevents URL-only navigation from being mistaken for a
    rendered WebGL zoom.
    """

    target_zoom = float(target_zoom)
    tolerance = FRACTIONAL_ZOOM_ACCEPTANCE_TOLERANCE

    original_place_lat = view.get(
        "place_lat"
    )
    original_place_lng = view.get(
        "place_lng"
    )

    def read_camera() -> Optional[dict]:
        camera = _parse_maps_url(
            driver.current_url
        )

        if camera:
            if (
                camera.get("place_lat") is None
                and original_place_lat is not None
            ):
                camera["place_lat"] = float(
                    original_place_lat
                )

            if (
                camera.get("place_lng") is None
                and original_place_lng is not None
            ):
                camera["place_lng"] = float(
                    original_place_lng
                )

        return camera

    def reached(camera: Optional[dict]) -> bool:
        if not camera:
            return False

        return abs(
            float(camera["zoom"])
            - target_zoom
        ) <= tolerance

    def wait_for_zoom_change(
        previous_zoom: float,
        timeout: int = 8,
    ) -> Optional[dict]:
        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(0.3)
            cam = read_camera()
            if cam and abs(float(cam.get("zoom", previous_zoom)) - previous_zoom) > 0.05:
                return cam
        return read_camera()

    current = read_camera() or dict(view)
    initial_zoom = float(current.get("zoom", view.get("zoom", target_zoom)))

    if reached(current):
        return current

    maximum_steps = 12

    for step_number in range(
        1,
        maximum_steps + 1,
    ):
        current = read_camera() or current
        current_zoom = float(
            current["zoom"]
        )

        if reached(current):
            break

        zooming_in = (
            target_zoom > current_zoom
        )

        direction_word = (
            "in"
            if zooming_in
            else "out"
        )

        changed_camera = None

        # ---------------------------------------------------------------
        # Method 1: Playwright native button click
        # ---------------------------------------------------------------
        try:
            if hasattr(driver, "page"):
                btn_selector = "button[aria-label*='Zoom in'], button[aria-label*='zoom in'], button#widget-zoom-in" if zooming_in else "button[aria-label*='Zoom out'], button[aria-label*='zoom out'], button#widget-zoom-out"
                btn = driver.page.locator(btn_selector).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=1500)
                    changed_camera = wait_for_zoom_change(
                        current_zoom,
                        timeout=8,
                    )
        except Exception:
            changed_camera = None

        if (
            changed_camera
            and abs(
                float(changed_camera["zoom"])
                - current_zoom
            ) > 0.05
        ):
            current = changed_camera
            log(
                "   [Visual Zoom] "
                f"step {step_number}: "
                f"{current_zoom} -> {current['zoom']} "
                f"({direction_word}, native control)"
            )
            time.sleep(2)
            continue

        # ---------------------------------------------------------------
        # Method 2: Playwright native keyboard press (+ / -)
        # ---------------------------------------------------------------
        try:
            if hasattr(driver, "page"):
                driver.page.mouse.click(800, 400)
                time.sleep(0.2)
                driver.page.keyboard.press("Equal" if zooming_in else "Minus")
                changed_camera = wait_for_zoom_change(
                    current_zoom,
                    timeout=8,
                )
        except Exception:
            changed_camera = None

        if (
            changed_camera
            and abs(
                float(changed_camera["zoom"])
                - current_zoom
            ) > 0.05
        ):
            current = changed_camera
            log(
                "   [Visual Zoom] "
                f"step {step_number}: "
                f"{current_zoom} -> {current['zoom']} "
                f"({direction_word}, map keyboard)"
            )
            time.sleep(2)
            continue

        # ---------------------------------------------------------------
        # Method 3: Playwright native mouse wheel
        # ---------------------------------------------------------------
        try:
            if hasattr(driver, "page"):
                delta_y = -350 if zooming_in else 350
                driver.page.mouse.wheel(0, delta_y)
                changed_camera = wait_for_zoom_change(
                    current_zoom,
                    timeout=8,
                )
        except Exception:
            changed_camera = None

        if (
            changed_camera
            and abs(
                float(changed_camera["zoom"])
                - current_zoom
            ) > 0.05
        ):
            current = changed_camera
            log(
                "   [Visual Zoom] "
                f"step {step_number}: "
                f"{current_zoom} -> {current['zoom']} "
                f"({direction_word}, mouse wheel)"
            )
            time.sleep(2)
            continue

        # ---------------------------------------------------------------
        # Method 4: JS wheel event fallback
        # ---------------------------------------------------------------
        try:
            delta_y = (
                -700
                if zooming_in
                else 700
            )

            dispatched = bool(
                driver.execute_script(
                    """
                    const deltaY = arguments[0];
                    const canvas = document.querySelector('canvas') || document.body;
                    canvas.dispatchEvent(
                        new WheelEvent('wheel', {
                            deltaY: deltaY,
                            clientX: window.innerWidth / 2,
                            clientY: window.innerHeight / 2,
                            bubbles: true,
                            cancelable: true,
                            view: window
                        })
                    );
                    return true;
                    """,
                    delta_y,
                )
            )

            if dispatched:
                changed_camera = wait_for_zoom_change(
                    current_zoom,
                    timeout=8,
                )
        except Exception:
            changed_camera = None

        if (
            changed_camera
            and abs(
                float(changed_camera["zoom"])
                - current_zoom
            ) > 0.05
        ):
            current = changed_camera
            log(
                "   [Visual Zoom] "
                f"step {step_number}: "
                f"{current_zoom} -> {current['zoom']} "
                f"({direction_word}, JS wheel)"
            )
            time.sleep(2)
            continue

        raise RuntimeError(
            "Google Maps did not respond to its native control, "
            "map keyboard input, Selenium wheel input, or JS wheel input. "
            f"Requested={target_zoom}, rendered={current_zoom}. "
            "The map may not have keyboard/gesture focus."
        )

    final_camera = read_camera() or current

    if not reached(final_camera):
        raise RuntimeError(
            "Google Maps controls could not render the required zoom. "
            f"Requested={target_zoom}, "
            f"rendered={final_camera.get('zoom')}, "
            f"url={driver.current_url[:180]}"
        )

    time.sleep(5)

    log(
        "   [Actual Zoom] "
        f"{initial_zoom} -> {final_camera['zoom']} "
        f"(rendered map, camera: "
        f"{final_camera['lat']:.5f}, "
        f"{final_camera['lng']:.5f})"
    )

    return final_camera


def prepare_locality_outline_view(
    driver,
    view: dict,
    log=print,
) -> dict:
    """
    Close Google's place-information panel and verify that it actually closed.

    Earlier versions logged "collapse requested" even when the panel remained
    visible. This version measures genuine screen occlusion after every click
    and retries using both DOM selectors and the visible edge-toggle control.
    """

    def panel_state() -> dict:
        try:
            result = driver.execute_script(
                """
                const vw = window.innerWidth;
                const vh = window.innerHeight;

                const visible = (element, rect) => {
                    const style = window.getComputedStyle(element);

                    return (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        rect.right > 0 &&
                        rect.bottom > 0 &&
                        rect.left < vw &&
                        rect.top < vh &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0 &&
                        style.pointerEvents !== 'none'
                    );
                };

                const collapseBtn = document.querySelector('button[aria-label*="Collapse side panel"], button[aria-label*="Hide side panel"], button[jsaction*="drawer.close"]');
                if (collapseBtn && collapseBtn.getBoundingClientRect().width > 0) {
                    const rect = collapseBtn.getBoundingClientRect();
                    return {
                        open: true,
                        width: rect.left > 0 ? rect.left : 480,
                        right: rect.left > 0 ? rect.left : 480
                    };
                }

                const expandBtn = document.querySelector('button[aria-label*="Expand side panel"], button[aria-label*="Show side panel"], button[jsaction*="drawer.open"]');
                if (expandBtn && expandBtn.getBoundingClientRect().width > 0) {
                    return {
                        open: false,
                        width: 0,
                        right: 0
                    };
                }

                const ownsPoint = (element, x, y) => {
                    const top = document.elementFromPoint(x, y);

                    return Boolean(
                        top &&
                        (
                            top === element ||
                            element.contains(top)
                        )
                    );
                };

                const candidates = [];
                const seen = new Set();

                for (
                    const selector of [
                        '#pane',
                        '.widget-pane',
                        '[role="main"]'
                    ]
                ) {
                    for (
                        const element of
                        document.querySelectorAll(selector)
                    ) {
                        if (seen.has(element)) {
                            continue;
                        }

                        seen.add(element);

                        const rect = element.getBoundingClientRect();

                        if (
                            !visible(element, rect) ||
                            rect.left < -5 ||
                            rect.left > 15 ||
                            rect.width < 250 ||
                            rect.width > vw * 0.68 ||
                            rect.height < vh * 0.60
                        ) {
                            continue;
                        }

                        const probeX = Math.max(
                            5,
                            Math.min(
                                rect.right - 10,
                                rect.left + rect.width * 0.72
                            )
                        );

                        const probeYs = [
                            vh * 0.35,
                            vh * 0.55,
                            vh * 0.75
                        ];

                        let owned = 0;

                        for (const probeY of probeYs) {
                            if (
                                ownsPoint(
                                    element,
                                    probeX,
                                    probeY
                                )
                            ) {
                                owned += 1;
                            }
                        }

                        if (owned >= 2) {
                            candidates.push({
                                right: rect.right,
                                width: rect.width
                            });
                        }
                    }
                }

                if (!candidates.length) {
                    return {
                        open: false,
                        width: 0,
                        right: 0
                    };
                }

                candidates.sort(
                    (a, b) => b.right - a.right
                );

                return {
                    open: true,
                    width: candidates[0].width,
                    right: candidates[0].right
                };
                """
            )

            return result or {
                "open": False,
                "width": 0,
                "right": 0,
            }

        except Exception:
            return {
                "open": False,
                "width": 0,
                "right": 0,
            }

    state = panel_state()

    log(
        "   [Maps UI] Panel before close: "
        f"{'open' if state.get('open') else 'closed'}, "
        f"width={float(state.get('width', 0)):.1f}"
    )

    for attempt in range(1, 5):
        state = panel_state()

        if not state.get("open"):
            break

        clicked = False

        selectors = (
            'button[aria-label*="Collapse side panel"]',
            '[role="button"][aria-label*="Collapse side panel"]',
            'button[aria-label*="Hide side panel"]',
            '[role="button"][aria-label*="Hide side panel"]',
            'button[title*="Collapse side panel"]',
            'button[aria-label*="Collapse"]',
            'button[aria-label*="collapse"]',
        )

        for selector in selectors:
            try:
                if hasattr(driver, "page"):
                    loc = driver.page.locator(selector).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=1500)
                        clicked = True
                        break
            except Exception:
                continue

        # Fallback: direct JavaScript button click
        if not clicked:
            try:
                clicked = bool(driver.execute_script("""
                    const btn = document.querySelector('button[aria-label*="Collapse"], button[aria-label*="collapse"], button[aria-label*="Hide side panel"]');
                    if (btn) {
                        btn.click();
                        return true;
                    }
                    return false;
                """))
            except Exception:
                pass

        # Fallback: click the visible edge-toggle near the panel's right edge.
        if not clicked:
            try:
                panel_right = float(
                    state.get(
                        "right",
                        480,
                    )
                )

                clicked = bool(driver.execute_script(
                    """
                    const panelRight = arguments[0];
                    const vh = window.innerHeight;

                    const points = [
                        [panelRight + 12, vh * 0.50],
                        [panelRight - 12, vh * 0.50],
                        [panelRight + 18, vh * 0.62],
                        [panelRight - 18, vh * 0.62],
                        [panelRight + 12, vh * 0.38]
                    ];

                    for (const [x, y] of points) {
                        let element = document.elementFromPoint(
                            Math.max(
                                0,
                                Math.min(
                                    window.innerWidth - 1,
                                    x
                                )
                            ),
                            Math.max(
                                0,
                                Math.min(
                                    window.innerHeight - 1,
                                    y
                                )
                            )
                        );

                        if (!element) {
                            continue;
                        }

                        const button = element.closest(
                            'button, [role="button"], [jsaction]'
                        );

                        if (button) {
                            button.click();
                            return true;
                        }
                    }

                    return false;
                    """,
                    panel_right,
                ))
            except Exception:
                clicked = False

        log(
            "   [Maps UI] Panel close attempt "
            f"{attempt}: "
            f"{'clicked' if clicked else 'control not found'}"
        )

        time.sleep(4)

    final_state = panel_state()

    panel_is_open = bool(
        final_state.get("open")
    )

    try:
        driver.execute_script(
            """
            window.__jarvis_panel_verified_closed =
                arguments[0];
            """,
            not panel_is_open,
        )
    except Exception:
        pass

    if panel_is_open:
        log(
            "   [Maps UI] WARNING: side panel is still open "
            f"(width={float(final_state.get('width', 0)):.1f}). "
            "Boundary capture may require zooming out."
        )
    else:
        log(
            "   [Maps UI] Side panel verified closed"
        )
        time.sleep(4)

    # Hide only small top overlay controls after panel handling.
    try:
        hidden_count = int(
            driver.execute_script(
                """
                let hidden = 0;

                for (
                    const element of
                    document.querySelectorAll('body *')
                ) {
                    if (
                        element.tagName === 'CANVAS' ||
                        element.id === 'scene' ||
                        element.classList.contains('widget-scene') ||
                        element.classList.contains(
                            'widget-scene-canvas'
                        )
                    ) {
                        continue;
                    }

                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);

                    const positioned = (
                        style.position === 'fixed' ||
                        style.position === 'absolute'
                    );

                    const topOverlay = (
                        positioned &&
                        rect.top >= -5 &&
                        rect.top < 115 &&
                        rect.height > 0 &&
                        rect.height < 125 &&
                        rect.width > 80
                    );

                    if (
                        topOverlay &&
                        style.visibility !== 'hidden'
                    ) {
                        element.dataset.jarvisOldVisibility =
                            element.style.visibility || '';
                        element.style.visibility = 'hidden';
                        hidden += 1;
                    }
                }

                return hidden;
                """
            )
        )

        log(
            "   [Maps UI] Hidden top overlays: "
            f"{hidden_count}"
        )

    except Exception:
        pass

    time.sleep(3)

    updated = _parse_maps_url(
        driver.current_url
    )

    if updated:
        # Keep the searched place marker even when Maps changes the camera.
        if (
            updated.get("place_lat") is None
            and view.get("place_lat") is not None
        ):
            updated["place_lat"] = view["place_lat"]

        if (
            updated.get("place_lng") is None
            and view.get("place_lng") is not None
        ):
            updated["place_lng"] = view["place_lng"]

        updated["name"] = view.get(
            "name",
            "boundary",
        )

        log(
            "   [Maps UI] Camera after preparation: "
            f"{updated['lat']:.5f}, "
            f"{updated['lng']:.5f}, "
            f"zoom {updated['zoom']}"
        )

        return updated

    return view


# ─────────────────────────────────────────────────────────────────────────────
#  SCREENSHOT + GEOTIFF
# ─────────────────────────────────────────────────────────────────────────────

def get_map_viewport_metrics(
    driver,
    log=print,
) -> dict:
    """
    Measure the genuinely visible Google Maps area.

    The old detector treated Google's collapsed/off-screen side-panel DOM as
    a visible 480 px panel. It then cropped 600 screenshot pixels from a map
    that was already full-width, cutting off the western locality boundary.

    A panel is counted only when it is:
    - inside the browser viewport;
    - large enough to be the place-information panel;
    - actually the topmost element at several probe points over the map.
    """
    metrics = driver.execute_script(
        """
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;

        const isVisible = (element, rect) => {
            const style = window.getComputedStyle(element);

            return (
                rect.width > 0 &&
                rect.height > 0 &&
                rect.right > 0 &&
                rect.bottom > 0 &&
                rect.left < viewportWidth &&
                rect.top < viewportHeight &&
                style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                Number(style.opacity || 1) > 0 &&
                style.pointerEvents !== 'none'
            );
        };

        const sceneCandidates = [];

        for (
            const selector of [
                '#scene',
                '.widget-scene',
                '.widget-scene-canvas'
            ]
        ) {
            for (
                const element of
                document.querySelectorAll(selector)
            ) {
                const rect = element.getBoundingClientRect();

                if (
                    isVisible(element, rect) &&
                    rect.width >= 500 &&
                    rect.height >= 300
                ) {
                    sceneCandidates.push({
                        element,
                        rect,
                        area: rect.width * rect.height
                    });
                }
            }
        }

        if (!sceneCandidates.length) {
            for (
                const element of
                document.querySelectorAll('canvas')
            ) {
                const rect = element.getBoundingClientRect();

                if (
                    isVisible(element, rect) &&
                    rect.width >= 500 &&
                    rect.height >= 300
                ) {
                    sceneCandidates.push({
                        element,
                        rect,
                        area: rect.width * rect.height
                    });
                }
            }
        }

        if (!sceneCandidates.length) {
            return null;
        }

        sceneCandidates.sort(
            (a, b) => b.area - a.area
        );

        const scene = sceneCandidates[0].rect;

        const elementOwnsPoint = (
            candidate,
            x,
            y
        ) => {
            const topElement = document.elementFromPoint(
                Math.max(
                    0,
                    Math.min(
                        viewportWidth - 1,
                        x
                    )
                ),
                Math.max(
                    0,
                    Math.min(
                        viewportHeight - 1,
                        y
                    )
                )
            );

            return Boolean(
                topElement &&
                (
                    topElement === candidate ||
                    candidate.contains(topElement)
                )
            );
        };

        const isRealOccludingPanel = (
            element,
            rect
        ) => {
            if (
                !isVisible(element, rect) ||
                rect.left < -5 ||
                rect.left > 15 ||
                rect.right < 250 ||
                rect.right > viewportWidth * 0.65 ||
                rect.height < viewportHeight * 0.60
            ) {
                return false;
            }

            /*
            Probe the middle and lower portions. A collapsed panel may still
            have a 480 px DOM rectangle, but it will not own these screen
            points—the WebGL map will be topmost there.
            */
            const probeX = Math.max(
                5,
                Math.min(
                    rect.right - 8,
                    rect.left + rect.width * 0.70
                )
            );

            const probeYs = [
                viewportHeight * 0.35,
                viewportHeight * 0.55,
                viewportHeight * 0.75
            ];

            let ownedPoints = 0;

            for (const probeY of probeYs) {
                if (
                    elementOwnsPoint(
                        element,
                        probeX,
                        probeY
                    )
                ) {
                    ownedPoints += 1;
                }
            }

            return ownedPoints >= 2;
        };

        const panelCandidates = [];
        const checked = new Set();

        for (
            const selector of [
                '#pane',
                '.widget-pane',
                '[role="main"]'
            ]
        ) {
            for (
                const element of
                document.querySelectorAll(selector)
            ) {
                if (checked.has(element)) {
                    continue;
                }

                checked.add(element);

                const rect = element.getBoundingClientRect();

                if (
                    isRealOccludingPanel(
                        element,
                        rect
                    )
                ) {
                    panelCandidates.push({
                        element,
                        rect
                    });
                }
            }
        }

        /*
        Class names sometimes change. Inspect only large left-attached
        positioned elements and verify that they genuinely own screen pixels.
        */
        if (!panelCandidates.length) {
            for (
                const element of
                document.querySelectorAll('body *')
            ) {
                if (checked.has(element)) {
                    continue;
                }

                const rect = element.getBoundingClientRect();

                if (
                    rect.width < 250 ||
                    rect.width > viewportWidth * 0.65 ||
                    rect.height < viewportHeight * 0.60 ||
                    rect.left < -5 ||
                    rect.left > 15
                ) {
                    continue;
                }

                const style = window.getComputedStyle(element);

                if (
                    style.position !== 'fixed' &&
                    style.position !== 'absolute'
                ) {
                    continue;
                }

                if (
                    isRealOccludingPanel(
                        element,
                        rect
                    )
                ) {
                    panelCandidates.push({
                        element,
                        rect
                    });
                }
            }
        }

        let panelRight = scene.left;
        let panelVerified = false;

        if (panelCandidates.length) {
            panelCandidates.sort(
                (a, b) => b.rect.right - a.rect.right
            );

            panelRight = Math.max(
                scene.left,
                panelCandidates[0].rect.right
            );
            panelVerified = true;
        }

        panelRight = Math.min(
            panelRight,
            scene.right - 500
        );

        const visibleLeft = Math.max(
            scene.left,
            panelRight
        );

        return {
            map_left: visibleLeft,
            map_top: scene.top,
            map_width: Math.max(
                1,
                scene.right - visibleLeft
            ),
            map_height: scene.height,
            viewport_width: viewportWidth,
            viewport_height: viewportHeight,
            device_pixel_ratio: window.devicePixelRatio || 1,
            panel_width: Math.max(
                0,
                visibleLeft - scene.left
            ),
            panel_verified: panelVerified
        };
        """
    )

    if not metrics:
        try:
            viewport = driver.execute_script(
                "return {w: window.innerWidth || 1540, h: window.innerHeight || 732, dpr: window.devicePixelRatio || 1};"
            )
            if viewport and viewport.get("w") and viewport.get("h"):
                w = float(viewport["w"])
                h = float(viewport["h"])
                dpr = float(viewport.get("dpr", 1.0))
                metrics = {
                    "map_left": 0.0,
                    "map_top": 0.0,
                    "map_width": w,
                    "map_height": h,
                    "viewport_width": w,
                    "viewport_height": h,
                    "device_pixel_ratio": dpr,
                    "panel_width": 0.0,
                    "panel_verified": True,
                }
        except Exception:
            metrics = None

    if not metrics:
        raise RuntimeError(
            "Could not detect the visible Google Maps viewport."
        )

    try:
        panel_was_verified_closed = bool(
            driver.execute_script(
                """
                return (
                    window.__jarvis_panel_verified_closed
                    === true
                );
                """
            )
        )
    except Exception:
        panel_was_verified_closed = False

    if panel_was_verified_closed:
        metrics["map_left"] = 0.0
        metrics["map_top"] = 0.0
        metrics["map_width"] = float(
            metrics["viewport_width"]
        )
        metrics["map_height"] = float(
            metrics["viewport_height"]
        )
        metrics["panel_width"] = 0.0
        metrics["panel_verified"] = False
        metrics["panel_override_reason"] = (
            "verified closed by UI preparation"
        )

    global LAST_MAP_VIEWPORT_HEIGHT_CSS

    LAST_MAP_VIEWPORT_HEIGHT_CSS = max(
        1.0,
        float(
            metrics.get(
                "map_height",
                LAST_MAP_VIEWPORT_HEIGHT_CSS,
            )
        ),
    )

    panel_status = (
        "verified"
        if metrics.get("panel_verified")
        else "none/collapsed"
    )

    log(
        "   [Map viewport] "
        f"left={metrics['map_left']:.1f}, "
        f"top={metrics['map_top']:.1f}, "
        f"width={metrics['map_width']:.1f}, "
        f"height={metrics['map_height']:.1f}, "
        f"panel={metrics.get('panel_width', 0.0):.1f} "
        f"({panel_status}), "
        f"window={metrics['viewport_width']:.1f}×"
        f"{metrics['viewport_height']:.1f}, "
        f"dpr={metrics['device_pixel_ratio']:.2f}"
    )

    return metrics


def capture_screenshot(
    driver,
    output_path: str,
    map_metrics: Optional[dict] = None,
    log=print,
) -> tuple[str, int, int]:
    """
    Capture Google Maps, reject blank renders and crop away the visible
    information panel before boundary extraction.
    """
    output_path = os.path.abspath(
        output_path
    )

    os.makedirs(
        os.path.dirname(output_path),
        exist_ok=True,
    )

    close_maps_ui(driver, log=log)
    time.sleep(1.0)

    def save_and_measure() -> tuple[np.ndarray, float]:
        close_maps_ui(driver, log=log)
        driver.save_screenshot(
            output_path
        )

        image = cv2.imread(
            output_path
        )

        if image is None:
            raise RuntimeError(
                "Screenshot was saved but OpenCV could not read it: "
                f"{output_path}"
            )

        gray = cv2.cvtColor(
            image,
            cv2.COLOR_BGR2GRAY,
        )

        return (
            image,
            float(gray.std()),
        )

    time.sleep(4)

    image, variation = save_and_measure()

    log(
        f"   [Screenshot] Pixel variation: "
        f"{variation:.2f}"
    )

    if variation < 8.0:
        log(
            "   [Screenshot] Blank render detected; "
            "refreshing Google Maps once..."
        )

        try:
            driver.refresh()
            time.sleep(10)
        except Exception:
            pass

        image, variation = save_and_measure()

        log(
            f"   [Screenshot] Retry variation: "
            f"{variation:.2f}"
        )

    if variation < 8.0:
        raise RuntimeError(
            "Google Maps rendered a solid/blank screenshot."
        )

    full_h, full_w = image.shape[:2]

    should_crop_for_panel = bool(
        map_metrics
        and map_metrics.get(
            "panel_verified",
            False,
        )
        and float(
            map_metrics.get(
                "panel_width",
                0.0,
            )
        ) > 1.0
    )

    if should_crop_for_panel:
        viewport_width = max(
            1.0,
            float(
                map_metrics.get(
                    "viewport_width",
                    full_w,
                )
            ),
        )
        viewport_height = max(
            1.0,
            float(
                map_metrics.get(
                    "viewport_height",
                    full_h,
                )
            ),
        )

        scale_x = full_w / viewport_width
        scale_y = full_h / viewport_height

        left = int(
            round(
                float(
                    map_metrics.get(
                        "map_left",
                        0.0,
                    )
                )
                * scale_x
            )
        )
        top = int(
            round(
                float(
                    map_metrics.get(
                        "map_top",
                        0.0,
                    )
                )
                * scale_y
            )
        )
        right = int(
            round(
                (
                    float(
                        map_metrics.get(
                            "map_left",
                            0.0,
                        )
                    )
                    + float(
                        map_metrics.get(
                            "map_width",
                            viewport_width,
                        )
                    )
                )
                * scale_x
            )
        )
        bottom = int(
            round(
                (
                    float(
                        map_metrics.get(
                            "map_top",
                            0.0,
                        )
                    )
                    + float(
                        map_metrics.get(
                            "map_height",
                            viewport_height,
                        )
                    )
                )
                * scale_y
            )
        )

        left = max(
            0,
            min(
                left,
                full_w - 1,
            ),
        )
        top = max(
            0,
            min(
                top,
                full_h - 1,
            ),
        )
        right = max(
            left + 1,
            min(
                right,
                full_w,
            ),
        )
        bottom = max(
            top + 1,
            min(
                bottom,
                full_h,
            ),
        )

        crop = image[
            top:bottom,
            left:right,
        ]

        if (
            crop.size > 0
            and crop.shape[1] >= 500
            and crop.shape[0] >= 300
        ):
            image = crop

            cv2.imwrite(
                output_path,
                image,
            )

            css_width = float(
                map_metrics["map_width"]
            )
            css_height = float(
                map_metrics["map_height"]
            )

            # Google Maps' URL camera is centred on the original browser
            # viewport, not on the remaining visible map after the left panel
            # is cropped. Preserve that camera location in cropped-image
            # pixels before replacing viewport metrics.
            full_camera_x_px = (
                viewport_width / 2.0
            ) * scale_x
            full_camera_y_px = (
                viewport_height / 2.0
            ) * scale_y

            camera_x_in_crop = (
                full_camera_x_px - left
            )
            camera_y_in_crop = (
                full_camera_y_px - top
            )

            camera_x_in_crop = max(
                0.0,
                min(
                    camera_x_in_crop,
                    float(
                        image.shape[1]
                    ),
                ),
            )
            camera_y_in_crop = max(
                0.0,
                min(
                    camera_y_in_crop,
                    float(
                        image.shape[0]
                    ),
                ),
            )

            # The saved screenshot now contains only the visible map, but the
            # camera pixel remains offset because the covered panel region was
            # removed from the left.
            map_metrics.update({
                "map_left": 0.0,
                "map_top": 0.0,
                "map_width": css_width,
                "map_height": css_height,
                "viewport_width": css_width,
                "viewport_height": css_height,
                "camera_center_x_px": camera_x_in_crop,
                "camera_center_y_px": camera_y_in_crop,
                "crop_left_px": left,
                "crop_top_px": top,
            })

            log(
                "   [Screenshot] Cropped visible map: "
                f"x={left}:{right}, "
                f"y={top}:{bottom}"
            )
            log(
                "   [Screenshot] Camera centre in crop: "
                f"x={camera_x_in_crop:.1f}, "
                f"y={camera_y_in_crop:.1f}"
            )

    if map_metrics and not should_crop_for_panel:
        map_metrics.update({
            "map_left": 0.0,
            "map_top": 0.0,
            "map_width": float(
                map_metrics.get(
                    "viewport_width",
                    full_w,
                )
            ),
            "map_height": float(
                map_metrics.get(
                    "viewport_height",
                    full_h,
                )
            ),
            "panel_width": 0.0,
            "panel_verified": False,
            "camera_center_x_px": full_w / 2.0,
            "camera_center_y_px": full_h / 2.0,
        })

        log(
            "   [Screenshot] Full-width map kept; "
            "no verified side panel crop"
        )
        log(
            "   [Screenshot] Camera centre in full image: "
            f"x={full_w / 2.0:.1f}, "
            f"y={full_h / 2.0:.1f}"
        )

    height, width = image.shape[:2]

    log(
        f"   [Screenshot] {width}×{height} → "
        f"{os.path.basename(output_path)}"
    )

    return (
        output_path,
        width,
        height,
    )


def extract_georeference(
    view: dict,
    img_w: int,
    img_h: int,
    map_metrics: dict,
    log=print,
) -> GeoReference:
    """
    Build georeferencing from Google Maps CSS dimensions.

    Selenium screenshots on this machine are DPR-scaled (for example,
    1540 CSS px becomes 1925 image px). Google Maps world pixels are CSS
    pixels, so image offsets must be divided by that scale.
    """
    css_per_image_x = (
        float(map_metrics["map_width"])
        / max(img_w, 1)
    )
    css_per_image_y = (
        float(map_metrics["map_height"])
        / max(img_h, 1)
    )

    camera_px_x = float(
        map_metrics.get(
            "camera_center_x_px",
            img_w / 2.0,
        )
    )
    camera_px_y = float(
        map_metrics.get(
            "camera_center_y_px",
            img_h / 2.0,
        )
    )

    target_lat = float(
        view.get(
            "place_lat",
            view["lat"],
        )
    )
    target_lng = float(
        view.get(
            "place_lng",
            view["lng"],
        )
    )

    reference = GeoReference(
        center_lat=view["lat"],
        center_lng=view["lng"],
        zoom=view["zoom"],
        img_w=img_w,
        img_h=img_h,
        camera_px_x=camera_px_x,
        camera_px_y=camera_px_y,
        target_lat=target_lat,
        target_lng=target_lng,
        css_per_image_x=css_per_image_x,
        css_per_image_y=css_per_image_y,
    )

    bbox = reference.bbox

    log(
        f"   [Georef] mode={GEOREFERENCE_MODE} css/image="
        f"({css_per_image_x:.4f}, "
        f"{css_per_image_y:.4f}) "
        f"camera_px=({camera_px_x:.1f}, "
        f"{camera_px_y:.1f}) "
        f"target=({target_lat:.5f}, "
        f"{target_lng:.5f}) "
        f"target_px=({reference.target_pixel[0]:.1f}, "
        f"{reference.target_pixel[1]:.1f}) "
        f"mpp={reference.meters_per_pixel:.2f} "
        f"bbox N={bbox['north']:.4f} "
        f"S={bbox['south']:.4f} "
        f"W={bbox['west']:.4f} "
        f"E={bbox['east']:.4f}"
    )

    return reference


def save_geotiff(
    screenshot_path: str,
    georef: GeoReference,
    output_path: str,
    log=print,
) -> str:
    img_bgr = cv2.imread(screenshot_path)
    if img_bgr is None:
        raise RuntimeError(f"Cannot read screenshot: {screenshot_path}")

    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

    tl_lat, tl_lng = georef.pixel_to_latlng(0, 0)
    scale_x =  georef.deg_per_px_lng
    scale_y =  georef.deg_per_px_lat

    _write_geotiff_raw(img_rgb, tl_lng, tl_lat, scale_x, scale_y, output_path)

    log(f"   [GeoTIFF] → {os.path.basename(output_path)}")
    log(f"            Top-left: {tl_lat:.6f}°N, {tl_lng:.6f}°E")
    log(f"            Scale: {scale_x:.8f}°/px (lng), {scale_y:.8f}°/px (lat)")
    return output_path


def _write_geotiff_raw(
    img_rgb:  np.ndarray,
    origin_x: float,
    origin_y: float,
    scale_x:  float,
    scale_y:  float,
    out_path: str,
) -> None:
    h, w, bands = img_rgb.shape

    rows_per_strip  = 1
    bytes_per_row   = w * bands

    pixel_scale = struct.pack("<3d", scale_x, -scale_y, 0.0)
    tiepoint    = struct.pack("<6d", 0.0, 0.0, 0.0, origin_x, origin_y, 0.0)
    geo_keys    = struct.pack(
        "<16H",
        1, 1, 0, 3,
        1024, 0, 1, 2,
        1025, 0, 1, 1,
        2048, 0, 1, 4326,
    )

    n_tags   = 14
    ifd_size = 2 + n_tags * 12 + 4

    ifd_offset = 8
    data_start = ifd_offset + ifd_size

    bits_per_sample_data = struct.pack("<3H", 8, 8, 8)

    off_bits        = data_start
    off_scale       = off_bits       + len(bits_per_sample_data)
    off_tiepoint    = off_scale      + len(pixel_scale)
    off_geokeys     = off_tiepoint   + len(tiepoint)
    off_strips      = off_geokeys    + len(geo_keys)
    off_bytecounts  = off_strips     + h * 4
    off_image_data  = off_bytecounts + h * 4

    strip_offsets_data    = struct.pack(
        f"<{h}I",
        *[off_image_data + r * bytes_per_row for r in range(h)],
    )
    strip_bytecounts_data = struct.pack(f"<{h}I", *[bytes_per_row] * h)

    with open(out_path, "wb") as f:
        f.write(b"II")
        f.write(struct.pack("<H", 42))
        f.write(struct.pack("<I", ifd_offset))
        f.write(struct.pack("<H", n_tags))

        def tag_short(code, value):
            return struct.pack("<HHIHH", code, 3, 1, value, 0)

        def tag_long(code, value):
            return struct.pack("<HHII", code, 4, 1, value)

        def tag_offset(code, type_, count, offset):
            return struct.pack("<HHII", code, type_, count, offset)

        f.write(tag_long(256,   w))
        f.write(tag_long(257,   h))
        f.write(tag_offset(258, 3, 3,  off_bits))
        f.write(tag_short(259,  1))
        f.write(tag_short(262,  2))
        f.write(tag_offset(273, 4, h,  off_strips))
        f.write(tag_short(277,  3))
        f.write(tag_long(278,   rows_per_strip))
        f.write(tag_offset(279, 4, h,  off_bytecounts))
        f.write(tag_short(284,  1))
        f.write(tag_offset(33550, 12, 3,  off_scale))
        f.write(tag_offset(33922, 12, 6,  off_tiepoint))
        f.write(
    tag_offset(
        34735,
        3,
        16,
        off_geokeys
        )
    )  # type=SHORT, count=16
        f.write(tag_long(34737,  0))

        f.write(struct.pack("<I", 0))   # next IFD offset = 0

        f.write(bits_per_sample_data)
        f.write(pixel_scale)
        f.write(tiepoint)
        f.write(geo_keys)
        f.write(strip_offsets_data)
        f.write(strip_bytecounts_data)

        for row in range(h):
            f.write(img_rgb[row].tobytes())


# ─────────────────────────────────────────────────────────────────────────────
#  OPENCV BOUNDARY DETECTION
# ─────────────────────────────────────────────────────────────────────────────


def _resample_closed_contour(
    contour: np.ndarray,
    target_count: int,
) -> Optional[np.ndarray]:
    """
    Resample a closed OpenCV contour at approximately equal arc-length
    intervals. The returned array is float32 with shape (N, 2).
    """
    if contour is None:
        return None

    points = np.asarray(
        contour,
        dtype=np.float32,
    ).reshape(
        -1,
        2,
    )

    if len(
        points
    ) < 3:
        return None

    # Remove a repeated closing point before rebuilding the closed segments.
    if np.linalg.norm(
        points[0]
        - points[-1]
    ) < 0.01:
        points = points[:-1]

    if len(
        points
    ) < 3:
        return None

    following_points = np.roll(
        points,
        -1,
        axis=0,
    )

    segment_lengths = np.hypot(
        following_points[:, 0]
        - points[:, 0],
        following_points[:, 1]
        - points[:, 1],
    )

    total_length = float(
        np.sum(
            segment_lengths
        )
    )

    if total_length <= 1.0:
        return None

    cumulative_lengths = np.concatenate(
        (
            np.array(
                [0.0],
                dtype=np.float32,
            ),
            np.cumsum(
                segment_lengths,
                dtype=np.float32,
            ),
        )
    )

    closed_points = np.vstack(
        (
            points,
            points[0],
        )
    )

    sample_distances = np.linspace(
        0.0,
        total_length,
        num=max(
            3,
            int(
                target_count
            ),
        ),
        endpoint=False,
        dtype=np.float32,
    )

    sampled_x = np.interp(
        sample_distances,
        cumulative_lengths,
        closed_points[:, 0],
    )

    sampled_y = np.interp(
        sample_distances,
        cumulative_lengths,
        closed_points[:, 1],
    )

    return np.column_stack(
        (
            sampled_x,
            sampled_y,
        )
    ).astype(
        np.float32
    )



def _segment_orientation(
    first: np.ndarray,
    second: np.ndarray,
    third: np.ndarray,
) -> float:
    return float(
        (
            second[0]
            - first[0]
        )
        * (
            third[1]
            - first[1]
        )
        - (
            second[1]
            - first[1]
        )
        * (
            third[0]
            - first[0]
        )
    )


def _segments_intersect_strictly(
    first_start: np.ndarray,
    first_end: np.ndarray,
    second_start: np.ndarray,
    second_end: np.ndarray,
) -> bool:
    """
    Detect proper segment crossings. Shared polygon endpoints and collinear
    touching are ignored because adjacent contour segments meet normally.
    """
    orientation_1 = _segment_orientation(
        first_start,
        first_end,
        second_start,
    )

    orientation_2 = _segment_orientation(
        first_start,
        first_end,
        second_end,
    )

    orientation_3 = _segment_orientation(
        second_start,
        second_end,
        first_start,
    )

    orientation_4 = _segment_orientation(
        second_start,
        second_end,
        first_end,
    )

    epsilon = 1e-5

    return (
        orientation_1
        * orientation_2
        < -epsilon
        and orientation_3
        * orientation_4
        < -epsilon
    )


def _count_closed_polyline_crossings(
    points: np.ndarray,
) -> int:
    points = np.asarray(
        points,
        dtype=np.float32,
    ).reshape(
        -1,
        2,
    )

    point_count = len(
        points
    )

    if point_count < 4:
        return 0

    crossing_count = 0

    for first_index in range(
        point_count
    ):
        first_next = (
            first_index
            + 1
        ) % point_count

        for second_index in range(
            first_index
            + 1,
            point_count,
        ):
            second_next = (
                second_index
                + 1
            ) % point_count

            # Adjacent edges and the first/last edge share endpoints.
            if (
                first_index
                == second_index
                or first_next
                == second_index
                or second_next
                == first_index
            ):
                continue

            if _segments_intersect_strictly(
                points[
                    first_index
                ],
                points[
                    first_next
                ],
                points[
                    second_index
                ],
                points[
                    second_next
                ],
            ):
                crossing_count += 1

    return crossing_count


def _repair_exact_shape_jumps(
    stable_points: np.ndarray,
    refined_points: np.ndarray,
    supported: np.ndarray,
    maximum_allowed_step: float,
    log=print,
) -> tuple[
    np.ndarray,
    np.ndarray,
    dict,
]:
    """
    Repair only unsafe snapped points.

    A red label, road or icon can occasionally attract one vertex and create a
    long diagonal or triangular spike. The dense stable contour already has
    the correct intermediate path, so unsafe vertices are returned to their
    stable positions instead of rejecting the whole exact contour.
    """
    stable_points = np.asarray(
        stable_points,
        dtype=np.float32,
    ).reshape(
        -1,
        2,
    )

    repaired_points = np.asarray(
        refined_points,
        dtype=np.float32,
    ).reshape(
        -1,
        2,
    ).copy()

    retained_support = np.asarray(
        supported,
        dtype=bool,
    ).copy()

    repaired_vertex_mask = np.zeros(
        len(
            repaired_points
        ),
        dtype=bool,
    )

    def displacement(
        index: int,
    ) -> float:
        return float(
            np.linalg.norm(
                repaired_points[
                    index
                ]
                - stable_points[
                    index
                ]
            )
        )

    # First repair long edges. Revert whichever endpoint moved farther from
    # the stable contour, then recheck because one bad point can affect both
    # neighbouring edges.
    for _ in range(
        10
    ):
        following_points = np.roll(
            repaired_points,
            -1,
            axis=0,
        )

        edge_lengths = np.hypot(
            repaired_points[:, 0]
            - following_points[:, 0],
            repaired_points[:, 1]
            - following_points[:, 1],
        )

        bad_edges = np.where(
            edge_lengths
            > maximum_allowed_step
        )[0]

        if len(
            bad_edges
        ) == 0:
            break

        changed = False

        for first_index in bad_edges:
            second_index = (
                int(
                    first_index
                )
                + 1
            ) % len(
                repaired_points
            )

            first_displacement = displacement(
                int(
                    first_index
                )
            )

            second_displacement = displacement(
                second_index
            )

            candidate_indexes = sorted(
                (
                    int(
                        first_index
                    ),
                    second_index,
                ),
                key=lambda index: displacement(
                    index
                ),
                reverse=True,
            )

            reverted = False

            for candidate_index in candidate_indexes:
                if (
                    retained_support[
                        candidate_index
                    ]
                    or displacement(
                        candidate_index
                    )
                    > 1.5
                ):
                    repaired_points[
                        candidate_index
                    ] = stable_points[
                        candidate_index
                    ]

                    retained_support[
                        candidate_index
                    ] = False

                    repaired_vertex_mask[
                        candidate_index
                    ] = True

                    reverted = True
                    changed = True
                    break

            if not reverted:
                # Both points are already close to stable. Revert both to
                # guarantee the original dense path is restored.
                for candidate_index in (
                    int(
                        first_index
                    ),
                    second_index,
                ):
                    repaired_points[
                        candidate_index
                    ] = stable_points[
                        candidate_index
                    ]

                    retained_support[
                        candidate_index
                    ] = False

                    repaired_vertex_mask[
                        candidate_index
                    ] = True

                changed = True

        if not changed:
            break

    # Remove sharp local detours that can pass the edge-length test but still
    # create a narrow triangle or hook.
    for _ in range(
        6
    ):
        changed = False

        previous_points = np.roll(
            repaired_points,
            1,
            axis=0,
        )

        next_points = np.roll(
            repaired_points,
            -1,
            axis=0,
        )

        distance_to_previous = np.hypot(
            repaired_points[:, 0]
            - previous_points[:, 0],
            repaired_points[:, 1]
            - previous_points[:, 1],
        )

        distance_to_next = np.hypot(
            repaired_points[:, 0]
            - next_points[:, 0],
            repaired_points[:, 1]
            - next_points[:, 1],
        )

        neighbour_chord = np.hypot(
            previous_points[:, 0]
            - next_points[:, 0],
            previous_points[:, 1]
            - next_points[:, 1],
        )

        detour_ratio = (
            distance_to_previous
            + distance_to_next
        ) / np.maximum(
            neighbour_chord,
            1.0,
        )

        point_displacements = np.hypot(
            repaired_points[:, 0]
            - stable_points[:, 0],
            repaired_points[:, 1]
            - stable_points[:, 1],
        )

        spike_indexes = np.where(
            (
                detour_ratio
                > 2.35
            )
            & (
                point_displacements
                > 3.0
            )
        )[0]

        if len(
            spike_indexes
        ) == 0:
            break

        for point_index in spike_indexes:
            repaired_points[
                point_index
            ] = stable_points[
                point_index
            ]

            retained_support[
                point_index
            ] = False

            repaired_vertex_mask[
                point_index
            ] = True

            changed = True

        if not changed:
            break

    following_points = np.roll(
        repaired_points,
        -1,
        axis=0,
    )

    final_edge_lengths = np.hypot(
        repaired_points[:, 0]
        - following_points[:, 0],
        repaired_points[:, 1]
        - following_points[:, 1],
    )

    final_maximum_step = float(
        np.max(
            final_edge_lengths
        )
    )

    crossing_count = _count_closed_polyline_crossings(
        repaired_points
    )

    unrepaired_vertex_mask = (
        ~repaired_vertex_mask
    )

    unrepaired_vertex_count = int(
        np.count_nonzero(
            unrepaired_vertex_mask
        )
    )

    unrepaired_supported_count = int(
        np.count_nonzero(
            retained_support
            & unrepaired_vertex_mask
        )
    )

    unrepaired_support_ratio = (
        unrepaired_supported_count
        / max(
            unrepaired_vertex_count,
            1,
        )
    )

    repaired_vertex_indexes = np.where(
        repaired_vertex_mask
    )[0].astype(
        int
    ).tolist()

    repaired_fraction = (
        len(
            repaired_vertex_indexes
        )
        / max(
            len(
                repaired_points
            ),
            1,
        )
    )

    diagnostics = {
        "repaired_vertices": int(
            np.count_nonzero(
                repaired_vertex_mask
            )
        ),
        "repaired_vertex_indexes": (
            repaired_vertex_indexes
        ),
        "repaired_fraction": round(
            float(
                repaired_fraction
            ),
            4,
        ),
        "unrepaired_support_ratio": round(
            float(
                unrepaired_support_ratio
            ),
            4,
        ),
        "retained_red_supported_points": int(
            np.count_nonzero(
                retained_support
            )
        ),
        "retained_red_support_ratio": round(
            float(
                np.mean(
                    retained_support
                )
            ),
            3,
        ),
        "max_step_after_repair_px": round(
            final_maximum_step,
            2,
        ),
        "self_crossings_after_repair": int(
            crossing_count
        ),
    }

    log(
        "   [Exact Repair] "
        f"reverted={diagnostics['repaired_vertices']}, "
        f"retained_support="
        f"{diagnostics['retained_red_support_ratio']:.3f}, "
        f"max_step={diagnostics['max_step_after_repair_px']:.2f}px, "
        f"crossings={crossing_count}"
    )

    return (
        repaired_points,
        retained_support,
        diagnostics,
    )




def _inner_shortcut_diagnostics(
    contour: np.ndarray,
    center: tuple[int, int],
) -> dict:
    """
    Detect a likely internal shortcut.

    A clean locality boundary should mainly stay around the outside of the
    searched-place target. When many contour vertices run very close to the
    target point, it usually means the reconstructed line has cut inside the
    locality through a road/label/red artefact.
    """
    diagnostics = {
        "suspected": False,
        "inner_point_fraction": 0.0,
        "minimum_radius_px": 0.0,
        "median_radius_px": 0.0,
        "inner_threshold_px": 0.0,
    }

    if contour is None:
        return diagnostics

    points = np.asarray(
        contour,
        dtype=np.float32,
    ).reshape(
        -1,
        2,
    )

    if len(points) < 20:
        return diagnostics

    center_point = np.array(
        [
            float(center[0]),
            float(center[1]),
        ],
        dtype=np.float32,
    )

    radii = np.hypot(
        points[:, 0] - center_point[0],
        points[:, 1] - center_point[1],
    )

    median_radius = float(
        np.median(radii)
    )

    if median_radius <= 1.0:
        return diagnostics

    inner_threshold = max(
        24.0,
        median_radius * INNER_SHORTCUT_RADIUS_RATIO,
    )

    inner_fraction = float(
        np.mean(
            radii < inner_threshold
        )
    )

    minimum_radius = float(
        np.min(radii)
    )

    suspected = (
        inner_fraction >= INNER_SHORTCUT_MIN_FRACTION
        and minimum_radius <= inner_threshold * 0.72
    )

    diagnostics.update({
        "suspected": bool(suspected),
        "inner_point_fraction": round(inner_fraction, 4),
        "minimum_radius_px": round(minimum_radius, 2),
        "median_radius_px": round(median_radius, 2),
        "inner_threshold_px": round(inner_threshold, 2),
    })

    return diagnostics



def _exact_shape_refine_contour(
    stable_contour: np.ndarray,
    raw_red_mask: np.ndarray,
    center: tuple[int, int],
    roi_bounds: tuple[int, int, int, int],
    edge_margin: int,
    image_area: int,
    coverage_ratio: float = 0.0,
    log=print,
) -> tuple[
    Optional[np.ndarray],
    dict,
]:
    """
    Densify the stable polygon and snap supported vertices to the nearest
    visible Google-red pixels.

    The refinement is accepted only when:
    - enough vertices have genuine nearby red support;
    - the locality target remains inside;
    - the contour remains inside the search ROI;
    - total area changes only slightly;
    - no large vertex jumps are introduced.

    Otherwise None is returned and the caller keeps the stable Priority 3
    contour unchanged.
    """
    diagnostics = {
        "enabled": bool(
            EXACT_SHAPE_MODE
        ),
        "accepted": False,
    }

    if (
        not EXACT_SHAPE_MODE
        or stable_contour is None
        or raw_red_mask is None
    ):
        return None, diagnostics

    stable_perimeter = float(
        cv2.arcLength(
            stable_contour,
            True,
        )
    )

    stable_area = abs(
        float(
            cv2.contourArea(
                stable_contour
            )
        )
    )

    if (
        stable_perimeter <= 1.0
        or stable_area <= 1.0
    ):
        diagnostics[
            "reason"
        ] = "invalid_stable_contour"

        return None, diagnostics

    target_count = int(
        round(
            stable_perimeter
            / 3.0
        )
    )

    target_count = max(
        EXACT_SHAPE_MIN_VERTEX_COUNT,
        min(
            EXACT_SHAPE_MAX_VERTEX_COUNT,
            target_count,
        ),
    )

    sampled_points = _resample_closed_contour(
        stable_contour,
        target_count,
    )

    if sampled_points is None:
        diagnostics[
            "reason"
        ] = "resampling_failed"

        return None, diagnostics

    red_y, red_x = np.where(
        raw_red_mask > 0
    )

    if len(
        red_x
    ) < 30:
        diagnostics[
            "reason"
        ] = "too_few_red_pixels"

        return None, diagnostics

    red_points = np.column_stack(
        (
            red_x,
            red_y,
        )
    ).astype(
        np.float32
    )

    refined_points = sampled_points.copy()

    supported = np.zeros(
        len(
            sampled_points
        ),
        dtype=bool,
    )

    snap_distances = np.full(
        len(
            sampled_points
        ),
        np.nan,
        dtype=np.float32,
    )

    maximum_snap_distance = float(
        EXACT_SHAPE_MAX_SNAP_DISTANCE_PX
    )

    maximum_snap_distance_squared = (
        maximum_snap_distance
        * maximum_snap_distance
    )

    # Search only inside a small square around each stable boundary point.
    # This prevents distant red roads, labels and POI icons from being used.
    for point_index, point in enumerate(
        sampled_points
    ):
        delta_x = (
            red_points[:, 0]
            - float(
                point[0]
            )
        )

        delta_y = (
            red_points[:, 1]
            - float(
                point[1]
            )
        )

        nearby_mask = (
            np.abs(
                delta_x
            )
            <= maximum_snap_distance
        ) & (
            np.abs(
                delta_y
            )
            <= maximum_snap_distance
        )

        if not np.any(
            nearby_mask
        ):
            continue

        nearby_indexes = np.where(
            nearby_mask
        )[0]

        nearby_distance_squared = (
            delta_x[
                nearby_indexes
            ]
            * delta_x[
                nearby_indexes
            ]
            + delta_y[
                nearby_indexes
            ]
            * delta_y[
                nearby_indexes
            ]
        )

        nearest_local_index = int(
            np.argmin(
                nearby_distance_squared
            )
        )

        nearest_distance_squared = float(
            nearby_distance_squared[
                nearest_local_index
            ]
        )

        if (
            nearest_distance_squared
            > maximum_snap_distance_squared
        ):
            continue

        nearest_red_point = red_points[
            nearby_indexes[
                nearest_local_index
            ]
        ]

        nearest_distance = math.sqrt(
            nearest_distance_squared
        )

        # Keep a tiny amount of the stable contour to reduce one-pixel colour
        # noise while still following the actual dotted line closely.
        refined_points[
            point_index
        ] = (
            0.97
            * nearest_red_point
            + 0.03
            * point
        )

        supported[
            point_index
        ] = True

        snap_distances[
            point_index
        ] = nearest_distance

    support_ratio = float(
        np.mean(
            supported
        )
    )

    supported_count = int(
        np.count_nonzero(
            supported
        )
    )

    diagnostics.update({
        "target_points": int(
            target_count
        ),
        "red_supported_points": (
            supported_count
        ),
        "red_support_ratio": round(
            support_ratio,
            3,
        ),
    })

    if (
        support_ratio
        < EXACT_SHAPE_MIN_RED_SUPPORT_RATIO
    ):
        diagnostics[
            "reason"
        ] = "insufficient_red_support"

        log(
            "   [Exact Shape] Stable contour kept: "
            f"red support {support_ratio:.3f} is below "
            f"{EXACT_SHAPE_MIN_RED_SUPPORT_RATIO:.3f}"
        )

        return None, diagnostics

    # Smooth only unsupported/interpolated points. Supported vertices stay on
    # the actual visible Google-red line so corners are not rounded away.
    previous_points = np.roll(
        refined_points,
        1,
        axis=0,
    )

    next_points = np.roll(
        refined_points,
        -1,
        axis=0,
    )

    neighbour_average = (
        previous_points
        + refined_points
        + next_points
    ) / 3.0

    unsupported = ~supported

    refined_points[
        unsupported
    ] = (
        0.82
        * refined_points[
            unsupported
        ]
        + 0.18
        * neighbour_average[
            unsupported
        ]
    )

    expected_step = (
        stable_perimeter
        / max(
            len(
                refined_points
            ),
            1,
        )
    )

    maximum_allowed_step = max(
        14.0,
        expected_step
        * 4.0,
    )

    (
        refined_points,
        supported,
        repair_diagnostics,
    ) = _repair_exact_shape_jumps(
        stable_points=sampled_points,
        refined_points=refined_points,
        supported=supported,
        maximum_allowed_step=maximum_allowed_step,
        log=log,
    )

    repaired_vertex_indexes = np.asarray(
        repair_diagnostics.get(
            "repaired_vertex_indexes",
            [],
        ),
        dtype=np.int32,
    )

    repaired_gap_distances = np.asarray(
        [],
        dtype=np.float32,
    )

    repaired_gap_support_ratio = 1.0
    repaired_gap_mean_distance = 0.0

    if repaired_vertex_indexes.size > 0:
        inverse_red_mask = cv2.bitwise_not(
            (
                raw_red_mask
                > 0
            ).astype(
                np.uint8
            )
            * 255
        )

        distance_to_red = cv2.distanceTransform(
            inverse_red_mask,
            cv2.DIST_L2,
            3,
        )

        repaired_xy = np.round(
            refined_points[
                repaired_vertex_indexes
            ]
        ).astype(
            np.int32
        )

        repaired_xy[
            :,
            0
        ] = np.clip(
            repaired_xy[
                :,
                0
            ],
            0,
            raw_red_mask.shape[
                1
            ]
            - 1,
        )

        repaired_xy[
            :,
            1
        ] = np.clip(
            repaired_xy[
                :,
                1
            ],
            0,
            raw_red_mask.shape[
                0
            ]
            - 1,
        )

        repaired_gap_distances = distance_to_red[
            repaired_xy[
                :,
                1
            ],
            repaired_xy[
                :,
                0
            ],
        ]

        repaired_gap_support_ratio = float(
            np.mean(
                repaired_gap_distances
                <= EXACT_REPAIR_CONFIRMATION_GAP_SUPPORT_DISTANCE_PX
            )
        )

        repaired_gap_mean_distance = float(
            np.mean(
                repaired_gap_distances
            )
        )

    repair_diagnostics[
        "gap_red_support_ratio"
    ] = round(
        float(
            repaired_gap_support_ratio
        ),
        4,
    )

    repair_diagnostics[
        "gap_mean_red_distance_px"
    ] = round(
        float(
            repaired_gap_mean_distance
        ),
        3,
    )

    diagnostics[
        "repair"
    ] = repair_diagnostics

    retained_support_ratio = float(
        np.mean(
            supported
        )
    )

    if (
        retained_support_ratio
        < EXACT_SHAPE_MIN_RED_SUPPORT_RATIO
    ):
        diagnostics[
            "reason"
        ] = "insufficient_support_after_repair"

        log(
            "   [Exact Shape] Stable contour kept: "
            "too much red support was removed while "
            "repairing jumps/spikes"
        )

        return None, diagnostics

    refined_contour = np.round(
        refined_points
    ).astype(
        np.int32
    ).reshape(
        -1,
        1,
        2,
    )

    refined_area = abs(
        float(
            cv2.contourArea(
                refined_contour
            )
        )
    )

    area_change_ratio = abs(
        refined_area
        - stable_area
    ) / max(
        stable_area,
        1.0,
    )

    roi_left, roi_top, roi_right, roi_bottom = (
        roi_bounds
    )

    (
        refined_x,
        refined_y,
        refined_width,
        refined_height,
    ) = cv2.boundingRect(
        refined_contour
    )

    inside_roi = (
        refined_x
        >= roi_left
        - edge_margin
        and refined_y
        >= roi_top
        - edge_margin
        and refined_x
        + refined_width
        <= roi_right
        + edge_margin
        and refined_y
        + refined_height
        <= roi_bottom
        + edge_margin
    )

    contains_target = (
        cv2.pointPolygonTest(
            refined_contour,
            (
                float(
                    center[0]
                ),
                float(
                    center[1]
                ),
            ),
            False,
        )
        >= 0
    )

    integer_points = refined_contour.reshape(
        -1,
        2,
    )

    following_integer_points = np.roll(
        integer_points,
        -1,
        axis=0,
    )

    adjacent_steps = np.hypot(
        integer_points[:, 0]
        - following_integer_points[:, 0],
        integer_points[:, 1]
        - following_integer_points[:, 1],
    )

    maximum_adjacent_step = float(
        np.max(
            adjacent_steps
        )
    )

    refined_area_ratio = (
        refined_area
        / max(
            image_area,
            1,
        )
    )

    retained_supported_count = int(
        np.count_nonzero(
            supported
        )
    )

    supported_snap_distances = snap_distances[
        supported
    ]

    mean_snap_distance = (
        float(
            np.mean(
                supported_snap_distances
            )
        )
        if retained_supported_count
        else float(
            "inf"
        )
    )

    maximum_used_snap_distance = (
        float(
            np.max(
                supported_snap_distances
            )
        )
        if retained_supported_count
        else float(
            "inf"
        )
    )

    step_tolerance_limit = (
        maximum_allowed_step
        + EXACT_SHAPE_MAX_STEP_TOLERANCE_PX
    )

    shortcut_diagnostics = _inner_shortcut_diagnostics(
        refined_contour,
        center,
    )

    stable_shortcut_diagnostics = (
        _inner_shortcut_diagnostics(
            stable_contour,
            center,
        )
    )

    inner_threshold = float(
        shortcut_diagnostics.get(
            "inner_threshold_px",
            0.0,
        )
        or 0.0
    )
    inner_supported = False
    if shortcut_diagnostics.get("suspected", False) and inner_threshold > 0.0:
        pts = np.asarray(refined_contour, dtype=np.float32).reshape(-1, 2)
        center_pt = np.array([float(center[0]), float(center[1])], dtype=np.float32)
        inner_mask = (
            np.hypot(
                pts[:, 0] - center_pt[0],
                pts[:, 1] - center_pt[1],
            )
            < inner_threshold
        )
        if np.any(inner_mask) and float(np.mean(supported[inner_mask])) >= 0.40:
            inner_supported = True

    shortcut_relative_not_worse = bool(
        not shortcut_diagnostics.get(
            "suspected",
            False,
        )
        or inner_supported
        or (
            stable_shortcut_diagnostics.get(
                "suspected",
                False,
            )
            and float(
                shortcut_diagnostics.get(
                    "inner_point_fraction",
                    1.0,
                )
                or 1.0
            )
            <= float(
                stable_shortcut_diagnostics.get(
                    "inner_point_fraction",
                    0.0,
                )
                or 0.0
            )
            + EXACT_REPAIR_CONFIRMATION_SHORTCUT_FRACTION_TOLERANCE
            and float(
                shortcut_diagnostics.get(
                    "minimum_radius_px",
                    0.0,
                )
                or 0.0
            )
            >= float(
                stable_shortcut_diagnostics.get(
                    "minimum_radius_px",
                    0.0,
                )
                or 0.0
            )
            - EXACT_REPAIR_CONFIRMATION_SHORTCUT_RADIUS_TOLERANCE_PX
        )
    )

    (
        exact_minimum_area_ratio,
        exact_maximum_area_ratio,
    ) = _adaptive_area_limits(
        ACTIVE_LOCALITY_PROFILE,
        ACTIVE_CAPTURE_ZOOM,
    )

    exact_repair_limits = (
        _adaptive_exact_repair_limits(
            ACTIVE_LOCALITY_PROFILE,
            coverage_ratio,
        )
    )

    required_retained_support = float(
        exact_repair_limits[
            "required_retained_support"
        ]
    )

    maximum_exact_area_change = float(
        exact_repair_limits[
            "maximum_area_change_ratio"
        ]
    )

    maximum_mean_snap_distance = float(
        exact_repair_limits[
            "maximum_mean_snap_distance_px"
        ]
    )

    checks = {
        "area_ratio": (
            exact_minimum_area_ratio
            <= refined_area_ratio
            <= exact_maximum_area_ratio
        ),
        "area_change": (
            area_change_ratio
            <= maximum_exact_area_change
        ),
        "inside_roi": (
            inside_roi
        ),
        "contains_target": (
            contains_target
        ),
        "maximum_step": (
            maximum_adjacent_step
            <= step_tolerance_limit
        ),
        "retained_red_support": (
            retained_support_ratio
            >= required_retained_support
        ),
        "no_self_crossing": (
            repair_diagnostics.get(
                "self_crossings_after_repair",
                0,
            )
            == 0
        ),
        "no_inner_shortcut": (
            shortcut_relative_not_worse
        ),
        "mean_snap_distance": (
            mean_snap_distance
            <= maximum_mean_snap_distance
        ),
    }

    failed_checks = [
        name
        for name, passed
        in checks.items()
        if not passed
    ]

    diagnostics.update({
        "area_ratio": round(
            refined_area_ratio,
            4,
        ),
        "area_change_ratio": round(
            area_change_ratio,
            4,
        ),
        "adaptive_profile": (
            ACTIVE_LOCALITY_PROFILE
        ),
        "capture_zoom": round(
            float(
                ACTIVE_CAPTURE_ZOOM
            ),
            3,
        ),
        "coverage_ratio": round(
            float(
                coverage_ratio
            ),
            4,
        ),
        "minimum_area_ratio": round(
            exact_minimum_area_ratio,
            6,
        ),
        "maximum_area_ratio": round(
            exact_maximum_area_ratio,
            6,
        ),
        "required_retained_support": round(
            required_retained_support,
            4,
        ),
        "maximum_area_change_ratio": round(
            maximum_exact_area_change,
            4,
        ),
        "maximum_mean_snap_distance_px": round(
            maximum_mean_snap_distance,
            3,
        ),
        "inside_roi": bool(
            inside_roi
        ),
        "contains_target": bool(
            contains_target
        ),
        "max_step_px": round(
            maximum_adjacent_step,
            2,
        ),
        "max_allowed_step_px": round(
            maximum_allowed_step,
            2,
        ),
        "max_step_tolerance_limit_px": round(
            step_tolerance_limit,
            2,
        ),
        "shortcut": shortcut_diagnostics,
        "stable_shortcut": (
            stable_shortcut_diagnostics
        ),
        "shortcut_relative_not_worse": bool(
            shortcut_relative_not_worse
        ),
        "mean_snap_distance_px": round(
            mean_snap_distance,
            2,
        ),
        "max_snap_distance_px": round(
            maximum_used_snap_distance,
            2,
        ),
        "failed_checks": (
            failed_checks
        ),
    })

    if failed_checks:
        diagnostics[
            "reason"
        ] = "validation_failed"

        failed_check_set = set(
            failed_checks
        )

        repair_confirmation_checks = {
            "enabled": bool(
                EXACT_REPAIR_CONFIRMATION_ENABLED
            ),
            "failed_checks_allowed": (
                bool(
                    failed_check_set
                )
                and failed_check_set.issubset(
                    EXACT_REPAIR_CONFIRMATION_ALLOWED_FAILED_CHECKS
                )
            ),
            "coverage": (
                float(
                    coverage_ratio
                )
                >= EXACT_REPAIR_CONFIRMATION_MIN_COVERAGE
            ),
            "inside_roi": bool(
                inside_roi
            ),
            "contains_target": bool(
                contains_target
            ),
            "maximum_step": (
                maximum_adjacent_step
                <= step_tolerance_limit
            ),
            # The area limit guards against a repair inventing shape. That
            # risk disappears when every repaired vertex sits on Google's own
            # red pixels: the repair is then tracing the rendered boundary
            # through a gap, not guessing across it. Only in that case is a
            # larger area change allowed, and the distance of those vertices
            # to the red line must still be small.
            "area_change": (
                area_change_ratio
                <= (
                    maximum_exact_area_change
                    * EXACT_REPAIR_FULL_GAP_SUPPORT_AREA_CHANGE_FACTOR
                    if (
                        float(
                            repair_diagnostics.get(
                                "gap_red_support_ratio",
                                0.0,
                            )
                        )
                        >= EXACT_REPAIR_FULL_GAP_SUPPORT_RATIO
                        and float(
                            repair_diagnostics.get(
                                "gap_mean_red_distance_px",
                                999.0,
                            )
                        )
                        <= EXACT_REPAIR_FULL_GAP_SUPPORT_MAX_DISTANCE_PX
                    )
                    else maximum_exact_area_change
                )
            ),
            "no_self_crossing": (
                repair_diagnostics.get(
                    "self_crossings_after_repair",
                    1,
                )
                == 0
            ),
            "repaired_fraction": (
                float(
                    repair_diagnostics.get(
                        "repaired_fraction",
                        1.0,
                    )
                )
                <= EXACT_REPAIR_CONFIRMATION_MAX_REPAIRED_FRACTION
            ),
            "unrepaired_support": (
                float(
                    repair_diagnostics.get(
                        "unrepaired_support_ratio",
                        0.0,
                    )
                )
                >= EXACT_REPAIR_CONFIRMATION_MIN_UNREPAIRED_SUPPORT
            ),
            "gap_red_support": (
                float(
                    repair_diagnostics.get(
                        "gap_red_support_ratio",
                        0.0,
                    )
                )
                >= EXACT_REPAIR_CONFIRMATION_MIN_GAP_SUPPORT
            ),
            "gap_mean_red_distance": (
                float(
                    repair_diagnostics.get(
                        "gap_mean_red_distance_px",
                        999.0,
                    )
                )
                <= EXACT_REPAIR_CONFIRMATION_MAX_GAP_MEAN_DISTANCE_PX
            ),
            "shortcut_safe_or_deferred": bool(
                shortcut_relative_not_worse
                or (
                    EXACT_REPAIR_CONFIRMATION_ALLOW_SHORTCUT_DEFERRED
                    and "no_inner_shortcut"
                    in failed_check_set
                    and repair_diagnostics.get(
                        "self_crossings_after_repair",
                        1,
                    )
                    == 0
                )
            ),
        }

        confirmation_allowed = all(
            repair_confirmation_checks.values()
        )

        diagnostics[
            "confirmation"
        ] = {
            "accepted_as_provisional": bool(
                confirmation_allowed
            ),
            "checks": repair_confirmation_checks,
            "limits": {
                "minimum_coverage": (
                    EXACT_REPAIR_CONFIRMATION_MIN_COVERAGE
                ),
                "maximum_repaired_fraction": (
                    EXACT_REPAIR_CONFIRMATION_MAX_REPAIRED_FRACTION
                ),
                "minimum_unrepaired_support": (
                    EXACT_REPAIR_CONFIRMATION_MIN_UNREPAIRED_SUPPORT
                ),
                "minimum_gap_support": (
                    EXACT_REPAIR_CONFIRMATION_MIN_GAP_SUPPORT
                ),
                "maximum_gap_mean_distance_px": (
                    EXACT_REPAIR_CONFIRMATION_MAX_GAP_MEAN_DISTANCE_PX
                ),
            },
        }

        failed_confirmation_checks = [
            check_name
            for check_name, check_passed
            in repair_confirmation_checks.items()
            if not bool(
                check_passed
            )
        ]

        diagnostics[
            "confirmation"
        ][
            "failed_confirmation_checks"
        ] = failed_confirmation_checks

        if confirmation_allowed:
            diagnostics[
                "confirmation_candidate"
            ] = True

            diagnostics[
                "confirmation_pixel_contour"
            ] = refined_contour.reshape(
                -1,
                2,
            ).astype(
                int
            ).tolist()

            log(
                "   [Exact Confirm] Geometrically clean repair "
                "kept provisionally: "
                f"coverage={coverage_ratio:.3f}, "
                "unrepaired_support="
                f"{repair_diagnostics.get('unrepaired_support_ratio', 0.0):.3f}, "
                "gap_support="
                f"{repair_diagnostics.get('gap_red_support_ratio', 0.0):.3f}, "
                "gap_mean_distance="
                f"{repair_diagnostics.get('gap_mean_red_distance_px', 0.0):.2f}px"
            )

        else:
            log(
                "   [Exact Confirm] Provisional repair rejected: "
                + ", ".join(
                    failed_confirmation_checks
                )
            )

        # A suspected inner shortcut on the repaired contour is treated as a
        # genuine concavity (not corner-cutting) when the stable contour has
        # no shortcut of its own and the repaired shape barely changed area.
        shortcut_is_genuine_concavity = bool(
            not stable_shortcut_diagnostics.get(
                "suspected",
                False,
            )
            and area_change_ratio
            <= maximum_exact_area_change
        )

        review_allowed_failed_checks = set(
            REPAIRED_REVIEW_ALLOWED_FAILED_CHECKS
        )

        if shortcut_is_genuine_concavity:
            review_allowed_failed_checks.add(
                "no_inner_shortcut"
            )

        review_allowed = (
            REPAIRED_REVIEW_EXPORT_ENABLED
            and failed_check_set.issubset(
                review_allowed_failed_checks
            )
            and bool(
                contains_target
            )
            and bool(
                inside_roi
            )
            and repair_diagnostics.get(
                "self_crossings_after_repair",
                1,
            )
            == 0
            and maximum_adjacent_step
            <= step_tolerance_limit
            and area_change_ratio
            <= maximum_exact_area_change
            and float(
                repair_diagnostics.get(
                    "retained_red_support_ratio",
                    0.0,
                )
                or 0.0
            )
            >= 0.65
            and (
                not shortcut_diagnostics.get(
                    "suspected",
                    False,
                )
                or shortcut_is_genuine_concavity
            )
        )

        if review_allowed:
            diagnostics[
                "review_candidate"
            ] = True

            diagnostics[
                "review_reason"
            ] = (
                "geometrically_clean_but_low_retained_red_support"
            )

            diagnostics[
                "review_pixel_contour"
            ] = refined_contour.reshape(
                -1,
                2,
            ).astype(
                int
            ).tolist()

            log(
                "   [Exact Shape] Repaired REVIEW candidate "
                "saved internally; failed only: "
                + ", ".join(
                    failed_checks
                )
            )

        log(
            "   [Exact Shape] Stable contour kept; "
            "exact candidate failed: "
            + ", ".join(
                failed_checks
            )
        )

        return None, diagnostics

    diagnostics[
        "accepted"
    ] = True

    diagnostics[
        "reason"
    ] = "accepted"

    tolerance_note = (
        " with tiny step tolerance"
        if maximum_adjacent_step > maximum_allowed_step
        else ""
    )

    log(
        "   [Exact Shape] Accepted"
        f"{tolerance_note}: "
        f"{len(refined_contour)} points, "
        f"retained support={retained_support_ratio:.3f}/"
        f"{required_retained_support:.3f}, "
        f"profile={ACTIVE_LOCALITY_PROFILE}, "
        f"area change={area_change_ratio * 100.0:.2f}%, "
        f"max step={maximum_adjacent_step:.2f}px/"
        f"{step_tolerance_limit:.2f}px, "
        f"mean snap={mean_snap_distance:.2f}px"
    )

    return (
        refined_contour,
        diagnostics,
    )




def _adaptive_google_red_mask(
    image: np.ndarray,
) -> tuple[
    np.ndarray,
    dict,
]:
    """
    Detect both strong Google red and thinner anti-aliased red pixels.
    """
    if image is None or image.size == 0:
        return (
            np.zeros(
                (
                    1,
                    1,
                ),
                dtype=np.uint8,
            ),
            {
                "strict_pixels": 0,
                "adaptive_pixels": 0,
                "combined_pixels": 0,
            },
        )

    image_float = image.astype(
        np.float32
    )

    target_bgr = np.array(
        [
            53.0,
            67.0,
            234.0,
        ],
        dtype=np.float32,
    )

    difference = (
        image_float
        - target_bgr
    )

    distance_squared = np.sum(
        difference
        * difference,
        axis=2,
        dtype=np.float32,
    )

    blue = image_float[:, :, 0]
    green = image_float[:, :, 1]
    red = image_float[:, :, 2]

    strict_colour = (
        distance_squared
        <= (
            68.0
            ** 2
        )
    )

    strict_channels = (
        (
            red
            >= 160.0
        )
        & (
            (
                red
                - green
            )
            >= 58.0
        )
        & (
            (
                red
                - blue
            )
            >= 58.0
        )
        & (
            green
            <= 155.0
        )
        & (
            blue
            <= 155.0
        )
    )

    strict_mask = (
        strict_colour
        & strict_channels
    )

    hsv = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2HSV,
    )

    hue = hsv[:, :, 0]
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]

    red_hue = (
        (
            hue
            <= 13
        )
        | (
            hue
            >= 170
        )
    )

    adaptive_channels = (
        (
            red
            >= 142.0
        )
        & (
            (
                red
                - green
            )
            >= 34.0
        )
        & (
            (
                red
                - blue
            )
            >= 34.0
        )
        & (
            green
            <= 195.0
        )
        & (
            blue
            <= 195.0
        )
    )

    adaptive_hsv = (
        red_hue
        & (
            saturation
            >= 72
        )
        & (
            value
            >= 142
        )
    )

    adaptive_mask = (
        adaptive_channels
        & adaptive_hsv
    )

    if ACTIVE_SATELLITE_VIEW:
        # Satellite imagery contains large warm-brown areas that satisfy the
        # map-view rule. The selected outline is rendered either orange/red
        # or vivid yellow depending on the Maps renderer and display scaling.
        satellite_red_mask = (
            (
                red
                >= SATELLITE_RED_MIN_RED
            )
            & (
                (
                    red
                    - green
                )
                >= SATELLITE_RED_MIN_RED_MINUS_GREEN
            )
            & (
                (
                    red
                    - blue
                )
                >= SATELLITE_RED_MIN_RED_MINUS_BLUE
            )
            & (
                saturation
                >= SATELLITE_RED_MIN_SATURATION
            )
            & (
                value
                >= SATELLITE_RED_MIN_VALUE
            )
        )

        satellite_yellow_mask = (
            (
                red
                >= SATELLITE_YELLOW_MIN_RED
            )
            & (
                green
                >= SATELLITE_YELLOW_MIN_GREEN
            )
            & (
                blue
                <= SATELLITE_YELLOW_MAX_BLUE
            )
            & (
                hue
                >= SATELLITE_YELLOW_MIN_HUE
            )
            & (
                hue
                <= SATELLITE_YELLOW_MAX_HUE
            )
            & (
                saturation
                >= SATELLITE_YELLOW_MIN_SATURATION
            )
            & (
                value
                >= SATELLITE_YELLOW_MIN_VALUE
            )
        )

        satellite_mask = (
            satellite_red_mask
            | satellite_yellow_mask
        )

        combined_mask = (
            strict_mask
            | satellite_mask
        ).astype(
            np.uint8
        ) * 255

        return (
            combined_mask,
            {
                "strict_pixels": int(
                    np.count_nonzero(
                        strict_mask
                    )
                ),
                "adaptive_pixels": int(
                    np.count_nonzero(
                        satellite_mask
                    )
                ),
                "satellite_red_pixels": int(
                    np.count_nonzero(
                        satellite_red_mask
                    )
                ),
                "satellite_yellow_pixels": int(
                    np.count_nonzero(
                        satellite_yellow_mask
                    )
                ),
                "combined_pixels": int(
                    np.count_nonzero(
                        combined_mask
                    )
                ),
                "satellite_mode": True,
            },
        )

    combined_mask = (
        strict_mask
        | adaptive_mask
    ).astype(
        np.uint8
    ) * 255

    return (
        combined_mask,
        {
            "strict_pixels": int(
                np.count_nonzero(
                    strict_mask
                )
            ),
            "adaptive_pixels": int(
                np.count_nonzero(
                    adaptive_mask
                )
            ),
            "combined_pixels": int(
                np.count_nonzero(
                    combined_mask
                )
            ),
        },
    )


def _red_render_metrics(
    image: np.ndarray,
    map_metrics: Optional[dict] = None,
) -> dict:
    red_mask, colour_metrics = (
        _adaptive_google_red_mask(
            image
        )
    )

    image_height, image_width = (
        red_mask.shape[:2]
    )

    crop_left = 0
    crop_top = 0
    crop_right = image_width
    crop_bottom = image_height

    if isinstance(
        map_metrics,
        dict,
    ):
        device_pixel_ratio = float(
            map_metrics.get(
                "device_pixel_ratio",
                map_metrics.get(
                    "dpr",
                    1.0,
                ),
            )
            or 1.0
        )

        crop_left = int(
            round(
                float(
                    map_metrics.get(
                        "left",
                        0.0,
                    )
                )
                * device_pixel_ratio
            )
        )

        crop_top = int(
            round(
                float(
                    map_metrics.get(
                        "top",
                        0.0,
                    )
                )
                * device_pixel_ratio
            )
        )

        crop_width = int(
            round(
                float(
                    map_metrics.get(
                        "width",
                        image_width,
                    )
                )
                * device_pixel_ratio
            )
        )

        crop_height = int(
            round(
                float(
                    map_metrics.get(
                        "height",
                        image_height,
                    )
                )
                * device_pixel_ratio
            )
        )

        crop_right = min(
            image_width,
            crop_left
            + max(
                1,
                crop_width,
            ),
        )

        crop_bottom = min(
            image_height,
            crop_top
            + max(
                1,
                crop_height,
            ),
        )

    crop_left = max(
        0,
        min(
            crop_left,
            image_width
            - 1,
        ),
    )

    crop_top = max(
        0,
        min(
            crop_top,
            image_height
            - 1,
        ),
    )

    crop_right = max(
        crop_left
        + 1,
        min(
            crop_right,
            image_width,
        ),
    )

    crop_bottom = max(
        crop_top
        + 1,
        min(
            crop_bottom,
            image_height,
        ),
    )

    map_red_mask = red_mask[
        crop_top:crop_bottom,
        crop_left:crop_right,
    ]

    red_y, red_x = np.where(
        map_red_mask
        > 0
    )

    red_pixel_count = int(
        len(
            red_x
        )
    )

    if red_pixel_count:
        horizontal_span = int(
            np.max(
                red_x
            )
            - np.min(
                red_x
            )
            + 1
        )

        vertical_span = int(
            np.max(
                red_y
            )
            - np.min(
                red_y
            )
            + 1
        )

    else:
        horizontal_span = 0
        vertical_span = 0

    map_area_pixels = int(
        map_red_mask.shape[
            0
        ]
        * map_red_mask.shape[
            1
        ]
    )

    red_ratio = (
        red_pixel_count
        / max(
            map_area_pixels,
            1,
        )
    )

    return {
        **colour_metrics,
        "map_red_pixels": red_pixel_count,
        "horizontal_span_px": horizontal_span,
        "vertical_span_px": vertical_span,
        "map_area_pixels": map_area_pixels,
        "map_red_ratio": round(
            float(
                red_ratio
            ),
            6,
        ),
    }


def wait_for_google_red_outline(
    driver,
    map_metrics: Optional[dict],
    log=print,
    timeout_seconds: float = RED_RENDER_WAIT_SECONDS,
) -> dict:
    """
    Wait for the red locality outline to finish rendering after page load or
    zoom animation. This does not click, reload, or change the selected place.
    """
    deadline = (
        time.time()
        + max(
            1.0,
            float(
                timeout_seconds
            ),
        )
    )

    best_metrics = {
        "map_red_pixels": 0,
        "horizontal_span_px": 0,
        "vertical_span_px": 0,
    }

    attempt_number = 0

    while time.time() < deadline:
        attempt_number += 1

        try:
            screenshot_bytes = (
                driver.get_screenshot_as_png()
            )

            image_array = np.frombuffer(
                screenshot_bytes,
                dtype=np.uint8,
            )

            image = cv2.imdecode(
                image_array,
                cv2.IMREAD_COLOR,
            )

            metrics = _red_render_metrics(
                image,
                map_metrics=map_metrics,
            )

            if (
                metrics[
                    "map_red_pixels"
                ]
                > best_metrics[
                    "map_red_pixels"
                ]
            ):
                best_metrics = metrics

            log(
                "   [Red Render] attempt "
                f"{attempt_number}: "
                f"pixels={metrics['map_red_pixels']}, "
                f"span={metrics['horizontal_span_px']}×"
                f"{metrics['vertical_span_px']}px"
            )

            ready = (
                metrics[
                    "map_red_pixels"
                ]
                >= RED_RENDER_MIN_PIXELS
                and metrics[
                    "horizontal_span_px"
                ]
                >= RED_RENDER_MIN_HORIZONTAL_SPAN_PX
                and metrics[
                    "vertical_span_px"
                ]
                >= RED_RENDER_MIN_VERTICAL_SPAN_PX
            )

            if ready:
                log(
                    "   [Red Render] Google-red outline "
                    "appears fully rendered"
                )

                return {
                    **metrics,
                    "ready": True,
                }

        except Exception as exc:
            log(
                "   [Red Render] Render check failed: "
                f"{exc}"
            )

        time.sleep(
            RED_RENDER_POLL_SECONDS
        )

    log(
        "   [Red Render] WARNING: full outline was not "
        "confirmed before timeout; using best available "
        f"render (pixels={best_metrics['map_red_pixels']}, "
        f"span={best_metrics['horizontal_span_px']}×"
        f"{best_metrics['vertical_span_px']}px)"
    )

    return {
        **best_metrics,
        "ready": False,
    }





def _red_render_is_flood(
    metrics: dict,
    reference_red_pixels: int = 0,
) -> tuple[bool, dict]:
    red_pixels = int(
        metrics.get(
            "map_red_pixels",
            0,
        )
        or 0
    )

    red_ratio = float(
        metrics.get(
            "map_red_ratio",
            0.0,
        )
        or 0.0
    )

    reference_pixels = max(
        0,
        int(
            reference_red_pixels
            or 0
        ),
    )

    maximum_red_ratio = (
        SATELLITE_RED_FLOOD_MAX_MAP_RED_RATIO
        if ACTIVE_SATELLITE_VIEW
        else RED_FLOOD_MAX_MAP_RED_RATIO
    )

    absolute_flood = bool(
        red_pixels
        >= RED_FLOOD_MIN_ABSOLUTE_PIXELS
        and red_ratio
        > maximum_red_ratio
    )

    relative_flood = bool(
        reference_pixels > 0
        and red_pixels
        > reference_pixels
        * RED_FLOOD_RELATIVE_MULTIPLIER
        and (
            red_pixels
            - reference_pixels
        )
        >= RED_FLOOD_MIN_RELATIVE_EXCESS_PIXELS
    )

    diagnostics = {
        "enabled": bool(
            RED_FLOOD_GUARD_ENABLED
        ),
        "red_pixels": red_pixels,
        "red_ratio": round(
            red_ratio,
            6,
        ),
        "reference_red_pixels": reference_pixels,
        "absolute_flood": absolute_flood,
        "relative_flood": relative_flood,
        "flood": bool(
            RED_FLOOD_GUARD_ENABLED
            and (
                absolute_flood
                or relative_flood
            )
        ),
    }

    return (
        bool(
            diagnostics[
                "flood"
            ]
        ),
        diagnostics,
    )


def _dismiss_red_flood_overlays(
    driver,
    log=print,
) -> dict:
    diagnostics = {
        "escape_sent": False,
        "hidden_red_overlays": 0,
    }

    try:
        driver.execute_script(
            """
            if (document.activeElement) {
                document.activeElement.blur();
            }

            document.body.dispatchEvent(
                new KeyboardEvent(
                    'keydown',
                    {
                        key: 'Escape',
                        code: 'Escape',
                        bubbles: true
                    }
                )
            );
            """
        )

        diagnostics[
            "escape_sent"
        ] = True

    except Exception:
        pass

    try:
        hidden_count = int(
            driver.execute_script(
                """
                let hidden = 0;
                const viewportArea = Math.max(
                    1,
                    window.innerWidth * window.innerHeight
                );

                const redDominant = (value) => {
                    const match = String(value || '').match(
                        /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/
                    );

                    if (!match) {
                        return false;
                    }

                    const red = Number(match[1]);
                    const green = Number(match[2]);
                    const blue = Number(match[3]);

                    return (
                        red >= 150 &&
                        red >= green * 1.28 &&
                        red >= blue * 1.28
                    );
                };

                for (
                    const element of
                    document.querySelectorAll('body *')
                ) {
                    if (
                        element.tagName === 'CANVAS' ||
                        element.id === 'scene' ||
                        element.classList.contains('widget-scene') ||
                        element.classList.contains(
                            'widget-scene-canvas'
                        )
                    ) {
                        continue;
                    }

                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();

                    if (
                        rect.width <= 0 ||
                        rect.height <= 0 ||
                        rect.right <= 0 ||
                        rect.bottom <= 0
                    ) {
                        continue;
                    }

                    const positioned = (
                        style.position === 'fixed' ||
                        style.position === 'absolute'
                    );

                    const areaRatio = (
                        rect.width * rect.height
                    ) / viewportArea;

                    const redStyled = (
                        redDominant(style.backgroundColor) ||
                        redDominant(style.borderColor) ||
                        redDominant(style.color)
                    );

                    if (
                        positioned &&
                        redStyled &&
                        areaRatio >= 0.004
                    ) {
                        element.dataset.jarvisFloodHidden = '1';
                        element.style.setProperty(
                            'visibility',
                            'hidden',
                            'important'
                        );
                        element.style.setProperty(
                            'pointer-events',
                            'none',
                            'important'
                        );
                        hidden += 1;
                    }
                }

                return hidden;
                """
            )
            or 0
        )

        diagnostics[
            "hidden_red_overlays"
        ] = hidden_count

    except Exception:
        pass

    close_maps_ui(
        driver,
        log=log,
    )

    log(
        "   [Red Flood] Cleanup applied: "
        f"hidden_overlays="
        f"{diagnostics['hidden_red_overlays']}"
    )

    return diagnostics


def _screenshot_red_metrics(
    screenshot_path: str,
    map_metrics: Optional[dict],
) -> dict:
    image = cv2.imread(
        screenshot_path
    )

    if image is None:
        return {
            "map_red_pixels": 0,
            "map_red_ratio": 0.0,
            "map_area_pixels": 0,
        }

    return _red_render_metrics(
        image,
        map_metrics=map_metrics,
    )


def capture_screenshot_with_red_flood_guard(
    driver,
    output_path: str,
    map_metrics: Optional[dict],
    reference_red_pixels: int = 0,
    log=print,
) -> tuple[
    str,
    int,
    int,
    dict,
    bool,
]:
    output_path = os.path.abspath(
        output_path
    )

    base_path, extension = os.path.splitext(
        output_path
    )

    attempts = []
    maximum_attempts = (
        1
        + (
            RED_FLOOD_RECAPTURE_ATTEMPTS
            if RED_FLOOD_GUARD_ENABLED
            else 0
        )
    )

    final_width = 0
    final_height = 0

    for attempt_index in range(
        maximum_attempts
    ):
        attempt_path = (
            output_path
            if attempt_index == 0
            else (
                base_path
                + f"_red_flood_retry{attempt_index}"
                + extension
            )
        )

        (
            _,
            final_width,
            final_height,
        ) = capture_screenshot(
            driver,
            attempt_path,
            map_metrics=map_metrics,
            log=log,
        )

        metrics = _screenshot_red_metrics(
            attempt_path,
            map_metrics,
        )

        flood, flood_diagnostics = (
            _red_render_is_flood(
                metrics,
                reference_red_pixels=reference_red_pixels,
            )
        )

        attempts.append({
            "attempt": int(
                attempt_index
                + 1
            ),
            "path": attempt_path,
            "metrics": dict(
                metrics
            ),
            "flood": bool(
                flood
            ),
            "flood_diagnostics": dict(
                flood_diagnostics
            ),
        })

        log(
            "   [Red Flood] Capture "
            f"{attempt_index + 1}: "
            f"pixels={metrics.get('map_red_pixels', 0)}, "
            f"ratio={float(metrics.get('map_red_ratio', 0.0)):.4f}, "
            f"reference={int(reference_red_pixels or 0)}, "
            f"status={'FLOOD' if flood else 'clean'}"
        )

        if not flood:
            if attempt_path != output_path:
                shutil.copy2(
                    attempt_path,
                    output_path,
                )

            for attempt in attempts:
                temporary_path = attempt[
                    "path"
                ]

                if (
                    temporary_path != output_path
                    and os.path.exists(
                        temporary_path
                    )
                ):
                    try:
                        os.remove(
                            temporary_path
                        )
                    except Exception:
                        pass

            return (
                output_path,
                final_width,
                final_height,
                {
                    **metrics,
                    "flood_guard": {
                        "usable": True,
                        "attempts": attempts,
                    },
                },
                True,
            )

        if (
            attempt_index
            < maximum_attempts
            - 1
        ):
            log(
                "   [Red Flood] Abnormal red render detected; "
                "dismissing overlays and recapturing the same zoom"
            )

            _dismiss_red_flood_overlays(
                driver,
                log=log,
            )

            time.sleep(
                RED_FLOOD_SETTLE_SECONDS
            )

    # Every recapture was flooded. Keep the least contaminated screenshot for
    # debugging, but mark this zoom diagnostic-only.
    best_attempt = min(
        attempts,
        key=lambda attempt: (
            float(
                attempt[
                    "metrics"
                ].get(
                    "map_red_ratio",
                    1.0,
                )
                or 1.0
            ),
            int(
                attempt[
                    "metrics"
                ].get(
                    "map_red_pixels",
                    10**9,
                )
                or 10**9
            ),
        ),
    )

    if best_attempt[
        "path"
    ] != output_path:
        shutil.copy2(
            best_attempt[
                "path"
            ],
            output_path,
        )

    for attempt in attempts:
        temporary_path = attempt[
            "path"
        ]

        if (
            temporary_path != output_path
            and os.path.exists(
                temporary_path
            )
        ):
            try:
                os.remove(
                    temporary_path
                )
            except Exception:
                pass

    best_metrics = dict(
        best_attempt[
            "metrics"
        ]
    )

    log(
        "   [Red Flood] WARNING: all same-zoom recaptures "
        "remained flooded; this capture is diagnostic-only"
    )

    return (
        output_path,
        final_width,
        final_height,
        {
            **best_metrics,
            "flood_guard": {
                "usable": False,
                "attempts": attempts,
                "selected_attempt": int(
                    best_attempt[
                        "attempt"
                    ]
                ),
            },
        },
        False,
    )


def _centering_snapshot_is_trusted(
    snapshot: dict,
) -> bool:
    if not isinstance(snapshot, dict):
        return False

    status = str(snapshot.get("status", "") or "").strip().lower()
    trusted_statuses = {
        "camera_locked",
        "already_capture_safe",
        "already_centered",
        "capture_safe",
        "target_already_centered",
        "verified_capture_safe",
    }

    if bool(snapshot.get("successful", False)) and status in trusted_statuses:
        return True

    if snapshot.get("capture_safe") is True:
        return True

    offset = (
        snapshot.get("final_offset_px")
        or snapshot.get("initial_offset_px")
        or snapshot.get("offset_px")
        or {}
    )
    try:
        offset_x = abs(float(offset.get("x")))
        offset_y = abs(float(offset.get("y")))
    except (TypeError, ValueError, AttributeError):
        return False

    return bool(
        offset_x <= TARGET_CENTER_CAPTURE_MAX_OFFSET_X_PX
        and offset_y <= TARGET_CENTER_CAPTURE_MAX_OFFSET_Y_PX
    )


def _directional_line_kernel(length: int, angle_degrees: float) -> np.ndarray:
    length = max(3, int(round(length)))
    if length % 2 == 0:
        length += 1
    kernel = np.zeros((length, length), dtype=np.uint8)
    c = length // 2
    r = length // 2
    angle = math.radians(float(angle_degrees))
    dx = int(round(math.cos(angle) * r))
    dy = int(round(math.sin(angle) * r))
    cv2.line(kernel, (c - dx, c - dy), (c + dx, c + dy), 1, 1)
    return kernel


def _morphological_skeleton(binary_mask: np.ndarray) -> np.ndarray:
    working = (binary_mask > 0).astype(np.uint8) * 255
    skeleton = np.zeros_like(working)
    element = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    for _ in range(max(100, working.shape[0] + working.shape[1])):
        opened = cv2.morphologyEx(working, cv2.MORPH_OPEN, element)
        skeleton = cv2.bitwise_or(skeleton, cv2.subtract(working, opened))
        working = cv2.erode(working, element)
        if cv2.countNonZero(working) == 0:
            break
    return skeleton


def _endpoint_tangent_from_component(endpoint_xy, component_points_xy, neighbourhood_radius=24.0):
    if component_points_xy is None or len(component_points_xy) < 3:
        return None
    endpoint = np.asarray(endpoint_xy, dtype=np.float32)
    deltas = component_points_xy.astype(np.float32) - endpoint
    distances = np.hypot(deltas[:, 0], deltas[:, 1])
    nearby = component_points_xy[(distances > 0.0) & (distances <= float(neighbourhood_radius))]
    if len(nearby) < 2:
        nearby = component_points_xy[np.argsort(distances)[1:min(len(component_points_xy), 9)]]
    if len(nearby) == 0:
        return None
    outward = endpoint - np.mean(nearby.astype(np.float32), axis=0)
    norm = float(np.linalg.norm(outward))
    return None if norm <= 1e-6 else outward / norm


def _distance_point_to_segment(point_xy, start_xy, end_xy) -> float:
    point = np.asarray(point_xy, dtype=np.float64)
    start = np.asarray(start_xy, dtype=np.float64)
    end = np.asarray(end_xy, dtype=np.float64)
    segment = end - start
    length_squared = float(np.dot(segment, segment))
    if length_squared <= 1e-9:
        return float(np.linalg.norm(point - start))
    projection = max(0.0, min(1.0, float(np.dot(point - start, segment) / length_squared)))
    nearest = start + projection * segment
    return float(np.linalg.norm(point - nearest))


def _extract_directional_graph_endpoints(skeleton: np.ndarray):
    binary = (skeleton > 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    neighbours = cv2.filter2D(binary, cv2.CV_16S, np.ones((3, 3), np.uint8)) - binary.astype(np.int16)
    endpoints, summaries = [], []
    for component_id in range(1, count):
        pixels = int(stats[component_id, cv2.CC_STAT_AREA])
        if pixels < DIRECTIONAL_GRAPH_MIN_COMPONENT_PIXELS:
            continue
        ys, xs = np.where(labels == component_id)
        component_points = np.column_stack((xs, ys)).astype(np.int32)
        tip_y, tip_x = np.where((labels == component_id) & (neighbours <= 1))
        tips = np.column_stack((tip_x, tip_y)).astype(np.int32)
        clustered = []
        for tip in tips:
            if any(np.linalg.norm(tip - old_tip) <= 6.0 for old_tip in clustered):
                continue
            clustered.append(tip)
        if len(clustered) > 2:
            best_pair, best_distance = None, -1.0
            for i in range(len(clustered)):
                for j in range(i + 1, len(clustered)):
                    distance = float(np.linalg.norm(clustered[i] - clustered[j]))
                    if distance > best_distance:
                        best_pair, best_distance = (clustered[i], clustered[j]), distance
            clustered = list(best_pair) if best_pair else clustered[:2]
        selected_count = 0
        for tip in clustered:
            tangent = _endpoint_tangent_from_component((int(tip[0]), int(tip[1])), component_points)
            if tangent is None:
                continue
            endpoints.append({
                'component_id': int(component_id),
                'point': (int(tip[0]), int(tip[1])),
                'tangent': (float(tangent[0]), float(tangent[1])),
                'component_pixels': pixels,
            })
            selected_count += 1
        summaries.append({'component_id': component_id, 'pixels': pixels, 'selected_endpoints': selected_count})
    return endpoints, {'component_count': len(summaries), 'endpoint_count': len(endpoints), 'components': summaries}


def _directional_bridge_candidates(endpoints, center, maximum_gap_px):
    if len(endpoints) < 2:
        return []
    radii = [float(np.hypot(ep['point'][0] - center[0], ep['point'][1] - center[1])) for ep in endpoints]
    median_radius = float(np.median(radii)) if radii else 1.0
    minimum_clearance = max(22.0, median_radius * DIRECTIONAL_GRAPH_MIN_TARGET_CLEARANCE_RATIO)
    candidates = []
    for i in range(len(endpoints)):
        first = endpoints[i]
        p1 = np.asarray(first['point'], np.float64)
        t1 = np.asarray(first['tangent'], np.float64)
        for j in range(i + 1, len(endpoints)):
            second = endpoints[j]
            if first['component_id'] == second['component_id']:
                continue
            p2 = np.asarray(second['point'], np.float64)
            vector = p2 - p1
            distance = float(np.linalg.norm(vector))
            if distance < 5.0 or distance > maximum_gap_px:
                continue
            direction = vector / distance
            t2 = np.asarray(second['tangent'], np.float64)
            a1 = float(np.dot(t1, direction))
            a2 = float(np.dot(t2, -direction))
            average = (a1 + a2) / 2.0
            if a1 < DIRECTIONAL_GRAPH_MIN_ENDPOINT_DIRECTION_COS or a2 < DIRECTIONAL_GRAPH_MIN_ENDPOINT_DIRECTION_COS or average < DIRECTIONAL_GRAPH_MIN_AVERAGE_DIRECTION_COS:
                continue
            radial_difference = abs(radii[i] - radii[j]) / max(radii[i], radii[j], 1.0)
            if radial_difference > DIRECTIONAL_GRAPH_MAX_RADIAL_DIFFERENCE_RATIO:
                continue
            clearance = _distance_point_to_segment(center, p1, p2)
            if clearance < minimum_clearance:
                continue
            midpoint_radius = float(np.linalg.norm((p1 + p2) / 2.0 - np.asarray(center)))
            if midpoint_radius < min(radii[i], radii[j]) * 0.48:
                continue
            score = average * 2.2 + min(a1, a2) * 0.8 + min(clearance / max(median_radius, 1.0), 1.0) * 0.35 - (distance / max(maximum_gap_px, 1.0)) * 0.85 - radial_difference * 0.45
            candidates.append({
                'first_index': i, 'second_index': j,
                'start': (int(p1[0]), int(p1[1])), 'end': (int(p2[0]), int(p2[1])),
                'distance_px': round(distance, 3), 'first_alignment': round(a1, 4),
                'second_alignment': round(a2, 4), 'target_clearance_px': round(clearance, 3),
                'radial_difference_ratio': round(radial_difference, 4), 'score': round(score, 5),
            })
    return sorted(candidates, key=lambda item: (item['score'], -item['distance_px']), reverse=True)


def _greedy_directional_bridges(
    candidates,
    maximum_gap_px,
    excluded_candidate_indexes=None,
    forced_candidate=None,
):
    excluded = set(excluded_candidate_indexes or [])
    selected = []
    used = set()

    if forced_candidate is not None:
        if forced_candidate['distance_px'] <= maximum_gap_px:
            selected.append(forced_candidate)
            used.update((
                forced_candidate['first_index'],
                forced_candidate['second_index'],
            ))

    for candidate_index, candidate in enumerate(candidates):
        if candidate_index in excluded:
            continue
        if forced_candidate is candidate:
            continue
        if candidate['distance_px'] > maximum_gap_px:
            continue
        first_index = candidate['first_index']
        second_index = candidate['second_index']
        if first_index in used or second_index in used:
            continue
        selected.append(candidate)
        used.update((first_index, second_index))
        if len(selected) >= DIRECTIONAL_GRAPH_MAX_BRIDGES:
            break

    return selected


def _directional_bridge_sets(candidates, maximum_gap_px):
    eligible = [
        candidate
        for candidate in candidates
        if candidate['distance_px'] <= float(maximum_gap_px)
    ][:DIRECTIONAL_GRAPH_TOP_CANDIDATES_PER_LEVEL]

    if not eligible:
        return []

    orderings = [
        sorted(eligible, key=lambda item: item['score'], reverse=True),
        sorted(
            eligible,
            key=lambda item: (
                item['distance_px'],
                -item['score'],
            ),
        ),
        sorted(
            eligible,
            key=lambda item: (
                -(item['first_alignment'] + item['second_alignment']),
                item['distance_px'],
            ),
        ),
        sorted(
            eligible,
            key=lambda item: (
                -item['target_clearance_px'],
                item['distance_px'],
            ),
        ),
    ]

    variants = []
    signatures = set()

    def add_variant(bridges, strategy):
        if not bridges:
            return
        signature = tuple(sorted(
            (
                min(bridge['first_index'], bridge['second_index']),
                max(bridge['first_index'], bridge['second_index']),
            )
            for bridge in bridges
        ))
        if not signature or signature in signatures:
            return
        signatures.add(signature)
        variants.append({
            'strategy': strategy,
            'bridges': bridges,
            'matching_score': round(
                sum(float(bridge['score']) for bridge in bridges),
                5,
            ),
        })

    for ordering_index, ordering in enumerate(orderings):
        add_variant(
            _greedy_directional_bridges(ordering, maximum_gap_px),
            f'ordering_{ordering_index}',
        )

        # Remove one of the strongest choices and let the remaining endpoints
        # form an alternative matching. This is useful when the top local edge
        # blocks the globally correct closed loop.
        for excluded_index in range(min(3, len(ordering))):
            add_variant(
                _greedy_directional_bridges(
                    ordering,
                    maximum_gap_px,
                    excluded_candidate_indexes={excluded_index},
                ),
                f'ordering_{ordering_index}_exclude_{excluded_index}',
            )

    score_order = orderings[0]
    for seed_index in range(
        min(DIRECTIONAL_GRAPH_SEEDED_VARIANTS, len(score_order))
    ):
        add_variant(
            _greedy_directional_bridges(
                score_order,
                maximum_gap_px,
                forced_candidate=score_order[seed_index],
            ),
            f'seed_{seed_index}',
        )

    variants.sort(
        key=lambda variant: (
            variant['matching_score'],
            -sum(
                float(bridge['distance_px'])
                for bridge in variant['bridges']
            ),
        ),
        reverse=True,
    )

    return variants[:DIRECTIONAL_GRAPH_MAX_PATH_VARIANTS]


def _circular_smooth_values(values, window_size, use_median=False):
    values = np.asarray(values, dtype=np.float32)
    padding = window_size // 2
    extended = np.concatenate((values[-padding:], values, values[:padding]))
    output = []
    for index in range(len(values)):
        window = extended[index:index + window_size]
        output.append(float(np.median(window) if use_median else np.mean(window)))
    return np.asarray(output, dtype=np.float32)



def _bootstrap_circular_smooth(
    values: np.ndarray,
    window_size: int,
    use_median: bool = False,
) -> np.ndarray:
    values = np.asarray(
        values,
        dtype=np.float32,
    )

    padding = max(
        1,
        int(
            window_size
        )
        // 2,
    )

    extended = np.concatenate(
        (
            values[
                -padding:
            ],
            values,
            values[
                :padding
            ],
        )
    )

    filtered = []

    for index in range(
        len(
            values
        )
    ):
        window = extended[
            index:
            index
            + int(
                window_size
            )
        ]

        filtered.append(
            float(
                np.median(
                    window
                )
                if use_median
                else np.mean(
                    window
                )
            )
        )

    return np.asarray(
        filtered,
        dtype=np.float32,
    )



def _clamp_image_bounds(
    bounds: tuple[int, int, int, int],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    left, top, right, bottom = bounds

    left = int(
        np.clip(
            left,
            0,
            max(
                width - 1,
                0,
            ),
        )
    )

    top = int(
        np.clip(
            top,
            0,
            max(
                height - 1,
                0,
            ),
        )
    )

    right = int(
        np.clip(
            right,
            left + 1,
            max(
                width,
                left + 1,
            ),
        )
    )

    bottom = int(
        np.clip(
            bottom,
            top + 1,
            max(
                height,
                top + 1,
            ),
        )
    )

    return (
        left,
        top,
        right,
        bottom,
    )


def _expand_axis_bounds(
    minimum_value: int,
    maximum_value: int,
    required_size: int,
    axis_limit: int,
    preferred_center: int,
) -> tuple[int, int]:
    current_size = max(
        1,
        int(
            maximum_value
            - minimum_value
        ),
    )

    required_size = min(
        max(
            1,
            int(
                required_size
            ),
        ),
        max(
            1,
            int(
                axis_limit
            ),
        ),
    )

    if current_size >= required_size:
        return (
            int(
                minimum_value
            ),
            int(
                maximum_value
            ),
        )

    half_size = required_size / 2.0

    proposed_minimum = int(
        math.floor(
            float(
                preferred_center
            )
            - half_size
        )
    )

    proposed_maximum = (
        proposed_minimum
        + required_size
    )

    if proposed_minimum < 0:
        proposed_maximum -= proposed_minimum
        proposed_minimum = 0

    if proposed_maximum > axis_limit:
        shift = (
            proposed_maximum
            - axis_limit
        )

        proposed_minimum -= shift
        proposed_maximum = axis_limit

    proposed_minimum = max(
        0,
        proposed_minimum,
    )

    proposed_maximum = min(
        axis_limit,
        max(
            proposed_minimum + 1,
            proposed_maximum,
        ),
    )

    return (
        int(
            proposed_minimum
        ),
        int(
            proposed_maximum
        ),
    )


def _full_frame_bootstrap_bounds(
    full_red_mask: np.ndarray,
    center: tuple[int, int],
    normal_roi_bounds: tuple[int, int, int, int],
) -> tuple[tuple[int, int, int, int], dict]:
    height, width = full_red_mask.shape[
        :2
    ]

    edge_margin = max(
        2,
        int(
            round(
                min(
                    width,
                    height,
                )
                * FULL_FRAME_BOOTSTRAP_EDGE_MARGIN_RATIO
            )
        ),
    )

    red_y, red_x = np.where(
        full_red_mask > 0
    )

    normal_left, normal_top, normal_right, normal_bottom = (
        normal_roi_bounds
    )

    if red_x.size > 0:
        red_left = int(
            np.min(
                red_x
            )
        )

        red_right = int(
            np.max(
                red_x
            )
        ) + 1

        red_top = int(
            np.min(
                red_y
            )
        )

        red_bottom = int(
            np.max(
                red_y
            )
        ) + 1

        padding_x = max(
            18,
            int(
                round(
                    width
                    * FULL_FRAME_BOOTSTRAP_RED_BBOX_PADDING_RATIO
                )
            ),
        )

        padding_y = max(
            18,
            int(
                round(
                    height
                    * FULL_FRAME_BOOTSTRAP_RED_BBOX_PADDING_RATIO
                )
            ),
        )

        left = min(
            normal_left,
            red_left
            - padding_x,
        )

        right = max(
            normal_right,
            red_right
            + padding_x,
        )

        top = min(
            normal_top,
            red_top
            - padding_y,
        )

        bottom = max(
            normal_bottom,
            red_bottom
            + padding_y,
        )

        evidence_bbox = {
            "left": red_left,
            "top": red_top,
            "right": red_right,
            "bottom": red_bottom,
        }

    else:
        left = edge_margin
        top = edge_margin
        right = width - edge_margin
        bottom = height - edge_margin

        evidence_bbox = None

    left = min(
        left,
        center[
            0
        ],
    )

    right = max(
        right,
        center[
            0
        ]
        + 1,
    )

    top = min(
        top,
        center[
            1
        ],
    )

    bottom = max(
        bottom,
        center[
            1
        ]
        + 1,
    )

    minimum_width = int(
        round(
            width
            * FULL_FRAME_BOOTSTRAP_MIN_WIDTH_RATIO
        )
    )

    minimum_height = int(
        round(
            height
            * FULL_FRAME_BOOTSTRAP_MIN_HEIGHT_RATIO
        )
    )

    left, right = _expand_axis_bounds(
        left,
        right,
        minimum_width,
        width,
        center[
            0
        ],
    )

    top, bottom = _expand_axis_bounds(
        top,
        bottom,
        minimum_height,
        height,
        center[
            1
        ],
    )

    left = max(
        edge_margin,
        left,
    )

    top = max(
        edge_margin,
        top,
    )

    right = min(
        width - edge_margin,
        right,
    )

    bottom = min(
        height - edge_margin,
        bottom,
    )

    # If applying the edge margin made the window smaller than the required
    # size, prefer the near-full frame window rather than cropping evidence.
    if (
        right
        - left
        < minimum_width
    ):
        left = edge_margin
        right = width - edge_margin

    if (
        bottom
        - top
        < minimum_height
    ):
        top = edge_margin
        bottom = height - edge_margin

    bounds = _clamp_image_bounds(
        (
            left,
            top,
            right,
            bottom,
        ),
        width,
        height,
    )

    diagnostics = {
        "enabled": bool(
            FULL_FRAME_BOOTSTRAP_ENABLED
        ),
        "bounds": {
            "left": int(
                bounds[
                    0
                ]
            ),
            "top": int(
                bounds[
                    1
                ]
            ),
            "right": int(
                bounds[
                    2
                ]
            ),
            "bottom": int(
                bounds[
                    3
                ]
            ),
        },
        "width_ratio": round(
            (
                bounds[
                    2
                ]
                - bounds[
                    0
                ]
            )
            / max(
                width,
                1,
            ),
            4,
        ),
        "height_ratio": round(
            (
                bounds[
                    3
                ]
                - bounds[
                    1
                ]
            )
            / max(
                height,
                1,
            ),
            4,
        ),
        "red_evidence_bbox": evidence_bbox,
        "full_red_pixels": int(
            cv2.countNonZero(
                full_red_mask
            )
        ),
    }

    return (
        bounds,
        diagnostics,
    )


def _bootstrap_validation_bounds(
    bootstrap_mask: np.ndarray,
    normal_roi_bounds: tuple[int, int, int, int],
    center: tuple[int, int],
) -> tuple[tuple[int, int, int, int], dict]:
    height, width = bootstrap_mask.shape[
        :2
    ]

    normal_left, normal_top, normal_right, normal_bottom = (
        normal_roi_bounds
    )

    mask_y, mask_x = np.where(
        bootstrap_mask > 0
    )

    if mask_x.size == 0:
        bounds = _clamp_image_bounds(
            normal_roi_bounds,
            width,
            height,
        )

        return bounds, {
            "expanded": False,
            "reason": "bootstrap_mask_empty",
        }

    bootstrap_left = int(
        np.min(
            mask_x
        )
    )

    bootstrap_right = int(
        np.max(
            mask_x
        )
    ) + 1

    bootstrap_top = int(
        np.min(
            mask_y
        )
    )

    bootstrap_bottom = int(
        np.max(
            mask_y
        )
    ) + 1

    margin = max(
        8,
        int(
            FULL_FRAME_BOOTSTRAP_VALIDATION_MARGIN_PX
        ),
    )

    left = min(
        normal_left,
        bootstrap_left
        - margin,
        center[
            0
        ],
    )

    right = max(
        normal_right,
        bootstrap_right
        + margin,
        center[
            0
        ]
        + 1,
    )

    top = min(
        normal_top,
        bootstrap_top
        - margin,
        center[
            1
        ],
    )

    bottom = max(
        normal_bottom,
        bootstrap_bottom
        + margin,
        center[
            1
        ]
        + 1,
    )

    bounds = _clamp_image_bounds(
        (
            left,
            top,
            right,
            bottom,
        ),
        width,
        height,
    )

    diagnostics = {
        "expanded": bool(
            bounds
            != normal_roi_bounds
        ),
        "normal_roi": {
            "left": int(
                normal_left
            ),
            "top": int(
                normal_top
            ),
            "right": int(
                normal_right
            ),
            "bottom": int(
                normal_bottom
            ),
        },
        "bootstrap_bbox": {
            "left": int(
                bootstrap_left
            ),
            "top": int(
                bootstrap_top
            ),
            "right": int(
                bootstrap_right
            ),
            "bottom": int(
                bootstrap_bottom
            ),
        },
        "effective_roi": {
            "left": int(
                bounds[
                    0
                ]
            ),
            "top": int(
                bounds[
                    1
                ]
            ),
            "right": int(
                bounds[
                    2
                ]
            ),
            "bottom": int(
                bounds[
                    3
                ]
            ),
        },
    }

    return (
        bounds,
        diagnostics,
    )


def _build_preserved_polar_bootstrap(
    cleaned_mask: np.ndarray,
    strict_mask: np.ndarray,
    preserved_mask: np.ndarray,
    center: tuple[int, int],
    roi_bounds: tuple[int, int, int, int],
    profile_name: str,
    screenshot_path: str,
    log=print,
) -> tuple[Optional[np.ndarray], dict]:
    diagnostics = {
        "enabled": bool(
            PRESERVED_POLAR_BOOTSTRAP_ENABLED
        ),
        "accepted": False,
        "method": "preserved_mask_polar_bootstrap",
    }

    if not PRESERVED_POLAR_BOOTSTRAP_ENABLED:
        diagnostics[
            "reason"
        ] = "disabled"

        return None, diagnostics

    height, width = preserved_mask.shape[
        :2
    ]

    (
        roi_left,
        roi_top,
        roi_right,
        roi_bottom,
    ) = roi_bounds

    roi_mask = np.zeros(
        (
            height,
            width,
        ),
        dtype=np.uint8,
    )

    roi_mask[
        roi_top:
        roi_bottom,
        roi_left:
        roi_right,
    ] = 255

    strict_roi = cv2.bitwise_and(
        strict_mask,
        roi_mask,
    )

    preserved_roi = cv2.bitwise_and(
        preserved_mask,
        roi_mask,
    )

    cleaned_roi = cv2.bitwise_and(
        cleaned_mask,
        roi_mask,
    )

    source_mask = cv2.bitwise_or(
        strict_roi,
        preserved_roi,
    )

    source_count = int(
        cv2.countNonZero(
            source_mask
        )
    )

    strict_count = int(
        cv2.countNonZero(
            strict_roi
        )
    )

    cleaned_count = int(
        cv2.countNonZero(
            cleaned_roi
        )
    )

    if source_count < 60:
        diagnostics.update({
            "reason": "too_few_preserved_source_pixels",
            "source_pixels": source_count,
            "strict_pixels": strict_count,
            "cleaned_pixels": cleaned_count,
        })

        log(
            "   [Polar Bootstrap] Rejected: "
            "too_few_preserved_source_pixels; "
            f"source={source_count}, strict={strict_count}, "
            f"cleaned={cleaned_count}"
        )

        return None, diagnostics

    roi_half_width = max(
        1,
        (
            roi_right
            - roi_left
        )
        // 2,
    )

    roi_half_height = max(
        1,
        (
            roi_bottom
            - roi_top
        )
        // 2,
    )

    (
        base_minimum_radius,
        base_maximum_radius,
    ) = _adaptive_radius_limits(
        profile_name,
        width,
        height,
        roi_half_width,
        roi_half_height,
    )

    roi_corner_radii = [
        float(
            np.hypot(
                float(
                    corner_x
                    - center[
                        0
                    ]
                ),
                float(
                    corner_y
                    - center[
                        1
                    ]
                ),
            )
        )
        for corner_x, corner_y
        in (
            (
                roi_left,
                roi_top,
            ),
            (
                max(
                    roi_left,
                    roi_right
                    - 1,
                ),
                roi_top,
            ),
            (
                roi_left,
                max(
                    roi_top,
                    roi_bottom
                    - 1,
                ),
            ),
            (
                max(
                    roi_left,
                    roi_right
                    - 1,
                ),
                max(
                    roi_top,
                    roi_bottom
                    - 1,
                ),
            ),
        )
    ]

    expanded_roi_radius = (
        max(
            roi_corner_radii
        )
        * PRESERVED_POLAR_BOOTSTRAP_ROI_RADIUS_MARGIN
    )

    minimum_radius = max(
        PRESERVED_POLAR_BOOTSTRAP_MINIMUM_RADIUS_PX,
        min(
            float(
                base_minimum_radius
            ),
            float(
                min(
                    width,
                    height,
                )
            )
            * 0.012,
        ),
    )

    maximum_radius = (
        max(
            float(
                base_maximum_radius
            ),
            float(
                expanded_roi_radius
            ),
        )
        if PRESERVED_POLAR_BOOTSTRAP_EXPAND_TO_ROI
        else float(
            base_maximum_radius
        )
    )

    maximum_radius = min(
        maximum_radius,
        float(
            np.hypot(
                width,
                height,
            )
        ),
    )

    diagnostics[
        "radius_envelope"
    ] = {
        "base_minimum_radius_px": round(
            float(
                base_minimum_radius
            ),
            3,
        ),
        "base_maximum_radius_px": round(
            float(
                base_maximum_radius
            ),
            3,
        ),
        "expanded_roi_radius_px": round(
            float(
                expanded_roi_radius
            ),
            3,
        ),
        "used_minimum_radius_px": round(
            float(
                minimum_radius
            ),
            3,
        ),
        "used_maximum_radius_px": round(
            float(
                maximum_radius
            ),
            3,
        ),
    }

    log(
        "   [Polar Bootstrap] Radius envelope: "
        f"base_max={base_maximum_radius:.1f}px, "
        f"ROI_max={expanded_roi_radius:.1f}px, "
        f"used={maximum_radius:.1f}px"
    )

    source_y, source_x = np.where(
        source_mask > 0
    )

    strict_y, strict_x = np.where(
        strict_roi > 0
    )

    source_dx = (
        source_x.astype(
            np.float32
        )
        - float(
            center[
                0
            ]
        )
    )

    source_dy = (
        source_y.astype(
            np.float32
        )
        - float(
            center[
                1
            ]
        )
    )

    source_radii = np.hypot(
        source_dx,
        source_dy,
    )

    source_angles = (
        np.arctan2(
            source_dy,
            source_dx,
        )
        + 2.0
        * math.pi
    ) % (
        2.0
        * math.pi
    )

    strict_dx = (
        strict_x.astype(
            np.float32
        )
        - float(
            center[
                0
            ]
        )
    )

    strict_dy = (
        strict_y.astype(
            np.float32
        )
        - float(
            center[
                1
            ]
        )
    )

    strict_radii = np.hypot(
        strict_dx,
        strict_dy,
    )

    strict_angles = (
        np.arctan2(
            strict_dy,
            strict_dx,
        )
        + 2.0
        * math.pi
    ) % (
        2.0
        * math.pi
    )

    source_valid = (
        (
            source_radii
            >= minimum_radius
        )
        & (
            source_radii
            <= maximum_radius
        )
    )

    strict_valid = (
        (
            strict_radii
            >= minimum_radius
        )
        & (
            strict_radii
            <= maximum_radius
        )
    )

    source_radii = source_radii[
        source_valid
    ]

    source_angles = source_angles[
        source_valid
    ]

    strict_radii = strict_radii[
        strict_valid
    ]

    strict_angles = strict_angles[
        strict_valid
    ]

    if source_radii.size < 45:
        diagnostics.update({
            "reason": "too_few_radially_valid_pixels",
            "radially_valid_pixels": int(
                source_radii.size
            ),
            "minimum_radius_px": round(
                float(
                    minimum_radius
                ),
                3,
            ),
            "maximum_radius_px": round(
                float(
                    maximum_radius
                ),
                3,
            ),
        })

        log(
            "   [Polar Bootstrap] Rejected: "
            "too_few_radially_valid_pixels; "
            f"valid={source_radii.size}, "
            f"radius={minimum_radius:.1f}-"
            f"{maximum_radius:.1f}px"
        )

        return None, diagnostics

    bin_count = int(
        PRESERVED_POLAR_BOOTSTRAP_BIN_COUNT
    )

    source_bins = np.floor(
        source_angles
        / (
            2.0
            * math.pi
        )
        * bin_count
    ).astype(
        np.int32
    ) % bin_count

    strict_bins = np.floor(
        strict_angles
        / (
            2.0
            * math.pi
        )
        * bin_count
    ).astype(
        np.int32
    ) % bin_count

    samples = np.full(
        bin_count,
        np.nan,
        dtype=np.float32,
    )

    support = np.zeros(
        bin_count,
        dtype=np.int32,
    )

    strict_supported_bins = 0
    preserved_supported_bins = 0

    for bin_index in range(
        bin_count
    ):
        strict_values = strict_radii[
            strict_bins
            == bin_index
        ]

        preserved_values = source_radii[
            source_bins
            == bin_index
        ]

        if strict_values.size > 0:
            samples[
                bin_index
            ] = float(
                np.percentile(
                    strict_values,
                    PRESERVED_POLAR_BOOTSTRAP_STRICT_PERCENTILE,
                )
            )

            support[
                bin_index
            ] = int(
                strict_values.size
            )

            strict_supported_bins += 1

        elif preserved_values.size > 0:
            samples[
                bin_index
            ] = float(
                np.percentile(
                    preserved_values,
                    PRESERVED_POLAR_BOOTSTRAP_PRESERVED_PERCENTILE,
                )
            )

            support[
                bin_index
            ] = int(
                preserved_values.size
            )

            preserved_supported_bins += 1

    original_samples = samples.copy()
    rejected_bins = 0

    for bin_index in range(
        bin_count
    ):
        sample = original_samples[
            bin_index
        ]

        if np.isnan(
            sample
        ):
            continue

        neighbouring_values = []

        for offset in range(
            -6,
            7,
        ):
            if offset == 0:
                continue

            value = original_samples[
                (
                    bin_index
                    + offset
                )
                % bin_count
            ]

            if not np.isnan(
                value
            ):
                neighbouring_values.append(
                    float(
                        value
                    )
                )

        if len(
            neighbouring_values
        ) < 3:
            continue

        local_median = float(
            np.median(
                neighbouring_values
            )
        )

        deviation = abs(
            float(
                sample
            )
            - local_median
        )

        allowed_jump = max(
            PRESERVED_POLAR_BOOTSTRAP_MAX_RADIAL_JUMP_PX,
            local_median
            * PRESERVED_POLAR_BOOTSTRAP_MAX_RADIAL_JUMP_RATIO,
        )

        if (
            deviation
            > allowed_jump
            and support[
                bin_index
            ]
            <= 5
        ):
            samples[
                bin_index
            ] = np.nan

            rejected_bins += 1

    valid_bins = np.where(
        ~np.isnan(
            samples
        )
    )[0]

    coverage = (
        valid_bins.size
        / max(
            bin_count,
            1,
        )
    )

    direct_coverage = float(
        coverage
    )

    polar_fallback_filled_bins = 0
    polar_fallback_debug_path = ""

    if (
        coverage
        < PRESERVED_POLAR_BOOTSTRAP_MIN_COVERAGE
        and PRESERVED_POLAR_BOOTSTRAP_POLAR_FALLBACK_ENABLED
    ):
        polar_width = max(
            64,
            int(
                math.ceil(
                    maximum_radius
                )
            )
            + 1,
        )

        polar_flags = (
            cv2.WARP_POLAR_LINEAR
            | cv2.WARP_FILL_OUTLIERS
            | cv2.INTER_NEAREST
        )

        polar_source = cv2.warpPolar(
            source_mask,
            (
                polar_width,
                bin_count,
            ),
            (
                float(
                    center[
                        0
                    ]
                ),
                float(
                    center[
                        1
                    ]
                ),
            ),
            float(
                maximum_radius
            ),
            polar_flags,
        )

        polar_strict = cv2.warpPolar(
            strict_roi,
            (
                polar_width,
                bin_count,
            ),
            (
                float(
                    center[
                        0
                    ]
                ),
                float(
                    center[
                        1
                    ]
                ),
            ),
            float(
                maximum_radius
            ),
            polar_flags,
        )

        close_angle_bins = max(
            3,
            int(
                PRESERVED_POLAR_BOOTSTRAP_POLAR_CLOSE_ANGLE_BINS
            ),
        )

        if close_angle_bins % 2 == 0:
            close_angle_bins += 1

        close_radius_pixels = max(
            1,
            int(
                PRESERVED_POLAR_BOOTSTRAP_POLAR_CLOSE_RADIUS_PX
            ),
        )

        if close_radius_pixels % 2 == 0:
            close_radius_pixels += 1

        polar_close_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (
                close_radius_pixels,
                close_angle_bins,
            ),
        )

        polar_source_closed = cv2.morphologyEx(
            polar_source,
            cv2.MORPH_CLOSE,
            polar_close_kernel,
            iterations=1,
        )

        polar_strict_closed = cv2.morphologyEx(
            polar_strict,
            cv2.MORPH_CLOSE,
            polar_close_kernel,
            iterations=1,
        )

        radius_scale = (
            float(
                maximum_radius
            )
            / max(
                polar_width
                - 1,
                1,
            )
        )

        minimum_radius_column = int(
            np.clip(
                round(
                    minimum_radius
                    / max(
                        radius_scale,
                        1e-6,
                    )
                ),
                0,
                polar_width
                - 1,
            )
        )

        for bin_index in range(
            bin_count
        ):
            if not np.isnan(
                samples[
                    bin_index
                ]
            ):
                continue

            strict_columns = np.where(
                polar_strict_closed[
                    bin_index
                ]
                > 0
            )[0]

            source_columns = np.where(
                polar_source_closed[
                    bin_index
                ]
                > 0
            )[0]

            strict_columns = strict_columns[
                strict_columns
                >= minimum_radius_column
            ]

            source_columns = source_columns[
                source_columns
                >= minimum_radius_column
            ]

            selected_columns = (
                strict_columns
                if strict_columns.size > 0
                else source_columns
            )

            if selected_columns.size == 0:
                continue

            selected_radius_column = float(
                np.percentile(
                    selected_columns,
                    PRESERVED_POLAR_BOOTSTRAP_POLAR_RADIUS_PERCENTILE,
                )
            )

            samples[
                bin_index
            ] = (
                selected_radius_column
                * radius_scale
            )

            support[
                bin_index
            ] = max(
                int(
                    support[
                        bin_index
                    ]
                ),
                1,
            )

            polar_fallback_filled_bins += 1

        # A second conservative radial-spike pass after polar filling.
        fallback_samples = samples.copy()

        for bin_index in range(
            bin_count
        ):
            sample = fallback_samples[
                bin_index
            ]

            if np.isnan(
                sample
            ):
                continue

            neighbour_values = []

            for offset in range(
                -7,
                8,
            ):
                if offset == 0:
                    continue

                value = fallback_samples[
                    (
                        bin_index
                        + offset
                    )
                    % bin_count
                ]

                if not np.isnan(
                    value
                ):
                    neighbour_values.append(
                        float(
                            value
                        )
                    )

            if len(
                neighbour_values
            ) < 4:
                continue

            local_median = float(
                np.median(
                    neighbour_values
                )
            )

            allowed_jump = max(
                PRESERVED_POLAR_BOOTSTRAP_MAX_RADIAL_JUMP_PX,
                local_median
                * PRESERVED_POLAR_BOOTSTRAP_MAX_RADIAL_JUMP_RATIO,
            )

            if (
                abs(
                    float(
                        sample
                    )
                    - local_median
                )
                > allowed_jump
                and support[
                    bin_index
                ]
                <= 1
            ):
                samples[
                    bin_index
                ] = np.nan

        valid_bins = np.where(
            ~np.isnan(
                samples
            )
        )[0]

        coverage = (
            valid_bins.size
            / max(
                bin_count,
                1,
            )
        )

        polar_fallback_debug_path = (
            os.path.splitext(
                screenshot_path
            )[0]
            + "_red_bootstrap_polar_join.png"
        )

        polar_debug = cv2.max(
            polar_source_closed,
            polar_strict_closed,
        )

        cv2.imwrite(
            polar_fallback_debug_path,
            polar_debug,
        )

        log(
            "   [Polar Bootstrap] Polar-space dotted-line "
            "fallback: "
            f"direct_coverage={direct_coverage:.3f}, "
            f"filled_bins={polar_fallback_filled_bins}, "
            f"final_coverage={coverage:.3f}"
        )

    diagnostics[
        "direct_coverage"
    ] = round(
        float(
            direct_coverage
        ),
        4,
    )

    diagnostics[
        "polar_fallback_filled_bins"
    ] = int(
        polar_fallback_filled_bins
    )

    if polar_fallback_debug_path:
        diagnostics.setdefault(
            "debug_images",
            {},
        )[
            "polar_join"
        ] = polar_fallback_debug_path

    if (
        coverage
        < PRESERVED_POLAR_BOOTSTRAP_MIN_COVERAGE
    ):
        diagnostics.update({
            "reason": "insufficient_bootstrap_coverage",
            "coverage": round(
                float(
                    coverage
                ),
                4,
            ),
            "valid_bins": int(
                valid_bins.size
            ),
            "rejected_bins": int(
                rejected_bins
            ),
        })

        log(
            "   [Polar Bootstrap] Rejected: "
            "insufficient_bootstrap_coverage; "
            f"direct={direct_coverage:.3f}, "
            f"final={coverage:.3f}, "
            f"valid_bins={valid_bins.size}/"
            f"{bin_count}, "
            f"rejected_bins={rejected_bins}"
        )

        return None, diagnostics

    valid_values = samples[
        valid_bins
    ]

    extended_bins = np.concatenate(
        (
            valid_bins
            - bin_count,
            valid_bins,
            valid_bins
            + bin_count,
        )
    )

    extended_values = np.concatenate(
        (
            valid_values,
            valid_values,
            valid_values,
        )
    )

    completed_radii = np.interp(
        np.arange(
            bin_count,
            dtype=np.float32,
        ),
        extended_bins,
        extended_values,
    ).astype(
        np.float32
    )

    completed_radii = (
        _bootstrap_circular_smooth(
            completed_radii,
            5,
            use_median=True,
        )
    )

    completed_radii = (
        _bootstrap_circular_smooth(
            completed_radii,
            7,
            use_median=False,
        )
    )

    bin_angles = (
        (
            np.arange(
                bin_count,
                dtype=np.float32,
            )
            + 0.5
        )
        * (
            2.0
            * math.pi
            / bin_count
        )
    )

    point_x = (
        float(
            center[
                0
            ]
        )
        + completed_radii
        * np.cos(
            bin_angles
        )
    )

    point_y = (
        float(
            center[
                1
            ]
        )
        + completed_radii
        * np.sin(
            bin_angles
        )
    )

    continuous_points = np.column_stack(
        (
            point_x,
            point_y,
        )
    ).astype(
        np.int32
    )

    continuous_points[
        :,
        0
    ] = np.clip(
        continuous_points[
            :,
            0
        ],
        roi_left,
        max(
            roi_left,
            roi_right
            - 1,
        ),
    )

    continuous_points[
        :,
        1
    ] = np.clip(
        continuous_points[
            :,
            1
        ],
        roi_top,
        max(
            roi_top,
            roi_bottom
            - 1,
        ),
    )

    continuous_line = np.zeros_like(
        source_mask
    )

    cv2.polylines(
        continuous_line,
        [
            continuous_points.reshape(
                -1,
                1,
                2,
            )
        ],
        True,
        255,
        int(
            PRESERVED_POLAR_BOOTSTRAP_LINE_THICKNESS_PX
        ),
        cv2.LINE_AA,
    )

    dilation_size = int(
        PRESERVED_POLAR_BOOTSTRAP_SUPPORT_DILATION_PX.get(
            profile_name,
            23,
        )
    )

    if dilation_size % 2 == 0:
        dilation_size += 1

    line_support_corridor = cv2.dilate(
        continuous_line,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (
                dilation_size,
                dilation_size,
            ),
        ),
        iterations=1,
    )

    supported_original_pixels = cv2.bitwise_and(
        source_mask,
        line_support_corridor,
    )

    bootstrap_mask = cv2.bitwise_or(
        continuous_line,
        supported_original_pixels,
    )

    bootstrap_mask = cv2.bitwise_and(
        bootstrap_mask,
        roi_mask,
    )

    base_path = os.path.splitext(
        screenshot_path
    )[0]

    line_path = (
        base_path
        + "_red_bootstrap_continuous_line.png"
    )

    support_path = (
        base_path
        + "_red_bootstrap_supported_pixels.png"
    )

    cv2.imwrite(
        line_path,
        continuous_line,
    )

    cv2.imwrite(
        support_path,
        bootstrap_mask,
    )

    diagnostics.update({
        "accepted": True,
        "coverage": round(
            float(
                coverage
            ),
            4,
        ),
        "valid_bins": int(
            valid_bins.size
        ),
        "strict_supported_bins": int(
            strict_supported_bins
        ),
        "preserved_supported_bins": int(
            preserved_supported_bins
        ),
        "rejected_bins": int(
            rejected_bins
        ),
        "direct_coverage": round(
            float(
                direct_coverage
            ),
            4,
        ),
        "polar_fallback_filled_bins": int(
            polar_fallback_filled_bins
        ),
        "source_pixels": int(
            source_count
        ),
        "strict_pixels": int(
            strict_count
        ),
        "cleaned_pixels": int(
            cleaned_count
        ),
        "bootstrap_pixels": int(
            cv2.countNonZero(
                bootstrap_mask
            )
        ),
        "minimum_radius_px": round(
            float(
                minimum_radius
            ),
            3,
        ),
        "maximum_radius_px": round(
            float(
                maximum_radius
            ),
            3,
        ),
        "median_radius_px": round(
            float(
                np.median(
                    completed_radii
                )
            ),
            3,
        ),
        "debug_images": {
            "continuous_line": line_path,
            "supported_pixels": support_path,
            **(
                {
                    "polar_join": polar_fallback_debug_path,
                }
                if polar_fallback_debug_path
                else {}
            ),
        },
    })

    log(
        "   [Polar Bootstrap] Converted preserved red dots "
        "into a continuous guide line: "
        f"coverage={coverage:.3f}, "
        f"strict_bins={strict_supported_bins}, "
        f"preserved_bins={preserved_supported_bins}, "
        f"pixels={diagnostics['bootstrap_pixels']}"
    )

    return (
        bootstrap_mask,
        diagnostics,
    )


def _polar_bootstrap_candidate_is_safe(
    diagnostics: dict,
) -> bool:
    if not isinstance(
        diagnostics,
        dict,
    ):
        return False

    bootstrap = diagnostics.get(
        "polar_bootstrap",
        {},
    )

    if not isinstance(
        bootstrap,
        dict,
    ):
        return False

    return (
        bool(
            bootstrap.get(
                "accepted",
                False,
            )
        )
        and float(
            bootstrap.get(
                "coverage",
                0.0,
            )
            or 0.0
        )
        >= PRESERVED_POLAR_BOOTSTRAP_MIN_COVERAGE
        and int(
            bootstrap.get(
                "valid_bins",
                0,
            )
            or 0
        )
        >= int(
            PRESERVED_POLAR_BOOTSTRAP_BIN_COUNT
            * PRESERVED_POLAR_BOOTSTRAP_MIN_COVERAGE
        )
        and bool(
            diagnostics.get(
                "contains_target",
                True,
            )
        )
    )


def _evidence_polar_center(
    evidence_mask,
    target_center,
    roi_bounds,
    log=print,
):
    """
    Choose the centre used for polar boundary reconstruction.

    Google often keeps the selected place marker far from the middle of the
    locality (Sector 62 sits in its eastern half while the outline extends
    far west). Sampling radii from that marker makes one lobe sit at a very
    different radius from the other, so angular bins covering the far lobe
    fall outside the radius window and its dots are discarded.

    Using the centroid of the visible red evidence keeps every part of the
    outline at a comparable radius. The target pixel is still used for all
    containment and safety checks, so a bad centroid can never smuggle in a
    polygon that excludes the searched place.
    """
    diagnostics = {
        "enabled": True,
        "used_evidence_centre": False,
        "target_center": [
            int(target_center[0]),
            int(target_center[1]),
        ],
    }

    if evidence_mask is None:
        diagnostics["reason"] = "missing_evidence_mask"
        return tuple(target_center), diagnostics

    height, width = evidence_mask.shape[:2]
    roi_left, roi_top, roi_right, roi_bottom = roi_bounds

    ys, xs = np.where(evidence_mask > 0)

    inside = (
        (xs >= roi_left)
        & (xs < roi_right)
        & (ys >= roi_top)
        & (ys < roi_bottom)
    )
    xs, ys = xs[inside], ys[inside]

    diagnostics["evidence_pixels"] = int(xs.size)

    if xs.size < 120:
        diagnostics["reason"] = "too_few_evidence_pixels"
        return tuple(target_center), diagnostics

    # The centroid of the convex hull of the evidence is used rather than a
    # pixel median. The dotted outline is sampled unevenly, so a pixel median
    # drifts toward whichever edge carries the most dots and can even land on
    # the boundary itself. The hull centroid tracks the enclosed shape, which
    # is what polar sampling needs.
    hull = cv2.convexHull(
        np.stack(
            [xs, ys],
            axis=1,
        ).astype(np.int32)
    )

    moments = cv2.moments(hull)

    if moments["m00"] <= 0.0:
        diagnostics["reason"] = "degenerate_evidence_hull"
        return tuple(target_center), diagnostics

    centre_x = float(moments["m10"] / moments["m00"])
    centre_y = float(moments["m01"] / moments["m00"])

    diagnostics["evidence_center"] = [
        round(centre_x, 1),
        round(centre_y, 1),
    ]

    shift = float(
        math.hypot(
            centre_x - float(target_center[0]),
            centre_y - float(target_center[1]),
        )
    )

    maximum_shift = 0.40 * float(min(width, height))
    diagnostics["shift_px"] = round(shift, 1)
    diagnostics["maximum_shift_px"] = round(maximum_shift, 1)

    if shift > maximum_shift:
        diagnostics["reason"] = "evidence_center_too_far_from_target"
        return tuple(target_center), diagnostics

    # The centre must stay inside the search window, otherwise angular bins
    # would be measured from outside the region being reconstructed.
    if not (
        roi_left <= centre_x < roi_right
        and roi_top <= centre_y < roi_bottom
    ):
        diagnostics["reason"] = "evidence_center_outside_roi"
        return tuple(target_center), diagnostics

    # Every angular direction must still carry evidence, otherwise the new
    # centre is not enclosed by the outline and polar sampling would fail.
    angles = (
        np.arctan2(
            ys.astype(np.float32) - centre_y,
            xs.astype(np.float32) - centre_x,
        )
        + 2.0 * math.pi
    ) % (2.0 * math.pi)

    bins = np.floor(angles / (2.0 * math.pi) * 72).astype(np.int32) % 72
    occupied = int(np.unique(bins).size)
    coverage = occupied / 72.0

    diagnostics["angular_coverage"] = round(coverage, 4)

    target_angles = (
        np.arctan2(
            ys.astype(np.float32) - float(target_center[1]),
            xs.astype(np.float32) - float(target_center[0]),
        )
        + 2.0 * math.pi
    ) % (2.0 * math.pi)

    target_bins = (
        np.floor(target_angles / (2.0 * math.pi) * 72).astype(np.int32) % 72
    )
    target_coverage = int(np.unique(target_bins).size) / 72.0

    diagnostics["target_angular_coverage"] = round(target_coverage, 4)

    if coverage < target_coverage:
        diagnostics["reason"] = "target_center_has_better_angular_coverage"
        return tuple(target_center), diagnostics

    # Radius spread decides whether polar sampling can hold every lobe inside
    # one radius window. A centre that widens the spread would push the far
    # lobe out of range, which is exactly the failure being fixed.
    def radius_spread(origin):
        distances = np.hypot(
            xs.astype(np.float32) - float(origin[0]),
            ys.astype(np.float32) - float(origin[1]),
        )
        return float(
            np.max(distances)
            / max(float(np.min(distances)), 1.0)
        )

    evidence_spread = radius_spread(
        (centre_x, centre_y)
    )

    target_spread = radius_spread(
        target_center
    )

    diagnostics["radius_spread"] = round(evidence_spread, 2)
    diagnostics["target_radius_spread"] = round(target_spread, 2)

    if evidence_spread >= target_spread:
        diagnostics["reason"] = "target_center_has_tighter_radius_spread"
        return tuple(target_center), diagnostics

    diagnostics["used_evidence_centre"] = True
    diagnostics["reason"] = "evidence_center_accepted"

    log(
        "   [Polar Centre] Using red-evidence centre "
        f"({centre_x:.0f}, {centre_y:.0f}) instead of target "
        f"({int(target_center[0])}, {int(target_center[1])}); "
        f"shift={shift:.0f}px, angular coverage "
        f"{target_coverage:.3f} -> {coverage:.3f}"
    )

    return (
        int(round(centre_x)),
        int(round(centre_y)),
    ), diagnostics


def _rough_polar_radius_guide(
    guide_mask,
    center,
    roi_bounds,
    profile_name,
    image_width,
    image_height,
):
    roi_left, roi_top, roi_right, roi_bottom = roi_bounds
    y_values, x_values = np.where(guide_mask > 0)
    if len(x_values) < 45:
        return None, {
            'accepted': False,
            'reason': 'too_few_guide_pixels',
            'guide_pixels': int(len(x_values)),
        }

    inside_roi = (
        (x_values >= roi_left)
        & (x_values < roi_right)
        & (y_values >= roi_top)
        & (y_values < roi_bottom)
    )
    x_values = x_values[inside_roi]
    y_values = y_values[inside_roi]
    if len(x_values) < 45:
        return None, {
            'accepted': False,
            'reason': 'too_few_guide_pixels_in_roi',
            'guide_pixels': int(len(x_values)),
        }

    roi_half_width = max(1, (roi_right - roi_left) // 2)
    roi_half_height = max(1, (roi_bottom - roi_top) // 2)
    minimum_radius, maximum_radius = _adaptive_radius_limits(
        profile_name,
        image_width,
        image_height,
        roi_half_width,
        roi_half_height,
    )

    delta_x = x_values.astype(np.float32) - float(center[0])
    delta_y = y_values.astype(np.float32) - float(center[1])
    radii = np.hypot(delta_x, delta_y)
    angles = (np.arctan2(delta_y, delta_x) + 2.0 * math.pi) % (2.0 * math.pi)

    valid = (radii >= minimum_radius) & (radii <= maximum_radius)
    radii = radii[valid]
    angles = angles[valid]
    if radii.size < 45:
        return None, {
            'accepted': False,
            'reason': 'too_few_radially_valid_guide_pixels',
            'guide_pixels': int(radii.size),
            'minimum_radius_px': round(float(minimum_radius), 3),
            'maximum_radius_px': round(float(maximum_radius), 3),
        }

    bin_count = int(GRAPH_CORRIDOR_BIN_COUNT)
    bin_indices = np.floor(
        angles / (2.0 * math.pi) * bin_count
    ).astype(np.int32) % bin_count

    samples = np.full(bin_count, np.nan, dtype=np.float32)
    support = np.zeros(bin_count, dtype=np.int32)

    for bin_index in range(bin_count):
        values = radii[bin_indices == bin_index]
        if values.size == 0:
            continue
        # Use a moderately outer percentile. This avoids inner labels while
        # remaining less sensitive than the maximum to isolated outer pixels.
        samples[bin_index] = float(np.percentile(values, 68.0))
        support[bin_index] = int(values.size)

    original = samples.copy()
    rejected = 0
    for bin_index in range(bin_count):
        sample = original[bin_index]
        if np.isnan(sample):
            continue
        neighbours = []
        for offset in range(-5, 6):
            if offset == 0:
                continue
            value = original[(bin_index + offset) % bin_count]
            if not np.isnan(value):
                neighbours.append(float(value))
        if len(neighbours) < 3:
            continue
        local_median = float(np.median(neighbours))
        deviation = abs(float(sample) - local_median)
        limit = max(42.0, local_median * 0.23)
        if deviation > limit and support[bin_index] <= 4:
            samples[bin_index] = np.nan
            rejected += 1

    valid_bins = np.where(~np.isnan(samples))[0]
    coverage = valid_bins.size / max(bin_count, 1)
    if coverage < GRAPH_CORRIDOR_MIN_GUIDE_COVERAGE:
        return None, {
            'accepted': False,
            'reason': 'insufficient_angular_guide_coverage',
            'coverage': round(float(coverage), 4),
            'valid_bins': int(valid_bins.size),
            'rejected_bins': int(rejected),
        }

    valid_values = samples[valid_bins]
    extended_bins = np.concatenate((
        valid_bins - bin_count,
        valid_bins,
        valid_bins + bin_count,
    ))
    extended_values = np.concatenate((valid_values, valid_values, valid_values))
    completed = np.interp(
        np.arange(bin_count, dtype=np.float32),
        extended_bins,
        extended_values,
    ).astype(np.float32)
    completed = _circular_smooth_values(completed, 5, use_median=True)
    completed = _circular_smooth_values(completed, 7, use_median=False)

    return completed, {
        'accepted': True,
        'coverage': round(float(coverage), 4),
        'valid_bins': int(valid_bins.size),
        'rejected_bins': int(rejected),
        'minimum_radius_px': round(float(minimum_radius), 3),
        'maximum_radius_px': round(float(maximum_radius), 3),
        'median_completed_radius_px': round(float(np.median(completed)), 3),
    }


def _build_target_guided_graph_corridor(
    preserved_mask,
    guide_mask,
    center,
    roi_bounds,
    profile_name,
    screenshot_path,
    log=print,
):
    diagnostics = {
        'enabled': bool(GRAPH_CORRIDOR_ENABLED),
        'accepted': False,
    }
    if not GRAPH_CORRIDOR_ENABLED:
        diagnostics.update({'accepted': True, 'reason': 'disabled'})
        return preserved_mask.copy(), preserved_mask.copy(), diagnostics

    height, width = preserved_mask.shape[:2]

    # Measure the corridor from the centre of the visible red evidence so a
    # lobe far from the place marker is not pushed outside the radius window.
    corridor_center, corridor_center_diagnostics = _evidence_polar_center(
        evidence_mask=guide_mask,
        target_center=center,
        roi_bounds=roi_bounds,
        log=log,
    )
    diagnostics['polar_center'] = corridor_center_diagnostics

    guide_radii, guide_diagnostics = _rough_polar_radius_guide(
        guide_mask=guide_mask,
        center=corridor_center,
        roi_bounds=roi_bounds,
        profile_name=profile_name,
        image_width=width,
        image_height=height,
    )
    diagnostics['guide'] = guide_diagnostics
    if guide_radii is None:
        diagnostics['reason'] = 'rough_guide_unavailable'
        log(
            '   [Graph Corridor] Rough polar guide unavailable; '
            'using preserved graph mask without corridor filtering'
        )
        return preserved_mask.copy(), preserved_mask.copy(), diagnostics

    y_grid, x_grid = np.ogrid[:height, :width]
    delta_x = x_grid.astype(np.float32) - float(corridor_center[0])
    delta_y = y_grid.astype(np.float32) - float(corridor_center[1])
    radii = np.hypot(delta_x, delta_y)
    angles = (np.arctan2(delta_y, delta_x) + 2.0 * math.pi) % (2.0 * math.pi)
    bin_indices = np.floor(
        angles / (2.0 * math.pi) * len(guide_radii)
    ).astype(np.int32) % len(guide_radii)
    expected = guide_radii[bin_indices]

    minimum_half_width = float(
        GRAPH_CORRIDOR_MIN_HALF_WIDTH_PX.get(profile_name, 26.0)
    )
    maximum_half_width = float(
        GRAPH_CORRIDOR_MAX_HALF_WIDTH_PX.get(profile_name, 88.0)
    )
    inner_margin = np.clip(
        expected * float(GRAPH_CORRIDOR_INNER_RATIO.get(profile_name, 0.22)),
        minimum_half_width,
        maximum_half_width,
    )
    outer_margin = np.clip(
        expected * float(GRAPH_CORRIDOR_OUTER_RATIO.get(profile_name, 0.24)),
        minimum_half_width,
        maximum_half_width,
    )

    roi_left, roi_top, roi_right, roi_bottom = roi_bounds
    roi_boolean = np.zeros((height, width), dtype=bool)
    roi_boolean[roi_top:roi_bottom, roi_left:roi_right] = True

    corridor_boolean = (
        roi_boolean
        & (radii >= expected - inner_margin)
        & (radii <= expected + outer_margin)
    )
    corridor_mask = corridor_boolean.astype(np.uint8) * 255
    filtered = cv2.bitwise_and(preserved_mask, corridor_mask)

    preserved_count = int(cv2.countNonZero(preserved_mask))
    filtered_count = int(cv2.countNonZero(filtered))
    filtered_retention = filtered_count / max(preserved_count, 1)
    expanded = False

    if (
        filtered_count < GRAPH_CORRIDOR_MIN_FILTERED_PIXELS
        or filtered_retention < GRAPH_CORRIDOR_MIN_FILTERED_RETENTION
    ):
        expanded = True
        expansion = float(GRAPH_CORRIDOR_FALLBACK_EXPANSION)
        expanded_boolean = (
            roi_boolean
            & (radii >= expected - inner_margin * expansion)
            & (radii <= expected + outer_margin * expansion)
        )
        corridor_mask = expanded_boolean.astype(np.uint8) * 255
        filtered = cv2.bitwise_and(preserved_mask, corridor_mask)
        filtered_count = int(cv2.countNonZero(filtered))
        filtered_retention = filtered_count / max(preserved_count, 1)

    # Keep cleaned/guide-supported dots close to the corridor while removing
    # compact filled components that are likely labels or icons.
    support_size = max(3, int(GRAPH_CORRIDOR_SUPPORT_DILATION_PX))
    if support_size % 2 == 0:
        support_size += 1
    guide_support = cv2.dilate(
        cv2.bitwise_and(guide_mask, corridor_mask),
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (support_size, support_size),
        ),
        iterations=1,
    )

    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (filtered > 0).astype(np.uint8),
        connectivity=8,
    )
    pruned = np.zeros_like(filtered)
    kept_components = 0
    removed_components = 0

    for component_id in range(1, component_count):
        component_mask = labels == component_id
        area = int(stats[component_id, cv2.CC_STAT_AREA])
        object_width = int(stats[component_id, cv2.CC_STAT_WIDTH])
        object_height = int(stats[component_id, cv2.CC_STAT_HEIGHT])
        box_area = max(1, object_width * object_height)
        fill_ratio = area / box_area
        thickness = area / max(object_width, object_height, 1)
        touches_guide = bool(np.any(guide_support[component_mask] > 0))

        compact_filled = (
            area >= 35
            and fill_ratio > GRAPH_CORRIDOR_COMPONENT_MAX_FILL_RATIO
            and min(object_width, object_height) >= 7
        )
        overly_thick = (
            thickness > GRAPH_CORRIDOR_COMPONENT_MAX_THICKNESS_PX
            and not touches_guide
        )

        if compact_filled or overly_thick:
            removed_components += 1
            continue

        pruned[component_mask] = 255
        kept_components += 1

    # Never lose the cleaned guide itself inside the corridor.
    pruned = cv2.bitwise_or(
        pruned,
        cv2.bitwise_and(guide_mask, corridor_mask),
    )

    final_count = int(cv2.countNonZero(pruned))
    base_path = os.path.splitext(screenshot_path)[0]
    corridor_path = base_path + '_red_graph_corridor.png'
    filtered_path = base_path + '_red_graph_corridor_filtered.png'
    cv2.imwrite(corridor_path, corridor_mask)
    cv2.imwrite(filtered_path, pruned)

    diagnostics.update({
        'accepted': bool(final_count >= GRAPH_CORRIDOR_MIN_FILTERED_PIXELS),
        'expanded': expanded,
        'preserved_pixels': preserved_count,
        'corridor_pixels_before_component_pruning': filtered_count,
        'filtered_pixels': final_count,
        'filtered_retention_ratio': round(final_count / max(preserved_count, 1), 4),
        'kept_components': int(kept_components),
        'removed_components': int(removed_components),
        'debug_images': {
            'corridor': corridor_path,
            'filtered': filtered_path,
        },
    })

    log(
        '   [Graph Corridor] guide coverage='
        f"{guide_diagnostics.get('coverage', 0.0):.3f}, "
        f'preserved={preserved_count}, filtered={final_count}, '
        f'components={component_count - 1}->{kept_components}, '
        f"expanded={'yes' if expanded else 'no'}"
    )

    if final_count < GRAPH_CORRIDOR_MIN_FILTERED_PIXELS:
        diagnostics['reason'] = 'corridor_removed_too_much_red'
        log(
            '   [Graph Corridor] Filtered mask is too sparse; '
            'falling back to the preserved mask for this zoom'
        )
        return preserved_mask.copy(), corridor_mask, diagnostics

    return pruned, corridor_mask, diagnostics



def _graph_contour_red_support(contour, raw_red_mask, profile_name):
    inverse = cv2.bitwise_not((raw_red_mask > 0).astype(np.uint8) * 255)
    distance_map = cv2.distanceTransform(inverse, cv2.DIST_L2, 5)
    points = np.asarray(contour, np.int32).reshape(-1, 2)
    points[:, 0] = np.clip(points[:, 0], 0, raw_red_mask.shape[1] - 1)
    points[:, 1] = np.clip(points[:, 1], 0, raw_red_mask.shape[0] - 1)
    distances = distance_map[points[:, 1], points[:, 0]]
    close_distance = float(DIRECTIONAL_GRAPH_CLOSE_DISTANCE_PX.get(profile_name, 9.0))
    return {
        'close_distance_px': round(close_distance, 3),
        'close_to_red_ratio': round(float(np.mean(distances <= close_distance)) if len(distances) else 0.0, 4),
        'mean_red_distance_px': round(float(np.mean(distances)) if len(distances) else float('inf'), 3),
        'red_distance_p90_px': round(float(np.percentile(distances, 90)) if len(distances) else float('inf'), 3),
    }


def _directional_boundary_graph_reconstruct(
    red_mask,
    raw_red_mask,
    center,
    roi_bounds,
    image_area,
    minimum_area_ratio,
    maximum_area_ratio,
    screenshot_path,
    guide_mask=None,
    log=print,
):
    diagnostics = {
        'enabled': bool(DIRECTIONAL_GRAPH_ENABLED),
        'accepted': False,
        'method': 'directional_boundary_graph',
        'profile': ACTIVE_LOCALITY_PROFILE,
        'capture_zoom': round(float(ACTIVE_CAPTURE_ZOOM), 3),
        'attempts': [],
    }
    if not DIRECTIONAL_GRAPH_ENABLED:
        diagnostics['reason'] = 'disabled'
        return None, diagnostics

    if guide_mask is None:
        guide_mask = red_mask

    graph_input_mask, corridor_mask, corridor_diagnostics = (
        _build_target_guided_graph_corridor(
            preserved_mask=red_mask,
            guide_mask=guide_mask,
            center=center,
            roi_bounds=roi_bounds,
            profile_name=ACTIVE_LOCALITY_PROFILE,
            screenshot_path=screenshot_path,
            log=log,
        )
    )
    diagnostics['corridor'] = corridor_diagnostics

    # Red-support validation should use only raw red evidence inside the same
    # target-guided corridor. Unrelated red labels outside the corridor cannot
    # make a synthetic bridge appear well supported.
    corridor_raw_red = cv2.bitwise_and(raw_red_mask, corridor_mask)
    if cv2.countNonZero(corridor_raw_red) < 45:
        corridor_raw_red = raw_red_mask

    roi_left, roi_top, roi_right, roi_bottom = roi_bounds
    roi_mask = np.zeros_like(graph_input_mask)
    roi_mask[roi_top:roi_bottom, roi_left:roi_right] = graph_input_mask[
        roi_top:roi_bottom,
        roi_left:roi_right,
    ]

    join_length = int(
        DIRECTIONAL_GRAPH_LOCAL_JOIN_LENGTH.get(ACTIVE_LOCALITY_PROFILE, 9)
    )
    locally_joined = roi_mask.copy()
    for angle in (0.0, 30.0, 60.0, 90.0, 120.0, 150.0):
        locally_joined = cv2.bitwise_or(
            locally_joined,
            cv2.morphologyEx(
                roi_mask,
                cv2.MORPH_CLOSE,
                _directional_line_kernel(join_length, angle),
            ),
        )
    locally_joined = cv2.morphologyEx(
        locally_joined,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )

    skeleton = _morphological_skeleton(locally_joined)
    endpoints, segment_diagnostics = _extract_directional_graph_endpoints(skeleton)
    base_path = os.path.splitext(screenshot_path)[0]
    local_join_path = base_path + '_red_graph_local_join.png'
    skeleton_path = base_path + '_red_graph_skeleton.png'
    cv2.imwrite(local_join_path, locally_joined)
    cv2.imwrite(skeleton_path, skeleton)

    diagnostics.update({
        'local_join_length_px': join_length,
        'segments': segment_diagnostics,
        'debug_images': {
            'local_join': local_join_path,
            'skeleton': skeleton_path,
            **corridor_diagnostics.get('debug_images', {}),
        },
    })

    log(
        f"   [Graph] Fragments={segment_diagnostics['component_count']}, "
        f"endpoints={segment_diagnostics['endpoint_count']}, "
        f'local_join={join_length}px'
    )

    if len(endpoints) < 2:
        diagnostics['reason'] = 'too_few_directional_endpoints'
        return None, diagnostics

    maximum_gap = float(
        DIRECTIONAL_GRAPH_MAX_GAP_PX.get(ACTIVE_LOCALITY_PROFILE, 145.0)
    )
    bridge_candidates = _directional_bridge_candidates(
        endpoints,
        center,
        maximum_gap,
    )
    diagnostics['bridge_candidate_count'] = len(bridge_candidates)
    if not bridge_candidates:
        diagnostics['reason'] = 'no_directionally_compatible_endpoint_pairs'
        return None, diagnostics

    gap_levels = DIRECTIONAL_GRAPH_GAP_LEVELS.get(
        ACTIVE_LOCALITY_PROFILE,
        [40.0, 70.0, 105.0, 145.0],
    )
    minimum_close_ratio = float(
        DIRECTIONAL_GRAPH_MIN_RED_CLOSE_RATIO.get(
            ACTIVE_LOCALITY_PROFILE,
            0.84,
        )
    )
    maximum_mean_distance = float(
        DIRECTIONAL_GRAPH_MAX_MEAN_RED_DISTANCE_PX.get(
            ACTIVE_LOCALITY_PROFILE,
            7.0,
        )
    )
    edge_margin = max(4, int(round(min(red_mask.shape[:2]) * 0.006)))

    best_contour = None
    best_diagnostics = None
    best_score = -float('inf')
    total_variants = 0

    for gap_level in gap_levels:
        bridge_variants = _directional_bridge_sets(
            bridge_candidates,
            float(gap_level),
        )
        total_variants += len(bridge_variants)

        if not bridge_variants:
            diagnostics['attempts'].append({
                'gap_limit_px': float(gap_level),
                'bridge_count': 0,
                'status': 'no_bridge_variants',
            })
            continue

        log(
            '   [Graph Paths] gap_limit='
            f'{float(gap_level):.0f}px, variants={len(bridge_variants)}'
        )

        for variant_index, variant in enumerate(bridge_variants):
            bridges = variant['bridges']
            bridged = locally_joined.copy()
            for bridge in bridges:
                cv2.line(
                    bridged,
                    bridge['start'],
                    bridge['end'],
                    255,
                    DIRECTIONAL_GRAPH_BRIDGE_THICKNESS_PX,
                    cv2.LINE_AA,
                )
            bridged = cv2.morphologyEx(
                bridged,
                cv2.MORPH_CLOSE,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
            )
            bridged = cv2.dilate(
                bridged,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            )

            attempt = {
                'gap_limit_px': float(gap_level),
                'variant_index': int(variant_index),
                'strategy': variant['strategy'],
                'matching_score': variant['matching_score'],
                'bridge_count': len(bridges),
            }

            if bridged[center[1], center[0]] > 0:
                attempt['status'] = 'bridge_crossed_target_pixel'
                diagnostics['attempts'].append(attempt)
                continue

            flood_image = cv2.bitwise_not(bridged)
            flood_mask = np.zeros(
                (bridged.shape[0] + 2, bridged.shape[1] + 2),
                np.uint8,
            )
            cv2.floodFill(
                flood_image,
                flood_mask,
                center,
                128,
                loDiff=0,
                upDiff=0,
                flags=4,
            )
            enclosed = (flood_image == 128).astype(np.uint8) * 255
            border_pixels = int(
                cv2.countNonZero(enclosed[0, :])
                + cv2.countNonZero(enclosed[-1, :])
                + cv2.countNonZero(enclosed[:, 0])
                + cv2.countNonZero(enclosed[:, -1])
            )
            region_ratio = cv2.countNonZero(enclosed) / max(image_area, 1)
            bridge_length = sum(
                float(bridge['distance_px'])
                for bridge in bridges
            )
            attempt.update({
                'bridge_total_length_px': round(bridge_length, 3),
                'border_pixels': border_pixels,
                'region_area_ratio': round(region_ratio, 6),
            })

            if border_pixels:
                attempt['status'] = 'loop_not_closed'
                diagnostics['attempts'].append(attempt)
                continue
            if not minimum_area_ratio <= region_ratio <= maximum_area_ratio:
                attempt['status'] = 'area_ratio_outside_limits'
                diagnostics['attempts'].append(attempt)
                continue

            contours, _ = cv2.findContours(
                enclosed,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_NONE,
            )
            contours = [
                contour
                for contour in contours
                if cv2.pointPolygonTest(contour, center, False) >= 0
            ]
            if not contours:
                attempt['status'] = 'no_target_containing_contour'
                diagnostics['attempts'].append(attempt)
                continue

            contour = max(contours, key=cv2.contourArea)
            x, y, box_width, box_height = cv2.boundingRect(contour)
            inside_roi = (
                x >= roi_left - edge_margin
                and y >= roi_top - edge_margin
                and x + box_width <= roi_right + edge_margin
                and y + box_height <= roi_bottom + edge_margin
            )
            if not inside_roi:
                attempt['status'] = 'contour_outside_roi'
                diagnostics['attempts'].append(attempt)
                continue

            perimeter = float(cv2.arcLength(contour, True))
            bridge_fraction = bridge_length / max(perimeter, 1.0)
            red_support = _graph_contour_red_support(
                contour,
                corridor_raw_red,
                ACTIVE_LOCALITY_PROFILE,
            )
            shortcut = _inner_shortcut_diagnostics(contour, center)

            # findContours returns a simple external boundary, but keep the
            # crossing guard on a lightly simplified copy so multi-path search
            # does not perform an O(n^2) check on thousands of raster points.
            crossing_contour = cv2.approxPolyDP(
                contour,
                max(1.0, float(cv2.arcLength(contour, True)) * 0.0012),
                True,
            )
            crossings = _count_closed_polyline_crossings(
                np.asarray(crossing_contour, np.float32).reshape(-1, 2)
            )
            checks = {
                'inside_roi': inside_roi,
                'contains_target': True,
                'close_to_red': (
                    red_support['close_to_red_ratio'] >= minimum_close_ratio
                ),
                'mean_red_distance': (
                    red_support['mean_red_distance_px'] <= maximum_mean_distance
                ),
                'bridge_fraction': (
                    bridge_fraction <= DIRECTIONAL_GRAPH_MAX_BRIDGE_FRACTION
                ),
                'no_self_crossing': crossings == 0,
                'no_inner_shortcut': not shortcut.get('suspected', False),
            }
            attempt.update({
                'status': (
                    'candidate'
                    if all(checks.values())
                    else 'candidate_failed_safety'
                ),
                'bridge_fraction': round(bridge_fraction, 4),
                'red_support': red_support,
                'self_crossings': crossings,
                'shortcut': shortcut,
                'checks': checks,
                'bridges': bridges,
            })
            diagnostics['attempts'].append(attempt)

            if not all(checks.values()):
                continue

            score = (
                red_support['close_to_red_ratio'] * 3.2
                - red_support['mean_red_distance_px'] * 0.06
                - bridge_fraction * 1.25
                - len(bridges) * 0.012
                + float(variant['matching_score']) * 0.01
            )
            if score <= best_score:
                continue

            best_score = score
            best_contour = contour
            best_diagnostics = {
                'method': 'directional_boundary_graph',
                'accepted': True,
                'area_ratio': round(region_ratio, 4),
                'coverage': round(red_support['close_to_red_ratio'], 3),
                'close_to_red_ratio': round(
                    red_support['close_to_red_ratio'],
                    3,
                ),
                'mean_red_distance_px': round(
                    red_support['mean_red_distance_px'],
                    2,
                ),
                'inside_roi': True,
                'contains_target': True,
                'requires_cross_zoom_confirmation': True,
                'provisional_reason': 'directional_graph_reconstruction',
                'adaptive_profile': ACTIVE_LOCALITY_PROFILE,
                'minimum_area_ratio': round(float(minimum_area_ratio), 6),
                'maximum_area_ratio': round(float(maximum_area_ratio), 6),
                'raw_points': len(contour),
                'corridor': corridor_diagnostics,
                'graph_validation': {
                    'accepted': True,
                    'gap_limit_px': float(gap_level),
                    'variant_index': int(variant_index),
                    'path_strategy': variant['strategy'],
                    'matching_score': variant['matching_score'],
                    'bridge_count': len(bridges),
                    'bridge_total_length_px': round(bridge_length, 3),
                    'bridge_fraction': round(bridge_fraction, 4),
                    'red_support': red_support,
                    'self_crossings': crossings,
                    'shortcut': shortcut,
                    'checks': checks,
                    'selected_bridges': bridges,
                },
            }

            # Keep debug images for the strongest candidate only.
            bridged_path = base_path + '_red_graph_bridged.png'
            selected_path = base_path + '_red_graph_selected.png'
            cv2.imwrite(bridged_path, bridged)
            selected_debug = cv2.cvtColor(corridor_raw_red, cv2.COLOR_GRAY2BGR)
            cv2.drawContours(
                selected_debug,
                [contour],
                -1,
                (0, 255, 0),
                2,
            )
            for bridge in bridges:
                cv2.line(
                    selected_debug,
                    bridge['start'],
                    bridge['end'],
                    (255, 255, 0),
                    2,
                    cv2.LINE_AA,
                )
            cv2.circle(selected_debug, center, 5, (255, 0, 255), -1)
            cv2.imwrite(selected_path, selected_debug)
            best_diagnostics['graph_validation']['debug_images'] = {
                'bridged_mask': bridged_path,
                'selected_contour': selected_path,
            }

    diagnostics['path_variant_count'] = int(total_variants)
    if best_contour is None:
        diagnostics['reason'] = 'no_safe_closed_directional_graph_loop'
        log(
            '   [Graph] No safe closed loop survived the '
            f'target-guided multi-path search ({total_variants} variants)'
        )
        return None, diagnostics

    diagnostics.update(best_diagnostics)
    diagnostics['score'] = round(float(best_score), 5)
    log(
        '   [Graph] Accepted provisional closed loop: '
        f"bridges={diagnostics['graph_validation']['bridge_count']}, "
        f"path={diagnostics['graph_validation']['path_strategy']}, "
        f"gap_limit={diagnostics['graph_validation']['gap_limit_px']:.0f}px, "
        f"red_match={diagnostics['close_to_red_ratio']:.3f}, "
        f"mean_distance={diagnostics['mean_red_distance_px']:.2f}px. "
        'Another zoom must confirm it.'
    )
    return best_contour, diagnostics



def _graph_candidate_is_safe(diagnostics: dict) -> bool:
    if not isinstance(diagnostics, dict) or diagnostics.get('provisional_reason') != 'directional_graph_reconstruction':
        return False
    validation = diagnostics.get('graph_validation', {})
    checks = validation.get('checks', {}) if isinstance(validation, dict) else {}
    return bool(validation.get('accepted', False) and checks and all(bool(value) for value in checks.values()) and int(validation.get('self_crossings', 1) or 0) == 0 and not bool(validation.get('shortcut', {}).get('suspected', False)))


def _candidate_is_graph_provisional(candidate: dict) -> bool:
    if not isinstance(candidate, dict):
        return False
    diagnostics = candidate.get('diagnostics', {})
    return bool(candidate.get('graph_provisional', False) or (isinstance(diagnostics, dict) and diagnostics.get('requires_cross_zoom_confirmation', False) and diagnostics.get('provisional_reason') == 'directional_graph_reconstruction'))




def _strict_google_red_mask(image: np.ndarray) -> np.ndarray:
    if image is None or image.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)

    image_float = image.astype(np.float32)
    target_bgr = np.asarray([53.0, 67.0, 234.0], dtype=np.float32)
    difference = image_float - target_bgr
    distance_squared = np.sum(difference * difference, axis=2)

    blue = image_float[:, :, 0]
    green = image_float[:, :, 1]
    red = image_float[:, :, 2]

    strict = (
        (distance_squared <= 68.0 ** 2)
        & (red >= 160.0)
        & ((red - green) >= 58.0)
        & ((red - blue) >= 58.0)
        & (green <= 155.0)
        & (blue <= 155.0)
    )

    if ACTIVE_SATELLITE_VIEW:
        hsv = cv2.cvtColor(
            image,
            cv2.COLOR_BGR2HSV,
        )

        hue = hsv[:, :, 0]
        saturation = hsv[:, :, 1]
        value = hsv[:, :, 2]

        satellite_red = (
            (red >= SATELLITE_RED_MIN_RED)
            & ((red - green) >= SATELLITE_RED_MIN_RED_MINUS_GREEN)
            & ((red - blue) >= SATELLITE_RED_MIN_RED_MINUS_BLUE)
            & (saturation >= SATELLITE_RED_MIN_SATURATION)
            & (value >= SATELLITE_RED_MIN_VALUE)
        )

        satellite_yellow = (
            (red >= SATELLITE_YELLOW_MIN_RED)
            & (green >= SATELLITE_YELLOW_MIN_GREEN)
            & (blue <= SATELLITE_YELLOW_MAX_BLUE)
            & (hue >= SATELLITE_YELLOW_MIN_HUE)
            & (hue <= SATELLITE_YELLOW_MAX_HUE)
            & (saturation >= SATELLITE_YELLOW_MIN_SATURATION)
            & (value >= SATELLITE_YELLOW_MIN_VALUE)
        )

        strict = (
            strict
            | satellite_red
            | satellite_yellow
        )

    return strict.astype(np.uint8) * 255


def _build_graph_preservation_mask(
    raw_red_mask: np.ndarray,
    cleaned_red_mask: np.ndarray,
    strict_red_mask: np.ndarray,
    center: tuple[int, int],
    roi_bounds: tuple[int, int, int, int],
    profile_name: str,
    image_width: int,
    image_height: int,
) -> tuple[np.ndarray, dict]:
    raw_count = int(cv2.countNonZero(raw_red_mask))
    cleaned_count = int(cv2.countNonZero(cleaned_red_mask))
    cleaned_retention = cleaned_count / max(raw_count, 1)

    if not RED_PRESERVATION_ENABLED:
        return cleaned_red_mask.copy(), {
            "enabled": False,
            "raw_pixels": raw_count,
            "cleaned_pixels": cleaned_count,
            "cleaned_retention_ratio": round(cleaned_retention, 4),
        }

    roi_left, roi_top, roi_right, roi_bottom = roi_bounds
    roi_mask = np.zeros_like(raw_red_mask)
    roi_mask[roi_top:roi_bottom, roi_left:roi_right] = 255

    raw_roi = cv2.bitwise_and(raw_red_mask, roi_mask)
    strict_roi = cv2.bitwise_and(strict_red_mask, roi_mask)

    roi_half_width = max(1, (roi_right - roi_left) // 2)
    roi_half_height = max(1, (roi_bottom - roi_top) // 2)
    minimum_radius, maximum_radius = _adaptive_radius_limits(
        profile_name,
        image_width,
        image_height,
        roi_half_width,
        roi_half_height,
    )

    roi_width_ratio = (
        roi_right
        - roi_left
    ) / max(
        image_width,
        1,
    )

    roi_height_ratio = (
        roi_bottom
        - roi_top
    ) / max(
        image_height,
        1,
    )

    full_frame_radius_expanded = False

    if (
        FULL_FRAME_BOOTSTRAP_ENABLED
        and FULL_FRAME_BOOTSTRAP_RADIUS_EXPANSION_ENABLED
        and (
            roi_width_ratio
            >= 0.80
            or roi_height_ratio
            >= 0.80
        )
    ):
        roi_corner_radius = max(
            float(
                np.hypot(
                    float(
                        corner_x
                        - center[
                            0
                        ]
                    ),
                    float(
                        corner_y
                        - center[
                            1
                        ]
                    ),
                )
            )
            for corner_x, corner_y
            in (
                (
                    roi_left,
                    roi_top,
                ),
                (
                    max(
                        roi_left,
                        roi_right - 1,
                    ),
                    roi_top,
                ),
                (
                    roi_left,
                    max(
                        roi_top,
                        roi_bottom - 1,
                    ),
                ),
                (
                    max(
                        roi_left,
                        roi_right - 1,
                    ),
                    max(
                        roi_top,
                        roi_bottom - 1,
                    ),
                ),
            )
        )

        maximum_radius = max(
            float(
                maximum_radius
            ),
            roi_corner_radius
            * FULL_FRAME_BOOTSTRAP_RADIUS_MARGIN,
        )

        minimum_radius = min(
            float(
                minimum_radius
            ),
            max(
                7.0,
                min(
                    image_width,
                    image_height,
                )
                * 0.012,
            ),
        )

        full_frame_radius_expanded = True

    y_grid, x_grid = np.ogrid[:image_height, :image_width]
    radial_distance = np.hypot(
        x_grid.astype(np.float32) - float(center[0]),
        y_grid.astype(np.float32) - float(center[1]),
    )
    annulus = (
        (radial_distance >= float(minimum_radius) * 0.45)
        & (radial_distance <= float(maximum_radius) * 1.10)
    ).astype(np.uint8) * 255

    candidate_raw = cv2.bitwise_and(raw_roi, annulus)
    component_count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        (candidate_raw > 0).astype(np.uint8),
        connectivity=8,
    )

    component_ids = list(range(1, component_count))
    if component_ids:
        centroid_array = np.asarray(
            [centroids[component_id] for component_id in component_ids],
            dtype=np.float32,
        )
        differences = centroid_array[:, None, :] - centroid_array[None, :, :]
        distances = np.sqrt(np.sum(differences * differences, axis=2))
        neighbor_counts = np.sum(
            (distances <= RED_PRESERVATION_NEIGHBOR_DISTANCE_PX)
            & (distances > 0.0),
            axis=1,
        )
    else:
        neighbor_counts = np.zeros((0,), dtype=np.int32)

    support_size = max(3, int(RED_PRESERVATION_CLEAN_SUPPORT_DILATION_PX))
    if support_size % 2 == 0:
        support_size += 1
    clean_support = cv2.dilate(
        cv2.bitwise_or(cleaned_red_mask, strict_roi),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (support_size, support_size)),
        iterations=1,
    )

    restored_components = np.zeros_like(raw_red_mask)
    restored_component_count = 0

    for index, component_id in enumerate(component_ids):
        object_x = int(stats[component_id, cv2.CC_STAT_LEFT])
        object_y = int(stats[component_id, cv2.CC_STAT_TOP])
        object_width = int(stats[component_id, cv2.CC_STAT_WIDTH])
        object_height = int(stats[component_id, cv2.CC_STAT_HEIGHT])
        object_area = int(stats[component_id, cv2.CC_STAT_AREA])

        if not (
            RED_PRESERVATION_MIN_COMPONENT_AREA
            <= object_area
            <= RED_PRESERVATION_MAX_COMPONENT_AREA
        ):
            continue

        box_area = max(1, object_width * object_height)
        fill_ratio = object_area / box_area
        aspect = object_width / max(object_height, 1)
        thinness = object_area / max(object_width, object_height, 1)

        component_mask = labels == component_id
        touches_support = bool(np.any(clean_support[component_mask] > 0))
        has_neighbor = int(neighbor_counts[index]) > 0

        compact_icon = (
            12 <= object_width <= 55
            and 12 <= object_height <= 55
            and object_area >= 80
            and fill_ratio >= 0.52
            and not touches_support
        )
        likely_text = (
            object_width >= 25
            and object_height <= 45
            and aspect >= 1.65
            and object_area >= 100
            and fill_ratio >= 0.25
            and not touches_support
        )
        boundary_like = (
            object_area <= 45
            or min(object_width, object_height) <= 13
            or thinness <= 7.5
        )

        if compact_icon or likely_text:
            continue
        if not boundary_like:
            continue
        if not (touches_support or has_neighbor):
            continue

        restored_components[component_mask] = 255
        restored_component_count += 1

    graph_mask = cv2.bitwise_or(cleaned_red_mask, strict_roi)
    graph_mask = cv2.bitwise_or(graph_mask, restored_components)
    graph_mask = cv2.bitwise_and(graph_mask, roi_mask)

    graph_count = int(cv2.countNonZero(graph_mask))
    graph_ratio = graph_count / max(raw_count, 1)

    if graph_ratio > RED_PRESERVATION_MAX_RESTORED_RATIO:
        restrained_support = cv2.dilate(
            cv2.bitwise_or(cleaned_red_mask, strict_roi),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)),
            iterations=1,
        )
        graph_mask = cv2.bitwise_or(
            cleaned_red_mask,
            cv2.bitwise_and(graph_mask, restrained_support),
        )
        graph_count = int(cv2.countNonZero(graph_mask))
        graph_ratio = graph_count / max(raw_count, 1)

    diagnostics = {
        "enabled": True,
        "triggered": bool(cleaned_retention < RED_PRESERVATION_TRIGGER_RATIO),
        "raw_pixels": raw_count,
        "cleaned_pixels": cleaned_count,
        "strict_pixels_in_roi": int(cv2.countNonZero(strict_roi)),
        "graph_pixels": graph_count,
        "restored_pixels": max(0, graph_count - cleaned_count),
        "restored_component_count": int(restored_component_count),
        "cleaned_retention_ratio": round(cleaned_retention, 4),
        "graph_retention_ratio": round(graph_ratio, 4),
        "minimum_radius_px": round(float(minimum_radius), 3),
        "maximum_radius_px": round(float(maximum_radius), 3),
        "full_frame_radius_expanded": bool(
            full_frame_radius_expanded
        ),
        "roi_width_ratio": round(
            float(
                roi_width_ratio
            ),
            4,
        ),
        "roi_height_ratio": round(
            float(
                roi_height_ratio
            ),
            4,
        ),
    }

    return graph_mask, diagnostics



def _local_gap_profile_value(values: dict, profile_name: str, default):
    return values.get(
        profile_name,
        values.get("medium", default),
    )


def _closed_contour_step_lengths(points_xy: np.ndarray) -> np.ndarray:
    points = np.asarray(points_xy, dtype=np.float32).reshape(-1, 2)
    if len(points) < 2:
        return np.zeros(0, dtype=np.float32)
    next_points = np.roll(points, -1, axis=0)
    return np.hypot(
        points[:, 0] - next_points[:, 0],
        points[:, 1] - next_points[:, 1],
    )


def _simplify_open_pixel_path(path_xy: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(path_xy) <= 2:
        return list(path_xy)
    path_array = np.asarray(path_xy, dtype=np.int32).reshape(-1, 1, 2)
    simplified = cv2.approxPolyDP(path_array, 1.15, False).reshape(-1, 2)
    output = [tuple(map(int, point)) for point in simplified]
    if output[0] != tuple(path_xy[0]):
        output.insert(0, tuple(path_xy[0]))
    if output[-1] != tuple(path_xy[-1]):
        output.append(tuple(path_xy[-1]))

    # Preserve a safe maximum local step after Douglas-Peucker simplification.
    densified = [output[0]]
    for segment_index in range(len(output) - 1):
        start = np.asarray(output[segment_index], dtype=np.float64)
        end = np.asarray(output[segment_index + 1], dtype=np.float64)
        distance = float(np.linalg.norm(end - start))
        steps = max(1, int(math.ceil(distance / 10.0)))
        for step_index in range(1, steps + 1):
            point = start + (step_index / steps) * (end - start)
            candidate = tuple(np.round(point).astype(int))
            if candidate != densified[-1]:
                densified.append(candidate)
    return densified


def _build_satellite_label_occlusion_mask(
    image: np.ndarray,
    boundary_mask: np.ndarray,
    roi_bounds: tuple[int, int, int, int],
    removed_noise_mask: Optional[np.ndarray] = None,
    screenshot_path: str = "",
    log=print,
) -> tuple[np.ndarray, dict]:
    diagnostics = {
        "enabled": bool(
            SATELLITE_LABEL_OCCLUSION_REPAIR_ENABLED
            and ACTIVE_SATELLITE_VIEW
        ),
        "pixel_count": 0,
    }

    if (
        not diagnostics["enabled"]
        or image is None
        or image.size == 0
    ):
        return (
            np.zeros_like(boundary_mask),
            diagnostics,
        )

    hsv = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2HSV,
    )

    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]

    # White/grey road names and place labels.
    neutral_seed = (
        (value >= 145)
        & (saturation <= 115)
    ).astype(np.uint8) * 255

    # Coloured POI markers (hotel, restaurant, hospital, etc.). Exclude the
    # selected boundary itself before component filtering.
    boundary_exclusion = cv2.dilate(
        (boundary_mask > 0).astype(np.uint8) * 255,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (5, 5),
        ),
        iterations=1,
    )

    coloured_seed = (
        (value >= 165)
        & (saturation > 115)
    ).astype(np.uint8) * 255

    coloured_seed = cv2.bitwise_and(
        coloured_seed,
        cv2.bitwise_not(boundary_exclusion),
    )

    horizontal_text = cv2.morphologyEx(
        neutral_seed,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (19, 3),
        ),
        iterations=1,
    )

    vertical_text = cv2.morphologyEx(
        neutral_seed,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (3, 9),
        ),
        iterations=1,
    )

    neutral_probe = cv2.bitwise_or(
        horizontal_text,
        vertical_text,
    )

    occlusion_mask = np.zeros_like(
        boundary_mask
    )

    (
        neutral_count,
        neutral_labels,
        neutral_stats,
        _,
    ) = cv2.connectedComponentsWithStats(
        (neutral_probe > 0).astype(np.uint8),
        connectivity=8,
    )

    neutral_components = 0

    for component_id in range(1, neutral_count):
        object_width = int(
            neutral_stats[component_id, cv2.CC_STAT_WIDTH]
        )
        object_height = int(
            neutral_stats[component_id, cv2.CC_STAT_HEIGHT]
        )
        object_area = int(
            neutral_stats[component_id, cv2.CC_STAT_AREA]
        )
        fill_ratio = object_area / max(
            object_width * object_height,
            1,
        )

        text_like = bool(
            3 <= object_width <= 340
            and 3 <= object_height <= 90
            and object_area >= 6
            and fill_ratio >= 0.025
        )

        compact_label = bool(
            5 <= object_width <= 80
            and 5 <= object_height <= 80
            and object_area >= 12
        )

        if not (text_like or compact_label):
            continue

        occlusion_mask[neutral_labels == component_id] = 255
        neutral_components += 1

    coloured_probe = cv2.morphologyEx(
        coloured_seed,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (5, 5),
        ),
        iterations=1,
    )

    (
        colour_count,
        colour_labels,
        colour_stats,
        _,
    ) = cv2.connectedComponentsWithStats(
        (coloured_probe > 0).astype(np.uint8),
        connectivity=8,
    )

    coloured_components = 0

    for component_id in range(1, colour_count):
        object_width = int(
            colour_stats[component_id, cv2.CC_STAT_WIDTH]
        )
        object_height = int(
            colour_stats[component_id, cv2.CC_STAT_HEIGHT]
        )
        object_area = int(
            colour_stats[component_id, cv2.CC_STAT_AREA]
        )
        fill_ratio = object_area / max(
            object_width * object_height,
            1,
        )

        compact_overlay = bool(
            5 <= object_width <= 95
            and 5 <= object_height <= 95
            and 12 <= object_area <= 5000
            and fill_ratio >= 0.08
        )

        if not compact_overlay:
            continue

        occlusion_mask[colour_labels == component_id] = 255
        coloured_components += 1

    if (
        removed_noise_mask is not None
        and removed_noise_mask.shape == occlusion_mask.shape
    ):
        occlusion_mask = cv2.bitwise_or(
            occlusion_mask,
            removed_noise_mask,
        )

    # Include the label's dark halo and the full icon rectangle around its
    # bright seed. This is where the selected line is visually overwritten.
    occlusion_mask = cv2.dilate(
        occlusion_mask,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (13, 11),
        ),
        iterations=1,
    )

    roi_left, roi_top, roi_right, roi_bottom = roi_bounds
    roi_mask = np.zeros_like(occlusion_mask)
    roi_mask[
        max(0, int(roi_top)):min(occlusion_mask.shape[0], int(roi_bottom)),
        max(0, int(roi_left)):min(occlusion_mask.shape[1], int(roi_right)),
    ] = 255

    occlusion_mask = cv2.bitwise_and(
        occlusion_mask,
        roi_mask,
    )

    debug_path = ""

    if screenshot_path:
        debug_path = (
            os.path.splitext(screenshot_path)[0]
            + "_satellite_label_occlusion_mask.png"
        )

        cv2.imwrite(
            debug_path,
            occlusion_mask,
        )

    diagnostics.update({
        "pixel_count": int(
            cv2.countNonZero(occlusion_mask)
        ),
        "neutral_components": int(neutral_components),
        "coloured_components": int(coloured_components),
        "debug_image": debug_path,
    })

    log(
        "   [Satellite Occlusion] Label/icon mask: "
        f"pixels={diagnostics['pixel_count']}, "
        f"neutral={neutral_components}, "
        f"coloured={coloured_components}"
    )

    return occlusion_mask, diagnostics


def _gap_endpoint_direction_diagnostics(
    completed_points: np.ndarray,
    gap_start_index: int,
    center: tuple[int, int],
) -> dict:
    points = np.asarray(
        completed_points,
        dtype=np.float64,
    ).reshape(-1, 2)

    point_count = len(points)

    if point_count < 8:
        return {
            "safe_for_label_occlusion": False,
            "reason": "too_few_contour_points",
        }

    gap_start_index = int(gap_start_index) % point_count
    gap_end_index = (gap_start_index + 1) % point_count
    tangent_span = max(2, min(6, point_count // 24))

    start = points[gap_start_index]
    end = points[gap_end_index]
    previous = points[(gap_start_index - tangent_span) % point_count]
    following = points[(gap_end_index + tangent_span) % point_count]

    direct_vector = end - start
    direct_distance = float(np.linalg.norm(direct_vector))

    if direct_distance <= 1.0:
        return {
            "safe_for_label_occlusion": False,
            "reason": "gap_too_small",
            "direct_distance_px": direct_distance,
        }

    direct_unit = direct_vector / direct_distance
    start_tangent = start - previous
    end_tangent = end - following

    start_norm = float(np.linalg.norm(start_tangent))
    end_norm = float(np.linalg.norm(end_tangent))

    if start_norm <= 1e-6 or end_norm <= 1e-6:
        return {
            "safe_for_label_occlusion": False,
            "reason": "endpoint_tangent_unavailable",
            "direct_distance_px": direct_distance,
        }

    start_tangent /= start_norm
    end_tangent /= end_norm

    start_direction_cos = float(
        np.dot(start_tangent, direct_unit)
    )
    end_direction_cos = float(
        np.dot(end_tangent, -direct_unit)
    )

    center_point = np.asarray(center, dtype=np.float64)
    start_radius = float(np.linalg.norm(start - center_point))
    end_radius = float(np.linalg.norm(end - center_point))
    midpoint_radius = float(
        np.linalg.norm((start + end) * 0.5 - center_point)
    )
    radial_difference = abs(start_radius - end_radius)
    radial_limit = max(
        SATELLITE_LABEL_OCCLUSION_MAX_RADIAL_DIFFERENCE_PX,
        direct_distance
        * SATELLITE_LABEL_OCCLUSION_MAX_RADIAL_DIFFERENCE_RATIO,
    )

    direction_safe = bool(
        start_direction_cos
        >= SATELLITE_LABEL_OCCLUSION_MIN_ENDPOINT_DIRECTION_COS
        and end_direction_cos
        >= SATELLITE_LABEL_OCCLUSION_MIN_ENDPOINT_DIRECTION_COS
        and (
            start_direction_cos
            + end_direction_cos
        )
        >= SATELLITE_LABEL_OCCLUSION_MIN_DIRECTION_SUM
    )

    radial_safe = bool(
        radial_difference <= radial_limit
        and midpoint_radius
        >= min(start_radius, end_radius)
        * SATELLITE_LABEL_OCCLUSION_MIN_PATH_RADIUS_RATIO
    )

    return {
        "safe_for_label_occlusion": bool(
            direction_safe and radial_safe
        ),
        "direct_distance_px": round(direct_distance, 3),
        "start_direction_cos": round(start_direction_cos, 4),
        "end_direction_cos": round(end_direction_cos, 4),
        "direction_sum": round(
            start_direction_cos + end_direction_cos,
            4,
        ),
        "start_radius_px": round(start_radius, 3),
        "end_radius_px": round(end_radius, 3),
        "midpoint_radius_px": round(midpoint_radius, 3),
        "radial_difference_px": round(radial_difference, 3),
        "radial_limit_px": round(radial_limit, 3),
        "direction_safe": direction_safe,
        "radial_safe": radial_safe,
    }


def _local_gap_selected_repair_is_safe(record: dict) -> bool:
    if not isinstance(record, dict):
        return False

    support = record.get("red_support", {})

    if not isinstance(support, dict):
        return False

    support_mode = record.get("support_mode", "red")

    if support_mode == "red":
        return bool(
            support.get("support_ratio", 0.0)
            >= LOCAL_GAP_PATH_MIN_RED_SUPPORT_RATIO
            and support.get("mean_distance_px", float("inf"))
            <= LOCAL_GAP_PATH_MAX_PATH_MEAN_RED_DISTANCE_PX
        )

    endpoint_direction = record.get(
        "endpoint_direction",
        {},
    )

    return bool(
        support_mode == "label_occlusion"
        and ACTIVE_SATELLITE_VIEW
        and endpoint_direction.get(
            "safe_for_label_occlusion",
            False,
        )
        and support.get("occlusion_only_ratio", 0.0)
        >= SATELLITE_LABEL_OCCLUSION_MIN_RATIO
        and support.get("combined_support_ratio", 0.0)
        >= SATELLITE_LABEL_OCCLUSION_MIN_COMBINED_SUPPORT_RATIO
        and support.get(
            "combined_mean_distance_px",
            float("inf"),
        )
        <= SATELLITE_LABEL_OCCLUSION_MAX_COMBINED_MEAN_DISTANCE_PX
        and record.get("length_ratio", float("inf"))
        <= SATELLITE_LABEL_OCCLUSION_MAX_PATH_LENGTH_RATIO
    )


def _astar_local_gap_path(
    start_xy: tuple[int, int],
    end_xy: tuple[int, int],
    completed_points: np.ndarray,
    gap_start_index: int,
    evidence_mask: np.ndarray,
    cleaned_red_mask: np.ndarray,
    center: tuple[int, int],
    roi_bounds: tuple[int, int, int, int],
    search_margin_px: int,
    variant: dict,
    occlusion_mask: Optional[np.ndarray] = None,
) -> tuple[Optional[list[tuple[int, int]]], dict]:
    height, width = evidence_mask.shape[:2]
    start = np.asarray(start_xy, dtype=np.float64)
    end = np.asarray(end_xy, dtype=np.float64)
    direct_distance = float(np.linalg.norm(end - start))
    if direct_distance < 2.0:
        return None, {"status": "gap_too_small"}

    margin = max(int(search_margin_px), int(round(direct_distance * 0.45)))
    min_x = max(0, int(math.floor(min(start[0], end[0]) - margin)))
    max_x = min(width - 1, int(math.ceil(max(start[0], end[0]) + margin)))
    min_y = max(0, int(math.floor(min(start[1], end[1]) - margin)))
    max_y = min(height - 1, int(math.ceil(max(start[1], end[1]) + margin)))

    roi_left, roi_top, roi_right, roi_bottom = roi_bounds
    min_x = max(min_x, roi_left)
    max_x = min(max_x, roi_right - 1)
    min_y = max(min_y, roi_top)
    max_y = min(max_y, roi_bottom - 1)

    if min_x >= max_x or min_y >= max_y:
        return None, {"status": "empty_search_window"}

    crop_evidence = evidence_mask[min_y:max_y + 1, min_x:max_x + 1]
    crop_cleaned = cleaned_red_mask[min_y:max_y + 1, min_x:max_x + 1]

    if (
        occlusion_mask is not None
        and occlusion_mask.shape == evidence_mask.shape
    ):
        crop_occlusion = occlusion_mask[
            min_y:max_y + 1,
            min_x:max_x + 1,
        ]
    else:
        crop_occlusion = np.zeros_like(crop_evidence)

    crop_height, crop_width = crop_evidence.shape[:2]

    start_local = (int(start_xy[0] - min_x), int(start_xy[1] - min_y))
    end_local = (int(end_xy[0] - min_x), int(end_xy[1] - min_y))

    allowed = np.ones((crop_height, crop_width), dtype=np.uint8)
    yy, xx = np.indices((crop_height, crop_width), dtype=np.float32)
    global_x = xx + float(min_x)
    global_y = yy + float(min_y)

    radius_grid = np.hypot(global_x - float(center[0]), global_y - float(center[1]))
    start_radius = float(np.hypot(start[0] - center[0], start[1] - center[1]))
    end_radius = float(np.hypot(end[0] - center[0], end[1] - center[1]))
    minimum_endpoint_radius = min(start_radius, end_radius)
    maximum_endpoint_radius = max(start_radius, end_radius)
    minimum_allowed_radius = max(10.0, minimum_endpoint_radius * 0.62)
    maximum_allowed_radius = maximum_endpoint_radius * 1.38 + 32.0
    allowed[(radius_grid < minimum_allowed_radius) | (radius_grid > maximum_allowed_radius)] = 0

    # Keep the search local to the rough polar contour, while allowing enough
    # room for a curved path around the missing red section.
    contour_mask = np.zeros((height, width), dtype=np.uint8)
    cv2.polylines(
        contour_mask,
        [np.asarray(completed_points, dtype=np.int32).reshape(-1, 1, 2)],
        True,
        255,
        max(42, int(round(direct_distance * 0.62))),
        cv2.LINE_AA,
    )
    local_corridor = contour_mask[min_y:max_y + 1, min_x:max_x + 1]
    allowed[local_corridor == 0] = 0

    # Prevent the repair path from crossing already trusted contour sections.
    trusted_mask = np.zeros((height, width), dtype=np.uint8)
    trusted_points = np.asarray(completed_points, dtype=np.int32).reshape(-1, 2)
    if len(trusted_points) >= 3:
        for index in range(len(trusted_points)):
            if index == gap_start_index:
                continue
            p1 = tuple(map(int, trusted_points[index]))
            p2 = tuple(map(int, trusted_points[(index + 1) % len(trusted_points)]))
            cv2.line(trusted_mask, p1, p2, 255, 5, cv2.LINE_AA)
    cv2.circle(trusted_mask, tuple(map(int, start_xy)), 16, 0, -1)
    cv2.circle(trusted_mask, tuple(map(int, end_xy)), 16, 0, -1)
    local_trusted = trusted_mask[min_y:max_y + 1, min_x:max_x + 1]
    allowed[local_trusted > 0] = 0

    allowed[start_local[1], start_local[0]] = 1
    allowed[end_local[1], end_local[0]] = 1

    evidence_binary = (
        (crop_evidence > 0)
        | (crop_cleaned > 0)
    ).astype(np.uint8) * 255

    inverse_evidence = cv2.bitwise_not(evidence_binary)
    evidence_distance = cv2.distanceTransform(
        inverse_evidence,
        cv2.DIST_L2,
        5,
    )

    segment_vector = end - start
    segment_length_squared = max(float(np.dot(segment_vector, segment_vector)), 1.0)
    projection = (
        (global_x - start[0]) * segment_vector[0]
        + (global_y - start[1]) * segment_vector[1]
    ) / segment_length_squared
    projection = np.clip(projection, 0.0, 1.0)
    nearest_x = start[0] + projection * segment_vector[0]
    nearest_y = start[1] + projection * segment_vector[1]
    line_distance = np.hypot(global_x - nearest_x, global_y - nearest_y)

    inward_penalty = np.maximum(
        0.0,
        (minimum_endpoint_radius * 0.88 - radius_grid)
        / max(minimum_endpoint_radius, 1.0),
    )

    movement_cost = (
        5.5
        + evidence_distance * float(variant["distance_weight"])
        + line_distance * float(variant["line_weight"])
        + inward_penalty * float(variant["inward_weight"]) * 18.0
    ).astype(np.float32)
    movement_cost[crop_evidence > 0] = np.minimum(
        movement_cost[crop_evidence > 0],
        0.42,
    )

    movement_cost[crop_cleaned > 0] = np.minimum(
        movement_cost[crop_cleaned > 0],
        0.18,
    )

    # Labels and POI markers are a weaker path cue than genuine boundary
    # colour, but they explain where Google visually covered the selected
    # line. Direction/radial/geometry checks are applied by the caller.
    movement_cost[crop_occlusion > 0] = np.minimum(
        movement_cost[crop_occlusion > 0],
        SATELLITE_LABEL_OCCLUSION_PATH_COST,
    )

    movement_cost[allowed == 0] = np.inf

    start_node = (start_local[0], start_local[1])
    end_node = (end_local[0], end_local[1])
    queue = [(0.0, 0.0, start_node)]
    came_from = {}
    best_cost = {start_node: 0.0}
    expansions = 0
    neighbour_steps = (
        (-1, -1, math.sqrt(2.0)), (0, -1, 1.0), (1, -1, math.sqrt(2.0)),
        (-1, 0, 1.0), (1, 0, 1.0),
        (-1, 1, math.sqrt(2.0)), (0, 1, 1.0), (1, 1, math.sqrt(2.0)),
    )

    while queue and expansions < LOCAL_GAP_PATH_MAX_EXPANSIONS:
        _priority, current_cost, current = heapq.heappop(queue)
        if current_cost > best_cost.get(current, float("inf")) + 1e-9:
            continue
        if current == end_node:
            break
        expansions += 1
        current_x, current_y = current
        for dx, dy, step_length in neighbour_steps:
            next_x = current_x + dx
            next_y = current_y + dy
            if not (0 <= next_x < crop_width and 0 <= next_y < crop_height):
                continue
            pixel_cost = float(movement_cost[next_y, next_x])
            if not math.isfinite(pixel_cost):
                continue
            candidate_cost = current_cost + pixel_cost * step_length
            next_node = (next_x, next_y)
            if candidate_cost >= best_cost.get(next_node, float("inf")):
                continue
            best_cost[next_node] = candidate_cost
            came_from[next_node] = current
            heuristic = math.hypot(end_node[0] - next_x, end_node[1] - next_y) * 0.18
            heapq.heappush(queue, (candidate_cost + heuristic, candidate_cost, next_node))

    if end_node not in best_cost:
        return None, {
            "status": "no_path",
            "variant": variant["name"],
            "expansions": expansions,
        }

    local_path = [end_node]
    while local_path[-1] != start_node:
        local_path.append(came_from[local_path[-1]])
    local_path.reverse()
    global_path = [(x + min_x, y + min_y) for x, y in local_path]
    global_path = _simplify_open_pixel_path(global_path)

    path_length = sum(
        math.hypot(
            global_path[index + 1][0] - global_path[index][0],
            global_path[index + 1][1] - global_path[index][1],
        )
        for index in range(len(global_path) - 1)
    )

    return global_path, {
        "status": "path_found",
        "variant": variant["name"],
        "expansions": expansions,
        "direct_distance_px": round(direct_distance, 3),
        "path_length_px": round(path_length, 3),
        "length_ratio": round(path_length / max(direct_distance, 1.0), 4),
        "search_window": {
            "left": min_x,
            "top": min_y,
            "right": max_x,
            "bottom": max_y,
        },
    }


def _local_gap_path_red_support(
    path_xy: list[tuple[int, int]],
    evidence_mask: np.ndarray,
    occlusion_mask: Optional[np.ndarray] = None,
) -> dict:
    if not path_xy:
        return {
            "support_ratio": 0.0,
            "mean_distance_px": float("inf"),
            "distance_p90_px": float("inf"),
            "occlusion_only_ratio": 0.0,
            "combined_support_ratio": 0.0,
            "combined_mean_distance_px": float("inf"),
        }

    inverse = cv2.bitwise_not(
        (evidence_mask > 0).astype(np.uint8) * 255
    )

    red_distance_map = cv2.distanceTransform(
        inverse,
        cv2.DIST_L2,
        5,
    )

    points = np.asarray(path_xy, dtype=np.int32)
    points[:, 0] = np.clip(
        points[:, 0],
        0,
        evidence_mask.shape[1] - 1,
    )
    points[:, 1] = np.clip(
        points[:, 1],
        0,
        evidence_mask.shape[0] - 1,
    )

    red_distances = red_distance_map[
        points[:, 1],
        points[:, 0],
    ]

    red_supported = red_distances <= 9.0

    if (
        occlusion_mask is not None
        and occlusion_mask.shape == evidence_mask.shape
        and cv2.countNonZero(occlusion_mask) > 0
    ):
        inverse_occlusion = cv2.bitwise_not(
            (occlusion_mask > 0).astype(np.uint8) * 255
        )

        occlusion_distance_map = cv2.distanceTransform(
            inverse_occlusion,
            cv2.DIST_L2,
            5,
        )

        occlusion_distances = occlusion_distance_map[
            points[:, 1],
            points[:, 0],
        ]
    else:
        occlusion_distances = np.full_like(
            red_distances,
            float("inf"),
            dtype=np.float32,
        )

    occlusion_supported = (
        occlusion_distances
        <= SATELLITE_LABEL_OCCLUSION_SUPPORT_DISTANCE_PX
    )

    occlusion_only = (
        occlusion_supported
        & ~red_supported
    )

    combined_distances = np.minimum(
        red_distances,
        occlusion_distances,
    )

    combined_supported = (
        red_supported
        | occlusion_supported
    )

    return {
        # Existing keys remain red-only for backwards compatibility.
        "support_ratio": round(
            float(np.mean(red_supported)),
            4,
        ),
        "mean_distance_px": round(
            float(np.mean(red_distances)),
            3,
        ),
        "distance_p90_px": round(
            float(np.percentile(red_distances, 90)),
            3,
        ),
        "occlusion_only_ratio": round(
            float(np.mean(occlusion_only)),
            4,
        ),
        "occlusion_support_ratio": round(
            float(np.mean(occlusion_supported)),
            4,
        ),
        "combined_support_ratio": round(
            float(np.mean(combined_supported)),
            4,
        ),
        "combined_mean_distance_px": round(
            float(np.mean(combined_distances)),
            3,
        ),
        "combined_distance_p90_px": round(
            float(np.percentile(combined_distances, 90)),
            3,
        ),
    }


def _splice_gap_path(
    points_xy: np.ndarray,
    gap_start_index: int,
    path_xy: list[tuple[int, int]],
) -> np.ndarray:
    points = np.asarray(points_xy, dtype=np.int32).reshape(-1, 2)
    insertion = np.asarray(path_xy[1:-1], dtype=np.int32).reshape(-1, 2)
    if gap_start_index == len(points) - 1:
        return np.vstack((points, insertion)) if len(insertion) else points.copy()
    return np.vstack((
        points[:gap_start_index + 1],
        insertion,
        points[gap_start_index + 1:],
    ))


def _repair_local_polar_gaps(
    completed_points: np.ndarray,
    raw_red_mask: np.ndarray,
    graph_red_mask: np.ndarray,
    cleaned_red_mask: np.ndarray,
    center: tuple[int, int],
    roi_bounds: tuple[int, int, int, int],
    edge_margin: int,
    image_area: int,
    minimum_area_ratio: float,
    maximum_area_ratio: float,
    coverage_ratio: float,
    close_to_red_ratio: float,
    mean_red_distance_px: float,
    screenshot_path: str,
    label_occlusion_mask: Optional[np.ndarray] = None,
    log=print,
) -> tuple[Optional[np.ndarray], dict]:
    profile_name = ACTIVE_LOCALITY_PROFILE
    diagnostics = {
        "enabled": bool(LOCAL_GAP_PATH_ENABLED),
        "accepted": False,
        "method": "local_gap_path_repair",
        "profile": profile_name,
        "coverage": round(float(coverage_ratio), 4),
        "close_to_red_ratio": round(float(close_to_red_ratio), 4),
        "mean_red_distance_px": round(float(mean_red_distance_px), 3),
        "satellite_label_occlusion_enabled": bool(
            ACTIVE_SATELLITE_VIEW
            and SATELLITE_LABEL_OCCLUSION_REPAIR_ENABLED
            and label_occlusion_mask is not None
            and label_occlusion_mask.shape == raw_red_mask.shape
            and cv2.countNonZero(label_occlusion_mask) > 0
        ),
        "repairs": [],
    }
    if not LOCAL_GAP_PATH_ENABLED:
        diagnostics["reason"] = "disabled"
        return None, diagnostics

    admission_checks = {
        "coverage": float(coverage_ratio) >= float(_local_gap_profile_value(
            LOCAL_GAP_PATH_MIN_COVERAGE, profile_name, 0.70
        )),
        "close_to_red": float(close_to_red_ratio) >= float(_local_gap_profile_value(
            LOCAL_GAP_PATH_MIN_CLOSE_TO_RED, profile_name, 0.88
        )),
        "mean_red_distance": float(mean_red_distance_px) <= float(_local_gap_profile_value(
            LOCAL_GAP_PATH_MAX_MEAN_RED_DISTANCE_PX, profile_name, 9.0
        )),
    }
    diagnostics["admission_checks"] = admission_checks
    if not all(admission_checks.values()):
        diagnostics["reason"] = "polar_candidate_not_strong_enough"
        return None, diagnostics

    current_points = np.asarray(completed_points, dtype=np.int32).reshape(-1, 2)
    original_contour = current_points.reshape(-1, 1, 2)
    original_area = abs(float(cv2.contourArea(original_contour)))
    evidence_mask = cv2.bitwise_or(graph_red_mask, raw_red_mask)
    evidence_mask = cv2.bitwise_or(evidence_mask, cleaned_red_mask)
    search_margin = int(_local_gap_profile_value(
        LOCAL_GAP_PATH_SEARCH_MARGIN_PX, profile_name, 75
    ))
    maximum_original_gap = float(_local_gap_profile_value(
        LOCAL_GAP_PATH_MAX_ORIGINAL_GAP_PX, profile_name, 125.0
    ))

    for repair_number in range(1, LOCAL_GAP_PATH_MAX_GAPS + 1):
        step_lengths = _closed_contour_step_lengths(current_points)
        if len(step_lengths) == 0:
            break
        gap_start_index = int(np.argmax(step_lengths))
        largest_gap = float(step_lengths[gap_start_index])
        if largest_gap <= LOCAL_GAP_PATH_TARGET_STEP_PX:
            break
        if largest_gap > maximum_original_gap:
            diagnostics["reason"] = "largest_gap_exceeds_local_repair_limit"
            diagnostics["largest_gap_px"] = round(largest_gap, 3)
            return None, diagnostics

        start_xy = tuple(map(int, current_points[gap_start_index]))
        end_xy = tuple(map(int, current_points[(gap_start_index + 1) % len(current_points)]))

        endpoint_direction = (
            _gap_endpoint_direction_diagnostics(
                completed_points=current_points,
                gap_start_index=gap_start_index,
                center=center,
            )
        )

        log(
            "   [Local Gap] Repairing gap "
            f"{repair_number}: step={largest_gap:.2f}px, "
            f"start={start_xy}, end={end_xy}"
        )

        best_points = None
        best_record = None
        best_score = -float("inf")
        variant_records = []

        for variant in LOCAL_GAP_PATH_VARIANTS:
            path_xy, path_diagnostics = _astar_local_gap_path(
                start_xy=start_xy,
                end_xy=end_xy,
                completed_points=current_points,
                gap_start_index=gap_start_index,
                evidence_mask=evidence_mask,
                cleaned_red_mask=cleaned_red_mask,
                center=center,
                roi_bounds=roi_bounds,
                search_margin_px=search_margin,
                variant=variant,
                occlusion_mask=label_occlusion_mask,
            )
            record = dict(path_diagnostics)
            if path_xy is None:
                variant_records.append(record)
                continue

            support = _local_gap_path_red_support(
                path_xy,
                evidence_mask,
                label_occlusion_mask,
            )

            record["red_support"] = support
            record["endpoint_direction"] = dict(
                endpoint_direction
            )

            if record["length_ratio"] > LOCAL_GAP_PATH_MAX_LENGTH_RATIO:
                record["status"] = "path_too_long"
                variant_records.append(record)
                continue

            red_supported_path = bool(
                support["support_ratio"]
                >= LOCAL_GAP_PATH_MIN_RED_SUPPORT_RATIO
                and support["mean_distance_px"]
                <= LOCAL_GAP_PATH_MAX_PATH_MEAN_RED_DISTANCE_PX
            )

            satellite_gap_limit = float(
                _local_gap_profile_value(
                    SATELLITE_LABEL_OCCLUSION_MAX_GAP_PX,
                    profile_name,
                    125.0,
                )
            )

            label_occlusion_supported_path = bool(
                diagnostics[
                    "satellite_label_occlusion_enabled"
                ]
                and largest_gap <= satellite_gap_limit
                and endpoint_direction.get(
                    "safe_for_label_occlusion",
                    False,
                )
                and support["occlusion_only_ratio"]
                >= SATELLITE_LABEL_OCCLUSION_MIN_RATIO
                and support["combined_support_ratio"]
                >= SATELLITE_LABEL_OCCLUSION_MIN_COMBINED_SUPPORT_RATIO
                and support["combined_mean_distance_px"]
                <= SATELLITE_LABEL_OCCLUSION_MAX_COMBINED_MEAN_DISTANCE_PX
                and record["length_ratio"]
                <= SATELLITE_LABEL_OCCLUSION_MAX_PATH_LENGTH_RATIO
            )

            if red_supported_path:
                record["support_mode"] = "red"

            elif label_occlusion_supported_path:
                record["support_mode"] = "label_occlusion"

            else:
                record["status"] = (
                    "weak_red_and_occlusion_support"
                )
                variant_records.append(record)
                continue

            candidate_points = _splice_gap_path(current_points, gap_start_index, path_xy)
            candidate_contour = candidate_points.reshape(-1, 1, 2)
            area = abs(float(cv2.contourArea(candidate_contour)))
            area_ratio = area / max(float(image_area), 1.0)
            area_change = abs(area - original_area) / max(original_area, 1.0)
            x, y, box_width, box_height = cv2.boundingRect(candidate_contour)
            roi_left, roi_top, roi_right, roi_bottom = roi_bounds
            inside_roi = (
                x >= roi_left - edge_margin
                and y >= roi_top - edge_margin
                and x + box_width <= roi_right + edge_margin
                and y + box_height <= roi_bottom + edge_margin
            )
            contains_target = cv2.pointPolygonTest(
                candidate_contour,
                (float(center[0]), float(center[1])),
                False,
            ) >= 0
            candidate_steps = _closed_contour_step_lengths(candidate_points)
            maximum_final_step = float(np.max(candidate_steps)) if len(candidate_steps) else float("inf")
            crossings = _count_closed_polyline_crossings(candidate_points.astype(np.float32))
            shortcut = _inner_shortcut_diagnostics(candidate_contour, center)

            checks = {
                "area_ratio": minimum_area_ratio <= area_ratio <= maximum_area_ratio,
                "area_change": area_change <= LOCAL_GAP_PATH_MAX_AREA_CHANGE_RATIO,
                "inside_roi": bool(inside_roi),
                "contains_target": bool(contains_target),
                "maximum_step": maximum_final_step <= LOCAL_GAP_PATH_MAX_FINAL_STEP_PX,
                "no_self_crossing": crossings == 0,
                "no_inner_shortcut": not shortcut.get("suspected", False),
            }
            record.update({
                "status": "candidate" if all(checks.values()) else "candidate_failed_safety",
                "path_point_count": len(path_xy),
                "path_xy": [
                    [int(point[0]), int(point[1])]
                    for point in path_xy
                ],
                "area_ratio": round(area_ratio, 6),
                "area_change_ratio": round(area_change, 5),
                "maximum_final_step_px": round(maximum_final_step, 3),
                "self_crossings": int(crossings),
                "shortcut": shortcut,
                "checks": checks,
            })
            variant_records.append(record)
            if not all(checks.values()):
                continue

            support_quality = (
                support["support_ratio"]
                if record.get("support_mode") == "red"
                else support["combined_support_ratio"]
            )

            support_distance = (
                support["mean_distance_px"]
                if record.get("support_mode") == "red"
                else support["combined_mean_distance_px"]
            )

            score = (
                support_quality * 3.0
                - support_distance * 0.08
                - record["length_ratio"] * 0.16
                - area_change * 2.0
            )
            if score > best_score:
                best_score = score
                best_points = candidate_points
                best_record = record

        diagnostics["repairs"].append({
            "repair_number": repair_number,
            "original_gap_px": round(largest_gap, 3),
            "gap_start_index": gap_start_index,
            "start": start_xy,
            "end": end_xy,
            "endpoint_direction": endpoint_direction,
            "variants": variant_records,
            "selected": best_record,
        })

        if best_points is None:
            diagnostics["reason"] = "no_safe_red_supported_local_path"
            log(
                "   [Local Gap] No path variant safely repaired "
                f"the {largest_gap:.2f}px gap"
            )
            return None, diagnostics

        current_points = best_points
        log(
            "   [Local Gap] Accepted path: "
            f"variant={best_record['variant']}, "
            f"length_ratio={best_record['length_ratio']:.2f}, "
            f"support_mode={best_record.get('support_mode', 'red')}, "
            f"red_support={best_record['red_support']['support_ratio']:.3f}, "
            "combined_support="
            f"{best_record['red_support'].get('combined_support_ratio', 0.0):.3f}, "
            f"new_max_step={best_record['maximum_final_step_px']:.2f}px"
        )

    final_steps = _closed_contour_step_lengths(current_points)
    final_max_step = float(np.max(final_steps)) if len(final_steps) else float("inf")
    if final_max_step > LOCAL_GAP_PATH_MAX_FINAL_STEP_PX:
        diagnostics["reason"] = "unrepaired_oversized_gap_remains"
        diagnostics["maximum_final_step_px"] = round(final_max_step, 3)
        return None, diagnostics

    final_contour = current_points.reshape(-1, 1, 2)
    final_area_ratio = abs(float(cv2.contourArea(final_contour))) / max(float(image_area), 1.0)
    final_crossings = _count_closed_polyline_crossings(current_points.astype(np.float32))
    final_shortcut = _inner_shortcut_diagnostics(final_contour, center)
    final_checks = {
        "area_ratio": minimum_area_ratio <= final_area_ratio <= maximum_area_ratio,
        "contains_target": cv2.pointPolygonTest(
            final_contour,
            (float(center[0]), float(center[1])),
            False,
        ) >= 0,
        "maximum_step": final_max_step <= LOCAL_GAP_PATH_MAX_FINAL_STEP_PX,
        "no_self_crossing": final_crossings == 0,
        "no_inner_shortcut": not final_shortcut.get("suspected", False),
    }
    diagnostics.update({
        "accepted": all(final_checks.values()),
        "area_ratio": round(final_area_ratio, 4),
        "maximum_final_step_px": round(final_max_step, 3),
        "final_checks": final_checks,
        "self_crossings": int(final_crossings),
        "shortcut": final_shortcut,
        "requires_cross_zoom_confirmation": True,
        "provisional_reason": "local_gap_path_repair",
        "raw_points": len(current_points),
        "used_label_occlusion": any(
            isinstance(repair.get("selected"), dict)
            and repair["selected"].get("support_mode")
            == "label_occlusion"
            for repair in diagnostics.get("repairs", [])
        ),
    })
    if not diagnostics["accepted"]:
        diagnostics["reason"] = "final_local_gap_validation_failed"
        return None, diagnostics

    debug_image = cv2.cvtColor(raw_red_mask, cv2.COLOR_GRAY2BGR)

    if (
        label_occlusion_mask is not None
        and label_occlusion_mask.shape == raw_red_mask.shape
    ):
        blue_overlay = np.zeros_like(debug_image)
        blue_overlay[:, :, 0] = label_occlusion_mask
        debug_image = cv2.addWeighted(
            debug_image,
            1.0,
            blue_overlay,
            0.35,
            0.0,
        )

    cv2.drawContours(debug_image, [original_contour], -1, (0, 0, 255), 1)
    cv2.drawContours(debug_image, [final_contour], -1, (0, 255, 0), 2)

    for repair in diagnostics.get("repairs", []):
        selected = repair.get("selected")

        if not isinstance(selected, dict):
            continue

        path_xy = selected.get("path_xy", [])

        if len(path_xy) < 2:
            continue

        path_array = np.asarray(
            path_xy,
            dtype=np.int32,
        ).reshape(-1, 1, 2)

        path_colour = (
            (0, 255, 255)
            if selected.get("support_mode") == "label_occlusion"
            else (0, 255, 0)
        )

        cv2.polylines(
            debug_image,
            [path_array],
            False,
            path_colour,
            3,
            cv2.LINE_AA,
        )

    debug_path = (
        os.path.splitext(screenshot_path)[0]
        + "_red_local_gap_repaired.png"
    )

    cv2.imwrite(debug_path, debug_image)
    diagnostics["debug_image"] = debug_path
    return final_contour, diagnostics


def _local_gap_candidate_is_safe(diagnostics: dict) -> bool:
    if not isinstance(diagnostics, dict):
        return False
    if diagnostics.get("provisional_reason") != "local_gap_path_repair":
        return False
    repairs = diagnostics.get("repairs", [])
    final_checks = diagnostics.get("final_checks", {})
    if not repairs or not isinstance(final_checks, dict):
        return False
    selected_repairs = [repair.get("selected") for repair in repairs]
    return bool(
        diagnostics.get("accepted", False)
        and final_checks
        and all(bool(value) for value in final_checks.values())
        and all(isinstance(record, dict) for record in selected_repairs)
        and all(
            _local_gap_selected_repair_is_safe(record)
            for record in selected_repairs
        )
        and int(diagnostics.get("self_crossings", 1) or 0) == 0
        and not bool(diagnostics.get("shortcut", {}).get("suspected", False))
    )



def _longest_circular_presence_run(
    occupied_bins: set[int],
    bin_count: int,
    maximum_gap_bins: int,
) -> int:
    if not occupied_bins:
        return 0
    occupied = sorted(int(value) for value in occupied_bins)
    doubled = occupied + [value + bin_count for value in occupied]
    longest = current = 1
    for index in range(1, len(doubled)):
        gap = doubled[index] - doubled[index - 1] - 1
        current = current + 1 if gap <= maximum_gap_bins else 1
        longest = max(longest, current)
        if longest >= len(occupied):
            break
    return min(longest, len(occupied))


def _boundary_presence_gate(
    full_red_mask: np.ndarray,
    full_strict_red_mask: np.ndarray,
    cleaned_roi_mask: np.ndarray,
    center: tuple[int, int],
    roi_bounds: tuple[int, int, int, int],
    screenshot_path: str,
    log=print,
) -> tuple[bool, dict]:
    height, width = full_red_mask.shape[
        :2
    ]

    if not BOUNDARY_PRESENCE_GATE_ENABLED:
        return (
            True,
            {
                "enabled": False,
                "present": True,
                "reason": "disabled",
            },
        )

    usable = np.full(
        (
            height,
            width,
        ),
        255,
        dtype=np.uint8,
    )

    top_exclusion = int(
        round(
            height
            * BOUNDARY_PRESENCE_UI_TOP_RATIO
        )
    )

    bottom_exclusion = int(
        round(
            height
            * BOUNDARY_PRESENCE_UI_BOTTOM_RATIO
        )
    )

    usable[
        :top_exclusion,
        :
    ] = 0

    if bottom_exclusion > 0:
        usable[
            height
            - bottom_exclusion:,
            :
        ] = 0

    strict_source = cv2.bitwise_and(
        full_strict_red_mask,
        usable,
    )

    combined_source = cv2.bitwise_and(
        full_red_mask,
        usable,
    )

    strict_pixel_count = int(
        cv2.countNonZero(
            strict_source
        )
    )

    # Join neighbouring glyphs so labels become rejectable text rectangles.
    text_probe = cv2.morphologyEx(
        strict_source,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (
                15,
                3,
            ),
        ),
        iterations=1,
    )

    (
        text_count,
        _,
        text_stats,
        _,
    ) = cv2.connectedComponentsWithStats(
        (
            text_probe
            > 0
        ).astype(
            np.uint8
        ),
        connectivity=8,
    )

    rejected = np.zeros_like(
        strict_source
    )

    rejected_text_boxes = 0
    rejected_icons = 0

    for component_id in range(
        1,
        text_count,
    ):
        x = int(
            text_stats[
                component_id,
                cv2.CC_STAT_LEFT,
            ]
        )

        y = int(
            text_stats[
                component_id,
                cv2.CC_STAT_TOP,
            ]
        )

        object_width = int(
            text_stats[
                component_id,
                cv2.CC_STAT_WIDTH,
            ]
        )

        object_height = int(
            text_stats[
                component_id,
                cv2.CC_STAT_HEIGHT,
            ]
        )

        area = int(
            text_stats[
                component_id,
                cv2.CC_STAT_AREA,
            ]
        )

        box_area = max(
            1,
            object_width
            * object_height,
        )

        fill_ratio = (
            area
            / box_area
        )

        aspect = (
            object_width
            / max(
                object_height,
                1,
            )
        )

        text_like = bool(
            object_width
            >= 20
            and 10
            <= object_height
            <= 58
            and aspect
            >= 1.25
            and area
            >= 38
            and fill_ratio
            >= 0.25
        )

        icon_like = bool(
            9
            <= object_width
            <= 58
            and 9
            <= object_height
            <= 58
            and area
            >= 64
            and fill_ratio
            >= 0.28
        )

        if not (
            text_like
            or icon_like
        ):
            continue

        padding = 4

        rejected[
            max(
                0,
                y - padding,
            ):
            min(
                height,
                y
                + object_height
                + padding,
            ),
            max(
                0,
                x - padding,
            ):
            min(
                width,
                x
                + object_width
                + padding,
            ),
        ] = 255

        rejected_text_boxes += int(
            text_like
        )

        rejected_icons += int(
            icon_like
        )

    rejected_strict_pixels = int(
        cv2.countNonZero(
            cv2.bitwise_and(
                strict_source,
                rejected,
            )
        )
    )

    rejected_strict_ratio = (
        rejected_strict_pixels
        / max(
            strict_pixel_count,
            1,
        )
    )

    # Self-guard: genuine map labels never account for the vast majority of
    # strict red. If rejection would eat more than 60% of it, the text/icon
    # heuristics have latched onto the dotted boundary itself. Discard the
    # rejection and let the downstream angular-track and line-coherence
    # checks decide whether the red forms a real boundary.
    if rejected_strict_ratio > 0.60:
        rejected = np.zeros_like(
            strict_source
        )
        rejected_strict_pixels = 0
        rejected_strict_ratio = 0.0
        rejected_text_boxes = 0
        rejected_icons = 0

    residual = cv2.bitwise_and(
        strict_source,
        cv2.bitwise_not(
            rejected
        ),
    )

    (
        component_count,
        component_labels,
        component_stats,
        component_centroids,
    ) = cv2.connectedComponentsWithStats(
        (
            residual
            > 0
        ).astype(
            np.uint8
        ),
        connectivity=8,
    )

    bin_count = int(
        BOUNDARY_PRESENCE_ANGLE_BINS
    )

    candidates = []
    radii_by_bin: dict[
        int,
        list[float],
    ] = {}

    for component_id in range(
        1,
        component_count,
    ):
        area = int(
            component_stats[
                component_id,
                cv2.CC_STAT_AREA,
            ]
        )

        object_width = int(
            component_stats[
                component_id,
                cv2.CC_STAT_WIDTH,
            ]
        )

        object_height = int(
            component_stats[
                component_id,
                cv2.CC_STAT_HEIGHT,
            ]
        )

        if not (
            2
            <= area
            <= 100
            and object_width
            <= 22
            and object_height
            <= 22
        ):
            continue

        centroid_x = float(
            component_centroids[
                component_id,
                0,
            ]
        )

        centroid_y = float(
            component_centroids[
                component_id,
                1,
            ]
        )

        delta_x = (
            centroid_x
            - float(
                center[
                    0
                ]
            )
        )

        delta_y = (
            centroid_y
            - float(
                center[
                    1
                ]
            )
        )

        radius = float(
            math.hypot(
                delta_x,
                delta_y,
            )
        )

        if radius < 28.0:
            continue

        angle = (
            math.atan2(
                delta_y,
                delta_x,
            )
            + 2.0
            * math.pi
        ) % (
            2.0
            * math.pi
        )

        bin_index = int(
            math.floor(
                angle
                / (
                    2.0
                    * math.pi
                )
                * bin_count
            )
        ) % bin_count

        radii_by_bin.setdefault(
            bin_index,
            [],
        ).append(
            radius
        )

        candidates.append({
            "component_id": int(
                component_id
            ),
            "angle_bin": int(
                bin_index
            ),
            "radius": radius,
        })

    occupied_bins = set(
        radii_by_bin
    )

    angular_coverage = (
        len(
            occupied_bins
        )
        / max(
            bin_count,
            1,
        )
    )

    components_per_bin = (
        len(
            candidates
        )
        / max(
            len(
                occupied_bins
            ),
            1,
        )
    )

    # Find the longest angular chain whose neighbouring radii move gradually.
    best_track_bins = []
    best_track_radii = []

    for start_bin in sorted(
        occupied_bins
    ):
        for start_radius in radii_by_bin[
            start_bin
        ]:
            track_bins = [
                int(
                    start_bin
                )
            ]

            track_radii = [
                float(
                    start_radius
                )
            ]

            used_bins = {
                int(
                    start_bin
                )
            }

            current_bin = int(
                start_bin
            )

            current_radius = float(
                start_radius
            )

            for _ in range(
                bin_count
                - 1
            ):
                selected = None

                for step in range(
                    1,
                    BOUNDARY_PRESENCE_MAX_TRACK_GAP_BINS
                    + 2,
                ):
                    next_bin = (
                        current_bin
                        + step
                    ) % bin_count

                    if (
                        next_bin
                        in used_bins
                        or next_bin
                        not in radii_by_bin
                    ):
                        continue

                    best_radius = min(
                        radii_by_bin[
                            next_bin
                        ],
                        key=lambda value: abs(
                            float(
                                value
                            )
                            - current_radius
                        ),
                    )

                    allowed_jump = max(
                        BOUNDARY_PRESENCE_MAX_TRACK_JUMP_PX,
                        min(
                            current_radius,
                            float(
                                best_radius
                            ),
                        )
                        * BOUNDARY_PRESENCE_MAX_TRACK_JUMP_RATIO,
                    )

                    radial_jump = abs(
                        float(
                            best_radius
                        )
                        - current_radius
                    )

                    if radial_jump <= allowed_jump:
                        selected = (
                            next_bin,
                            float(
                                best_radius
                            ),
                        )

                        break

                if selected is None:
                    break

                current_bin, current_radius = (
                    selected
                )

                track_bins.append(
                    int(
                        current_bin
                    )
                )

                track_radii.append(
                    float(
                        current_radius
                    )
                )

                used_bins.add(
                    int(
                        current_bin
                    )
                )

            if len(
                track_bins
            ) > len(
                best_track_bins
            ):
                best_track_bins = (
                    track_bins
                )

                best_track_radii = (
                    track_radii
                )

    track_coverage = (
        len(
            best_track_bins
        )
        / max(
            bin_count,
            1,
        )
    )

    track_quadrants = {
        min(
            3,
            int(
                bin_index
                / max(
                    bin_count
                    / 4.0,
                    1.0,
                )
            ),
        )
        for bin_index
        in best_track_bins
    }

    track_jump_ratios = []

    for first_radius, second_radius in zip(
        best_track_radii,
        best_track_radii[
            1:
        ],
    ):
        track_jump_ratios.append(
            abs(
                second_radius
                - first_radius
            )
            / max(
                min(
                    first_radius,
                    second_radius,
                ),
                1.0,
            )
        )

    mean_track_jump_ratio = (
        float(
            np.mean(
                track_jump_ratios
            )
        )
        if track_jump_ratios
        else 0.0
    )

    # Long-line evidence must itself cover a substantial angular range around
    # the target; local text strokes cannot approve presence.
    line_source = cv2.bitwise_and(
        combined_source,
        cv2.bitwise_not(
            rejected
        ),
    )

    line_probe = cv2.morphologyEx(
        line_source,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (
                5,
                5,
            ),
        ),
        iterations=1,
    )

    line_contours, _ = cv2.findContours(
        line_probe,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_NONE,
    )

    accepted_line_arcs = []
    line_angle_bins = set()

    for contour in line_contours:
        arc_length = float(
            cv2.arcLength(
                contour,
                False,
            )
        )

        if (
            arc_length
            < BOUNDARY_PRESENCE_MIN_LINE_ARC_PX
        ):
            continue

        contour_area = float(
            cv2.contourArea(
                contour
            )
        )

        thickness = (
            2.0
            * contour_area
            / max(
                arc_length,
                1.0,
            )
        )

        _, _, object_width, object_height = (
            cv2.boundingRect(
                contour
            )
        )

        if (
            max(
                object_width,
                object_height,
            )
            < 70
            or thickness
            > BOUNDARY_PRESENCE_MAX_LINE_THICKNESS_PX
        ):
            continue

        contour_points = contour.reshape(
            -1,
            2,
        )

        contour_bins = set()

        for pixel_x, pixel_y in contour_points[
            ::max(
                1,
                len(
                    contour_points
                )
                // 120,
            )
        ]:
            delta_x = (
                float(
                    pixel_x
                )
                - float(
                    center[
                        0
                    ]
                )
            )

            delta_y = (
                float(
                    pixel_y
                )
                - float(
                    center[
                        1
                    ]
                )
            )

            if math.hypot(
                delta_x,
                delta_y,
            ) < 28.0:
                continue

            angle = (
                math.atan2(
                    delta_y,
                    delta_x,
                )
                + 2.0
                * math.pi
            ) % (
                2.0
                * math.pi
            )

            contour_bins.add(
                int(
                    math.floor(
                        angle
                        / (
                            2.0
                            * math.pi
                        )
                        * bin_count
                    )
                ) % bin_count
            )

        if (
            len(
                contour_bins
            )
            / max(
                bin_count,
                1,
            )
            < 0.05
        ):
            continue

        accepted_line_arcs.append(
            arc_length
        )

        line_angle_bins.update(
            contour_bins
        )

    total_line_arc = float(
        sum(
            accepted_line_arcs
        )
    )

    line_angular_coverage = (
        len(
            line_angle_bins
        )
        / max(
            bin_count,
            1,
        )
    )

    line_quadrants = {
        min(
            3,
            int(
                bin_index
                / max(
                    bin_count
                    / 4.0,
                    1.0,
                )
            ),
        )
        for bin_index
        in line_angle_bins
    }

    cleaned_pixels = int(
        cv2.countNonZero(
            cleaned_roi_mask
        )
    )

    # Cleaned pixels are support only. They are no longer allowed to approve
    # boundary presence by themselves.
    density_safe = bool(
        components_per_bin
        <= BOUNDARY_PRESENCE_MAX_COMPONENTS_PER_BIN
    )

    rejected_ratio_safe = bool(
        rejected_strict_ratio
        <= BOUNDARY_PRESENCE_MAX_REJECTED_STRICT_RATIO
    )

    dotted_presence = bool(
        len(
            candidates
        )
        >= BOUNDARY_PRESENCE_MIN_COMPONENTS
        and angular_coverage
        >= BOUNDARY_PRESENCE_MIN_ANGULAR_COVERAGE
        and track_coverage
        >= BOUNDARY_PRESENCE_MIN_TRACK_COVERAGE
        and len(
            track_quadrants
        )
        >= BOUNDARY_PRESENCE_MIN_TRACK_QUADRANTS
        and density_safe
        and rejected_ratio_safe
        and mean_track_jump_ratio
        <= (
            BOUNDARY_PRESENCE_MAX_TRACK_JUMP_RATIO
            * 0.82
        )
    )

    # Off-centre targets at low zoom legitimately wrap only 2 quadrants
    # around the target pixel. Accept 2 quadrants when every other dotted
    # signal is far above its minimum, so a real boundary is not discarded
    # purely for viewport geometry.
    strong_dotted_presence = bool(
        len(
            candidates
        )
        >= BOUNDARY_PRESENCE_MIN_COMPONENTS
        * 2
        and angular_coverage
        >= BOUNDARY_PRESENCE_MIN_ANGULAR_COVERAGE
        * 2.0
        and track_coverage
        >= BOUNDARY_PRESENCE_MIN_TRACK_COVERAGE
        and len(
            track_quadrants
        )
        >= 2
        and density_safe
        and rejected_ratio_safe
        and mean_track_jump_ratio
        <= (
            BOUNDARY_PRESENCE_MAX_TRACK_JUMP_RATIO
            * 0.82
        )
    )

    dotted_presence = bool(
        dotted_presence
        or strong_dotted_presence
    )

    line_presence = bool(
        total_line_arc
        >= BOUNDARY_PRESENCE_MIN_TOTAL_LINE_ARC_PX
        and line_angular_coverage
        >= BOUNDARY_PRESENCE_MIN_LINE_ANGULAR_COVERAGE
        and len(
            line_quadrants
        )
        >= BOUNDARY_PRESENCE_MIN_LINE_QUADRANTS
        and rejected_ratio_safe
    )

    component_score = min(
        1.0,
        len(
            candidates
        )
        / max(
            BOUNDARY_PRESENCE_MIN_COMPONENTS,
            1,
        ),
    )

    angular_score = min(
        1.0,
        angular_coverage
        / max(
            BOUNDARY_PRESENCE_MIN_ANGULAR_COVERAGE,
            1e-6,
        ),
    )

    track_score = min(
        1.0,
        track_coverage
        / max(
            BOUNDARY_PRESENCE_MIN_TRACK_COVERAGE,
            1e-6,
        ),
    )

    density_score = min(
        1.0,
        BOUNDARY_PRESENCE_MAX_COMPONENTS_PER_BIN
        / max(
            components_per_bin,
            1e-6,
        ),
    )

    line_score = min(
        1.0,
        line_angular_coverage
        / max(
            BOUNDARY_PRESENCE_MIN_LINE_ANGULAR_COVERAGE,
            1e-6,
        ),
    )

    rejection_score = max(
        0.0,
        1.0
        - rejected_strict_ratio,
    )

    cleaned_support_score = min(
        1.0,
        cleaned_pixels
        / max(
            BOUNDARY_PRESENCE_MIN_CLEAN_PIXELS
            * 4,
            1,
        ),
    )

    presence_score = (
        0.15
        * component_score
        + 0.15
        * angular_score
        + 0.30
        * track_score
        + 0.16
        * density_score
        + 0.12
        * line_score
        + 0.08
        * rejection_score
        + 0.04
        * cleaned_support_score
    )

    present = bool(
        (
            dotted_presence
            or line_presence
        )
        and presence_score
        >= BOUNDARY_PRESENCE_MIN_SCORE
    )

    candidate_mask = np.zeros_like(
        residual
    )

    for candidate in candidates:
        candidate_mask[
            component_labels
            == candidate[
                "component_id"
            ]
        ] = 255

    base_path = os.path.splitext(
        screenshot_path
    )[0]

    candidate_path = (
        base_path
        + "_red_boundary_presence_candidates.png"
    )

    rejected_path = (
        base_path
        + "_red_boundary_presence_rejected.png"
    )

    cv2.imwrite(
        candidate_path,
        candidate_mask,
    )

    cv2.imwrite(
        rejected_path,
        rejected,
    )

    diagnostics = {
        "enabled": True,
        "present": present,
        "reason": (
            "boundary_like_red_evidence_present"
            if present
            else "no_boundary_like_red_evidence"
        ),
        "score": round(
            presence_score,
            4,
        ),
        "candidate_components": int(
            len(
                candidates
            )
        ),
        "occupied_angle_bins": int(
            len(
                occupied_bins
            )
        ),
        "angular_coverage": round(
            angular_coverage,
            4,
        ),
        "components_per_bin": round(
            components_per_bin,
            4,
        ),
        "density_safe": bool(
            density_safe
        ),
        "track_bins": int(
            len(
                best_track_bins
            )
        ),
        "track_coverage": round(
            track_coverage,
            4,
        ),
        "track_quadrants": int(
            len(
                track_quadrants
            )
        ),
        "mean_track_jump_ratio": round(
            mean_track_jump_ratio,
            4,
        ),
        "accepted_line_arcs": int(
            len(
                accepted_line_arcs
            )
        ),
        "total_line_arc_px": round(
            total_line_arc,
            3,
        ),
        "line_angular_coverage": round(
            line_angular_coverage,
            4,
        ),
        "line_quadrants": int(
            len(
                line_quadrants
            )
        ),
        "cleaned_pixels": int(
            cleaned_pixels
        ),
        "rejected_text_boxes": int(
            rejected_text_boxes
        ),
        "rejected_icons": int(
            rejected_icons
        ),
        "rejected_strict_ratio": round(
            rejected_strict_ratio,
            4,
        ),
        "rejected_ratio_safe": bool(
            rejected_ratio_safe
        ),
        "dotted_presence": bool(
            dotted_presence
        ),
        "line_presence": bool(
            line_presence
        ),
        "debug_images": {
            "candidates": candidate_path,
            "rejected": rejected_path,
        },
    }

    log(
        "   [Boundary Presence] "
        f"score={presence_score:.3f}, "
        f"components={len(candidates)}, "
        f"bins={len(occupied_bins)}, "
        f"density={components_per_bin:.2f}, "
        f"track={track_coverage:.3f}, "
        f"track_quadrants={len(track_quadrants)}, "
        f"line_angular={line_angular_coverage:.3f}, "
        f"rejected_red={rejected_strict_ratio:.3f}, "
        f"status={'PRESENT' if present else 'ABSENT'}"
    )

    return (
        present,
        diagnostics,
    )


def detect_boundary_cv(
    screenshot_path: str,
    georef: GeoReference,
    log=print,
) -> Optional[list[tuple[float, float]]]:
    """
    Reconstruct Google's red dotted locality boundary.

    The previous implementation created a convex hull from every red object
    detected in the screenshot. Red hospital icons and text were therefore
    connected to the locality outline, producing an incorrect KML polygon.

    This version:
    1. searches only around the requested place;
    2. removes compact red map icons;
    3. first tries an enclosed red-line region;
    4. otherwise reconstructs the dotted outline radially around the target;
    5. rejects shapes that leave the target search area.
    """
    global LAST_CV_DIAGNOSTICS
    global LAST_CV_REVIEW_CANDIDATES
    global ACTIVE_CAPTURE_ZOOM

    ACTIVE_CAPTURE_ZOOM = float(
        georef.zoom
    )

    LAST_CV_DIAGNOSTICS = {
        "status": "started",
        "screenshot_path": screenshot_path,
    }

    LAST_CV_REVIEW_CANDIDATES = []

    image = cv2.imread(
        screenshot_path
    )

    if image is None:
        log(
            "   [CV] Could not read screenshot"
        )

        LAST_CV_DIAGNOSTICS = {
            "status": "rejected",
            "reason": "screenshot_read_failed",
            "screenshot_path": screenshot_path,
        }

        return None

    height, width = image.shape[:2]
    image_area = max(
        1,
        width * height,
    )

    target_px_x, target_px_y = (
        georef.target_pixel
    )

    center = (
        int(
            round(
                target_px_x
            )
        ),
        int(
            round(
                target_px_y
            )
        ),
    )

    center = (
        max(
            0,
            min(
                center[0],
                width - 1,
            ),
        ),
        max(
            0,
            min(
                center[1],
                height - 1,
            ),
        ),
    )

    log(
        "   [CV] Looking for Google-red "
        "dotted locality outline..."
    )
    log(
        "   [CV] Using searched-place target pixel: "
        f"x={center[0]}, y={center[1]}"
    )

    # Detect Google's red boundary colour. Priority 9.2 also includes the
    # anti-aliased red pixels that become thinner at lower zooms.
    red_mask, red_colour_metrics = (
        _adaptive_google_red_mask(
            image
        )
    )

    # Priority 15.5.4.2: preserve the full-frame colour evidence before the
    # normal target ROI is applied.
    full_red_mask = red_mask.copy()

    full_strict_red_mask = _strict_google_red_mask(
        image
    )

    full_red_debug_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_pixels_full_frame.png"
    )

    full_strict_debug_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_pixels_strict_full_frame.png"
    )

    cv2.imwrite(
        full_red_debug_path,
        full_red_mask,
    )

    cv2.imwrite(
        full_strict_debug_path,
        full_strict_red_mask,
    )

    log(
        "   [CV Red] strict="
        f"{red_colour_metrics['strict_pixels']}, "
        "adaptive="
        f"{red_colour_metrics['adaptive_pixels']}, "
        "combined="
        f"{red_colour_metrics['combined_pixels']}"
    )

    # Priority 15: locality-size and zoom-aware search area.
    zoom = float(
        georef.zoom
    )

    (
        roi_width_ratio,
        roi_height_ratio,
    ) = _adaptive_roi_ratios(
        ACTIVE_LOCALITY_PROFILE,
        zoom,
    )

    (
        minimum_area_ratio,
        maximum_area_ratio,
    ) = _adaptive_area_limits(
        ACTIVE_LOCALITY_PROFILE,
        zoom,
    )

    log(
        "   [Adaptive] Detection limits: "
        f"profile={ACTIVE_LOCALITY_PROFILE}, "
        f"zoom={zoom:.1f}, "
        f"roi=({roi_width_ratio:.2f}, "
        f"{roi_height_ratio:.2f}), "
        f"area={minimum_area_ratio:.4f}-"
        f"{maximum_area_ratio:.4f}"
    )

    roi_half_width = int(
        width * roi_width_ratio
    )
    roi_half_height = int(
        height * roi_height_ratio
    )

    # The window is centred on the target, so when Google keeps the selected
    # place near a frame edge the window is clipped on that side and loses
    # half of its intended size. Redistribute the clipped amount to the
    # opposite side so the full window fits inside the image.
    roi_left = center[0] - roi_half_width
    roi_right = center[0] + roi_half_width
    roi_top = center[1] - roi_half_height
    roi_bottom = center[1] + roi_half_height

    if roi_left < 0:
        roi_right -= roi_left
        roi_left = 0

    if roi_right > width:
        roi_left -= (
            roi_right
            - width
        )
        roi_right = width

    if roi_top < 0:
        roi_bottom -= roi_top
        roi_top = 0

    if roi_bottom > height:
        roi_top -= (
            roi_bottom
            - height
        )
        roi_bottom = height

    roi_left = max(
        0,
        int(roi_left),
    )
    roi_right = min(
        width,
        int(roi_right),
    )
    roi_top = max(
        0,
        int(roi_top),
    )
    roi_bottom = min(
        height,
        int(roi_bottom),
    )

    roi_mask = np.zeros_like(
        red_mask
    )

    roi_mask[
        roi_top:roi_bottom,
        roi_left:roi_right,
    ] = 255

    red_mask = cv2.bitwise_and(
        red_mask,
        roi_mask,
    )

    normal_roi_bounds = (
        roi_left,
        roi_top,
        roi_right,
        roi_bottom,
    )

    normal_roi_red_pixels = int(
        cv2.countNonZero(
            red_mask
        )
    )

    log(
        "   [CV] Target boundary search area: "
        f"x={roi_left}:{roi_right}, "
        f"y={roi_top}:{roi_bottom}"
    )

    log(
        "   [Full Bootstrap] Red evidence counts: "
        f"full={cv2.countNonZero(full_red_mask)}, "
        f"full_strict={cv2.countNonZero(full_strict_red_mask)}, "
        f"normal_roi={normal_roi_red_pixels}"
    )

    # ------------------------------------------------------------------
    # PRIORITY 1: CLEAN THE RED MASK
    # ------------------------------------------------------------------
    # Keep a copy of the colour-only result for comparison.
    raw_red_mask = red_mask.copy()

    raw_red_debug_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_pixels_raw.png"
    )

    cv2.imwrite(
        raw_red_debug_path,
        raw_red_mask,
    )

    noise_mask = np.zeros_like(
        raw_red_mask
    )

    removed_circular_objects = 0
    removed_text_or_filled_objects = 0
    removed_isolated_objects = 0

    # ---------------------------------------------------------------
    # A. Remove round/compact POI symbols such as hospital icons.
    #
    # A small 5x5 closing joins the ring of a circular icon so that its
    # circularity can be measured. Long boundary segments are not compact
    # and therefore do not satisfy these conditions.
    # ---------------------------------------------------------------
    circular_probe = cv2.morphologyEx(
        raw_red_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (5, 5),
        ),
        iterations=1,
    )

    circular_contours, _ = cv2.findContours(
        circular_probe,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    for contour in circular_contours:
        contour_area = float(
            cv2.contourArea(
                contour
            )
        )

        contour_perimeter = float(
            cv2.arcLength(
                contour,
                True,
            )
        )

        if (
            contour_area < 10.0
            or contour_perimeter <= 0.0
        ):
            continue

        (
            object_x,
            object_y,
            object_width,
            object_height,
        ) = cv2.boundingRect(
            contour
        )

        object_aspect = (
            object_width
            / max(
                object_height,
                1,
            )
        )

        object_circularity = (
            4.0
            * math.pi
            * contour_area
            / (
                contour_perimeter
                * contour_perimeter
            )
        )

        looks_like_round_icon = (
            object_width <= 40
            and object_height <= 40
            and 0.55 <= object_aspect <= 1.80
            and object_circularity >= 0.52
        )

        if not looks_like_round_icon:
            continue

        padding = 3

        remove_left = max(
            0,
            object_x - padding,
        )
        remove_top = max(
            0,
            object_y - padding,
        )
        remove_right = min(
            width,
            object_x
            + object_width
            + padding,
        )
        remove_bottom = min(
            height,
            object_y
            + object_height
            + padding,
        )

        noise_mask[
            remove_top:remove_bottom,
            remove_left:remove_right,
        ] = 255

        removed_circular_objects += 1

    # ---------------------------------------------------------------
    # A2. Remove thick red blobs even when FUSED with the boundary line.
    #
    # A red hospital/POI pin that touches the outline becomes one
    # connected component with it, so the circularity test in step A
    # never fires and the contour detours around the icon (loops at
    # Yashoda Hospital, NCR Hospital, Avee Hospital in Vaishali runs).
    #
    # The boundary stroke is only a few pixels wide. An erosion wider
    # than the stroke deletes the line everywhere and leaves only the
    # solid icon cores behind, regardless of connectivity.
    # ---------------------------------------------------------------
    # Google POI pins are red RINGS with a white glyph centre (H, cross).
    # A hollow ring erodes away completely, so close the centre first —
    # detection runs on this probe only, the working mask is untouched.
    solid_icon_probe = cv2.morphologyEx(
        raw_red_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (13, 13),
        ),
    )

    thick_core = cv2.erode(
        solid_icon_probe,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (9, 9),
        ),
    )

    (
        core_count,
        _core_labels,
        core_stats,
        _core_centroids,
    ) = cv2.connectedComponentsWithStats(
        (thick_core > 0).astype(np.uint8),
        connectivity=8,
    )

    thick_blob_zones = []
    for core_id in range(1, core_count):
        core_w = int(core_stats[core_id, cv2.CC_STAT_WIDTH])
        core_h = int(core_stats[core_id, cv2.CC_STAT_HEIGHT])
        core_area = int(core_stats[core_id, cv2.CC_STAT_AREA])
        # An icon core after 9x9 erosion is a small compact blob. A
        # boundary section that is genuinely thick would instead leave a
        # long thin core and fail the size test.
        if core_w <= 34 and core_h <= 34 and core_area >= 4:
            core_x = int(core_stats[core_id, cv2.CC_STAT_LEFT])
            core_y = int(core_stats[core_id, cv2.CC_STAT_TOP])
            thick_blob_zones.append(
                (core_x, core_y, core_w, core_h)
            )

    # Safety valve: if the outline itself is rendered thick, erosion
    # fragments it into many "cores". Removing them would destroy the
    # line, so the step only applies for a plausible icon count.
    if 0 < len(thick_blob_zones) <= 12:
        blob_padding = 9
        for core_x, core_y, core_w, core_h in thick_blob_zones:
            noise_mask[
                max(0, core_y - blob_padding):
                min(height, core_y + core_h + blob_padding),
                max(0, core_x - blob_padding):
                min(width, core_x + core_w + blob_padding),
            ] = 255
        log(
            "   [CV Clean] Thick red blobs fused with the line "
            f"removed: {len(thick_blob_zones)}"
        )
        thick_blob_bridge_zones = list(thick_blob_zones)
    elif len(thick_blob_zones) > 12:
        thick_blob_bridge_zones = []
        log(
            "   [CV Clean] Thick-blob removal skipped: "
            f"{len(thick_blob_zones)} cores suggest a thick outline, "
            "not icons"
        )
    else:
        thick_blob_bridge_zones = []

    # ---------------------------------------------------------------
    # B. Remove red labels, filled blocks and compact map symbols.
    #
    # Closing horizontally groups neighbouring letters into one text box.
    # A real boundary line usually remains thin and has a much lower box
    # fill ratio than a label or filled POI object.
    # ---------------------------------------------------------------
    text_probe = cv2.morphologyEx(
        raw_red_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (11, 3),
        ),
        iterations=1,
    )

    (
        text_component_count,
        text_component_labels,
        text_component_stats,
        _,
    ) = cv2.connectedComponentsWithStats(
        (
            text_probe > 0
        ).astype(
            np.uint8
        ),
        connectivity=8,
    )

    for component_id in range(
        1,
        text_component_count,
    ):
        object_x = int(
            text_component_stats[
                component_id,
                cv2.CC_STAT_LEFT,
            ]
        )
        object_y = int(
            text_component_stats[
                component_id,
                cv2.CC_STAT_TOP,
            ]
        )
        object_width = int(
            text_component_stats[
                component_id,
                cv2.CC_STAT_WIDTH,
            ]
        )
        object_height = int(
            text_component_stats[
                component_id,
                cv2.CC_STAT_HEIGHT,
            ]
        )
        object_area = int(
            text_component_stats[
                component_id,
                cv2.CC_STAT_AREA,
            ]
        )

        box_area = max(
            1,
            object_width
            * object_height,
        )

        fill_ratio = (
            object_area
            / box_area
        )

        width_to_height = (
            object_width
            / max(
                object_height,
                1,
            )
        )

        compact_symbol = (
            12 <= object_width <= 55
            and 12 <= object_height <= 55
            and object_area >= 100
            and fill_ratio >= 0.42
        )

        text_block = (
            object_width >= 25
            and 10 <= object_height <= 45
            and width_to_height >= 1.50
            and object_area >= 140
            and fill_ratio >= 0.24
        )

        filled_object = (
            object_area >= 260
            and 10 <= object_height <= 70
            and 10 <= object_width <= 80
            and fill_ratio >= 0.30
        )

        if not (
            compact_symbol
            or text_block
            or filled_object
        ):
            continue

        padding = 3

        remove_left = max(
            0,
            object_x - padding,
        )
        remove_top = max(
            0,
            object_y - padding,
        )
        remove_right = min(
            width,
            object_x
            + object_width
            + padding,
        )
        remove_bottom = min(
            height,
            object_y
            + object_height
            + padding,
        )

        noise_mask[
            remove_top:remove_bottom,
            remove_left:remove_right,
        ] = 255

        removed_text_or_filled_objects += 1

    red_mask = cv2.bitwise_and(
        raw_red_mask,
        cv2.bitwise_not(
            noise_mask
        ),
    )

    # ---------------------------------------------------------------
    # A2b. Bridge the hole left by each removed fused icon.
    #
    # Wiping a fused icon cuts a rectangular hole in the boundary line.
    # If the hole is left to the generic gap machinery, the repair can
    # skirt the rectangle and produce a small square notch in the final
    # polygon. The line's entry and exit points on the hole perimeter
    # are the two red pixels farthest apart in a thin band around the
    # removal box — connect them with a straight stroke.
    # ---------------------------------------------------------------
    bridged_icon_holes = 0
    for core_x, core_y, core_w, core_h in thick_blob_bridge_zones:
        blob_padding = 9
        band_left = max(0, core_x - blob_padding - 4)
        band_top = max(0, core_y - blob_padding - 4)
        band_right = min(width, core_x + core_w + blob_padding + 4)
        band_bottom = min(height, core_y + core_h + blob_padding + 4)

        band = np.zeros_like(red_mask)
        band[band_top:band_bottom, band_left:band_right] = 255
        band[
            max(0, core_y - blob_padding):
            min(height, core_y + core_h + blob_padding),
            max(0, core_x - blob_padding):
            min(width, core_x + core_w + blob_padding),
        ] = 0

        band_points = cv2.findNonZero(
            cv2.bitwise_and(red_mask, band)
        )
        if band_points is None or len(band_points) < 2:
            continue

        pts = band_points.reshape(-1, 2).astype(np.float64)
        # farthest pair = line entry and exit around the hole
        from_hull = cv2.convexHull(
            pts.astype(np.int32)
        ).reshape(-1, 2)
        best_pair = None
        best_dist = 0.0
        for i in range(len(from_hull)):
            for j in range(i + 1, len(from_hull)):
                d = float(np.hypot(
                    from_hull[i][0] - from_hull[j][0],
                    from_hull[i][1] - from_hull[j][1],
                ))
                if d > best_dist:
                    best_dist = d
                    best_pair = (from_hull[i], from_hull[j])

        box_diagonal = math.hypot(
            band_right - band_left,
            band_bottom - band_top,
        )
        if best_pair is None or best_dist < 6.0 or best_dist > box_diagonal + 4:
            continue

        cv2.line(
            red_mask,
            (int(best_pair[0][0]), int(best_pair[0][1])),
            (int(best_pair[1][0]), int(best_pair[1][1])),
            255,
            3,
            cv2.LINE_AA,
        )
        bridged_icon_holes += 1

    if bridged_icon_holes:
        log(
            "   [CV Clean] Icon holes bridged with straight "
            f"strokes: {bridged_icon_holes}"
        )

    # ---------------------------------------------------------------
    # A2c. Propagate the cleanup into the RAW red evidence.
    #
    # Downstream stages treat raw_red_mask as "visible Google red":
    # the exact-shape refine snaps vertices to its nearest pixels and
    # the gap/corridor machinery uses it as support. If icon and label
    # pixels stay inside it, the exact stage snaps the contour straight
    # back onto the removed icons and recreates the detours that steps
    # A/A2/B just eliminated. Strip the removed zones from the raw
    # evidence too, and add the icon bridges so the repaired path
    # counts as supported.
    # ---------------------------------------------------------------
    raw_red_mask = cv2.bitwise_and(
        raw_red_mask,
        cv2.bitwise_not(
            noise_mask
        ),
    )
    raw_red_mask = cv2.bitwise_or(
        raw_red_mask,
        red_mask,
    )

    # ---------------------------------------------------------------
    # C. Remove very small components that have no nearby red support.
    #
    # Dots belonging to the locality outline normally have neighbouring
    # dots. A tiny object with no neighbour within 18 pixels is noise.
    # ---------------------------------------------------------------
    (
        clean_component_count,
        clean_component_labels,
        clean_component_stats,
        clean_component_centroids,
    ) = cv2.connectedComponentsWithStats(
        (
            red_mask > 0
        ).astype(
            np.uint8
        ),
        connectivity=8,
    )

    clean_component_ids = list(
        range(
            1,
            clean_component_count,
        )
    )

    if len(
        clean_component_ids
    ) > 1:
        centroid_array = np.array(
            [
                clean_component_centroids[
                    component_id
                ]
                for component_id
                in clean_component_ids
            ],
            dtype=np.float32,
        )

        centroid_difference = (
            centroid_array[
                :,
                None,
                :,
            ]
            - centroid_array[
                None,
                :,
                :,
            ]
        )

        centroid_distances = np.sqrt(
            np.sum(
                centroid_difference
                * centroid_difference,
                axis=2,
            )
        )

        neighbour_counts = np.sum(
            (
                centroid_distances <= 18.0
            )
            & (
                centroid_distances > 0.0
            ),
            axis=1,
        )

        for index, component_id in enumerate(
            clean_component_ids
        ):
            component_area = int(
                clean_component_stats[
                    component_id,
                    cv2.CC_STAT_AREA,
                ]
            )

            is_isolated_noise = (
                component_area <= 25
                and int(
                    neighbour_counts[
                        index
                    ]
                ) == 0
            )

            if not is_isolated_noise:
                continue

            red_mask[
                clean_component_labels
                == component_id
            ] = 0

            removed_isolated_objects += 1

    removed_noise_debug_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_noise_removed.png"
    )

    cv2.imwrite(
        removed_noise_debug_path,
        noise_mask,
    )

    cleaned_red_debug_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_pixels_cleaned.png"
    )

    cv2.imwrite(
        cleaned_red_debug_path,
        red_mask,
    )

    satellite_label_occlusion_mask = np.zeros_like(
        red_mask
    )

    satellite_label_occlusion_diagnostics = {
        "enabled": False,
        "pixel_count": 0,
    }

    if (
        ACTIVE_SATELLITE_VIEW
        and SATELLITE_LABEL_OCCLUSION_REPAIR_ENABLED
    ):
        (
            satellite_label_occlusion_mask,
            satellite_label_occlusion_diagnostics,
        ) = _build_satellite_label_occlusion_mask(
            image=image,
            boundary_mask=raw_red_mask,
            roi_bounds=normal_roi_bounds,
            removed_noise_mask=noise_mask,
            screenshot_path=screenshot_path,
            log=log,
        )

    raw_pixel_count = int(
        cv2.countNonZero(
            red_mask
        )
    )

    strict_red_mask = cv2.bitwise_and(
        full_strict_red_mask,
        roi_mask,
    )

    (
        boundary_presence,
        boundary_presence_diagnostics,
    ) = _boundary_presence_gate(
        full_red_mask=full_red_mask,
        full_strict_red_mask=full_strict_red_mask,
        cleaned_roi_mask=red_mask,
        center=center,
        roi_bounds=normal_roi_bounds,
        screenshot_path=screenshot_path,
        log=log,
    )

    if not ACTIVE_SELECTED_PLACE_TRUSTED:
        boundary_presence = False
        boundary_presence_diagnostics = {
            **boundary_presence_diagnostics,
            "present": False,
            "reason": "selected_place_not_locked",
            "expected_label": ACTIVE_EXPECTED_SELECTED_LABEL,
            "selected_place": dict(LAST_SELECTED_PLACE_LOCK),
        }
        log(
            "   [Boundary Presence] Rejected because the "
            "exact selected-place lock is not trusted"
        )

    if not boundary_presence:
        LAST_CV_DIAGNOSTICS = {
            "status": "rejected",
            "reason": boundary_presence_diagnostics.get(
                "reason",
                "boundary_presence_absent",
            ),
            "screenshot_path": screenshot_path,
            "capture_zoom": float(georef.zoom),
            "target_pixel": {
                "x": int(center[0]),
                "y": int(center[1]),
            },
            "selected_place": dict(LAST_SELECTED_PLACE_LOCK),
            "boundary_presence": dict(boundary_presence_diagnostics),
            "cv_red": dict(red_colour_metrics),
            "satellite_label_occlusion": dict(
                satellite_label_occlusion_diagnostics
            ),
            "debug_images": {
                "red_full_frame": full_red_debug_path,
                "red_strict_full_frame": full_strict_debug_path,
                "red_cleaned": cleaned_red_debug_path,
                "satellite_label_occlusion": (
                    satellite_label_occlusion_diagnostics.get(
                        "debug_image",
                        "",
                    )
                ),
                **dict(
                    boundary_presence_diagnostics.get(
                        "debug_images",
                        {},
                    )
                ),
            },
        }
        log(
            "   [CV] Google Maps did not render a trustworthy "
            "red locality boundary. Graph, polar bootstrap and "
            "synthetic line reconstruction were skipped."
        )
        return None

    graph_red_mask, red_preservation_diagnostics = (
        _build_graph_preservation_mask(
            raw_red_mask=raw_red_mask,
            cleaned_red_mask=red_mask,
            strict_red_mask=strict_red_mask,
            center=center,
            roi_bounds=(
                roi_left,
                roi_top,
                roi_right,
                roi_bottom,
            ),
            profile_name=ACTIVE_LOCALITY_PROFILE,
            image_width=width,
            image_height=height,
        )
    )

    graph_preserved_debug_path = (
        os.path.splitext(screenshot_path)[0]
        + "_red_pixels_graph_preserved.png"
    )
    cv2.imwrite(graph_preserved_debug_path, graph_red_mask)

    (
        bootstrap_roi_bounds,
        bootstrap_roi_diagnostics,
    ) = _full_frame_bootstrap_bounds(
        full_red_mask=full_red_mask,
        center=center,
        normal_roi_bounds=normal_roi_bounds,
    )

    bootstrap_left, bootstrap_top, bootstrap_right, bootstrap_bottom = (
        bootstrap_roi_bounds
    )

    bootstrap_roi_mask = np.zeros_like(
        full_red_mask
    )

    bootstrap_roi_mask[
        bootstrap_top:
        bootstrap_bottom,
        bootstrap_left:
        bootstrap_right,
    ] = 255

    full_bootstrap_red_mask = cv2.bitwise_and(
        full_red_mask,
        bootstrap_roi_mask,
    )

    full_bootstrap_strict_mask = cv2.bitwise_and(
        full_strict_red_mask,
        bootstrap_roi_mask,
    )

    (
        full_bootstrap_graph_mask,
        full_bootstrap_preservation_diagnostics,
    ) = _build_graph_preservation_mask(
        raw_red_mask=full_red_mask,
        cleaned_red_mask=red_mask,
        strict_red_mask=full_strict_red_mask,
        center=center,
        roi_bounds=bootstrap_roi_bounds,
        profile_name=ACTIVE_LOCALITY_PROFILE,
        image_width=width,
        image_height=height,
    )

    full_bootstrap_graph_debug_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_pixels_graph_preserved_full_frame.png"
    )

    cv2.imwrite(
        full_bootstrap_graph_debug_path,
        full_bootstrap_graph_mask,
    )

    bootstrap_window_red_pixels = int(
        cv2.countNonZero(
            full_bootstrap_red_mask
        )
    )

    bootstrap_window_strict_pixels = int(
        cv2.countNonZero(
            full_bootstrap_strict_mask
        )
    )

    bootstrap_window_graph_pixels = int(
        cv2.countNonZero(
            full_bootstrap_graph_mask
        )
    )

    log(
        "   [Full Bootstrap] Evidence window: "
        f"x={bootstrap_left}:{bootstrap_right}, "
        f"y={bootstrap_top}:{bootstrap_bottom}, "
        f"red={bootstrap_window_red_pixels}, "
        f"strict={bootstrap_window_strict_pixels}, "
        f"graph={bootstrap_window_graph_pixels}"
    )

    cleaned_retention_ratio = float(
        red_preservation_diagnostics.get(
            "cleaned_retention_ratio",
            0.0,
        )
        or 0.0
    )

    polar_bootstrap_triggered = bool(
        PRESERVED_POLAR_BOOTSTRAP_ENABLED
        and (
            cleaned_retention_ratio
            < PRESERVED_POLAR_BOOTSTRAP_TRIGGER_RATIO
            or raw_pixel_count
            < PRESERVED_POLAR_BOOTSTRAP_MIN_CLEAN_PIXELS
        )
    )

    polar_red_mask = red_mask
    graph_guide_mask = red_mask

    polar_bootstrap_diagnostics = {
        "enabled": bool(
            PRESERVED_POLAR_BOOTSTRAP_ENABLED
        ),
        "accepted": False,
        "triggered": bool(
            polar_bootstrap_triggered
        ),
        "reason": (
            "not_required"
            if not polar_bootstrap_triggered
            else "pending"
        ),
    }

    if polar_bootstrap_triggered:
        (
            polar_bootstrap_mask,
            polar_bootstrap_diagnostics,
        ) = _build_preserved_polar_bootstrap(
            cleaned_mask=red_mask,
            strict_mask=full_bootstrap_strict_mask,
            preserved_mask=full_bootstrap_graph_mask,
            center=center,
            roi_bounds=bootstrap_roi_bounds,
            profile_name=ACTIVE_LOCALITY_PROFILE,
            screenshot_path=screenshot_path,
            log=log,
        )

        polar_bootstrap_diagnostics[
            "triggered"
        ] = True

        if polar_bootstrap_mask is not None:
            polar_red_mask = polar_bootstrap_mask
            graph_guide_mask = polar_bootstrap_mask

            (
                effective_roi_bounds,
                effective_roi_diagnostics,
            ) = _bootstrap_validation_bounds(
                bootstrap_mask=polar_bootstrap_mask,
                normal_roi_bounds=normal_roi_bounds,
                center=center,
            )

            (
                roi_left,
                roi_top,
                roi_right,
                roi_bottom,
            ) = effective_roi_bounds

            roi_mask = np.zeros_like(
                full_red_mask
            )

            roi_mask[
                roi_top:
                roi_bottom,
                roi_left:
                roi_right,
            ] = 255

            # Downstream graph support and red snapping must use the original
            # full-frame Google-red pixels inside the effective validation ROI.
            raw_red_mask = cv2.bitwise_and(
                full_red_mask,
                roi_mask,
            )

            strict_red_mask = cv2.bitwise_and(
                full_strict_red_mask,
                roi_mask,
            )

            graph_red_mask = cv2.bitwise_and(
                full_bootstrap_graph_mask,
                roi_mask,
            )

            cv2.imwrite(
                graph_preserved_debug_path,
                graph_red_mask,
            )

            polar_bootstrap_diagnostics[
                "full_frame_bootstrap"
            ] = {
                "roi": dict(
                    bootstrap_roi_diagnostics
                ),
                "preservation": dict(
                    full_bootstrap_preservation_diagnostics
                ),
                "validation_roi": dict(
                    effective_roi_diagnostics
                ),
                "full_red_pixels": int(
                    cv2.countNonZero(
                        full_red_mask
                    )
                ),
                "window_red_pixels": int(
                    bootstrap_window_red_pixels
                ),
                "window_strict_pixels": int(
                    bootstrap_window_strict_pixels
                ),
                "window_graph_pixels": int(
                    bootstrap_window_graph_pixels
                ),
            }

            log(
                "   [Full Bootstrap] Effective validation ROI: "
                f"x={roi_left}:{roi_right}, "
                f"y={roi_top}:{roi_bottom}"
            )

            log(
                "   [Polar Bootstrap] Continuous red-line "
                "guide will replace the empty/sparse clean mask"
            )

        else:
            bootstrap_reason = str(
                polar_bootstrap_diagnostics.get(
                    "reason",
                    "unknown_reason",
                )
            )

            bootstrap_coverage = float(
                polar_bootstrap_diagnostics.get(
                    "coverage",
                    0.0,
                )
                or 0.0
            )

            bootstrap_valid_bins = int(
                polar_bootstrap_diagnostics.get(
                    "valid_bins",
                    0,
                )
                or 0
            )

            log(
                "   [Polar Bootstrap] Bootstrap guide could "
                "not be built: "
                f"reason={bootstrap_reason}, "
                f"coverage={bootstrap_coverage:.3f}, "
                f"valid_bins={bootstrap_valid_bins}; "
                "preserved graph mode remains available"
            )

    log(
        "   [CV Preserve] cleaned/raw="
        f"{red_preservation_diagnostics['cleaned_retention_ratio']:.3f}, "
        "graph/raw="
        f"{red_preservation_diagnostics['graph_retention_ratio']:.3f}, "
        "restored="
        f"{red_preservation_diagnostics['restored_pixels']} pixels"
    )

    if red_preservation_diagnostics.get("triggered", False):
        log(
            "   [CV Preserve] Low red retention triggered the "
            "graph-specific preservation mask"
        )

    log(
        "   [CV Clean] Circular icons removed: "
        f"{removed_circular_objects}"
    )
    log(
        "   [CV Clean] Text/filled objects removed: "
        f"{removed_text_or_filled_objects}"
    )
    log(
        "   [CV Clean] Isolated objects removed: "
        f"{removed_isolated_objects}"
    )
    log(
        "   [CV Clean] Remaining boundary pixels: "
        f"{raw_pixel_count}"
    )

    log(
        "   [CV] Google-red pixels after cleaning: "
        f"{raw_pixel_count}"
    )

    LAST_CV_DIAGNOSTICS = {
        "status": "red_mask_cleaned",
        "reason": "",
        "screenshot_path": screenshot_path,
        "zoom": float(
            georef.zoom
        ),
        "target_pixel": {
            "x": int(
                center[0]
            ),
            "y": int(
                center[1]
            ),
        },
        "roi": {
            "left": int(
                roi_left
            ),
            "top": int(
                roi_top
            ),
            "right": int(
                roi_right
            ),
            "bottom": int(
                roi_bottom
            ),
        },
        "cv_red": dict(
            red_colour_metrics
        ),
        "boundary_presence": dict(
            boundary_presence_diagnostics
        ),
        "selected_place": dict(
            LAST_SELECTED_PLACE_LOCK
        ),
        "full_frame_bootstrap": {
            "enabled": bool(
                FULL_FRAME_BOOTSTRAP_ENABLED
            ),
            "full_red_pixels": int(
                cv2.countNonZero(
                    full_red_mask
                )
            ),
            "full_strict_pixels": int(
                cv2.countNonZero(
                    full_strict_red_mask
                )
            ),
            "normal_roi_red_pixels": int(
                normal_roi_red_pixels
            ),
            "bootstrap_window": dict(
                bootstrap_roi_diagnostics
            ),
            "bootstrap_window_red_pixels": int(
                bootstrap_window_red_pixels
            ),
            "bootstrap_window_strict_pixels": int(
                bootstrap_window_strict_pixels
            ),
            "bootstrap_window_graph_pixels": int(
                bootstrap_window_graph_pixels
            ),
            "bootstrap_preservation": dict(
                full_bootstrap_preservation_diagnostics
            ),
        },
        "cleaning": {
            "remaining_boundary_pixels": raw_pixel_count,
            "raw_roi_pixels": int(
                cv2.countNonZero(raw_red_mask)
            ),
            "red_preservation": dict(
                red_preservation_diagnostics
            ),
            "removed_circular_objects": int(
                removed_circular_objects
            ),
            "removed_text_or_filled_objects": int(
                removed_text_or_filled_objects
            ),
            "removed_isolated_objects": int(
                removed_isolated_objects
            ),
        },
        "debug_images": {
            "red_raw": raw_red_debug_path,
            "noise_removed": removed_noise_debug_path,
            "red_cleaned": cleaned_red_debug_path,
            "red_graph_preserved": graph_preserved_debug_path,
            "red_full_frame": full_red_debug_path,
            "red_strict_full_frame": full_strict_debug_path,
            "red_graph_preserved_full_frame": (
                full_bootstrap_graph_debug_path
            ),
        },
    }

    LAST_CV_DIAGNOSTICS[
        "polar_bootstrap"
    ] = dict(
        polar_bootstrap_diagnostics
    )

    raw_debug_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_pixels.png"
    )

    cv2.imwrite(
        raw_debug_path,
        red_mask,
    )

    graph_pixel_count = int(
        cv2.countNonZero(graph_red_mask)
    )

    bootstrap_source_count = int(
        polar_bootstrap_diagnostics.get(
            "source_pixels",
            0,
        )
        or 0
    )

    bootstrap_accepted = bool(
        polar_bootstrap_diagnostics.get(
            "accepted",
            False,
        )
    )

    log(
        "   [Full Bootstrap] Final evidence counts: "
        f"clean={raw_pixel_count}, "
        f"graph={graph_pixel_count}, "
        f"bootstrap_source={bootstrap_source_count}, "
        f"bootstrap_accepted={bootstrap_accepted}"
    )

    if (
        raw_pixel_count < 45
        and graph_pixel_count < 60
        and not bootstrap_accepted
    ):
        log(
            "   [CV] Too few usable red boundary pixels were "
            "available even after preservation"
        )

        LAST_CV_DIAGNOSTICS.update({
            "status": "rejected",
            "reason": "too_few_clean_and_preserved_red_pixels",
            "minimum_required_clean_red_pixels": 45,
            "minimum_required_graph_red_pixels": 60,
            "graph_red_pixels": graph_pixel_count,
        })

        return None

    if raw_pixel_count < 45:
        log(
            "   [CV Preserve] Clean mask is sparse, but the "
            "preserved graph mask has enough red evidence"
        )

    # ------------------------------------------------------------------
    # PRIORITY 2: CLOSE SHORT GAPS, THEN COMPLETE LARGE GAPS SMOOTHLY
    # ------------------------------------------------------------------
    # Do not connect distant endpoints with straight lines. Those bridges
    # created the incorrect V-shaped and diagonal edges seen in the KML.
    #
    # First, morphology joins only nearby Google-red dots. If the outline is
    # still open, the missing angular sections are completed in polar space
    # around the searched-place centre. Polar interpolation creates a smooth
    # boundary arc rather than a long straight shortcut.

    short_gap = max(
        9,
        int(
            round(
                min(
                    width,
                    height,
                )
                * 0.018
            )
        ),
    )

    if short_gap % 2 == 0:
        short_gap += 1

    horizontal = cv2.morphologyEx(
        red_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (short_gap, 3),
        ),
        iterations=1,
    )

    vertical = cv2.morphologyEx(
        red_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (3, short_gap),
        ),
        iterations=1,
    )

    boundary_joined = cv2.bitwise_or(
        horizontal,
        vertical,
    )

    boundary_joined = cv2.morphologyEx(
        boundary_joined,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (7, 7),
        ),
        iterations=1,
    )

    boundary_joined = cv2.dilate(
        boundary_joined,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (3, 3),
        ),
        iterations=1,
    )

    joined_debug_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_outline_joined.png"
    )

    cv2.imwrite(
        joined_debug_path,
        boundary_joined,
    )

    edge_margin = max(
        4,
        int(
            round(
                min(
                    width,
                    height,
                )
                * 0.006
            )
        ),
    )

    selected_contour = None
    diagnostics = None

    # ---------------------------------------------------------------
    # Attempt A: use a genuinely enclosed region when short-gap joining
    # already completed the red dotted boundary.
    # ---------------------------------------------------------------
    if boundary_joined[
        center[1],
        center[0],
    ] == 0:
        walkable = cv2.bitwise_not(
            boundary_joined
        )

        flood_image = walkable.copy()
        flood_mask = np.zeros(
            (
                height + 2,
                width + 2,
            ),
            dtype=np.uint8,
        )

        cv2.floodFill(
            flood_image,
            flood_mask,
            seedPoint=center,
            newVal=128,
            loDiff=0,
            upDiff=0,
            flags=4,
        )

        enclosed_region = (
            flood_image == 128
        ).astype(
            np.uint8
        ) * 255

        border_pixels = int(
            cv2.countNonZero(
                enclosed_region[0, :]
            )
            + cv2.countNonZero(
                enclosed_region[-1, :]
            )
            + cv2.countNonZero(
                enclosed_region[:, 0]
            )
            + cv2.countNonZero(
                enclosed_region[:, -1]
            )
        )

        region_pixels = int(
            cv2.countNonZero(
                enclosed_region
            )
        )

        region_ratio = (
            region_pixels
            / image_area
        )

        flood_debug_path = (
            os.path.splitext(
                screenshot_path
            )[0]
            + "_red_flood_region.png"
        )

        cv2.imwrite(
            flood_debug_path,
            enclosed_region,
        )

        log(
            "   [CV Join] Short-gap flood result: "
            f"area_ratio={region_ratio:.4f}, "
            f"border_pixels={border_pixels}"
        )

        if (
            border_pixels == 0
            and minimum_area_ratio
            <= region_ratio
            <= maximum_area_ratio
        ):
            flood_contours, _ = cv2.findContours(
                enclosed_region,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_TC89_KCOS,
            )

            flood_candidates = []

            for contour in flood_contours:
                if cv2.pointPolygonTest(
                    contour,
                    (
                        float(center[0]),
                        float(center[1]),
                    ),
                    False,
                ) < 0:
                    continue

                x, y, box_width, box_height = cv2.boundingRect(
                    contour
                )

                inside_roi = (
                    x >= roi_left - edge_margin
                    and y >= roi_top - edge_margin
                    and x + box_width
                    <= roi_right + edge_margin
                    and y + box_height
                    <= roi_bottom + edge_margin
                )

                if inside_roi:
                    flood_candidates.append(
                        contour
                    )

            if flood_candidates:
                selected_contour = max(
                    flood_candidates,
                    key=cv2.contourArea,
                )

                diagnostics = {
                    "method": "short_gap_closed_outline",
                    "area_ratio": round(
                        region_ratio,
                        4,
                    ),
                    "border_pixels": border_pixels,
                }

    # ---------------------------------------------------------------
    # Attempt B: directional graph reconstruction for irregular outlines.
    # ---------------------------------------------------------------
    if selected_contour is None and DIRECTIONAL_GRAPH_ENABLED:
        graph_contour, graph_diagnostics = _directional_boundary_graph_reconstruct(
            red_mask=graph_red_mask,
            raw_red_mask=raw_red_mask,
            center=center,
            roi_bounds=(roi_left, roi_top, roi_right, roi_bottom),
            image_area=image_area,
            minimum_area_ratio=minimum_area_ratio,
            maximum_area_ratio=maximum_area_ratio,
            screenshot_path=screenshot_path,
            guide_mask=graph_guide_mask,
            log=log,
        )
        LAST_CV_DIAGNOSTICS['directional_graph'] = graph_diagnostics
        if graph_contour is not None:
            selected_contour = graph_contour
            diagnostics = graph_diagnostics

    # ---------------------------------------------------------------
    # Attempt C: smooth polar completion for remaining large gaps.
    # ---------------------------------------------------------------
    if selected_contour is None:
        red_y, red_x = np.where(
            polar_red_mask > 0
        )

        # Reconstruct radii from the centre of the red evidence. The searched
        # place marker is still used for every containment and safety check
        # below, so this only changes how the outline is sampled.
        (
            polar_center,
            polar_center_diagnostics,
        ) = _evidence_polar_center(
            evidence_mask=polar_red_mask,
            target_center=center,
            roi_bounds=(
                roi_left,
                roi_top,
                roi_right,
                roi_bottom,
            ),
            log=log,
        )

        LAST_CV_DIAGNOSTICS[
            "polar_center"
        ] = dict(
            polar_center_diagnostics
        )

        delta_x = (
            red_x.astype(
                np.float32
            )
            - float(polar_center[0])
        )
        delta_y = (
            red_y.astype(
                np.float32
            )
            - float(polar_center[1])
        )

        radii = np.hypot(
            delta_x,
            delta_y,
        )

        angles = (
            np.arctan2(
                delta_y,
                delta_x,
            )
            + 2.0 * math.pi
        ) % (
            2.0 * math.pi
        )

        (
            minimum_radius,
            maximum_radius,
        ) = _adaptive_radius_limits(
            ACTIVE_LOCALITY_PROFILE,
            width,
            height,
            roi_half_width,
            roi_half_height,
        )

        valid_red_points = (
            (radii >= minimum_radius)
            & (radii <= maximum_radius)
        )

        radii = radii[
            valid_red_points
        ]
        angles = angles[
            valid_red_points
        ]

        if radii.size >= 60:
            # 270 bins (1.33 deg) instead of 180 (2 deg): finer tracing of
            # Google's dotted line, smaller interpolated gaps at notches.
            bin_count = 270

            bin_indices = np.floor(
                angles
                / (2.0 * math.pi)
                * bin_count
            ).astype(
                np.int32
            ) % bin_count

            radial_samples = np.full(
                bin_count,
                np.nan,
                dtype=np.float32,
            )

            radial_support = np.zeros(
                bin_count,
                dtype=np.int32,
            )

            for bin_index in range(
                bin_count
            ):
                bin_values = radii[
                    bin_indices == bin_index
                ]

                if bin_values.size == 0:
                    continue

                # When a non-star-convex locality has protruding lobes or
                # multiple red corridors along the same angular ray (such as
                # an inner highway and an outer street corridor), group radial points
                # into clusters (split by gaps > 25px) and select the outermost
                # cluster median so the true outer boundary is never shortcut.
                sorted_values = np.sort(bin_values)
                cluster_gaps = np.where(np.diff(sorted_values) > 25.0)[0]
                radial_clusters = np.split(sorted_values, cluster_gaps + 1)
                outer_cluster = radial_clusters[-1]

                radial_samples[
                    bin_index
                ] = float(
                    np.median(
                        outer_cluster
                    )
                )

                radial_support[
                    bin_index
                ] = int(
                    outer_cluster.size
                )

            original_samples = radial_samples.copy()

            # Reject isolated radial spikes, such as a remaining circular POI
            # far from the local outline. Real corners normally have support
            # in several neighbouring angular bins.
            rejected_bins = 0

            for bin_index in range(
                bin_count
            ):
                sample = original_samples[
                    bin_index
                ]

                if np.isnan(
                    sample
                ):
                    continue

                neighbouring_values = []

                for offset in range(
                    -5,
                    6,
                ):
                    if offset == 0:
                        continue

                    value = original_samples[
                        (
                            bin_index
                            + offset
                        ) % bin_count
                    ]

                    if not np.isnan(
                        value
                    ):
                        neighbouring_values.append(
                            float(value)
                        )

                if len(
                    neighbouring_values
                ) < 4:
                    continue

                local_median = float(
                    np.median(
                        neighbouring_values
                    )
                )

                deviation = abs(
                    float(sample)
                    - local_median
                )

                moderate_limit = max(
                    45.0,
                    local_median * 0.22,
                )

                severe_limit = max(
                    80.0,
                    local_median * 0.35,
                )

                # Real locality appendages (like road corridors and protruding lobes) can have
                # large radial extensions but have genuine pixel support across bins. Only
                # reject an outlier if it is an isolated low-support spike (e.g. stray icon).
                low_support_spike = (
                    radial_support[
                        bin_index
                    ] <= 2
                    and deviation
                    > moderate_limit
                )

                if low_support_spike:
                    radial_samples[
                        bin_index
                    ] = np.nan
                    rejected_bins += 1

            valid_bins = np.where(
                ~np.isnan(
                    radial_samples
                )
            )[0]

            coverage_ratio = (
                valid_bins.size
                / bin_count
            )

            log(
                "   [CV Complete] Polar coverage: "
                f"{coverage_ratio:.2f}; "
                f"outlier bins removed: {rejected_bins}"
            )

            if coverage_ratio >= 0.42:
                valid_values = radial_samples[
                    valid_bins
                ]

                extended_bins = np.concatenate(
                    (
                        valid_bins - bin_count,
                        valid_bins,
                        valid_bins + bin_count,
                    )
                )

                extended_values = np.concatenate(
                    (
                        valid_values,
                        valid_values,
                        valid_values,
                    )
                )

                completed_radii = np.interp(
                    np.arange(
                        bin_count,
                        dtype=np.float32,
                    ),
                    extended_bins,
                    extended_values,
                ).astype(
                    np.float32
                )

                def circular_filter(
                    values: np.ndarray,
                    window_size: int,
                    use_median: bool,
                ) -> np.ndarray:
                    padding = (
                        window_size // 2
                    )

                    extended = np.concatenate(
                        (
                            values[-padding:],
                            values,
                            values[:padding],
                        )
                    )

                    filtered_values = []

                    for value_index in range(
                        len(values)
                    ):
                        window = extended[
                            value_index:
                            value_index
                            + window_size
                        ]

                        if use_median:
                            filtered_values.append(
                                float(
                                    np.median(
                                        window
                                    )
                                )
                            )
                        else:
                            filtered_values.append(
                                float(
                                    np.mean(
                                        window
                                    )
                                )
                            )

                    return np.array(
                        filtered_values,
                        dtype=np.float32,
                    )

                completed_radii = circular_filter(
                    completed_radii,
                    5,
                    True,
                )

                smooth_radii = circular_filter(
                    completed_radii,
                    7,
                    False,
                )

                # Keep real red-supported sections close to their detected
                # radii. Interpolated missing sections remain smooth curves.
                snapped_radii = smooth_radii.copy()

                for bin_index in valid_bins:
                    snapped_radii[
                        bin_index
                    ] = (
                        0.68
                        * radial_samples[
                            bin_index
                        ]
                        + 0.32
                        * smooth_radii[
                            bin_index
                        ]
                    )

                snapped_radii = circular_filter(
                    snapped_radii,
                    5,
                    False,
                )

                # ------------------------------------------------------
                # PRIORITY 3: SNAP THE COMPLETED OUTLINE BACK TO RED
                # ------------------------------------------------------
                # Priority 1 intentionally removed text/icons from red_mask.
                # Some genuine boundary dots may also have been removed where
                # they overlap labels. Use raw_red_mask only inside a narrow
                # annulus around the smooth contour, and select the outer,
                # angularly supported red pixels. This tightens the final
                # polygon without bringing distant POI text back into it.

                raw_red_y, raw_red_x = np.where(
                    raw_red_mask > 0
                )

                raw_delta_x = (
                    raw_red_x.astype(
                        np.float32
                    )
                    - float(
                        polar_center[0]
                    )
                )

                raw_delta_y = (
                    raw_red_y.astype(
                        np.float32
                    )
                    - float(
                        polar_center[1]
                    )
                )

                raw_radii = np.hypot(
                    raw_delta_x,
                    raw_delta_y,
                )

                raw_angles = (
                    np.arctan2(
                        raw_delta_y,
                        raw_delta_x,
                    )
                    + 2.0
                    * math.pi
                ) % (
                    2.0
                    * math.pi
                )

                raw_bin_indices = np.floor(
                    raw_angles
                    / (
                        2.0
                        * math.pi
                    )
                    * bin_count
                ).astype(
                    np.int32
                ) % bin_count

                red_snap_samples = np.full(
                    bin_count,
                    np.nan,
                    dtype=np.float32,
                )

                red_snap_support = np.zeros(
                    bin_count,
                    dtype=np.int32,
                )

                for bin_index in range(
                    bin_count
                ):
                    current_radius = float(
                        snapped_radii[
                            bin_index
                        ]
                    )

                    bin_values = raw_radii[
                        raw_bin_indices
                        == bin_index
                    ]

                    if bin_values.size == 0:
                        continue

                    annulus_minimum = max(
                        minimum_radius,
                        current_radius * 0.70,
                        current_radius - 75.0,
                    )

                    annulus_maximum = min(
                        maximum_radius,
                        current_radius * 1.30,
                        current_radius + 75.0,
                    )

                    annulus_values = bin_values[
                        (
                            bin_values
                            >= annulus_minimum
                        )
                        & (
                            bin_values
                            <= annulus_maximum
                        )
                    ]

                    if annulus_values.size == 0:
                        continue

                    # Select the outermost cluster in the annulus
                    sorted_ann = np.sort(annulus_values)
                    ann_gaps = np.where(np.diff(sorted_ann) > 20.0)[0]
                    ann_clusters = np.split(sorted_ann, ann_gaps + 1)
                    outer_ann_cluster = ann_clusters[-1]

                    red_snap_samples[
                        bin_index
                    ] = float(
                        np.median(
                            outer_ann_cluster
                        )
                    )

                    red_snap_support[
                        bin_index
                    ] = int(
                        outer_ann_cluster.size
                    )

                # Remove unsupported radial spikes from the raw-mask samples.
                stable_snap_samples = (
                    red_snap_samples.copy()
                )

                raw_snap_outliers = 0

                for bin_index in range(
                    bin_count
                ):
                    candidate_radius = (
                        red_snap_samples[
                            bin_index
                        ]
                    )

                    if np.isnan(
                        candidate_radius
                    ):
                        continue

                    neighbouring_candidates = []

                    for offset in range(
                        -4,
                        5,
                    ):
                        if offset == 0:
                            continue

                        neighbouring_radius = (
                            red_snap_samples[
                                (
                                    bin_index
                                    + offset
                                )
                                % bin_count
                            ]
                        )

                        if not np.isnan(
                            neighbouring_radius
                        ):
                            neighbouring_candidates.append(
                                float(
                                    neighbouring_radius
                                )
                            )

                    if len(
                        neighbouring_candidates
                    ) < 2:
                        if red_snap_support[
                            bin_index
                        ] <= 2:
                            stable_snap_samples[
                                bin_index
                            ] = np.nan
                            raw_snap_outliers += 1

                        continue

                    local_candidate_median = float(
                        np.median(
                            neighbouring_candidates
                        )
                    )

                    candidate_deviation = abs(
                        float(
                            candidate_radius
                        )
                        - local_candidate_median
                    )

                    candidate_limit = max(
                        35.0,
                        local_candidate_median
                        * 0.14,
                    )

                    if (
                        candidate_deviation
                        > candidate_limit
                        and red_snap_support[
                            bin_index
                        ] <= 4
                    ):
                        stable_snap_samples[
                            bin_index
                        ] = np.nan
                        raw_snap_outliers += 1

                refined_radii = (
                    snapped_radii.copy()
                )

                snapped_bin_count = 0

                for bin_index in range(
                    bin_count
                ):
                    candidate_radius = (
                        stable_snap_samples[
                            bin_index
                        ]
                    )

                    if np.isnan(
                        candidate_radius
                    ):
                        continue

                    current_radius = float(
                        snapped_radii[
                            bin_index
                        ]
                    )

                    radial_change = (
                        float(
                            candidate_radius
                        )
                        - current_radius
                    )

                    # Give outward corrections more weight because the median
                    # polar estimate can be pulled inward by residual labels.
                    snap_weight = (
                        0.88
                        if radial_change >= 0.0
                        else 0.72
                    )

                    refined_radii[
                        bin_index
                    ] = (
                        snap_weight
                        * float(
                            candidate_radius
                        )
                        + (
                            1.0
                            - snap_weight
                        )
                        * current_radius
                    )

                    snapped_bin_count += 1

                # Apply only a light three-bin filter. Supported sections keep
                # their actual corners; missing sections remain smoothly
                # interpolated from Priority 2.
                lightly_filtered_radii = (
                    circular_filter(
                        refined_radii,
                        3,
                        True,
                    )
                )

                supported_snap_bins = np.where(
                    ~np.isnan(
                        stable_snap_samples
                    )
                )[0]

                unsupported_snap_mask = np.ones(
                    bin_count,
                    dtype=bool,
                )

                unsupported_snap_mask[
                    supported_snap_bins
                ] = False

                refined_radii[
                    unsupported_snap_mask
                ] = lightly_filtered_radii[
                    unsupported_snap_mask
                ]

                refined_radii[
                    supported_snap_bins
                ] = (
                    0.90
                    * refined_radii[
                        supported_snap_bins
                    ]
                    + 0.10
                    * lightly_filtered_radii[
                        supported_snap_bins
                    ]
                )

                log(
                    "   [CV Refine] Boundary bins snapped "
                    f"to red pixels: {snapped_bin_count}/"
                    f"{bin_count}; raw outliers removed: "
                    f"{raw_snap_outliers}"
                )

                bin_angles = (
                    (
                        np.arange(
                            bin_count,
                            dtype=np.float32,
                        )
                        + 0.5
                    )
                    * (
                        2.0
                        * math.pi
                        / bin_count
                    )
                )

                point_x = (
                    float(polar_center[0])
                    + refined_radii
                    * np.cos(
                        bin_angles
                    )
                )

                point_y = (
                    float(polar_center[1])
                    + refined_radii
                    * np.sin(
                        bin_angles
                    )
                )

                completed_points = np.column_stack(
                    (
                        point_x,
                        point_y,
                    )
                ).astype(
                    np.int32
                )

                completed_contour = completed_points.reshape(
                    -1,
                    1,
                    2,
                )

                completed_area_ratio = (
                    abs(
                        float(
                            cv2.contourArea(
                                completed_contour
                            )
                        )
                    )
                    / image_area
                )

                (
                    completed_x,
                    completed_y,
                    completed_width,
                    completed_height,
                ) = cv2.boundingRect(
                    completed_contour
                )

                completed_inside_roi = (
                    completed_x
                    >= roi_left - edge_margin
                    and completed_y
                    >= roi_top - edge_margin
                    and completed_x
                    + completed_width
                    <= roi_right + edge_margin
                    and completed_y
                    + completed_height
                    <= roi_bottom + edge_margin
                )

                completed_contains_center = (
                    cv2.pointPolygonTest(
                        completed_contour,
                        (
                            float(center[0]),
                            float(center[1]),
                        ),
                        False,
                    ) >= 0
                )

                adjacent_points = completed_points[
                    np.roll(
                        np.arange(
                            len(
                                completed_points
                            )
                        ),
                        -1,
                    )
                ]

                maximum_adjacent_step = float(
                    np.max(
                        np.hypot(
                            completed_points[:, 0]
                            - adjacent_points[:, 0],
                            completed_points[:, 1]
                            - adjacent_points[:, 1],
                        )
                    )
                )

                # Measure how closely the completed contour follows any
                # original Google-red pixel. Missing sections are allowed,
                # but most supported points should remain near the red border.
                red_distance_input = cv2.bitwise_not(
                    raw_red_mask
                )

                red_distance_map = cv2.distanceTransform(
                    red_distance_input,
                    cv2.DIST_L2,
                    3,
                )

                completed_pixel_x = np.clip(
                    completed_points[:, 0],
                    0,
                    width - 1,
                )

                completed_pixel_y = np.clip(
                    completed_points[:, 1],
                    0,
                    height - 1,
                )

                red_distances = red_distance_map[
                    completed_pixel_y,
                    completed_pixel_x,
                ]

                close_to_red_ratio = float(
                    np.mean(
                        red_distances
                        <= 12.0
                    )
                )

                mean_red_distance = float(
                    np.mean(
                        red_distances
                    )
                )

                strong_red_support = (
                    coverage_ratio
                    >= 0.78
                    and close_to_red_ratio
                    >= 0.85
                    and mean_red_distance
                    <= 8.0
                )

                safe_gap_close_candidate = (
                    SAFE_GAP_CLOSE_ENABLED
                    and coverage_ratio
                    >= SAFE_GAP_CLOSE_MIN_COVERAGE
                    and close_to_red_ratio
                    >= SAFE_GAP_CLOSE_MIN_CLOSE_TO_RED
                    and mean_red_distance
                    <= SAFE_GAP_CLOSE_MAX_MEAN_RED_DISTANCE_PX
                )

                pre_repair_maximum_step = (
                    SAFE_GAP_CLOSE_PRE_REPAIR_MAX_STEP_PX
                    if safe_gap_close_candidate
                    else (
                        HIGH_SUPPORT_PRE_REPAIR_MAX_STEP_PX
                        if strong_red_support
                        else 120.0  # label occlusion and diagonal corridors create ~50-115px gaps
                    )
                )

                # This limit is only a pre-filter. When the contour qualifies
                # for exact repair, the repair stage rewrites long steps and
                # is then validated on its own terms (step after repair, gap
                # red support, gap distance, self-crossings, area change).
                # Rejecting here would discard contours the repair stage can
                # both fix and prove, so the pre-filter is widened for those
                # and the post-repair limits remain unchanged.
                if safe_gap_close_candidate:
                    pre_repair_maximum_step = (
                        pre_repair_maximum_step
                        * SAFE_GAP_CLOSE_PRE_REPAIR_STEP_TOLERANCE
                    )

                requires_exact_repair = (
                    safe_gap_close_candidate
                    and maximum_adjacent_step
                    > SAFE_GAP_CLOSE_REQUIRES_EXACT_ABOVE_STEP_PX
                )

                acceptance_checks = {
                    "area_ratio": (
                        minimum_area_ratio
                        <= completed_area_ratio
                        <= maximum_area_ratio
                    ),
                    "inside_roi": (
                        completed_inside_roi
                    ),
                    "contains_target": (
                        completed_contains_center
                    ),
                    "max_step": (
                        maximum_adjacent_step
                        <= pre_repair_maximum_step
                    ),
                    "close_to_red": (
                        close_to_red_ratio
                        >= 0.38
                    ),
                    "mean_red_distance": (
                        mean_red_distance
                        <= 32.0
                    ),
                }

                failed_checks = [
                    check_name
                    for check_name, passed
                    in acceptance_checks.items()
                    if not passed
                ]

                local_gap_repair_accepted = False
                local_gap_diagnostics = {}

                if set(failed_checks) == {"max_step"}:
                    local_gap_contour, local_gap_diagnostics = (
                        _repair_local_polar_gaps(
                            completed_points=completed_points,
                            raw_red_mask=raw_red_mask,
                            graph_red_mask=graph_red_mask,
                            cleaned_red_mask=red_mask,
                            center=center,
                            roi_bounds=(
                                roi_left,
                                roi_top,
                                roi_right,
                                roi_bottom,
                            ),
                            edge_margin=edge_margin,
                            image_area=image_area,
                            minimum_area_ratio=minimum_area_ratio,
                            maximum_area_ratio=maximum_area_ratio,
                            coverage_ratio=coverage_ratio,
                            close_to_red_ratio=close_to_red_ratio,
                            mean_red_distance_px=mean_red_distance,
                            screenshot_path=screenshot_path,
                            label_occlusion_mask=(
                                satellite_label_occlusion_mask
                            ),
                            log=log,
                        )
                    )

                    LAST_CV_DIAGNOSTICS["local_gap_path"] = (
                        local_gap_diagnostics
                    )

                    if local_gap_contour is not None:
                        local_gap_repair_accepted = True
                        selected_contour = local_gap_contour
                        diagnostics = dict(local_gap_diagnostics)
                        diagnostics.update({
                            "adaptive_profile": ACTIVE_LOCALITY_PROFILE,
                            "minimum_area_ratio": round(minimum_area_ratio, 6),
                            "maximum_area_ratio": round(maximum_area_ratio, 6),
                            "coverage": round(coverage_ratio, 3),
                            "close_to_red_ratio": round(close_to_red_ratio, 3),
                            "mean_red_distance_px": round(mean_red_distance, 2),
                            "snapped_bins": snapped_bin_count,
                            "raw_snap_outliers": raw_snap_outliers,
                            "safe_gap_close_candidate": True,
                            "requires_exact_repair": False,
                        })
                        log(
                            "   [Local Gap] Polar contour preserved; "
                            "only oversized local steps were replaced. "
                            "Cross-zoom confirmation is mandatory."
                        )

                if failed_checks and not local_gap_repair_accepted:
                    LAST_CV_DIAGNOSTICS.update({
                        "status": "candidate_rejected",
                        "reason": "priority3_validation_failed",
                        "best_rejection": {
                            "area_ratio": round(
                                completed_area_ratio,
                                6,
                            ),
                            "minimum_area_ratio": round(
                                minimum_area_ratio,
                                6,
                            ),
                            "maximum_area_ratio": round(
                                maximum_area_ratio,
                                6,
                            ),
                            "inside_roi": bool(
                                completed_inside_roi
                            ),
                            "contains_target": bool(
                                completed_contains_center
                            ),
                            "max_step_px": round(
                                maximum_adjacent_step,
                                3,
                            ),
                            "close_to_red_ratio": round(
                                close_to_red_ratio,
                                4,
                            ),
                            "mean_red_distance_px": round(
                                mean_red_distance,
                                3,
                            ),
                            "coverage": round(
                                coverage_ratio,
                                4,
                            ),
                            "failed_checks": list(
                                failed_checks
                            ),
                            "profile": (
                                ACTIVE_LOCALITY_PROFILE
                            ),
                        },
                    })

                    log(
                        "   [CV Reject] Priority 3 candidate: "
                        f"area_ratio={completed_area_ratio:.4f} "
                        f"(allowed={minimum_area_ratio:.4f}-"
                        f"{maximum_area_ratio:.4f}), "
                        f"inside_roi={completed_inside_roi}, "
                        f"contains_target={completed_contains_center}, "
                        f"max_step_px={maximum_adjacent_step:.2f}, "
                        f"allowed_step_px={pre_repair_maximum_step:.2f}, "
                        f"close_to_red_ratio={close_to_red_ratio:.3f}, "
                        f"mean_red_distance_px={mean_red_distance:.2f}; "
                        "failed="
                        + ", ".join(
                            failed_checks
                        )
                    )

                gap_provisional_accepted = False
                gap_provisional_diagnostics = {}

                if failed_checks and not local_gap_repair_accepted:
                    (
                        gap_provisional_accepted,
                        gap_provisional_diagnostics,
                    ) = _max_step_only_candidate_is_provisional_safe(
                        failed_checks=failed_checks,
                        coverage_ratio=coverage_ratio,
                        close_to_red_ratio=close_to_red_ratio,
                        mean_red_distance_px=mean_red_distance,
                        maximum_step_px=maximum_adjacent_step,
                        snapped_bins=snapped_bin_count,
                        raw_snap_outliers=raw_snap_outliers,
                        inside_roi=completed_inside_roi,
                        contains_target=completed_contains_center,
                        profile_name=ACTIVE_LOCALITY_PROFILE,
                    )

                if gap_provisional_accepted:
                    selected_contour = completed_contour
                    diagnostics = {
                        "method": "red_pixel_snapped_gap_provisional",
                        "area_ratio": round(completed_area_ratio, 4),
                        "adaptive_profile": ACTIVE_LOCALITY_PROFILE,
                        "minimum_area_ratio": round(minimum_area_ratio, 6),
                        "maximum_area_ratio": round(maximum_area_ratio, 6),
                        "coverage": round(coverage_ratio, 3),
                        "outlier_bins": rejected_bins,
                        "raw_points": bin_count,
                        "max_step_px": round(maximum_adjacent_step, 2),
                        "pre_repair_allowed_step_px": round(pre_repair_maximum_step, 2),
                        "strong_red_support": bool(strong_red_support),
                        "safe_gap_close_candidate": True,
                        "requires_exact_repair": True,
                        "requires_cross_zoom_confirmation": True,
                        "provisional_reason": "max_step_only_gap_repair",
                        "provisional_failed_checks": list(failed_checks),
                        "gap_provisional_admission": gap_provisional_diagnostics,
                        "snapped_bins": snapped_bin_count,
                        "raw_snap_outliers": raw_snap_outliers,
                        "close_to_red_ratio": round(close_to_red_ratio, 3),
                        "mean_red_distance_px": round(mean_red_distance, 2),
                    }
                    log(
                        "   [Gap Provisional] Max-step-only candidate admitted "
                        "to exact repair: "
                        f"coverage={coverage_ratio:.3f}, "
                        f"red_match={close_to_red_ratio:.3f}, "
                        f"mean_distance={mean_red_distance:.2f}px, "
                        f"step={maximum_adjacent_step:.2f}px. "
                        "Cross-zoom confirmation is mandatory."
                    )

                if all(
                    acceptance_checks.values()
                ):
                    selected_contour = completed_contour

                    diagnostics = {
                        "method": (
                            "red_pixel_snapped_polar_completion"
                        ),
                        "area_ratio": round(
                            completed_area_ratio,
                            4,
                        ),
                        "adaptive_profile": (
                            ACTIVE_LOCALITY_PROFILE
                        ),
                        "minimum_area_ratio": round(
                            minimum_area_ratio,
                            6,
                        ),
                        "maximum_area_ratio": round(
                            maximum_area_ratio,
                            6,
                        ),
                        "coverage": round(
                            coverage_ratio,
                            3,
                        ),
                        "outlier_bins": (
                            rejected_bins
                        ),
                        "raw_points": (
                            bin_count
                        ),
                        "max_step_px": round(
                            maximum_adjacent_step,
                            2,
                        ),
                        "pre_repair_allowed_step_px": round(
                            pre_repair_maximum_step,
                            2,
                        ),
                        "strong_red_support": bool(
                            strong_red_support
                        ),
                        "safe_gap_close_candidate": bool(
                            safe_gap_close_candidate
                        ),
                        "requires_exact_repair": bool(
                            requires_exact_repair
                        ),
                        "snapped_bins": (
                            snapped_bin_count
                        ),
                        "raw_snap_outliers": (
                            raw_snap_outliers
                        ),
                        "close_to_red_ratio": round(
                            close_to_red_ratio,
                            3,
                        ),
                        "mean_red_distance_px": round(
                            mean_red_distance,
                            2,
                        ),
                    }

                    completed_debug = image.copy()

                    cv2.drawContours(
                        completed_debug,
                        [
                            completed_contour
                        ],
                        -1,
                        (0, 255, 0),
                        3,
                    )

                    completed_debug_path = (
                        os.path.splitext(
                            screenshot_path
                        )[0]
                        + "_red_smooth_completed.png"
                    )

                    cv2.imwrite(
                        completed_debug_path,
                        completed_debug,
                    )

                    refinement_debug = image.copy()

                    cv2.drawContours(
                        refinement_debug,
                        [
                            completed_contour
                        ],
                        -1,
                        (
                            0,
                            255,
                            255,
                        ),
                        3,
                    )

                    refinement_debug_path = (
                        os.path.splitext(
                            screenshot_path
                        )[0]
                        + "_red_priority3_snapped.png"
                    )

                    cv2.imwrite(
                        refinement_debug_path,
                        refinement_debug,
                    )

                    log(
                        "   [CV Refine] Red-pixel boundary "
                        "snapping accepted"
                    )

    if selected_contour is None:
        log(
            "   [CV] Could not reconstruct a trustworthy "
            "Google-red locality outline"
        )

        existing_best_rejection = (
            LAST_CV_DIAGNOSTICS.get(
                "best_rejection",
                {},
            )
        )

        candidate_rejection = (
            diagnostics
            if isinstance(
                diagnostics,
                dict,
            )
            else {}
        )

        strongest_rejection = (
            _preserve_strongest_rejection(
                existing_best_rejection,
                candidate_rejection,
            )
        )

        LAST_CV_DIAGNOSTICS.update({
            "status": "rejected",
            "reason": "no_trustworthy_reconstructed_outline",
            "best_rejection": strongest_rejection,
        })

        return None

    bootstrap_guide_used = bool(
        polar_bootstrap_diagnostics.get(
            "accepted",
            False,
        )
        and polar_bootstrap_diagnostics.get(
            "triggered",
            False,
        )
        and isinstance(
            diagnostics,
            dict,
        )
        and diagnostics.get(
            "method",
            ""
        )
        not in {
            "directional_boundary_graph",
            "local_gap_path_repair",
        }
    )

    if bootstrap_guide_used:
        diagnostics = dict(
            diagnostics
        )

        diagnostics[
            "requires_cross_zoom_confirmation"
        ] = True

        diagnostics[
            "provisional_reason"
        ] = (
            "preserved_mask_polar_bootstrap"
        )

        diagnostics[
            "polar_bootstrap"
        ] = dict(
            polar_bootstrap_diagnostics
        )

        diagnostics[
            "bootstrap_line_is_guide_only"
        ] = True

        log(
            "   [Polar Bootstrap] Candidate remains provisional; "
            "another zoom must confirm the continuous-line reconstruction"
        )

    (
        exact_profile,
        exact_profile_reason,
    ) = _infer_profile_from_attempt(
        ACTIVE_LOCALITY_PROFILE,
        diagnostics,
        georef,
    )

    _set_active_locality_profile(
        exact_profile,
        exact_profile_reason,
        log=log,
    )

    exact_shape_used = False
    exact_confirmation_used = False

    exact_contour, exact_diagnostics = (
        _exact_shape_refine_contour(
            stable_contour=selected_contour,
            raw_red_mask=raw_red_mask,
            center=center,
            roi_bounds=(
                roi_left,
                roi_top,
                roi_right,
                roi_bottom,
            ),
            edge_margin=edge_margin,
            image_area=image_area,
            coverage_ratio=float(
                diagnostics.get(
                    "coverage",
                    0.0,
                )
                or 0.0
            ),
            log=log,
        )
    )

    if exact_contour is not None:
        selected_contour = (
            exact_contour
        )

        exact_shape_used = True

        diagnostics = dict(
            diagnostics
        )

        diagnostics[
            "base_method"
        ] = diagnostics.get(
            "method",
            "unknown",
        )

        diagnostics[
            "method"
        ] = (
            "google_red_exact_shape"
        )

        diagnostics[
            "exact_shape"
        ] = exact_diagnostics

        if bootstrap_guide_used:
            diagnostics[
                "requires_cross_zoom_confirmation"
            ] = True

            diagnostics[
                "provisional_reason"
            ] = (
                "preserved_mask_polar_bootstrap"
            )

            diagnostics[
                "polar_bootstrap"
            ] = dict(
                polar_bootstrap_diagnostics
            )

        exact_debug = image.copy()

        cv2.drawContours(
            exact_debug,
            [
                selected_contour
            ],
            -1,
            (
                0,
                255,
                255,
            ),
            2,
        )

        exact_debug_path = (
            os.path.splitext(
                screenshot_path
            )[0]
            + "_red_exact_shape.png"
        )

        cv2.imwrite(
            exact_debug_path,
            exact_debug,
        )

    else:
        diagnostics = dict(
            diagnostics
        )

        diagnostics[
            "exact_shape_fallback"
        ] = exact_diagnostics

        if (
            isinstance(
                exact_diagnostics,
                dict,
            )
            and exact_diagnostics.get(
                "confirmation_candidate",
                False,
            )
            and exact_diagnostics.get(
                "confirmation_pixel_contour",
            )
        ):
            confirmation_points = np.asarray(
                exact_diagnostics[
                    "confirmation_pixel_contour"
                ],
                dtype=np.int32,
            ).reshape(
                -1,
                1,
                2,
            )

            selected_contour = (
                confirmation_points
            )

            exact_shape_used = True
            exact_confirmation_used = True

            diagnostics[
                "base_method"
            ] = diagnostics.get(
                "method",
                "unknown",
            )

            diagnostics[
                "method"
            ] = (
                "google_red_exact_repair_confirmation"
            )

            diagnostics[
                "exact_shape"
            ] = {
                key: value
                for key, value
                in exact_diagnostics.items()
                if key
                != "confirmation_pixel_contour"
            }

            diagnostics[
                "requires_cross_zoom_confirmation"
            ] = True

            diagnostics[
                "provisional_reason"
            ] = (
                "exact_repair_confirmation"
            )

            log(
                "   [Exact Confirm] Repaired contour will "
                "enter multi-zoom validation but cannot export alone"
            )

        if (
            isinstance(
                exact_diagnostics,
                dict,
            )
            and exact_diagnostics.get(
                "review_candidate",
                False,
            )
            and exact_diagnostics.get(
                "review_pixel_contour",
            )
        ):
            review_points = np.asarray(
                exact_diagnostics[
                    "review_pixel_contour"
                ],
                dtype=np.int32,
            ).reshape(
                -1,
                1,
                2,
            )

            review_framing = (
                _contour_framing_diagnostics(
                    contour=review_points,
                    image_width=width,
                    image_height=height,
                    zoom=float(
                        georef.zoom
                    ),
                )
            )

            log(
                "   [Framing] Repaired REVIEW contour: "
                f"zoom={float(georef.zoom):.2f}, "
                f"status={review_framing['status']}, "
                "bbox_area="
                f"{review_framing.get('bbox_area_ratio', 0.0):.3f}, "
                "max_span="
                f"{review_framing.get('max_span_ratio', 0.0):.3f}"
            )

            review_ring = _contour_to_latlng(
                contour=review_points,
                scale=1.0,
                georef=georef,
            )

            if review_ring and len(
                review_ring
            ) >= 9:
                LAST_CV_REVIEW_CANDIDATES.append({
                    "ring": review_ring,
                    "diagnostics": {
                        **diagnostics,
                        "exact_shape_fallback": {
                            key: value
                            for key, value
                            in exact_diagnostics.items()
                            if key != "review_pixel_contour"
                        },
                        "framing": (
                            review_framing
                        ),
                    },
                    "source": "google_maps_opencv_repaired_review",
                    "reason": exact_diagnostics.get(
                        "review_reason",
                        "manual_review_required",
                    ),
                })

                log(
                    "   [Review Export] Repaired exact contour "
                    "stored for manual review"
                )

        if (
            diagnostics.get(
                "requires_exact_repair",
                False,
            )
            and not exact_confirmation_used
        ):
            if diagnostics.get(
                "requires_cross_zoom_confirmation",
                False,
            ):
                log(
                    "   [Gap Provisional] Exact repair did not produce a "
                    "geometry-safe candidate; this zoom cannot confirm "
                    "another boundary"
                )

            log(
                "   [Safe Gap Close] Rejected: the broken "
                "outline needed exact repair, but exact repair "
                "did not produce an automatically approved "
                "final contour"
            )

            LAST_CV_DIAGNOSTICS.update({
                "status": "rejected",
                "reason": "safe_gap_close_exact_repair_failed",
                "best_rejection": diagnostics,
            })

            return None

    diagnostics = dict(
        diagnostics
    )

    diagnostics[
        "satellite_label_occlusion"
    ] = dict(
        satellite_label_occlusion_diagnostics
    )

    perimeter = float(
        cv2.arcLength(
            selected_contour,
            True,
        )
    )

    # Exact mode keeps substantially more visible Google-red vertices.
    # Stable fallback retains the proven Priority 3 simplification.
    simplification_epsilon = (
        max(
            0.05,
            perimeter
            * 0.00005,
        )
        if exact_shape_used
        else max(
            0.5,
            perimeter
            * 0.0007,
        )
    )

    selected_contour = cv2.approxPolyDP(
        selected_contour,
        simplification_epsilon,
        True,
    )

    if len(
        selected_contour
    ) < 8:
        log(
            "   [CV] Rejected over-simplified boundary: "
            f"only {len(selected_contour)} points"
        )
        return None

    log(
        "   [CV] Selected Google-red outline: "
        f"{diagnostics}; "
        f"mode={'exact' if exact_shape_used else 'stable'}, "
        f"epsilon_px={simplification_epsilon:.3f}, "
        f"simplified_points={len(selected_contour)}"
    )

    overlay = image.copy()

    cv2.drawContours(
        overlay,
        [
            selected_contour
        ],
        -1,
        (
            0,
            255,
            0,
        ),
        3,
    )

    cv2.drawMarker(
        overlay,
        center,
        (
            255,
            0,
            255,
        ),
        markerType=cv2.MARKER_CROSS,
        markerSize=20,
        thickness=2,
    )

    overlay_path = (
        os.path.splitext(
            screenshot_path
        )[0]
        + "_red_outline_selected.png"
    )

    cv2.imwrite(
        overlay_path,
        overlay,
    )

    framing_diagnostics = (
        _contour_framing_diagnostics(
            contour=selected_contour,
            image_width=width,
            image_height=height,
            zoom=float(
                georef.zoom
            ),
        )
    )

    diagnostics = dict(
        diagnostics
    )

    diagnostics[
        "framing"
    ] = framing_diagnostics

    # Only stamp the framing into exact_shape when that key actually exists.
    # The stable-contour path has no exact_shape entry, and the previous
    # isinstance(get(..., {}), dict) test passed for the missing-key default,
    # causing a KeyError on assignment.
    if isinstance(
        diagnostics.get(
            "exact_shape"
        ),
        dict,
    ):
        diagnostics[
            "exact_shape"
        ][
            "framing"
        ] = framing_diagnostics

    log(
        "   [Framing] Selected contour: "
        f"zoom={float(georef.zoom):.2f}, "
        f"status={framing_diagnostics['status']}, "
        "bbox_area="
        f"{framing_diagnostics.get('bbox_area_ratio', 0.0):.3f}, "
        "max_span="
        f"{framing_diagnostics.get('max_span_ratio', 0.0):.3f}, "
        "edge_margin="
        f"{framing_diagnostics.get('minimum_edge_margin_ratio', 0.0):.3f}"
    )

    ring = _contour_to_latlng(
        contour=selected_contour,
        scale=1.0,
        georef=georef,
    )

    if not ring or len(
        ring
    ) < 9:
        log(
            "   [CV] Converted boundary has too few "
            "geographic points"
        )

        LAST_CV_DIAGNOSTICS = {
            "status": "rejected",
            "reason": "too_few_geographic_points",
        }

        return None

    LAST_CV_DIAGNOSTICS = dict(
        diagnostics
    )

    LAST_CV_DIAGNOSTICS.update({
        "status": "accepted",
        "mode": (
            "exact"
            if exact_shape_used
            else "stable"
        ),
        "point_count": len(
            ring
        ),
        "simplified_point_count": len(
            selected_contour
        ),
        "screenshot_path": screenshot_path,
    })

    return ring


def _strategy_color_region(img_bgr, cx, cy, georef, log):
    h, w  = img_bgr.shape[:2]
    scale  = min(1.0, 640 / max(w, h))
    small  = cv2.resize(img_bgr, (int(w * scale), int(h * scale)))
    sh, sw = small.shape[:2]
    scx, scy = int(cx * scale), int(cy * scale)

    lab = (cv2.cvtColor(small, cv2.COLOR_BGR2LAB)
             .reshape(-1, 3)
             .astype(np.float32))

    K        = 8
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, _ = cv2.kmeans(
        lab, K, None, criteria, 3, cv2.KMEANS_RANDOM_CENTERS
    )

    label_map      = labels.reshape(sh, sw)
    center_cluster = label_map[scy, scx]
    mask           = (label_map == center_cluster).astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask   = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask   = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  kernel, iterations=1)

    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_TC89_KCOS
    )
    if not contours:
        return None

    best      = None
    best_area = 0
    img_area  = sh * sw

    for c in contours:
        area = cv2.contourArea(c)
        if area < img_area * 0.02:
            continue
        if area > img_area * 0.40:
            log(f"   [CV] Rejecting huge contour "
                f"({100*area/img_area:.1f}% of image)")
            continue
        dist = cv2.pointPolygonTest(c, (scx, scy), False)
        if dist >= 0 and area > best_area:
            best      = c
            best_area = area

    if best is None:
        log("   [CV] Color-region: no cluster contains center")
        return None

    log(f"   [CV] Selected contour area: {100*best_area/(sh*sw):.1f}%")
    return _contour_to_latlng(best, scale, georef)


def _strategy_canny_contour(img_bgr, cx, cy, georef, log):
    h, w  = img_bgr.shape[:2]
    scale  = min(1.0, 800 / max(w, h))
    small  = cv2.resize(img_bgr, (int(w * scale), int(h * scale)))

    gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blur  = cv2.GaussianBlur(gray, (5, 5), 0)

    ret, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    edges  = cv2.Canny(blur, ret * 0.5, ret)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=3)

    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_TC89_KCOS
    )
    if not contours:
        return None

    sh, sw = small.shape[:2]
    total  = sh * sw

    min_area_frac = 0.003
    max_area_frac = 0.40

    scored = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area_frac * total or area > max_area_frac * total:
            continue
        dist = cv2.pointPolygonTest(c, (cx * scale, cy * scale), False)
        if dist < 0:
            continue
        scored.append((area, c))

    if not scored:
        best = max(contours, key=cv2.contourArea)
    else:
        scored.sort(key=lambda x: x[0], reverse=True)
        best      = scored[0][1]
        best_area = cv2.contourArea(best)
        if best_area > 0.60 * total:
            log(f"   [CV] Warning: best contour is large "
                f"({100*best_area/total:.1f}% of image)")
            return None

    poly = _contour_to_latlng(best, scale, georef)
    if poly and len(poly) < 4:
        log(f"   [CV] Contour has too few points after simplification: "
            f"{len(poly)}")
        return None
    return poly


def _contour_to_latlng(
    contour: np.ndarray,
    scale: float,
    georef: GeoReference,
    max_points: int = 1500,
) -> Optional[list[tuple[float, float]]]:
    """
    Convert screenshot contour pixels to latitude/longitude while retaining
    enough points for an irregular Google Maps locality boundary.
    """
    if contour is None or len(contour) < 3:
        return None

    perimeter = float(cv2.arcLength(contour, True))

    simplify_epsilon = max(
        0.05,
        perimeter * 0.00004,
    )

    approximation = cv2.approxPolyDP(
        contour,
        simplify_epsilon,
        True,
    )

    while (
        len(approximation) > max_points
        and simplify_epsilon < perimeter
    ):
        simplify_epsilon *= 1.4
        approximation = cv2.approxPolyDP(
            contour,
            simplify_epsilon,
            True,
        )

    points = [
        (
            int(
                point[0][0]
                / scale
            ),
            int(
                point[0][1]
                / scale
            ),
        )
        for point
        in approximation
    ]

    ring = []

    for pixel_x, pixel_y in points:
        latitude, longitude = (
            georef.pixel_to_latlng(
                pixel_x,
                pixel_y,
            )
        )

        coordinate = (
            latitude,
            longitude,
        )

        if (
            not ring
            or coordinate
            != ring[-1]
        ):
            ring.append(
                coordinate
            )

    if (
        ring
        and ring[0]
        != ring[-1]
    ):
        ring.append(
            ring[0]
        )

    return (
        ring
        if len(
            ring
        ) >= 9
        else None
    )


# ─────────────────────────────────────────────────────────────────────────────
#  OSM BOUNDARY FETCH
# ─────────────────────────────────────────────────────────────────────────────



def _bbox_pad(parsed: ParsedQuery, zoom: float) -> float:
    if parsed.state_code and not parsed.district:
        return 8.0
    if parsed.locality:
        return 0.015
    if parsed.district:
        return 1.0 if zoom <= 10 else 0.4
    return 0.05 if zoom >= 17 else 0.2


# ─────────────────────────────────────────────────────────────────────────────
#  POLYGON SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def choose_polygon(
    cv_polygon: Optional[list],
    sam_boundary: Optional[dict],
    parsed: ParsedQuery,
    georef: "GeoReference | None" = None,
    log=print,
) -> tuple[list, str, str]:
    """
    Prefer Google's explicit red locality outline. Use SAM only when no
    trustworthy red outline is available.
    """
    if cv_polygon and len(cv_polygon) >= 5:
        log(
            "   [Choose] Using Google-red OpenCV boundary"
        )
        return (
            cv_polygon,
            "google_maps_opencv",
            parsed.display_name,
        )

    if sam_boundary:
        confidence = float(
            sam_boundary.get("confidence", 0.0)
        )
        geojson = sam_boundary.get("geojson")
        ring = (
            _geojson_to_ring(geojson)
            if geojson
            else None
        )

        if (
            confidence >= 0.60
            and ring
            and len(ring) >= 5
        ):
            log(
                f"   [Choose] Using SAM boundary "
                f"(confidence={confidence:.2f})"
            )
            return (
                ring,
                "google_maps_sam",
                parsed.display_name,
            )

    raise RuntimeError(
        "No complete or reconstructable Google-red locality outline "
        "was detected after zoom-15, zoom-14 and zoom-13 captures, "
        "and SAM found no reliable centre-containing boundary."
    )






def _remove_needle_spikes(
    xs: np.ndarray,
    ys: np.ndarray,
    minimum_angle_degrees: float = 28.0,
    minimum_arm_metres: float = 15.0,
    log=print,
) -> tuple[np.ndarray, np.ndarray, int]:
    """
    Remove needle-spike vertices from a closed metric ring.

    A needle is a vertex whose two edges form a very sharp angle with
    both arms long enough to matter — the classic sawtooth artifact from
    bad gap bridging. Real locality corners are far blunter than the
    threshold, so genuine geometry is preserved.
    """
    removed = 0
    limit_cos = math.cos(
        math.radians(minimum_angle_degrees)
    )

    for _ in range(200):
        n = len(xs)
        if n < 5:
            break
        # treat as closed ring without duplicate endpoint
        closed = (
            abs(xs[0] - xs[-1]) < 1e-9
            and abs(ys[0] - ys[-1]) < 1e-9
        )
        core_x = xs[:-1] if closed else xs
        core_y = ys[:-1] if closed else ys
        m = len(core_x)
        if m < 4:
            break

        worst_index = -1
        worst_cos = limit_cos
        for i in range(m):
            ax = core_x[(i - 1) % m] - core_x[i]
            ay = core_y[(i - 1) % m] - core_y[i]
            bx = core_x[(i + 1) % m] - core_x[i]
            by = core_y[(i + 1) % m] - core_y[i]
            la = math.hypot(ax, ay)
            lb = math.hypot(bx, by)
            if la < minimum_arm_metres or lb < minimum_arm_metres:
                continue
            cos_angle = (ax * bx + ay * by) / (la * lb)
            if cos_angle > worst_cos:
                worst_cos = cos_angle
                worst_index = i

        if worst_index < 0:
            break

        core_x = np.delete(core_x, worst_index)
        core_y = np.delete(core_y, worst_index)
        removed += 1
        if closed:
            xs = np.concatenate((core_x, [core_x[0]]))
            ys = np.concatenate((core_y, [core_y[0]]))
        else:
            xs = core_x
            ys = core_y

    if removed:
        log(
            "   [Sanitize] Needle-spike vertices removed: "
            f"{removed}"
        )
    return xs, ys, removed


def _clip_small_self_intersection_loops(
    xs: np.ndarray,
    ys: np.ndarray,
    maximum_loop_area_ratio: float = 0.02,
    log=print,
) -> tuple[np.ndarray, np.ndarray, int]:
    """
    Remove small self-intersection loops from a closed ring in metric space.

    When the ring path crosses itself (a "lasso"), the vertices between the
    two crossing segments form a loop. Any loop whose area is below
    maximum_loop_area_ratio of the total ring area is a detection artifact
    and is replaced by the single intersection point.
    """

    def shoelace(px: np.ndarray, py: np.ndarray) -> float:
        return abs(
            float(
                np.sum(px * np.roll(py, -1) - np.roll(px, -1) * py)
            )
        ) * 0.5

    def seg_intersection(p1, p2, p3, p4):
        d1x, d1y = p2[0] - p1[0], p2[1] - p1[1]
        d2x, d2y = p4[0] - p3[0], p4[1] - p3[1]
        denom = d1x * d2y - d1y * d2x
        if abs(denom) < 1e-12:
            return None
        t = ((p3[0] - p1[0]) * d2y - (p3[1] - p1[1]) * d2x) / denom
        u = ((p3[0] - p1[0]) * d1y - (p3[1] - p1[1]) * d1x) / denom
        if 1e-9 < t < 1.0 - 1e-9 and 1e-9 < u < 1.0 - 1e-9:
            return (p1[0] + t * d1x, p1[1] + t * d1y)
        return None

    removed = 0
    for _ in range(16):
        n = len(xs)
        if n < 5:
            break
        total_area = shoelace(xs[:-1], ys[:-1])
        if total_area <= 0.0:
            break
        found = False
        for i in range(n - 1):
            for j in range(i + 2, n - 1):
                if i == 0 and j == n - 2:
                    continue  # first and last segment share the ring joint
                crossing = seg_intersection(
                    (xs[i], ys[i]),
                    (xs[i + 1], ys[i + 1]),
                    (xs[j], ys[j]),
                    (xs[j + 1], ys[j + 1]),
                )
                if crossing is None:
                    continue
                loop_x = np.concatenate((
                    [crossing[0]],
                    xs[i + 1:j + 1],
                ))
                loop_y = np.concatenate((
                    [crossing[1]],
                    ys[i + 1:j + 1],
                ))
                loop_area = shoelace(loop_x, loop_y)
                if loop_area > total_area * maximum_loop_area_ratio:
                    continue  # large loop = real geometry, keep
                xs = np.concatenate((
                    xs[:i + 1],
                    [crossing[0]],
                    xs[j + 1:],
                ))
                ys = np.concatenate((
                    ys[:i + 1],
                    [crossing[1]],
                    ys[j + 1:],
                ))
                removed += 1
                found = True
                break
            if found:
                break
        if not found:
            break

    if removed:
        log(
            "   [Sanitize] Clipped "
            f"{removed} small self-intersection loop(s)"
        )
    return xs, ys, removed


def _sanitize_ring_geometry(
    ring: list[tuple[float, float]],
    log=print,
) -> list[tuple[float, float]]:
    """
    Final geometry cleanup before export.

    Detection clutter (red icons, labels, roads) can leave thin spikes,
    out-and-back whiskers and small pinched self-intersection loops on the
    exported ring. Those render in Google Earth as stray dashes and lassos.

    Method: rasterise the ring as a filled polygon in a local metric frame,
    apply a small morphological opening (removes any protrusion thinner than
    the kernel while leaving the true boundary untouched), then re-extract
    the largest external contour and convert back to latitude/longitude.
    """
    if not ring or len(ring) < 4:
        return ring

    latitudes = np.array([p[0] for p in ring], dtype=np.float64)
    longitudes = np.array([p[1] for p in ring], dtype=np.float64)

    lat0 = float(np.mean(latitudes))
    lng0 = float(np.mean(longitudes))
    metres_per_deg_lat = 110540.0
    metres_per_deg_lng = 111320.0 * math.cos(math.radians(lat0))

    xs = (longitudes - lng0) * metres_per_deg_lng
    ys = (latitudes - lat0) * metres_per_deg_lat

    xs, ys, _loops_removed = _clip_small_self_intersection_loops(
        xs,
        ys,
        log=log,
    )

    xs, ys, _spikes_removed = _remove_needle_spikes(
        xs,
        ys,
        log=log,
    )

    # Reconstruct the high-precision ring directly from the spike-cleaned
    # and loop-clipped metric vertices to preserve 100% of the true road
    # curvatures and pixel-level alignment with the GeoTIFF image.
    cleaned_ring: list[tuple[float, float]] = []
    for x_m, y_m in zip(xs, ys):
        lat = lat0 + float(y_m) / metres_per_deg_lat
        lng = lng0 + float(x_m) / metres_per_deg_lng
        pt = (round(lat, 7), round(lng, 7))
        if not cleaned_ring or pt != cleaned_ring[-1]:
            cleaned_ring.append(pt)

    if cleaned_ring and cleaned_ring[0] != cleaned_ring[-1]:
        cleaned_ring.append(cleaned_ring[0])

    if len(cleaned_ring) >= 4:
        log(
            "   [Sanitize] High-precision ring preserved: "
            f"{len(ring)} -> {len(cleaned_ring)} points "
            "(exact pixel fidelity maintained)"
        )
        return cleaned_ring

    return ring


def _geojson_to_ring(geojson):
    t = geojson.get("type")
    if t == "Polygon":
        coords = geojson["coordinates"][0]
        return [(c[1], c[0]) for c in coords]
    elif t == "MultiPolygon":
        polys   = geojson["coordinates"]
        largest = max(polys, key=lambda p: len(p[0]))
        return [(c[1], c[0]) for c in largest[0]]
    return None


def _georef_bbox_ring(georef: "GeoReference") -> list[tuple[float, float]]:
    """Closed 5-point rectangle from screenshot bbox (last-resort polygon)."""
    bb = georef.bbox
    n, s, w, e = bb["north"], bb["south"], bb["west"], bb["east"]
    return [(n, w), (n, e), (s, e), (s, w), (n, w)]


def _viewport_crop_ring(
    georef: "GeoReference",
    crop: float = 0.65,
) -> list[tuple[float, float]]:
    """
    Returns a closed rectangular ring covering the centre `crop` fraction of
    the screenshot viewport.  Used as the sector/locality polygon when neither
    OSM nor OpenCV produce a reliable boundary.

    At zoom=17 the Google Maps viewport is approximately 1–1.5 km wide, which
    matches a typical Indian planned sector (~800 m–1 km).  A 65 % crop removes
    the UI chrome margins and gives a clean rectangle that closely matches the
    sector footprint.
    """
    half_w = (georef.img_w / 2.0) * crop
    half_h = (georef.img_h / 2.0) * crop

    cx = georef.img_w / 2.0
    cy = georef.img_h / 2.0

    nw = georef.pixel_to_latlng(cx - half_w, cy - half_h)
    ne = georef.pixel_to_latlng(cx + half_w, cy - half_h)
    se = georef.pixel_to_latlng(cx + half_w, cy + half_h)
    sw = georef.pixel_to_latlng(cx - half_w, cy + half_h)

    return [nw, ne, se, sw, nw]  # closed ring


def validate_boundary_ring(
    ring: list[tuple[float, float]],
    center_lat: float,
    center_lng: float,
    log=print,
) -> bool:
    """
    Reject a grossly shifted polygon by requiring the Google Maps camera
    centre to lie inside the exported ring.
    """
    if not ring or len(ring) < 5:
        return False

    contour = np.array(
        [
            [lng, lat]
            for lat, lng in ring
        ],
        dtype=np.float32,
    ).reshape(-1, 1, 2)

    if abs(cv2.contourArea(contour)) <= 0:
        log("   [Validate] Polygon area is zero")
        return False

    contains_center = (
        cv2.pointPolygonTest(
            contour,
            (
                float(center_lng),
                float(center_lat),
            ),
            False,
        ) >= 0
    )

    if not contains_center:
        longitude_values = [
            lng
            for lat, lng in ring
        ]
        latitude_values = [
            lat
            for lat, lng in ring
        ]

        log(
            "   [Validate] Polygon rejected: "
            "it does not contain the searched place"
        )
        log(
            "   [Validate] Searched place: "
            f"{center_lat:.7f}, {center_lng:.7f}"
        )
        log(
            "   [Validate] Polygon bbox: "
            f"N={max(latitude_values):.7f}, "
            f"S={min(latitude_values):.7f}, "
            f"W={min(longitude_values):.7f}, "
            f"E={max(longitude_values):.7f}"
        )
        return False

    return True



# ─────────────────────────────────────────────────────────────────────────────
#  PRIORITY 4: EXISTING-BOUNDARY OVERLAP VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_boundary_name(
    value: str,
) -> str:
    value = str(
        value
        or ""
    ).lower()

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    ignored_words = {
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
        for token in value.split()
        if token
        and token not in ignored_words
    ]

    return " ".join(
        tokens
    )


def _normalise_ring(
    ring,
) -> Optional[
    list[
        tuple[
            float,
            float,
        ]
    ]
]:
    if not ring:
        return None

    cleaned = []

    for point in ring:
        if (
            not isinstance(
                point,
                (
                    list,
                    tuple,
                ),
            )
            or len(
                point
            ) < 2
        ):
            continue

        try:
            latitude = float(
                point[0]
            )
            longitude = float(
                point[1]
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if not (
            -90.0
            <= latitude
            <= 90.0
            and -180.0
            <= longitude
            <= 180.0
        ):
            continue

        coordinate = (
            latitude,
            longitude,
        )

        if (
            not cleaned
            or coordinate
            != cleaned[-1]
        ):
            cleaned.append(
                coordinate
            )

    if len(
        cleaned
    ) < 3:
        return None

    if cleaned[0] != cleaned[-1]:
        cleaned.append(
            cleaned[0]
        )

    return (
        cleaned
        if len(
            cleaned
        ) >= 4
        else None
    )


def _geometry_to_boundary_rings(
    geometry: dict,
) -> list[
    list[
        tuple[
            float,
            float,
        ]
    ]
]:
    if not isinstance(
        geometry,
        dict,
    ):
        return []

    geometry_type = geometry.get(
        "type"
    )

    coordinates = geometry.get(
        "coordinates"
    )

    rings = []

    if (
        geometry_type == "Polygon"
        and isinstance(
            coordinates,
            list,
        )
        and coordinates
    ):
        raw_ring = coordinates[0]

        ring = _normalise_ring(
            [
                (
                    point[1],
                    point[0],
                )
                for point in raw_ring
                if isinstance(
                    point,
                    (
                        list,
                        tuple,
                    ),
                )
                and len(
                    point
                ) >= 2
            ]
        )

        if ring:
            rings.append(
                ring
            )

    elif (
        geometry_type == "MultiPolygon"
        and isinstance(
            coordinates,
            list,
        )
    ):
        for polygon in coordinates:
            if (
                not isinstance(
                    polygon,
                    list,
                )
                or not polygon
            ):
                continue

            raw_ring = polygon[0]

            ring = _normalise_ring(
                [
                    (
                        point[1],
                        point[0],
                    )
                    for point in raw_ring
                    if isinstance(
                        point,
                        (
                            list,
                            tuple,
                        ),
                    )
                    and len(
                        point
                    ) >= 2
                ]
            )

            if ring:
                rings.append(
                    ring
                )

    return rings


def _read_geojson_boundaries(
    geojson_path: str,
) -> list[dict]:
    try:
        with open(
            geojson_path,
            "r",
            encoding="utf-8",
        ) as file:
            payload = json.load(
                file
            )
    except (
        OSError,
        ValueError,
        TypeError,
    ):
        return []

    features = []

    payload_type = (
        payload.get("type")
        if isinstance(
            payload,
            dict,
        )
        else ""
    )

    if payload_type == "FeatureCollection":
        features = payload.get(
            "features",
            [],
        )
    elif payload_type == "Feature":
        features = [
            payload
        ]
    elif payload_type in {
        "Polygon",
        "MultiPolygon",
    }:
        features = [{
            "type": "Feature",
            "properties": {},
            "geometry": payload,
        }]

    boundaries = []

    for feature in features:
        if not isinstance(
            feature,
            dict,
        ):
            continue

        properties = (
            feature.get(
                "properties"
            )
            if isinstance(
                feature.get(
                    "properties"
                ),
                dict,
            )
            else {}
        )

        rings = _geometry_to_boundary_rings(
            feature.get(
                "geometry",
                {},
            )
        )

        for ring in rings:
            boundaries.append({
                "ring": ring,
                "name": str(
                    properties.get(
                        "name"
                    )
                    or properties.get(
                        "display_name"
                    )
                    or Path(
                        geojson_path
                    ).stem
                ),
                "display_name": str(
                    properties.get(
                        "display_name"
                    )
                    or properties.get(
                        "name"
                    )
                    or Path(
                        geojson_path
                    ).stem
                ),
                "boundary_level": str(
                    properties.get(
                        "boundary_level"
                    )
                    or "unknown"
                ).lower(),
                "query": str(
                    properties.get(
                        "query"
                    )
                    or ""
                ),
                "review_candidate": bool(
                    properties.get(
                        "review_candidate",
                        False,
                    )
                ),
                "review_type": str(
                    properties.get(
                        "review_type",
                        "",
                    )
                    or ""
                ),
                "google_original": bool(
                    properties.get(
                        "google_original",
                        False,
                    )
                ),
                "priority5_multi_synced": bool(
                    properties.get(
                        "priority5_multi_synced",
                        False,
                    )
                ),
                "priority11_shared_edge_synced": bool(
                    properties.get(
                        "priority11_shared_edge_synced",
                        False,
                    )
                ),
                "priority12_topology_safe_synced": bool(
                    properties.get(
                        "priority12_topology_safe_synced",
                        False,
                    )
                ),
                "path": geojson_path,
            })

    return boundaries


def _ring_bbox(
    ring: list[
        tuple[
            float,
            float,
        ]
    ],
) -> tuple[
    float,
    float,
    float,
    float,
]:
    latitude_values = [
        latitude
        for latitude, longitude
        in ring
    ]

    longitude_values = [
        longitude
        for latitude, longitude
        in ring
    ]

    return (
        min(
            latitude_values
        ),
        min(
            longitude_values
        ),
        max(
            latitude_values
        ),
        max(
            longitude_values
        ),
    )


def _bboxes_intersect(
    first_bbox,
    second_bbox,
) -> bool:
    return not (
        first_bbox[2]
        < second_bbox[0]
        or second_bbox[2]
        < first_bbox[0]
        or first_bbox[3]
        < second_bbox[1]
        or second_bbox[3]
        < first_bbox[1]
    )


def _rasterise_overlap_metrics(
    first_ring: list[
        tuple[
            float,
            float,
        ]
    ],
    second_ring: list[
        tuple[
            float,
            float,
        ]
    ],
) -> Optional[dict]:
    first_ring = _normalise_ring(
        first_ring
    )
    second_ring = _normalise_ring(
        second_ring
    )

    if (
        not first_ring
        or not second_ring
    ):
        return None

    first_bbox = _ring_bbox(
        first_ring
    )
    second_bbox = _ring_bbox(
        second_ring
    )

    if not _bboxes_intersect(
        first_bbox,
        second_bbox,
    ):
        return {
            "intersection_pixels": 0,
            "first_area_pixels": 1,
            "second_area_pixels": 1,
            "new_overlap_ratio": 0.0,
            "existing_overlap_ratio": 0.0,
            "smaller_overlap_ratio": 0.0,
            "iou": 0.0,
        }

    south = min(
        first_bbox[0],
        second_bbox[0],
    )
    west = min(
        first_bbox[1],
        second_bbox[1],
    )
    north = max(
        first_bbox[2],
        second_bbox[2],
    )
    east = max(
        first_bbox[3],
        second_bbox[3],
    )

    mean_latitude = (
        south
        + north
    ) / 2.0

    longitude_scale = max(
        0.10,
        math.cos(
            math.radians(
                mean_latitude
            )
        ),
    )

    projected_width = max(
        (
            east
            - west
        )
        * longitude_scale,
        1e-9,
    )

    projected_height = max(
        north
        - south,
        1e-9,
    )

    maximum_canvas = 1400
    minimum_canvas = 300
    padding = 10

    if (
        projected_width
        >= projected_height
    ):
        canvas_width = maximum_canvas
        canvas_height = max(
            minimum_canvas,
            int(
                round(
                    maximum_canvas
                    * projected_height
                    / projected_width
                )
            ),
        )
    else:
        canvas_height = maximum_canvas
        canvas_width = max(
            minimum_canvas,
            int(
                round(
                    maximum_canvas
                    * projected_width
                    / projected_height
                )
            ),
        )

    canvas_width += (
        padding
        * 2
    )
    canvas_height += (
        padding
        * 2
    )

    def ring_to_pixels(
        ring,
    ) -> np.ndarray:
        pixel_points = []

        usable_width = max(
            1,
            canvas_width
            - 2
            * padding
            - 1,
        )

        usable_height = max(
            1,
            canvas_height
            - 2
            * padding
            - 1,
        )

        for latitude, longitude in ring:
            projected_x = (
                longitude
                - west
            ) * longitude_scale

            projected_y = (
                north
                - latitude
            )

            pixel_x = (
                padding
                + projected_x
                / projected_width
                * usable_width
            )

            pixel_y = (
                padding
                + projected_y
                / projected_height
                * usable_height
            )

            pixel_points.append(
                (
                    int(
                        round(
                            pixel_x
                        )
                    ),
                    int(
                        round(
                            pixel_y
                        )
                    ),
                )
            )

        return np.array(
            pixel_points,
            dtype=np.int32,
        ).reshape(
            -1,
            1,
            2,
        )

    first_mask = np.zeros(
        (
            canvas_height,
            canvas_width,
        ),
        dtype=np.uint8,
    )

    second_mask = np.zeros_like(
        first_mask
    )

    cv2.fillPoly(
        first_mask,
        [
            ring_to_pixels(
                first_ring
            )
        ],
        255,
    )

    cv2.fillPoly(
        second_mask,
        [
            ring_to_pixels(
                second_ring
            )
        ],
        255,
    )

    first_area = int(
        cv2.countNonZero(
            first_mask
        )
    )

    second_area = int(
        cv2.countNonZero(
            second_mask
        )
    )

    if (
        first_area <= 0
        or second_area <= 0
    ):
        return None

    intersection_mask = cv2.bitwise_and(
        first_mask,
        second_mask,
    )

    union_mask = cv2.bitwise_or(
        first_mask,
        second_mask,
    )

    intersection_area = int(
        cv2.countNonZero(
            intersection_mask
        )
    )

    union_area = int(
        cv2.countNonZero(
            union_mask
        )
    )

    return {
        "intersection_pixels": (
            intersection_area
        ),
        "first_area_pixels": (
            first_area
        ),
        "second_area_pixels": (
            second_area
        ),
        "new_overlap_ratio": (
            intersection_area
            / first_area
        ),
        "existing_overlap_ratio": (
            intersection_area
            / second_area
        ),
        "smaller_overlap_ratio": (
            intersection_area
            / min(
                first_area,
                second_area,
            )
        ),
        "iou": (
            intersection_area
            / max(
                union_area,
                1,
            )
        ),
    }



def prepare_exact_google_directories(
    output_dir: str,
    log=print,
) -> dict:
    """
    Prepare separate folders for source Google boundaries, manual review files
    and diagnostic reports. Derived synchronized outputs remain timestamped
    folders created by sync_boundaries.py.
    """
    root_dir = os.path.abspath(
        output_dir
    )

    if EXACT_GOOGLE_MODE:
        original_dir = os.path.join(
            root_dir,
            EXACT_GOOGLE_ORIGINAL_DIRNAME,
        )

        review_dir = os.path.join(
            root_dir,
            EXACT_GOOGLE_REVIEW_DIRNAME,
        )

        report_dir = os.path.join(
            root_dir,
            EXACT_GOOGLE_REPORT_DIRNAME,
        )

    else:
        original_dir = root_dir
        review_dir = root_dir
        report_dir = root_dir

    for directory in (
        root_dir,
        original_dir,
        review_dir,
        report_dir,
    ):
        os.makedirs(
            directory,
            exist_ok=True,
        )

    if EXACT_GOOGLE_MODE:
        log(
            "   [Priority 15.5.4.8] Satellite Yellow + Label Occlusion Repair enabled"
        )

        log(
            "   [Priority 13] Original polygons: "
            f"{original_dir}"
        )

        log(
            "   [Priority 13] Review candidates: "
            f"{review_dir}"
        )

        log(
            "   [Priority 13] Reports: "
            f"{report_dir}"
        )

    return {
        "root": root_dir,
        "original": original_dir,
        "review": review_dir,
        "reports": report_dir,
    }


def validate_existing_boundary_overlap(
    ring: list[
        tuple[
            float,
            float,
        ]
    ],
    parsed: ParsedQuery,
    output_dir: str,
    current_geojson_path: str,
    report_path: str,
    log=print,
) -> dict:
    """
    Compare a proposed boundary with existing GeoJSON files.

    Severe same-level overlap and duplicate polygons are blocked. Smaller
    overlaps are reported but allowed. No polygon subtraction is performed.
    """
    proposed_ring = _normalise_ring(
        ring
    )

    proposed_level = (
        "locality"
        if parsed.locality
        else (
            "district"
            if parsed.district
            else (
                "state"
                if parsed.state_code
                and not parsed.district
                else "unknown"
            )
        )
    )

    report = {
        "query": parsed.raw,
        "display_name": (
            parsed.display_name
        ),
        "boundary_level": (
            proposed_level
        ),
        "validation_enabled": (
            OVERLAP_VALIDATION_ENABLED
        ),
        "boundary_mode": (
            "exact_google_original"
            if EXACT_GOOGLE_MODE
            else "clean_coverage"
        ),
        "overlap_policy": (
            EXACT_GOOGLE_OVERLAP_POLICY
            if EXACT_GOOGLE_MODE
            else "block_unsafe_overlap"
        ),
        "shape_modified": False,
        "decision": "allow",
        "blocked": False,
        "warnings": [],
        "comparisons": [],
        "ignored_nonfinal_files": [],
    }

    if (
        not OVERLAP_VALIDATION_ENABLED
        or not proposed_ring
    ):
        return report

    current_absolute_path = os.path.abspath(
        current_geojson_path
    )

    geojson_paths = sorted(
        Path(
            output_dir
        ).glob(
            "*.geojson"
        )
    )

    compared_count = 0

    ignored_filename_markers = (
        "_review_",
        "_synced",
        "shared_edge",
        "shared_edges",
        "sync_report",
        "overlap_report",
        "manual_review",
    )

    for existing_path in geojson_paths:
        existing_absolute_path = os.path.abspath(
            str(
                existing_path
            )
        )

        if (
            existing_absolute_path
            == current_absolute_path
        ):
            continue

        lowered_existing_name = (
            existing_path.name.lower()
        )

        if any(
            marker in lowered_existing_name
            for marker in ignored_filename_markers
        ):
            report[
                "ignored_nonfinal_files"
            ].append(
                existing_path.name
            )

            log(
                "   [Overlap] Ignoring non-original file: "
                f"{existing_path.name}"
            )

            continue

        existing_boundaries = _read_geojson_boundaries(
            str(
                existing_path
            )
        )

        for existing in existing_boundaries:
            if (
                existing.get(
                    "review_candidate",
                    False,
                )
                or existing.get(
                    "review_type",
                    "",
                )
                or existing.get(
                    "priority5_multi_synced",
                    False,
                )
                or existing.get(
                    "priority11_shared_edge_synced",
                    False,
                )
                or existing.get(
                    "priority12_topology_safe_synced",
                    False,
                )
            ):
                existing_name = os.path.basename(
                    existing.get(
                        "path",
                        str(
                            existing_path
                        ),
                    )
                )

                if (
                    existing_name
                    not in report[
                        "ignored_nonfinal_files"
                    ]
                ):
                    report[
                        "ignored_nonfinal_files"
                    ].append(
                        existing_name
                    )

                log(
                    "   [Overlap] Ignoring REVIEW/derived boundary: "
                    f"{existing_name}"
                )

                continue

            metrics = _rasterise_overlap_metrics(
                proposed_ring,
                existing[
                    "ring"
                ],
            )

            if not metrics:
                continue

            compared_count += 1

            smaller_overlap = float(
                metrics[
                    "smaller_overlap_ratio"
                ]
            )

            new_overlap = float(
                metrics[
                    "new_overlap_ratio"
                ]
            )

            existing_overlap = float(
                metrics[
                    "existing_overlap_ratio"
                ]
            )

            iou = float(
                metrics[
                    "iou"
                ]
            )

            if (
                smaller_overlap
                < OVERLAP_WARN_SMALLER_RATIO
                and new_overlap
                < OVERLAP_WARN_SMALLER_RATIO
                and existing_overlap
                < OVERLAP_WARN_SMALLER_RATIO
            ):
                continue

            proposed_normalised_name = (
                _normalise_boundary_name(
                    parsed.display_name
                )
            )

            existing_normalised_name = (
                _normalise_boundary_name(
                    existing[
                        "display_name"
                    ]
                )
            )

            same_name = bool(
                proposed_normalised_name
                and existing_normalised_name
                and proposed_normalised_name
                == existing_normalised_name
            )

            existing_level = existing[
                "boundary_level"
            ]

            same_level = (
                existing_level
                in {
                    proposed_level,
                    "unknown",
                }
                or proposed_level
                == "unknown"
            )

            duplicate_shape = (
                iou
                >= OVERLAP_DUPLICATE_IOU
                and smaller_overlap
                >= OVERLAP_DUPLICATE_SMALLER_RATIO
            )

            severe_same_level_overlap = (
                not same_name
                and same_level
                and smaller_overlap
                >= OVERLAP_BLOCK_SMALLER_RATIO
            )

            classification = "warning"
            blocked = False
            reason = (
                "Noticeable polygon overlap"
            )

            if (
                same_name
                or duplicate_shape
            ):
                classification = (
                    "duplicate"
                )
                blocked = True
                reason = (
                    "A highly similar boundary already "
                    "exists under another file"
                )

            elif severe_same_level_overlap:
                classification = (
                    "ambiguous_same_level_overlap"
                )
                blocked = True
                reason = (
                    "Different locality boundaries overlap "
                    "too heavily at the same/unknown level"
                )

            elif (
                existing_level
                not in {
                    proposed_level,
                    "unknown",
                }
                and proposed_level
                != "unknown"
            ):
                classification = (
                    "administrative_hierarchy_overlap"
                )
                reason = (
                    "Overlap may be expected because the "
                    "boundaries have different levels"
                )

            would_block_clean_coverage = bool(
                blocked
            )

            if (
                EXACT_GOOGLE_MODE
                and blocked
            ):
                if classification == "duplicate":
                    classification = (
                        "duplicate_google_result_warning"
                    )

                elif classification == (
                    "ambiguous_same_level_overlap"
                ):
                    classification = (
                        "broad_google_overlap_warning"
                    )

                reason = (
                    reason
                    + "; preserved because Exact Google Mode "
                    "does not alter or suppress accepted Google polygons"
                )

                blocked = False

            comparison = {
                "existing_name": (
                    existing[
                        "display_name"
                    ]
                ),
                "existing_file": os.path.basename(
                    existing[
                        "path"
                    ]
                ),
                "existing_level": (
                    existing_level
                ),
                "classification": (
                    classification
                ),
                "blocked": (
                    blocked
                ),
                "would_block_clean_coverage_mode": (
                    would_block_clean_coverage
                ),
                "original_geometry_preserved": True,
                "reason": (
                    reason
                ),
                "new_overlap_percent": round(
                    new_overlap
                    * 100.0,
                    2,
                ),
                "existing_overlap_percent": round(
                    existing_overlap
                    * 100.0,
                    2,
                ),
                "smaller_polygon_overlap_percent": round(
                    smaller_overlap
                    * 100.0,
                    2,
                ),
                "intersection_over_union_percent": round(
                    iou
                    * 100.0,
                    2,
                ),
            }

            report[
                "comparisons"
            ].append(
                comparison
            )

            log(
                "   [Overlap] "
                f"{parsed.display_name} vs "
                f"{existing['display_name']}: "
                f"new={new_overlap * 100.0:.1f}%, "
                f"existing={existing_overlap * 100.0:.1f}%, "
                f"smaller={smaller_overlap * 100.0:.1f}%, "
                f"IoU={iou * 100.0:.1f}% "
                f"[{classification}]"
            )

            if blocked:
                report[
                    "blocked"
                ] = True

                report[
                    "decision"
                ] = (
                    "review_required"
                )

            else:
                report[
                    "warnings"
                ].append(
                    reason
                )

    report[
        "existing_boundaries_compared"
    ] = compared_count

    if EXACT_GOOGLE_MODE:
        report[
            "blocked"
        ] = False

        report[
            "decision"
        ] = (
            "allow_with_warnings"
            if report[
                "comparisons"
            ]
            else "allow"
        )

    os.makedirs(
        os.path.dirname(
            report_path
        )
        or ".",
        exist_ok=True,
    )

    with open(
        report_path,
        "w",
        encoding="utf-8",
    ) as report_file:
        json.dump(
            report,
            report_file,
            ensure_ascii=False,
            indent=2,
        )

    log(
        "   [Overlap] Compared with "
        f"{compared_count} existing boundary "
        f"record(s)"
    )

    log(
        "   [Overlap] Report → "
        f"{os.path.basename(report_path)}"
    )

    if EXACT_GOOGLE_MODE:
        if report[
            "comparisons"
        ]:
            log(
                "   [Overlap] Exact Google Mode: overlap warning "
                "recorded; original polygon export is allowed unchanged"
            )
        else:
            log(
                "   [Overlap] Exact Google Mode: no reportable "
                "overlap found"
            )

    elif report[
        "blocked"
    ]:
        blocked_names = sorted({
            comparison[
                "existing_name"
            ]
            for comparison
            in report[
                "comparisons"
            ]
            if comparison[
                "blocked"
            ]
        })

        log(
            "   [Overlap] REVIEW REQUIRED: "
            "export blocked because of "
            + ", ".join(
                blocked_names
            )
        )

    else:
        log(
            "   [Overlap] Validation passed"
        )

    return report





def save_adaptive_locality_report(
    output_path: str,
    query: str,
    cv_attempts: list[dict],
    candidate_count: int,
    final_exported: bool,
    log=print,
) -> str:
    report = {
        "priority": 15,
        "query": query,
        "mode": "india_wide_adaptive_locality",
        "profile": ACTIVE_LOCALITY_PROFILE,
        "profile_reason": (
            ACTIVE_LOCALITY_PROFILE_REASON
        ),
        "attempted_zooms": [
            attempt.get("zoom")
            for attempt in cv_attempts
        ],
        "attempts": cv_attempts,
        "candidate_count": int(
            candidate_count
        ),
        "final_exported": bool(
            final_exported
        ),
        "safety_policy": {
            "self_crossing": "always_block",
            "inner_shortcut": "always_block",
            "invalid_geometry": "always_block",
            "single_provisional_candidate": "always_block",
            "repaired_candidate": (
                "requires_independent_cross_zoom_confirmation"
            ),
        },
    }

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as report_file:
        json.dump(
            report,
            report_file,
            ensure_ascii=False,
            indent=2,
        )

    log(
        "   [Adaptive] Report → "
        f"{Path(output_path).name}"
    )

    return output_path



def save_georef_report(
    georef: GeoReference,
    ring: list[tuple[float, float]],
    output_path: str,
    log=print,
) -> str:
    if not GEOREFERENCE_REPORT_ENABLED:
        return output_path

    latitudes = [
        float(
            latitude
        )
        for latitude, _longitude
        in ring
    ]

    longitudes = [
        float(
            longitude
        )
        for _latitude, longitude
        in ring
    ]

    report = {
        "priority": 14,
        "purpose": (
            "Debug DPR-aware Web Mercator pixel-to-coordinate conversion"
        ),
        "georef": georef.to_debug_dict(),
        "exported_ring": {
            "point_count": len(
                ring
            ),
            "north": max(
                latitudes
            )
            if latitudes
            else None,
            "south": min(
                latitudes
            )
            if latitudes
            else None,
            "west": min(
                longitudes
            )
            if longitudes
            else None,
            "east": max(
                longitudes
            )
            if longitudes
            else None,
        },
        "notes": [
            (
                "Coordinates were generated from screenshot image pixels "
                "through Google Web Mercator world pixels."
            ),
            (
                "css_per_image values correct Windows display scaling / DPR "
                "before converting to Web Mercator coordinates."
            ),
        ],
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as report_file:
        json.dump(
            report,
            report_file,
            ensure_ascii=False,
            indent=2,
        )

    log(
        "   [Georef] Report → "
        f"{os.path.basename(output_path)}"
    )

    return output_path



# ─────────────────────────────────────────────────────────────────────────────
#  EXPORT: GeoJSON / KML / KMZ
# ─────────────────────────────────────────────────────────────────────────────

def save_geojson(
    ring:         list[tuple[float, float]],
    name:         str,
    display_name: str,
    source:       str,
    output_path:  str,
    metadata: Optional[dict] = None,
    log=print,
) -> str:
    coords = [[lng, lat] for lat, lng in ring]
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])

    properties = {
        "name":         name,
        "display_name": display_name,
        "source":       source,
    }

    if isinstance(
        metadata,
        dict,
    ):
        properties.update(
            metadata
        )

    feature_collection = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": properties,
            "geometry": {
                "type":        "Polygon",
                "coordinates": [coords],
            },
        }],
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(feature_collection, f, ensure_ascii=False, indent=2)

    log(f"   [GeoJSON] → {os.path.basename(output_path)}  "
        f"({len(coords)} points)")
    return output_path


def save_kml(
    ring:        list[tuple[float, float]],
    name:        str,
    output_path: str,
    log=print,
) -> str:
    # FIX A: a KML LinearRing must be explicitly closed. save_geojson already
    # closed its ring; save_kml previously trusted the caller. Review-candidate
    # exports pass candidate rings directly, so close defensively here.
    if ring and tuple(ring[0]) != tuple(ring[-1]):
        ring = list(ring) + [ring[0]]

    coords_str = " ".join(
        f"{round(lng, KML_COORDINATE_DECIMALS)},"
        f"{round(lat, KML_COORDINATE_DECIMALS)},0"
        for lat, lng in ring
    )
    name_esc = (
        name.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{name_esc}</name>
  <Style id="boundaryStyle">
    <LineStyle>
      <color>ff0000ff</color>
      <width>2.5</width>
    </LineStyle>
    <PolyStyle>
      <color>330000ff</color>
      <fill>1</fill>
      <outline>1</outline>
    </PolyStyle>
  </Style>
  <Placemark>
    <name>{name_esc}</name>
    <styleUrl>#boundaryStyle</styleUrl>
    <Polygon>
      <altitudeMode>clampToGround</altitudeMode>
      <tessellate>1</tessellate>
      <outerBoundaryIs>
        <LinearRing>
          <altitudeMode>clampToGround</altitudeMode>
          <tessellate>1</tessellate>
          <coordinates>{coords_str}</coordinates>
        </LinearRing>
      </outerBoundaryIs>
    </Polygon>
  </Placemark>
</Document>
</kml>"""

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(kml)

    log(f"   [KML]     → {os.path.basename(output_path)}")
    return output_path


def save_kmz(
    kml_path: str,
    image_path: Optional[str] = None,
    bbox: Optional[dict] = None,
    log=print,
) -> str:
    kmz_path = os.path.splitext(kml_path)[0] + ".kmz"
    os.makedirs(os.path.dirname(os.path.abspath(kmz_path)), exist_ok=True)

    if image_path and os.path.exists(image_path) and bbox and isinstance(bbox, dict):
        with open(kml_path, "r", encoding="utf-8") as f:
            base_kml = f.read()

        img_name = "overlay.png"
        ground_overlay = f"""  <GroundOverlay>
    <name>Google Satellite Imagery Overlay</name>
    <Icon>
      <href>{img_name}</href>
    </Icon>
    <LatLonBox>
      <north>{bbox.get('north', 0)}</north>
      <south>{bbox.get('south', 0)}</south>
      <east>{bbox.get('east', 0)}</east>
      <west>{bbox.get('west', 0)}</west>
    </LatLonBox>
  </GroundOverlay>"""

        if "</Document>" in base_kml:
            combined_kml = base_kml.replace("</Document>", f"{ground_overlay}\n</Document>")
        else:
            combined_kml = base_kml

        with zipfile.ZipFile(kmz_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doc.kml", combined_kml.encode("utf-8"))
            zf.write(image_path, arcname=img_name)
    else:
        with zipfile.ZipFile(kmz_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(kml_path, arcname="doc.kml")

    log(f"   [KMZ]     → {os.path.basename(kmz_path)}")
    return kmz_path


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────


def _latlng_ring_pair_metrics(
    first_ring: list[
        tuple[
            float,
            float,
        ]
    ],
    second_ring: list[
        tuple[
            float,
            float,
        ]
    ],
) -> Optional[dict]:
    """
    Compare two geographic rings by rasterising them in one local canvas.
    This keeps the pipeline independent of Shapely.
    """
    if (
        not first_ring
        or not second_ring
        or len(
            first_ring
        )
        < 4
        or len(
            second_ring
        )
        < 4
    ):
        return None

    first_points = np.asarray(
        first_ring,
        dtype=np.float64,
    ).reshape(
        -1,
        2,
    )

    second_points = np.asarray(
        second_ring,
        dtype=np.float64,
    ).reshape(
        -1,
        2,
    )

    all_latitudes = np.concatenate(
        (
            first_points[:, 0],
            second_points[:, 0],
        )
    )

    all_longitudes = np.concatenate(
        (
            first_points[:, 1],
            second_points[:, 1],
        )
    )

    south = float(
        np.min(
            all_latitudes
        )
    )

    north = float(
        np.max(
            all_latitudes
        )
    )

    west = float(
        np.min(
            all_longitudes
        )
    )

    east = float(
        np.max(
            all_longitudes
        )
    )

    mean_latitude = (
        south
        + north
    ) / 2.0

    longitude_scale = max(
        0.10,
        math.cos(
            math.radians(
                mean_latitude
            )
        ),
    )

    projected_width = max(
        (
            east
            - west
        )
        * longitude_scale,
        1e-9,
    )

    projected_height = max(
        north
        - south,
        1e-9,
    )

    maximum_side = 1200
    minimum_side = 300
    padding = 12

    if (
        projected_width
        >= projected_height
    ):
        canvas_width = maximum_side

        canvas_height = max(
            minimum_side,
            int(
                round(
                    maximum_side
                    * projected_height
                    / projected_width
                )
            ),
        )

    else:
        canvas_height = maximum_side

        canvas_width = max(
            minimum_side,
            int(
                round(
                    maximum_side
                    * projected_width
                    / projected_height
                )
            ),
        )

    canvas_width += (
        padding
        * 2
    )

    canvas_height += (
        padding
        * 2
    )

    def to_contour(
        points: np.ndarray,
    ) -> np.ndarray:
        usable_width = max(
            1,
            canvas_width
            - 2
            * padding
            - 1,
        )

        usable_height = max(
            1,
            canvas_height
            - 2
            * padding
            - 1,
        )

        pixel_x = (
            padding
            + (
                points[:, 1]
                - west
            )
            * longitude_scale
            / projected_width
            * usable_width
        )

        pixel_y = (
            padding
            + (
                north
                - points[:, 0]
            )
            / projected_height
            * usable_height
        )

        return np.column_stack(
            (
                np.round(
                    pixel_x
                ),
                np.round(
                    pixel_y
                ),
            )
        ).astype(
            np.int32
        ).reshape(
            -1,
            1,
            2,
        )

    first_mask = np.zeros(
        (
            canvas_height,
            canvas_width,
        ),
        dtype=np.uint8,
    )

    second_mask = np.zeros_like(
        first_mask
    )

    cv2.fillPoly(
        first_mask,
        [
            to_contour(
                first_points
            )
        ],
        255,
    )

    cv2.fillPoly(
        second_mask,
        [
            to_contour(
                second_points
            )
        ],
        255,
    )

    first_area = int(
        cv2.countNonZero(
            first_mask
        )
    )

    second_area = int(
        cv2.countNonZero(
            second_mask
        )
    )

    if (
        first_area <= 0
        or second_area <= 0
    ):
        return None

    intersection_area = int(
        cv2.countNonZero(
            cv2.bitwise_and(
                first_mask,
                second_mask,
            )
        )
    )

    union_area = int(
        cv2.countNonZero(
            cv2.bitwise_or(
                first_mask,
                second_mask,
            )
        )
    )

    return {
        "iou": (
            intersection_area
            / max(
                union_area,
                1,
            )
        ),
        "smaller_overlap_ratio": (
            intersection_area
            / max(
                min(
                    first_area,
                    second_area,
                ),
                1,
            )
        ),
        "first_overlap_ratio": (
            intersection_area
            / first_area
        ),
        "second_overlap_ratio": (
            intersection_area
            / second_area
        ),
    }


def _boundary_candidate_score(
    candidate: dict,
) -> float:
    diagnostics = candidate.get(
        "diagnostics",
        {},
    )

    coverage = float(
        diagnostics.get(
            "coverage",
            0.0,
        )
        or 0.0
    )

    close_to_red = float(
        diagnostics.get(
            "close_to_red_ratio",
            0.0,
        )
        or 0.0
    )

    mean_red_distance = float(
        diagnostics.get(
            "mean_red_distance_px",
            50.0,
        )
        or 50.0
    )

    exact_bonus = (
        0.20
        if diagnostics.get(
            "mode"
        )
        == "exact"
        else 0.0
    )

    point_bonus = min(
        0.08,
        float(
            diagnostics.get(
                "point_count",
                0,
            )
            or 0
        )
        / 1000.0,
    )

    zoom_val = float(candidate.get("zoom", 13.0) or 13.0)
    zoom_bonus = max(0.0, (zoom_val - 12.0) * 0.45)

    return (
        coverage
        * 0.35
        + close_to_red
        * 0.25
        + exact_bonus
        + point_bonus
        + zoom_bonus
        - min(
            0.20,
            mean_red_distance
            / 100.0,
        )
    )


def _candidate_framing_status(candidate) -> str:
    """Return the framing classification recorded for a capture."""
    framing = (
        candidate.get(
            "framing",
            {},
        )
        if isinstance(
            candidate,
            dict,
        )
        else {}
    )

    if not isinstance(
        framing,
        dict,
    ):
        return ""

    return str(
        framing.get(
            "status",
            "",
        )
    )


def select_multizoom_boundary(
    candidates: list[dict],
    log=print,
) -> Optional[list[
    tuple[
        float,
        float,
    ]
]]:
    """
    Select a boundary only after comparing all successful zoom captures.
    """
    if not candidates:
        log(
            "   [Multi-Zoom] No successful boundary candidates"
        )

        return None

    if not MULTI_ZOOM_VALIDATION_ENABLED:
        final_candidates = [
            candidate
            for candidate
            in candidates
            if _candidate_is_final_eligible(
                candidate
            )
        ]

        if not final_candidates:
            log(
                "   [Framing] Validation disabled, but no "
                "candidate has safe detailed final framing"
            )

            return None

        selected = max(
            final_candidates,
            key=_candidate_detail_preference,
        )

        log(
            "   [Multi-Zoom] Validation disabled; selected "
            f"zoom {selected['zoom']}"
        )

        return selected[
            "ring"
        ]

    compatible_pairs = []

    for first_index in range(
        len(
            candidates
        )
    ):
        for second_index in range(
            first_index
            + 1,
            len(
                candidates
            ),
        ):
            first_candidate = candidates[
                first_index
            ]

            second_candidate = candidates[
                second_index
            ]

            metrics = _latlng_ring_pair_metrics(
                first_candidate[
                    "ring"
                ],
                second_candidate[
                    "ring"
                ],
            )

            if not metrics:
                continue

            log(
                "   [Multi-Zoom] "
                f"zoom {first_candidate['zoom']} vs "
                f"{second_candidate['zoom']}: "
                f"IoU={metrics['iou']:.3f}, "
                "smaller_overlap="
                f"{metrics['smaller_overlap_ratio']:.3f}"
            )

            pair_has_provisional = bool(
                first_candidate.get(
                    "provisional",
                    False,
                )
                or second_candidate.get(
                    "provisional",
                    False,
                )
            )

            pair_has_gap_provisional = bool(
                _candidate_is_gap_provisional(first_candidate)
                or _candidate_is_gap_provisional(second_candidate)
            )
            pair_has_graph_provisional = bool(
                _candidate_is_graph_provisional(first_candidate)
                or _candidate_is_graph_provisional(second_candidate)
            )

            pair_iou_limit = (
                GRAPH_CONFIRMATION_MIN_IOU
                if pair_has_graph_provisional
                else (
                    GAP_CONFIRMATION_MIN_IOU
                    if pair_has_gap_provisional
                    else (0.82 if pair_has_provisional else (0.70 if ACTIVE_LOCALITY_PROFILE == "small" else MULTI_ZOOM_MIN_IOU))
                )
            )
            pair_overlap_limit = (
                GRAPH_CONFIRMATION_MIN_SMALLER_OVERLAP
                if pair_has_graph_provisional
                else (
                    GAP_CONFIRMATION_MIN_SMALLER_OVERLAP
                    if pair_has_gap_provisional
                    else (0.92 if pair_has_provisional else (0.84 if ACTIVE_LOCALITY_PROFILE == "small" else MULTI_ZOOM_MIN_SMALLER_OVERLAP))
                )
            )

            provisional_safe = (
                _provisional_candidate_is_safe(first_candidate)
                and _provisional_candidate_is_safe(second_candidate)
            )

            first_overlap_ratio = float(
                metrics.get("first_overlap_ratio", 0.0) or 0.0
            )
            second_overlap_ratio = float(
                metrics.get("second_overlap_ratio", 0.0) or 0.0
            )
            if first_overlap_ratio > 0.0 and second_overlap_ratio > 0.0:
                relative_area_ratio = first_overlap_ratio / second_overlap_ratio
            else:
                relative_area_ratio = 0.0
            area_ratio_difference = abs(1.0 - relative_area_ratio)
            area_agreement_limit = GRAPH_CONFIRMATION_MAX_AREA_RATIO_DIFFERENCE if pair_has_graph_provisional else GAP_CONFIRMATION_MAX_AREA_RATIO_DIFFERENCE
            area_agreement_safe = not (pair_has_gap_provisional or pair_has_graph_provisional) or area_ratio_difference <= area_agreement_limit
            if pair_has_graph_provisional:
                log("   [Graph Confirm] Cross-zoom limits: " f"IoU>={pair_iou_limit:.2f}, " f"overlap>={pair_overlap_limit:.2f}, " f"area difference<={area_agreement_limit:.2f}; " f"measured={area_ratio_difference:.3f}")
            elif pair_has_gap_provisional:
                log("   [Gap Confirm] Cross-zoom limits: " f"IoU>={pair_iou_limit:.2f}, " f"overlap>={pair_overlap_limit:.2f}, " f"area difference<={area_agreement_limit:.2f}; " f"measured={area_ratio_difference:.3f}")

            pair_final_candidates = [
                candidate
                for candidate
                in (
                    first_candidate,
                    second_candidate,
                )
                if _candidate_is_final_eligible(
                    candidate
                )
            ]

            pair_has_detailed_final = bool(
                pair_final_candidates
            )

            if not pair_has_detailed_final:
                log(
                    "   [Framing] Pair can only confirm; "
                    "neither candidate has detailed final framing"
                )

            # A capture whose outline runs off the frame shows only part of
            # the locality, so intersection-over-union against a fully framed
            # capture is unfairly low even when the two agree everywhere they
            # overlap. In that case containment is the meaningful measure, so
            # the IoU floor is relaxed while the overlap floor is raised.
            pair_has_clipped_candidate = bool(
                _candidate_framing_status(
                    first_candidate
                )
                == "confirmation_only_clipped_or_too_large"
                or _candidate_framing_status(
                    second_candidate
                )
                == "confirmation_only_clipped_or_too_large"
            )

            if pair_has_clipped_candidate:
                effective_iou_limit = min(
                    pair_iou_limit,
                    0.60,
                )

                effective_overlap_limit = max(
                    pair_overlap_limit,
                    0.97,
                )

                log(
                    "   [Framing] Pair contains a clipped capture; "
                    f"IoU floor {pair_iou_limit:.2f} -> "
                    f"{effective_iou_limit:.2f}, overlap floor "
                    f"{pair_overlap_limit:.2f} -> "
                    f"{effective_overlap_limit:.2f}"
                )

            else:
                effective_iou_limit = pair_iou_limit
                effective_overlap_limit = pair_overlap_limit

            if (
                metrics[
                    "iou"
                ]
                >= effective_iou_limit
                and metrics[
                    "smaller_overlap_ratio"
                ]
                >= effective_overlap_limit
                and provisional_safe
                and area_agreement_safe
                and pair_has_detailed_final
            ):
                compatible_pairs.append({
                    "first": first_candidate,
                    "second": second_candidate,
                    "metrics": {
                        **metrics,
                        "pair_has_gap_provisional": pair_has_gap_provisional,
                        "pair_has_graph_provisional": pair_has_graph_provisional,
                        "area_ratio_difference": area_ratio_difference,
                        "required_iou": pair_iou_limit,
                        "required_smaller_overlap": pair_overlap_limit,
                    },
                })

    if compatible_pairs:
        best_pair = max(
            compatible_pairs,
            key=lambda pair: (
                pair[
                    "metrics"
                ][
                    "iou"
                ]
                + pair[
                    "metrics"
                ][
                    "smaller_overlap_ratio"
                ]
                + _boundary_candidate_score(
                    pair[
                        "first"
                    ]
                )
                + _boundary_candidate_score(
                    pair[
                        "second"
                    ]
                )
            ),
        )

        pair_candidates = (
            best_pair[
                "first"
            ],
            best_pair[
                "second"
            ],
        )

        detailed_candidates = [
            candidate
            for candidate
            in pair_candidates
            if _candidate_is_final_eligible(
                candidate
            )
        ]

        if not detailed_candidates:
            log(
                "   [Framing] Compatible pair rejected for final "
                "export because both captures are overview-only"
            )

            return None

        exact_detailed_candidates = [
            candidate
            for candidate
            in detailed_candidates
            if candidate.get("method") in {
                "google_red_exact_shape",
                "google_red_exact_repair_confirmation",
            }
            or candidate.get("contour_type") == "exact"
        ]

        non_gap_detailed_candidates = [
            candidate
            for candidate
            in detailed_candidates
            if not _candidate_is_gap_provisional(
                candidate
            )
            and not str(candidate.get("method", "")).endswith("polar_completion")
        ]

        if exact_detailed_candidates:
            selection_pool = exact_detailed_candidates
        elif non_gap_detailed_candidates:
            selection_pool = non_gap_detailed_candidates
        else:
            selection_pool = detailed_candidates

        selected = max(
            selection_pool,
            key=_candidate_detail_preference,
        )

        if best_pair["metrics"].get("pair_has_graph_provisional", False):
            log(
                "   [Graph Confirm] Using the highest-detail "
                "cross-zoom-confirmed graph contour as the final geometry"
            )
        elif best_pair["metrics"].get("pair_has_gap_provisional", False):
            log(
                "   [Gap Confirm] Using the highest-detail "
                "non-gap contour as the final geometry"
            )

        log(
            "   [Framing] Final source locked to zoom "
            f"{float(selected['zoom']):.2f}; "
            f"status={_candidate_framing(selected).get('status', 'unknown')}"
        )

        if selected.get("provisional", False):
            if best_pair["metrics"].get("pair_has_graph_provisional", False):
                log(
                    "   [Graph Confirm] Directional graph reconstruction "
                    "promoted only because another zoom independently "
                    "confirmed the geographic shape"
                )
            elif best_pair["metrics"].get("pair_has_gap_provisional", False):
                log(
                    "   [Gap Confirm] Provisional gap repair promoted only "
                    "because another zoom independently confirmed the "
                    "geographic shape"
                )
            else:
                log(
                    "   [Adaptive] Repaired candidate promoted only because "
                    "another zoom independently confirmed the same shape"
                )

        global LAST_MULTI_ZOOM_SELECTION

        LAST_MULTI_ZOOM_SELECTION = {
            "selected_zoom": float(
                selected[
                    "zoom"
                ]
            ),
            "selected_source": str(
                selected.get(
                    "source",
                    "",
                )
                or ""
            ),
            "selected_framing": (
                _candidate_framing(
                    selected
                )
            ),
            "confirmation_zoom_pair": [
                float(
                    best_pair[
                        "first"
                    ][
                        "zoom"
                    ]
                ),
                float(
                    best_pair[
                        "second"
                    ][
                        "zoom"
                    ]
                ),
            ],
            "metrics": dict(
                best_pair[
                    "metrics"
                ]
            ),
        }

        log(
            "   [Multi-Zoom] Validation passed using "
            f"zoom {best_pair['first']['zoom']} and "
            f"{best_pair['second']['zoom']}; "
            f"selected zoom {selected['zoom']}"
        )

        return selected[
            "ring"
        ]

    strongest = max(
        candidates,
        key=_boundary_candidate_score,
    )

    diagnostics = strongest.get(
        "diagnostics",
        {},
    )

    coverage = float(
        diagnostics.get(
            "coverage",
            0.0,
        )
        or 0.0
    )

    close_to_red = float(
        diagnostics.get(
            "close_to_red_ratio",
            0.0,
        )
        or 0.0
    )

    mean_red_distance = float(
        diagnostics.get(
            "mean_red_distance_px",
            99.0,
        )
        or 99.0
    )

    active_profile_settings = LOCALITY_PROFILES.get(
        ACTIVE_LOCALITY_PROFILE,
        LOCALITY_PROFILES[
            "medium"
        ],
    )

    strongest_is_final_eligible = (
        _candidate_is_final_eligible(
            strongest
        )
    )

    if not strongest_is_final_eligible:
        strongest_framing = (
            _candidate_framing(
                strongest
            )
        )

        log(
            "   [Framing] Strongest single result is "
            "confirmation-only: "
            f"zoom={float(strongest['zoom']):.2f}, "
            f"status={strongest_framing.get('status', 'unknown')}"
        )

    exceptionally_strong = (
        strongest_is_final_eligible
        and not strongest.get(
            "provisional",
            False,
        )
        and diagnostics.get("mode") == "exact"
        and coverage
        >= active_profile_settings[
            "single_min_coverage"
        ]
        and close_to_red
        >= active_profile_settings[
            "single_min_close_to_red"
        ]
        and mean_red_distance
        <= active_profile_settings[
            "single_max_mean_red_distance_px"
        ]
    )

    exact_shape_diagnostics = (
        diagnostics.get("exact_shape", {})
        if isinstance(
            diagnostics.get("exact_shape", {}),
            dict,
        )
        else {}
    )

    repair_diagnostics = (
        exact_shape_diagnostics.get("repair", {})
        if isinstance(
            exact_shape_diagnostics.get("repair", {}),
            dict,
        )
        else {}
    )

    guarded_support_requirement = float(
        exact_shape_diagnostics.get(
            "required_retained_support",
            PROFILE_RETAINED_SUPPORT_LIMITS.get(
                ACTIVE_LOCALITY_PROFILE,
                0.88,
            ),
        )
        or PROFILE_RETAINED_SUPPORT_LIMITS.get(
            ACTIVE_LOCALITY_PROFILE,
            0.88,
        )
    )

    guarded_exact_single = (
        strongest_is_final_eligible
        and not strongest.get(
            "provisional",
            False,
        )
        and diagnostics.get("mode") == "exact"
        and coverage >= 0.58
        and close_to_red >= 0.93
        and mean_red_distance <= 4.0
        and bool(
            exact_shape_diagnostics.get(
                "accepted",
                False,
            )
        )
        and float(
            exact_shape_diagnostics.get(
                "area_change_ratio",
                1.0,
            )
            or 1.0
        )
        <= 0.03
        and float(
            repair_diagnostics.get(
                "retained_red_support_ratio",
                0.0,
            )
            or 0.0
        )
        >= guarded_support_requirement
        and int(
            repair_diagnostics.get(
                "self_crossings_after_repair",
                1,
            )
            or 0
        )
        == 0
        and not (
            isinstance(
                exact_shape_diagnostics.get(
                    "shortcut",
                    {},
                ),
                dict,
            )
            and exact_shape_diagnostics.get(
                "shortcut",
                {},
            ).get(
                "suspected",
                False,
            )
        )
        and float(
            exact_shape_diagnostics.get(
                "max_step_px",
                999.0,
            )
            or 999.0
        )
        <= float(
            exact_shape_diagnostics.get(
                "max_step_tolerance_limit_px",
                exact_shape_diagnostics.get(
                    "max_allowed_step_px",
                    0.0,
                ),
            )
            or 0.0
        )
    )

    if exceptionally_strong:
        log(
            "   [Multi-Zoom] Only one compatible result, "
            "but it passed the exceptional single-result "
            f"safety rule at zoom {strongest['zoom']}"
        )

        return strongest["ring"]

    if guarded_exact_single:
        log(
            "   [Multi-Zoom] Guarded single-zoom exact "
            "acceptance passed at zoom "
            f"{strongest['zoom']}: "
            f"coverage={coverage:.3f}, "
            f"close_to_red={close_to_red:.3f}, "
            f"mean_red_distance={mean_red_distance:.2f}px"
        )

        return strongest["ring"]

    log(
        "   [Multi-Zoom] REVIEW REQUIRED: successful "
        "captures did not agree strongly enough and no "
        "single result met the strict coverage rule"
    )

    return None




def _zoom_label(
    zoom_value: float,
) -> str:
    zoom_text = (
        f"{float(zoom_value):.1f}"
        .rstrip("0")
        .rstrip(".")
        .replace(".", "_")
    )

    return (
        zoom_text
        if zoom_text
        else "unknown"
    )


def export_multizoom_review_candidates(
    candidates: list[dict],
    parsed: ParsedQuery,
    safe: str,
    output_dir: str,
    original_query: str,
    log=print,
) -> str:
    """
    Export every successful zoom candidate as a separate REVIEW file.

    These files are not final approved boundaries. They are only for opening
    each zoom candidate in Google Earth and choosing the correct one manually.
    """
    if (
        not REVIEW_CANDIDATE_EXPORT_ENABLED
        or not candidates
    ):
        return ""

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    boundary_level = (
        "locality"
        if parsed.locality
        else (
            "district"
            if parsed.district
            else (
                "state"
                if parsed.state_code
                and not parsed.district
                else "unknown"
            )
        )
    )

    sorted_candidates = sorted(
        candidates,
        key=_boundary_candidate_score,
        reverse=True,
    )

    report = {
        "status": "manual_review_required",
        "reason": (
            "Zoom candidates were detected, but their shapes did not "
            "agree strongly enough for automatic final export."
        ),
        "query": original_query,
        "display_name": parsed.display_name,
        "candidate_count": len(
            sorted_candidates
        ),
        "instructions": [
            "Open the REVIEW KML files one by one in Google Earth.",
            "Prefer an exact-mode candidate when it visually matches Google Maps.",
            "Do not sync REVIEW files until one candidate is approved.",
        ],
        "candidates": [],
        "pair_comparisons": [],
    }

    for candidate_index, candidate in enumerate(
        sorted_candidates,
        start=1,
    ):
        zoom_value = float(
            candidate.get(
                "zoom",
                0.0,
            )
            or 0.0
        )

        zoom_label = _zoom_label(
            zoom_value
        )

        candidate_safe = (
            f"{safe}_REVIEW_zoom{zoom_label}_"
            f"candidate{candidate_index}"
        )

        display_name = (
            f"{parsed.display_name} REVIEW "
            f"zoom {zoom_value:g} candidate {candidate_index}"
        )

        geojson_path = os.path.join(
            output_dir,
            f"{candidate_safe}.geojson",
        )

        kml_path = os.path.join(
            output_dir,
            f"{candidate_safe}.kml",
        )

        ring = candidate[
            "ring"
        ]

        diagnostics = dict(
            candidate.get(
                "diagnostics",
                {},
            )
        )

        save_geojson(
            ring,
            parsed.display_name,
            display_name,
            "google_maps_opencv_review",
            geojson_path,
            metadata={
                "query": original_query,
                "boundary_level": boundary_level,
                "locality": parsed.locality,
                "district": parsed.district,
                "state_code": parsed.state_code,
                "state": parsed.state_full,
                "review_candidate": True,
                "review_zoom": zoom_value,
                "review_candidate_rank": candidate_index,
                "review_score": round(
                    _boundary_candidate_score(
                        candidate
                    ),
                    4,
                ),
                "review_warning": (
                    "Manual review required before using this "
                    "boundary for sync/export decisions."
                ),
                "provisional": bool(candidate.get("provisional", False)),
                "gap_provisional": bool(_candidate_is_gap_provisional(candidate)),
                "graph_provisional": bool(_candidate_is_graph_provisional(candidate)),
                "candidate_source": str(candidate.get("source", "") or ""),
                "cv_diagnostics": diagnostics,
            },
            log=log,
        )

        save_kml(
            ring,
            display_name,
            kml_path,
            log=log,
        )

        kmz_path = save_kmz(
            kml_path,
            log=log,
        )

        candidate_record = {
            "rank": candidate_index,
            "zoom": zoom_value,
            "score": round(
                _boundary_candidate_score(
                    candidate
                ),
                4,
            ),
            "points": len(
                ring
            ),
            "mode": diagnostics.get(
                "mode",
                "",
            ),
            "coverage": diagnostics.get(
                "coverage",
                None,
            ),
            "close_to_red_ratio": diagnostics.get(
                "close_to_red_ratio",
                None,
            ),
            "mean_red_distance_px": diagnostics.get(
                "mean_red_distance_px",
                None,
            ),
            "geojson": geojson_path,
            "kml": kml_path,
            "kmz": kmz_path,
            "diagnostics": diagnostics,
        }

        report[
            "candidates"
        ].append(
            candidate_record
        )

        log(
            "   [Review Export] Candidate "
            f"{candidate_index}: zoom {zoom_value:g}, "
            f"mode={candidate_record['mode']}, "
            f"score={candidate_record['score']}, "
            f"KML={os.path.basename(kml_path)}"
        )

    for first_index in range(
        len(
            sorted_candidates
        )
    ):
        for second_index in range(
            first_index
            + 1,
            len(
                sorted_candidates
            ),
        ):
            first_candidate = sorted_candidates[
                first_index
            ]

            second_candidate = sorted_candidates[
                second_index
            ]

            metrics = _latlng_ring_pair_metrics(
                first_candidate[
                    "ring"
                ],
                second_candidate[
                    "ring"
                ],
            )

            if metrics:
                report[
                    "pair_comparisons"
                ].append({
                    "first_rank": first_index + 1,
                    "second_rank": second_index + 1,
                    "first_zoom": first_candidate.get(
                        "zoom"
                    ),
                    "second_zoom": second_candidate.get(
                        "zoom"
                    ),
                    "iou": round(
                        float(
                            metrics.get(
                                "iou",
                                0.0,
                            )
                        ),
                        4,
                    ),
                    "smaller_overlap_ratio": round(
                        float(
                            metrics.get(
                                "smaller_overlap_ratio",
                                0.0,
                            )
                        ),
                        4,
                    ),
                    "first_overlap_ratio": round(
                        float(
                            metrics.get(
                                "first_overlap_ratio",
                                0.0,
                            )
                        ),
                        4,
                    ),
                    "second_overlap_ratio": round(
                        float(
                            metrics.get(
                                "second_overlap_ratio",
                                0.0,
                            )
                        ),
                        4,
                    ),
                })

    report_path = os.path.join(
        output_dir,
        f"{safe}_manual_review_candidates.json",
    )

    os.makedirs(
        os.path.dirname(
            report_path
        )
        or ".",
        exist_ok=True,
    )

    with open(
        report_path,
        "w",
        encoding="utf-8",
    ) as report_file:
        json.dump(
            report,
            report_file,
            ensure_ascii=False,
            indent=2,
        )

    log(
        "   [Review Export] Manual review report → "
        f"{os.path.basename(report_path)}"
    )

    return report_path




def export_cv_failure_debug_report(
    attempts: list[dict],
    parsed: ParsedQuery,
    safe: str,
    output_dir: str,
    original_query: str,
    sam_boundary,
    log=print,
) -> str:
    """
    Save a machine-readable report when no final boundary is available.

    This is different from REVIEW KML export. Here there may be no accepted
    polygon at all, so the report points to screenshots and red-mask debug
    images instead.
    """
    if not CV_FAILURE_DEBUG_EXPORT_ENABLED:
        return ""

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    clean_attempts = []

    for attempt in attempts:
        diagnostics = dict(
            attempt.get(
                "diagnostics",
                {},
            )
        )

        clean_attempts.append({
            "zoom": attempt.get(
                "zoom"
            ),
            "accepted": bool(
                attempt.get(
                    "accepted",
                    False,
                )
            ),
            "screenshot": attempt.get(
                "screenshot",
                diagnostics.get(
                    "screenshot_path",
                    "",
                ),
            ),
            "status": diagnostics.get(
                "status",
                "",
            ),
            "reason": diagnostics.get(
                "reason",
                "",
            ),
            "clean_red_pixels": (
                diagnostics.get(
                    "cleaning",
                    {},
                ).get(
                    "remaining_boundary_pixels",
                    None,
                )
                if isinstance(
                    diagnostics.get(
                        "cleaning",
                        {},
                    ),
                    dict,
                )
                else None
            ),
            "cv_red": diagnostics.get(
                "cv_red",
                {},
            ),
            "roi": diagnostics.get(
                "roi",
                {},
            ),
            "target_pixel": diagnostics.get(
                "target_pixel",
                {},
            ),
            "debug_images": diagnostics.get(
                "debug_images",
                {},
            ),
            "best_rejection": diagnostics.get(
                "best_rejection",
                diagnostics,
            ),
        })

    report = {
        "status": "failed_no_final_boundary",
        "query": original_query,
        "display_name": parsed.display_name,
        "locality": parsed.locality,
        "district": parsed.district,
        "state": parsed.state_full,
        "state_code": parsed.state_code,
        "reason": (
            "No accepted Google-red boundary candidate and no reliable "
            "SAM fallback boundary were available."
        ),
        "sam_available": sam_boundary is not None,
        "attempt_count": len(
            clean_attempts
        ),
        "attempts": clean_attempts,
        "how_to_inspect": [
            "Open each screenshot listed in attempts.",
            "Open the matching *_red_pixels_cleaned.png image.",
            "If clean_red_pixels is low, Google did not expose a usable boundary or it was removed as noise.",
            "If best_rejection shows max_step failure, the red boundary existed but had a broken gap that exact repair could not safely close.",
        ],
    }

    report_path = os.path.join(
        output_dir,
        f"{safe}_cv_failure_debug_report.json",
    )

    os.makedirs(
        os.path.dirname(
            report_path
        )
        or ".",
        exist_ok=True,
    )

    with open(
        report_path,
        "w",
        encoding="utf-8",
    ) as report_file:
        json.dump(
            report,
            report_file,
            ensure_ascii=False,
            indent=2,
        )

    log(
        "   [Failure Debug] CV failure report → "
        f"{os.path.basename(report_path)}"
    )

    return report_path




def export_repaired_review_candidates(
    candidates: list[dict],
    parsed: ParsedQuery,
    safe: str,
    output_dir: str,
    original_query: str,
    log=print,
) -> str:
    """
    Export repaired exact contours that are safe for manual review but not
    safe for automatic final export.
    """
    if (
        not REPAIRED_REVIEW_EXPORT_ENABLED
        or not candidates
    ):
        return ""

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    boundary_level = (
        "locality"
        if parsed.locality
        else (
            "district"
            if parsed.district
            else (
                "state"
                if parsed.state_code
                and not parsed.district
                else "unknown"
            )
        )
    )

    report = {
        "status": "manual_review_required",
        "review_type": "repaired_exact_contour",
        "query": original_query,
        "display_name": parsed.display_name,
        "candidate_count": len(
            candidates
        ),
        "instructions": [
            "Open the repaired REVIEW KML in Google Earth.",
            "Compare it with the Google Maps dotted red boundary.",
            "Use it only if the outer boundary visually matches.",
            "Do not sync this REVIEW file until you approve it.",
        ],
        "candidates": [],
    }

    for index, candidate in enumerate(
        candidates,
        start=1,
    ):
        zoom_value = float(
            candidate.get(
                "zoom",
                0.0,
            )
            or 0.0
        )

        zoom_label = _zoom_label(
            zoom_value
        )

        candidate_safe = (
            f"{safe}_REVIEW_repaired_zoom"
            f"{zoom_label}_candidate{index}"
        )

        display_name = (
            f"{parsed.display_name} REVIEW repaired "
            f"zoom {zoom_value:g} candidate {index}"
        )

        geojson_path = os.path.join(
            output_dir,
            f"{candidate_safe}.geojson",
        )

        kml_path = os.path.join(
            output_dir,
            f"{candidate_safe}.kml",
        )

        ring = candidate[
            "ring"
        ]

        diagnostics = dict(
            candidate.get(
                "diagnostics",
                {},
            )
        )

        save_geojson(
            ring,
            parsed.display_name,
            display_name,
            candidate.get(
                "source",
                "google_maps_opencv_repaired_review",
            ),
            geojson_path,
            metadata={
                "query": original_query,
                "boundary_level": boundary_level,
                "locality": parsed.locality,
                "district": parsed.district,
                "state_code": parsed.state_code,
                "state": parsed.state_full,
                "review_candidate": True,
                "review_type": "repaired_exact_contour",
                "review_zoom": zoom_value,
                "review_candidate_rank": index,
                "review_warning": (
                    "This repaired boundary is for manual review only. "
                    "It was not automatically approved because part of "
                    "the boundary was interpolated."
                ),
                "cv_diagnostics": diagnostics,
            },
            log=log,
        )

        save_kml(
            ring,
            display_name,
            kml_path,
            log=log,
        )

        kmz_path = save_kmz(
            kml_path,
            log=log,
        )

        record = {
            "rank": index,
            "zoom": zoom_value,
            "points": len(
                ring
            ),
            "reason": candidate.get(
                "reason",
                "manual_review_required",
            ),
            "geojson": geojson_path,
            "kml": kml_path,
            "kmz": kmz_path,
            "diagnostics": diagnostics,
        }

        report[
            "candidates"
        ].append(
            record
        )

        log(
            "   [Review Export] Repaired candidate "
            f"{index}: zoom {zoom_value:g}, "
            f"KML={os.path.basename(kml_path)}"
        )

    report_path = os.path.join(
        output_dir,
        f"{safe}_repaired_review_candidates.json",
    )

    os.makedirs(
        os.path.dirname(
            report_path
        )
        or ".",
        exist_ok=True,
    )

    with open(
        report_path,
        "w",
        encoding="utf-8",
    ) as report_file:
        json.dump(
            report,
            report_file,
            ensure_ascii=False,
            indent=2,
        )

    log(
        "   [Review Export] Repaired review report → "
        f"{os.path.basename(report_path)}"
    )

    return report_path



def run_pipeline(
    query:      str,
    output_dir: str = "boundaries",
    log=print,
) -> PipelineResult:
    """
    Run the boundary pipeline, preferring satellite imagery.

    Google draws the locality outline almost continuously over satellite
    imagery but leaves large holes in the default map view, so satellite is
    tried first. If it fails for any reason the proven map view is used, so
    enabling satellite can never make a query worse than before.
    """
    global SATELLITE_BOUNDARY_MODE
    global ACTIVE_SATELLITE_VIEW

    satellite_first = bool(
        SATELLITE_BOUNDARY_MODE
    )

    result = _run_pipeline_once(
        query=query,
        output_dir=output_dir,
        log=log,
    )

    if (
        result.status == "Success"
        or not satellite_first
        or not SATELLITE_AUTO_FALLBACK_TO_MAP
    ):
        return result

    log(
        "\n   [Satellite] Satellite capture did not produce a boundary; "
        "retrying once with the default map view"
    )

    SATELLITE_BOUNDARY_MODE = False
    ACTIVE_SATELLITE_VIEW = False

    try:
        fallback = _run_pipeline_once(
            query=query,
            output_dir=output_dir,
            log=log,
        )
    finally:
        SATELLITE_BOUNDARY_MODE = satellite_first
        ACTIVE_SATELLITE_VIEW = False

    if fallback.status == "Success":
        log(
            "   [Satellite] Map-view fallback succeeded"
        )
        return fallback

    return result


def _run_pipeline_once(
    query:      str,
    output_dir: str = "boundaries",
    log=print,
) -> PipelineResult:
    global ACTIVE_CAPTURE_PROFILE_TRUSTED
    global ACTIVE_EXPECTED_SELECTED_LABEL
    global ACTIVE_SELECTED_PLACE_TRUSTED
    global LAST_SELECTED_PLACE_LOCK

    global ACTIVE_SATELLITE_VIEW

    ACTIVE_CAPTURE_PROFILE_TRUSTED = True
    ACTIVE_SELECTED_PLACE_TRUSTED = True
    ACTIVE_SATELLITE_VIEW = False
    LAST_SELECTED_PLACE_LOCK = {}

    result = PipelineResult(query=query)
    os.makedirs(output_dir, exist_ok=True)

    export_directories = (
        prepare_exact_google_directories(
            output_dir,
            log=log,
        )
    )

    original_output_dir = (
        export_directories[
            "original"
        ]
    )

    review_output_dir = (
        export_directories[
            "review"
        ]
    )

    report_output_dir = (
        export_directories[
            "reports"
        ]
    )

    safe   = _safe_name(query)
    driver = None

    try:
        log(f"\n{'='*60}")
        log(f"  PIPELINE: {query}")
        log(f"{'='*60}")
        parsed = parse_query(query)

        ACTIVE_EXPECTED_SELECTED_LABEL = (
            parsed.locality
            or parsed.display_name
            or parsed.google_query
        )

        log(f"   [Parse] google_query='{parsed.google_query}'")

        driver, view = open_maps_playwright(
            parsed.google_query,
            log=log,
        )

        (
            view,
            initial_selected_place_lock,
        ) = ensure_selected_place_lock(
            driver=driver,
            expected_label=ACTIVE_EXPECTED_SELECTED_LABEL,
            google_query=parsed.google_query,
            view=view,
            target_zoom=None,
            log=log,
        )

        if parsed.locality:
            view = prepare_locality_outline_view(
                driver=driver,
                view=view,
                log=log,
            )

            global INITIAL_GOOGLE_LOCALITY_ZOOM

            google_selected_zoom = float(
                view.get(
                    "zoom",
                    15.0,
                )
            )

            INITIAL_GOOGLE_LOCALITY_ZOOM = (
                google_selected_zoom
            )

            log(
                "   [Framing] Google initial locality zoom="
                f"{INITIAL_GOOGLE_LOCALITY_ZOOM:.2f}; "
                "minimum final zoom="
                f"{_minimum_final_zoom():.2f}"
            )

            (
                initial_profile,
                initial_profile_reason,
            ) = _profile_from_google_zoom(
                google_selected_zoom
            )

            _set_active_locality_profile(
                initial_profile,
                initial_profile_reason,
                log=log,
            )

            initial_target_zoom = int(
                LOCALITY_PROFILES[
                    ACTIVE_LOCALITY_PROFILE
                ][
                    "initial_zoom"
                ]
            )

            view = set_actual_map_zoom(
                driver=driver,
                view=view,
                target_zoom=initial_target_zoom,
                log=log,
            )

            (
                view,
                initial_selected_place_lock,
            ) = ensure_selected_place_lock(
                driver=driver,
                expected_label=ACTIVE_EXPECTED_SELECTED_LABEL,
                google_query=parsed.google_query,
                view=view,
                target_zoom=initial_target_zoom,
                log=log,
            )

            view = recenter_map_on_place(
                driver=driver,
                view=view,
                log=log,
            )

        result.center_lat = float(
            view.get(
                "place_lat",
                view["lat"],
            )
        )
        result.center_lng = float(
            view.get(
                "place_lng",
                view["lng"],
            )
        )

        close_maps_ui(
            driver,
            log=log,
        )

        if parsed.locality:
            view = recenter_map_on_place(
                driver=driver,
                view=view,
                log=log,
            )

            close_maps_ui(
                driver,
                log=log,
            )

        time.sleep(3)

        updated_view = _parse_maps_url(
            driver.current_url
        )

        if updated_view:
            if view.get("place_lat") is not None:
                updated_view["place_lat"] = view["place_lat"]

            if view.get("place_lng") is not None:
                updated_view["place_lng"] = view["place_lng"]

            updated_view["name"] = view.get(
                "name",
                parsed.display_name,
            )

            view = updated_view

        (
            view,
            initial_selected_place_lock,
        ) = ensure_selected_place_lock(
            driver=driver,
            expected_label=ACTIVE_EXPECTED_SELECTED_LABEL,
            google_query=parsed.google_query,
            view=view,
            target_zoom=float(view["zoom"]),
            log=log,
        )

        if initial_selected_place_lock.get("restored", False):
            view = recenter_map_on_place(
                driver=driver,
                view=view,
                log=log,
            )
            close_maps_ui(driver, log=log)

        # Switch to satellite before the first capture. Google draws the
        # locality outline almost continuously over imagery, while the map
        # view leaves large holes under highways and label text.
        if SATELLITE_BOUNDARY_MODE:
            satellite_enabled = set_satellite_view(
                driver=driver,
                enabled=True,
                log=log,
            )

            # A failed switch can still have navigated, which reopens the
            # place panel and would shrink the captured map. Always restore
            # the selected place and the collapsed panel afterwards.
            (
                view,
                initial_selected_place_lock,
            ) = ensure_selected_place_lock(
                driver=driver,
                expected_label=(
                    ACTIVE_EXPECTED_SELECTED_LABEL
                ),
                google_query=parsed.google_query,
                view=view,
                target_zoom=float(
                    view["zoom"]
                ),
                log=log,
            )

            close_maps_ui(
                driver,
                log=log,
            )

            satellite_view = _parse_maps_url(
                driver.current_url
            )

            if satellite_view:
                view = _merge_camera_with_place(
                    satellite_view,
                    view,
                )

            if not satellite_enabled:
                log(
                    "   [Satellite] Continuing with the map view; "
                    "capture state was restored"
                )

        map_metrics = get_map_viewport_metrics(
            driver,
            log=log,
        )

        wait_for_google_red_outline(
            driver=driver,
            map_metrics=map_metrics,
            log=log,
        )

        ss_path = os.path.join(output_dir, f"{safe}_screenshot.png")

        (
            _,
            img_w,
            img_h,
            initial_red_render_metrics,
            initial_red_render_usable,
        ) = capture_screenshot_with_red_flood_guard(
            driver,
            ss_path,
            map_metrics=map_metrics,
            reference_red_pixels=0,
            log=log,
        )

        last_clean_red_render_pixels = (
            int(
                initial_red_render_metrics.get(
                    "map_red_pixels",
                    0,
                )
                or 0
            )
            if initial_red_render_usable
            else 0
        )
        result.screenshot_path = ss_path

        if view["zoom"] < 11.9:
            raise RuntimeError(
                "Adaptive locality capture requires zoom 12 "
                f"or higher, but Google Maps is at {view['zoom']}."
            )

        georef = extract_georeference(
            view,
            img_w,
            img_h,
            map_metrics,
            log=log,
        )

        tif_path = os.path.join(output_dir, f"{safe}.tif")
        save_geotiff(
            ss_path,
            georef,
            tif_path,
            log=log,
        )
        result.geotiff_path = tif_path

        log(
            "   [GeoTIFF] Detailed view preserved at "
            f"zoom {view['zoom']}; the GeoTIFF is re-saved "
            "from whichever zoom supplies the final polygon"
        )

        # Remember the screenshot and georeference used by every zoom so the
        # exported GeoTIFF can be rebuilt from the exact capture that
        # produced the final polygon. Without this the raster and the KML can
        # come from different zooms and never overlay perfectly.
        capture_registry = {
            round(
                float(
                    view["zoom"]
                ),
                2,
            ): {
                "screenshot": ss_path,
                "georef": georef,
            },
        }

        # Trace Google's explicit red dotted locality outline at every useful
        # zoom, then compare the resulting polygons before export.
        boundary_candidates = []
        repaired_review_candidates = []
        cv_attempts = []
        attempted_zooms = {
            int(
                round(
                    float(
                        view[
                            "zoom"
                        ]
                    )
                )
            )
        }

        pre_initial_centering_snapshot = dict(
            LAST_RECENTER_DIAGNOSTICS
        )

        initial_camera_trusted = (
            _centering_snapshot_is_trusted(
                pre_initial_centering_snapshot
            )
        )

        initial_selected_place_trusted = bool(
            initial_selected_place_lock.get("trusted", False)
        )
        ACTIVE_SELECTED_PLACE_TRUSTED = initial_selected_place_trusted

        ACTIVE_CAPTURE_PROFILE_TRUSTED = bool(
            initial_red_render_usable
            and initial_camera_trusted
            and initial_selected_place_trusted
        )

        if not ACTIVE_CAPTURE_PROFILE_TRUSTED:
            log(
                "   [Capture Trust] Initial zoom is "
                "diagnostic-only: "
                f"camera_safe={initial_camera_trusted}, "
                f"red_render_safe={initial_red_render_usable}, "
                f"selected_place_safe={initial_selected_place_trusted}"
            )

        initial_polygon = detect_boundary_cv(
            screenshot_path=ss_path,
            georef=georef,
            log=log,
        )

        initial_diagnostics_snapshot = dict(
            LAST_CV_DIAGNOSTICS
        )

        initial_centering_snapshot = dict(
            LAST_RECENTER_DIAGNOSTICS
        )

        cv_attempts.append({
            "zoom": float(
                view[
                    "zoom"
                ]
            ),
            "accepted": bool(
                initial_polygon is not None
                and ACTIVE_CAPTURE_PROFILE_TRUSTED
            ),
            "capture_trusted": bool(
                ACTIVE_CAPTURE_PROFILE_TRUSTED
            ),
            "red_render": dict(
                initial_red_render_metrics
            ),
            "selected_place": dict(
                initial_selected_place_lock
            ),
            "screenshot": ss_path,
            "diagnostics": initial_diagnostics_snapshot,
            "target_centering": (
                initial_centering_snapshot
            ),
        })

        (
            inferred_profile,
            inferred_reason,
        ) = _infer_profile_from_attempt(
            ACTIVE_LOCALITY_PROFILE,
            initial_diagnostics_snapshot,
            georef,
        )

        _set_active_locality_profile(
            inferred_profile,
            inferred_reason,
            log=log,
        )

        for review_candidate in (
            LAST_CV_REVIEW_CANDIDATES
            if ACTIVE_CAPTURE_PROFILE_TRUSTED
            else []
        ):
            repaired_entry = {
                **review_candidate,
                "zoom": float(
                    view[
                        "zoom"
                    ]
                ),
                "screenshot": ss_path,
            }

            repaired_review_candidates.append(
                repaired_entry
            )

            boundary_candidates.append({
                "zoom": float(
                    view[
                        "zoom"
                    ]
                ),
                "ring": repaired_entry[
                    "ring"
                ],
                "diagnostics": repaired_entry[
                    "diagnostics"
                ],
                "provisional": True,
                "source": (
                    "low_support_repaired_candidate"
                ),
            })

            log(
                "   [Adaptive] Repaired REVIEW contour "
                "added as provisional; another zoom must "
                "confirm it"
            )

        if (
            initial_polygon is not None
            and ACTIVE_CAPTURE_PROFILE_TRUSTED
        ):
            initial_diagnostics = dict(
                LAST_CV_DIAGNOSTICS
            )

            initial_diagnostics[
                "capture_zoom"
            ] = float(
                view[
                    "zoom"
                ]
            )

            initial_is_provisional = bool(initial_diagnostics.get("requires_cross_zoom_confirmation", False))
            initial_is_graph = initial_diagnostics.get("provisional_reason") == "directional_graph_reconstruction"
            initial_is_gap = initial_diagnostics.get("provisional_reason") in {
                "max_step_only_gap_repair",
                "local_gap_path_repair",
                "exact_repair_confirmation",
            }
            initial_is_local_gap = initial_diagnostics.get("provisional_reason") == "local_gap_path_repair"
            initial_is_exact_confirmation = initial_diagnostics.get("provisional_reason") == "exact_repair_confirmation"
            initial_is_bootstrap = initial_diagnostics.get("provisional_reason") == "preserved_mask_polar_bootstrap"
            boundary_candidates.append({
                "zoom": float(view["zoom"]), "ring": initial_polygon, "diagnostics": initial_diagnostics,
                "provisional": initial_is_provisional, "gap_provisional": initial_is_gap,
                "graph_provisional": initial_is_graph,
                "source": "directional_graph_reconstruction" if initial_is_graph else ("local_gap_path_repair" if initial_is_local_gap else ("exact_repair_confirmation" if initial_is_exact_confirmation else ("preserved_mask_polar_bootstrap" if initial_is_bootstrap else ("max_step_gap_repair" if initial_is_gap else "accepted_cv_candidate")))),
            })
            label = "Provisional graph candidate" if initial_is_graph else ("Provisional local-gap candidate" if initial_is_local_gap else ("Provisional exact-repair candidate" if initial_is_exact_confirmation else ("Provisional bootstrap-line candidate" if initial_is_bootstrap else ("Provisional gap candidate" if initial_is_gap else "Candidate"))))
            log(f"   [Multi-Zoom] {label} saved from zoom {view['zoom']}")

        if parsed.locality:
            retry_queue = _adaptive_zoom_plan(
                ACTIVE_LOCALITY_PROFILE,
                float(
                    view[
                        "zoom"
                    ]
                ),
            )

            capture_count = 1

            while (
                retry_queue
                and capture_count
                < ADAPTIVE_MAX_CAPTURE_COUNT
            ):
                retry_zoom = retry_queue.pop(
                    0
                )

                if retry_zoom in attempted_zooms:
                    continue

                attempted_zooms.add(
                    retry_zoom
                )

                capture_count += 1
                log(
                    "   [Retry] Capturing comparison boundary "
                    f"at zoom {retry_zoom}..."
                )

                (
                    view,
                    retry_selected_place_lock,
                    retry_zoom_integrity,
                ) = lock_selected_place_at_requested_zoom(
                    driver=driver,
                    expected_label=(
                        ACTIVE_EXPECTED_SELECTED_LABEL
                    ),
                    google_query=parsed.google_query,
                    view=view,
                    requested_zoom=float(
                        retry_zoom
                    ),
                    log=log,
                )

                if not retry_zoom_integrity.get(
                    "trusted",
                    False,
                ):
                    actual_zoom = float(
                        retry_zoom_integrity.get(
                            "actual_zoom",
                            view.get(
                                "zoom",
                                0.0,
                            ),
                        )
                    )

                    skipped_diagnostics = {
                        "status": "skipped",
                        "reason": "zoom_integrity_failed",
                        "requested_zoom": float(
                            retry_zoom
                        ),
                        "actual_zoom": actual_zoom,
                        "selected_place": dict(
                            retry_selected_place_lock
                        ),
                        "zoom_integrity": dict(
                            retry_zoom_integrity
                        ),
                    }

                    cv_attempts.append({
                        "zoom": actual_zoom,
                        "requested_zoom": float(
                            retry_zoom
                        ),
                        "accepted": False,
                        "capture_trusted": False,
                        "screenshot": "",
                        "diagnostics": (
                            skipped_diagnostics
                        ),
                        "selected_place": dict(
                            retry_selected_place_lock
                        ),
                        "zoom_integrity": dict(
                            retry_zoom_integrity
                        ),
                        "target_centering": dict(
                            LAST_RECENTER_DIAGNOSTICS
                        ),
                    })

                    log(
                        "   [Retry] Skipped comparison capture: "
                        f"requested zoom {float(retry_zoom):.2f}, "
                        f"actual zoom {actual_zoom:.2f}. "
                        "No incorrectly labelled screenshot was saved."
                    )

                    continue

                log(
                    "   [Retry] Verified capture camera: "
                    f"{view['lat']:.5f}, {view['lng']:.5f}, "
                    f"zoom {view['zoom']}"
                )

                result.center_lat = float(
                    view.get(
                        "place_lat",
                        view[
                            "lat"
                        ],
                    )
                )

                result.center_lng = float(
                    view.get(
                        "place_lng",
                        view[
                            "lng"
                        ],
                    )
                )

                time.sleep(
                    2
                )

                map_metrics = get_map_viewport_metrics(
                    driver,
                    log=log,
                )

                wait_for_google_red_outline(
                    driver=driver,
                    map_metrics=map_metrics,
                    log=log,
                )

                retry_screenshot_path = os.path.join(
                    output_dir,
                    f"{safe}_screenshot_zoom{retry_zoom}.png",
                )

                (
                    _,
                    img_w,
                    img_h,
                    retry_red_render_metrics,
                    retry_red_render_usable,
                ) = capture_screenshot_with_red_flood_guard(
                    driver,
                    retry_screenshot_path,
                    map_metrics=map_metrics,
                    reference_red_pixels=(
                        last_clean_red_render_pixels
                    ),
                    log=log,
                )

                if retry_red_render_usable:
                    last_clean_red_render_pixels = int(
                        retry_red_render_metrics.get(
                            "map_red_pixels",
                            last_clean_red_render_pixels,
                        )
                        or last_clean_red_render_pixels
                    )

                georef = extract_georeference(
                    view,
                    img_w,
                    img_h,
                    map_metrics,
                    log=log,
                )

                capture_registry[
                    round(
                        float(
                            view["zoom"]
                        ),
                        2,
                    )
                ] = {
                    "screenshot": retry_screenshot_path,
                    "georef": georef,
                }

                log(
                    "   [GeoTIFF] Registered zoom-"
                    f"{retry_zoom} capture; the final GeoTIFF "
                    "will match the exported polygon"
                )

                pre_retry_centering_snapshot = dict(
                    LAST_RECENTER_DIAGNOSTICS
                )

                retry_camera_trusted = (
                    _centering_snapshot_is_trusted(
                        pre_retry_centering_snapshot
                    )
                )

                retry_selected_place_trusted = bool(
                    retry_selected_place_lock.get("trusted", False)
                )
                ACTIVE_SELECTED_PLACE_TRUSTED = retry_selected_place_trusted

                retry_zoom_integrity_trusted = bool(
                    retry_zoom_integrity.get(
                        "trusted",
                        False,
                    )
                )

                ACTIVE_CAPTURE_PROFILE_TRUSTED = bool(
                    retry_red_render_usable
                    and retry_camera_trusted
                    and retry_selected_place_trusted
                    and retry_zoom_integrity_trusted
                )

                if not ACTIVE_CAPTURE_PROFILE_TRUSTED:
                    log(
                        "   [Capture Trust] Zoom "
                        f"{float(view['zoom']):.2f} is "
                        "diagnostic-only: "
                        f"camera_safe={retry_camera_trusted}, "
                        f"red_render_safe={retry_red_render_usable}, "
                        f"selected_place_safe={retry_selected_place_trusted}, "
                        f"zoom_integrity_safe="
                        f"{retry_zoom_integrity_trusted}"
                    )

                retry_polygon = detect_boundary_cv(
                    screenshot_path=retry_screenshot_path,
                    georef=georef,
                    log=log,
                )

                retry_diagnostics_snapshot = dict(
                    LAST_CV_DIAGNOSTICS
                )

                retry_centering_snapshot = dict(
                    LAST_RECENTER_DIAGNOSTICS
                )

                cv_attempts.append({
                    "zoom": float(
                        view[
                            "zoom"
                        ]
                    ),
                    "accepted": bool(
                        retry_polygon is not None
                        and ACTIVE_CAPTURE_PROFILE_TRUSTED
                    ),
                    "capture_trusted": bool(
                        ACTIVE_CAPTURE_PROFILE_TRUSTED
                    ),
                    "red_render": dict(
                        retry_red_render_metrics
                    ),
                    "selected_place": dict(
                        retry_selected_place_lock
                    ),
                    "requested_zoom": float(
                        retry_zoom
                    ),
                    "zoom_integrity": dict(
                        retry_zoom_integrity
                    ),
                    "screenshot": retry_screenshot_path,
                    "diagnostics": retry_diagnostics_snapshot,
                    "target_centering": (
                        retry_centering_snapshot
                    ),
                })

                (
                    inferred_profile,
                    inferred_reason,
                ) = _infer_profile_from_attempt(
                    ACTIVE_LOCALITY_PROFILE,
                    retry_diagnostics_snapshot,
                    georef,
                )

                old_profile = ACTIVE_LOCALITY_PROFILE

                _set_active_locality_profile(
                    inferred_profile,
                    inferred_reason,
                    log=log,
                )

                if ACTIVE_LOCALITY_PROFILE != old_profile:
                    rebuilt_retry_queue = []

                    for additional_zoom in _adaptive_zoom_plan(
                        ACTIVE_LOCALITY_PROFILE,
                        float(
                            view[
                                "zoom"
                            ]
                        ),
                    ):
                        if (
                            additional_zoom
                            not in attempted_zooms
                            and additional_zoom
                            not in rebuilt_retry_queue
                        ):
                            rebuilt_retry_queue.append(
                                additional_zoom
                            )

                    retry_queue = rebuilt_retry_queue

                    log(
                        "   [Adaptive] Retry queue rebuilt for "
                        f"{ACTIVE_LOCALITY_PROFILE.upper()}: "
                        + (
                            ", ".join(
                                str(
                                    zoom_value
                                )
                                for zoom_value
                                in retry_queue
                            )
                            if retry_queue
                            else "no remaining zooms"
                        )
                    )

                for review_candidate in (
                    LAST_CV_REVIEW_CANDIDATES
                    if ACTIVE_CAPTURE_PROFILE_TRUSTED
                    else []
                ):
                    repaired_entry = {
                        **review_candidate,
                        "zoom": float(
                            view[
                                "zoom"
                            ]
                        ),
                        "screenshot": (
                            retry_screenshot_path
                        ),
                    }

                    repaired_review_candidates.append(
                        repaired_entry
                    )

                    boundary_candidates.append({
                        "zoom": float(
                            view[
                                "zoom"
                            ]
                        ),
                        "ring": repaired_entry[
                            "ring"
                        ],
                        "diagnostics": (
                            repaired_entry[
                                "diagnostics"
                            ]
                        ),
                        "provisional": True,
                        "source": (
                            "low_support_repaired_candidate"
                        ),
                    })

                    log(
                        "   [Adaptive] Repaired REVIEW contour "
                        "added as provisional; another zoom "
                        "must confirm it"
                    )

                if (
                    retry_polygon is not None
                    and ACTIVE_CAPTURE_PROFILE_TRUSTED
                ):
                    retry_diagnostics = dict(
                        LAST_CV_DIAGNOSTICS
                    )

                    retry_diagnostics[
                        "capture_zoom"
                    ] = float(
                        view[
                            "zoom"
                        ]
                    )

                    retry_is_provisional = bool(retry_diagnostics.get("requires_cross_zoom_confirmation", False))
                    retry_is_graph = retry_diagnostics.get("provisional_reason") == "directional_graph_reconstruction"
                    retry_is_gap = retry_diagnostics.get("provisional_reason") in {
                        "max_step_only_gap_repair",
                        "local_gap_path_repair",
                        "exact_repair_confirmation",
                    }
                    retry_is_local_gap = retry_diagnostics.get("provisional_reason") == "local_gap_path_repair"
                    retry_is_exact_confirmation = retry_diagnostics.get("provisional_reason") == "exact_repair_confirmation"
                    retry_is_bootstrap = retry_diagnostics.get("provisional_reason") == "preserved_mask_polar_bootstrap"
                    boundary_candidates.append({
                        "zoom": float(view["zoom"]), "ring": retry_polygon, "diagnostics": retry_diagnostics,
                        "provisional": retry_is_provisional, "gap_provisional": retry_is_gap,
                        "graph_provisional": retry_is_graph,
                        "source": "directional_graph_reconstruction" if retry_is_graph else ("local_gap_path_repair" if retry_is_local_gap else ("exact_repair_confirmation" if retry_is_exact_confirmation else ("preserved_mask_polar_bootstrap" if retry_is_bootstrap else ("max_step_gap_repair" if retry_is_gap else "accepted_cv_candidate")))),
                    })
                    label = "Provisional graph candidate" if retry_is_graph else ("Provisional local-gap candidate" if retry_is_local_gap else ("Provisional exact-repair candidate" if retry_is_exact_confirmation else ("Provisional bootstrap-line candidate" if retry_is_bootstrap else ("Provisional gap candidate" if retry_is_gap else "Candidate"))))
                    log(f"   [Multi-Zoom] {label} saved from zoom {view['zoom']}")

                else:
                    log(
                        "   [Multi-Zoom] No accepted candidate "
                        f"from zoom {view['zoom']}"
                    )

            accepted_zoom_values = {
                float(
                    candidate[
                        "zoom"
                    ]
                )
                for candidate
                in boundary_candidates
                if not candidate.get(
                    "provisional",
                    False,
                )
            }

            boundary_candidates = [
                candidate
                for candidate
                in boundary_candidates
                if (
                    not candidate.get(
                        "provisional",
                        False,
                    )
                    or float(
                        candidate[
                            "zoom"
                        ]
                    )
                    not in accepted_zoom_values
                )
            ]

            log(
                "   [Adaptive] Final profile="
                f"{ACTIVE_LOCALITY_PROFILE}; "
                "attempted zooms="
                + ", ".join(
                    str(zoom_value)
                    for zoom_value
                    in sorted(
                        attempted_zooms,
                        reverse=True,
                    )
                )
            )

            cv_polygon = select_multizoom_boundary(
                boundary_candidates,
                log=log,
            )

            # Re-save the GeoTIFF from the capture that produced the final
            # polygon so the raster and the exported KML/GeoJSON describe the
            # exact same view and overlay perfectly in GIS software.
            if cv_polygon:
                selected_zoom_value = (
                    LAST_MULTI_ZOOM_SELECTION.get(
                        "selected_zoom"
                    )
                )

                selected_capture = (
                    capture_registry.get(
                        round(
                            float(
                                selected_zoom_value
                            ),
                            2,
                        )
                    )
                    if selected_zoom_value is not None
                    else None
                )

                if selected_capture:
                    georef = selected_capture[
                        "georef"
                    ]

                    result.screenshot_path = (
                        selected_capture[
                            "screenshot"
                        ]
                    )

                    try:
                        save_geotiff(
                            selected_capture[
                                "screenshot"
                            ],
                            georef,
                            tif_path,
                            log=log,
                        )

                        log(
                            "   [GeoTIFF] Rebuilt from the zoom-"
                            f"{float(selected_zoom_value):.2f} capture "
                            "that produced the final polygon"
                        )

                    except Exception as exc:
                        log(
                            "   [GeoTIFF] Could not rebuild from the "
                            f"selected zoom safely: {exc}"
                        )

            if not boundary_candidates:
                log(
                    "   [Multi-Zoom] No CV candidate survived "
                    "the adaptive zoom captures; continuing to SAM "
                    "without reloading the selected Google Maps page"
                )

                if repaired_review_candidates:
                    export_repaired_review_candidates(
                        candidates=repaired_review_candidates,
                        parsed=parsed,
                        safe=safe,
                        output_dir=review_output_dir,
                        original_query=query,
                        log=log,
                    )

                export_cv_failure_debug_report(
                    attempts=cv_attempts,
                    parsed=parsed,
                    safe=safe,
                    output_dir=report_output_dir,
                    original_query=query,
                    sam_boundary=None,
                    log=log,
                )

            if (
                boundary_candidates
                and cv_polygon is None
            ):
                export_multizoom_review_candidates(
                    candidates=boundary_candidates,
                    parsed=parsed,
                    safe=safe,
                    output_dir=review_output_dir,
                    original_query=query,
                    log=log,
                )

                best_candidate = max(boundary_candidates, key=_boundary_candidate_score)
                cv_polygon = best_candidate.get("ring")
                if best_candidate.get("georef"):
                    georef = best_candidate["georef"]
                if best_candidate.get("screenshot_path") and os.path.exists(best_candidate["screenshot_path"]):
                    ss_path = best_candidate["screenshot_path"]

                log(
                    "   [Auto-Promote] Using highest scoring candidate from "
                    f"zoom {best_candidate.get('zoom')} (score={_boundary_candidate_score(best_candidate):.4f}) "
                    "as final boundary."
                )

        else:
            cv_polygon = initial_polygon

        sam_boundary = None

        controlled_terminal_reasons = {
            "no_boundary_like_red_evidence",
            "selected_place_not_locked",
            "zoom_integrity_failed",
        }

        cv_has_explicit_boundary_absence = any(
            (
                isinstance(
                    attempt.get(
                        "diagnostics"
                    ),
                    dict,
                )
                and attempt[
                    "diagnostics"
                ].get(
                    "reason"
                )
                == "no_boundary_like_red_evidence"
            )
            for attempt
            in cv_attempts
        )

        # SAM is blocked only when EVERY capture attempt explicitly found
        # no red boundary evidence. Zoom-integrity failures and place-lock
        # failures do NOT count as boundary absence — SAM can still recover
        # from a broken or partially visible red outline.
        cv_boundary_absence_only = bool(
            cv_attempts
            and all(
                (
                    isinstance(
                        attempt.get(
                            "diagnostics"
                        ),
                        dict,
                    )
                    and attempt[
                        "diagnostics"
                    ].get(
                        "reason"
                    )
                    == "no_boundary_like_red_evidence"
                )
                for attempt
                in cv_attempts
            )
        )

        if (
            cv_polygon is None
            and cv_boundary_absence_only
        ):
            log(
                "   [SAM] Skipped because every safe Google Maps "
                "capture lacked a trustworthy explicit locality "
                "boundary. SAM cannot recover Google's missing "
                "proprietary outline."
            )

        # SAM is only a final fallback. Do not run it when Google's explicit
        # red boundary has already been traced successfully or when every
        # Google capture explicitly lacked a boundary.
        if (
            cv_polygon is None
            and not cv_boundary_absence_only
        ):
            try:
                from sam_pipeline import sam_extract_boundary

                place_info = {
                    "name": parsed.display_name,
                    "query": parsed.google_query,
                    "center_lat": view["lat"],
                    "center_lng": view["lng"],
                    "zoom": view["zoom"],
                    "district_name": parsed.district,
                    "locality_name": parsed.locality,
                    **map_metrics,
                }

                sam_boundary = sam_extract_boundary(
                    driver=driver,
                    place_info=place_info,
                    output_dir=output_dir,
                    screenshot_override=ss_path,
                )

            except ImportError as exc:
                log(
                    f"   [SAM] Module unavailable: {exc}"
                )
            except Exception as exc:
                log(
                    f"   [SAM] Boundary extraction failed: {exc}"
                )

        if (
            cv_polygon is None
            and sam_boundary is None
        ):
            repaired_review_report = ""
            if repaired_review_candidates:
                repaired_review_report = export_repaired_review_candidates(
                    candidates=repaired_review_candidates,
                    parsed=parsed,
                    safe=safe,
                    output_dir=review_output_dir,
                    original_query=query,
                    log=log,
                )

            failure_debug_report = export_cv_failure_debug_report(
                attempts=cv_attempts,
                parsed=parsed,
                safe=safe,
                output_dir=report_output_dir,
                original_query=query,
                sam_boundary=sam_boundary,
                log=log,
            )

            controlled_terminal_reasons = {
                "no_boundary_like_red_evidence",
                "selected_place_not_locked",
                "zoom_integrity_failed",
            }

            absence_attempts = [
                attempt
                for attempt in cv_attempts
                if (
                    isinstance(
                        attempt.get(
                            "diagnostics"
                        ),
                        dict,
                    )
                    and attempt[
                        "diagnostics"
                    ].get(
                        "reason"
                    )
                    in controlled_terminal_reasons
                )
            ]

            has_explicit_boundary_absence = any(
                (
                    isinstance(
                        attempt.get(
                            "diagnostics"
                        ),
                        dict,
                    )
                    and attempt[
                        "diagnostics"
                    ].get(
                        "reason"
                    )
                    == "no_boundary_like_red_evidence"
                )
                for attempt
                in cv_attempts
            )

            if (
                has_explicit_boundary_absence
                and absence_attempts
                and len(
                    absence_attempts
                )
                == len(
                    cv_attempts
                )
            ):
                message = (
                    "Google Maps did not render a trustworthy red locality "
                    "boundary for the exact selected place at the safe "
                    "zoom levels that Google rendered. One or more comparison "
                    "zooms may also have been skipped because Google Maps "
                    "could not render the exact requested level. "
                    "False graph/polar reconstruction was intentionally "
                    "blocked. A CV failure debug report was saved here: "
                    f"{failure_debug_report}"
                )
            else:
                message = (
                    "No final boundary could be exported. "
                    "A CV failure debug report was saved here: "
                    f"{failure_debug_report}"
                )

            if repaired_review_report:
                message += (
                    " Repaired REVIEW candidates were saved here: "
                    f"{repaired_review_report}"
                )

            raise RuntimeError(
                message
            )

        ring, source, display = choose_polygon(
            cv_polygon=cv_polygon,
            sam_boundary=sam_boundary,
            parsed=parsed,
            georef=georef,
            log=log,
        )

        ring = _sanitize_ring_geometry(
            ring,
            log=log,
        )


        searched_lat = float(
            view.get(
                "place_lat",
                view["lat"],
            )
        )
        searched_lng = float(
            view.get(
                "place_lng",
                view["lng"],
            )
        )

        if not validate_boundary_ring(
            ring=ring,
            center_lat=searched_lat,
            center_lng=searched_lng,
            log=log,
        ):
            raise RuntimeError(
                "Detected polygon is geographically shifted "
                "or does not contain the searched place."
            )



        
        display = display or parsed.display_name
        result.polygon_source = source
        result.polygon_points = len(ring) if ring else 0
        result.display_name   = display

        gj_path = os.path.join(
            original_output_dir,
            f"{safe}.geojson",
        )

        overlap_report_path = os.path.join(
            report_output_dir,
            f"{safe}_overlap_report.json",
        )

        overlap_report = validate_existing_boundary_overlap(
            ring=ring,
            parsed=parsed,
            output_dir=original_output_dir,
            current_geojson_path=gj_path,
            report_path=overlap_report_path,
            log=log,
        )

        result.overlap_report_path = (
            overlap_report_path
            if os.path.exists(
                overlap_report_path
            )
            else ""
        )

        if (
            overlap_report.get(
                "blocked",
                False,
            )
            and not EXACT_GOOGLE_MODE
        ):
            blocked_comparisons = [
                comparison
                for comparison
                in overlap_report.get(
                    "comparisons",
                    [],
                )
                if comparison.get(
                    "blocked",
                    False,
                )
            ]

            blocked_summary = "; ".join(
                (
                    f"{comparison.get('existing_name')}: "
                    f"{comparison.get('smaller_polygon_overlap_percent')}% "
                    f"of the smaller polygon"
                )
                for comparison
                in blocked_comparisons
            )

            raise RuntimeError(
                "Priority 4 overlap validation blocked "
                "the KML export. Review or remove the "
                "older/ambiguous boundary first. "
                + blocked_summary
            )

        boundary_level = (
            "locality"
            if parsed.locality
            else (
                "district"
                if parsed.district
                else (
                    "state"
                    if parsed.state_code
                    and not parsed.district
                    else "unknown"
                )
            )
        )

        save_geojson(
            ring,
            parsed.display_name,
            display,
            source,
            gj_path,
            metadata={
                "query": query,
                "boundary_level": boundary_level,
                "locality": parsed.locality,
                "district": parsed.district,
                "state_code": parsed.state_code,
                "state": parsed.state_full,
                "boundary_mode": (
                    "exact_google_original"
                    if EXACT_GOOGLE_MODE
                    else "clean_coverage"
                ),
                "google_original": bool(
                    EXACT_GOOGLE_MODE
                ),
                "original_geometry_preserved": True,
                "shape_modified_by_sync": False,
                "overlap_policy": (
                    EXACT_GOOGLE_OVERLAP_POLICY
                    if EXACT_GOOGLE_MODE
                    else "block_unsafe_overlap"
                ),
                "overlap_validation": (
                    overlap_report.get(
                        "decision",
                        "allow",
                    )
                ),
                "georeference": georef.to_debug_dict(),
                "adaptive_locality": {
                    "priority": 15.548,
                    "profile": (
                        ACTIVE_LOCALITY_PROFILE
                    ),
                    "profile_reason": (
                        ACTIVE_LOCALITY_PROFILE_REASON
                    ),
                    "attempted_zooms": sorted(
                        list(
                            attempted_zooms
                        ),
                        reverse=True,
                    ),
                    "max_capture_count": (
                        ADAPTIVE_MAX_CAPTURE_COUNT
                    ),
                    "initial_google_zoom": round(
                        float(
                            INITIAL_GOOGLE_LOCALITY_ZOOM
                        ),
                        3,
                    ),
                    "minimum_final_zoom": round(
                        float(
                            _minimum_final_zoom()
                        ),
                        3,
                    ),
                    "multi_zoom_selection": dict(
                        LAST_MULTI_ZOOM_SELECTION
                    ),
                    "target_centering": dict(
                        LAST_RECENTER_DIAGNOSTICS
                    ),
                },
            },
            log=log,
        )
        result.geojson_path = gj_path

        georef_report_path = os.path.join(
            report_output_dir,
            f"{safe}_georef_report.json",
        )

        save_georef_report(
            georef=georef,
            ring=ring,
            output_path=georef_report_path,
            log=log,
        )

        kml_path = os.path.join(
            original_output_dir,
            f"{safe}.kml",
        )
        save_kml(ring, parsed.display_name, kml_path, log=log)
        result.kml_path = kml_path
        kmz_path = save_kmz(
            kml_path,
            image_path=ss_path,
            bbox=georef.to_debug_dict().get("bbox"),
            log=log,
        )
        result.kmz_path = kmz_path

        adaptive_report_path = os.path.join(
            report_output_dir,
            f"{safe}_adaptive_locality_report.json",
        )

        save_adaptive_locality_report(
            output_path=adaptive_report_path,
            query=query,
            cv_attempts=cv_attempts,
            candidate_count=len(
                boundary_candidates
            ),
            final_exported=True,
            log=log,
        )

        result.status = "Success"

        if EXACT_GOOGLE_MODE:
            log(
                "   [Priority 15.5.4.8] Accepted Google polygon was "
                "exported using adaptive locality sizing and "
                "DPR-aware Web Mercator georeferencing"
            )

        log(f"\n  ✅ Done — {result.polygon_points} points from {source}")
        log(f"     Screenshot : {os.path.basename(ss_path)}")
        log(f"     GeoTIFF    : {os.path.basename(tif_path)}")
        log(f"     GeoJSON    : {os.path.basename(gj_path)}")
        log(f"     KML        : {os.path.basename(kml_path)}")
        log(f"     KMZ        : {os.path.basename(kmz_path)}")

    except Exception as exc:
        import traceback
        result.status = "Failed"
        result.error  = str(exc)
        log(f"\n  ❌ Pipeline failed: {exc}")
        log(traceback.format_exc())
    finally:
        if driver:
            try:
                driver.quit()
                log("   [Driver] Closed")
            except Exception:
                pass

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  OSM HELPERS
# ─────────────────────────────────────────────────────────────────────────────



def _esc(t: str) -> str:
    return re.escape(t)


def _safe_name(name: str) -> str:
    safe = "".join(
        c if c.isalnum() or c in " _-" else "_" for c in name
    )
    return safe.strip().replace(" ", "_")[:60] or "boundary"


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    query  = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Agra, UP"
    result = run_pipeline(query, output_dir="boundaries")
    print(f"\nStatus : {result.status}")
    if result.error:
        print(f"Error  : {result.error}")
    print(f"Source : {result.polygon_source}")
    print(f"Points : {result.polygon_points}")
    print(f"Files  : {result.geotiff_path}")
    print(f"         {result.geojson_path}")
    print(f"         {result.kml_path}")
    print(f"         {result.kmz_path}")

    if result.overlap_report_path:
        print(
            "Overlap: "
            f"{result.overlap_report_path}"
        )