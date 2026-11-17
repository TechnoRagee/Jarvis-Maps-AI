"""
updater.py
----------
Jarvis Maps AI — In-App Update Checker & Notification Engine.
Checks for online updates on startup and notifies the user with direct links to the website.
"""

import os
import sys
import re
import json
import threading
import webbrowser
import urllib.request
import urllib.error
import tkinter as tk
import tkinter.messagebox as messagebox

CURRENT_VERSION = "1.0.0"
DEFAULT_UPDATE_URL = os.getenv("JARVIS_UPDATE_URL", "http://localhost:8080/version.json")


def parse_version_tuple(version_str: str) -> tuple:
    """Parses a version string into a comparable tuple of integers (e.g. '1.2.3' -> (1, 2, 3))."""
    if not version_str:
        return (0, 0, 0)
    nums = re.findall(r"\d+", str(version_str))
    return tuple(int(n) for n in nums) if nums else (0, 0, 0)


def is_version_newer(latest_version: str, current_version: str = CURRENT_VERSION) -> bool:
    """Returns True if latest_version is strictly higher than current_version."""
    v_latest = parse_version_tuple(latest_version)
    v_curr = parse_version_tuple(current_version)
    return v_latest > v_curr


def fetch_version_manifest(manifest_url: str = None, timeout: float = 3.5) -> dict | None:
    """Synchronously fetches the online version manifest JSON from the website/server."""
    url = manifest_url or DEFAULT_UPDATE_URL
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"JarvisMapsAI-Updater/{CURRENT_VERSION}", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                return data
    except Exception:
        pass
    return None


def check_for_updates_async(root: tk.Tk, on_result, manifest_url: str = None, silent_if_latest: bool = True):
    """
    Checks for updates in a background thread to prevent UI freezing.
    Calls on_result(result_dict) on the Tkinter main thread via root.after.
    """
    def _worker():
        manifest = fetch_version_manifest(manifest_url)
        if not manifest:
            res = {
                "success": False,
                "update_available": False,
                "current_version": CURRENT_VERSION,
                "message": "Could not connect to update server.",
                "silent": silent_if_latest,
            }
        else:
            latest_ver = manifest.get("latest_version", CURRENT_VERSION)
            update_avail = is_version_newer(latest_ver, CURRENT_VERSION)
            res = {
                "success": True,
                "update_available": update_avail,
                "current_version": CURRENT_VERSION,
                "latest_version": latest_ver,
                "release_name": manifest.get("release_name", f"Jarvis Maps AI v{latest_ver}"),
                "release_date": manifest.get("release_date", ""),
                "download_url": manifest.get("download_url", "http://localhost:8080/#download"),
                "changelog": manifest.get("changelog", []),
                "critical": manifest.get("critical", False),
                "silent": silent_if_latest,
            }

        if root and root.winfo_exists():
            root.after(0, lambda: on_result(res))

    threading.Thread(target=_worker, daemon=True).start()


def open_website_download(url: str = None):
    """Launches the user's default web browser to the download portal."""
    target_url = url or "http://localhost:8080/#download"
    try:
        webbrowser.open_new_tab(target_url)
    except Exception:
        try:
            os.system(f'start "" "{target_url}"')
        except Exception:
            pass


class UpdateNotificationDialog:
    """Modern dark theme modal window alerting the user to a new software release."""
    def __init__(self, parent: tk.Tk, update_info: dict):
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("🚀 Update Available — Jarvis Maps AI")
        self.dialog.configure(bg="#0b0f19")
        self.dialog.resizable(False, False)

        w, h = 530, 480
        sw = self.dialog.winfo_screenwidth()
        sh = self.dialog.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 30)
        self.dialog.geometry(f"{w}x{h}+{x}+{y}")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self.update_info = update_info
        self._build_ui()

    def _build_ui(self):
        card = tk.Frame(self.dialog, bg="#111827", highlightthickness=1, highlightbackground="#374151", padx=24, pady=22)
        card.pack(fill="both", expand=True, padx=14, pady=14)

        # Header with icon
        header_row = tk.Frame(card, bg="#111827")
        header_row.pack(fill="x", pady=(0, 14))

        tk.Label(header_row, text="🚀", font=("Segoe UI", 26), bg="#111827").pack(side="left", padx=(0, 12))
        
        title_col = tk.Frame(header_row, bg="#111827")
        title_col.pack(side="left", fill="x", expand=True)

        latest_v = self.update_info.get("latest_version", "New Version")
        tk.Label(title_col, text=f"Update Available: v{latest_v}", font=("Segoe UI", 15, "bold"), bg="#111827", fg="#f9fafb").pack(anchor="w")
        rel_name = self.update_info.get("release_name", "A new version of Jarvis Maps AI is ready.")
        tk.Label(title_col, text=rel_name, font=("Segoe UI", 9), bg="#111827", fg="#9ca3af").pack(anchor="w", pady=(2, 0))

        # Version comparison badge
        badge_box = tk.Frame(card, bg="#1f2937", highlightthickness=1, highlightbackground="#374151", padx=12, pady=8)
        badge_box.pack(fill="x", pady=(0, 14))

        curr_v = self.update_info.get("current_version", CURRENT_VERSION)
        rel_date = self.update_info.get("release_date", "Recently Released")
        
        tk.Label(
            badge_box,
            text=f"Current: v{curr_v}   ➜   Latest: v{latest_v}   •   Released: {rel_date}",
            font=("Consolas", 10, "bold"), bg="#1f2937", fg="#38bdf8"
        ).pack(anchor="w")

        # Changelog section
        tk.Label(card, text="What's New in this Release:", font=("Segoe UI", 10, "bold"), bg="#111827", fg="#e5e7eb").pack(anchor="w", pady=(0, 6))

        changelog_frame = tk.Frame(card, bg="#0d1117", highlightthickness=1, highlightbackground="#21262d", padx=12, pady=10)
        changelog_frame.pack(fill="both", expand=True, pady=(0, 16))

        items = self.update_info.get("changelog", [])
        if not items:
            items = ["New UI improvements and enhancements", "Faster geospatial processing", "Bug fixes and stability updates"]

        for item in items[:5]:
            row = tk.Frame(changelog_frame, bg="#0d1117")
            row.pack(fill="x", pady=2)
            tk.Label(row, text="•", font=("Segoe UI", 10, "bold"), bg="#0d1117", fg="#34d399").pack(side="left", padx=(0, 6), anchor="n")
            tk.Label(row, text=item, font=("Segoe UI", 9), bg="#0d1117", fg="#d1d5db", wraplength=410, justify="left").pack(side="left", fill="x", expand=True)

        # Action Buttons
        btn_row = tk.Frame(card, bg="#111827")
        btn_row.pack(fill="x")

        download_url = self.update_info.get("download_url", "http://localhost:8080/#download")

        def _on_download():
            open_website_download(download_url)
            self.dialog.destroy()

        # Download / Update button
        download_btn = tk.Button(
            btn_row, text="🌐 Download Update from Website", font=("Segoe UI", 10, "bold"),
            bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8", activeforeground="#ffffff",
            relief="flat", cursor="hand2", padx=16, pady=8, command=_on_download
        )
        download_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # Later button
        later_btn = tk.Button(
            btn_row, text="Remind Later", font=("Segoe UI", 9),
            bg="#1f2937", fg="#9ca3af", activebackground="#374151", activeforeground="#f3f4f6",
            relief="flat", cursor="hand2", padx=14, pady=8, command=self.dialog.destroy
        )
        later_btn.pack(side="right")
