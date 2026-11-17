"""
sam_pipeline.py
---------------

SAM boundary extraction pipeline for Google Maps screenshots.

Designed for:
- Segment Anything Model (SAM ViT-B)
- CPU execution by default
- Optional DirectML execution on supported AMD GPUs
- Existing screenshots supplied by screenshot_pipeline.py

Public API:
    sam_extract_boundary(
        driver,
        place_info,
        output_dir="boundaries",
        checkpoint=None,
        screenshot_override=None,
    )

Expected optional map metrics in place_info:
    map_left
    map_top
    map_width
    map_height
    device_pixel_ratio
    viewport_width
    viewport_height

The map metrics should come from getBoundingClientRect() in Selenium.
They let this module distinguish the actual map canvas from browser UI and
convert screenshot pixels to geographic coordinates more accurately.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from pathlib import Path

# RX 6500M has limited VRAM. CPU is more reliable.
USE_DIRECTML = False


# ─────────────────────────────────────────────────────────────────────────────
#  OPTIONAL IMPORTS
# ─────────────────────────────────────────────────────────────────────────────

try:
    import torch
    from segment_anything import (
        SamAutomaticMaskGenerator,
        SamPredictor,
        sam_model_registry,
    )

    _SAM_AVAILABLE = True
    _IMPORT_ERROR = ""

except ImportError as exc:
    _SAM_AVAILABLE = False
    _IMPORT_ERROR = str(exc)


torch_directml = None
_DIRECTML_AVAILABLE = False

if USE_DIRECTML:
    try:
        import importlib

        torch_directml = importlib.import_module(
            "torch_directml"
        )
        _DIRECTML_AVAILABLE = True
    except Exception as exc:
        print(f"   DirectML unavailable: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

_WORKSPACE_ROOT = Path(__file__).resolve().parent
_LOCAL_CHECKPOINT = _WORKSPACE_ROOT / "models" / "sam_vit_b_01ec64.pth"
_DEFAULT_CHECKPOINT = (
    str(_LOCAL_CHECKPOINT)
    if _LOCAL_CHECKPOINT.exists()
    else r"C:\AIModels\sam_vit_b_01ec64.pth"
)

# Resize the longest image side to this value before SAM inference.
# 768 gives noticeably better masks than 640 while remaining practical on CPU.
_SAM_INPUT_SIZE = 768

# Area thresholds are relative to the visible map rectangle, not the full image.
_MIN_MASK_AREA_RATIO = 0.015
_MAX_MASK_AREA_RATIO = 0.70

_MIN_SCREENSHOT_VARIATION = 8.0
_MIN_CONFIDENCE = 0.60
_MAX_POLYGON_POINTS = 500

# Cached model objects.
_sam_model = None
_sam_device = None
_mask_gen = None
_predictor = None
_loaded_checkpoint = None


# ─────────────────────────────────────────────────────────────────────────────
#  INTERNAL DATA
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MapGeometry:
    """
    Map position in both original screenshot pixels and resized SAM pixels.
    """

    original_left: float
    original_top: float
    original_width: float
    original_height: float

    resized_left: int
    resized_top: int
    resized_right: int
    resized_bottom: int

    resized_center_x: int
    resized_center_y: int

    screenshot_to_css_x: float
    screenshot_to_css_y: float

    @property
    def resized_width(self) -> int:
        return max(
            1,
            self.resized_right - self.resized_left,
        )

    @property
    def resized_height(self) -> int:
        return max(
            1,
            self.resized_bottom - self.resized_top,
        )

    @property
    def resized_area(self) -> int:
        return self.resized_width * self.resized_height


# ─────────────────────────────────────────────────────────────────────────────
#  MASK GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def _create_mask_generator(
    model,
    points_per_side: int = 8,
):
    """
    Create the generator in one place so CPU, DirectML and fallback paths use
    exactly the same thresholds.
    """
    return SamAutomaticMaskGenerator(
        model=model,
        points_per_side=points_per_side,
        pred_iou_thresh=0.80,
        stability_score_thresh=0.88,
        box_nms_thresh=0.70,
        crop_n_layers=0,
        min_mask_region_area=300,
    )


def _switch_sam_to_cpu():
    global _sam_model
    global _sam_device
    global _mask_gen
    global _predictor

    if _sam_model is None:
        return None

    try:
        _sam_device = torch.device("cpu")
        _sam_model.to(_sam_device)
        _sam_model.eval()

        _mask_gen = _create_mask_generator(
            model=_sam_model,
            points_per_side=8,
        )
        _predictor = SamPredictor(_sam_model)

        return _mask_gen

    except Exception as exc:
        print(f"   Could not switch SAM to CPU: {exc}")
        return None


def _get_mask_generator(
    checkpoint: str,
):
    """
    Load SAM once and reuse it. Reload only when a different checkpoint is
    supplied.
    """
    global _sam_model
    global _sam_device
    global _mask_gen
    global _loaded_checkpoint
    global _predictor

    checkpoint = os.path.abspath(
        checkpoint
    )

    if (
        _mask_gen is not None
        and _loaded_checkpoint == checkpoint
    ):
        return _mask_gen

    try:
        if (
            USE_DIRECTML
            and _DIRECTML_AVAILABLE
        ):
            _sam_device = torch_directml.device()
        else:
            _sam_device = torch.device("cpu")

        print(f"   Using device: {_sam_device}")

        _sam_model = sam_model_registry[
            "vit_b"
        ](
            checkpoint=checkpoint
        )

        _sam_model.to(
            device=_sam_device
        )
        _sam_model.eval()

        _mask_gen = _create_mask_generator(
            model=_sam_model,
            points_per_side=8,
        )
        _predictor = SamPredictor(_sam_model)

        _loaded_checkpoint = checkpoint

        print(
            f"   SAM model loaded on "
            f"{_sam_device}"
        )

        return _mask_gen

    except Exception as exc:
        print(f"   Failed to load SAM: {exc}")

        _sam_model = None
        _sam_device = None
        _mask_gen = None
        _predictor = None
        _loaded_checkpoint = None

        return None


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def sam_extract_boundary(
    driver,
    place_info: dict,
    output_dir: str = "boundaries",
    checkpoint: Optional[str] = None,
    screenshot_override: Optional[str] = None,
) -> Optional[dict]:
    """
    Extract one screenshot-derived polygon using SAM.

    Returns:
        {
            "geojson": dict,
            "screenshot_path": str,
            "mask_path": str,
            "confidence": float,
            "source": "sam_cpu" | "sam_directml",
        }

    Returns None when no reliable centre-containing map segment is found.
    """
    if not _SAM_AVAILABLE:
        print(
            f"   SAM not available: "
            f"{_IMPORT_ERROR}"
        )
        return None

    if not checkpoint or not os.path.exists(checkpoint):
        if _LOCAL_CHECKPOINT.exists():
            checkpoint = str(_LOCAL_CHECKPOINT)
        elif os.path.exists(r"C:\AIModels\sam_vit_b_01ec64.pth"):
            checkpoint = r"C:\AIModels\sam_vit_b_01ec64.pth"
        else:
            checkpoint = _DEFAULT_CHECKPOINT

    if not os.path.exists(
        checkpoint
    ):
        print(
            f"   SAM checkpoint not found: "
            f"{checkpoint}"
        )
        return None

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    safe_name = _safe_filename(
        place_info.get(
            "name",
            "boundary",
        )
    )

    screenshot_path = os.path.join(
        output_dir,
        f"{safe_name}_screenshot.png",
    )

    # ── Step 1: obtain screenshot ─────────────────────────────────────────
    if (
        screenshot_override
        and os.path.exists(
            screenshot_override
        )
    ):
        screenshot_path = os.path.abspath(
            screenshot_override
        )

        print(
            "   Using existing screenshot: "
            f"{os.path.basename(screenshot_path)}"
        )

    else:
        print("   Capturing screenshot...")

        try:
            # Do not resize or hide the map immediately before capture.
            # screenshot_pipeline.py should handle map preparation.
            time.sleep(2)
            if hasattr(driver, "screenshot"):
                driver.screenshot(path=screenshot_path)
            elif hasattr(driver, "save_screenshot"):
                driver.save_screenshot(screenshot_path)
            else:
                raise ValueError(f"Unsupported driver/page object: {type(driver)}")

            print(
                "   Screenshot saved: "
                f"{os.path.basename(screenshot_path)}"
            )

        except Exception as exc:
            print(
                f"   Screenshot failed: {exc}"
            )
            return None

    # ── Step 2: validate image ────────────────────────────────────────────
    image_bgr = cv2.imread(
        screenshot_path
    )

    if image_bgr is None:
        print(
            "   Could not read screenshot"
        )
        return None

    img_h, img_w = image_bgr.shape[
        :2
    ]

    gray = cv2.cvtColor(
        image_bgr,
        cv2.COLOR_BGR2GRAY,
    )
    variation = float(
        gray.std()
    )

    print(
        f"   Image size: {img_w}x{img_h}"
    )
    print(
        "   Screenshot pixel variation: "
        f"{variation:.2f}"
    )

    if variation < _MIN_SCREENSHOT_VARIATION:
        print(
            "   Screenshot is blank or almost solid. "
            "SAM was not executed."
        )
        return None

    image_rgb = cv2.cvtColor(
        image_bgr,
        cv2.COLOR_BGR2RGB,
    )

    # ── Step 3: resize ────────────────────────────────────────────────────
    scale = min(
        1.0,
        _SAM_INPUT_SIZE
        / max(img_w, img_h),
    )

    new_w = max(
        1,
        int(round(img_w * scale)),
    )
    new_h = max(
        1,
        int(round(img_h * scale)),
    )

    image_resized = cv2.resize(
        image_rgb,
        (new_w, new_h),
        interpolation=cv2.INTER_AREA,
    )

    map_geometry = _resolve_map_geometry(
        place_info=place_info,
        img_w=img_w,
        img_h=img_h,
        resized_w=new_w,
        resized_h=new_h,
    )

    print(
        "   Map rectangle in screenshot: "
        f"left={map_geometry.original_left:.1f}, "
        f"top={map_geometry.original_top:.1f}, "
        f"width={map_geometry.original_width:.1f}, "
        f"height={map_geometry.original_height:.1f}"
    )

    # ── Step 4: load SAM ──────────────────────────────────────────────────
    print("   Loading SAM model...")

    mask_generator = _get_mask_generator(
        checkpoint
    )

    if mask_generator is None:
        return None

    # ── Step 5: segmentation ──────────────────────────────────────────────
    print(
        "   Running SAM segmentation..."
    )

    started = time.time()

    try:
        masks = mask_generator.generate(
            image_resized
        )

    except RuntimeError as exc:
        error_message = str(
            exc
        ).lower()

        memory_error = any(
            keyword in error_message
            for keyword in (
                "video memory",
                "out of memory",
                "allocation",
                "not enough memory",
            )
        )

        if not memory_error:
            print(
                f"   SAM runtime error: {exc}"
            )
            return None

        print(
            "   GPU memory error. "
            "Retrying on CPU..."
        )

        cpu_generator = (
            _switch_sam_to_cpu()
        )

        if cpu_generator is None:
            return None

        try:
            masks = cpu_generator.generate(
                image_resized
            )
        except Exception as cpu_exc:
            print(
                "   SAM CPU retry failed: "
                f"{cpu_exc}"
            )
            return None

    except Exception as exc:
        print(
            f"   SAM segmentation failed: "
            f"{exc}"
        )
        return None

    elapsed = time.time() - started

    print(
        f"   SAM found {len(masks)} "
        f"segments in {elapsed:.1f}s"
    )

    if not masks:
        print("   No SAM segments found")
        return None

    # ── Step 6: select reliable map mask ─────────────────────────────────
    best_mask, confidence, diagnostics = (
        _find_boundary_segment(
            masks=masks,
            img_w=new_w,
            img_h=new_h,
            map_geometry=map_geometry,
            place_info=place_info
        )
    )

    selection_mode = "automatic"

    # Automatic generation may return only a large rectangular land block.
    # Try centre-point prompting when strict filtering rejects it.
    if best_mask is None or confidence < _MIN_CONFIDENCE:
        print(
            "No sufficiently large and reliable "
            "locality boundary was detected."
        )

        print(
            "Point-prompt fallback is disabled because "
            "it may select a building or small land plot."
        )
        return None

    diagnostics = {
        **diagnostics,
        "selection_mode": selection_mode,
    }

    print(
        "   Selected mask confidence: "
        f"{confidence:.2f}"
    )
    print(
        "   Selected mask diagnostics: "
        f"{diagnostics}"
    )

    if confidence < _MIN_CONFIDENCE:
        print(
            "   SAM confidence is too low: "
            f"{confidence:.2f}"
        )
        return None

    # ── Step 7: save debug overlay ────────────────────────────────────────
    mask_path = os.path.join(
        output_dir,
        f"{safe_name}_mask.png",
    )

    _save_mask_image(
        best_mask,
        image_resized,
        mask_path,
        center=(
            map_geometry.resized_center_x,
            map_geometry.resized_center_y,
        ),
        map_geometry=map_geometry,
    )

    print(
        f"   Mask saved: "
        f"{os.path.basename(mask_path)}"
    )

    # ── Step 8: mask to polygon ───────────────────────────────────────────
    pixel_polygon_resized = (
        _mask_to_pixel_polygon(
            mask=best_mask,
            target_center=(
                map_geometry.resized_center_x,
                map_geometry.resized_center_y,
            ),
        )
    )

    if (
        pixel_polygon_resized is None
        or len(pixel_polygon_resized) < 5
    ):
        print(
            "   Could not extract a valid "
            "centre contour from mask"
        )
        return None

    # Scale back to original screenshot pixels.
    pixel_polygon = [
        (
            int(round(x / scale)),
            int(round(y / scale)),
        )
        for x, y in pixel_polygon_resized
    ]

    if not _validate_pixel_polygon(
        pixel_polygon,
        img_w,
        img_h,
        map_geometry,
    ):
        print(
            "   Pixel polygon validation failed"
        )
        return None

    # ── Step 9: pixels to lat/lng ─────────────────────────────────────────
    geo_polygon = _pixels_to_latlng(
        pixel_polygon=pixel_polygon,
        img_w=img_w,
        img_h=img_h,
        place_info=place_info,
    )

    if (
        geo_polygon is None
        or len(geo_polygon) < 5
    ):
        print(
            "   Georeferencing failed"
        )
        return None

    print(
        f"   Polygon has "
        f"{len(geo_polygon)} points"
    )

    # ── Step 10: GeoJSON ──────────────────────────────────────────────────
    coordinates = [
        [lng, lat]
        for lat, lng in geo_polygon
    ]

    if (
        coordinates
        and coordinates[0]
        != coordinates[-1]
    ):
        coordinates.append(
            coordinates[0]
        )

    geojson = {
        "type": "Polygon",
        "coordinates": [
            coordinates
        ],
    }

    source = (
        "sam_cpu"
        if str(_sam_device) == "cpu"
        else "sam_directml"
    )

    return {
        "geojson": geojson,
        "screenshot_path": screenshot_path,
        "mask_path": mask_path,
        "confidence": confidence,
        "source": source,
        "diagnostics": diagnostics,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  MAP GEOMETRY
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_map_geometry(
    place_info: dict,
    img_w: int,
    img_h: int,
    resized_w: int,
    resized_h: int,
) -> MapGeometry:
    """
    Convert Selenium CSS map coordinates to screenshot pixels.

    Best case:
        viewport_width / viewport_height are supplied, allowing exact scaling.

    Fallback:
        device_pixel_ratio is used.

    Final fallback:
        the whole screenshot is treated as map content.
    """
    device_pixel_ratio = _positive_float(
        place_info.get(
            "device_pixel_ratio"
        ),
        1.0,
    )

    viewport_width = _positive_float(
        place_info.get(
            "viewport_width"
        ),
        0.0,
    )
    viewport_height = _positive_float(
        place_info.get(
            "viewport_height"
        ),
        0.0,
    )

    if viewport_width > 0:
        css_to_screenshot_x = (
            img_w / viewport_width
        )
        screenshot_to_css_x = (
            viewport_width / img_w
        )
    else:
        css_to_screenshot_x = (
            device_pixel_ratio
        )
        screenshot_to_css_x = (
            1.0 / device_pixel_ratio
        )

    if viewport_height > 0:
        css_to_screenshot_y = (
            img_h / viewport_height
        )
        screenshot_to_css_y = (
            viewport_height / img_h
        )
    else:
        css_to_screenshot_y = (
            device_pixel_ratio
        )
        screenshot_to_css_y = (
            1.0 / device_pixel_ratio
        )

    map_left_css = _float_or_default(
        place_info.get("map_left"),
        0.0,
    )
    map_top_css = _float_or_default(
        place_info.get("map_top"),
        0.0,
    )

    default_map_width_css = (
        viewport_width
        if viewport_width > 0
        else img_w / css_to_screenshot_x
    )
    default_map_height_css = (
        viewport_height
        if viewport_height > 0
        else img_h / css_to_screenshot_y
    )

    map_width_css = _positive_float(
        place_info.get("map_width"),
        default_map_width_css,
    )
    map_height_css = _positive_float(
        place_info.get("map_height"),
        default_map_height_css,
    )

    original_left = (
        map_left_css
        * css_to_screenshot_x
    )
    original_top = (
        map_top_css
        * css_to_screenshot_y
    )
    original_width = (
        map_width_css
        * css_to_screenshot_x
    )
    original_height = (
        map_height_css
        * css_to_screenshot_y
    )

    original_left = max(
        0.0,
        min(
            original_left,
            float(img_w - 1),
        ),
    )
    original_top = max(
        0.0,
        min(
            original_top,
            float(img_h - 1),
        ),
    )

    original_width = max(
        1.0,
        min(
            original_width,
            img_w - original_left,
        ),
    )
    original_height = max(
        1.0,
        min(
            original_height,
            img_h - original_top,
        ),
    )

    scale_x = resized_w / img_w
    scale_y = resized_h / img_h

    resized_left = int(
        round(
            original_left * scale_x
        )
    )
    resized_top = int(
        round(
            original_top * scale_y
        )
    )
    resized_right = int(
        round(
            (
                original_left
                + original_width
            )
            * scale_x
        )
    )
    resized_bottom = int(
        round(
            (
                original_top
                + original_height
            )
            * scale_y
        )
    )

    resized_left = max(
        0,
        min(
            resized_left,
            resized_w - 1,
        ),
    )
    resized_top = max(
        0,
        min(
            resized_top,
            resized_h - 1,
        ),
    )
    resized_right = max(
        resized_left + 1,
        min(
            resized_right,
            resized_w,
        ),
    )
    resized_bottom = max(
        resized_top + 1,
        min(
            resized_bottom,
            resized_h,
        ),
    )

    center_x = int(
        round(
            (
                resized_left
                + resized_right
            )
            / 2.0
        )
    )
    center_y = int(
        round(
            (
                resized_top
                + resized_bottom
            )
            / 2.0
        )
    )

    center_x = max(
        0,
        min(
            center_x,
            resized_w - 1,
        ),
    )
    center_y = max(
        0,
        min(
            center_y,
            resized_h - 1,
        ),
    )

    return MapGeometry(
        original_left=original_left,
        original_top=original_top,
        original_width=original_width,
        original_height=original_height,
        resized_left=resized_left,
        resized_top=resized_top,
        resized_right=resized_right,
        resized_bottom=resized_bottom,
        resized_center_x=center_x,
        resized_center_y=center_y,
        screenshot_to_css_x=screenshot_to_css_x,
        screenshot_to_css_y=screenshot_to_css_y,
    )



def _generate_point_prompt_masks(
    image_rgb: np.ndarray,
    map_geometry: MapGeometry,
) -> list[dict]:
    """
    Generate three centre-prompted masks.

    The map centre is a positive point. Points near map corners and side
    midpoints are negative prompts so SAM is discouraged from selecting the
    complete map/background rectangle.
    """
    global _predictor

    if _predictor is None:
        return []

    center_x = float(map_geometry.resized_center_x)
    center_y = float(map_geometry.resized_center_y)

    width = float(map_geometry.resized_width)
    height = float(map_geometry.resized_height)
    left = float(map_geometry.resized_left)
    top = float(map_geometry.resized_top)
    right = float(map_geometry.resized_right - 1)
    bottom = float(map_geometry.resized_bottom - 1)

    margin_x = max(8.0, width * 0.08)
    margin_y = max(8.0, height * 0.08)

    point_coords = np.array(
        [
            [center_x, center_y],  # positive
            [left + margin_x, top + margin_y],
            [right - margin_x, top + margin_y],
            [left + margin_x, bottom - margin_y],
            [right - margin_x, bottom - margin_y],
            [center_x, top + margin_y],
            [center_x, bottom - margin_y],
            [left + margin_x, center_y],
            [right - margin_x, center_y],
        ],
        dtype=np.float32,
    )

    point_labels = np.array(
        [1, 0, 0, 0, 0, 0, 0, 0, 0],
        dtype=np.int32,
    )

    try:
        _predictor.set_image(image_rgb)

        masks, scores, _ = _predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            multimask_output=True,
        )

        results = []

        for index, mask in enumerate(masks):
            results.append({
                "segmentation": mask.astype(bool),
                "area": int(mask.sum()),
                "predicted_iou": float(scores[index]),
                "stability_score": 1.0,
                "prompt_source": "center_point",
            })

        return results

    except Exception as exc:
        print(f"   Point-prompt SAM fallback failed: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  MASK SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def _find_boundary_segment(
    masks: list,
    img_w: int,
    img_h: int,
    map_geometry: MapGeometry,
    place_info: dict,
) -> tuple[
    Optional[np.ndarray],
    float,
    dict,
]:
    """
    Select a mask that:
    - lies mainly inside the map viewport;
    - contains the geographic map centre;
    - does not cover nearly the entire map;
    - does not touch three or four map edges;
    - has a valid centre-containing contour;
    - has strong SAM IoU/stability values.

    The returned confidence is a custom ranking confidence, not a probability.
    """
    center_x = (
        map_geometry.resized_center_x
    )
    center_y = (
        map_geometry.resized_center_y
    )

    left = map_geometry.resized_left
    top = map_geometry.resized_top
    right = map_geometry.resized_right
    bottom = map_geometry.resized_bottom

    map_area = max(
        1,
        map_geometry.resized_area,
    )

    scored = []

    for index, mask_data in enumerate(
        masks
    ):
        segmentation = mask_data.get(
            "segmentation"
        )

        if (
            not isinstance(
                segmentation,
                np.ndarray,
            )
            or segmentation.shape[:2]
            != (img_h, img_w)
        ):
            continue

        segmentation = segmentation.astype(
            bool
        )

        total_area = int(
            segmentation.sum()
        )

        if total_area <= 0:
            continue

        inside_map = segmentation[
            top:bottom,
            left:right,
        ]

        inside_area = int(
            inside_map.sum()
        )

        if inside_area <= 0:
            continue

        # Reject browser panels or controls that are mostly outside the map.
        map_overlap_ratio = (
            inside_area / total_area
        )

        if map_overlap_ratio < 0.85:
            continue

        area_ratio = (
            inside_area / map_area
        )

        query_text = str(
            place_info.get("query", "")
        ).lower()

        locality_name = str(
            place_info.get("locality_name", "")
        ).lower()

        is_locality_search = bool(
            locality_name
            or "sector" in query_text
            or "locality" in query_text
        )

        minimum_area_ratio = (
            0.03
            if is_locality_search
            else _MIN_MASK_AREA_RATIO
        )


        if not (
            minimum_area_ratio
            <= area_ratio
            <= _MAX_MASK_AREA_RATIO
        ):
            continue

        # A valid selected locality/sector region must contain the map centre.
        if not segmentation[
            center_y,
            center_x,
        ]:
            continue

        contour = _center_contour(
            segmentation,
            (
                center_x,
                center_y,
            ),
        )

        if contour is None:
            continue

        contour_area = float(
            cv2.contourArea(
                contour
            )
        )
        perimeter = float(
            cv2.arcLength(
                contour,
                True,
            )
        )

        if (
            contour_area <= 0
            or perimeter <= 0
        ):
            continue

        # Check map-edge contact relative to the map rectangle.
        edge_margin = max(
            1,
            int(
                min(
                    map_geometry.resized_width,
                    map_geometry.resized_height,
                )
                * 0.005
            ),
        )

        top_touch = bool(
            segmentation[
                top:min(
                    bottom,
                    top + edge_margin,
                ),
                left:right,
            ].any()
        )
        bottom_touch = bool(
            segmentation[
                max(
                    top,
                    bottom - edge_margin,
                ):bottom,
                left:right,
            ].any()
        )
        left_touch = bool(
            segmentation[
                top:bottom,
                left:min(
                    right,
                    left + edge_margin,
                ),
            ].any()
        )
        right_touch = bool(
            segmentation[
                top:bottom,
                max(
                    left,
                    right - edge_margin,
                ):right,
            ].any()
        )

        border_count = sum(
            (
                top_touch,
                bottom_touch,
                left_touch,
                right_touch,
            )
        )

        # Locality masks touching two or more map edges are normally
        # clipped background/road regions rather than a complete boundary.
        if border_count >= 2:
            continue

        x, y, width, height = (
            cv2.boundingRect(
                contour
            )
        )

        width_ratio = (
            width
            / map_geometry.resized_width
        )

        height_ratio = (
            height
            / map_geometry.resized_height
        )

        if is_locality_search:
            if width_ratio < 0.12:
                continue

            if height_ratio < 0.12:
                continue





        bounding_area = max(
            1,
            width * height,
        )
        fill_ratio = (
            contour_area
            / bounding_area
        )

        # A mask filling almost its entire bounding rectangle was the source
        # of the previous false Sector 62 polygon.
        if fill_ratio > 0.92:
            continue

        circularity = (
            4.0
            * math.pi
            * contour_area
            / (perimeter ** 2)
        )

        predicted_iou = float(
            mask_data.get(
                "predicted_iou",
                0.0,
            )
        )
        stability = float(
            mask_data.get(
                "stability_score",
                0.0,
            )
        )

        score = 0.0

        # Strong centre requirement.
        score += 35.0

        # Prefer useful locality-sized regions.
        if 0.05 <= area_ratio <= 0.45:
            score += 25.0
        elif 0.025 <= area_ratio <= 0.60:
            score += 15.0
        else:
            score += 5.0

        # Penalize masks touching map boundaries.
        score += max(
            0.0,
            12.0
            - border_count * 4.0,
        )

        # Reject extremely line-like masks while allowing irregular polygons.
        if 0.12 <= fill_ratio <= 0.90:
            score += 8.0

        if 0.05 <= circularity <= 0.75:
            score += 5.0

        score += predicted_iou * 12.0
        score += stability * 8.0
        score += map_overlap_ratio * 5.0

        diagnostics = {
            "mask_index": index,
            "score": round(score, 2),
            "area_ratio": round(
                area_ratio,
                4,
            ),
            "map_overlap_ratio": round(
                map_overlap_ratio,
                4,
            ),
            "predicted_iou": round(
                predicted_iou,
                4,
            ),
            "stability_score": round(
                stability,
                4,
            ),
            "border_count": border_count,
            "fill_ratio": round(
                fill_ratio,
                4,
            ),
            "circularity": round(
                circularity,
                4,
            ),
        }

        scored.append((
            score,
            segmentation,
            diagnostics,
        ))

    print(
        "   Candidate masks after "
        f"strict filtering: {len(scored)}"
    )

    if not scored:
        return None, 0.0, {}

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    best_score, best_mask, diagnostics = (
        scored[0]
    )

    # Maximum practical score is about 105.
    confidence = min(
        best_score / 105.0,
        1.0,
    )

    return (
        best_mask,
        confidence,
        diagnostics,
    )


def _center_contour(
    mask: np.ndarray,
    target_center: tuple[int, int],
) -> Optional[np.ndarray]:
    """
    Return only a contour that contains the geographic map centre.
    """
    mask_uint8 = (
        mask.astype(np.uint8)
        * 255
    )

    contours, _ = cv2.findContours(
        mask_uint8,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    center_x, center_y = (
        target_center
    )

    containing = [
        contour
        for contour in contours
        if cv2.pointPolygonTest(
            contour,
            (
                float(center_x),
                float(center_y),
            ),
            False,
        ) >= 0
    ]

    if not containing:
        return None

    return max(
        containing,
        key=cv2.contourArea,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  MASK TO POLYGON
# ─────────────────────────────────────────────────────────────────────────────

def _mask_to_pixel_polygon(
    mask: np.ndarray,
    target_center: tuple[int, int],
) -> Optional[list[tuple[int, int]]]:
    """
    Convert the centre-containing SAM mask component to an ordered polygon.
    """
    mask_uint8 = (
        mask.astype(np.uint8)
        * 255
    )

    # Mild morphology removes small holes and jagged single-pixel noise.
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (3, 3),
    )

    mask_uint8 = cv2.morphologyEx(
        mask_uint8,
        cv2.MORPH_CLOSE,
        kernel,
        iterations=1,
    )

    contours, _ = cv2.findContours(
        mask_uint8,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_TC89_KCOS,
    )

    if not contours:
        return None

    center_x, center_y = (
        target_center
    )

    centre_contours = [
        contour
        for contour in contours
        if cv2.pointPolygonTest(
            contour,
            (
                float(center_x),
                float(center_y),
            ),
            False,
        ) >= 0
    ]

    if not centre_contours:
        return None

    contour = max(
        centre_contours,
        key=cv2.contourArea,
    )

    area = float(
        cv2.contourArea(
            contour
        )
    )
    perimeter = float(
        cv2.arcLength(
            contour,
            True,
        )
    )

    if (
        area <= 0
        or perimeter <= 0
    ):
        return None

    print(
        f"   Centre contour area: {area:.1f}"
    )
    print(
        "   Centre contour perimeter: "
        f"{perimeter:.1f}"
    )

    epsilon = max(
        1.0,
        0.0035 * perimeter,
    )

    approximation = cv2.approxPolyDP(
        contour,
        epsilon,
        True,
    )

    points = [
        (
            int(point[0][0]),
            int(point[0][1]),
        )
        for point in approximation
    ]

    if len(points) > _MAX_POLYGON_POINTS:
        step = max(
            1,
            math.ceil(
                len(points)
                / _MAX_POLYGON_POINTS
            ),
        )
        points = points[
            ::step
        ]

    if (
        points
        and points[0] != points[-1]
    ):
        points.append(
            points[0]
        )

    print(
        "   Polygon points after "
        f"simplification: {len(points)}"
    )

    return (
        points
        if len(points) >= 5
        else None
    )


def _validate_pixel_polygon(
    polygon: list[tuple[int, int]],
    img_w: int,
    img_h: int,
    map_geometry: MapGeometry,
) -> bool:
    if len(polygon) < 5:
        return False

    array = np.array(
        polygon,
        dtype=np.int32,
    ).reshape(
        -1,
        1,
        2,
    )

    area = abs(
        cv2.contourArea(
            array
        )
    )

    map_area = (
        map_geometry.original_width
        * map_geometry.original_height
    )

    if map_area <= 0:
        return False

    area_ratio = (
        area / map_area
    )

    if not (
        _MIN_MASK_AREA_RATIO * 0.5
        <= area_ratio
        <= _MAX_MASK_AREA_RATIO * 1.05
    ):
        print(
            "   Rejected polygon area ratio: "
            f"{area_ratio:.4f}"
        )
        return False

    center_x = (
        map_geometry.original_left
        + map_geometry.original_width / 2.0
    )
    center_y = (
        map_geometry.original_top
        + map_geometry.original_height / 2.0
    )

    contains_center = cv2.pointPolygonTest(
        array,
        (
            float(center_x),
            float(center_y),
        ),
        False,
    ) >= 0

    if not contains_center:
        print(
            "   Rejected polygon: it does not "
            "contain map centre"
        )
        return False

    for x, y in polygon:
        if not (
            -1 <= x <= img_w
            and -1 <= y <= img_h
        ):
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
#  GEOREFERENCING
# ─────────────────────────────────────────────────────────────────────────────

def _latlng_to_world_pixels(
    latitude: float,
    longitude: float,
    zoom: float,
) -> tuple[float, float]:
    world_size = (
        256.0 * (2 ** zoom)
    )

    latitude = max(
        min(
            latitude,
            85.05112878,
        ),
        -85.05112878,
    )

    world_x = (
        (longitude + 180.0)
        / 360.0
        * world_size
    )

    sin_latitude = math.sin(
        math.radians(
            latitude
        )
    )

    world_y = (
        0.5
        - math.log(
            (
                1.0
                + sin_latitude
            )
            / (
                1.0
                - sin_latitude
            )
        )
        / (
            4.0
            * math.pi
        )
    ) * world_size

    return world_x, world_y


def _world_pixels_to_latlng(
    world_x: float,
    world_y: float,
    zoom: float,
) -> tuple[float, float]:
    world_size = (
        256.0 * (2 ** zoom)
    )

    longitude = (
        world_x / world_size
    ) * 360.0 - 180.0

    mercator_y = (
        math.pi
        - (
            2.0
            * math.pi
            * world_y
            / world_size
        )
    )

    latitude = math.degrees(
        math.atan(
            math.sinh(
                mercator_y
            )
        )
    )

    return latitude, longitude


def _pixels_to_latlng(
    pixel_polygon: list[tuple[int, int]],
    img_w: int,
    img_h: int,
    place_info: dict,
) -> Optional[list[tuple[float, float]]]:
    """
    Convert screenshot pixels to Web Mercator coordinates.

    Pixel offsets are converted back to CSS pixels before being applied to
    Google Maps world-pixel coordinates.
    """
    center_lat = place_info.get(
        "center_lat"
    )
    center_lng = place_info.get(
        "center_lng"
    )
    zoom = place_info.get(
        "zoom"
    )

    if (
        center_lat is None
        or center_lng is None
        or zoom is None
    ):
        return None

    center_lat = float(
        center_lat
    )
    center_lng = float(
        center_lng
    )
    zoom = float(
        zoom
    )

    center_world_x, center_world_y = (
        _latlng_to_world_pixels(
            latitude=center_lat,
            longitude=center_lng,
            zoom=zoom,
        )
    )

    geometry = _resolve_map_geometry(
        place_info=place_info,
        img_w=img_w,
        img_h=img_h,
        resized_w=img_w,
        resized_h=img_h,
    )

    map_center_x = (
        geometry.original_left
        + geometry.original_width / 2.0
    )
    map_center_y = (
        geometry.original_top
        + geometry.original_height / 2.0
    )

    geographic_points = []

    for pixel_x, pixel_y in pixel_polygon:
        offset_x_css = (
            pixel_x - map_center_x
        ) * geometry.screenshot_to_css_x

        offset_y_css = (
            pixel_y - map_center_y
        ) * geometry.screenshot_to_css_y

        world_x = (
            center_world_x
            + offset_x_css
        )
        world_y = (
            center_world_y
            + offset_y_css
        )

        latitude, longitude = (
            _world_pixels_to_latlng(
                world_x=world_x,
                world_y=world_y,
                zoom=zoom,
            )
        )

        geographic_points.append((
            round(latitude, 7),
            round(longitude, 7),
        ))

    if (
        geographic_points
        and geographic_points[0]
        != geographic_points[-1]
    ):
        geographic_points.append(
            geographic_points[0]
        )

    return (
        geographic_points
        if len(geographic_points) >= 5
        else None
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DEBUG IMAGE
# ─────────────────────────────────────────────────────────────────────────────

def _save_mask_image(
    mask: np.ndarray,
    image_rgb: np.ndarray,
    path: str,
    center: tuple[int, int],
    map_geometry: MapGeometry,
) -> None:
    """
    Save a diagnostic overlay showing:
    - selected mask;
    - contour;
    - map rectangle;
    - geographic map centre.
    """
    overlay = image_rgb.copy()

    overlay[mask] = (
        overlay[mask] * 0.45
        + np.array(
            [0, 110, 255],
            dtype=np.float32,
        )
        * 0.55
    ).astype(
        np.uint8
    )

    contours, _ = cv2.findContours(
        mask.astype(np.uint8),
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    cv2.drawContours(
        overlay,
        contours,
        -1,
        (0, 255, 80),
        2,
    )

    cv2.rectangle(
        overlay,
        (
            map_geometry.resized_left,
            map_geometry.resized_top,
        ),
        (
            map_geometry.resized_right - 1,
            map_geometry.resized_bottom - 1,
        ),
        (255, 255, 0),
        2,
    )

    cv2.drawMarker(
        overlay,
        center,
        (255, 0, 255),
        markerType=cv2.MARKER_CROSS,
        markerSize=18,
        thickness=2,
    )

    cv2.imwrite(
        path,
        cv2.cvtColor(
            overlay,
            cv2.COLOR_RGB2BGR,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  MISC HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _float_or_default(
    value,
    default: float,
) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (
        TypeError,
        ValueError,
    ):
        return float(default)


def _positive_float(
    value,
    default: float,
) -> float:
    result = _float_or_default(
        value,
        default,
    )

    if result <= 0:
        return float(default)

    return result


def _safe_filename(
    name: str,
) -> str:
    safe = "".join(
        character
        if (
            character.isalnum()
            or character in " _-,"
        )
        else "_"
        for character in str(name)
    )

    return (
        safe.strip()
        .replace(" ", "_")[:60]
        or "boundary"
    )


def _hide_maps_ui(driver) -> None:
    """
    Kept for compatibility, but intentionally does nothing.

    Changing display properties or dispatching resize immediately before a
    screenshot can blank or shift Google Maps WebGL rendering.
    """
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  STANDALONE TEST
# ─────────────────────────────────────────────────────────────────────────────

def test_sam_load(
    checkpoint: Optional[str] = None,
) -> bool:
    checkpoint = (
        checkpoint
        or _DEFAULT_CHECKPOINT
    )

    print("=== SAM Pipeline Test ===")
    print(f"Checkpoint: {checkpoint}")
    print(
        f"Exists: "
        f"{os.path.exists(checkpoint)}"
    )

    if not _SAM_AVAILABLE:
        print(
            f"ERROR: {_IMPORT_ERROR}"
        )
        return False

    generator = _get_mask_generator(
        checkpoint
    )

    if generator is None:
        print(
            "ERROR: Could not load model"
        )
        return False

    test_image = np.zeros(
        (256, 256, 3),
        dtype=np.uint8,
    )
    test_image[
        60:180,
        60:180,
    ] = [
        100,
        150,
        200,
    ]

    print(
        "Running SAM on test image..."
    )

    started = time.time()
    masks = generator.generate(
        test_image
    )

    print(
        f"Done in "
        f"{time.time() - started:.1f}s — "
        f"{len(masks)} segments found"
    )
    print(
        "=== SAM Test PASSED ==="
    )

    return True


if __name__ == "__main__":
    test_sam_load()