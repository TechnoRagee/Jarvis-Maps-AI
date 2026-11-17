"""
db_handler.py
-------------
SQLite database handler for Jarvis Maps.
Optimized for large datasets (100GB+):
  - WAL mode for fast concurrent writes
  - Indexes on all searchable columns
  - Paginated queries (never loads all records at once)
  - Bulk insert for speed
  - Auto VACUUM and ANALYZE
"""

import os
import sqlite3
import threading
from datetime import datetime
import hashlib
import secrets


def get_user_data_dir() -> str:
    """
    Returns the persistent user data directory: %LOCALAPPDATA%/JarvisMapsAI.
    Ensures user database, history, cache, logs, and settings remain untouched by application updates.
    """
    app_dir = os.path.join(os.getenv("LOCALAPPDATA", os.path.expanduser("~")), "JarvisMapsAI")
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


def _resolve_db_file() -> str:
    env_file = os.environ.get("JARVIS_DB_FILE")
    if env_file:
        return env_file
    import sys
    if getattr(sys, "frozen", False):
        # In packaged/installed release: always store database in %LOCALAPPDATA%/JarvisMapsAI/
        # so future application installer updates never overwrite or delete user data.
        return os.path.join(get_user_data_dir(), "jarvis_maps.db")
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, "jarvis_maps.db")


DB_FILE      = _resolve_db_file()
PAGE_SIZE    = 500    # Records per page in UI
CACHE_SIZE   = 10000  # SQLite page cache (higher = faster queries)


# ─────────────────────────────────────────────
#  CONNECTION
# ─────────────────────────────────────────────

_thread_local = threading.local()


