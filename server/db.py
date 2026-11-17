"""
server/db.py
------------
Transactional, restart-safe SQLite storage engine for Jarvis Maps AI licensing server.
Preserves existing customer licenses, hardware bindings, and audit history.
Provides ACID safety, rolling online SQLite backups, and backward-compatible data access.
"""

import os
import json
import sqlite3
import shutil
import logging
from datetime import datetime

logger = logging.getLogger("JarvisDB")

CURRENT_SCHEMA_VERSION = 1


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Creates a configured connection to the SQLite database with WAL mode."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_schema(conn: sqlite3.Connection):
    """Initializes schema tables if not already present."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                details TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                key TEXT PRIMARY KEY,
                tier TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                max_devices INTEGER NOT NULL DEFAULT 1,
                bound_hwids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                expires_at TEXT,
                notes TEXT DEFAULT '',
                client_name TEXT DEFAULT '',
                preset TEXT DEFAULT '',
                duration_display TEXT DEFAULT ''
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_ips (
                ip TEXT PRIMARY KEY,
                blocked_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 3,
                expires_at TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_hwids (
                hwid TEXT PRIMARY KEY,
                blocked_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                last_ip TEXT DEFAULT ''
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS failed_attempts (
                identifier TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0,
                last_attempt TEXT NOT NULL,
                locked_until TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event TEXT NOT NULL,
                ip TEXT DEFAULT '',
                hwid TEXT DEFAULT '',
                details TEXT DEFAULT ''
            );
        """)


def migrate_from_json_if_needed(conn: sqlite3.Connection, json_source_path: str) -> dict:
    """
    Transactionally migrates legacy license_db.json into SQLite.
    Restart-safe: checks schema_migrations table first.
    Never modifies or deletes the source JSON file.
    Verifies record counts strictly before committing.
    """
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_migrations WHERE version = ?", (CURRENT_SCHEMA_VERSION,))
    if cur.fetchone():
        logger.info("Database already at schema version %s. Migration skipped.", CURRENT_SCHEMA_VERSION)
        return {"status": "skipped", "reason": "already_migrated"}

    if not os.path.isfile(json_source_path):
        # Fresh installation with no prior JSON database
        with conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, details) VALUES (?, ?, ?)",
                (CURRENT_SCHEMA_VERSION, datetime.utcnow().isoformat(), "Fresh installation initialization")
            )
        return {"status": "initialized", "reason": "fresh_install"}

    with open(json_source_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    json_licenses = data.get("licenses", {})
    json_blocked_ips = data.get("blocked_ips", {})
    json_blocked_hwids = data.get("blocked_hwids", {})
    json_audit_logs = data.get("audit_logs", [])
    master_passcode = data.get("master_passcode", "Jarvis2026!")

    # Begin atomic transaction
    with conn:
        # 1. Licenses
        for key, lic in json_licenses.items():
            bound_hwids_json = json.dumps(lic.get("bound_hwids", []))
            conn.execute("""
                INSERT OR REPLACE INTO licenses (
                    key, tier, status, max_devices, bound_hwids, created_at,
                    expires_at, notes, client_name, preset, duration_display
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                key,
                lic.get("tier", "Professional"),
                str(lic.get("status", "ACTIVE")).upper(),
                int(lic.get("max_devices", 1)),
                bound_hwids_json,
                lic.get("created_at", datetime.utcnow().isoformat()),
                lic.get("expires_at"),
                lic.get("notes", ""),
                lic.get("client_name", ""),
                lic.get("preset", ""),
                lic.get("duration_display", "")
            ))

        # 2. Blocked IPs
        for ip, item in json_blocked_ips.items():
            if isinstance(item, dict):
                blocked_at = item.get("blocked_at", datetime.utcnow().isoformat())
                reason = item.get("reason", "Administrative IP block")
                attempts = item.get("attempts", 3)
                expires_at = item.get("expires_at")
            else:
                blocked_at = datetime.utcnow().isoformat()
                reason = str(item)
                attempts = 3
                expires_at = None
            conn.execute("""
                INSERT OR REPLACE INTO blocked_ips (ip, blocked_at, reason, attempts, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (ip, blocked_at, reason, attempts, expires_at))

        # 3. Blocked HWIDs
        for hwid, item in json_blocked_hwids.items():
            if isinstance(item, dict):
                blocked_at = item.get("blocked_at", datetime.utcnow().isoformat())
                reason = item.get("reason", "Administrative HWID block")
                last_ip = item.get("last_ip", "")
            else:
                blocked_at = datetime.utcnow().isoformat()
                reason = str(item)
                last_ip = ""
            conn.execute("""
                INSERT OR REPLACE INTO blocked_hwids (hwid, blocked_at, reason, last_ip)
                VALUES (?, ?, ?, ?)
            """, (hwid, blocked_at, reason, last_ip))

        # 4. Audit logs
        for log in json_audit_logs:
            conn.execute("""
                INSERT INTO audit_logs (timestamp, event, ip, hwid, details)
                VALUES (?, ?, ?, ?, ?)
            """, (
                log.get("timestamp", datetime.utcnow().isoformat()),
                log.get("event", "UNKNOWN"),
                log.get("ip", ""),
                log.get("hwid", ""),
                log.get("details", "")
            ))

        # 5. App config & master passcode
        conn.execute("""
            INSERT OR REPLACE INTO app_config (key, value, updated_at)
            VALUES (?, ?, ?)
        """, ("master_passcode", master_passcode, datetime.utcnow().isoformat()))

        # Verify Record Counts strictly before marking migration completed
        count_lic = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        count_hwid = conn.execute("SELECT COUNT(*) FROM blocked_hwids").fetchone()[0]
        count_logs = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]

        if count_lic != len(json_licenses):
            raise ValueError(f"License count mismatch: expected {len(json_licenses)}, got {count_lic}")
        if count_hwid != len(json_blocked_hwids):
            raise ValueError(f"Blocked HWID count mismatch: expected {len(json_blocked_hwids)}, got {count_hwid}")
        if count_logs != len(json_audit_logs):
            raise ValueError(f"Audit log count mismatch: expected {len(json_audit_logs)}, got {count_logs}")

        # Mark migration successful
        conn.execute("""
            INSERT INTO schema_migrations (version, applied_at, details)
            VALUES (?, ?, ?)
        """, (
            CURRENT_SCHEMA_VERSION,
            datetime.utcnow().isoformat(),
            f"Migrated {count_lic} licenses, {count_hwid} blocked HWIDs, {count_logs} logs from JSON."
        ))

    logger.info("Successfully migrated %d licenses, %d blocked HWIDs, %d audit logs to SQLite.",
                count_lic, count_hwid, count_logs)

    return {
        "status": "success",
        "migrated_licenses": count_lic,
        "migrated_blocked_hwids": count_hwid,
        "migrated_audit_logs": count_logs,
        "master_passcode_preserved": bool(master_passcode)
    }


def backup_sqlite_database(source_conn: sqlite3.Connection, backup_dir: str, max_backups: int = 10) -> str:
    """
    Performs an online, non-blocking transactional backup using SQLite Backup API.
    Maintains a rolling window of recent backups.
    """
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"licenses_backup_{timestamp}.db")

    target_conn = sqlite3.connect(backup_file)
    with target_conn:
        source_conn.backup(target_conn, pages=100)
    target_conn.close()

    # Clean up older backups exceeding max_backups
    existing = sorted([
        os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
        if f.startswith("licenses_backup_") and f.endswith(".db")
    ])
    while len(existing) > max_backups:
        oldest = existing.pop(0)
        try:
            os.remove(oldest)
        except Exception:
            pass

    return backup_file
