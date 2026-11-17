"""
GeoTIFF Merger & Mosaicing Utility
==================================
Merges two or more neighbouring or overlapping GeoTIFF images
(such as satellite or map captures of adjacent localities) into
a single seamless, georeferenced GeoTIFF at the same zoom level.

Features:
- Pure Python using PIL and OpenCV (no external GDAL/rasterio dependency needed).
- Exact 1:1 pixel alignment for GeoTIFFs captured at the same zoom level.
- Supports smooth feathering / alpha-blending across overlap seams.
- Embeds standard WGS84 (EPSG:4326) tags: ModelPixelScaleTag, ModelTiepointTag, GeoKeyDirectoryTag.
- CLI and Python API for single-line usage.
"""

import math
import gc
import os
import sys
import struct
import numpy as np
import cv2
from PIL import Image, TiffImagePlugin
from typing import List, Dict, Tuple, Optional, Union

class GeoTIFFMetadata:
    def __init__(self, path: str, width: int, height: int,
                 origin_x: float, origin_y: float,
                 scale_x: float, scale_y: float):
        self.path = path
        self.width = width
        self.height = height
        self.origin_x = origin_x  # Top-left longitude
        self.origin_y = origin_y  # Top-left latitude
        self.scale_x = scale_x    # Degrees longitude per pixel
        self.scale_y = abs(scale_y)  # Degrees latitude per pixel
        
        self.min_lng = origin_x
        self.max_lng = origin_x + width * self.scale_x
        self.max_lat = origin_y
        self.min_lat = origin_y - height * self.scale_y

    def __repr__(self):
        return (f"<GeoTIFFMetadata {os.path.basename(self.path)}: "
                f"{self.width}x{self.height}px, "
                f"Lng=[{self.min_lng:.5f}, {self.max_lng:.5f}], "
                f"Lat=[{self.min_lat:.5f}, {self.max_lat:.5f}], "
                f"res=({self.scale_x:.6e}, {self.scale_y:.6e})>")


def read_geotiff_metadata(path: str) -> GeoTIFFMetadata:
    """Fast header-only metadata extraction from GeoTIFF without loading raster pixels into RAM."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"GeoTIFF file not found: {path}")

    with Image.open(path) as pil_img:
        width, height = pil_img.size
        tag_scale = pil_img.tag_v2.get(33550)
        tag_tiepoint = pil_img.tag_v2.get(33922)

        if not tag_scale or not tag_tiepoint:
            raise ValueError(f"File {path} does not contain standard GeoTIFF ModelPixelScale/Tiepoint tags.")

        scale_x = float(tag_scale[0])
        scale_y = abs(float(tag_scale[1]))
        origin_x = float(tag_tiepoint[3])
        origin_y = float(tag_tiepoint[4])

        return GeoTIFFMetadata(
            path=path,
            width=width,
            height=height,
            origin_x=origin_x,
            origin_y=origin_y,
            scale_x=scale_x,
            scale_y=scale_y,
        )


def read_geotiff(path: str) -> Tuple[np.ndarray, GeoTIFFMetadata]:
    """Reads a GeoTIFF image and extracts its image data (RGB) and georeferencing metadata."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"GeoTIFF file not found: {path}")

    pil_img = Image.open(path)
    width, height = pil_img.size

    # Check for GeoTIFF tags
    # 33550: ModelPixelScaleTag
    # 33922: ModelTiepointTag
    tag_scale = pil_img.tag_v2.get(33550)
    tag_tiepoint = pil_img.tag_v2.get(33922)

    if not tag_scale or not tag_tiepoint:
        raise ValueError(f"File {path} does not contain standard GeoTIFF ModelPixelScale/Tiepoint tags.")

    scale_x = float(tag_scale[0])
    scale_y = abs(float(tag_scale[1]))

    origin_x = float(tag_tiepoint[3])
    origin_y = float(tag_tiepoint[4])

    meta = GeoTIFFMetadata(
        path=path,
        width=width,
        height=height,
        origin_x=origin_x,
        origin_y=origin_y,
        scale_x=scale_x,
        scale_y=scale_y,
    )

    rgb = np.array(pil_img.convert("RGB"))
    return rgb, meta


