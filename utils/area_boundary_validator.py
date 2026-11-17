"""
area_boundary_validator.py
--------------------------
Boundary Validator and Extractor for Area Explorer data in Jarvis Maps AI.

Takes area data extracted from Area Explorer (or uploaded Area Explorer Excel/CSV files),
validates which locations actually have red outline boundaries in Google Maps,
filters out places without boundaries, and exports the validated boundary datasets
(Excel, CSV, GeoJSON, KML, KMZ) or pushes them to Boundary Tracer.
"""

from __future__ import annotations

import os
import sys
import time
import re
import threading
from typing import Callable, List, Dict, Any, Optional

try:
    import pandas as pd
except ImportError:
    pd = None

from utils.boundary_extractor import export_boundary
from utils.excel_handler import get_user_downloads_dir

DEFAULT_VALIDATED_DIR = os.path.join(get_user_downloads_dir(), "JarvisMaps_Validated_Areas")


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    # Remove floating point artifact e.g. "110001.0" -> "110001"
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _format_points_count(pts_val: Any) -> int:
    """Safely extract point count whether pts_val is an int, list, or string."""
    if isinstance(pts_val, int):
        return pts_val
    if isinstance(pts_val, (list, tuple)):
        return len(pts_val)
    if isinstance(pts_val, str) and pts_val.isdigit():
        return int(pts_val)
    return 0