def get_connection() -> sqlite3.Connection:
    """
    Returns a per-thread cached SQLite connection.
    PRAGMAs are set once when the connection is first created.
    """
    conn = getattr(_thread_local, "connection", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            # Connection is broken, recreate it
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row  # Access columns by name

    # Performance settings — set once per thread connection
    conn.execute("PRAGMA journal_mode=WAL")         # Write-Ahead Logging — faster writes
    conn.execute("PRAGMA synchronous=NORMAL")        # Balanced safety/speed
    conn.execute(f"PRAGMA cache_size={CACHE_SIZE}")  # Larger cache = faster reads
    conn.execute("PRAGMA temp_store=MEMORY")         # Temp tables in RAM
    conn.execute("PRAGMA mmap_size=2147483648")      # 2GB memory-mapped I/O

    _thread_local.connection = conn
    return conn


# ─────────────────────────────────────────────
#  INITIALIZE DATABASE
# ─────────────────────────────────────────────

def init_db() -> None:
    """
    Creates tables and indexes for all tabs if not already present.
    Safe to call multiple times.
    """
    with get_connection() as conn:

        # ── Tab 1: Coordinates Extractor results ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coordinate_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                place       TEXT NOT NULL,
                latitude    REAL,
                longitude   REAL,
                address     TEXT,
                status      TEXT,
                timestamp   TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Tab 2: Boundary Tracer results ───────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS boundary_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                query           TEXT NOT NULL,
                display_name    TEXT,
                status          TEXT,
                source          TEXT,
                polygon_points  INTEGER DEFAULT 0,
                center_lat      REAL,
                center_lng      REAL,
                geojson_path    TEXT,
                kml_path        TEXT,
                kmz_path        TEXT,
                geotiff_path    TEXT,
                error           TEXT,
                timestamp       TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Tab 3: Amenity Finder results ────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amenity_results (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        TEXT,
                name          TEXT NOT NULL,
                amenity       TEXT NOT NULL,
                area          TEXT NOT NULL,
                latitude      REAL,
                longitude     REAL,
                address       TEXT,
                rating        TEXT,
                phone         TEXT,
                opening_hours TEXT,
                timestamp     TEXT DEFAULT (datetime('now'))
            )
        """)

        # Migration: Ensure run_id column exists for existing databases
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(amenity_results)")
        existing_cols = {row["name"] for row in cursor.fetchall()}
        if "run_id" not in existing_cols:
            conn.execute("ALTER TABLE amenity_results ADD COLUMN run_id TEXT")

        # ── Tab 4: Bulk Runner jobs ──────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bulk_runner_jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      TEXT,
                amenity     TEXT NOT NULL,
                area        TEXT NOT NULL,
                district    TEXT,
                level       TEXT DEFAULT 'City',
                job_limit   INTEGER DEFAULT 100,
                status      TEXT DEFAULT 'Completed',
                total_found INTEGER DEFAULT 0,
                excel_path  TEXT,
                kml_path    TEXT,
                error       TEXT,
                timestamp   TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Tab 5: Area Explorer results ─────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS area_explorer_results (
                osm_id                  TEXT PRIMARY KEY,
                name                    TEXT,
                name_en                 TEXT,
                name_native             TEXT,
                vernacular_json         TEXT,
                settlement_type         TEXT,
                display_type            TEXT,
                parent_area             TEXT,
                country                 TEXT,
                state                   TEXT,
                state_code              TEXT,
                district                TEXT,
                subdistrict             TEXT,
                tehsil                  TEXT,
                city                    TEXT,
                town                    TEXT,
                village                 TEXT,
                postal_code             TEXT,
                latitude                REAL,
                longitude               REAL,
                discovery_status        TEXT,
                polygon_status          TEXT,
                geometry_type           TEXT,
                polygon_points          INTEGER,
                geojson                 TEXT,
                tile_id                 TEXT,
                source                  TEXT,
                maps_url                TEXT,
                created_at              REAL,
                polygon_match_confidence TEXT,
                state_membership        TEXT,
                hierarchy_confidence    TEXT
            )
        """)

        # ── Tab 6: Boundary Validator results ────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS validated_boundaries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                area_name       TEXT NOT NULL,
                parent_area     TEXT,
                area_type       TEXT,
                boundary_status TEXT DEFAULT 'Unverified',
                boundary_points INTEGER DEFAULT 0,
                boundary_source TEXT,
                latitude        REAL,
                longitude       REAL,
                query           TEXT,
                geojson_path    TEXT,
                kml_path        TEXT,
                kmz_path        TEXT,
                geotiff_path    TEXT,
                maps_url        TEXT,
                timestamp       TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Tab 7 helper: Harvest Checkpoints ────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS area_harvest_checkpoints (
                tile_id         TEXT PRIMARY KEY,
                parent_tile_id  TEXT,
                region_name     TEXT,
                bbox_json       TEXT,
                status          TEXT DEFAULT 'PENDING',
                items_count     INTEGER DEFAULT 0,
                depth           INTEGER DEFAULT 0,
                created_at      REAL,
                updated_at      REAL
            )
        """)

        # ── App settings & authentication ────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key         TEXT PRIMARY KEY,
                value       TEXT,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Indexes for fast queries across all tables ──
        indexes = [
            # Coordinate indexes
            "CREATE INDEX IF NOT EXISTS idx_coord_place ON coordinate_results(place)",
            "CREATE INDEX IF NOT EXISTS idx_coord_status ON coordinate_results(status)",
            "CREATE INDEX IF NOT EXISTS idx_coord_timestamp ON coordinate_results(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_coord_latlon ON coordinate_results(latitude, longitude)",

            # Boundary indexes
            "CREATE INDEX IF NOT EXISTS idx_bnd_query ON boundary_results(query)",
            "CREATE INDEX IF NOT EXISTS idx_bnd_status ON boundary_results(status)",
            "CREATE INDEX IF NOT EXISTS idx_bnd_timestamp ON boundary_results(timestamp)",

            # Amenity indexes
            "CREATE INDEX IF NOT EXISTS idx_amenity_name ON amenity_results(name)",
            "CREATE INDEX IF NOT EXISTS idx_amenity_type ON amenity_results(amenity)",
            "CREATE INDEX IF NOT EXISTS idx_amenity_area ON amenity_results(area)",
            "CREATE INDEX IF NOT EXISTS idx_amenity_run_id ON amenity_results(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_amenity_timestamp ON amenity_results(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_amenity_latlon ON amenity_results(latitude, longitude)",
            "CREATE INDEX IF NOT EXISTS idx_amenity_type_area ON amenity_results(amenity, area)",

            # Bulk Runner indexes
            "CREATE INDEX IF NOT EXISTS idx_bulk_amenity ON bulk_runner_jobs(amenity)",
            "CREATE INDEX IF NOT EXISTS idx_bulk_area ON bulk_runner_jobs(area)",
            "CREATE INDEX IF NOT EXISTS idx_bulk_status ON bulk_runner_jobs(status)",
            "CREATE INDEX IF NOT EXISTS idx_bulk_timestamp ON bulk_runner_jobs(timestamp)",

            # Area Explorer indexes
            "CREATE INDEX IF NOT EXISTS idx_area_name ON area_explorer_results(name)",
            "CREATE INDEX IF NOT EXISTS idx_area_parent ON area_explorer_results(parent_area)",
            "CREATE INDEX IF NOT EXISTS idx_area_state ON area_explorer_results(state)",
            "CREATE INDEX IF NOT EXISTS idx_area_district ON area_explorer_results(district)",
            "CREATE INDEX IF NOT EXISTS idx_area_type ON area_explorer_results(settlement_type)",
            "CREATE INDEX IF NOT EXISTS idx_area_poly_status ON area_explorer_results(polygon_status)",

            # Boundary Validator indexes
            "CREATE INDEX IF NOT EXISTS idx_val_name ON validated_boundaries(area_name)",
            "CREATE INDEX IF NOT EXISTS idx_val_status ON validated_boundaries(boundary_status)",
            "CREATE INDEX IF NOT EXISTS idx_val_timestamp ON validated_boundaries(timestamp)",

            # Checkpoint indexes
            "CREATE INDEX IF NOT EXISTS idx_checkpoint_status ON area_harvest_checkpoints(status)",
            "CREATE INDEX IF NOT EXISTS idx_checkpoint_region ON area_harvest_checkpoints(region_name)",
        ]
        for idx in indexes:
            conn.execute(idx)

        conn.commit()

    # Index any existing boundary files on startup if boundary_results is fresh
    try:
        sync_existing_boundaries_to_db("boundaries")
    except Exception:
        pass

    print(f"[OK] Database initialized with all tabs: {DB_FILE}")
    print(f"   Storage: {get_db_size()}")


# ─────────────────────────────────────────────
#  SAVE FUNCTIONS (Bulk insert for speed)
# ─────────────────────────────────────────────

def save_coordinate_results(results: list) -> int:
    """Bulk inserts coordinate results. Returns number of records inserted."""
    if not results:
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    for r in results:
        place  = r.get("place") or r.get("Place") or ""
        lat    = _safe_float(r.get("latitude") or r.get("Latitude"))
        lng    = _safe_float(r.get("longitude") or r.get("Longitude"))
        addr   = str(r.get("address") or r.get("Address") or "")
        status = str(r.get("status") or r.get("Status") or "")
        ts     = str(r.get("timestamp") or r.get("Timestamp") or now)

        if place:
            rows.append((place, lat, lng, addr, status, ts))

    if not rows:
        return 0

    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO coordinate_results
                (place, latitude, longitude, address, status, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()

    return len(rows)


def _check_valid_name(name: str) -> bool:
    try:
        from utils.amenity_finder import _is_valid_place_name
        return _is_valid_place_name(name)
    except Exception:
        if not name or not isinstance(name, str):
            return False
        clean = name.strip()
        if len(clean) < 3 or not any(c.isalpha() for c in clean):
            return False
        if clean[0] in "₹$€£¥":
            return False
        return True


def save_amenity_results(results: list, run_id: str = None) -> int:
    """Bulk inserts amenity results with data hygiene filtering and run_id tagging."""
    if not results:
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    for r in results:
        rec_run_id = r.get("run_id") or run_id or None
        name   = str(r.get("name") or r.get("Name") or "").strip()
        amenity= r.get("amenity") or r.get("Amenity") or ""
        area   = r.get("area") or r.get("Area") or ""
        lat    = _safe_float(r.get("latitude") or r.get("Latitude"))
        lng    = _safe_float(r.get("longitude") or r.get("Longitude"))
        addr   = str(r.get("address") or r.get("Address") or "")
        rating = str(r.get("rating") or r.get("Rating") or "")
        phone  = str(r.get("phone") or r.get("Phone") or "")
        hours  = str(r.get("opening_hours") or r.get("Opening Hours") or "")
        ts     = str(r.get("timestamp") or r.get("Timestamp") or now)

        if name and _check_valid_name(name) and lat is not None and lng is not None:
            rows.append((rec_run_id, name, amenity, area, lat, lng, addr, rating, phone, hours, ts))

    if not rows:
        return 0

    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO amenity_results
                (run_id, name, amenity, area, latitude, longitude,
                 address, rating, phone, opening_hours, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()

    return len(rows)


def save_boundary_result(result: dict) -> int:
    """Inserts a single boundary trace result into boundary_results table."""
    if not result or not isinstance(result, dict):
        return 0

    query = result.get("query") or result.get("display_name") or ""
    if not query:
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO boundary_results
                (query, display_name, status, source, polygon_points,
                 center_lat, center_lng, geojson_path, kml_path, kmz_path,
                 geotiff_path, error, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(query),
            str(result.get("display_name") or query),
            str(result.get("status") or ("Success" if result.get("geojson_path") or result.get("kml_path") else "Failed")),
            str(result.get("source") or "Google Maps"),
            int(result.get("polygon_points") or 0),
            _safe_float(result.get("center_lat")),
            _safe_float(result.get("center_lng")),
            str(result.get("geojson_path") or ""),
            str(result.get("kml_path") or ""),
            str(result.get("kmz_path") or ""),
            str(result.get("geotiff_path") or ""),
            str(result.get("error") or ""),
            str(result.get("timestamp") or now),
        ))
        conn.commit()
    return 1


def save_bulk_runner_job(job: dict) -> int:
    """Inserts a bulk runner job into bulk_runner_jobs table."""
    if not job or not isinstance(job, dict):
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO bulk_runner_jobs
                (job_id, amenity, area, district, level, job_limit, status,
                 total_found, excel_path, kml_path, error, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(job.get("id") or job.get("job_id") or ""),
            str(job.get("amenity") or ""),
            str(job.get("area") or ""),
            str(job.get("district") or ""),
            str(job.get("level") or "City"),
            int(job.get("limit") or job.get("job_limit") or 100),
            str(job.get("status") or "Completed"),
            int(job.get("total_found") or job.get("collected") or job.get("count") or 0),
            str(job.get("excel_path") or job.get("output_file") or ""),
            str(job.get("kml_path") or ""),
            str(job.get("error") or ""),
            str(job.get("timestamp") or now),
        ))
        conn.commit()
    return 1


def save_validated_boundaries(records: list) -> int:
    """Bulk inserts validated area boundary records into validated_boundaries table."""
    if not records:
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in records:
        b_info = r.get("boundary_info") or {}
        has_bnd = r.get("has_boundary")
        bnd_status = "Confirmed" if has_bnd is True else ("Not Found" if has_bnd is False else "Unverified")
        rows.append((
            str(r.get("name") or ""),
            str(r.get("parent_area") or ""),
            str(r.get("type") or "Area"),
            bnd_status,
            int(b_info.get("polygon_points") or r.get("polygon_points") or 0),
            str(b_info.get("source") or r.get("source") or ""),
            _safe_float(b_info.get("center_lat") or r.get("latitude")),
            _safe_float(b_info.get("center_lng") or r.get("longitude")),
            str(r.get("query") or ""),
            str(b_info.get("geojson_path") or ""),
            str(b_info.get("kml_path") or ""),
            str(b_info.get("kmz_path") or ""),
            str(b_info.get("geotiff_path") or ""),
            str(r.get("google_maps_url") or ""),
            now,
        ))

    if not rows:
        return 0

    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO validated_boundaries
                (area_name, parent_area, area_type, boundary_status, boundary_points,
                 boundary_source, latitude, longitude, query, geojson_path,
                 kml_path, kmz_path, geotiff_path, maps_url, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
    return len(rows)


def sync_existing_boundaries_to_db(boundaries_dir: str = "boundaries") -> int:
    """Scans boundaries directory and registers any existing files into boundary_results table."""
    if not os.path.isdir(boundaries_dir):
        return 0

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT query FROM boundary_results")
        existing_queries = {row[0].strip().casefold() for row in cursor.fetchall() if row[0]}

        try:
            files = os.listdir(boundaries_dir)
        except Exception:
            return 0

        stems = {}
        for f in files:
            p = os.path.join(boundaries_dir, f)
            if not os.path.isfile(p):
                continue
            base, ext = os.path.splitext(f)
            ext = ext.lower()
            if base not in stems:
                stems[base] = {}
            if ext in ('.tif', '.tiff'):
                stems[base]['geotiff'] = p
            elif ext == '.geojson':
                stems[base]['geojson'] = p
            elif ext == '.kml':
                stems[base]['kml'] = p
            elif ext == '.kmz':
                stems[base]['kmz'] = p

        rows = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for stem, file_dict in stems.items():
            if not any(file_dict.values()):
                continue
            clean_query = stem.replace("__", ", ").replace("_", " ")
            if clean_query.casefold() in existing_queries:
                continue

            rows.append((
                clean_query,
                clean_query,
                "Success",
                "Google Maps / Local File",
                0,
                None,
                None,
                file_dict.get('geojson', ''),
                file_dict.get('kml', ''),
                file_dict.get('kmz', ''),
                file_dict.get('geotiff', ''),
                "",
                now,
            ))
            existing_queries.add(clean_query.casefold())

        if rows:
            cursor.executemany("""
                INSERT INTO boundary_results
                    (query, display_name, status, source, polygon_points,
                     center_lat, center_lng, geojson_path, kml_path, kmz_path,
                     geotiff_path, error, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            return len(rows)
    return 0


# ─────────────────────────────────────────────
#  QUERY FUNCTIONS FOR ALL TABS (Paginated)
# ─────────────────────────────────────────────

def query_coordinates(search_term: str = None, page: int = 1, page_size: int = PAGE_SIZE) -> list:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(place LIKE ? OR address LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term])

    where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * page_size
    params.extend([page_size, offset])

    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT id, place, latitude, longitude, address, status, timestamp
            FROM coordinate_results
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
    return [dict(r) for r in rows]