def compute_edge_weights(height: int, width: int, feather_pixels: int = 40) -> np.ndarray:
    """
    Computes a 2D feathering weight matrix that smoothly decays to 0 at image borders
    to avoid sharp seam lines when blending overlapping neighbouring tiles.
    """
    if feather_pixels <= 0:
        return np.ones((height, width), dtype=np.float32)

    # 1D ramp for y and x
    y_dist = np.minimum(np.arange(height), np.arange(height)[::-1])
    x_dist = np.minimum(np.arange(width), np.arange(width)[::-1])

    y_ramp = np.clip(y_dist / float(feather_pixels), 0.0, 1.0).astype(np.float32)
    x_ramp = np.clip(x_dist / float(feather_pixels), 0.0, 1.0).astype(np.float32)

    weights = np.outer(y_ramp, x_ramp)
    return np.maximum(weights, 1e-4)


def merge_geotiffs(
    input_paths: List[str],
    output_path: str,
    blend_mode: str = "feather",
    feather_px: int = 35,
    enhance_lines: bool = False,
    target_scale: Optional[Tuple[float, float]] = None,
    max_canvas_dim: int = 16384,
    max_megapixels: float = 120.0,
    auto_rescale: bool = True,
    log=print,
) -> Dict:
    """
    Merges multiple neighbouring or overlapping GeoTIFF files into one combined GeoTIFF.
    Seamlessly handles 10+ tiles via streaming tile placement and adaptive canvas resolution scaling.

    Args:
        input_paths: List of file paths to GeoTIFF images.
        output_path: Destination path for the merged GeoTIFF (.tif).
        blend_mode: 'feather' (smooth distance-based seam blending),
                    'average' (uniform weighting), or
                    'replace' (later inputs overwrite earlier inputs).
        feather_px: Number of edge pixels to ramp for feather blending.
        enhance_lines: If True, enhances faint Google Maps red boundary lines into
                       high-contrast, vivid, easily detectable lines.
        target_scale: Optional tuple (scale_x, scale_y). If None, uses the finest resolution
                      of the input images, adaptively rescaled if dimensions exceed safe memory limits.
        max_canvas_dim: Maximum width or height of merged canvas (default: 16384 px).
        max_megapixels: Maximum total pixels in millions (default: 120 MP) to prevent MemoryError.
        auto_rescale: If True, automatically scales resolution down to safe limits instead of raising error.
        log: Logging callable.

    Returns:
        Dict with summary of the merged raster (bounds, resolution, dimensions, rescaled status).
    """
    if len(input_paths) < 1:
        raise ValueError("At least one GeoTIFF file is required to merge.")

    log(f"[GeoTIFF Merger] Inspecting {len(input_paths)} GeoTIFF files...")
    metas: List[GeoTIFFMetadata] = []

    for p in input_paths:
        try:
            m = read_geotiff_metadata(p)
            metas.append(m)
            log(f"   Loaded: {os.path.basename(p)} ({m.width}x{m.height}px, scale: {m.scale_x:.6e} deg/px)")
        except Exception as e:
            log(f"   [!] Could not read {os.path.basename(p)}: {e}")

    if not metas:
        raise ValueError("No valid GeoTIFF files with proper georeferencing tags could be loaded.")

    # 1. Determine base target pixel resolution
    if target_scale is not None:
        target_scale_x, target_scale_y = target_scale
    else:
        # Default to the finest resolution among inputs (smallest degrees/pixel)
        target_scale_x = min(m.scale_x for m in metas)
        target_scale_y = min(m.scale_y for m in metas)

    # 2. Determine global union bounding box in geographic coordinates (WGS84)
    union_min_lng = min(m.min_lng for m in metas)
    union_max_lng = max(m.max_lng for m in metas)
    union_min_lat = min(m.min_lat for m in metas)
    union_max_lat = max(m.max_lat for m in metas)

    span_lng = max(1e-9, union_max_lng - union_min_lng)
    span_lat = max(1e-9, union_max_lat - union_min_lat)

    raw_w = max(1, int(round(span_lng / target_scale_x)))
    raw_h = max(1, int(round(span_lat / target_scale_y)))
    raw_mp = (raw_w * raw_h) / 1e6

    log(f"[GeoTIFF Merger] Combined Bounding Box:")
    log(f"   Longitude: [{union_min_lng:.6f}, {union_max_lng:.6f}] (span: {span_lng:.6f} deg)")
    log(f"   Latitude:  [{union_min_lat:.6f}, {union_max_lat:.6f}] (span: {span_lat:.6f} deg)")
    log(f"   Native Canvas: {raw_w} x {raw_h} px ({raw_mp:.1f} MP) at {target_scale_x:.6e} deg/px")

    # Spatial coverage check
    total_tile_area = sum((m.max_lng - m.min_lng) * (m.max_lat - m.min_lat) for m in metas)
    union_area = span_lng * span_lat
    coverage_ratio = total_tile_area / union_area if union_area > 0 else 1.0
    if coverage_ratio < 0.12 and len(metas) >= 2:
        log(f"[GeoTIFF Merger] Notice: Selected files cover {coverage_ratio*100:.1f}% of bounding area (multi-region / non-contiguous span).")

    # Adaptive canvas resizing check
    needs_rescale = (raw_w > max_canvas_dim or raw_h > max_canvas_dim or raw_mp > max_megapixels)
    rescale_factor = 1.0

    if needs_rescale:
        if not auto_rescale:
            raise ValueError(
                f"Merged canvas dimensions too large ({raw_w}x{raw_h}px, {raw_mp:.1f} MP). "
                f"Max allowed dimension is {max_canvas_dim}px ({max_megapixels:.0f} MP). "
                f"Check that input GeoTIFFs are neighbouring or enable auto-rescaling."
            )

        factor_w = raw_w / float(max_canvas_dim)
        factor_h = raw_h / float(max_canvas_dim)
        factor_mp = math.sqrt(raw_mp / float(max_megapixels)) if max_megapixels > 0 else 1.0
        rescale_factor = max(factor_w, factor_h, factor_mp, 1.0)

        target_scale_x *= rescale_factor
        target_scale_y *= rescale_factor

        canvas_width = max(1, int(round(span_lng / target_scale_x)))
        canvas_height = max(1, int(round(span_lat / target_scale_y)))

        log(f"[GeoTIFF Merger] [*] Large area detected across {len(metas)} files ({raw_w}x{raw_h}px, {raw_mp:.1f} MP).")
        log(f"[GeoTIFF Merger] [~] Adaptively optimizing resolution by {rescale_factor:.2f}x for fast, safe merge:")
        log(f"   Adjusted Canvas: {canvas_width} x {canvas_height} px ({canvas_width * canvas_height / 1e6:.1f} MP)")
        log(f"   New Pixel Scale: {target_scale_x:.6e} deg/px (~{target_scale_x * 111320:.1f}m/px)")
    else:
        canvas_width = raw_w
        canvas_height = raw_h

    # 3. Initialize canvas arrays
    canvas_rgb = np.zeros((canvas_height, canvas_width, 3), dtype=np.float32)
    canvas_weight = np.zeros((canvas_height, canvas_width), dtype=np.float32)
    canvas_line_mask = None
    canvas_line_rgb = None

    # 4. Stream and project each tile onto the canvas one by one
    for i, meta in enumerate(metas):
        # Load single image on demand
        img, _ = read_geotiff(meta.path)

        # Resample image if its scale differs from target scale
        scale_ratio_x = meta.scale_x / target_scale_x
        scale_ratio_y = meta.scale_y / target_scale_y

        if abs(scale_ratio_x - 1.0) > 0.005 or abs(scale_ratio_y - 1.0) > 0.005:
            new_w = max(1, int(round(meta.width * scale_ratio_x)))
            new_h = max(1, int(round(meta.height * scale_ratio_y)))
            interp = cv2.INTER_AREA if (scale_ratio_x < 1.0 and scale_ratio_y < 1.0) else cv2.INTER_LANCZOS4
            img_placed = cv2.resize(img, (new_w, new_h), interpolation=interp)
        else:
            img_placed = img

        h_placed, w_placed = img_placed.shape[:2]

        col_start = int(round((meta.min_lng - union_min_lng) / target_scale_x))
        row_start = int(round((union_max_lat - meta.max_lat) / target_scale_y))

        col_end = col_start + w_placed
        row_end = row_start + h_placed

        # Bounds clipping
        src_c0 = max(0, -col_start)
        src_r0 = max(0, -row_start)
        dst_c0 = max(0, col_start)
        dst_r0 = max(0, row_start)

        dst_c1 = min(canvas_width, col_end)
        dst_r1 = min(canvas_height, row_end)
        src_c1 = src_c0 + (dst_c1 - dst_c0)
        src_r1 = src_r0 + (dst_r1 - dst_r0)

        if dst_c1 <= dst_c0 or dst_r1 <= dst_r0:
            del img, img_placed
            continue

        patch = img_placed[src_r0:src_r1, src_c0:src_c1].astype(np.float32)

        # Ignore totally black/empty background pixels if any
        valid_mask = np.any(patch > 0, axis=2).astype(np.float32)

        if blend_mode == "feather":
            weights = compute_edge_weights(h_placed, w_placed, feather_px)[src_r0:src_r1, src_c0:src_c1]
            weights = weights * valid_mask
        elif blend_mode == "average":
            weights = valid_mask
        else:  # 'replace'
            canvas_rgb[dst_r0:dst_r1, dst_c0:dst_c1][valid_mask > 0] = 0
            canvas_weight[dst_r0:dst_r1, dst_c0:dst_c1][valid_mask > 0] = 0
            weights = valid_mask

        # Accumulate weighted colors
        for c in range(3):
            canvas_rgb[dst_r0:dst_r1, dst_c0:dst_c1, c] += patch[:, :, c] * weights

        canvas_weight[dst_r0:dst_r1, dst_c0:dst_c1] += weights

        # Track boundary line features (red Google Maps dashed lines)
        pr = patch[:, :, 0]
        pg = patch[:, :, 1]
        pb = patch[:, :, 2]
        ratio_b = pr / np.maximum(pb, 1)
        ratio_g = pr / np.maximum(pg, 1)
        is_boundary_pixel = (pr >= 135) & (ratio_b >= 1.45) & (ratio_g >= 1.45) & (pg <= 135) & (pb <= 135)
        if np.any(is_boundary_pixel):
            if canvas_line_mask is None:
                canvas_line_mask = np.zeros((canvas_height, canvas_width), dtype=bool)
                canvas_line_rgb = np.zeros((canvas_height, canvas_width, 3), dtype=np.uint8)

            dst_slice_mask = canvas_line_mask[dst_r0:dst_r1, dst_c0:dst_c1]
            dst_slice_rgb = canvas_line_rgb[dst_r0:dst_r1, dst_c0:dst_c1]

            update_mask = is_boundary_pixel & (~dst_slice_mask | (pr > dst_slice_rgb[:, :, 0]))
            dst_slice_mask[update_mask] = True
            for c in range(3):
                dst_slice_rgb[:, :, c][update_mask] = patch[:, :, c][update_mask].astype(np.uint8)

        log(f"   Placed tile {i+1}/{len(metas)}: {os.path.basename(meta.path)} at col=[{dst_c0}:{dst_c1}], row=[{dst_r0}:{dst_r1}]")
        del img, img_placed, patch

    # 5. Normalize overlapping pixels with memory cleanup
    nonzero_mask = canvas_weight > 0
    weight_slice = canvas_weight[nonzero_mask]
    for c in range(3):
        canvas_rgb[:, :, c][nonzero_mask] /= weight_slice
    del nonzero_mask, weight_slice, canvas_weight
    gc.collect()

    merged_uint8 = np.clip(np.round(canvas_rgb), 0, 255).astype(np.uint8)
    del canvas_rgb
    gc.collect()

    # 5b. Feature-preserving line restoration:
    if canvas_line_mask is not None and np.any(canvas_line_mask):
        merged_uint8[canvas_line_mask] = canvas_line_rgb[canvas_line_mask]
        log(f"[GeoTIFF Merger] Preserved {np.count_nonzero(canvas_line_mask)} boundary line pixels from dilution.")

        # 5c. Optional line enhancement
        if enhance_lines:
            merged_uint8 = apply_line_enhancement(merged_uint8, canvas_line_mask, color=(255, 30, 30))
            log("[GeoTIFF Merger] Applied high-contrast vivid line enhancement.")

        del canvas_line_mask, canvas_line_rgb
        gc.collect()
    elif enhance_lines:
        r = merged_uint8[:, :, 0].astype(np.int16)
        g = merged_uint8[:, :, 1].astype(np.int16)
        b = merged_uint8[:, :, 2].astype(np.int16)
        ratio_b = r / np.maximum(b, 1)
        ratio_g = r / np.maximum(g, 1)
        is_red = (r >= 135) & (ratio_b >= 1.45) & (ratio_g >= 1.45) & (g <= 135) & (b <= 135)
        if np.any(is_red):
            merged_uint8 = apply_line_enhancement(merged_uint8, is_red, color=(255, 30, 30))
            log("[GeoTIFF Merger] Applied high-contrast vivid line enhancement.")

    # 6. Save as GeoTIFF with WGS84 tags
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    out_pil = Image.fromarray(merged_uint8, mode="RGB")
    del merged_uint8
    gc.collect()

    tiff_tags = TiffImagePlugin.ImageFileDirectory_v2()

    # ModelPixelScaleTag (33550): scale_x, -scale_y, 0.0
    tiff_tags[33550] = (float(target_scale_x), -float(target_scale_y), 0.0)

    # ModelTiepointTag (33922): 0, 0, 0, origin_x, origin_y, 0
    tiff_tags[33922] = (0.0, 0.0, 0.0, float(union_min_lng), float(union_max_lat), 0.0)

    # GeoKeyDirectoryTag (34735): EPSG:4326
    tiff_tags[34735] = (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)

    out_pil.save(output_path, tiffinfo=tiff_tags, compression="tiff_lzw")
    log(f"[GeoTIFF Merger] Successfully saved merged GeoTIFF -> {output_path}")

    return {
        "output_path": output_path,
        "width": canvas_width,
        "height": canvas_height,
        "origin_lng": union_min_lng,
        "origin_lat": union_max_lat,
        "min_lng": union_min_lng,
        "max_lng": union_max_lng,
        "min_lat": union_min_lat,
        "max_lat": union_max_lat,
        "scale_x": target_scale_x,
        "scale_y": target_scale_y,
        "inputs_merged": len(metas),
        "rescaled": needs_rescale,
        "rescale_factor": rescale_factor,
    }



