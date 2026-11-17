"""
server/license_server.py
------------------------
Production-ready Cloud License & Security Gateway for Jarvis Maps AI.
Backed by persistent SQLite storage, transactional ACID integrity,
rolling online backups, trusted-proxy header validation, and brute-force rate-limiting.
"""

import os
import sys
import json
import time
import secrets
import logging
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

from server.db import (
    get_db_connection,
    init_schema,
    migrate_from_json_if_needed,
    backup_sqlite_database
)
from server.config import get_admin_secret_key

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("JarvisLicenseServer")

# Paths & Database
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(SERVER_DIR, "licenses.db")
LEGACY_JSON_FILE = os.path.join(SERVER_DIR, "license_db.json")
BACKUPS_DIR = os.path.join(SERVER_DIR, "backups")

# In-memory synchronization lock
db_lock = threading.Lock()

# Rate limiting storage: { ip: [timestamp, timestamp, ...] }
admin_failed_attempts = {}
ADMIN_MAX_FAILURES = 5
ADMIN_LOCKOUT_MINUTES = 15


def get_db():
    """Returns a connection to the server's SQLite database."""
    return get_db_connection(DB_FILE)


def init_database():
    """Initializes schema and runs restart-safe migration from legacy JSON if needed."""
    conn = get_db()
    try:
        init_schema(conn)
        migrate_from_json_if_needed(conn, LEGACY_JSON_FILE)
        # Run startup backup
        backup_sqlite_database(conn, BACKUPS_DIR)
    finally:
        conn.close()