def count_coordinates(search_term: str = None) -> int:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(place LIKE ? OR address LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM coordinate_results {where}", params).fetchone()[0]


def query_boundaries(search_term: str = None, page: int = 1, page_size: int = PAGE_SIZE) -> list:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(query LIKE ? OR display_name LIKE ? OR source LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term])

    where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * page_size
    params.extend([page_size, offset])

    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT id, query, display_name, status, source, polygon_points,
                   center_lat, center_lng, geojson_path, kml_path, kmz_path,
                   geotiff_path, error, timestamp
            FROM boundary_results
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
    return [dict(r) for r in rows]


def count_boundaries(search_term: str = None) -> int:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(query LIKE ? OR display_name LIKE ? OR source LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM boundary_results {where}", params).fetchone()[0]


def query_amenities(search_term: str = None,
                    amenity_type: str = None,
                    area_name: str = None,
                    run_id: str = None,
                    page: int = 1,
                    page_size: int = PAGE_SIZE) -> list:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(name LIKE ? OR address LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term])
    if amenity_type:
        conditions.append("amenity = ?")
        params.append(amenity_type)
    if area_name:
        conditions.append("area LIKE ?")
        params.append(f"%{area_name}%")
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)

    where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * page_size
    params.extend([page_size, offset])

    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT id, run_id, name, amenity, area, latitude, longitude,
                   address, rating, phone, opening_hours, timestamp
            FROM amenity_results
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
    return [dict(r) for r in rows]


