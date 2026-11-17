"""
Jarvis Maps AI — Hardware ID (HWID) & Licensing Client Engine
Provides hardware fingerprinting, local tamper detection, and cloud license verification.
"""
import os
import sys
import json
import uuid
import hmac
import hashlib
import platform
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# Local license storage path in AppData or workspace
LICENSE_VAULT_FILE = os.path.join(
    os.getenv("LOCALAPPDATA", os.path.expanduser("~")),
    "JarvisMapsAI",
    ".jarvis_license.vault"
)

# Cache path for last known verified server
SERVER_CACHE_FILE = os.path.join(
    os.getenv("LOCALAPPDATA", os.path.expanduser("~")),
    "JarvisMapsAI",
    ".server_cache"
)

# Optional dynamic discovery endpoint (must be HTTPS)
DISCOVERY_ENDPOINT = os.getenv("JARVIS_DISCOVERY_URL", "")


def resolve_dynamic_discovery_url(discovery_url: str) -> str | None:
    """
    Safely queries an HTTPS dynamic discovery endpoint for the current server URL.
    Validates payload format strictly: {"version": "1.0", "server_url": "https://..."}.
    Rejects any insecure HTTP addresses in production.
    """
    if not discovery_url or not discovery_url.lower().startswith("https://"):
        return None

    try:
        req = urllib.request.Request(
            discovery_url,
            headers={"User-Agent": "JarvisMapsAI-Discovery/1.1", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidate = str(data.get("server_url", "")).strip()

            # Enforce HTTPS strictly (prevent downgrade attacks)
            if candidate and candidate.lower().startswith("https://"):
                # Cache last known valid address
                try:
                    os.makedirs(os.path.dirname(SERVER_CACHE_FILE), exist_ok=True)
                    with open(SERVER_CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump({"server_url": candidate, "updated_at": datetime.utcnow().isoformat()}, f)
                except Exception:
                    pass
                return candidate
    except Exception:
        pass
    return None


def get_cached_server_url() -> str | None:
    """Retrieves last known valid server URL from local cache."""
    if os.path.isfile(SERVER_CACHE_FILE):
        try:
            with open(SERVER_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                cached = str(data.get("server_url", "")).strip()
                if cached:
                    return cached
        except Exception:
            pass
    return None


def get_license_server_urls() -> list[str]:
    """
    Returns prioritized list of candidate license server URLs.
    1. Local override config (server_url.txt or JARVIS_LICENSE_SERVER) for development/testing
    2. Dynamic HTTPS Discovery Endpoint (if configured)
    3. Cached last known working server address
    4. Production default candidates
    """
    urls = []

    # 1. Development/Testing overrides
    env_server = os.getenv("JARVIS_LICENSE_SERVER", "").strip()
    if env_server:
        urls.append(env_server)

    app_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]))
    config_paths = [
        os.path.join(app_dir, "server_url.txt"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "JarvisMapsAI", "server_url.txt"),
    ]
    for p in config_paths:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    u = f.read().strip()
                    if u and u not in urls:
                        urls.append(u)
            except Exception:
                pass

    # 2. Dynamic HTTPS discovery resolution
    if DISCOVERY_ENDPOINT:
        discovered = resolve_dynamic_discovery_url(DISCOVERY_ENDPOINT)
        if discovered and discovered not in urls:
            urls.append(discovered)

    # 3. Last known working server from local cache
    cached_server = get_cached_server_url()
    if cached_server and cached_server not in urls:
        urls.append(cached_server)

    # 4. Standard defaults (Production Cloud, Host LAN, and Localhost fallback)
    for fallback in ("https://jarvis-maps-ai.onrender.com", "http://192.168.1.244:8081", "http://localhost:8081"):
        if fallback not in urls:
            urls.append(fallback)

    return urls

_CACHED_HWID = None

def get_machine_hwid() -> str:
    """
    Computes a deterministic, tamper-resistant Hardware ID (HWID)
    from Motherboard UUID, Windows MachineGuid, and CPU Identifier.
    """
    global _CACHED_HWID
    if _CACHED_HWID:
        return _CACHED_HWID

    raw_identifiers = []

    # 1. Windows MachineGuid from Registry
    if sys.platform == "win32":
        try:
            import winreg
            reg_key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            )
            guid, _ = winreg.QueryValueEx(reg_key, "MachineGuid")
            winreg.CloseKey(reg_key)
            if guid:
                raw_identifiers.append(f"GUID:{guid.strip()}")
        except Exception:
            pass

    # 2. Motherboard / Baseboard Serial Number
    try:
        if sys.platform == "win32":
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_BaseBoard).SerialNumber"],
                stderr=subprocess.DEVNULL,
                timeout=3
            ).decode().strip()
            if output:
                raw_identifiers.append(f"BOARD:{output}")
    except Exception:
        pass

    # 3. CPU Processor ID
    try:
        if sys.platform == "win32":
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).ProcessorId"],
                stderr=subprocess.DEVNULL,
                timeout=3
            ).decode().strip()
            if output:
                raw_identifiers.append(f"CPU:{output}")
    except Exception:
        pass

    # 4. Fallback MAC Address / Node
    mac = uuid.getnode()
    raw_identifiers.append(f"MAC:{mac}")

    # Fallback platform signature
    raw_identifiers.append(f"NODE:{platform.node()}:{platform.machine()}")

    combined = "|".join(raw_identifiers)
    digest = hashlib.sha256(combined.encode("utf-8")).hexdigest().upper()
    
    # Formatted HWID: JM-HWID-XXXX-XXXX-XXXX-XXXX
    _CACHED_HWID = f"JM-HWID-{digest[:4]}-{digest[4:8]}-{digest[8:12]}-{digest[12:16]}"
    return _CACHED_HWID


