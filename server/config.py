"""
server/config.py
----------------
Safe configuration and secrets loader for Jarvis Maps AI Licensing Server.
Ensures zero secrets are hardcoded in the codebase.
"""

import os
import secrets

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(SERVER_DIR, "config")
SECRET_KEY_FILE = os.path.join(CONFIG_DIR, "admin_secret.key")


def get_admin_secret_key() -> str:
    """
    Retrieves the administrator secret key from:
    1. JARVIS_ADMIN_KEY environment variable
    2. server/config/admin_secret.key file
    3. Auto-generates a secure 32-character random key if not present.
    """
    env_key = os.getenv("JARVIS_ADMIN_KEY", "").strip()
    if env_key:
        return env_key

    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.isfile(SECRET_KEY_FILE):
        try:
            with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
                saved = f.read().strip()
                if saved:
                    return saved
        except Exception:
            pass

    # Generate cryptographically secure key
    new_key = f"JARVIS-ADMIN-{secrets.token_hex(16).upper()}"
    try:
        with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(new_key + "\n")
        # Restrict permissions on Unix if available
        if hasattr(os, "chmod"):
            os.chmod(SECRET_KEY_FILE, 0o600)
    except Exception:
        pass

    return new_key