def count_amenities(search_term: str = None,
                    amenity_type: str = None,
                    area_name: str = None,
                    run_id: str = None) -> int:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(name LIKE ? OR address LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term])
    if amenity_type:
        conditions.append("amenity = ?")
        params.append(amenity_type)
    if area_name:
        conditions.append("area LIKE ?")
        params.append(f"%{area_name}%")
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM amenity_results {where}", params).fetchone()[0]


def query_bulk_jobs(search_term: str = None, page: int = 1, page_size: int = PAGE_SIZE) -> list:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(amenity LIKE ? OR area LIKE ? OR district LIKE ? OR job_id LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term, term])

    where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * page_size
    params.extend([page_size, offset])

    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT id, job_id, amenity, area, district, level, job_limit,
                   status, total_found, excel_path, kml_path, error, timestamp
            FROM bulk_runner_jobs
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
    return [dict(r) for r in rows]


def count_bulk_jobs(search_term: str = None) -> int:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(amenity LIKE ? OR area LIKE ? OR district LIKE ? OR job_id LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term, term])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM bulk_runner_jobs {where}", params).fetchone()[0]


def query_area_explorer(search_term: str = None, page: int = 1, page_size: int = PAGE_SIZE) -> list:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(name LIKE ? OR parent_area LIKE ? OR state LIKE ? OR district LIKE ? OR settlement_type LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term, term, term])

    where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * page_size
    params.extend([page_size, offset])

    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT osm_id AS id, osm_id, name, name_en, name_native, settlement_type,
                   display_type, parent_area, state, district, subdistrict, tehsil,
                   city, postal_code, latitude, longitude, discovery_status,
                   polygon_status, geometry_type, polygon_points, source, maps_url,
                   created_at
            FROM area_explorer_results
            {where}
            ORDER BY rowid DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
    return [dict(r) for r in rows]


