import os
import json
import zipfile
import requests
from playwright_pipeline import run_pipeline
from utils.excel_handler import get_user_downloads_dir

DEFAULT_BOUNDARY_OUTPUT_DIR = os.path.join(get_user_downloads_dir(), "JarvisMaps_Boundaries")


def save_geojson_direct(geom: dict, name: str, query: str, output_path: str):
    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": name,
                    "query": query,
                    "source": "OSM",
                },
                "geometry": geom,
            }
        ],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def save_kml_direct(ring: list[list[float]], name: str, output_path: str):
    coords_str = " ".join(f"{pt[0]},{pt[1]},0" for pt in ring)
    kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{name}</name>
    <Placemark>
      <name>{name}</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>{coords_str}</coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(kml_content)


def save_kmz_direct(kml_path: str, output_path: str):
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(kml_path, arcname=os.path.basename(kml_path))


def fetch_osm_boundary(query: str, output_dir: str = None) -> dict | None:
    if not output_dir or output_dir == "boundaries":
        output_dir = DEFAULT_BOUNDARY_OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "JarvisMapsAI/2.0 (mailto:support@jarvismaps.ai)"}
    params = {
        "q": query,
        "format": "jsonv2",
        "polygon_geojson": 1,
        "countrycodes": "in",
        "limit": 5,
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            for item in r.json():
                gj = item.get("geojson", {})
                gtype = gj.get("type")
                if gtype in ("Polygon", "MultiPolygon"):
                    disp = item.get("display_name", query)
                    lat = float(item.get("lat", 0))
                    lon = float(item.get("lon", 0))
                    coords = gj.get("coordinates", [])
                    ring = coords[0] if gtype == "Polygon" else coords[0][0]

                    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in query.replace(" ", "_"))[:60].strip("_")
                    gj_path = os.path.join(output_dir, f"{safe}.geojson")
                    kml_path = os.path.join(output_dir, f"{safe}.kml")
                    kmz_path = os.path.join(output_dir, f"{safe}.kmz")

                    save_geojson_direct(gj, disp, query, gj_path)
                    save_kml_direct(ring, disp, kml_path)
                    save_kmz_direct(kml_path, kmz_path)

                    return {
                        "query": query,
                        "display_name": disp,
                        "status": "Success",
                        "error": "",
                        "source": "OSM",
                        "polygon_points": len(ring),
                        "center_lat": lat,
                        "center_lng": lon,
                        "screenshot_path": None,
                        "geotiff_path": None,
                        "geojson_path": gj_path,
                        "kml_path": kml_path,
                        "kmz_path": kmz_path,
                    }
    except Exception as exc:
        print(f"OSM boundary fallback error: {exc}")

    return None


def fetch_boundary(
    query: str,
    output_dir: str = None,
) -> dict | None:
    if not output_dir or output_dir == "boundaries":
        output_dir = DEFAULT_BOUNDARY_OUTPUT_DIR
    query = query.strip()

    if not query:
        print("Query cannot be empty.")
        return None

    result = run_pipeline(
        query=query,
        output_dir=output_dir,
    )

    if result.status.lower() != "success":
        print(
            f"Google Maps boundary extraction failed: {result.error}. "
            f"Attempting OSM fallback..."
        )
        osm_res = fetch_osm_boundary(query=query, output_dir=output_dir)
        if osm_res:
            return osm_res

        return None

    return {
        "query": result.query,
        "display_name": result.display_name or query,
        "status": result.status,
        "error": result.error or "",
        "source": result.polygon_source,
        "polygon_points": result.polygon_points,
        "center_lat": result.center_lat,
        "center_lng": result.center_lng,
        "screenshot_path": result.screenshot_path,
        "geotiff_path": result.geotiff_path,
        "geojson_path": result.geojson_path,
        "kml_path": result.kml_path,
        "kmz_path": result.kmz_path,
    }


def export_boundary(
    query: str,
    output_dir: str = None,
) -> dict:
    if not output_dir or output_dir == "boundaries":
        output_dir = DEFAULT_BOUNDARY_OUTPUT_DIR
    try:
        result = fetch_boundary(
            query=query,
            output_dir=output_dir,
        )

        if not result:
            return {
                "query": query,
                "status": "Failed",
                "error": (
                    "Google Maps boundary could not "
                    "be detected."
                ),
                "display_name": query,
                "geojson_path": None,
                "kml_path": None,
                "kmz_path": None,
                "screenshot_path": None,
                "geotiff_path": None,
                "source": "",
            }

        return result

    except Exception as exc:
        return {
            "query": query,
            "status": "Failed",
            "error": str(exc),
            "display_name": query,
            "geojson_path": None,
            "kml_path": None,
            "kmz_path": None,
            "screenshot_path": None,
            "geotiff_path": None,
            "source": "",
        }