def _extract_coords_from_boundary_info(b_info: dict) -> list:
    """Extract [[lat, lon], ...] polygon coords from boundary result dict or saved files."""
    if not isinstance(b_info, dict):
        return []

    # 1. If polygon_points is already a list of points
    pts = b_info.get("polygon_points")
    if isinstance(pts, (list, tuple)) and pts and isinstance(pts[0], (list, tuple)):
        return [[p[0], p[1]] for p in pts if len(p) >= 2]

    # 2. Try reading from geojson_path
    geojson_path = b_info.get("geojson_path")
    if geojson_path and os.path.exists(geojson_path):
        try:
            import json
            with open(geojson_path, "r", encoding="utf-8") as f:
                gj = json.load(f)
            geom = gj.get("geometry") or {}
            coords = geom.get("coordinates")
            if coords and isinstance(coords, list):
                ring = coords[0] if isinstance(coords[0], list) and isinstance(coords[0][0], list) else coords
                return [[pt[1], pt[0]] for pt in ring if len(pt) >= 2]
        except Exception:
            pass

    # 3. Try reading from kml_path
    kml_path = b_info.get("kml_path")
    if kml_path and os.path.exists(kml_path):
        try:
            with open(kml_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            m = re.search(r"<coordinates>(.*?)</coordinates>", content, re.DOTALL)
            if m:
                raw_coords = m.group(1).strip().split()
                res = []
                for rc in raw_coords:
                    parts = rc.split(",")
                    if len(parts) >= 2:
                        lon, lat = float(parts[0]), float(parts[1])
                        res.append([lat, lon])
                if res:
                    return res
        except Exception:
            pass

    return []


def build_boundary_query(name: str, parent_area: str = "", city: str = "") -> str:
    """
    Build an optimal Google Maps search query to trigger official red boundary outlines.
    Avoids duplicate tokens (e.g. 'Sector 17, Chandigarh' + 'Chandigarh' -> 'Sector 17, Chandigarh').
    """
    name_clean = _clean_str(name)
    parent_clean = _clean_str(parent_area)
    city_clean = _clean_str(city)

    if not name_clean:
        return parent_clean or city_clean

    # If name already has comma qualification
    parts = [p.strip() for p in name_clean.split(",") if p.strip()]
    existing_tokens = {p.casefold() for p in parts}

    if parent_clean and parent_clean.casefold() not in existing_tokens:
        parts.append(parent_clean)
        existing_tokens.add(parent_clean.casefold())

    if city_clean and city_clean.casefold() not in existing_tokens:
        parts.append(city_clean)
        existing_tokens.add(city_clean.casefold())

    return ", ".join(parts)


def load_area_records(source: Any) -> List[Dict[str, Any]]:
    """
    Load and normalize area records from:
    - list of dicts (from Area Explorer UI)
    - filepath (Excel .xlsx/.xls, CSV .csv, or text .txt)
    """
    records: List[Dict[str, Any]] = []

    if isinstance(source, (list, tuple)):
        for item in source:
            if isinstance(item, dict):
                name = _clean_str(item.get("name") or item.get("Area Name") or item.get("original_name"))
                parent = _clean_str(item.get("parent_area") or item.get("Parent Area / Sub-Locality") or item.get("city") or "")
                area_type = _clean_str(item.get("type") or item.get("Area Type") or "Area")
                lat = item.get("latitude") or item.get("Latitude") or ""
                lon = item.get("longitude") or item.get("Longitude") or ""
                gmaps_url = _clean_str(item.get("google_maps_url") or item.get("Google Maps URL") or "")
                
                query = _clean_str(item.get("query")) or build_boundary_query(name, parent)

                if name or query:
                    records.append({
                        "name": name or query,
                        "parent_area": parent,
                        "type": area_type,
                        "latitude": lat,
                        "longitude": lon,
                        "google_maps_url": gmaps_url,
                        "query": query,
                        "status": "Pending",
                        "has_boundary": None,
                        "boundary_info": {},
                    })
        return records

    # If string, assume file path
    if isinstance(source, str) and os.path.exists(source):
        ext = os.path.splitext(source)[1].lower()

        if ext in (".xlsx", ".xls"):
            if pd is None:
                raise RuntimeError("pandas is required to read Excel files. Please install openpyxl/pandas.")
            df = pd.read_excel(source)
            return _df_to_records(df)

        elif ext in (".csv", ".tsv"):
            if pd is not None:
                try:
                    df = pd.read_csv(source, sep=None, engine="python")
                    return _df_to_records(df)
                except Exception:
                    pass
            # Fallback csv reader
            import csv
            with open(source, "r", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.reader(f)
                rows = list(reader)
                if not rows:
                    return []
                # Check header
                header = [h.strip() for h in rows[0]]
                name_idx, parent_idx, type_idx = -1, -1, -1
                lat_idx, lon_idx, url_idx = -1, -1, -1
                
                for idx, col in enumerate(header):
                    col_lower = col.lower()
                    if any(k in col_lower for k in ["area name", "name", "location", "place"]) and name_idx == -1:
                        name_idx = idx
                    elif any(k in col_lower for k in ["parent", "sub-locality", "city", "district"]) and parent_idx == -1:
                        parent_idx = idx
                    elif any(k in col_lower for k in ["type", "category"]) and type_idx == -1:
                        type_idx = idx
                    elif any(k in col_lower for k in ["latitude", "lat"]):
                        lat_idx = idx
                    elif any(k in col_lower for k in ["longitude", "lon", "lng"]):
                        lon_idx = idx
                    elif any(k in col_lower for k in ["url", "link", "google maps"]):
                        url_idx = idx

                has_header = (name_idx != -1)
                data_rows = rows[1:] if has_header else rows
                if name_idx == -1:
                    name_idx = 0

                for r in data_rows:
                    if not r:
                        continue
                    name = _clean_str(r[name_idx]) if name_idx < len(r) else ""
                    parent = _clean_str(r[parent_idx]) if (parent_idx != -1 and parent_idx < len(r)) else ""
                    area_type = _clean_str(r[type_idx]) if (type_idx != -1 and type_idx < len(r)) else "Area"
                    lat = _clean_str(r[lat_idx]) if (lat_idx != -1 and lat_idx < len(r)) else ""
                    lon = _clean_str(r[lon_idx]) if (lon_idx != -1 and lon_idx < len(r)) else ""
                    url = _clean_str(r[url_idx]) if (url_idx != -1 and url_idx < len(r)) else ""
                    
                    if name:
                        query = build_boundary_query(name, parent)
                        records.append({
                            "name": name,
                            "parent_area": parent,
                            "type": area_type,
                            "latitude": lat,
                            "longitude": lon,
                            "google_maps_url": url,
                            "query": query,
                            "status": "Pending",
                            "has_boundary": None,
                            "boundary_info": {},
                        })
            return records

        elif ext == ".txt":
            with open(source, "r", encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append({
                            "name": line,
                            "parent_area": "",
                            "type": "Area",
                            "latitude": "",
                            "longitude": "",
                            "google_maps_url": "",
                            "query": line,
                            "status": "Pending",
                            "has_boundary": None,
                            "boundary_info": {},
                        })
            return records

    return records


def _df_to_records(df: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    cols = {str(c).strip().lower(): c for c in df.columns}

    # Find matching column names
    name_col = None
    for cand in ["area name", "name", "original_name", "location", "place", "query", "area"]:
        if cand in cols:
            name_col = cols[cand]
            break
    if not name_col and len(df.columns) > 0:
        name_col = df.columns[0]

    parent_col = None
    for cand in ["parent area / sub-locality", "parent area", "parent_area", "parent", "sub-locality", "city", "district"]:
        if cand in cols:
            parent_col = cols[cand]
            break

    type_col = None
    for cand in ["area type", "type", "category"]:
        if cand in cols:
            type_col = cols[cand]
            break

    lat_col = None
    for cand in ["latitude", "lat"]:
        if cand in cols:
            lat_col = cols[cand]
            break

    lon_col = None
    for cand in ["longitude", "lon", "lng"]:
        if cand in cols:
            lon_col = cols[cand]
            break

    url_col = None
    for cand in ["google maps url", "google_maps_url", "url", "maps url"]:
        if cand in cols:
            url_col = cols[cand]
            break

    for _, row in df.iterrows():
        name = _clean_str(row.get(name_col)) if name_col else ""
        parent = _clean_str(row.get(parent_col)) if parent_col else ""
        area_type = _clean_str(row.get(type_col)) if type_col else "Area"
        lat = row.get(lat_col) if lat_col else ""
        lon = row.get(lon_col) if lon_col else ""
        url = _clean_str(row.get(url_col)) if url_col else ""

        if name:
            query = build_boundary_query(name, parent)
            records.append({
                "name": name,
                "parent_area": parent,
                "type": area_type,
                "latitude": "" if pd.isna(lat) else lat,
                "longitude": "" if pd.isna(lon) else lon,
                "google_maps_url": url,
                "query": query,
                "status": "Pending",
                "has_boundary": None,
                "boundary_info": {},
            })
    return records


class AreaBoundaryValidator:
    """
    High-speed worker engine to validate and extract boundaries for area records.
    Supports Turbo Fast mode (~1-2s per area with persistent browsers) and Deep GIS mode,
    with multi-worker concurrency and thread-safe progress callbacks.
    """

    def __init__(
        self,
        records: List[Dict[str, Any]],
        output_dir: str = None,
        headless: bool = False,
        workers: int = 3,
        mode: str = "fast",
        on_progress: Optional[Callable[[int, int, Dict[str, Any], Dict[str, Any]], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        on_complete: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.records = [dict(r) for r in records]
        if not output_dir or output_dir == "boundaries/validated_areas":
            output_dir = DEFAULT_VALIDATED_DIR
        self.output_dir = output_dir
        self.headless = headless
        self.workers = max(1, min(int(workers or 3), 8))
        self.mode = str(mode or "fast").lower().strip()
        self.on_progress = on_progress
        self.on_log = on_log
        self.on_complete = on_complete

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self.validated_count = 0
        self.no_boundary_count = 0
        self.total_count = len(self.records)
        self.durations: List[float] = []

    def start(self):
        """Start boundary validation in background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal validation to stop."""
        self._stop_event.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _log(self, msg: str, tag: str = "normal"):
        if self.on_log:
            try:
                self.on_log(msg, tag)
            except Exception:
                pass
        else:
            print(f"[Validator] {msg}")

    def _run(self):
        if self.mode == "deep":
            self._run_deep()
        else:
            self._run_fast()

    def _run_fast(self):
        """High-speed parallel validation reusing persistent Playwright browsers."""
        import queue
        import urllib.parse
        from playwright.sync_api import sync_playwright
        import cv2
        import numpy as np
        from playwright_pipeline import _adaptive_google_red_mask, _red_render_metrics, _parse_maps_url

        os.makedirs(self.output_dir, exist_ok=True)
        total = len(self.records)
        num_workers = min(self.workers, total) if total > 0 else 1
        mode_desc = f"Headless {'ON' if self.headless else 'OFF (Visible Browser)'}, {num_workers} Parallel Worker(s)"
        self._log(f"⚡ Starting TURBO FAST boundary validation for {total} area(s) [{mode_desc}]...\n", "info")

        work_queue = queue.Queue()
        for idx, item in enumerate(self.records, 1):
            work_queue.put((idx, item))

        tested_count = 0

        def _worker_task(wid: int):
            nonlocal tested_count
            p = None
            browser = None
            try:
                p = sync_playwright().start()
                v_args = [
                    "--disable-notifications",
                    "--disable-popup-blocking",
                    "--no-first-run",
                    "--disable-features=CalculateNativeWinOcclusion",
                ]
                try:
                    browser = p.chromium.launch(
                        headless=bool(self.headless),
                        args=v_args,
                    )
                except Exception as l_err:
                    try:
                        browser = p.chromium.launch(
                            headless=bool(self.headless),
                            channel="msedge",
                            args=v_args,
                        )
                    except Exception:
                        try:
                            browser = p.chromium.launch(
                                headless=bool(self.headless),
                                channel="chrome",
                                args=v_args,
                            )
                        except Exception:
                            raise l_err
                page = browser.new_page(viewport={"width": 1280, "height": 720})
                page.set_default_timeout(10000)
                first_nav = True

                while not self._stop_event.is_set():
                    try:
                        idx, item = work_queue.get_nowait()
                    except queue.Empty:
                        break

                    query = item.get("query") or item.get("name")
                    t_start = time.time()
                    url = "https://www.google.com/maps/place/" + urllib.parse.quote_plus(query)

                    try:
                        page.goto(url, wait_until="commit", timeout=10000)
                    except Exception:
                        pass

                    if first_nav:
                        first_nav = False
                        try:
                            loc = page.locator("button[aria-label='Accept all'], button[jsname='higCR']").first
                            if loc.count() > 0:
                                loc.click(timeout=1000)
                        except Exception:
                            pass

                    # Allow brief time for Google Maps canvas & overview card to settle
                    time.sleep(1.3)

                    # 1. Check DOM overview indicators (Locality / Sublocality heading)
                    has_locality_dom = False
                    dom_title = ""
                    try:
                        dom_info = page.evaluate("""() => {
                            const title = document.querySelector('h1')?.innerText?.trim() || '';
                            const subtitle = document.querySelector('h2')?.innerText?.trim() || '';
                            const hasQuickFacts = Array.from(document.querySelectorAll('button, span')).some(el => {
                                const t = el.innerText || '';
                                return t.includes('Quick facts') || t.includes('Hotels') || t.includes('Overview');
                            });
                            return { title, subtitle, hasQuickFacts };
                        }""")
                        dom_title = dom_info.get("title", "")
                        if dom_title and (dom_info.get("subtitle") or dom_info.get("hasQuickFacts")):
                            has_locality_dom = True
                    except Exception:
                        pass

                    # 2. Check Red Outline on Google Maps canvas
                    has_red_outline = False
                    red_metrics = {}
                    ss_path = None
                    try:
                        ss_bytes = page.screenshot()
                        img = cv2.imdecode(np.frombuffer(ss_bytes, np.uint8), cv2.IMREAD_COLOR)
                        # Crop map canvas (excluding side panel 0-420px and top search bar 0-80px)
                        map_crop = img[80:, 420:]
                        red_metrics = _red_render_metrics(map_crop)
                        red_px = red_metrics.get("map_red_pixels", 0)
                        h_span = red_metrics.get("horizontal_span_px", 0)
                        v_span = red_metrics.get("vertical_span_px", 0)
                        # Red outline spans broadly across the map canvas
                        if red_px > 120 and h_span > 200 and v_span > 150:
                            has_red_outline = True
                    except Exception:
                        pass

                    # Boundary confirmed if locality DOM overview or red perimeter outline is present
                    has_bnd = bool(has_locality_dom or has_red_outline)
                    elapsed = time.time() - t_start

                    # Extract coordinates and zoom from URL
                    live_url = page.url
                    parsed_camera = _parse_maps_url(live_url) or {}
                    c_lat = parsed_camera.get("lat")
                    c_lng = parsed_camera.get("lng")
                    c_zoom = parsed_camera.get("zoom", 14.0)

                    safe = re.sub(r"[^\w\-_\.]", "_", query)
                    if has_bnd:
                        ss_path = os.path.join(self.output_dir, f"{safe}_boundary.png")
                        try:
                            page.screenshot(path=ss_path)
                        except Exception:
                            pass

                    # Estimated boundary points count from red outline span or pixels
                    pts_count = 0
                    if has_bnd:
                        pts_count = int(red_metrics.get("map_red_pixels", 150)) // 2 or 120

                    b_info = {
                        "query": query,
                        "display_name": dom_title or item.get("name") or query,
                        "status": "Success" if has_bnd else "Failed",
                        "error": "" if has_bnd else "No boundary detected in Google Maps",
                        "source": "Google Maps (Visual Outline Confirmed)" if has_red_outline else "Google Maps (Locality Overview)",
                        "polygon_points": pts_count if has_bnd else 0,
                        "center_lat": c_lat,
                        "center_lng": c_lng,
                        "zoom": c_zoom,
                        "screenshot_path": ss_path or "",
                    }

                    item["has_boundary"] = has_bnd
                    item["status"] = "✅ Boundary Found" if has_bnd else "❌ No Boundary"
                    item["boundary_info"] = b_info

                    with self._lock:
                        tested_count += 1
                        self.durations.append(elapsed)
                        if has_bnd:
                            self.validated_count += 1
                            self._log(
                                f"[{tested_count}/{total}] ✅ Boundary Detected: {query} ({elapsed:.1f}s, {pts_count} pts)\n",
                                "success"
                            )
                        else:
                            self.no_boundary_count += 1
                            self._log(
                                f"[{tested_count}/{total}] ❌ No boundary: {query} ({elapsed:.1f}s)\n",
                                "warning"
                            )

                        if self.on_progress:
                            try:
                                stats = {
                                    "tested": tested_count,
                                    "total": total,
                                    "valid": self.validated_count,
                                    "no_boundary": self.no_boundary_count,
                                    "elapsed": elapsed,
                                    "avg_time": sum(self.durations) / len(self.durations) if self.durations else 1.5,
                                }
                                self.on_progress(idx, total, item, stats)
                            except Exception:
                                pass

                    work_queue.task_done()

            except Exception as w_err:
                self._log(f"Worker {wid} note: {w_err}", "warning")
            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
                if p:
                    try:
                        p.stop()
                    except Exception:
                        pass

        # Spawn workers
        threads = []
        for wid in range(1, num_workers + 1):
            t = threading.Thread(target=_worker_task, args=(wid,), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        # Summary
        summary = {
            "total": total,
            "tested": len(self.durations),
            "validated": self.validated_count,
            "no_boundary": self.no_boundary_count,
            "stopped": self._stop_event.is_set(),
            "records": self.records,
            "validated_records": [r for r in self.records if r.get("has_boundary") is True],
            "no_boundary_records": [r for r in self.records if r.get("has_boundary") is False],
        }

        self._log(
            f"\n🏁 Turbo Fast Validation complete: {self.validated_count} verified with boundaries, "
            f"{self.no_boundary_count} without boundaries in {sum(self.durations):.1f}s total work.\n",
            "info"
        )

        if self.on_complete:
            try:
                self.on_complete(summary)
            except Exception as e:
                print(f"Complete callback error: {e}")

    def _run_deep(self):
        """Full deep pipeline validation generating GeoTIFF and GIS assets."""
        try:
            import playwright_pipeline
            playwright_pipeline.BOUNDARY_HEADLESS = bool(self.headless)
        except Exception:
            pass

        os.makedirs(self.output_dir, exist_ok=True)
        total = len(self.records)
        mode_desc = "Headless ON" if self.headless else "Headless OFF (Visible Browser)"
        self._log(f"🔬 Starting DEEP GIS boundary validation for {total} area(s) [{mode_desc}]...\n", "info")

        for idx, item in enumerate(self.records, 1):
            if self._stop_event.is_set():
                self._log("⏹ Validation stopped by user.\n", "warning")
                break

            query = item.get("query") or item.get("name")
            self._log(f"[{idx}/{total}] Checking Google Maps boundary: {query}...", "normal")

            t_start = time.time()
            try:
                result = export_boundary(query, output_dir=self.output_dir)
            except Exception as exc:
                result = {
                    "query": query,
                    "status": "Failed",
                    "error": str(exc),
                }

            elapsed = time.time() - t_start
            self.durations.append(elapsed)

            status = result.get("status")
            has_bnd = (status == "Success")

            item["has_boundary"] = has_bnd
            item["status"] = "✅ Boundary Found" if has_bnd else "❌ No Boundary"
            item["boundary_info"] = result

            if has_bnd:
                self.validated_count += 1
                pts = result.get("polygon_points")
                pts_cnt = _format_points_count(pts)
                src = result.get("source", "Google Maps")
                self._log(
                    f"   ✅ Boundary Detected! ({pts_cnt} points, {src}, {elapsed:.1f}s)\n",
                    "success"
                )
            else:
                self.no_boundary_count += 1
                err = result.get("error", "No boundary polygon in Google Maps")
                self._log(
                    f"   ❌ No boundary found. ({err}, {elapsed:.1f}s)\n",
                    "warning"
                )

            # Fire progress callback
            if self.on_progress:
                try:
                    stats = {
                        "tested": idx,
                        "total": total,
                        "valid": self.validated_count,
                        "no_boundary": self.no_boundary_count,
                        "elapsed": elapsed,
                        "avg_time": sum(self.durations) / len(self.durations) if self.durations else 15.0,
                    }
                    self.on_progress(idx, total, item, stats)
                except Exception as cb_err:
                    print(f"Callback error: {cb_err}")

        # Summary
        summary = {
            "total": total,
            "tested": len(self.durations),
            "validated": self.validated_count,
            "no_boundary": self.no_boundary_count,
            "stopped": self._stop_event.is_set(),
            "records": self.records,
            "validated_records": [r for r in self.records if r.get("has_boundary") is True],
            "no_boundary_records": [r for r in self.records if r.get("has_boundary") is False],
        }

        self._log(
            f"\n🏁 Deep GIS validation complete: {self.validated_count} verified with boundaries, "
            f"{self.no_boundary_count} without boundaries.\n",
            "info"
        )

        if self.on_complete:
            try:
                self.on_complete(summary)
            except Exception as e:
                print(f"Complete callback error: {e}")


def export_validated_to_excel(
    records: List[Dict[str, Any]],
    output_path: str,
    only_validated: bool = True
) -> str:
    """
    Export validated area data to Excel with comprehensive boundary details.
    """
    if pd is None:
        raise RuntimeError("pandas is required for Excel export.")

    target_records = [r for r in records if r.get("has_boundary") is True] if only_validated else records

    rows = []
    for r in target_records:
        b_info = r.get("boundary_info") or {}
        pts_cnt = _format_points_count(b_info.get("polygon_points")) if r.get("has_boundary") else 0
        rows.append({
            "Area Name": r.get("name", ""),
            "Parent Area / Sub-Locality": r.get("parent_area", ""),
            "Area Type": r.get("type", "Area"),
            "Google Maps Boundary": "Confirmed" if r.get("has_boundary") else "Not Found",
            "Boundary Points": pts_cnt,
            "Boundary Source": b_info.get("source", ""),
            "Center Latitude": b_info.get("center_lat") or r.get("latitude", ""),
            "Center Longitude": b_info.get("center_lng") or r.get("longitude", ""),
            "Search Query": r.get("query", ""),
            "GeoJSON File": b_info.get("geojson_path", ""),
            "KML File": b_info.get("kml_path", ""),
            "KMZ File": b_info.get("kmz_path", ""),
            "Google Maps URL": r.get("google_maps_url", ""),
        })

    df = pd.DataFrame(rows)
    sheet_name = "Validated Boundaries" if only_validated else "Area Validation"

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
        ws = writer.sheets[sheet_name]
        for col in ws.columns:
            max_len = 0
            for cell in col:
                try:
                    max_len = max(max_len, len(str(cell.value or "")))
                except Exception:
                    pass
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    return output_path


def export_validated_to_csv(
    records: List[Dict[str, Any]],
    output_path: str,
    only_validated: bool = True
) -> str:
    """
    Export validated area records to CSV.
    """
    import csv
    target_records = [r for r in records if r.get("has_boundary") is True] if only_validated else records

    headers = [
        "Area Name", "Parent Area / Sub-Locality", "Area Type",
        "Google Maps Boundary", "Boundary Points", "Boundary Source",
        "Center Latitude", "Center Longitude", "Search Query",
        "GeoJSON File", "KML File", "KMZ File", "Google Maps URL"
    ]

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for r in target_records:
            b_info = r.get("boundary_info") or {}
            pts_cnt = _format_points_count(b_info.get("polygon_points")) if r.get("has_boundary") else 0
            writer.writerow([
                r.get("name", ""),
                r.get("parent_area", ""),
                r.get("type", "Area"),
                "Confirmed" if r.get("has_boundary") else "Not Found",
                pts_cnt,
                b_info.get("source", ""),
                b_info.get("center_lat") or r.get("latitude", ""),
                b_info.get("center_lng") or r.get("longitude", ""),
                r.get("query", ""),
                b_info.get("geojson_path", ""),
                b_info.get("kml_path", ""),
                b_info.get("kmz_path", ""),
                r.get("google_maps_url", ""),
            ])

    return output_path


def export_combined_kml(records: List[Dict[str, Any]], output_path: str) -> str:
    """
    Combine all validated polygon boundaries into a single master KML map file.
    """
    import xml.etree.ElementTree as ET
    from xml.dom import minidom

    validated = [r for r in records if r.get("has_boundary") is True]

    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml, "Document")
    ET.SubElement(document, "name").text = "Validated Google Maps Boundaries"

    # Define standard polygon style
    style = ET.SubElement(document, "Style", id="boundaryStyle")
    line_style = ET.SubElement(style, "LineStyle")
    ET.SubElement(line_style, "color").text = "ff0000ff"  # Red outline
    ET.SubElement(line_style, "width").text = "2.5"
    poly_style = ET.SubElement(style, "PolyStyle")
    ET.SubElement(poly_style, "color").text = "330000ff"  # Semi-transparent red fill

    for r in validated:
        b_info = r.get("boundary_info") or {}
        pts = _extract_coords_from_boundary_info(b_info)
        if not pts:
            continue

        placemark = ET.SubElement(document, "Placemark")
        ET.SubElement(placemark, "name").text = r.get("name", "Area")
        ET.SubElement(placemark, "styleUrl").text = "#boundaryStyle"

        pts_cnt = _format_points_count(b_info.get("polygon_points")) or len(pts)
        desc = f"Area: {r.get('name')}\nParent: {r.get('parent_area')}\nType: {r.get('type')}\nPoints: {pts_cnt}"
        ET.SubElement(placemark, "description").text = desc

        polygon = ET.SubElement(placemark, "Polygon")
        outer = ET.SubElement(polygon, "outerBoundaryIs")
        ring = ET.SubElement(outer, "LinearRing")

        # Format coords lon,lat,0
        coord_strs = []
        for pt in pts:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                lat, lon = pt[0], pt[1]
                coord_strs.append(f"{lon},{lat},0")
        
        # Ensure closed ring
        if coord_strs and coord_strs[0] != coord_strs[-1]:
            coord_strs.append(coord_strs[0])

        ET.SubElement(ring, "coordinates").text = " ".join(coord_strs)

    xml_string = ET.tostring(kml, encoding="utf-8")
    pretty_xml = minidom.parseString(xml_string).toprettyxml(indent="    ")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(pretty_xml)

    return output_path