def count_area_explorer(search_term: str = None) -> int:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(name LIKE ? OR parent_area LIKE ? OR state LIKE ? OR district LIKE ? OR settlement_type LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term, term, term])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM area_explorer_results {where}", params).fetchone()[0]


def query_validated_boundaries(search_term: str = None, page: int = 1, page_size: int = PAGE_SIZE) -> list:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(area_name LIKE ? OR parent_area LIKE ? OR query LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term])

    where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * page_size
    params.extend([page_size, offset])

    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT id, area_name, parent_area, area_type, boundary_status,
                   boundary_points, boundary_source, latitude, longitude,
                   query, geojson_path, kml_path, kmz_path, geotiff_path,
                   maps_url, timestamp
            FROM validated_boundaries
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()
    return [dict(r) for r in rows]


def count_validated_boundaries(search_term: str = None) -> int:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(area_name LIKE ? OR parent_area LIKE ? OR query LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM validated_boundaries {where}", params).fetchone()[0]


def query_checkpoints(search_term: str = None, page: int = 1, page_size: int = PAGE_SIZE) -> list:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(tile_id LIKE ? OR region_name LIKE ? OR status LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term])

    where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * page_size
    params.extend([page_size, offset])

    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT tile_id AS id, tile_id, parent_tile_id, region_name,
                   status, items_count, depth, created_at, updated_at
            FROM area_harvest_checkpoints
            {where}
            ORDER BY rowid DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        ca = d.get("created_at")
        ua = d.get("updated_at")
        d["created_at_str"] = datetime.fromtimestamp(ca).strftime("%Y-%m-%d %H:%M:%S") if ca else ""
        d["updated_at_str"] = datetime.fromtimestamp(ua).strftime("%Y-%m-%d %H:%M:%S") if ua else ""
        result.append(d)
    return result


def count_checkpoints(search_term: str = None) -> int:
    conditions, params = [], []
    if search_term and search_term.strip():
        conditions.append("(tile_id LIKE ? OR region_name LIKE ? OR status LIKE ?)")
        term = f"%{search_term.strip()}%"
        params.extend([term, term, term])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM area_harvest_checkpoints {where}", params).fetchone()[0]