def check_local_hardware_ban() -> tuple[bool, str]:
    """Checks if this specific device has been locally hardware-banned."""
    if not os.path.exists(LICENSE_VAULT_FILE):
        return False, ""
    try:
        with open(LICENSE_VAULT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("is_banned"):
                reason = data.get("ban_reason", "Hardware banned due to unauthorized security violations.")
                return True, reason
    except Exception:
        pass
    return False, ""


def apply_local_hardware_ban(reason: str = "3 Failed authentication attempts"):
    """Permanently bans this machine locally."""
    try:
        os.makedirs(os.path.dirname(LICENSE_VAULT_FILE), exist_ok=True)
        hwid = get_machine_hwid()
        data = {
            "hwid": hwid,
            "is_banned": True,
            "banned_at": datetime.utcnow().isoformat(),
            "ban_reason": reason,
        }
        with open(LICENSE_VAULT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def save_active_license_token(token_data: dict) -> bool:
    """Stores a cryptographically validated license certificate locally for offline access."""
    try:
        os.makedirs(os.path.dirname(LICENSE_VAULT_FILE), exist_ok=True)
        token_data["cached_hwid"] = get_machine_hwid()
        token_data["cached_at"] = datetime.utcnow().isoformat()
        with open(LICENSE_VAULT_FILE, "w", encoding="utf-8") as f:
            json.dump(token_data, f, indent=2)
        return True
    except Exception:
        return False


def get_cached_license() -> dict | None:
    """Retrieves and validates any locally stored license token."""
    if not os.path.exists(LICENSE_VAULT_FILE):
        return None
    try:
        with open(LICENSE_VAULT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("is_banned"):
                return None
            
            # Verify HWID match
            if data.get("cached_hwid") != get_machine_hwid():
                return None
            
            # Verify expiry
            expiry_str = data.get("expires_at")
            if expiry_str:
                expiry = datetime.fromisoformat(expiry_str)
                if datetime.utcnow() > expiry:
                    return None  # Expired
            
            return data
    except Exception:
        return None


def verify_online_license(key_or_password: str, server_url: str = None) -> dict:
    """
    Submits candidate license key or master passcode to central license server.
    Captures IP address on server side and verifies HWID and license status.
    Tries candidate servers (custom, config file, host LAN, localhost) in order.
    """
    candidate_urls = [server_url] if server_url else get_license_server_urls()
    last_offline_err = None

    hwid = get_machine_hwid()
    payload = json.dumps({
        "key": key_or_password.strip(),
        "hwid": hwid,
        "platform": platform.platform(),
        "hostname": platform.node(),
    }).encode("utf-8")

    for base_url in candidate_urls:
        if not base_url:
            continue
        endpoint = f"{base_url.rstrip('/')}/api/license/verify"
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "JarvisMapsAI-Client/1.1"}
        )

        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                result = json.loads(response.read().decode("utf-8"))
                result["connected_server"] = base_url
                return result
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
                return err_data
            except Exception:
                return {"status": "error", "message": f"Server HTTP Error {e.code}"}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_offline_err = f"{base_url} unreachable"
            continue
        except Exception as e:
            last_offline_err = str(e)
            continue

    return {
        "status": "offline",
        "message": f"License server unreachable. Tried: {', '.join(candidate_urls)}"
    }