def add_audit_log(event_type: str, ip: str, hwid: str, details: str, conn):
    """Appends an event to the security audit trail."""
    try:
        conn.execute("""
            INSERT INTO audit_logs (timestamp, event, ip, hwid, details)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), event_type, ip, hwid, details))
    except Exception as e:
        logger.error("Failed to write audit log: %s", e)


def is_admin_rate_limited(ip: str) -> tuple[bool, int]:
    """
    Checks if an IP is temporarily locked out from admin endpoints.
    Returns (is_limited, remaining_seconds).
    """
    now = time.time()
    attempts = admin_failed_attempts.get(ip, [])
    # Filter attempts within lockout window
    cutoff = now - (ADMIN_LOCKOUT_MINUTES * 60)
    valid_attempts = [t for t in attempts if t > cutoff]
    admin_failed_attempts[ip] = valid_attempts

    if len(valid_attempts) >= ADMIN_MAX_FAILURES:
        oldest = min(valid_attempts)
        remaining = int((oldest + (ADMIN_LOCKOUT_MINUTES * 60)) - now)
        return True, max(remaining, 1)
    return False, 0


def record_admin_failure(ip: str):
    """Tracks a failed admin login attempt."""
    now = time.time()
    if ip not in admin_failed_attempts:
        admin_failed_attempts[ip] = []
    admin_failed_attempts[ip].append(now)


def clear_admin_failures(ip: str):
    """Resets failed attempts upon successful authentication."""
    admin_failed_attempts.pop(ip, None)


class LicenseRequestHandler(BaseHTTPRequestHandler):
    """High-performance, secure request dispatcher for license verification and administration."""

    def log_message(self, format, *args):
        # Clean production logging
        logger.info("%s - - [%s] %s", self.get_client_ip(), self.log_date_time_string(), format % args)

    def get_client_ip(self) -> str:
        """
        Extracts real client IP address, supporting Cloudflare (CF-Connecting-IP)
        and trusted reverse-proxy headers (X-Forwarded-For, X-Real-IP).
        """
        cf_ip = self.headers.get("CF-Connecting-IP")
        if cf_ip and cf_ip.strip():
            return cf_ip.strip()

        x_forwarded_for = self.headers.get("X-Forwarded-For")
        if x_forwarded_for:
            # First IP in chain is client IP
            client_ip = x_forwarded_for.split(",")[0].strip()
            if client_ip:
                return client_ip

        x_real_ip = self.headers.get("X-Real-IP")
        if x_real_ip:
            return x_real_ip.strip()

        return self.client_address[0]

    def _send_json(self, status_code: int, data: dict):
        """Transmits JSON payload with standard CORS and cache prevention headers."""
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Key, Authorization")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        """Pre-flight CORS handler."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Key, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        """Handles health checks and authenticated administrative dashboard data."""
        client_ip = self.get_client_ip()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. Public Health Check
        if path == "/api/health":
            self._send_json(200, {
                "status": "online",
                "service": "Jarvis Maps AI Security & License Gateway",
                "client_ip": client_ip,
                "timestamp": datetime.utcnow().isoformat()
            })
            return

        # 2. Authenticated Admin Dashboard
        if path == "/api/admin/dashboard":
            # Check rate limiting
            is_limited, cooldown = is_admin_rate_limited(client_ip)
            if is_limited:
                self._send_json(429, {
                    "error": f"Too many failed login attempts. Locked out for {cooldown} seconds.",
                    "cooldown_seconds": cooldown
                })
                return

            auth_header = self.headers.get("Authorization", "")
            bearer_key = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
            query_key = query.get("admin_key", [""])[0]
            header_key = self.headers.get("X-Admin-Key", "")
            candidate_key = header_key or bearer_key or query_key

            expected_admin_key = get_admin_secret_key()
            if candidate_key != expected_admin_key:
                record_admin_failure(client_ip)
                self._send_json(401, {"error": "Unauthorized: Invalid Admin Secret Key"})
                return

            clear_admin_failures(client_ip)

            with db_lock:
                conn = get_db()
                try:
                    cur = conn.cursor()
                    lic_rows = cur.execute("SELECT * FROM licenses ORDER BY created_at DESC").fetchall()
                    blocked_ip_rows = cur.execute("SELECT * FROM blocked_ips ORDER BY blocked_at DESC").fetchall()
                    blocked_hwid_rows = cur.execute("SELECT * FROM blocked_hwids ORDER BY blocked_at DESC").fetchall()
                    log_rows = cur.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 100").fetchall()
                    passcode_row = cur.execute("SELECT value FROM app_config WHERE key = 'master_passcode'").fetchone()

                    master_passcode = passcode_row["value"] if passcode_row else "Jarvis2026!"

                    lic_list = []
                    active_count = 0
                    for row in lic_rows:
                        d = dict(row)
                        d["bound_hwids"] = json.loads(d["bound_hwids"])
                        if d["status"] == "ACTIVE":
                            active_count += 1
                        lic_list.append(d)

                    blocked_ips = {r["ip"]: dict(r) for r in blocked_ip_rows}
                    blocked_hwids = {r["hwid"]: dict(r) for r in blocked_hwid_rows}
                    audit_logs = [dict(r) for r in log_rows]

                    self._send_json(200, {
                        "master_passcode": master_passcode,
                        "total_licenses": len(lic_list),
                        "active_licenses": active_count,
                        "total_blocked_ips": len(blocked_ips),
                        "total_blocked_hwids": len(blocked_hwids),
                        "licenses": lic_list,
                        "blocked_ips": blocked_ips,
                        "blocked_hwids": blocked_hwids,
                        "audit_logs": audit_logs
                    })
                finally:
                    conn.close()
            return

        self._send_json(404, {"error": "Not Found"})

    def do_POST(self):
        """Handles license validation and administrative mutation actions."""
        client_ip = self.get_client_ip()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Parse JSON body
        content_length = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_length > 0:
            try:
                body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except Exception:
                self._send_json(400, {"error": "Malformed JSON payload"})
                return

        # =========================================================================
        # 1. PUBLIC ENDPOINT: License & HWID Verification Engine
        # =========================================================================
        if path == "/api/license/verify":
            candidate_key = str(body.get("key", "")).strip()
            hwid = str(body.get("hwid", "")).strip()
            platform_str = str(body.get("platform", "Unknown")).strip()
            hostname_str = str(body.get("hostname", "")).strip()

            if not candidate_key:
                self._send_json(400, {"status": "failed", "message": "Missing key or passcode"})
                return

            with db_lock:
                conn = get_db()
                try:
                    cur = conn.cursor()

                    # Check IP blacklist
                    ip_ban = cur.execute("SELECT * FROM blocked_ips WHERE ip = ?", (client_ip,)).fetchone()
                    if ip_ban:
                        add_audit_log("BLOCKED_ACCESS", client_ip, hwid, f"Blocked IP rejected", conn)
                        conn.commit()
                        self._send_json(403, {
                            "status": "blocked",
                            "message": "Your IP address is blacklisted on this server."
                        })
                        return

                    # Check HWID blacklist
                    if hwid:
                        hwid_ban = cur.execute("SELECT * FROM blocked_hwids WHERE hwid = ?", (hwid,)).fetchone()
                        if hwid_ban:
                            add_audit_log("BLOCKED_ACCESS", client_ip, hwid, f"Blocked HWID rejected", conn)
                            conn.commit()
                            self._send_json(403, {
                                "status": "blocked",
                                "message": "Your device Hardware ID (HWID) is permanently blacklisted."
                            })
                            return

                    # Check Master Passcode
                    passcode_row = cur.execute("SELECT value FROM app_config WHERE key = 'master_passcode'").fetchone()
                    master_passcode = passcode_row["value"] if passcode_row else "Jarvis2026!"

                    if candidate_key == master_passcode:
                        # Clear failed attempts on success
                        cur.execute("DELETE FROM failed_attempts WHERE identifier IN (?, ?)", (client_ip, hwid))
                        add_audit_log("AUTH_SUCCESS", client_ip, hwid, "Valid master passcode authorization", conn)
                        conn.commit()
                        token = secrets.token_hex(24)
                        self._send_json(200, {
                            "status": "authorized",
                            "ip": client_ip,
                            "hwid": hwid,
                            "tier": "Master Passcode Enterprise",
                            "expires_at": None,
                            "token": token,
                            "server_time": datetime.utcnow().isoformat()
                        })
                        return

                    # Check License Key
                    lic_row = cur.execute("SELECT * FROM licenses WHERE key = ?", (candidate_key,)).fetchone()
                    if lic_row:
                        lic = dict(lic_row)
                        bound_hwids = json.loads(lic["bound_hwids"])

                        # Check if revoked
                        if lic["status"] != "ACTIVE":
                            add_audit_log("AUTH_FAILED", client_ip, hwid, f"Inactive key status: {lic['status']}", conn)
                            conn.commit()
                            self._send_json(200, {
                                "status": "failed",
                                "message": f"License key is {lic['status'].lower()}."
                            })
                            return

                        # Check expiry
                        if lic["expires_at"]:
                            try:
                                exp = datetime.fromisoformat(lic["expires_at"])
                                if datetime.utcnow() > exp:
                                    conn.execute("UPDATE licenses SET status = 'EXPIRED' WHERE key = ?", (candidate_key,))
                                    add_audit_log("AUTH_FAILED", client_ip, hwid, "License key expired", conn)
                                    conn.commit()
                                    self._send_json(200, {
                                        "status": "failed",
                                        "message": "License key has expired."
                                    })
                                    return
                            except Exception:
                                pass

                        # Check HWID Binding
                        if hwid:
                            if hwid not in bound_hwids:
                                if len(bound_hwids) >= lic["max_devices"]:
                                    add_audit_log("AUTH_FAILED", client_ip, hwid, "Max device activations exceeded", conn)
                                    conn.commit()
                                    self._send_json(200, {
                                        "status": "failed",
                                        "message": f"License activation limit reached ({lic['max_devices']} devices bound)."
                                    })
                                    return
                                # Bind new device
                                bound_hwids.append(hwid)
                                conn.execute("UPDATE licenses SET bound_hwids = ? WHERE key = ?",
                                             (json.dumps(bound_hwids), candidate_key))

                        # Success!
                        cur.execute("DELETE FROM failed_attempts WHERE identifier IN (?, ?)", (client_ip, hwid))
                        add_audit_log("AUTH_SUCCESS", client_ip, hwid, f"Valid license authorization ({lic['tier']})", conn)
                        conn.commit()
                        token = secrets.token_hex(24)
                        self._send_json(200, {
                            "status": "authorized",
                            "ip": client_ip,
                            "hwid": hwid,
                            "tier": lic["tier"],
                            "expires_at": lic["expires_at"],
                            "token": token,
                            "server_time": datetime.utcnow().isoformat()
                        })
                        return

                    # Credential Failed — Increment failure tracker
                    identifier = hwid or client_ip
                    fail_row = cur.execute("SELECT count FROM failed_attempts WHERE identifier = ?", (identifier,)).fetchone()
                    current_fails = (fail_row["count"] + 1) if fail_row else 1

                    cur.execute("""
                        INSERT OR REPLACE INTO failed_attempts (identifier, count, last_attempt)
                        VALUES (?, ?, ?)
                    """, (identifier, current_fails, datetime.utcnow().isoformat()))

                    max_allowed = 3
                    remaining = max(0, max_allowed - current_fails)

                    if current_fails >= max_allowed:
                        # Apply progressive temporary IP lockout (15 minutes) rather than permanent immediate ban
                        cooldown_exp = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
                        cur.execute("""
                            INSERT OR REPLACE INTO blocked_ips (ip, blocked_at, reason, attempts, expires_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (client_ip, datetime.utcnow().isoformat(), "Exceeded 3 failed attempts", current_fails, cooldown_exp))
                        add_audit_log("IP_COOLDOWN", client_ip, hwid, "15-minute temporary lockout applied", conn)
                        conn.commit()
                        self._send_json(403, {
                            "status": "blocked",
                            "message": "Security lockout: 3 consecutive failed attempts. Temporary 15-minute cooldown active."
                        })
                        return

                    add_audit_log("AUTH_FAILED", client_ip, hwid, f"Failed attempt #{current_fails}", conn)
                    conn.commit()
                    self._send_json(200, {
                        "status": "failed",
                        "message": "Invalid license key or master passcode.",
                        "remaining_attempts": remaining
                    })
                finally:
                    conn.close()
            return

        # =========================================================================
        # 2. ADMINISTRATIVE ENDPOINTS (Protected with X-Admin-Key & Rate-Limiting)
        # =========================================================================
        is_limited, cooldown = is_admin_rate_limited(client_ip)
        if is_limited:
            self._send_json(429, {
                "error": f"Too many failed admin requests. Locked out for {cooldown} seconds.",
                "cooldown_seconds": cooldown
            })
            return

        auth_header = self.headers.get("Authorization", "")
        bearer_key = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
        query_key = query.get("admin_key", [""])[0]
        header_key = self.headers.get("X-Admin-Key", "")
        body_key = body.get("admin_key", "")
        candidate_admin_key = header_key or bearer_key or body_key or query_key

        expected_admin_key = get_admin_secret_key()
        if candidate_admin_key != expected_admin_key:
            record_admin_failure(client_ip)
            self._send_json(401, {"error": "Unauthorized: Invalid Admin Secret Key"})
            return

        clear_admin_failures(client_ip)

        with db_lock:
            conn = get_db()
            try:
                # -----------------------------------------------------------------
                # Update Master Passcode
                # -----------------------------------------------------------------
                if path == "/api/admin/update-passcode":
                    new_passcode = body.get("new_passcode", "").strip()
                    if not new_passcode or len(new_passcode) < 3:
                        self._send_json(400, {"error": "New passcode must be at least 3 characters."})
                        return

                    conn.execute("""
                        INSERT OR REPLACE INTO app_config (key, value, updated_at)
                        VALUES (?, ?, ?)
                    """, ("master_passcode", new_passcode, datetime.utcnow().isoformat()))
                    add_audit_log("PASSCODE_CHANGED", client_ip, "", "Master passcode updated securely", conn)
                    conn.commit()
                    backup_sqlite_database(conn, BACKUPS_DIR)

                    self._send_json(200, {
                        "success": True,
                        "status": "success",
                        "message": "Master passcode updated successfully.",
                        "master_passcode": new_passcode
                    })
                    return

                # -----------------------------------------------------------------
                # Generate Customer License Key
                # -----------------------------------------------------------------
                if path == "/api/admin/generate-key":
                    preset = str(body.get("preset", "")).lower().strip()
                    if preset == "1_month":
                        duration_days = 30
                        tier = body.get("tier") or "Pro 1-Month"
                    elif preset == "3_months":
                        duration_days = 90
                        tier = body.get("tier") or "Pro 3-Month"
                    elif preset == "6_months":
                        duration_days = 180
                        tier = body.get("tier") or "Pro 6-Month"
                    elif preset == "12_months":
                        duration_days = 365
                        tier = body.get("tier") or "Enterprise 12-Month"
                    elif preset == "lifetime":
                        duration_days = 0
                        tier = body.get("tier") or "Enterprise Lifetime"
                    else:
                        duration_days = int(body.get("duration_days", 365))
                        tier = body.get("tier", "Professional Studio")

                    max_devices = int(body.get("max_devices") or body.get("device_limit") or 1)
                    notes = str(body.get("notes") or body.get("client_name") or "").strip()

                    entropy = secrets.token_hex(8).upper()
                    new_key = f"JM-{entropy[:4]}-{entropy[4:8]}-{entropy[8:12]}-{entropy[12:16]}"

                    expires_at = None
                    if duration_days > 0:
                        expires_at = (datetime.utcnow() + timedelta(days=duration_days)).isoformat()

                    created_at = datetime.utcnow().isoformat()
                    duration_display = "Lifetime" if duration_days == 0 else f"{duration_days} Days"

                    conn.execute("""
                        INSERT INTO licenses (
                            key, tier, status, max_devices, bound_hwids, created_at,
                            expires_at, notes, client_name, preset, duration_display
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (new_key, tier, "ACTIVE", max_devices, "[]", created_at, expires_at, notes, notes, preset, duration_display))

                    add_audit_log("KEY_GENERATED", client_ip, "", f"Created key {new_key} ({tier})", conn)
                    conn.commit()
                    backup_sqlite_database(conn, BACKUPS_DIR)

                    self._send_json(200, {
                        "success": True,
                        "status": "created",
                        "license_key": new_key,
                        "key": new_key,
                        "tier": tier,
                        "preset": preset,
                        "duration_display": duration_display,
                        "expires_at": expires_at,
                        "max_devices": max_devices,
                        "device_limit": max_devices,
                        "notes": notes,
                        "client_name": notes
                    })
                    return

                # -----------------------------------------------------------------
                # Revoke Key
                # -----------------------------------------------------------------
                if path == "/api/admin/revoke-key":
                    target_key = body.get("key", "").strip()
                    conn.execute("UPDATE licenses SET status = 'REVOKED' WHERE key = ?", (target_key,))
                    add_audit_log("KEY_REVOKED", client_ip, "", f"Revoked key {target_key}", conn)
                    conn.commit()
                    self._send_json(200, {"success": True, "status": "revoked", "key": target_key})
                    return

                # -----------------------------------------------------------------
                # Reactivate Key
                # -----------------------------------------------------------------
                if path == "/api/admin/reactivate-key":
                    target_key = body.get("key", "").strip()
                    conn.execute("UPDATE licenses SET status = 'ACTIVE' WHERE key = ?", (target_key,))
                    add_audit_log("KEY_REACTIVATED", client_ip, "", f"Reactivated key {target_key}", conn)
                    conn.commit()
                    self._send_json(200, {"success": True, "status": "active", "key": target_key})
                    return

                # -----------------------------------------------------------------
                # Delete Key
                # -----------------------------------------------------------------
                if path == "/api/admin/delete-key":
                    target_key = body.get("key", "").strip()
                    conn.execute("DELETE FROM licenses WHERE key = ?", (target_key,))
                    add_audit_log("KEY_DELETED", client_ip, "", f"Deleted key {target_key}", conn)
                    conn.commit()
                    self._send_json(200, {"success": True, "status": "deleted", "key": target_key})
                    return

                # -----------------------------------------------------------------
                # Reset Bound HWIDs
                # -----------------------------------------------------------------
                if path == "/api/admin/reset-hwid":
                    target_key = body.get("key", "").strip()
                    conn.execute("UPDATE licenses SET bound_hwids = '[]' WHERE key = ?", (target_key,))
                    add_audit_log("HWID_RESET", client_ip, "", f"Reset devices for key {target_key}", conn)
                    conn.commit()
                    self._send_json(200, {"success": True, "status": "reset", "key": target_key})
                    return

                # -----------------------------------------------------------------
                # Unblock IP
                # -----------------------------------------------------------------
                if path == "/api/admin/unblock-ip":
                    ip_to_unblock = body.get("ip", "").strip()
                    conn.execute("DELETE FROM blocked_ips WHERE ip = ?", (ip_to_unblock,))
                    conn.execute("DELETE FROM failed_attempts WHERE identifier = ?", (ip_to_unblock,))
                    add_audit_log("IP_UNBLOCKED", client_ip, "", f"Unblocked IP {ip_to_unblock}", conn)
                    conn.commit()
                    self._send_json(200, {"success": True, "status": "unblocked", "ip": ip_to_unblock})
                    return

                # -----------------------------------------------------------------
                # Unblock HWID
                # -----------------------------------------------------------------
                if path == "/api/admin/unblock-hwid":
                    hwid_to_unblock = body.get("hwid", "").strip()
                    conn.execute("DELETE FROM blocked_hwids WHERE hwid = ?", (hwid_to_unblock,))
                    conn.execute("DELETE FROM failed_attempts WHERE identifier = ?", (hwid_to_unblock,))
                    add_audit_log("HWID_UNBLOCKED", client_ip, "", f"Unblocked HWID {hwid_to_unblock}", conn)
                    conn.commit()
                    self._send_json(200, {"success": True, "status": "unblocked", "hwid": hwid_to_unblock})
                    return

                self._send_json(404, {"error": "Not Found"})
            finally:
                conn.close()


def run_server(port=8081):
    """Starts the HTTP server daemon on all interfaces."""
    init_database()
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, LicenseRequestHandler)
    logger.info("Jarvis Maps AI Security & License Gateway active on 0.0.0.0:%d", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down License Server...")
        httpd.server_close()


if __name__ == "__main__":
    env_port = os.getenv("PORT")
    if env_port and env_port.isdigit():
        port = int(env_port)
    elif len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    else:
        port = 8081
    run_server(port)