# ─────────────────────────────────────────────
#  TAB / TABLE METADATA MAP FOR UI INTEGRATION
# ─────────────────────────────────────────────

TAB_TABLE_CONFIG = {
    "Coordinates Extractor": {
        "table": "coordinate_results",
        "id_col": "id",
        "query_fn": query_coordinates,
        "count_fn": count_coordinates,
        "columns": ("#", "Place", "Latitude", "Longitude", "Address", "Status", "Timestamp"),
        "widths": {"#": 40, "Place": 200, "Latitude": 95, "Longitude": 95, "Address": 240, "Status": 90, "Timestamp": 130},
        "row_builder": lambda i, r: (i, r.get("place", ""), r.get("latitude", ""), r.get("longitude", ""), r.get("address", ""), r.get("status", ""), r.get("timestamp", "")),
    },
    "Boundary Tracer": {
        "table": "boundary_results",
        "id_col": "id",
        "query_fn": query_boundaries,
        "count_fn": count_boundaries,
        "columns": ("#", "Query", "Display Name", "Status", "Points", "Latitude", "Longitude", "Source", "GeoJSON", "GeoTIFF", "Timestamp"),
        "widths": {"#": 40, "Query": 180, "Display Name": 200, "Status": 80, "Points": 60, "Latitude": 85, "Longitude": 85, "Source": 80, "GeoJSON": 120, "GeoTIFF": 120, "Timestamp": 120},
        "row_builder": lambda i, r: (i, r.get("query", ""), r.get("display_name", ""), r.get("status", ""), r.get("polygon_points", 0), r.get("center_lat", "") or "-", r.get("center_lng", "") or "-", r.get("source", ""), os.path.basename(r.get("geojson_path") or "") or "-", os.path.basename(r.get("geotiff_path") or "") or "-", r.get("timestamp", "")),
    },
    "Amenity Finder": {
        "table": "amenity_results",
        "id_col": "id",
        "query_fn": query_amenities,
        "count_fn": count_amenities,
        "columns": ("#", "Name", "Amenity", "Area", "Latitude", "Longitude", "Address", "Rating", "Phone", "Opening Hours", "Timestamp"),
        "widths": {"#": 40, "Name": 160, "Amenity": 90, "Area": 90, "Latitude": 80, "Longitude": 80, "Address": 200, "Rating": 50, "Phone": 100, "Opening Hours": 100, "Timestamp": 120},
        "row_builder": lambda i, r: (i, r.get("name", ""), r.get("amenity", ""), r.get("area", ""), r.get("latitude", ""), r.get("longitude", ""), r.get("address", ""), r.get("rating", ""), r.get("phone", ""), r.get("opening_hours", ""), r.get("timestamp", "")),
    },
    "Bulk Runner": {
        "table": "bulk_runner_jobs",
        "id_col": "id",
        "query_fn": query_bulk_jobs,
        "count_fn": count_bulk_jobs,
        "columns": ("#", "Job ID", "Amenity", "Area", "District", "Level", "Limit", "Status", "Found", "Excel File", "Timestamp"),
        "widths": {"#": 40, "Job ID": 110, "Amenity": 100, "Area": 110, "District": 90, "Level": 70, "Limit": 60, "Status": 90, "Found": 60, "Excel File": 160, "Timestamp": 120},
        "row_builder": lambda i, r: (i, r.get("job_id", "") or "-", r.get("amenity", ""), r.get("area", ""), r.get("district", "") or "-", r.get("level", ""), r.get("job_limit", ""), r.get("status", ""), r.get("total_found", 0), os.path.basename(r.get("excel_path") or "") or "-", r.get("timestamp", "")),
    },
    "Area Explorer": {
        "table": "area_explorer_results",
        "id_col": "osm_id",
        "query_fn": query_area_explorer,
        "count_fn": count_area_explorer,
        "columns": ("#", "Name", "Parent Area", "Type", "State", "District", "Polygon", "Discovery", "Points", "Latitude", "Longitude", "Source"),
        "widths": {"#": 40, "Name": 170, "Parent Area": 120, "Type": 90, "State": 100, "District": 100, "Polygon": 80, "Discovery": 80, "Points": 60, "Latitude": 85, "Longitude": 85, "Source": 90},
        "row_builder": lambda i, r: (i, r.get("name", ""), r.get("parent_area", "") or "-", r.get("settlement_type", "") or r.get("display_type", "") or "Area", r.get("state", ""), r.get("district", "") or "-", r.get("polygon_status", ""), r.get("discovery_status", ""), r.get("polygon_points", 0), r.get("latitude", "") or "-", r.get("longitude", "") or "-", r.get("source", "")),
    },
    "Boundary Validator": {
        "table": "validated_boundaries",
        "id_col": "id",
        "query_fn": query_validated_boundaries,
        "count_fn": count_validated_boundaries,
        "columns": ("#", "Area Name", "Parent Area", "Type", "Boundary", "Points", "Source", "Latitude", "Longitude", "Query", "Timestamp"),
        "widths": {"#": 40, "Area Name": 160, "Parent Area": 120, "Type": 80, "Boundary": 90, "Points": 60, "Source": 80, "Latitude": 85, "Longitude": 85, "Query": 160, "Timestamp": 120},
        "row_builder": lambda i, r: (i, r.get("area_name", ""), r.get("parent_area", "") or "-", r.get("area_type", "") or "Area", r.get("boundary_status", ""), r.get("boundary_points", 0), r.get("boundary_source", "") or "-", r.get("latitude", "") or "-", r.get("longitude", "") or "-", r.get("query", ""), r.get("timestamp", "")),
    },
    "Harvest Checkpoints": {
        "table": "area_harvest_checkpoints",
        "id_col": "tile_id",
        "query_fn": query_checkpoints,
        "count_fn": count_checkpoints,
        "columns": ("#", "Tile ID", "Region Name", "Status", "Items Count", "Depth", "Parent Tile", "Created At", "Updated At"),
        "widths": {"#": 40, "Tile ID": 130, "Region Name": 180, "Status": 90, "Items Count": 90, "Depth": 60, "Parent Tile": 100, "Created At": 130, "Updated At": 130},
        "row_builder": lambda i, r: (i, r.get("tile_id", ""), r.get("region_name", ""), r.get("status", ""), r.get("items_count", 0), r.get("depth", 0), r.get("parent_tile_id", "") or "-", r.get("created_at_str", "") or "-", r.get("updated_at_str", "") or "-"),
    },
}