def apply_line_enhancement(img_rgb: np.ndarray, line_mask: np.ndarray, color=(255, 30, 30), thickness: int = 1) -> np.ndarray:
    """Enhances faint / dashed boundary lines into bold, high-contrast, easily detectable lines."""
    mask_u8 = line_mask.astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    dilated = cv2.dilate(mask_u8, kernel, iterations=max(1, thickness))
    
    out = img_rgb.copy()
    out[dilated > 0] = color
    return out


def enhance_geotiff_lines(
    input_path: str,
    output_path: Optional[str] = None,
    color: Tuple[int, int, int] = (255, 30, 30),
    thickness: int = 1,
    logger=None,
) -> str:
    """
    Enhances faint boundary lines on an existing GeoTIFF while preserving all EPSG:4326 tags.
    """
    def log(msg):
        if logger:
            logger(msg)
        else:
            print(msg)

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"File not found: {input_path}")

    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_enhanced{ext}"

    pil_img = Image.open(input_path)
    tags = {k: pil_img.tag_v2[k] for k in [33550, 33922, 34735] if k in pil_img.tag_v2}

    img_rgb = np.array(pil_img)
    if img_rgb.ndim == 2:
        img_rgb = cv2.cvtColor(img_rgb, cv2.COLOR_GRAY2RGB)
    elif img_rgb.shape[2] == 4:
        img_rgb = img_rgb[:, :, :3]

    r = img_rgb[:, :, 0].astype(np.int16)
    g = img_rgb[:, :, 1].astype(np.int16)
    b = img_rgb[:, :, 2].astype(np.int16)

    # Google Maps red boundary line detection (excluding magenta/pink POI icons)
    ratio_b = r / np.maximum(b, 1)
    ratio_g = r / np.maximum(g, 1)
    is_red = (r >= 135) & (ratio_b >= 1.45) & (ratio_g >= 1.45) & (g <= 135) & (b <= 135)
    count = np.count_nonzero(is_red)
    log(f"[Line Enhancer] Detected {count} boundary line pixels in {os.path.basename(input_path)}")

    if count > 0:
        img_rgb = apply_line_enhancement(img_rgb, is_red, color=color, thickness=thickness)

    out_pil = Image.fromarray(img_rgb, mode="RGB")
    tiff_tags = TiffImagePlugin.ImageFileDirectory_v2()
    for k, v in tags.items():
        tiff_tags[k] = v

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    out_pil.save(output_path, tiffinfo=tiff_tags, compression="tiff_lzw")
    log(f"[Line Enhancer] Successfully saved enhanced GeoTIFF -> {output_path}")
    return output_path


def main():
    if len(sys.argv) < 3:
        print("Usage: python geotiff_merger.py <input1.tif> <input2.tif> ... -o <output.tif>")
        sys.exit(1)

    args = sys.argv[1:]
    output_path = "merged_output.tif"
    inputs = []
    
    i = 0
    while i < len(args):
        if args[i] in ("-o", "--output"):
            output_path = args[i+1]
            i += 2
        else:
            inputs.append(args[i])
            i += 1

    if not inputs:
        print("Error: No input files provided.")
        sys.exit(1)

    merge_geotiffs(inputs, output_path)

if __name__ == "__main__":
    main()
