"""
utils/qgis_exporter.py
======================
PyQGIS-based polygon exporter — tested against QGIS 3.26.3.

Supports export to: KML, KMZ, GeoJSON, Shapefile (.shp), GeoPackage (.gpkg).

Usage
-----
    from utils.qgis_exporter import QGISExporter

    # Standalone (outside QGIS) — most common case
    with QGISExporter() as exporter:
        result = exporter.export_polygon(
            geojson_dict = {"type": "Polygon", "coordinates": [...]},
            name         = "Sector 38 Noida",
            output_dir   = "boundaries",
            formats      = ["kml", "kmz"],
        )
    # result = {"kml": "boundaries/Sector_38_Noida.kml",
    #           "kmz": "boundaries/Sector_38_Noida.kmz",
    #           "status": "ok", "error": ""}

    # From existing GeoJSON file
    with QGISExporter() as exporter:
        result = exporter.export_from_file(
            geojson_path = "boundaries/sector.geojson",
            output_dir   = "boundaries",
            formats      = ["kml", "kmz"],
        )

    # Inside QGIS Python console (QgsApplication already running)
    exporter = QGISExporter(standalone=False)
    result = exporter.export_polygon(...)

Setup — Windows (QGIS 3.26.3 standalone installer)
---------------------------------------------------
Either set env var before running your script:
    set QGIS_PREFIX_PATH=C:\\Program Files\\QGIS 3.26.3

Or pass explicitly:
    QGISExporter(prefix_path=r"C:\\Program Files\\QGIS 3.26.3")

Auto-detection checks common install locations in order.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from typing import Optional

# ── QGIS 3.26.3 specific paths ───────────────────────────────────────────────
# Standalone installer on Windows creates "QGIS 3.26.3" (with patch version).
# OSGeo4W installer uses "C:/OSGeo4W" with apps/qgis-ltr or apps/qgis.
_COMMON_PREFIXES = [
    # Standalone installer — exact version first
    r"C:/Program Files/QGIS 3.26.3",
    r"C:/Program Files/QGIS 3.26",
    # Other nearby versions (fallback)
    r"C:/Program Files/QGIS 3.28",
    r"C:/Program Files/QGIS 3.28.0",
    r"C:/Program Files/QGIS 3.22",
    r"C:/Program Files/QGIS 3.34",
    r"C:/Program Files/QGIS 3.36",
    r"C:/Program Files/QGIS 3.38",
    # OSGeo4W
    r"C:/OSGeo4W",
    r"C:/OSGeo4W64",
    # Linux
    "/usr",
    "/usr/local",
]

# Python 3.9 bundled with QGIS 3.26 on Windows
_WIN_PYTHON_DIRS = [
    "apps/Python39/Lib/site-packages",
    "apps/Python310/Lib/site-packages",
    "apps/Python311/Lib/site-packages",
    "apps/Python312/Lib/site-packages",
]

# QGIS app dirs — standalone uses "apps/qgis", LTR uses "apps/qgis-ltr"
_WIN_QGIS_APP_DIRS = [
    "apps/qgis",
    "apps/qgis-ltr",
]


def _find_qgis_prefix() -> str:
    """Return QGIS prefix from env var or first matching common location."""
    env = os.environ.get("QGIS_PREFIX_PATH", "").strip()
    if env and os.path.isdir(env):
        return env

    for p in _COMMON_PREFIXES:
        if not os.path.isdir(p):
            continue
        # Must have at least one known QGIS app subdir
        for app in _WIN_QGIS_APP_DIRS:
            if os.path.isdir(os.path.join(p, app)):
                return p
        # Linux layout check
        if os.path.isdir(os.path.join(p, "share", "qgis")):
            return p

    raise EnvironmentError(
        "QGIS 3.26.3 prefix not found.\n"
        "Set QGIS_PREFIX_PATH env var to your install directory.\n"
        "  Windows example:  set QGIS_PREFIX_PATH=C:\\Program Files\\QGIS 3.26.3\n"
        "  Linux   example:  export QGIS_PREFIX_PATH=/usr\n"
        "Or pass prefix_path= directly:  QGISExporter(prefix_path=r'C:\\...')"
    )


def _find_qgis_app_dir(prefix: str) -> str:
    """Return the 'apps/qgis' or 'apps/qgis-ltr' subdir that actually exists."""
    for app in _WIN_QGIS_APP_DIRS:
        full = os.path.join(prefix, app)
        if os.path.isdir(full):
            return full
    # Linux: qgis lives under share/qgis, not apps/qgis
    share = os.path.join(prefix, "share", "qgis")
    if os.path.isdir(share):
        return prefix   # use prefix itself as app_dir for Linux
    raise EnvironmentError(
        f"Cannot find QGIS app directory under '{prefix}'.\n"
        f"Expected one of: {_WIN_QGIS_APP_DIRS}"
    )


def _setup_qgis_paths(prefix: str) -> None:
    """
    Inject QGIS Python paths into sys.path and fix env vars.
    Must be called BEFORE any 'from qgis.core import ...' statements.
    """
    app_dir = _find_qgis_app_dir(prefix)

    # Python paths for QGIS 3.26 (Python 3.9)
    candidates = [
        os.path.join(app_dir, "python"),
        os.path.join(app_dir, "python", "plugins"),
    ]
    for py_sub in _WIN_PYTHON_DIRS:
        candidates.append(os.path.join(prefix, py_sub))

    # Linux paths
    candidates += [
        os.path.join(prefix, "share", "qgis", "python"),
        os.path.join(prefix, "share", "qgis", "python", "plugins"),
        "/usr/lib/python3/dist-packages",
        "/usr/lib/python3.9/dist-packages",
        "/usr/lib/python3.10/dist-packages",
        "/usr/lib/python3.11/dist-packages",
    ]

    for path in candidates:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)

    # Windows: QGIS bin must be first in PATH so Qt5 / GDAL DLLs resolve
    for bin_sub in ["bin", "apps/qgis/bin", "apps/qt5/bin", "apps/Python39"]:
        bin_dir = os.path.join(prefix, bin_sub)
        if os.path.isdir(bin_dir):
            current_path = os.environ.get("PATH", "")
            if bin_dir not in current_path:
                os.environ["PATH"] = bin_dir + os.pathsep + current_path

    # QGIS_PREFIX_PATH — used internally by QgsApplication
    os.environ["QGIS_PREFIX_PATH"] = prefix

    # GDAL / PROJ data dirs — required for CRS definitions
    for gdal_sub in ["share/gdal", "apps/gdal-dev/share/gdal"]:
        gdal_data = os.path.join(prefix, gdal_sub)
        if os.path.isdir(gdal_data):
            os.environ.setdefault("GDAL_DATA", gdal_data)
            break

    for proj_sub in ["share/proj", "apps/proj/share/proj"]:
        proj_lib = os.path.join(prefix, proj_sub)
        if os.path.isdir(proj_lib):
            os.environ.setdefault("PROJ_LIB", proj_lib)
            break


# ── KML driver selection ──────────────────────────────────────────────────────
# GDAL ships two KML drivers: "KML" (simple) and "LIBKML" (full spec).
# LIBKML is preferred — richer output, proper styling, folder support.
# QGIS 3.26 bundles GDAL >= 3.5 which has both; try LIBKML first.

def _kml_driver() -> str:
    """Return 'LIBKML' if available in GDAL, else 'KML'."""
    try:
        from osgeo import ogr  # noqa: PLC0415
        if ogr.GetDriverByName("LIBKML") is not None:
            return "LIBKML"
    except ImportError:
        pass
    return "KML"


# ── QGISExporter ─────────────────────────────────────────────────────────────

class QGISExporter:
    """
    Lifecycle wrapper for QgsApplication + polygon export.

    Parameters
    ----------
    standalone : bool
        True  = running outside QGIS (sam_pipeline, screenshot_pipeline).
                 QgsApplication will be initialised here.
        False = running inside QGIS console/plugin — skip init.
    prefix_path : str | None
        QGIS install prefix.  Auto-detected when None.
    """

    # Class-level singleton — QgsApplication must be created once per process.
    _qgs_app = None
    _init_count: int = 0

    def __init__(
        self,
        standalone:  bool = True,
        prefix_path: Optional[str] = None,
    ) -> None:
        self._standalone = standalone

        if standalone and QGISExporter._qgs_app is None:
            prefix = prefix_path or _find_qgis_prefix()
            _setup_qgis_paths(prefix)

            # Import AFTER _setup_qgis_paths adds QGIS dirs to sys.path
            from qgis.core import QgsApplication  # noqa: PLC0415

            # QgsApplication(sys.argv, GUIenabled=False)
            # Must pass sys.argv (not []) on Windows — avoids Qt platform crash.
            QGISExporter._qgs_app = QgsApplication([], False)
            QGISExporter._qgs_app.setPrefixPath(prefix, True)
            QGISExporter._qgs_app.initQgis()

        QGISExporter._init_count += 1

    # ── Public API ────────────────────────────────────────────────────────────

    def export_polygon(
        self,
        geojson_dict: dict,
        name:         str,
        output_dir:   str,
        formats:      Optional[list] = None,
    ) -> dict:
        """
        Convert a GeoJSON geometry dict to one or more file formats.

        Parameters
        ----------
        geojson_dict : dict
            Polygon or MultiPolygon geometry dict (coordinates in [lng, lat]).
            Also accepts Feature or FeatureCollection — geometry extracted auto.
        name : str
            Feature/layer name written into output files.
        output_dir : str
            Directory for output files (created if absent).
        formats : list[str]
            Any of "kml", "kmz", "geojson", "shp", "gpkg".
            Default ["kml", "kmz"].

        Returns
        -------
        dict  {"kml": path, "kmz": path, "status": "ok"|"error", "error": msg}
        """
        formats = [f.lower() for f in (formats or ["kml", "kmz"])]
        os.makedirs(output_dir, exist_ok=True)
        safe   = _safe_name(name)
        result = {"status": "ok", "error": ""}

        try:
            layer = self._geojson_to_layer(geojson_dict, name)
            result = self._export_layer(layer, formats, output_dir, safe, name)
        except Exception as exc:
            result["status"] = "error"
            result["error"]  = str(exc)

        return result

    def export_from_file(
        self,
        geojson_path: str,
        output_dir:   str,
        formats:      Optional[list] = None,
    ) -> dict:
        """
        Export from an existing GeoJSON file on disk.

        Parameters
        ----------
        geojson_path : str   Path to .geojson file.
        output_dir   : str   Output directory.
        formats      : list  Same as export_polygon.
        """
        formats = [f.lower() for f in (formats or ["kml", "kmz"])]
        os.makedirs(output_dir, exist_ok=True)
        result = {"status": "ok", "error": ""}

        try:
            from qgis.core import QgsVectorLayer  # noqa: PLC0415

            abs_path = os.path.abspath(geojson_path)
            layer    = QgsVectorLayer(abs_path, "boundary", "ogr")
            if not layer.isValid():
                raise RuntimeError(
                    f"QgsVectorLayer could not load '{abs_path}'\n"
                    "Check the file exists and is valid GeoJSON."
                )

            name = os.path.splitext(os.path.basename(geojson_path))[0]
            safe = _safe_name(name)
            result = self._export_layer(layer, formats, output_dir, safe, name)

        except Exception as exc:
            result["status"] = "error"
            result["error"]  = str(exc)

        return result

    def shutdown(self) -> None:
        """
        Release QgsApplication.
        Safe to call multiple times — only shuts down on last instance.
        """
        QGISExporter._init_count -= 1
        if (
            self._standalone
            and QGISExporter._qgs_app is not None
            and QGISExporter._init_count <= 0
        ):
            QGISExporter._qgs_app.exitQgis()
            QGISExporter._qgs_app = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.shutdown()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _geojson_to_layer(self, geojson_dict: dict, name: str):
        """
        Build in-memory QgsVectorLayer from GeoJSON geometry / Feature /
        FeatureCollection dict.

        Strategy:
          1. Convert geometry to WKT → QgsGeometry.fromWkt()
          2. If WKT fails (null geom), fall back to temp-file OGR load
        """
        from qgis.core import (  # noqa: PLC0415
            QgsFeature,
            QgsField,
            QgsFields,
            QgsGeometry,
            QgsVectorLayer,
        )
        from PyQt5.QtCore import QVariant  # noqa: PLC0415

        geom_dict = _extract_geometry(geojson_dict)
        geom_type = geom_dict.get("type", "")

        if geom_type not in ("Polygon", "MultiPolygon"):
            raise ValueError(
                f"Expected Polygon or MultiPolygon, got '{geom_type}'"
            )

        wkt   = _geojson_to_wkt(geom_dict)
        qgeom = QgsGeometry.fromWkt(wkt)

        if qgeom is None or qgeom.isNull():
            # Fallback: write to temp file, load via OGR provider
            return self._layer_from_temp_file(geojson_dict, name)

        # Memory provider URI: "Polygon?crs=EPSG:4326"
        vtype = "MultiPolygon" if geom_type == "MultiPolygon" else "Polygon"
        layer = QgsVectorLayer(f"{vtype}?crs=EPSG:4326", name, "memory")

        if not layer.isValid():
            raise RuntimeError("Failed to create in-memory QgsVectorLayer")

        pr = layer.dataProvider()

        # Add a "name" attribute field
        fields = QgsFields()
        fields.append(QgsField("name", QVariant.String))
        pr.addAttributes(fields)
        layer.updateFields()

        feat = QgsFeature()
        feat.setGeometry(qgeom)
        feat.setAttributes([name])
        pr.addFeatures([feat])
        layer.updateExtents()

        return layer

    def _layer_from_temp_file(self, geojson_dict: dict, name: str):
        """Fallback: serialise to temp GeoJSON file, load via OGR."""
        from qgis.core import QgsVectorLayer  # noqa: PLC0415

        geom_dict = _extract_geometry(geojson_dict)
        fc = {
            "type": "FeatureCollection",
            "features": [{
                "type":       "Feature",
                "properties": {"name": name},
                "geometry":   geom_dict,
            }],
        }

        # delete=False — file must stay alive while layer uses it
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".geojson", delete=False, encoding="utf-8"
        )
        json.dump(fc, tmp)
        tmp.close()
        self._tmp_path = tmp.name   # keep ref for optional cleanup

        layer = QgsVectorLayer(tmp.name, name, "ogr")
        if not layer.isValid():
            raise RuntimeError(
                f"OGR could not load temp GeoJSON: {tmp.name}"
            )
        return layer

    def _export_layer(
        self,
        layer,
        formats:      list,
        output_dir:   str,
        safe_name:    str,
        display_name: str,
    ) -> dict:
        """Write layer to all requested formats, return result dict."""
        result = {"status": "ok", "error": ""}

        # Write all non-KMZ formats first
        for fmt in formats:
            if fmt == "kmz":
                continue
            path = self._write_format(layer, fmt, output_dir, safe_name)
            result[fmt] = path

        # KMZ = zip the KML (produce KML if not already requested)
        if "kmz" in formats:
            kml_path = result.get("kml")
            if not kml_path:
                kml_path      = self._write_format(
                    layer, "kml", output_dir, safe_name
                )
                result["kml"] = kml_path
            result["kmz"] = _make_kmz(kml_path, output_dir, safe_name)

        return result

    def _write_format(
        self,
        layer,
        fmt:       str,
        output_dir: str,
        safe_name:  str,
    ) -> str:
        """
        Write layer to one format using QgsVectorFileWriter.writeAsVectorFormatV3.

        QGIS 3.26.3 API notes
        ----------------------
        writeAsVectorFormatV3(layer, fileName, transformContext, options)
          → returns (error_code, error_message, new_filename, new_layer_name)
          error_code: QgsVectorFileWriter.NoError == 0

        SaveVectorOptions attributes used here:
          .driverName       GDAL driver string
          .fileEncoding     "UTF-8"
          .datasourceOptions  list[str]  GDAL open options for the datasource
          .layerOptions       list[str]  GDAL layer creation options
        """
        from qgis.core import (  # noqa: PLC0415
            QgsCoordinateReferenceSystem,
            QgsProject,
            QgsVectorFileWriter,
        )

        # ── Format → GDAL driver + extension map ─────────────────────────────
        # KML uses LIBKML when available (GDAL >= 2.x bundled with QGIS 3.26).
        # "KML" driver still works but is legacy.
        kml_drv = _kml_driver()

        fmt_map = {
            "kml":     (kml_drv,          ".kml"),
            "geojson": ("GeoJSON",        ".geojson"),
            "shp":     ("ESRI Shapefile", ".shp"),
            "gpkg":    ("GPKG",           ".gpkg"),
        }

        if fmt not in fmt_map:
            raise ValueError(
                f"Unknown format '{fmt}'. Valid: {list(fmt_map.keys())}"
            )

        driver, ext = fmt_map[fmt]
        out_path    = os.path.join(output_dir, f"{safe_name}{ext}")

        # ── Build SaveVectorOptions ───────────────────────────────────────────
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName    = driver
        opts.fileEncoding  = "UTF-8"

        # KML layer options: NameField tells GDAL which attribute → <name> tag
        if fmt == "kml":
            opts.layerOptions = ["NameField=name"]

        # transformContext from QgsProject — correct for QGIS 3.26 standalone
        # (layer.transformContext() also works but may warn in some builds)
        transform_ctx = QgsProject.instance().transformContext()

        # writeAsVectorFormatV3 returns a 4-tuple in QGIS 3.20+
        error_code, error_msg, _, _ = (
            QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, out_path, transform_ctx, opts
            )
        )

        if error_code != QgsVectorFileWriter.NoError:
            raise RuntimeError(
                f"QgsVectorFileWriter failed [{fmt}]: {error_msg}"
            )

        return out_path


# ── Standalone helpers ────────────────────────────────────────────────────────

def _make_kmz(kml_path: str, output_dir: str, safe_name: str) -> str:
    """Zip a KML file into KMZ."""
    kmz_path = os.path.join(output_dir, f"{safe_name}.kmz")
    with zipfile.ZipFile(kmz_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(kml_path, arcname="doc.kml")
    return kmz_path


def _extract_geometry(obj: dict) -> dict:
    """
    Extract geometry dict from:
      - raw Polygon/MultiPolygon dict
      - GeoJSON Feature dict
      - GeoJSON FeatureCollection dict (takes first feature)
    """
    t = obj.get("type", "")
    if t == "Feature":
        return obj["geometry"]
    if t == "FeatureCollection":
        feats = obj.get("features", [])
        if not feats:
            raise ValueError("FeatureCollection contains no features")
        return feats[0]["geometry"]
    # Already a geometry dict
    return obj


def _geojson_to_wkt(geom: dict) -> str:
    """
    GeoJSON geometry dict → WKT string.
    GeoJSON coordinates: [lng, lat]  →  WKT: (lng lat)  — same order, correct.
    """
    t = geom.get("type")

    def ring_wkt(ring: list) -> str:
        return "(" + ", ".join(f"{c[0]} {c[1]}" for c in ring) + ")"

    if t == "Polygon":
        return "POLYGON(" + ", ".join(ring_wkt(r) for r in geom["coordinates"]) + ")"

    if t == "MultiPolygon":
        poly_parts = []
        for rings in geom["coordinates"]:
            poly_parts.append(
                "(" + ", ".join(ring_wkt(r) for r in rings) + ")"
            )
        return "MULTIPOLYGON(" + ", ".join(poly_parts) + ")"

    raise ValueError(f"Cannot convert '{t}' to WKT")


def _safe_name(name: str) -> str:
    """Filesystem-safe basename from a place name."""
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)
    return safe.strip().replace(" ", "_")[:60] or "boundary"