VALID_TABLES = tuple(c["table"] for c in TAB_TABLE_CONFIG.values())


# ─────────────────────────────────────────────
#  DELETE / CLEAR
# ─────────────────────────────────────────────

def delete_records(table_name: str, ids: list, id_col: str = None) -> None:
    """Deletes specific records by ID from any table."""
    if not ids or table_name not in VALID_TABLES:
        return

    if id_col is None:
        if table_name == "area_explorer_results":
            id_col = "osm_id"
        elif table_name == "area_harvest_checkpoints":
            id_col = "tile_id"
        else:
            id_col = "id"

    placeholders = ",".join("?" for _ in ids)
    with get_connection() as conn:
        conn.execute(
            f"DELETE FROM {table_name} WHERE {id_col} IN ({placeholders})", ids
        )
        conn.commit()


def clear_table(table_name: str) -> None:
    """Clears ALL records from any valid table."""
    if table_name not in VALID_TABLES:
        return

    with get_connection() as conn:
        conn.execute(f"DELETE FROM {table_name}")
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table_name,))
        except Exception:
            pass
        conn.commit()

    optimize_db()


# ─────────────────────────────────────────────
#  DATABASE STATS & MAINTENANCE
# ─────────────────────────────────────────────

def get_db_size() -> str:
    """Returns human-readable database file size."""
    if not os.path.exists(DB_FILE):
        return "0 MB"
    size = os.path.getsize(DB_FILE)
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 ** 3:
        return f"{size / (1024**2):.1f} MB"
    else:
        return f"{size / (1024**3):.2f} GB"


def get_db_stats() -> dict:
    """Returns database statistics for all tabs/tables."""
    stats = {
        "coordinates": 0,
        "boundaries": 0,
        "amenities": 0,
        "bulk_jobs": 0,
        "areas": 0,
        "validated": 0,
        "checkpoints": 0,
        "total": 0,
        "size": get_db_size(),
        "file": DB_FILE,
    }

    with get_connection() as conn:
        def _get_cnt(tbl):
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:
                return 0

        stats["coordinates"] = _get_cnt("coordinate_results")
        stats["boundaries"]  = _get_cnt("boundary_results")
        stats["amenities"]   = _get_cnt("amenity_results")
        stats["bulk_jobs"]   = _get_cnt("bulk_runner_jobs")
        stats["areas"]       = _get_cnt("area_explorer_results")
        stats["validated"]   = _get_cnt("validated_boundaries")
        stats["checkpoints"] = _get_cnt("area_harvest_checkpoints")
        stats["total"] = (
            stats["coordinates"]
            + stats["boundaries"]
            + stats["amenities"]
            + stats["bulk_jobs"]
            + stats["areas"]
            + stats["validated"]
            + stats["checkpoints"]
        )

    return stats


def optimize_db() -> None:
    """Runs VACUUM and ANALYZE to optimize database."""
    with get_connection() as conn:
        conn.execute("ANALYZE")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    try:
        vacuum_conn = sqlite3.connect(DB_FILE)
        try:
            vacuum_conn.execute("VACUUM")
        finally:
            vacuum_conn.close()
    except Exception as exc:
        print(f"[!] VACUUM failed (non-fatal): {exc}")
    print("[OK] Database optimized")


# ─────────────────────────────────────────────
#  HELPER
# ─────────────────────────────────────────────

def _safe_float(val) -> float | None:
    """Safely converts a value to float."""
    try:
        if val is None:
            return None
        f = float(val)
        return f if f == f else None  # NaN check
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────
#  APP SETTINGS & PASSWORD AUTHENTICATION
# ─────────────────────────────────────────────

def _ensure_app_settings_table() -> None:
    """Ensures app_settings table exists in database."""
    try:
        with get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key         TEXT PRIMARY KEY,
                    value       TEXT,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    except Exception:
        pass


def get_setting(key: str, default: str = None) -> str | None:
    """Retrieve an application setting by key safely."""
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default
    except sqlite3.OperationalError:
        _ensure_app_settings_table()
        try:
            with get_connection() as conn:
                row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
                return row[0] if row else default
        except Exception:
            return default
    except Exception:
        return default


def set_setting(key: str, value: str) -> bool:
    """Save or update an application setting safely."""
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, value))
            conn.commit()
        return True
    except sqlite3.OperationalError:
        _ensure_app_settings_table()
        try:
            with get_connection() as conn:
                conn.execute("""
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                """, (key, value))
                conn.commit()
            return True
        except Exception:
            return False
    except Exception:
        return False


def _hash_password(password: str, salt: str = None) -> str:
    """Generates a secure salted SHA-256 hash."""
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}:{hashed}"


DEFAULT_MASTER_PASSWORD_HASH = "e7adbcdef11f1f2b08188a6480fbdb87:838ab3b2b3094dc6ee7fb839cc6ed94e602c958b9a9119605ddc5fc006292c0b"  # Jarvis2026!


def has_master_password() -> bool:
    """Returns True if master password is set or default is active, False if reset is pending."""
    try:
        stored = get_setting("master_password_hash")
        if stored == "RESET_PENDING":
            return False
    except Exception:
        pass
    return True


def set_master_password(password: str) -> bool:
    """Sets or resets the master password."""
    if not password or len(password.strip()) < 3:
        return False
    hashed = _hash_password(password.strip())
    return set_setting("master_password_hash", hashed)


def verify_master_password(password: str) -> bool:
    """Verifies candidate password against stored or default salted hash."""
    try:
        stored = get_setting("master_password_hash")
    except Exception:
        stored = None
    if stored == "RESET_PENDING":
        return False
    target = stored or DEFAULT_MASTER_PASSWORD_HASH
    if not target or ":" not in target:
        return False
    salt, expected_hash = target.split(":", 1)
    candidate_hash = hashlib.sha256((salt + password.strip()).encode("utf-8")).hexdigest()
    return secrets.compare_digest(expected_hash, candidate_hash)



def change_master_password(old_password: str, new_password: str) -> tuple[bool, str]:
    """Validates old password and sets new password if valid."""
    if not has_master_password():
        # First time setup
        if not new_password or len(new_password.strip()) < 3:
            return False, "New password must be at least 3 characters long."
        set_master_password(new_password)
        return True, "Master password created successfully."

    if not verify_master_password(old_password):
        return False, "Current password is incorrect."

    if not new_password or len(new_password.strip()) < 3:
        return False, "New password must be at least 3 characters long."

    if old_password == new_password:
        return False, "New password must be different from the current password."

    set_master_password(new_password)
    return True, "Password updated successfully."


def reset_master_password() -> bool:
    """Marks the master password as reset so the owner can configure a new one."""
    return set_setting("master_password_hash", "RESET_PENDING")



DEFAULT_OWNER_RECOVERY_KEY = "JARVIS-OWNER-ADMIN-2026"


def set_owner_recovery_key(key: str) -> bool:
    """Sets a custom hashed owner recovery key."""
    if not key or len(key.strip()) < 4:
        return False
    hashed = _hash_password(key.strip())
    return set_setting("owner_recovery_key_hash", hashed)


def verify_owner_recovery_key(candidate: str) -> bool:
    """Verifies candidate recovery key against stored or default owner key."""
    if not candidate:
        return False
    candidate = candidate.strip()
    stored = get_setting("owner_recovery_key_hash")
    if stored and ":" in stored:
        salt, expected_hash = stored.split(":", 1)
        cand_hash = hashlib.sha256((salt + candidate).encode("utf-8")).hexdigest()
        return secrets.compare_digest(expected_hash, cand_hash)
    return secrets.compare_digest(candidate, DEFAULT_OWNER_RECOVERY_KEY)

