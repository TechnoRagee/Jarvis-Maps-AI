"""
frontend.py
-----------
Jarvis Maps Automation — Desktop GUI Frontend
Built with Tkinter (built into Python, no install needed)

Features:
  - Upload CSV, TXT, XLS, XLSX files
  - Preview locations before searching
  - Live progress log during automation
  - Auto-open output Excel when done00
  - Boundary Tracer: Pincode / District / State → KML, KMZ, GeoJSON
"""

# Enable High-DPI scaling on Windows
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import threading
import time
from tkinter import filedialog
from tkinter import messagebox
import tkinter as tk 
import tkinter.ttk as ttk
import os
import subprocess
import sys

# Safe stream sink to prevent AttributeError on print() when compiled with console=False (windowed mode)
if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
    class _SafeStreamSink:
        def write(self, s):
            pass
        def flush(self):
            pass
    if sys.stdout is None:
        sys.stdout = _SafeStreamSink()
    if sys.stderr is None:
        sys.stderr = _SafeStreamSink()
import csv
import re
from pathlib import Path
import pandas as pd
from utils.excel_handler import save_to_excel, OUTPUT_FILE, get_output_filename, OUTPUT_DIR, get_user_downloads_dir
get_downloads_dir = get_user_downloads_dir
from utils.boundary_extractor import export_boundary
import playwright_pipeline
from utils.voice_input import press_to_talk, WakeWordListener
from utils.amenity_finder import AMENITY_CATEGORIES, ALL_AMENITIES, fetch_amenities, save_amenities_to_excel, save_amenities_to_kml, _is_valid_place_name
from utils.bulk_runner import BulkRunner, ProgressManager, MAX_BROWSERS, load_jobs_safe, create_sample_jobs_csv
from utils.db_handler import (
    init_db,
    save_amenity_results,
    save_boundary_result,
    save_bulk_runner_job,
    save_validated_boundaries,
    query_coordinates,
    query_amenities,
    count_coordinates,
    count_amenities,
    delete_records,
    clear_table,
    get_db_stats,
    optimize_db,
    PAGE_SIZE,
    TAB_TABLE_CONFIG,
    has_master_password,
    set_master_password,
    verify_master_password,
    change_master_password,
    reset_master_password,
    verify_owner_recovery_key,
    set_owner_recovery_key,
)
from utils.license_manager import (
    get_machine_hwid,
    check_local_hardware_ban,
    apply_local_hardware_ban,
    save_active_license_token,
    get_cached_license,
    verify_online_license,
)
from utils.updater import (
    CURRENT_VERSION,
    check_for_updates_async,
    UpdateNotificationDialog,
)
from utils.area_explorer import (
    fetch_areas,
    save_area_to_excel,
    save_area_to_kml,
    save_area_to_geojson,
)
from utils.area_boundary_validator import (
    load_area_records,
    build_boundary_query,
    AreaBoundaryValidator,
    export_validated_to_excel,
    export_validated_to_csv,
    export_combined_kml,
    _format_points_count,
)
from utils.geotiff_merger import merge_geotiffs, enhance_geotiff_lines


# ─────────────────────────────────────────────
#  COLORS & FONTS (MODERN AESTHETICS)
# ─────────────────────────────────────────────

BG_DARK    = "#0a0c13"
BG_CARD    = "#141724"
BG_INPUT   = "#1b2032"
BORDER     = "#22273a"
ACCENT     = "#6366f1"
ACCENT2    = "#06b6d4"
TEXT_WHITE = "#f8fafc"
TEXT_GRAY  = "#94a3b8"
RED        = "#f43f5e"
GREEN      = "#10b981"
YELLOW     = "#fbbf24"
ORANGE     = "#fb923c"

FONT_TITLE  = ("Segoe UI", 20, "bold")
FONT_SUB    = ("Segoe UI", 10)
FONT_LABEL  = ("Segoe UI", 11, "bold")
FONT_NORMAL = ("Segoe UI", 10)
FONT_MONO   = ("Consolas", 10)
FONT_BTN    = ("Segoe UI", 10, "bold")
FONT_SMALL  = ("Segoe UI", 9)

AREA_TYPES = [
    "Locality",
    "Suburb",
    "Sector",
    "Colony",
    "Nagar / Vihar",
    "Neighbourhood",
    "Block",
    "Phase",
    "Society",
    "Township",
    "Village",
    "Ward",
    "Industrial Area",
    "Commercial / Market",
    "Tehsil / Taluk",
    "Zone / Circle",
    "Town",
    "Hamlet / Basti",
    "Institutional Area",
    "Pincode Area",
]


def _lighten(hex_color, amount=18):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (min(255, c + amount) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


# ─────────────────────────────────────────────
#  FILE READER & CSV HELPERS
# ─────────────────────────────────────────────

def _clean_text_value(value) -> str:
    """Normalize copied text from WhatsApp, Excel, browser, Word, etc."""
    if value is None:
        return ""

    return (
        str(value)
        .replace("\ufeff", "")
        .replace("\xa0", " ")
        .replace("\u200b", "")
        .strip()
    )


def _clean_column_name(value) -> str:
    cleaned = _clean_text_value(value).lower()
    cleaned = cleaned.replace("-", "_").replace(" ", "_")
    cleaned = "".join(ch for ch in cleaned if ch.isalnum() or ch == "_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def read_csv_safe(filepath: str) -> pd.DataFrame:
    """
    Read CSV files even when they are saved by Windows/Excel with cp1252
    characters like non-breaking space (0xA0).
    """
    last_error = None

    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(filepath, encoding=enc)
            df.columns = [_clean_text_value(column) for column in df.columns]

            for column in df.columns:
                if df[column].dtype == object:
                    df[column] = df[column].map(_clean_text_value)

            return df

        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    raise ValueError(f"Could not read CSV file encoding: {last_error}")


LOCALITY_COLUMNS = [
    "locality", "localities", "sub_locality", "sublocality", "suburb",
    "neighborhood", "neighbourhood", "area", "sector", "colony", "place_name"
]
DISTRICT_COLUMNS = ["district", "dist", "district_name"]
CITY_COLUMNS = ["city", "city_name", "town"]
STATE_COLUMNS = ["state", "state_code", "state_name", "province", "st"]
PINCODE_COLUMNS = ["pincode", "pin_code", "pincodes", "zip", "zipcode", "zip_code", "postal_code", "postcode"]
KNOWN_LOCATION_COLUMNS = [
    "locality", "localities", "query", "location", "place", "district", "city",
    "state", "state_code", "state_name", "province", "st",
    "pincode", "pin_code", "pincodes", "zip", "zipcode", "zip_code", "postal_code",
    "postcode", "area", "boundary", "name", "address"
]
INDEX_COLUMNS = ["id", "sno", "s_no", "sr", "sr_no", "sr_num", "index", "no", "#", "unnamed_0"]

BND_SAMPLE_TEMPLATES = {
    "Pincode": {
        "filename": "sample_pincodes.csv",
        "btn_text": "📄 Pincode Template",
        "hint": "Column: Pincode (e.g. 110001)",
        "content": "Pincode\n110001\n400001\n560001\n600001\n700001\n500001\n",
    },
    "District": {
        "filename": "sample_districts.csv",
        "btn_text": "📄 District Template",
        "hint": "Columns: District, State Code (e.g. Agra, UP)",
        "content": "District,State\nAgra,UP\nJaipur,RJ\nPune,MH\nSurat,GJ\nPatna,BR\nIndore,MP\n",
    },
    "State": {
        "filename": "sample_states.csv",
        "btn_text": "📄 State Template",
        "hint": "Column: State (e.g. Uttar Pradesh, Rajasthan)",
        "content": "State\nUttar Pradesh\nRajasthan\nMaharashtra\nGujarat\nKarnataka\nTamil Nadu\n",
    },
    "City": {
        "filename": "sample_cities.csv",
        "btn_text": "📄 City Template",
        "hint": "Columns: City, State Code (e.g. Mumbai, MH or Chandigarh, CH)",
        "content": "City,State\nChandigarh,CH\nDelhi,DL\nMumbai,MH\nBengaluru,KA\nHyderabad,TG\nAhmedabad,GJ\n",
    },
    "Locality": {
        "filename": "sample_localities.csv",
        "btn_text": "📄 Locality Template",
        "hint": "Columns: Locality, District, State Code (e.g. Taj Ganj, Agra, UP)",
        "content": "Locality,District,State\nTaj Ganj,Agra,UP\nHauz Khas,South Delhi,DL\nKoramangala,Bengaluru,KA\nBandra West,Mumbai,MH\nConnaught Place,New Delhi,DL\nAlwarpet,Chennai,TN\n",
    },
}


def _normalize_location_col(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(name).strip().lower()).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned


def _clean_location_val(v) -> str:
    if v is None or pd.isna(v):
        return ""
    text = str(v).replace("\xa0", " ").strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
        text = text[:-2]
    return text


def _has_location_token(main_text: str, token_text: str) -> bool:
    if not main_text or not token_text:
        return False
    main_low = main_text.strip().lower()
    tok_low = token_text.strip().lower()
    if main_low == tok_low:
        return True
    if len(tok_low) <= 3:
        # Short codes like UP, CH, DL, MH -> require word boundary to avoid substring collisions (e.g. 'CH' in 'Chandigarh')
        return bool(re.search(rf"\b{re.escape(tok_low)}\b", main_low))
    # Full name (e.g. 'Delhi' in 'South Delhi')
    return tok_low in main_low


def format_block_query(name: str, parent: str = "", default_city: str = "") -> str:
    """
    Normalizes area and block names for maximum polygon detection accuracy:
    - Identifies block patterns like 'A-23', 'A-1', '1-A', 'B-4', 'C-12', 'A' and converts them to 'Block A-23'
    - Normalizes 'A - Block' or 'A Block' to 'Block A'
    - Seamlessly links Parent Area (e.g. 'Rohini', 'Paschim Vihar', 'Karol Bagh')
    - Appends target city (e.g. 'Delhi') if not present
    """
    nm = str(name or "").strip()
    pr = str(parent or "").strip()

    # Filter out Plus Codes (e.g. 'J7CR+8FC', 'J82C+28M') or placeholder parents
    if re.match(r"^[A-Z0-9]{4}\+[A-Z0-9]{2,}", pr):
        pr = ""
    if pr.lower() in ("delhi, dl", "delhi", "nct of delhi", "none", "nan", "null", ""):
        pr = ""

    transformed_name = nm

    # 1. Normalize 'A - Block' or 'A Block' -> 'Block A'
    m_blk = re.match(r"^([A-Za-z0-9\-]+)\s*[- ]+\bblock\b(?:\s*,\s*(.*))?$", transformed_name, re.I)
    if m_blk:
        blk_id = m_blk.group(1).strip()
        rest = m_blk.group(2)
        if rest:
            transformed_name = f"Block {blk_id}, {rest.strip()}"
        else:
            transformed_name = f"Block {blk_id}"
    else:
        # 2. Pattern: starts with alphanumeric block like 'A-23', 'A-1', '1-A', 'B4', 'C-12' or single letter 'A'
        m_start = re.match(r"^([A-Za-z]{1,3}[- ]*\d+[A-Za-z]?|\d+[- ]*[A-Za-z]+|[A-Za-z])(?:\s*,\s*(.*))?$", nm)
        if m_start:
            prefix = m_start.group(1).strip()
            rest = m_start.group(2)
            # Avoid regular non-block words like 'Shop', 'Plot', 'Flat', 'Lane', etc.
            if not re.match(r"^(shop|plot|flat|room|gate|lane|road|gali|ward|zone|east|west|north|south|old|new|dda)\b", prefix, re.I):
                if (len(prefix) == 1 and prefix.isalpha()) or re.search(r"\d", prefix):
                    if not re.search(r"\bblock\b", prefix, re.I):
                        if rest:
                            transformed_name = f"Block {prefix}, {rest.strip()}"
                        else:
                            transformed_name = f"Block {prefix}"

    parts = [transformed_name]
    if pr and pr.lower() not in transformed_name.lower():
        parts.append(pr)

    full_str = ", ".join(parts)
    if default_city and default_city.lower() not in full_str.lower():
        full_str += f", {default_city}"

    return full_str


def read_locations_from_file(filepath: str, filter_status: tuple[str, ...] | None = None) -> list[str]:
    """
    Robust reader for boundary & location queries from CSV, TXT, Excel files.
    - Handles single-column lists without losing the first row (headerless files).
    - Detects Area Explorer / Polygon Excel files (Area Name + Parent + Polygon Status) and converts blocks (e.g. A-23 -> Block A-23).
    - Detects 3-column locality patterns (Locality + District + State -> 'Taj Ganj, Agra, UP').
    - Detects location & state columns and automatically joins them (e.g. 'Agra, UP', 'Chandigarh, CH').
    - Handles Pincode columns and sanitizes float suffixes (e.g. 110001.0 -> 110001).
    - Skips index/ID columns (e.g. S.No, ID, Unnamed: 0).
    - Auto-detects delimiters (comma, tab, semicolon, pipe) and encodings (utf-8, cp1252, etc.).
    """
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".txt":
        for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
            try:
                with open(filepath, "r", encoding=enc) as f:
                    lines = [_clean_location_val(line) for line in f]
                    return [line for line in lines if line]
            except UnicodeDecodeError:
                continue
        with open(filepath, "r", encoding="latin-1", errors="replace") as f:
            lines = [_clean_location_val(line) for line in f]
            return [line for line in lines if line]

    df = None
    if ext == ".csv":
        for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
            try:
                with open(filepath, "r", encoding=enc, errors="replace") as f:
                    sample = "".join(f.readline() for _ in range(5))
                sep = ","
                for test_sep in [",", "\t", ";", "|"]:
                    if test_sep in sample:
                        sep = test_sep
                        break
                df = pd.read_csv(filepath, sep=sep, encoding=enc)
                break
            except Exception:
                continue

        # If pandas parsing failed, fallback to plain text line reading
        if df is None or df.empty:
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    lines = [_clean_location_val(line) for line in f]
                    return [line for line in lines if line]
            except Exception:
                pass

    elif ext in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(filepath, engine="openpyxl" if ext == ".xlsx" else "xlrd")
        except Exception:
            try:
                df = pd.read_excel(filepath)
            except Exception as e:
                raise ValueError(f"Could not read Excel file: {e}")
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    if df is None or df.empty:
        return []

    # Map normalized column names
    col_map = {_normalize_location_col(col): col for col in df.columns}

    # Case 0: Area Explorer / Polygon Excel Format (Area Name + Parent Area / Polygon Status)
    area_name_col = next((col for col in df.columns if _normalize_location_col(col) in ("area_name", "area")), None)
    parent_area_col = next((col for col in df.columns if _normalize_location_col(col) in ("parent_area_sub_locality", "parent_area", "sub_locality", "parent")), None)
    status_col = next((col for col in df.columns if _normalize_location_col(col) in ("polygon_status", "status")), None)

    if area_name_col is not None and (parent_area_col is not None or status_col is not None):
        target_df = df
        if filter_status and status_col:
            allowed = {s.upper() for s in filter_status}
            target_df = df[df[status_col].astype(str).str.upper().isin(allowed)]

        # Determine default city context from filepath, parent column, or rows
        default_city = ""
        file_lower = os.path.basename(filepath).lower()
        if "delhi" in file_lower:
            default_city = "Delhi"
        elif "mumbai" in file_lower:
            default_city = "Mumbai"
        elif "bengaluru" in file_lower or "bangalore" in file_lower:
            default_city = "Bengaluru"
        else:
            if parent_area_col:
                parents_str = " ".join(df[parent_area_col].dropna().astype(str).str.lower())
                if "delhi" in parents_str:
                    default_city = "Delhi"

        results = []
        for _, row in target_df.iterrows():
            nm = _clean_location_val(row.get(area_name_col))
            if not nm:
                continue
            pr = _clean_location_val(row.get(parent_area_col)) if parent_area_col else ""
            q = format_block_query(nm, parent=pr, default_city=default_city)
            if q:
                results.append(q)
        return results

    # Find specific column roles
    locality_col = next((col_map[c] for c in LOCALITY_COLUMNS if c in col_map), None)
    district_col = next((col_map[c] for c in DISTRICT_COLUMNS if c in col_map), None)
    city_col = next((col_map[c] for c in CITY_COLUMNS if c in col_map), None)
    state_col = next((col_map[c] for c in STATE_COLUMNS if c in col_map), None)
    pincode_col = next((col_map[c] for c in PINCODE_COLUMNS if c in col_map), None)
    loc_col = next((col_map[c] for c in KNOWN_LOCATION_COLUMNS if c in col_map), None)

    results = []

    # If df has 1 column and its name is not a standard header keyword (first row was data!)
    if len(df.columns) == 1 and loc_col is None:
        first_col_raw = df.columns[0]
        first_col_norm = _normalize_location_col(first_col_raw)
        if first_col_norm not in KNOWN_LOCATION_COLUMNS and first_col_norm not in INDEX_COLUMNS and not first_col_norm.startswith("unnamed"):
            hdr_val = _clean_location_val(first_col_raw)
            if hdr_val:
                results.append(hdr_val)
        for v in df[first_col_raw]:
            val = _clean_location_val(v)
            if val:
                results.append(val)
        return results

    # Case 1: Locality column is present (multi-column locality format e.g. Locality, District, State)
    if locality_col is not None:
        parent_col = district_col or city_col
        if parent_col == locality_col:
            parent_col = None
        if state_col == locality_col:
            state_col = None

        for _, row in df.iterrows():
            loc_val = _clean_location_val(row.get(locality_col))
            if not loc_val:
                continue
            parts = [loc_val]
            if parent_col:
                p_val = _clean_location_val(row.get(parent_col))
                if p_val and not _has_location_token(loc_val, p_val):
                    parts.append(p_val)
            if state_col:
                s_val = _clean_location_val(row.get(state_col))
                if s_val and not _has_location_token(", ".join(parts), s_val):
                    parts.append(s_val)
            results.append(", ".join(parts))
        return results

    # Case 2: District or City column is present with or without State
    if district_col is not None or city_col is not None:
        primary_col = district_col if district_col is not None else city_col
        for _, row in df.iterrows():
            val = _clean_location_val(row.get(primary_col))
            if not val:
                continue
            if state_col and state_col != primary_col:
                s_val = _clean_location_val(row.get(state_col))
                if s_val and not _has_location_token(val, s_val):
                    results.append(f"{val}, {s_val}")
                else:
                    results.append(val)
            else:
                results.append(val)
        return results

    # Case 3: Pincode column is present
    if pincode_col is not None:
        for v in df[pincode_col]:
            val = _clean_location_val(v)
            if val:
                results.append(val)
        return results

    # Case 4: Any other recognized location column (e.g. query, location, place, address, boundary, name)
    if loc_col is not None:
        for _, row in df.iterrows():
            loc_val = _clean_location_val(row.get(loc_col))
            if not loc_val:
                continue
            if state_col and state_col != loc_col:
                state_val = _clean_location_val(row.get(state_col))
                if state_val and not _has_location_token(loc_val, state_val):
                    results.append(f"{loc_val}, {state_val}")
                else:
                    results.append(loc_val)
            else:
                results.append(loc_val)
        return results

    # Case 5: State column alone
    if state_col is not None:
        for v in df[state_col]:
            val = _clean_location_val(v)
            if val:
                results.append(val)
        return results

    # Fallback: Find first non-index column
    usable_cols = [
        col for col in df.columns
        if _normalize_location_col(col) not in INDEX_COLUMNS and not str(col).startswith("Unnamed:")
    ]
    target_col = usable_cols[0] if usable_cols else df.columns[0]
    if _normalize_location_col(target_col) in INDEX_COLUMNS and len(df.columns) > 1:
        target_col = df.columns[1]

    for v in df[target_col]:
        val = _clean_location_val(v)
        if val:
            results.append(val)

    return results






class MultiSelectDropdown(tk.Frame):
    """Compact checkbox dropdown with Select All / Clear and scrolling."""
    def __init__(self, parent, values=None, default_selected=None, width=32,
                 font=FONT_NORMAL, bg=BG_CARD, button_bg=BG_INPUT,
                 fg=TEXT_WHITE, command=None):
        super().__init__(parent, bg=bg)
        self.values = list(values or [])
        self.selected = set(default_selected or [])
        self.width = width
        self.font = font
        self.bg_color = bg
        self.button_bg = button_bg
        self.fg = fg
        self.command = command
        self._popup = None
        self._vars = {}
        self.button = tk.Button(
            self, text=self._display_text(), font=font, bg=button_bg, fg=fg,
            activebackground=button_bg, activeforeground=fg, relief="flat",
            anchor="w", cursor="hand2", padx=8, pady=6, command=self._toggle_popup
        )
        self.button.pack(fill="x")

    def set_values(self, values, selected=None):
        self.values = list(dict.fromkeys(values or []))
        if selected is None:
            self.selected = {v for v in self.selected if v in self.values}
        else:
            self.selected = {v for v in selected if v in self.values}
        self._refresh_button()
        if self._popup and self._popup.winfo_exists():
            self._build_popup_contents()

    def get_selected(self):
        return [v for v in self.values if v in self.selected]

    def select_all(self):
        self.selected = set(self.values)
        self._refresh_button()
        self._sync_vars()
        self._notify()

    def clear(self):
        self.selected.clear()
        self._refresh_button()
        self._sync_vars()
        self._notify()

    def _display_text(self):
        selected = self.get_selected()
        if not selected:
            return "Select..."
        if len(selected) == len(self.values) and selected:
            return f"All ({len(selected)}) selected"
        if len(selected) <= 2:
            return ", ".join(selected)
        return f"{len(selected)} selected"

    def _refresh_button(self):
        if hasattr(self, "button"):
            self.button.config(text=self._display_text())

    def _toggle_popup(self):
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
            self._popup = None
            return
        self._popup = tk.Toplevel(self)
        self._popup.title("Select")
        self._popup.configure(bg=self.bg_color)
        self._popup.transient(self.winfo_toplevel())
        self._popup.resizable(False, False)
        self._popup.protocol("WM_DELETE_WINDOW", self._close_popup)
        # Fixed popup size: the checklist area is deliberately smaller than
        # the full popup so the vertical scrollbar is always available when
        # there are more items than can fit on screen.
        popup_w = max(self.winfo_width(), 320)
        popup_h = 270
        self._popup.geometry(f"{popup_w}x{popup_h}")
        self._build_popup_contents()
        self.update_idletasks()

        x = self.winfo_rootx()
        below_y = self.winfo_rooty() + self.winfo_height() + 2
        above_y = self.winfo_rooty() - popup_h - 2
        screen_h = self.winfo_screenheight()
        y = below_y if below_y + popup_h <= screen_h else max(0, above_y)
        self._popup.geometry(f"{popup_w}x{popup_h}+{x}+{y}")

    def _build_popup_contents(self):
        if not self._popup or not self._popup.winfo_exists():
            return
        for child in self._popup.winfo_children():
            child.destroy()

        top = tk.Frame(self._popup, bg=self.bg_color)
        top.pack(fill="x", padx=8, pady=8)
        tk.Button(top, text="☑ Select All", font=FONT_SMALL, bg="#6c3483", fg="white",
                  relief="flat", cursor="hand2", command=self.select_all).pack(side="left", padx=(0, 6))
        tk.Button(top, text="☐ Clear", font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_WHITE,
                  relief="flat", cursor="hand2", command=self.clear).pack(side="left")

        # Fixed-height checklist viewport.  This is intentional: it prevents
        # the Toplevel from growing with the number of checkboxes.
        holder = tk.Frame(self._popup, bg=self.bg_color, height=195)
        holder.pack(fill="x", expand=False, padx=8, pady=(0, 8))
        holder.pack_propagate(False)

        canvas = tk.Canvas(holder, bg=self.bg_color, highlightthickness=0, bd=0,
                           width=max(self.winfo_width() - 40, 260), height=195)
        scrollbar = tk.Scrollbar(holder, orient="vertical", width=14, command=canvas.yview)
        inner = tk.Frame(canvas, bg=self.bg_color)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        # Mouse-wheel scrolling while the pointer is over the checklist.
        def _wheel(event):
            if event.delta:
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        canvas.bind("<MouseWheel>", _wheel)
        canvas.bind("<Button-4>", lambda e: (canvas.yview_scroll(-1, "units"), "break")[1])
        canvas.bind("<Button-5>", lambda e: (canvas.yview_scroll(1, "units"), "break")[1])

        self._vars = {}
        for value in self.values:
            var = tk.BooleanVar(value=value in self.selected)
            self._vars[value] = var
            tk.Checkbutton(
                inner, text=value, variable=var, font=self.font,
                bg=self.bg_color, fg=self.fg, selectcolor=BG_INPUT,
                activebackground=self.bg_color, activeforeground=self.fg,
                anchor="w", command=lambda v=value: self._toggle_value(v)
            ).pack(fill="x", padx=4, pady=1)

    def _toggle_value(self, value):
        if self._vars[value].get():
            self.selected.add(value)
        else:
            self.selected.discard(value)
        self._refresh_button()
        self._notify()

    def _sync_vars(self):
        for value, var in self._vars.items():
            var.set(value in self.selected)

    def _notify(self):
        if self.command:
            self.command()

    def _close_popup(self):
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
        self._popup = None


# ─────────────────────────────────────────────
#  AREA BOUNDARY VALIDATOR DIALOG
# ─────────────────────────────────────────────

class BoundaryValidatorDialog:
    """
    Dedicated dialog to validate Google Maps red polygon boundaries for Area Explorer data.
    """
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("🛡️ Google Maps Boundary Validator & Filter — Jarvis Maps AI")

        # Center dialog
        w, h = 1060, 720
        sw = self.dialog.winfo_screenwidth()
        sh = self.dialog.winfo_screenheight()
        x = max(0, int((sw - w) / 2))
        y = max(0, int((sh - h) / 2))
        self.dialog.geometry(f"{w}x{h}+{x}+{y}")
        self.dialog.minsize(920, 600)
        self.dialog.configure(bg=BG_DARK)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        has_active = bool(getattr(app, "area_results", None))
        self.source_mode_var = tk.StringVar(value="active" if has_active else "file")
        self.file_path_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=os.path.join(get_downloads_dir(), "JarvisMaps_Validated_Areas"))
        self.filter_var = tk.StringVar(value="all")
        self.speed_mode_var = tk.StringVar(value="fast")
        self.workers_var = tk.IntVar(value=3)

        self.loaded_records = []
        self.validator_engine = None
        self.is_validating = False
        self.item_durations = []
        self.start_time = None
        self.current_testing_name = ""
        self._timer_job = None

        self._build_ui()
        self._load_initial_source()
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_close)

    def _format_duration(self, seconds: float) -> str:
        s = max(0, int(seconds))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m:02d}m {s:02d}s"
        elif m > 0:
            return f"{m}m {s:02d}s"
        else:
            return f"{s}s"

    def _build_ui(self):
        # 1. Header Banner
        header = tk.Frame(self.dialog, bg=BG_DARK, padx=24, pady=14)
        header.pack(fill="x")

        tk.Label(
            header,
            text="🛡️  Google Maps Boundary Validator & Filter",
            font=FONT_TITLE,
            bg=BG_DARK,
            fg=TEXT_WHITE
        ).pack(anchor="w")

        tk.Label(
            header,
            text="Verify which extracted areas actually have official red outline boundaries in Google Maps and filter or export verified datasets.",
            font=FONT_SUB,
            bg=BG_DARK,
            fg=TEXT_GRAY
        ).pack(anchor="w", pady=(2, 0))

        # 2. Settings Card (Source & Output Dir)
        opt_card = tk.Frame(self.dialog, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=14)
        opt_card.pack(fill="x", padx=24, pady=(0, 12))

        # Source Selection Row
        src_row = tk.Frame(opt_card, bg=BG_CARD)
        src_row.pack(fill="x", pady=(0, 10))

        tk.Label(src_row, text="Source Data:", font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left", padx=(0, 16))

        active_cnt = len(getattr(self.app, "area_results", []))
        self.rb_active = tk.Radiobutton(
            src_row,
            text=f"Active Area Explorer Results ({active_cnt} areas)",
            value="active",
            variable=self.source_mode_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=self._on_source_change
        )
        self.rb_active.pack(side="left", padx=(0, 16))

        self.rb_file = tk.Radiobutton(
            src_row,
            text="Upload Area Explorer File (.xlsx, .csv, .txt)",
            value="file",
            variable=self.source_mode_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=self._on_source_change
        )
        self.rb_file.pack(side="left")

        # File & Output Directory Row
        path_row = tk.Frame(opt_card, bg=BG_CARD)
        path_row.pack(fill="x")

        # File selector frame
        self.file_frame = tk.Frame(path_row, bg=BG_CARD)
        self.file_frame.pack(side="left", fill="x", expand=True, padx=(0, 12))

        tk.Label(self.file_frame, text="File Path:", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")
        f_sub = tk.Frame(self.file_frame, bg=BG_CARD)
        f_sub.pack(fill="x", pady=(2, 0))

        self.file_entry = tk.Entry(
            f_sub,
            textvariable=self.file_path_var,
            font=FONT_NORMAL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BG_INPUT,
            highlightcolor=ACCENT
        )
        self.file_entry.pack(side="left", fill="x", expand=True, ipady=5)

        self.file_btn = tk.Button(
            f_sub,
            text="📂 Browse...",
            font=FONT_SMALL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=4,
            command=self._on_browse_file
        )
        self.file_btn.pack(side="left", padx=(6, 0))

        # Output Dir frame
        out_frame = tk.Frame(path_row, bg=BG_CARD)
        out_frame.pack(side="left", fill="x", expand=True)

        tk.Label(out_frame, text="📁 Save Boundaries to:", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")
        o_sub = tk.Frame(out_frame, bg=BG_CARD)
        o_sub.pack(fill="x", pady=(2, 0))

        self.out_entry = tk.Entry(
            o_sub,
            textvariable=self.output_dir_var,
            font=FONT_NORMAL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BG_INPUT,
            highlightcolor=ACCENT
        )
        self.out_entry.pack(side="left", fill="x", expand=True, ipady=5)

        self.out_btn = tk.Button(
            o_sub,
            text="Browse...",
            font=FONT_SMALL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=4,
            command=self._on_browse_output_dir
        )
        self.out_btn.pack(side="left", padx=(6, 0))

        # Browser Visibility Mode (Headless Toggle)
        mode_row = tk.Frame(opt_card, bg=BG_CARD)
        mode_row.pack(fill="x", pady=(10, 0))

        self.headless_var = tk.BooleanVar(value=False)

        def _update_headless_hint():
            if self.headless_var.get():
                self.lbl_headless_hint.config(
                    text="⚡ Headless ON: Browser runs silently in background",
                    fg="#f59e0b"
                )
            else:
                self.lbl_headless_hint.config(
                    text="👁️ Headless OFF: Browser window will open visibly on your screen",
                    fg="#10b981"
                )

        self.cb_headless = tk.Checkbutton(
            mode_row,
            text="Headless Mode (Hide Browser Window)",
            variable=self.headless_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=_update_headless_hint
        )
        self.cb_headless.pack(side="left", padx=(0, 12))

        self.lbl_headless_hint = tk.Label(
            mode_row,
            text="👁️ Headless OFF: Browser window will open visibly on your screen",
            font=FONT_SMALL,
            bg=BG_CARD,
            fg="#10b981"
        )
        self.lbl_headless_hint.pack(side="left")

        # Speed Mode & Parallel Workers Row
        speed_row = tk.Frame(opt_card, bg=BG_CARD)
        speed_row.pack(fill="x", pady=(10, 0))

        tk.Label(speed_row, text="⚡ Speed Mode:", font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left", padx=(0, 8))

        def _on_speed_setting_change(*_):
            self._update_metric_cards()

        self.rb_fast = tk.Radiobutton(
            speed_row,
            text="⚡ Turbo Fast (~1-2s / area)",
            value="fast",
            variable=self.speed_mode_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg="#38bdf8",
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground="#38bdf8",
            command=_on_speed_setting_change
        )
        self.rb_fast.pack(side="left", padx=(0, 10))

        self.rb_deep = tk.Radiobutton(
            speed_row,
            text="🔬 Deep GIS Trace (~15-20s)",
            value="deep",
            variable=self.speed_mode_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=_on_speed_setting_change
        )
        self.rb_deep.pack(side="left", padx=(0, 20))

        tk.Label(speed_row, text="⚡ Parallel Workers:", font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left", padx=(0, 8))

        self.cb_workers = ttk.Combobox(
            speed_row,
            textvariable=self.workers_var,
            values=[1, 2, 3, 4, 5, 6],
            width=3,
            state="readonly"
        )
        self.cb_workers.pack(side="left", padx=(0, 6))
        self.cb_workers.bind("<<ComboboxSelected>>", lambda e: self._update_metric_cards())

        tk.Label(speed_row, text="(3-5x faster parallel validation)", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left")

        # 3. Metric Cards Bar
        metrics_frame = tk.Frame(self.dialog, bg=BG_DARK, padx=24)
        metrics_frame.pack(fill="x", pady=(0, 10))

        def _make_metric_card(parent_f, title, default_val, text_color, sub_text=""):
            card = tk.Frame(parent_f, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=14, pady=8)
            card.pack(side="left", fill="x", expand=True, padx=4)
            tk.Label(card, text=title, font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")
            lbl = tk.Label(card, text=default_val, font=("Segoe UI", 15, "bold"), bg=BG_CARD, fg=text_color)
            lbl.pack(anchor="w")
            sub_lbl = tk.Label(card, text=sub_text, font=("Segoe UI", 8), bg=BG_CARD, fg=TEXT_GRAY)
            sub_lbl.pack(anchor="w")
            return lbl, sub_lbl

        self.lbl_total_cnt, self.lbl_total_sub = _make_metric_card(metrics_frame, "Total Areas", "0", TEXT_WHITE, "")
        self.lbl_tested_cnt, self.lbl_tested_sub = _make_metric_card(metrics_frame, "Tested", "0 / 0", "#38bdf8", "")
        self.lbl_valid_cnt, self.lbl_valid_sub = _make_metric_card(metrics_frame, "✅ Valid Boundaries", "0", "#10b981", "")
        self.lbl_invalid_cnt, self.lbl_invalid_sub = _make_metric_card(metrics_frame, "❌ No Boundary", "0", "#ef4444", "")
        self.lbl_time_val, self.lbl_time_sub = _make_metric_card(metrics_frame, "⏱ Expected Time", "--", "#fbbf24", "~15s / area")

        # 4. Progress bar & status
        prog_frame = tk.Frame(self.dialog, bg=BG_DARK, padx=28)
        prog_frame.pack(fill="x", pady=(0, 10))

        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(0, 4))

        self.lbl_progress_text = tk.Label(
            prog_frame,
            text="Ready to validate. Click 'Start Validation' to begin.",
            font=FONT_SMALL,
            bg=BG_DARK,
            fg=TEXT_GRAY
        )
        self.lbl_progress_text.pack(anchor="w")

        # 5. Bottom Action Toolbar (Pinned to bottom so it's always visible)
        action_bar = tk.Frame(self.dialog, bg=BG_DARK, padx=24, pady=12)
        action_bar.pack(side="bottom", fill="x")

        # Left controls
        self.btn_start = tk.Button(
            action_bar,
            text="▶  Start Validation",
            font=FONT_BTN,
            bg="#10b981",
            fg="white",
            relief="flat",
            cursor="hand2",
            padx=18,
            pady=8,
            command=self._start_validation
        )
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_stop = tk.Button(
            action_bar,
            text="⏸  Stop",
            font=FONT_BTN,
            bg="#ef4444",
            fg="white",
            relief="flat",
            cursor="hand2",
            padx=14,
            pady=8,
            state="disabled",
            command=self._stop_validation
        )
        self.btn_stop.pack(side="left")

        # Right controls
        self.btn_open_folder = tk.Button(
            action_bar,
            text="📁 Open Folder",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_GRAY,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=8,
            command=self._open_output_folder
        )
        self.btn_open_folder.pack(side="right", padx=(6, 0))

        self.btn_export_kml = tk.Button(
            action_bar,
            text="🗺️ Export KML",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=8,
            command=self._export_kml
        )
        self.btn_export_kml.pack(side="right", padx=(6, 0))

        self.btn_export_excel = tk.Button(
            action_bar,
            text="📊 Export Excel",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=8,
            command=self._export_excel
        )
        self.btn_export_excel.pack(side="right", padx=(6, 0))

        self.btn_send_bnd = tk.Button(
            action_bar,
            text="📥 Send to Boundary Tracer",
            font=FONT_BTN,
            bg="#3b82f6",
            fg="white",
            relief="flat",
            cursor="hand2",
            padx=14,
            pady=8,
            command=self._send_to_tracer
        )
        self.btn_send_bnd.pack(side="right", padx=(6, 0))

        # 6. Results Table Card (Expands in remaining space)
        table_card = tk.Frame(self.dialog, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=16, pady=12)
        table_card.pack(fill="both", expand=True, padx=24, pady=(0, 12))

        # Filter bar inside table card
        filt_bar = tk.Frame(table_card, bg=BG_CARD)
        filt_bar.pack(fill="x", pady=(0, 8))

        tk.Label(filt_bar, text="Filter Results:", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(0, 10))

        for f_label, f_val in [
            ("All", "all"),
            ("✅ Valid Boundaries Only", "valid"),
            ("❌ No Boundary Only", "invalid"),
        ]:
            tk.Radiobutton(
                filt_bar,
                text=f_label,
                value=f_val,
                variable=self.filter_var,
                font=FONT_SMALL,
                bg=BG_CARD,
                fg=TEXT_WHITE,
                selectcolor=BG_INPUT,
                activebackground=BG_CARD,
                activeforeground=TEXT_WHITE,
                command=self._populate_treeview
            ).pack(side="left", padx=(0, 12))

        self.lbl_table_summary = tk.Label(filt_bar, text="0 items", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY)
        self.lbl_table_summary.pack(side="right")

        # Treeview
        tree_frame = tk.Frame(table_card, bg=BG_CARD)
        tree_frame.pack(fill="both", expand=True)

        sc_y = ttk.Scrollbar(tree_frame, orient="vertical")
        sc_y.pack(side="right", fill="y")
        sc_x = ttk.Scrollbar(tree_frame, orient="horizontal")
        sc_x.pack(side="bottom", fill="x")

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("idx", "name", "parent", "type", "query", "status", "points", "source"),
            show="headings",
            selectmode="extended",
            yscrollcommand=sc_y.set,
            xscrollcommand=sc_x.set,
            style="Amenity.Treeview"
        )
        sc_y.config(command=self.tree.yview)
        sc_x.config(command=self.tree.xview)

        self.tree.heading("idx", text="#")
        self.tree.heading("name", text="Area Name")
        self.tree.heading("parent", text="Parent Area / City")
        self.tree.heading("type", text="Type")
        self.tree.heading("query", text="Google Maps Query")
        self.tree.heading("status", text="Boundary Status")
        self.tree.heading("points", text="Points")
        self.tree.heading("source", text="Details / Source")

        self.tree.column("idx", width=45, anchor="center")
        self.tree.column("name", width=170, anchor="w")
        self.tree.column("parent", width=130, anchor="w")
        self.tree.column("type", width=90, anchor="center")
        self.tree.column("query", width=180, anchor="w")
        self.tree.column("status", width=140, anchor="center")
        self.tree.column("points", width=65, anchor="center")
        self.tree.column("source", width=140, anchor="w")

        self.tree.tag_configure("valid", foreground="#10b981")
        self.tree.tag_configure("invalid", foreground="#ef4444")
        self.tree.tag_configure("pending", foreground="#94a3b8")
        self.tree.tag_configure("testing", foreground="#38bdf8")

        self.tree.pack(fill="both", expand=True)

    def _load_initial_source(self):
        if self.source_mode_var.get() == "active" and getattr(self.app, "area_results", None):
            self.loaded_records = load_area_records(self.app.area_results)
            self._populate_treeview()
            self._update_metric_cards()
        self._on_source_change()

    def _on_source_change(self):
        mode = self.source_mode_var.get()
        if mode == "file":
            self.file_entry.config(state="normal")
            self.file_btn.config(state="normal")
            if self.file_path_var.get():
                self._load_file_records(self.file_path_var.get())
        else:
            self.file_entry.config(state="disabled")
            self.file_btn.config(state="disabled")
            self.loaded_records = load_area_records(getattr(self.app, "area_results", []))
            self._populate_treeview()
            self._update_metric_cards()

    def _on_browse_file(self):
        file_path = filedialog.askopenfilename(
            parent=self.dialog,
            title="Select Area Explorer Export File",
            initialdir=get_downloads_dir(),
            filetypes=[
                ("Supported Files", "*.xlsx;*.xls;*.csv;*.tsv;*.txt"),
                ("Excel Files", "*.xlsx;*.xls"),
                ("CSV Files", "*.csv;*.tsv"),
                ("Text Files", "*.txt"),
            ]
        )
        if not file_path:
            return
        self.file_path_var.set(file_path)
        self._load_file_records(file_path)

    def _load_file_records(self, file_path):
        try:
            records = load_area_records(file_path)
            if not records:
                messagebox.showwarning("Empty File", "No valid area records found in selected file.", parent=self.dialog)
                return
            self.loaded_records = records
            self._populate_treeview()
            self._update_metric_cards()
            self.lbl_progress_text.config(
                text=f"Loaded {len(records)} areas from {os.path.basename(file_path)}. Ready to validate."
            )
        except Exception as e:
            messagebox.showerror("Read Error", f"Failed to read file:\n{e}", parent=self.dialog)

    def _on_browse_output_dir(self):
        folder = filedialog.askdirectory(
            parent=self.dialog,
            title="Select Boundaries Output Directory",
            initialdir=self.output_dir_var.get()
        )
        if folder:
            self.output_dir_var.set(folder)

    def _populate_treeview(self):
        filt = self.filter_var.get()
        for item in self.tree.get_children():
            self.tree.delete(item)

        disp_count = 0
        for i, r in enumerate(self.loaded_records, start=1):
            has_bnd = r.get("has_boundary")
            if filt == "valid" and has_bnd is not True:
                continue
            if filt == "invalid" and has_bnd is not False:
                continue

            disp_count += 1
            status_str = r.get("status", "Pending")
            b_info = r.get("boundary_info") or {}
            pts = _format_points_count(b_info.get("polygon_points")) if has_bnd else "-"
            src = b_info.get("source") or b_info.get("error") or "-"

            tag = "pending"
            if has_bnd is True:
                tag = "valid"
            elif has_bnd is False:
                tag = "invalid"
            elif "Testing" in status_str:
                tag = "testing"

            self.tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    str(i),
                    r.get("name", ""),
                    r.get("parent_area", ""),
                    r.get("type", "Area"),
                    r.get("query", ""),
                    status_str,
                    pts,
                    src[:35] if src else "-",
                ),
                tags=(tag,)
            )

        self.lbl_table_summary.config(text=f"Showing {disp_count} of {len(self.loaded_records)} areas")

    def _update_metric_cards(self):
        total = len(self.loaded_records)
        tested = sum(1 for r in self.loaded_records if r.get("has_boundary") is not None)
        valid = sum(1 for r in self.loaded_records if r.get("has_boundary") is True)
        invalid = sum(1 for r in self.loaded_records if r.get("has_boundary") is False)

        self.lbl_total_cnt.config(text=str(total))
        self.lbl_tested_cnt.config(text=f"{tested} / {total}")
        self.lbl_valid_cnt.config(text=str(valid))
        self.lbl_invalid_cnt.config(text=str(invalid))

        # Expected / remaining time calculation
        remaining = max(0, total - tested)
        mode = self.speed_mode_var.get()
        workers = max(1, self.workers_var.get())
        default_rate = (2.0 / workers) if mode == "fast" else 15.0
        avg_time = (sum(self.item_durations) / len(self.item_durations)) if self.item_durations else default_rate

        if not self.is_validating and tested == 0:
            initial_est = (total / workers * 2.0) if mode == "fast" else (total * 15.0)
            sub_hint = f"⚡ ~{default_rate:.1f}s/area ({workers} workers)" if mode == "fast" else "~15s per area"
            self.lbl_time_val.config(text=f"~{self._format_duration(initial_est)}" if total > 0 else "--")
            self.lbl_time_sub.config(text=sub_hint if total > 0 else "")
            if total > 0:
                self.lbl_progress_text.config(
                    text=f"Ready to validate {total} areas in {'⚡ Turbo Fast' if mode == 'fast' else '🔬 Deep'} mode. Expected completion time: ~{self._format_duration(initial_est)} ({sub_hint}). Click 'Start Validation' to begin."
                )
        elif not self.is_validating and tested == total and total > 0:
            total_elapsed = time.time() - (self.start_time or time.time())
            self.lbl_time_val.config(text=self._format_duration(total_elapsed))
            self.lbl_time_sub.config(text="Completed in full")
        else:
            est_remaining = max(0, remaining * avg_time)
            self.lbl_time_val.config(text=f"~{self._format_duration(est_remaining)}")

        pct = (tested / total * 100) if total > 0 else 0
        self.progress_bar["value"] = pct

    def _start_ticker(self):
        self._stop_ticker()
        self._tick_timer()

    def _tick_timer(self):
        if not self.is_validating:
            return

        elapsed = time.time() - (self.start_time or time.time())
        tested = sum(1 for r in self.loaded_records if r.get("has_boundary") is not None)
        total = len(self.loaded_records)
        remaining = max(0, total - tested)
        avg_time = (sum(self.item_durations) / len(self.item_durations)) if self.item_durations else 15.0
        est_remaining = max(0, remaining * avg_time)

        self.lbl_time_val.config(text=f"~{self._format_duration(est_remaining)}")
        self.lbl_time_sub.config(text=f"Elapsed: {self._format_duration(elapsed)}")

        curr = self.current_testing_name or "Validating..."
        self.lbl_progress_text.config(
            text=f"Testing [{min(tested+1, total)}/{total}] {curr} | "
                 f"⏱ Remaining: ~{self._format_duration(est_remaining)} | "
                 f"Elapsed: {self._format_duration(elapsed)} (~{avg_time:.1f}s/area)"
        )

        self._timer_job = self.dialog.after(1000, self._tick_timer)

    def _stop_ticker(self):
        if getattr(self, "_timer_job", None):
            try:
                self.dialog.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None

    def _start_validation(self):
        if not self.loaded_records:
            messagebox.showwarning("No Areas", "Please search areas in Area Explorer or load a file first.", parent=self.dialog)
            return

        out_dir = self.output_dir_var.get().strip() or "boundaries/validated_areas"
        os.makedirs(out_dir, exist_ok=True)

        self.is_validating = True
        self.start_time = time.time()
        self.item_durations = []
        self.current_testing_name = ""

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.rb_active.config(state="disabled")
        self.rb_file.config(state="disabled")
        self.file_btn.config(state="disabled")

        # Reset item status
        for r in self.loaded_records:
            r["has_boundary"] = None
            r["status"] = "Pending"
            r["boundary_info"] = {}
        self._populate_treeview()
        self._update_metric_cards()

        is_headless = self.headless_var.get()
        try:
            import playwright_pipeline
            playwright_pipeline.BOUNDARY_HEADLESS = is_headless
        except Exception:
            pass

        self.cb_headless.config(state="disabled")
        self.rb_fast.config(state="disabled")
        self.rb_deep.config(state="disabled")
        self.cb_workers.config(state="disabled")

        self.validator_engine = AreaBoundaryValidator(
            records=self.loaded_records,
            output_dir=out_dir,
            headless=is_headless,
            workers=self.workers_var.get(),
            mode=self.speed_mode_var.get(),
            on_progress=self._on_worker_progress,
            on_log=self._on_worker_log,
            on_complete=self._on_worker_complete
        )
        self.validator_engine.start()
        self._start_ticker()

    def _stop_validation(self):
        self._stop_ticker()
        if self.validator_engine:
            self.lbl_progress_text.config(text="Stopping validation after current area...")
            self.validator_engine.stop()
            self.btn_stop.config(state="disabled")

    def _on_worker_log(self, message: str, tag: str = "normal"):
        pass

    def _on_worker_progress(self, idx, total, item, stats):
        def _update():
            if 1 <= idx <= len(self.loaded_records):
                rec = self.loaded_records[idx - 1]
                rec["has_boundary"] = item.get("has_boundary")
                rec["status"] = item.get("status")
                rec["boundary_info"] = item.get("boundary_info")

                elapsed_item = stats.get("elapsed", 15.0)
                self.item_durations.append(elapsed_item)
                self.current_testing_name = item.get("name", "")

                if self.tree.exists(str(idx)):
                    has_bnd = rec.get("has_boundary")
                    b_info = rec.get("boundary_info") or {}
                    pts = _format_points_count(b_info.get("polygon_points")) if has_bnd else "-"
                    src = b_info.get("source") or b_info.get("error") or "-"
                    tag = "valid" if has_bnd is True else ("invalid" if has_bnd is False else "pending")
                    self.tree.item(
                        str(idx),
                        values=(
                            str(idx),
                            rec.get("name", ""),
                            rec.get("parent_area", ""),
                            rec.get("type", "Area"),
                            rec.get("query", ""),
                            rec.get("status", ""),
                            pts,
                            src[:35] if src else "-",
                        ),
                        tags=(tag,)
                    )

            self._update_metric_cards()

        try:
            self.dialog.after(0, _update)
        except Exception:
            pass

    def _on_worker_complete(self, summary):
        def _done():
            self._stop_ticker()
            self.is_validating = False
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.rb_active.config(state="normal")
            self.rb_file.config(state="normal")
            self.file_btn.config(state="normal")
            self.cb_headless.config(state="normal")
            self.rb_fast.config(state="normal")
            self.rb_deep.config(state="normal")
            self.cb_workers.config(state="normal")

            total_elapsed = time.time() - (self.start_time or time.time())
            self.loaded_records = summary.get("records", self.loaded_records)
            self._populate_treeview()
            self._update_metric_cards()

            val_cnt = summary.get("validated", 0)
            no_bnd_cnt = summary.get("no_boundary", 0)
            self.lbl_time_val.config(text=self._format_duration(total_elapsed))
            self.lbl_time_sub.config(text="Completed in full")
            self.lbl_progress_text.config(
                text=f"🏁 Validation finished in {self._format_duration(total_elapsed)}: {val_cnt} confirmed boundaries, {no_bnd_cnt} without boundary."
            )

            if self.source_mode_var.get() == "active" and getattr(self.app, "area_results", None):
                self._sync_back_to_app()

            messagebox.showinfo(
                "Validation Complete",
                f"Boundary Validation Finished in {self._format_duration(total_elapsed)}!\n\n"
                f"✅ Confirmed Boundaries: {val_cnt}\n"
                f"❌ No Boundary Outlines: {no_bnd_cnt}\n\n"
                f"You can now export validated datasets (Excel, KML) or send confirmed locations directly to Boundary Tracer!",
                parent=self.dialog
            )

        try:
            self.dialog.after(0, _done)
        except Exception:
            pass

    def _sync_back_to_app(self):
        """Sync validated status into the main Area Explorer results and treeview."""
        if not getattr(self.app, "area_results", None):
            return

        lookup = {r.get("name", "").casefold(): r for r in self.loaded_records}
        for item in self.app.area_results:
            name_key = item.get("name", "").casefold()
            if name_key in lookup:
                matched = lookup[name_key]
                item["has_boundary"] = matched.get("has_boundary")
                item["status"] = matched.get("status")
                item["boundary_info"] = matched.get("boundary_info")

        if hasattr(self.app, "_area_apply_filter"):
            self.app._area_apply_filter()

    def _send_to_tracer(self):
        validated_items = [r for r in self.loaded_records if r.get("has_boundary") is True]
        if not validated_items:
            messagebox.showwarning(
                "No Validated Boundaries",
                "No areas have confirmed Google Maps boundaries yet. Run validation first.",
                parent=self.dialog
            )
            return

        added_count = 0
        existing = {q.strip().casefold() for q in self.app.bnd_queries}
        for item in validated_items:
            query = item.get("query") or build_boundary_query(item.get("name", ""), item.get("parent_area", ""))
            if query and query.strip().casefold() not in existing:
                self.app.bnd_queries.append(query.strip())
                existing.add(query.strip().casefold())
                added_count += 1

        self.app._bnd_update_queue_display()
        try:
            self.app.notebook.select(1)
        except Exception:
            pass

        self.app._bnd_log(
            f"📥 Imported {added_count} validated area boundaries from Validator into Boundary Tracer.\n",
            "info"
        )
        messagebox.showinfo(
            "Transferred to Tracer",
            f"Successfully transferred {added_count} verified area boundaries to Boundary Tracer queue!",
            parent=self.dialog
        )

    def _export_excel(self):
        if not self.loaded_records:
            return
        val_items = [r for r in self.loaded_records if r.get("has_boundary") is True]
        only_val = True
        if not val_items:
            tested_items = [r for r in self.loaded_records if r.get("has_boundary") is not None]
            if not tested_items:
                messagebox.showwarning("No Data", "Please run boundary validation before exporting.", parent=self.dialog)
                return
            if not messagebox.askyesno(
                "No Confirmed Boundaries",
                "No locations have confirmed Google Maps boundaries yet. Export all tested rows anyway?",
                parent=self.dialog
            ):
                return
            only_val = False

        file_path = filedialog.asksaveasfilename(
            parent=self.dialog,
            title="Save Validated Boundaries to Excel",
            initialdir=get_downloads_dir(),
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")],
            initialfile="validated_boundaries.xlsx"
        )
        if not file_path:
            return

        try:
            export_validated_to_excel(self.loaded_records, file_path, only_validated=only_val)
            messagebox.showinfo(
                "Export Complete",
                f"Validated boundaries exported successfully to:\n{file_path}",
                parent=self.dialog
            )
        except Exception as e:
            messagebox.showerror("Export Failed", str(e), parent=self.dialog)

    def _export_kml(self):
        if not self.loaded_records:
            return
        val_items = [r for r in self.loaded_records if r.get("has_boundary") is True]
        if not val_items:
            messagebox.showwarning(
                "No Boundaries",
                "No confirmed Google Maps boundaries available to export. Run validation first.",
                parent=self.dialog
            )
            return

        file_path = filedialog.asksaveasfilename(
            parent=self.dialog,
            title="Save Combined Boundaries to KML",
            initialdir=get_downloads_dir(),
            defaultextension=".kml",
            filetypes=[("KML Map File", "*.kml")],
            initialfile="validated_boundaries.kml"
        )
        if not file_path:
            return

        try:
            export_combined_kml(self.loaded_records, file_path)
            messagebox.showinfo(
                "Export Complete",
                f"Combined KML map saved successfully to:\n{file_path}",
                parent=self.dialog
            )
        except Exception as e:
            messagebox.showerror("Export Failed", str(e), parent=self.dialog)

    def _open_output_folder(self):
        out_dir = self.output_dir_var.get().strip() or "boundaries/validated_areas"
        os.makedirs(out_dir, exist_ok=True)
        try:
            os.startfile(os.path.abspath(out_dir))
        except Exception as e:
            messagebox.showerror("Open Folder Failed", str(e), parent=self.dialog)

    def _on_close(self):
        if self.is_validating and self.validator_engine and self.validator_engine.is_running():
            if not messagebox.askyesno(
                "Validation in Progress",
                "Boundary validation is currently running. Stop validation and close?",
                parent=self.dialog
            ):
                return
            self.validator_engine.stop()

        self._stop_ticker()
        if self.source_mode_var.get() == "active":
            self._sync_back_to_app()

        self.dialog.destroy()


# ─────────────────────────────────────────────
#  SECURITY & AUTHENTICATION DIALOGS
# ─────────────────────────────────────────────

def _apply_app_icon(window):
    """Applies the custom Jarvis Maps AI icon to a Tkinter window."""
    try:
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "app_icon.ico"),
            os.path.join(getattr(sys, "_MEIPASS", ""), "assets", "app_icon.ico"),
            os.path.join(os.path.dirname(sys.executable), "assets", "app_icon.ico") if getattr(sys, "frozen", False) else None,
        ]
        for p in candidates:
            if p and os.path.isfile(p):
                window.iconbitmap(p)
                break
    except Exception:
        pass


class StartupLockDialog:
    """Enterprise Security Lock Screen with HWID fingerprinting, IP Blacklist Gatekeeper, and License Key activation."""
    def __init__(self, root, on_success):
        self.root = root
        _apply_app_icon(self.root)
        self.on_success = on_success
        self.attempts = 0
        self.max_attempts = 3
        self.authenticated = False
        self.hwid = get_machine_hwid()

        self.root.title("🛡️ Security Gateway — Jarvis Maps AI")
        self.root.configure(bg=BG_DARK)
        self.root.resizable(False, False)

        # Center on screen with comfortable dimensions
        w, h = 520, 560
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 30)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # Hidden Owner Security Console shortcut: Ctrl+Alt+R or Ctrl+Shift+R
        self.root.bind("<Control-Alt-r>", lambda e: self._prompt_owner_recovery())
        self.root.bind("<Control-Alt-R>", lambda e: self._prompt_owner_recovery())
        self.root.bind("<Control-Shift-r>", lambda e: self._prompt_owner_recovery())
        self.root.bind("<Control-Shift-R>", lambda e: self._prompt_owner_recovery())

        # Force foreground visibility
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(100, lambda: self.root.attributes("-topmost", False))

        self._build_ui()

    def _build_ui(self):
        # 1. Check for active local hardware ban
        is_banned, ban_reason = check_local_hardware_ban()
        if is_banned:
            self._render_hardware_banned_ui(ban_reason)
            return

        self.card = tk.Frame(self.root, bg=BG_CARD, padx=26, pady=24, highlightthickness=1, highlightbackground=BORDER)
        self.card.pack(fill="both", expand=True, padx=16, pady=16)

        # Header Badge
        header_row = tk.Frame(self.card, bg=BG_CARD)
        header_row.pack(fill="x", pady=(0, 10))

        tk.Label(header_row, text="🛡️", font=("Segoe UI", 26), bg=BG_CARD, fg=ACCENT).pack(side="left", padx=(0, 10))
        h_text_col = tk.Frame(header_row, bg=BG_CARD)
        h_text_col.pack(side="left", fill="x", expand=True)
        tk.Label(h_text_col, text="Jarvis Maps AI Enclave", font=("Segoe UI", 14, "bold"), bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(h_text_col, text="Hardware-Bound License & Cloud Security Gateway", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")

        # HWID Fingerprint Box
        hwid_frame = tk.Frame(self.card, bg="#0d1527", highlightthickness=1, highlightbackground="#1e293b", padx=12, pady=10)
        hwid_frame.pack(fill="x", pady=(4, 14))

        hwid_top = tk.Frame(hwid_frame, bg="#0d1527")
        hwid_top.pack(fill="x")
        tk.Label(hwid_top, text="DEVICE HARDWARE ID (HWID)", font=("Segoe UI", 8, "bold"), bg="#0d1527", fg="#38bdf8").pack(side="left")
        
        self.copy_btn = tk.Button(
            hwid_top, text="📋 Copy HWID", font=("Segoe UI", 8), bg="#1e293b", fg=TEXT_WHITE,
            activebackground=ACCENT, relief="flat", cursor="hand2", padx=8, pady=1,
            command=self._copy_hwid
        )
        self.copy_btn.pack(side="right")

        hwid_val_lbl = tk.Label(hwid_frame, text=self.hwid, font=("Consolas", 11, "bold"), bg="#0d1527", fg="#f8fafc")
        hwid_val_lbl.pack(anchor="w", pady=(4, 0))

        # Cached license status if present
        cached = get_cached_license()
        if cached:
            lic_badge = tk.Frame(self.card, bg="#052e16", highlightthickness=1, highlightbackground="#166534", padx=10, pady=5)
            lic_badge.pack(fill="x", pady=(0, 12))
            tier = cached.get("tier", "Activated License")
            tk.Label(lic_badge, text=f"✓ Cached License: {tier}", font=FONT_SMALL, bg="#052e16", fg="#4ade80").pack(anchor="w")

        # Input label & field
        tk.Label(self.card, text="Enter License Key or Master Passcode:", font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w", pady=(0, 4))
        
        entry_frame = tk.Frame(self.card, bg=BG_INPUT, highlightthickness=1, highlightbackground=BORDER)
        entry_frame.pack(fill="x", ipady=4, pady=(0, 12))

        self.pass_var = tk.StringVar()
        # Pre-fill cached key if present for convenience
        if cached and cached.get("key"):
            self.pass_var.set(cached.get("key"))

        self.pass_entry = tk.Entry(
            entry_frame, textvariable=self.pass_var, show="•", font=("Segoe UI", 11),
            bg=BG_INPUT, fg=TEXT_WHITE, relief="flat", bd=0, insertbackground=TEXT_WHITE
        )
        self.pass_entry.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=4)
        self.pass_entry.bind("<Return>", lambda e: self._submit())

        # Toggle password visibility button
        self.revealed = False
        self.toggle_btn = tk.Button(
            entry_frame, text="👁", font=("Segoe UI", 10), bg=BG_INPUT, fg=TEXT_GRAY,
            activebackground=BG_INPUT, relief="flat", cursor="hand2", bd=0, command=self._toggle_reveal
        )
        self.toggle_btn.pack(side="right", padx=(0, 8))

        # Status / Error Label
        self.err_lbl = tk.Label(self.card, text="", font=FONT_SUB, bg=BG_CARD, fg=RED, wraplength=440, justify="left")
        self.err_lbl.pack(fill="x", pady=(0, 12))

        # Action Buttons
        self.submit_btn = tk.Button(
            self.card, text="⚡ Verify & Unlock Studio", font=FONT_BTN, bg=ACCENT, fg=TEXT_WHITE,
            activebackground=ACCENT2, activeforeground=TEXT_WHITE, relief="flat",
            cursor="hand2", pady=9, command=self._submit
        )
        self.submit_btn.pack(fill="x", pady=(0, 8))

        # Security policy notice
        sec_notice = tk.Label(
            self.card,
            text="🔒 Cloud IP Blacklist & HWID Protection Active.\n3 failed attempts permanently blacklists your IP and locks this device.",
            font=("Segoe UI", 8), bg=BG_CARD, fg="#64748b", justify="center"
        )
        sec_notice.pack(pady=(6, 0))

        self.pass_entry.focus_force()

    def _render_hardware_banned_ui(self, ban_reason: str):
        """Displays irreversible security lockout screen when workstation is hardware-banned."""
        banned_card = tk.Frame(self.root, bg="#180709", padx=26, pady=28, highlightthickness=2, highlightbackground="#dc2626")
        banned_card.pack(fill="both", expand=True, padx=16, pady=16)

        tk.Label(banned_card, text="⛔", font=("Segoe UI", 36), bg="#180709", fg="#ef4444").pack(pady=(0, 6))
        tk.Label(banned_card, text="WORKSTATION BLACKLISTED", font=("Segoe UI", 15, "bold"), bg="#180709", fg="#ef4444").pack()
        
        desc = (
            "This device and its associated IP address have been permanently blacklisted.\n"
            "Unauthorized intrusion or excessive failed authentication attempts detected."
        )
        tk.Label(banned_card, text=desc, font=FONT_SUB, bg="#180709", fg="#fca5a5", justify="center").pack(pady=(6, 16))

        # HWID Info box
        hw_box = tk.Frame(banned_card, bg="#270d10", padx=12, pady=10, highlightthickness=1, highlightbackground="#7f1d1d")
        hw_box.pack(fill="x", pady=(0, 16))
        tk.Label(hw_box, text=f"BANNED HWID: {self.hwid}", font=("Consolas", 10, "bold"), bg="#270d10", fg="#fca5a5").pack(anchor="w")
        tk.Label(hw_box, text=f"REASON: {ban_reason}", font=FONT_SMALL, bg="#270d10", fg="#f87171").pack(anchor="w", pady=(3, 0))

        tk.Button(
            banned_card, text="Exit Application", font=FONT_BTN, bg="#7f1d1d", fg=TEXT_WHITE,
            activebackground="#991b1b", relief="flat", cursor="hand2", pady=8,
            command=lambda: (self.root.destroy(), sys.exit(0))
        ).pack(fill="x")

    def _copy_hwid(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.hwid)
            self.copy_btn.config(text="✓ Copied!", fg="#4ade80")
            self.root.after(2000, lambda: self.copy_btn.config(text="📋 Copy HWID", fg=TEXT_WHITE))
        except Exception:
            pass

    def _toggle_reveal(self):
        self.revealed = not self.revealed
        if self.revealed:
            self.pass_entry.config(show="")
            self.toggle_btn.config(text="🔒")
        else:
            self.pass_entry.config(show="•")
            self.toggle_btn.config(text="👁")

    def _prompt_owner_recovery(self):
        """Hidden Owner Console accessible via Ctrl+Alt+R requiring secret Owner Recovery Key."""
        dialog = tk.Toplevel(self.root)
        dialog.title("🛡️ Owner Security Console")
        dialog.configure(bg=BG_DARK)
        dialog.resizable(False, False)

        w, h = 450, 240
        sw = dialog.winfo_screenwidth()
        sh = dialog.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        dialog.geometry(f"{w}x{h}+{x}+{y}")
        dialog.transient(self.root)
        dialog.grab_set()

        card = tk.Frame(dialog, bg=BG_CARD, padx=22, pady=18, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=12, pady=12)

        tk.Label(card, text="🛡️ Owner Security Authentication", font=("Segoe UI", 12, "bold"), bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(card, text="Enter confidential Owner Recovery Key to reset master credentials & bans:", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))

        key_var = tk.StringVar()
        e_frame = tk.Frame(card, bg=BG_INPUT, highlightthickness=1, highlightbackground=BORDER)
        e_frame.pack(fill="x", ipady=3, pady=(0, 8))
        key_entry = tk.Entry(e_frame, textvariable=key_var, show="•", font=("Segoe UI", 10), bg=BG_INPUT, fg=TEXT_WHITE, relief="flat", bd=0, insertbackground=TEXT_WHITE)
        key_entry.pack(fill="x", padx=8, pady=(4, 4))
        key_entry.focus_set()

        err_lbl = tk.Label(card, text="", font=FONT_SMALL, bg=BG_CARD, fg=RED)
        err_lbl.pack(pady=(0, 6))

        def _submit_key():
            candidate = key_var.get().strip()
            if verify_owner_recovery_key(candidate):
                reset_master_password()
                # Clear local hardware ban vault if any
                from utils.license_manager import LICENSE_VAULT_FILE
                try:
                    if os.path.exists(LICENSE_VAULT_FILE):
                        os.remove(LICENSE_VAULT_FILE)
                except Exception:
                    pass
                messagebox.showinfo("Owner Verified", "Master password and local security locks have been reset.", parent=dialog)
                dialog.destroy()
                self.attempts = 0
                for widget in self.root.winfo_children():
                    widget.destroy()
                self._build_ui()
            else:
                err_lbl.config(text="Invalid Owner Recovery Key.")
                key_entry.delete(0, tk.END)

        key_entry.bind("<Return>", lambda e: _submit_key())

        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Cancel", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY, relief="flat", padx=12, pady=4, command=dialog.destroy).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Verify & Reset", font=FONT_BTN, bg=ACCENT, fg=TEXT_WHITE, activebackground=ACCENT2, relief="flat", padx=12, pady=4, command=_submit_key).pack(side="right")

    def _submit(self):
        candidate = self.pass_var.get().strip()
        if not candidate:
            self.err_lbl.config(text="Please enter your license key or master passcode.", fg=RED)
            return

        self.submit_btn.config(text="⏳ Verifying Security Credentials...", state="disabled")
        self.root.update_idletasks()

        # 1. Attempt Online Verification through License & IP Gatekeeper Gateway
        res = verify_online_license(candidate)
        status = res.get("status")

        if status == "authorized":
            # Success! Save cached token for offline access
            lic_data = res.get("license", res)
            lic_data["key"] = candidate
            save_active_license_token(lic_data)
            self.err_lbl.config(text="✓ Authenticated successfully! Launching studio...", fg="#4ade80")
            self.authenticated = True
            self.root.after(350, self._unlock_app)
            return

        elif status == "blocked":
            # IP or Device is blacklisted on server
            apply_local_hardware_ban(res.get("message", "Cloud IP / Device Blacklisted"))
            messagebox.showerror(
                "Access Permanently Denied",
                res.get("message", "Your IP address or Workstation has been permanently blacklisted."),
                parent=self.root
            )
            self.root.destroy()
            sys.exit(0)

        elif status == "failed":
            # Server rejected credential and tracked failure count
            remaining = res.get("remaining_attempts", self.max_attempts - self.attempts - 1)
            self.attempts += 1
            if remaining <= 0:
                apply_local_hardware_ban("3 Failed authentication attempts (IP Blacklisted)")
                messagebox.showerror(
                    "Security Lockdown",
                    "Maximum failed attempts exceeded.\nYour IP address and Hardware ID (HWID) have been permanently blacklisted.",
                    parent=self.root
                )
                self.root.destroy()
                sys.exit(0)
            else:
                self.err_lbl.config(
                    text=f"Authentication failed: {res.get('message', 'Invalid key or passcode')}\n{remaining} attempt(s) remaining before permanent IP lockout.",
                    fg=RED
                )
                self.submit_btn.config(text="⚡ Verify & Unlock Studio", state="normal")
                self.pass_entry.delete(0, tk.END)
                return

        # 2. Offline fallback if server is unreachable
        if status == "offline":
            if verify_master_password(candidate):
                self.err_lbl.config(text="✓ Authenticated (Offline Enclave). Launching studio...", fg="#4ade80")
                self.authenticated = True
                self.root.after(350, self._unlock_app)
                return

            cached = get_cached_license()
            if cached and cached.get("key") == candidate:
                self.err_lbl.config(text="✓ Authenticated (Offline Enclave). Launching studio...", fg="#4ade80")
                self.authenticated = True
                self.root.after(350, self._unlock_app)
                return

            # Production Enterprise Lifetime Key offline grace authorization
            if candidate in ("JM-E74B-39D9-677A-BC2A", "JM-PRO9-88AA-77BB-66CC"):
                save_active_license_token({
                    "key": candidate,
                    "tier": "Enterprise Lifetime",
                    "status": "ACTIVE",
                    "offline_grace": True,
                })
                self.err_lbl.config(text="✓ Authenticated (Enterprise Lifetime Enclave). Launching studio...", fg="#4ade80")
                self.authenticated = True
                self.root.after(350, self._unlock_app)
                return

            # License key needs initial server connection to bind HWID
            self.err_lbl.config(
                text="⚠️ License server unreachable.\nEnsure device has an active internet connection, or enter Master Passcode (Jarvis2026! / Vareen2608).",
                fg=RED
            )
            self.submit_btn.config(text="⚡ Verify & Unlock Studio", state="normal")
            return

        # 3. Final fallback
        self.attempts += 1
        remaining = self.max_attempts - self.attempts
        if remaining <= 0:
            apply_local_hardware_ban("3 Failed offline authentication attempts")
            messagebox.showerror(
                "Security Lockdown",
                "Maximum failed attempts exceeded.\nWorkstation has been locked due to security policy violations.",
                parent=self.root
            )
            self.root.destroy()
            sys.exit(0)
        else:
            self.err_lbl.config(
                text=f"Invalid credential. {remaining} attempt(s) remaining before hardware lockout.",
                fg=RED
            )
            self.submit_btn.config(text="⚡ Verify & Unlock Studio", state="normal")
            self.pass_entry.delete(0, tk.END)

    def _unlock_app(self):
        for widget in self.root.winfo_children():
            widget.destroy()
        self.on_success()

    def _on_cancel(self):
        if not self.authenticated:
            self.root.destroy()
            sys.exit(0)


class ChangePasswordDialog:
    """Modal dialog allowing users to change their master password."""
    def __init__(self, parent):
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("🔑 Change Master Password")
        self.dialog.configure(bg=BG_DARK)
        self.dialog.resizable(False, False)

        w, h = 430, 420
        sw = self.dialog.winfo_screenwidth()
        sh = self.dialog.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.dialog.geometry(f"{w}x{h}+{x}+{y}")

        self.dialog.transient(parent)
        self.dialog.grab_set()

        self._build_ui()

    def _build_ui(self):
        card = tk.Frame(self.dialog, bg=BG_CARD, padx=22, pady=20, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(card, text="🔒 Change Master Password", font=("Segoe UI", 13, "bold"), bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(card, text="Update your security credentials for Jarvis Maps AI Studio.", font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 14))

        # Current password
        self.old_var = tk.StringVar()
        f1 = tk.Frame(card, bg=BG_INPUT, highlightthickness=1, highlightbackground=BORDER)
        f1.pack(fill="x", ipady=3, pady=(0, 8))
        tk.Label(f1, text="Current Password:", font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_GRAY).pack(anchor="w", padx=8, pady=(3, 0))
        self.old_entry = tk.Entry(f1, textvariable=self.old_var, show="•", font=("Segoe UI", 10), bg=BG_INPUT, fg=TEXT_WHITE, relief="flat", bd=0, insertbackground=TEXT_WHITE)
        self.old_entry.pack(fill="x", padx=8, pady=(2, 5))
        self.old_entry.focus_set()

        # New password
        self.new_var = tk.StringVar()
        f2 = tk.Frame(card, bg=BG_INPUT, highlightthickness=1, highlightbackground=BORDER)
        f2.pack(fill="x", ipady=3, pady=(0, 8))
        tk.Label(f2, text="New Password:", font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_GRAY).pack(anchor="w", padx=8, pady=(3, 0))
        self.new_entry = tk.Entry(f2, textvariable=self.new_var, show="•", font=("Segoe UI", 10), bg=BG_INPUT, fg=TEXT_WHITE, relief="flat", bd=0, insertbackground=TEXT_WHITE)
        self.new_entry.pack(fill="x", padx=8, pady=(2, 5))

        # Confirm new password
        self.confirm_var = tk.StringVar()
        f3 = tk.Frame(card, bg=BG_INPUT, highlightthickness=1, highlightbackground=BORDER)
        f3.pack(fill="x", ipady=3, pady=(0, 8))
        tk.Label(f3, text="Confirm New Password:", font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_GRAY).pack(anchor="w", padx=8, pady=(3, 0))
        self.confirm_entry = tk.Entry(f3, textvariable=self.confirm_var, show="•", font=("Segoe UI", 10), bg=BG_INPUT, fg=TEXT_WHITE, relief="flat", bd=0, insertbackground=TEXT_WHITE)
        self.confirm_entry.pack(fill="x", padx=8, pady=(2, 5))
        self.confirm_entry.bind("<Return>", lambda e: self._save())

        self.msg_lbl = tk.Label(card, text="", font=FONT_SUB, bg=BG_CARD, fg=RED)
        self.msg_lbl.pack(pady=(0, 8))

        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill="x")

        tk.Button(
            btn_row, text="Cancel", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
            relief="flat", cursor="hand2", padx=14, pady=6, command=self.dialog.destroy
        ).pack(side="right", padx=(8, 0))

        tk.Button(
            btn_row, text="💾 Save Changes", font=FONT_BTN, bg=ACCENT, fg=TEXT_WHITE,
            activebackground=ACCENT2, activeforeground=TEXT_WHITE, relief="flat",
            cursor="hand2", padx=16, pady=6, command=self._save
        ).pack(side="right")

    def _save(self):
        old_p = self.old_var.get().strip()
        new_p = self.new_var.get().strip()
        confirm_p = self.confirm_var.get().strip()

        if not old_p or not new_p:
            self.msg_lbl.config(text="Please fill in all password fields.", fg=RED)
            return

        if new_p != confirm_p:
            self.msg_lbl.config(text="New passwords do not match.", fg=RED)
            return

        ok, msg = change_master_password(old_p, new_p)
        if ok:
            messagebox.showinfo("Success", "Password updated successfully!", parent=self.dialog)
            self.dialog.destroy()
        else:
            self.msg_lbl.config(text=msg, fg=RED)



# ─────────────────────────────────────────────
#  MAIN APP
# ─────────────────────────────────────────────

class JarvisApp:

    def __init__(self, root):
        self.root = root
        _apply_app_icon(self.root)
        self.root.title("🗺️ Jarvis Maps Automation Studio")
        
        # Center the window with modern 1240x820 dimensions
        win_w = 1240
        win_h = 820
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        pos_x = max(0, int((screen_w - win_w) / 2))
        pos_y = max(0, int((screen_h - win_h) / 2) - 20)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.configure(bg=BG_DARK)
        self.root.resizable(True, True)
        self.root.minsize(1024, 680)
        self.root.protocol("WM_DELETE_WINDOW", self._on_app_exit)

        self.locations = []
        self.is_running = False
        self.bnd_queries = []
        self.bnd_trace_running = False
        self.bnd_trace_start_time = 0.0
        self.bnd_trace_total = 0
        self.bnd_trace_current_idx = 0
        self.bnd_trace_item_durations = []
        self.bnd_last_avg_time = 15.0
        self.bnd_current_item_start = 0.0
        self.bnd_sync_files = []
        self.bnd_sync_is_running = False
        self.bnd_sync_checked_signature = None
        self.bnd_sync_last_output_dir = None
        self.bnd_tiff_files = []
        self.bnd_tiff_is_running = False
        self.bnd_last_merged_tiff = None
        self.wake_listener = None
        self.wake_active = False
        init_db()  # Initialize SQLite database on startup

        self._build_ui()
        # Check for updates automatically in background after startup
        self.root.after(1500, self._auto_check_updates)

    def _auto_check_updates(self):
        """Silently checks for updates in background without interrupting unless an update is available."""
        def _on_result(res):
            if res.get("update_available"):
                self.update_btn.config(
                    text=f"🚀 v{res['latest_version']} Available!",
                    bg="#1e3a8a",
                    fg="#60a5fa",
                    font=("Segoe UI", 9, "bold")
                )
                UpdateNotificationDialog(self.root, res)
        check_for_updates_async(self.root, _on_result, silent_if_latest=True)

    def _manual_check_updates(self):
        """Manual check triggered when user clicks the version button in header."""
        self.update_btn.config(text="⏳ Checking...", fg=YELLOW)
        def _on_result(res):
            if res.get("update_available"):
                self.update_btn.config(
                    text=f"🚀 v{res['latest_version']} Available!",
                    bg="#1e3a8a",
                    fg="#60a5fa",
                    font=("Segoe UI", 9, "bold")
                )
                UpdateNotificationDialog(self.root, res)
            elif res.get("success"):
                self.update_btn.config(text=f"✓ v{res['current_version']} Up to Date", fg=GREEN, bg="#1e293b")
                messagebox.showinfo("Jarvis Maps AI", f"You are running the latest version (v{res['current_version']})!\nNo updates needed at this time.", parent=self.root)
            else:
                self.update_btn.config(text=f"v{CURRENT_VERSION}", fg=TEXT_GRAY, bg="#1e293b")
                messagebox.showwarning("Update Check", "Could not reach update server.\nPlease check your connection or visit the website directly.", parent=self.root)
        check_for_updates_async(self.root, _on_result, silent_if_latest=False)

    def _on_app_exit(self):
        """Gracefully terminate background runners and close browsers."""
        self.is_running = False
        self.bnd_trace_running = False
        self.bnd_sync_is_running = False
        if hasattr(self, "val_is_validating"):
            self.val_is_validating = False
        if hasattr(self, "wake_listener") and self.wake_listener:
            try:
                self.wake_listener.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ── UI BUILDER ────────────────────────────

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=BG_DARK, pady=16)
        header.pack(fill="x", padx=30)
        
        title_box = tk.Frame(header, bg=BG_DARK)
        title_box.pack(side="left", fill="y")
        tk.Label(title_box, text="🗺️  Jarvis Maps Automation Studio",
                 font=FONT_TITLE, bg=BG_DARK, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(title_box, text="AI Spatial Intelligence • Coordinates • Boundaries • Amenities",
                 font=FONT_SUB, bg=BG_DARK, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 0))

        # Status badge & Security button
        badge_frame = tk.Frame(header, bg=BG_DARK)
        badge_frame.pack(side="right", fill="y")

        status_badge = tk.Label(badge_frame, text="  🟢 SQLite Active • Engine Ready  ",
                                font=FONT_SMALL, bg="#1a2234", fg=GREEN, padx=12, pady=6)
        status_badge.pack(side="right")

        self.update_btn = tk.Button(
            badge_frame,
            text=f"🔄 v{CURRENT_VERSION}",
            font=FONT_SMALL,
            bg="#1e293b",
            fg=TEXT_GRAY,
            activebackground="#334155",
            activeforeground=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=4,
            command=self._manual_check_updates
        )
        self.update_btn.pack(side="right", padx=(0, 10))

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", padx=30, pady=(0, 6))

        # Notebook tabs
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook",
                        background=BG_DARK, borderwidth=0, tabmargins=[24, 6, 0, 0])
        style.configure("TNotebook.Tab",
                        background=BG_CARD, foreground=TEXT_GRAY,
                        font=FONT_BTN, padding=[20, 9], borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", BG_DARK)],
                  foreground=[("selected", TEXT_WHITE)])

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        # Tab 1 — Coordinates Extractor
        tab1 = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab1, text="  📍  Coordinates Extractor  ")
        self._build_extractor_tab(tab1)

        # Tab 2 — Boundary Tracer
        tab2 = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab2, text="  🗺️  Boundary Tracer  ")
        self._build_boundary_tab(tab2)

        # Tab 3 — Amenity Finder
        tab3 = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab3, text="  📍  Amenity Finder  ")
        self._build_amenity_tab(tab3)

        # Tab 4 — Bulk Runner
        tab4 = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab4, text="  ⚡  Bulk Runner  ")
        self._build_bulk_tab(tab4)
        
        # Tab 5 — Area Explorer
        tab5 = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab5, text="  🌍  Area Explorer  ")
        self._build_area_tab(tab5)

        # Tab 6 — Boundary Validator
        tab6 = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab6, text="  🛡️  Boundary Validator  ")
        self._build_validator_tab(tab6)

        # Tab 7 — History
        tab7 = tk.Frame(self.notebook, bg=BG_DARK)
        self.notebook.add(tab7, text="  🗄️  History  ")
        self._build_history_tab(tab7)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

        self._apply_hover_effects(self.root)

    def _on_notebook_tab_changed(self, event=None):
        try:
            sel_id = self.notebook.select()
            tab_text = self.notebook.tab(sel_id, "text")
            if "Boundary Validator" in tab_text:
                if hasattr(self, "_val_on_tab_focused"):
                    self._val_on_tab_focused()
            elif "Area Explorer" in tab_text:
                if hasattr(self, "area_left_canvas"):
                    self.area_left_canvas.configure(scrollregion=self.area_left_canvas.bbox("all"))
            elif "History" in tab_text:
                if hasattr(self, "_load_history_data"):
                    self._load_history_data()
        except Exception:
            pass

    def _apply_hover_effects(self, widget):
        """
        Recursively bind a subtle lighten-on-hover to every tk.Button
        so buttons feel interactive instead of flat/static.
        """
        if isinstance(widget, tk.Button):
            try:
                normal_bg = widget.cget("bg")
                hover_bg = _lighten(normal_bg, 22)

                widget.bind(
                    "<Enter>",
                    lambda e, w=widget, c=hover_bg: w.config(bg=c)
                )
                widget.bind(
                    "<Leave>",
                    lambda e, w=widget, c=normal_bg: w.config(bg=c)
                )
            except (tk.TclError, ValueError):
                pass

        for child in widget.winfo_children():
            self._apply_hover_effects(child)


    # ══════════════════════════════════════════
    #  TAB 1 — COORDINATES EXTRACTOR
    # ══════════════════════════════════════════

    def _split(self, body, left_width=380):
        """
        Responsive left/right split.
        left keeps a proportional weight with a clean minsize,
        right absorbs extra space dynamically.
        """
        body.grid_columnconfigure(0, weight=4, minsize=left_width)
        body.grid_columnconfigure(1, weight=6, minsize=460)
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=BG_DARK)
        right = tk.Frame(body, bg=BG_DARK)

        left.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        right.grid(row=0, column=1, sticky="nsew")

        return left, right

    def _build_extractor_tab(self, parent):
        # ── Bottom bar packed FIRST so it always stays visible ──
        self._build_bottom(parent)

        body = tk.Frame(parent, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=30, pady=20)

        left, right = self._split(body, 380)

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, parent):
        # File upload card
        card = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        card.pack(fill="x", pady=(0, 16))
        tk.Label(card, text="📂  Import File",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(card, text="Supports CSV, TXT, XLS, XLSX",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 12))
        self.file_label = tk.Label(card, text="No file selected",
                                   font=FONT_MONO, bg=BG_INPUT, fg=TEXT_GRAY,
                                   padx=12, pady=10, anchor="w", wraplength=340)
        self.file_label.pack(fill="x", pady=(0, 12))
        tk.Button(card, text="  Browse File", font=FONT_BTN, bg=ACCENT, fg="white",
                  relief="flat", cursor="hand2", padx=16, pady=8,
                  command=self._browse_file).pack(anchor="w")

        # Single mode card
        card_single = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        card_single.pack(fill="x", pady=(0, 16))
        tk.Label(card_single, text="📍  Single Mode / Add Location",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(card_single, text="Search single location or add to the batch list",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 12))
        self.single_entry = tk.Entry(card_single, font=FONT_NORMAL, bg=BG_INPUT,
                                     fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                                     relief="flat", bd=0, highlightthickness=1,
                                     highlightbackground=BG_INPUT, highlightcolor=ACCENT)
        self.single_entry.pack(fill="x", pady=(0, 12), ipady=8, padx=2)
        self.single_entry.bind("<Return>", lambda e: self._add_single_to_list())
        
        btn_frame = tk.Frame(card_single, bg=BG_CARD)
        btn_frame.pack(fill="x")
        self.btn_add_list = tk.Button(btn_frame, text="Add to List",
                                      font=FONT_BTN, bg=BG_INPUT, fg=TEXT_WHITE,
                                      relief="flat", cursor="hand2", padx=14, pady=7,
                                      command=self._add_single_to_list)
        self.btn_add_list.pack(side="left", padx=(0, 10))
        self.btn_search_single = tk.Button(btn_frame, text="Search & Save",
                                           font=FONT_BTN, bg=ACCENT2, fg=BG_DARK,
                                           relief="flat", cursor="hand2", padx=14, pady=7,
                                           command=self._search_single_now)
        self.btn_search_single.pack(side="left")

        # Voice buttons row
        voice_row = tk.Frame(card_single, bg=BG_CARD)
        voice_row.pack(fill="x", pady=(12, 0))
        self.ext_mic_btn = tk.Button(voice_row, text="🎤  Press to Talk",
                                     font=FONT_BTN, bg="#6c3483", fg="white",
                                     relief="flat", cursor="hand2", padx=14, pady=7,
                                     command=self._ext_press_to_talk)
        self.ext_mic_btn.pack(side="left", padx=(0, 10))
        self.ext_wake_btn = tk.Button(voice_row, text="👂  Start Listening",
                                      font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                      relief="flat", cursor="hand2", padx=14, pady=7,
                                      command=self._ext_toggle_wake)
        self.ext_wake_btn.pack(side="left")
        self.ext_voice_status = tk.Label(voice_row, text="",
                                         font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.ext_voice_status.pack(side="left", padx=(10, 0))

        # Locations list card
        card2 = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        card2.pack(fill="both", expand=True)
        header_row = tk.Frame(card2, bg=BG_CARD)
        header_row.pack(fill="x")
        tk.Label(header_row, text="📋  Locations",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left")
        self.count_label = tk.Label(header_row, text="0 items",
                                    font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.count_label.pack(side="right")
        list_frame = tk.Frame(card2, bg=BG_CARD)
        list_frame.pack(fill="both", expand=True, pady=(10, 0))
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set,
                                  bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_NORMAL,
                                  selectbackground=ACCENT, relief="flat",
                                  bd=0, highlightthickness=0)
        self.listbox.pack(fill="both", expand=True)
        scrollbar.config(command=self.listbox.yview)

    def _build_right(self, parent):
        card = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="📡  Live Progress",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(card, text="Real-time automation log",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        log_frame = tk.Frame(card, bg=BG_CARD)
        log_frame.pack(fill="both", expand=True)
        log_scroll = tk.Scrollbar(log_frame)
        log_scroll.pack(side="right", fill="y")
        self.log_text = tk.Text(log_frame, yscrollcommand=log_scroll.set,
                                bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_MONO,
                                relief="flat", bd=0, highlightthickness=0,
                                state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)
        log_scroll.config(command=self.log_text.yview)
        self.log_text.tag_config("success", foreground=GREEN)
        self.log_text.tag_config("error",   foreground=RED)
        self.log_text.tag_config("info",    foreground=ACCENT)
        self.log_text.tag_config("warn",    foreground=YELLOW)
        self.log_text.tag_config("normal",  foreground=TEXT_WHITE)
        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(card, variable=self.progress_var,
                                        maximum=100, mode="determinate")
        self.progress.pack(fill="x", pady=(12, 0))
        self.progress_label = tk.Label(card, text="Ready",
                                       font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.progress_label.pack(anchor="e", pady=(4, 0))

    def _build_bottom(self, parent):
        bar = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=30, pady=14)
        bar.pack(fill="x", side="bottom")
        self.status_label = tk.Label(bar, text="⬤  Ready",
                                     font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.status_label.pack(side="left")
        self.open_btn = tk.Button(bar, text="📊 Open Excel",
                                  font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                  relief="flat", cursor="hand2", padx=16, pady=8,
                                  state="disabled", command=self._open_excel)
        self.open_btn.pack(side="right", padx=(10, 0))
        self.start_btn = tk.Button(bar, text="▶  Start Automation",
                                   font=FONT_BTN, bg=ACCENT2, fg=BG_DARK,
                                   relief="flat", cursor="hand2", padx=22, pady=8,
                                   state="disabled", command=self._start_automation)
        self.start_btn.pack(side="right")

    # ── Extractor Actions ─────────────────────

    def _add_single_to_list(self):
        query = self.single_entry.get().strip()
        if not query:
            messagebox.showwarning("Empty Location", "Please enter a location name.")
            return
        self.locations.append(query)
        self.listbox.insert(tk.END, f"  {len(self.locations)}.  {query}")
        self.count_label.config(text=f"{len(self.locations)} items", fg=ACCENT)
        self.start_btn.config(state="normal")
        self.single_entry.delete(0, tk.END)
        self._log(f"➕ Added to list: {query}\n", "info")

    def _search_single_now(self):
        query = self.single_entry.get().strip()
        if not query:
            messagebox.showwarning("Empty Location", "Please enter a location name.")
            return
        if self.is_running:
            messagebox.showwarning("Running", "An automation job is already running.")
            return
        self.is_running = True
        self.start_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.btn_search_single.config(state="disabled", text="Searching...")
        self.btn_add_list.config(state="disabled")
        self.progress_var.set(0)
        self._set_status("🔄  Searching single location...", ACCENT)
        self.single_entry.config(state="disabled")
        threading.Thread(target=self._run_single_search, args=(query,), daemon=True).start()

    def _run_single_search(self, query):
        successful_results = []
        driver = None
        search_succeeded = False

        try:
            from automation.maps_bot import (
                setup_driver,
                search_location,
            )

            driver = setup_driver(
                headless=None,  # None -> maps_bot.HEADLESS (single switch)
                lightweight=False,
            )

            self._log(
                f"\n🔍 [Single Mode] Searching: {query}\n",
                "info",
            )

            result = search_location(
                driver,
                query,
            )

            if (
                isinstance(result, dict)
                and result.get("status") == "Success"
            ):
                search_succeeded = True
                successful_results.append(result)

                self._log(
                    f"   ✅ {result['latitude']}, "
                    f"{result['longitude']}\n",
                    "success",
                )

                self._log(
                    f"   📍 {result.get('address', '')}\n",
                    "normal",
                )

                self.root.after(
                    0,
                    self._set_status,
                    "✅ Coordinates found",
                    GREEN,
                )

            else:
                error_message = (
                    result.get(
                        "error",
                        "Coordinates not found",
                    )
                    if isinstance(result, dict)
                    else "No result returned"
                )

                final_url = (
                    result.get("url", "")
                    if isinstance(result, dict)
                    else ""
                )

                self._log(
                    f"   ❌ {error_message}\n",
                    "error",
                )

                if final_url:
                    self._log(
                        f"   🔗 Final URL: {final_url}\n",
                        "warn",
                    )

                self.root.after(
                    0,
                    self._set_status,
                    "❌ Coordinates not found",
                    RED,
                )

        except Exception as exc:
            self._log(
                f"\n❌ Automation error: {exc}\n",
                "error",
            )

            self.root.after(
                0,
                self._set_status,
                "❌ Search failed",
                RED,
            )

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        if successful_results:
            try:
                outfile = get_output_filename(
                    "coordinates"
                )

                save_to_excel(
                    successful_results,
                    filepath=outfile,
                )

                self._log(
                    f"\n✅ Saved to {outfile}\n",
                    "success",
                )

                self.root.after(
                    0,
                    self.open_btn.config,
                    {
                        "state": "normal",
                        "fg": GREEN,
                    },
                )

                self.root.after(
                    0,
                    setattr,
                    self,
                    "_last_ext_file",
                    outfile,
                )

            except Exception as exc:
                search_succeeded = False
                self._log(
                    f"\n❌ Save error: {exc}\n",
                    "error",
                )

        self.root.after(
            0,
            self._on_single_search_done,
            search_succeeded,
        )

    def _on_single_search_done(self, success):
        self.is_running = False
        self.start_btn.config(
            state="normal" if self.locations else "disabled"
        )
        self.btn_search_single.config(
            state="normal",
            text="Search & Save",
        )
        self.btn_add_list.config(state="normal")
        self.single_entry.config(state="normal")
        self.single_entry.delete(0, tk.END)
        self.progress_var.set(100)
        self.progress_label.config(text="Completed!")

        if success:
            self._set_status(
                "✅ Single location search completed",
                GREEN,
            )
        else:
            self._set_status(
                "❌ Coordinates not found",
                RED,
            )

    def _browse_file(self):
        filepath = filedialog.askopenfilename(
            title="Select location file",
            initialdir=get_downloads_dir(),
            filetypes=[("All supported", "*.csv *.txt *.xlsx *.xls"),
                       ("CSV files", "*.csv"), ("Text files", "*.txt"),
                       ("Excel files", "*.xlsx *.xls")])
        if not filepath: return
        try:
            self.locations = read_locations_from_file(filepath)
            filename = os.path.basename(filepath)
            self.file_label.config(text=f"✅  {filename}", fg=GREEN)
            self.listbox.delete(0, tk.END)
            for i, loc in enumerate(self.locations, 1):
                self.listbox.insert(tk.END, f"  {i}.  {loc}")
            self.count_label.config(text=f"{len(self.locations)} items", fg=ACCENT)
            self.start_btn.config(state="normal")
            self._log(f"✅ Loaded {len(self.locations)} locations from {filename}\n", "success")
        except Exception as e:
            messagebox.showerror("File Error", str(e))
            self._log(f"❌ Error loading file: {e}\n", "error")

    def _start_automation(self):
        if not self.locations:
            messagebox.showwarning("No Locations", "Please upload a file first.")
            return
        self.is_running = True
        self.start_btn.config(state="disabled", text="⏳ Running...")
        self.open_btn.config(state="disabled")
        self.progress_var.set(0)
        self._set_status("🔄  Running automation...", ACCENT)
        threading.Thread(target=self._run_automation, daemon=True).start()

    def _run_automation(self):
        total = len(self.locations)
        all_results = []
        driver = None

        try:
            from automation.maps_bot import (
                setup_driver,
                search_location,
            )

            driver = setup_driver(
                headless=None,  # None -> maps_bot.HEADLESS (single switch)
                lightweight=False,
            )

            for i, query in enumerate(
                self.locations,
                start=1,
            ):
                query = query.strip()

                if not query:
                    continue

                self._log(
                    f"\n🔍 [{i}/{total}] Searching: {query}\n",
                    "info",
                )

                result = search_location(
                    driver,
                    query,
                )

                if (
                    isinstance(result, dict)
                    and result.get("status") == "Success"
                ):
                    all_results.append(result)

                    self._log(
                        f"   ✅ {result['latitude']}, "
                        f"{result['longitude']}\n",
                        "success",
                    )

                    self._log(
                        f"   📍 {result.get('address', '')}\n",
                        "normal",
                    )

                else:
                    error_message = (
                        result.get(
                            "error",
                            "Coordinates not found",
                        )
                        if isinstance(result, dict)
                        else "No result returned"
                    )

                    final_url = (
                        result.get("url", "")
                        if isinstance(result, dict)
                        else ""
                    )

                    self._log(
                        f"   ❌ {error_message}\n",
                        "error",
                    )

                    if final_url:
                        self._log(
                            f"   🔗 Final URL: {final_url}\n",
                            "warn",
                        )

                progress = (i / total) * 100

                self.root.after(
                    0,
                    self.progress_var.set,
                    progress,
                )

                self.root.after(
                    0,
                    self.progress_label.config,
                    {
                        "text": f"{i} / {total} completed"
                    },
                )

        except Exception as exc:
            self._log(
                f"\n❌ Automation error: {exc}\n",
                "error",
            )

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        if all_results:
            try:
                outfile = get_output_filename(
                    "coordinates_batch"
                )

                save_to_excel(
                    all_results,
                    filepath=outfile,
                )

                self._log(
                    f"\n✅ Saved to {outfile}\n",
                    "success",
                )

                self.root.after(
                    0,
                    self.open_btn.config,
                    {
                        "state": "normal",
                        "fg": GREEN,
                    },
                )

                self.root.after(
                    0,
                    setattr,
                    self,
                    "_last_ext_file",
                    outfile,
                )

            except Exception as exc:
                self._log(
                    f"\n❌ Save error: {exc}\n",
                    "error",
                )

        self.root.after(
            0,
            self._on_automation_done,
            len(all_results),
        )

    def _on_automation_done(self, count):
        self.is_running = False
        self.start_btn.config(state="normal", text="▶  Start Automation")
        self._set_status(f"✅  Done — {count} locations processed", GREEN)
        self.progress_var.set(100)
        self.progress_label.config(text="Completed!")

    def _open_excel(self):
        # Open the most recently saved file
        last = getattr(self, "_last_ext_file", None)
        if last and os.path.exists(last):
            os.startfile(last)
        elif os.path.exists(OUTPUT_DIR):
            os.startfile(OUTPUT_DIR)
        else:
            messagebox.showinfo("Not Found", "No output file found yet.")

    def _log(self, message: str, tag: str = "normal"):
        def _write():
            self.log_text.config(state="normal")
            self.log_text.insert(tk.END, message, tag)
            self.log_text.see(tk.END)
            self.log_text.config(state="disabled")
        self.root.after(0, _write)

    def _set_status(self, text: str, color: str = TEXT_GRAY):
        self.status_label.config(text=text, fg=color)


    # ══════════════════════════════════════════
    #  TAB 2 — BOUNDARY TRACER
    # ══════════════════════════════════════════

    def _build_boundary_tab(self, parent):
        body = tk.Frame(parent, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=30, pady=20)

        left, right = self._split(body, 380)

        # ── LEFT ──────────────────────────────

        # Search type selector
        type_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        type_card.pack(fill="x", pady=(0, 16))
        tk.Label(type_card, text="🔎  Search By",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(type_card, text="Choose the parameter type",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 12))
        self.bnd_type = tk.StringVar(value="District")
        radio_row = tk.Frame(type_card, bg=BG_CARD)
        radio_row.pack(fill="x")
        for label in ["Pincode", "District", "State", "City", "Locality"]:
            tk.Radiobutton(radio_row, text=f" {label}", variable=self.bnd_type,
                           value=label, font=FONT_NORMAL, bg=BG_CARD,
                           fg=TEXT_WHITE, selectcolor=BG_INPUT,
                           activebackground=BG_CARD,
                           activeforeground=TEXT_WHITE).pack(side="left", padx=(0, 12))

        # Manual entry
        entry_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        entry_card.pack(fill="x", pady=(0, 16))
        tk.Label(entry_card, text="✏️  Add Location",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(entry_card,
                 text="District: name + state code below  •  State/City: name only",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 12))
        entry_row = tk.Frame(entry_card, bg=BG_CARD)
        entry_row.pack(fill="x")
        self.bnd_entry = tk.Entry(entry_row, font=FONT_NORMAL, bg=BG_INPUT,
                                  fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                                  relief="flat", bd=0, highlightthickness=1,
                                  highlightbackground=BG_INPUT, highlightcolor=ORANGE)
        self.bnd_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 10))
        self.bnd_entry.bind("<Return>", lambda e: self._bnd_add_entry())
        tk.Button(entry_row, text="+ Add", font=FONT_BTN, bg=ORANGE, fg="white",
                  relief="flat", cursor="hand2", padx=16, pady=8,
                  command=self._bnd_add_entry).pack(side="left")

        # ── State code field (District mode only) ──────────────
        self.bnd_state_var   = tk.StringVar()
        self.bnd_state_frame = tk.Frame(entry_card, bg=BG_CARD)
        # Not packed here — shown/hidden dynamically by radio selection

        state_row = tk.Frame(self.bnd_state_frame, bg=BG_CARD)
        state_row.pack(fill="x")
        tk.Label(state_row, text="State Code:",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(0, 8))
        self.bnd_state_entry = tk.Entry(
            state_row,
            textvariable=self.bnd_state_var,
            font=FONT_NORMAL, bg=BG_INPUT, fg=TEXT_WHITE,
            insertbackground=TEXT_WHITE, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BG_INPUT,
            highlightcolor=ORANGE, width=6,
        )
        self.bnd_state_entry.pack(side="left", ipady=7)
        tk.Label(state_row, text="e.g. UP  DL  MH  RJ  GJ",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(10, 0))

        # ── Locality fields (Locality mode only) ───────────────
        self.bnd_loc_district_var = tk.StringVar()
        self.bnd_loc_state_var    = tk.StringVar()
        self.bnd_loc_frame        = tk.Frame(entry_card, bg=BG_CARD)
        # Not packed here — shown/hidden dynamically

        loc_row1 = tk.Frame(self.bnd_loc_frame, bg=BG_CARD)
        loc_row1.pack(fill="x", pady=(0, 6))
        tk.Label(loc_row1, text="District:",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY, width=10,
                 anchor="w").pack(side="left")
        tk.Entry(loc_row1, textvariable=self.bnd_loc_district_var,
                 font=FONT_NORMAL, bg=BG_INPUT, fg=TEXT_WHITE,
                 insertbackground=TEXT_WHITE, relief="flat", bd=0,
                 highlightthickness=1, highlightbackground=BG_INPUT,
                 highlightcolor=ORANGE, width=18).pack(side="left", ipady=7)
        tk.Label(loc_row1, text="e.g. Agra",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(8, 0))

        loc_row2 = tk.Frame(self.bnd_loc_frame, bg=BG_CARD)
        loc_row2.pack(fill="x")
        tk.Label(loc_row2, text="State Code:",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY, width=10,
                 anchor="w").pack(side="left")
        tk.Entry(loc_row2, textvariable=self.bnd_loc_state_var,
                 font=FONT_NORMAL, bg=BG_INPUT, fg=TEXT_WHITE,
                 insertbackground=TEXT_WHITE, relief="flat", bd=0,
                 highlightthickness=1, highlightbackground=BG_INPUT,
                 highlightcolor=ORANGE, width=6).pack(side="left", ipady=7)
        tk.Label(loc_row2, text="e.g. UP",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(8, 0))

        def _on_bnd_type_change(*_):
            t = self.bnd_type.get()
            # Hide all contextual fields first
            self.bnd_state_frame.pack_forget()
            self.bnd_loc_frame.pack_forget()
            if t == "District":
                self.bnd_state_var.set("")
                self.bnd_state_frame.pack(fill="x", pady=(10, 0))
            elif t == "Locality":
                self.bnd_loc_district_var.set("")
                self.bnd_loc_state_var.set("")
                self.bnd_loc_frame.pack(fill="x", pady=(10, 0))

            tpl = BND_SAMPLE_TEMPLATES.get(t, BND_SAMPLE_TEMPLATES["District"])
            if hasattr(self, "bnd_sample_btn"):
                self.bnd_sample_btn.config(text=tpl["btn_text"])
            if hasattr(self, "bnd_import_hint"):
                self.bnd_import_hint.config(text=f"💡 Pattern: {tpl['hint']}")

        # Voice buttons for boundary tab
        bnd_voice_row = tk.Frame(entry_card, bg=BG_CARD)
        bnd_voice_row.pack(fill="x", pady=(12, 0))
        self.bnd_mic_btn = tk.Button(bnd_voice_row, text="🎤  Press to Talk",
                                     font=FONT_BTN, bg="#6c3483", fg="white",
                                     relief="flat", cursor="hand2", padx=14, pady=7,
                                     command=self._bnd_press_to_talk)
        self.bnd_mic_btn.pack(side="left", padx=(0, 10))
        self.bnd_wake_btn = tk.Button(bnd_voice_row, text="👂  Start Listening",
                                      font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                      relief="flat", cursor="hand2", padx=14, pady=7,
                                      command=self._bnd_toggle_wake)
        self.bnd_wake_btn.pack(side="left")
        self.bnd_voice_status = tk.Label(bnd_voice_row, text="",
                                         font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.bnd_voice_status.pack(side="left", padx=(10, 0))

        # Import from file
        import_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        import_card.pack(fill="x", pady=(0, 16))
        tk.Label(import_card, text="📂  OR Import from File",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(import_card, text="CSV / TXT / XLSX — pattern adapts to Search By",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        self.bnd_file_label = tk.Label(import_card, text="No file selected",
                                       font=FONT_MONO, bg=BG_INPUT, fg=TEXT_GRAY,
                                       padx=12, pady=10, anchor="w")
        self.bnd_file_label.pack(fill="x", pady=(0, 10))
        import_btn_row = tk.Frame(import_card, bg=BG_CARD)
        import_btn_row.pack(fill="x")
        tk.Button(import_btn_row, text="📂 Browse File", font=FONT_BTN, bg=ACCENT,
                  fg="white", relief="flat", cursor="hand2", padx=14, pady=8,
                  command=self._bnd_browse).pack(side="left", padx=(0, 8))
        self.bnd_sample_btn = tk.Button(import_btn_row, text="📄 District Template", font=FONT_BTN, bg=BG_INPUT,
                  fg=TEXT_WHITE, relief="flat", cursor="hand2", padx=12, pady=8,
                  command=self._bnd_create_sample)
        self.bnd_sample_btn.pack(side="left")
        self.bnd_import_hint = tk.Label(import_card, text="",
                                        font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY,
                                        anchor="w", justify="left", wraplength=340)
        self.bnd_import_hint.pack(fill="x", pady=(8, 0))

        self.bnd_type.trace_add("write", _on_bnd_type_change)
        _on_bnd_type_change()   # apply immediately for initial selection

        # Output folder
        out_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        out_card.pack(fill="x")
        tk.Label(out_card, text="📁  Output Folder",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        self.bnd_out_var = tk.StringVar(value=os.path.join(get_downloads_dir(), "JarvisMaps_Boundaries"))
        out_row = tk.Frame(out_card, bg=BG_CARD)
        out_row.pack(fill="x", pady=(10, 0))
        tk.Entry(out_row, textvariable=self.bnd_out_var, font=FONT_MONO,
                 bg=BG_INPUT, fg=TEXT_WHITE, relief="flat", bd=0,
                 insertbackground=TEXT_WHITE).pack(side="left", fill="x",
                 expand=True, ipady=7, padx=(0, 8))
        tk.Button(out_row, text="📂", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                 relief="flat", cursor="hand2", padx=12, pady=6,
                 command=self._bnd_pick_folder).pack(side="left")

        # Browser Visibility Mode (Headless Toggle)
        self.bnd_headless_var = tk.BooleanVar(value=True)

        def _update_bnd_headless_hint():
            if self.bnd_headless_var.get():
                self.lbl_bnd_headless_hint.config(
                    text="⚡ Headless ON: Silent background browser execution",
                    fg=GREEN,
                )
            else:
                self.lbl_bnd_headless_hint.config(
                    text="👁 Headless OFF: Browser window visible on screen",
                    fg=TEXT_GRAY,
                )

        bnd_hl_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=14)
        bnd_hl_card.pack(fill="x", pady=(16, 0))
        tk.Label(bnd_hl_card, text="⚙️  Browser Mode", font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        hl_row = tk.Frame(bnd_hl_card, bg=BG_CARD)
        hl_row.pack(fill="x", pady=(8, 0))
        self.cb_bnd_headless = tk.Checkbutton(
            hl_row,
            text="Headless Mode (Silent Background)",
            variable=self.bnd_headless_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=_update_bnd_headless_hint,
        )
        self.cb_bnd_headless.pack(side="left")
        self.lbl_bnd_headless_hint = tk.Label(
            bnd_hl_card,
            text="⚡ Headless ON: Silent background browser execution",
            font=FONT_SUB,
            bg=BG_CARD,
            fg=GREEN,
        )
        self.lbl_bnd_headless_hint.pack(anchor="w", pady=(4, 0))

        # ── RIGHT ─────────────────────────────

        # ── Bottom bar packed FIRST so always visible at bottom ──
        bnd_bar = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=12)
        bnd_bar.pack(fill="x", side="bottom")
        self.bnd_status = tk.Label(bnd_bar, text="⬤  Ready",
                                   font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.bnd_status.pack(side="left")
        self.bnd_open_btn = tk.Button(bnd_bar, text="📁 Open Folder",
                                      font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                      relief="flat", cursor="hand2", padx=16, pady=7,
                                      state="disabled", command=self._bnd_open_folder)
        self.bnd_open_btn.pack(side="right", padx=(10, 0))
        self.bnd_start_btn = tk.Button(bnd_bar, text="▶  Trace Boundaries",
                                       font=FONT_BTN, bg=ORANGE, fg="white",
                                       relief="flat", cursor="hand2", padx=22, pady=7,
                                       command=self._bnd_start)
        self.bnd_start_btn.pack(side="right")

        # Queue list (compact)
        queue_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=18, pady=12)
        queue_card.pack(fill="x", pady=(0, 10))
        qrow = tk.Frame(queue_card, bg=BG_CARD)
        qrow.pack(fill="x")
        tk.Label(qrow, text="📋  Queue",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left")
        self.bnd_count = tk.Label(qrow, text="0 items",
                                  font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.bnd_count.pack(side="right")
        qf = tk.Frame(queue_card, bg=BG_CARD)
        qf.pack(fill="x", pady=(6, 0))
        qsb = tk.Scrollbar(qf)
        qsb.pack(side="right", fill="y")
        self.bnd_listbox = tk.Listbox(qf, yscrollcommand=qsb.set, bg=BG_INPUT,
                                      fg=TEXT_WHITE, font=FONT_NORMAL, height=2,
                                      selectbackground=ORANGE, relief="flat",
                                      bd=0, highlightthickness=0)
        self.bnd_listbox.pack(fill="x")
        qsb.config(command=self.bnd_listbox.yview)
        tk.Button(queue_card, text="🗑  Clear Queue", font=FONT_SMALL, bg=RED,
                  fg="white", relief="flat", cursor="hand2", padx=10, pady=4,
                  command=self._bnd_clear).pack(anchor="w", pady=(6, 0))

        # Multi-area boundary synchronization (compact)
        sync_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=18, pady=12)
        sync_card.pack(fill="x", pady=(0, 10))

        sync_header = tk.Frame(sync_card, bg=BG_CARD)
        sync_header.pack(fill="x")
        tk.Label(sync_header, text="🔗  Sync Boundaries",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left")
        self.bnd_sync_count = tk.Label(sync_header, text="0 selected",
                                       font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.bnd_sync_count.pack(side="right")

        sync_list_frame = tk.Frame(sync_card, bg=BG_CARD)
        sync_list_frame.pack(fill="x", pady=(6, 0))
        sync_scroll = tk.Scrollbar(sync_list_frame)
        sync_scroll.pack(side="right", fill="y")
        self.bnd_sync_listbox = tk.Listbox(
            sync_list_frame,
            yscrollcommand=sync_scroll.set,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            font=FONT_SMALL,
            height=2,
            selectmode=tk.EXTENDED,
            selectbackground=ACCENT,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        self.bnd_sync_listbox.pack(fill="x")
        sync_scroll.config(command=self.bnd_sync_listbox.yview)

        sync_action_row = tk.Frame(sync_card, bg=BG_CARD)
        sync_action_row.pack(fill="x", pady=(8, 0))
        tk.Button(
            sync_action_row, text="📂 Select GeoJSON", font=FONT_BTN, bg=ACCENT, fg="white",
            relief="flat", cursor="hand2", padx=10, pady=5, command=self._bnd_sync_browse,
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            sync_action_row, text="📁 Load Folder", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
            relief="flat", cursor="hand2", padx=10, pady=5, command=self._bnd_sync_load_folder,
        ).pack(side="left", padx=(0, 6))
        self.bnd_sync_check_btn = tk.Button(
            sync_action_row, text="🛡 Safety Check", font=FONT_BTN, bg=YELLOW, fg=BG_DARK,
            relief="flat", cursor="hand2", padx=10, pady=5, command=lambda: self._bnd_sync_start(apply=False),
        )
        self.bnd_sync_check_btn.pack(side="left", padx=(0, 6))
        self.bnd_sync_apply_btn = tk.Button(
            sync_action_row, text="🔗 Sync & Export", font=FONT_BTN, bg=ACCENT2, fg=BG_DARK,
            relief="flat", cursor="hand2", padx=10, pady=5, state="disabled", command=lambda: self._bnd_sync_start(apply=True),
        )
        self.bnd_sync_apply_btn.pack(side="left", padx=(0, 6))
        self.bnd_sync_open_btn = tk.Button(
            sync_action_row, text="📁 Open", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
            relief="flat", cursor="hand2", padx=8, pady=5, state="disabled", command=self._bnd_sync_open_output,
        )
        self.bnd_sync_open_btn.pack(side="left", padx=(0, 6))
        tk.Button(
            sync_action_row, text="Clear", font=FONT_BTN, bg=RED, fg="white",
            relief="flat", cursor="hand2", padx=8, pady=5, command=self._bnd_sync_clear,
        ).pack(side="left")

        # ── GeoTIFF Mosaic Merger card (compact with prominent Merge button) ───────────
        tiff_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=18, pady=12)
        tiff_card.pack(fill="x", pady=(0, 10))

        tiff_header = tk.Frame(tiff_card, bg=BG_CARD)
        tiff_header.pack(fill="x")
        tk.Label(tiff_header, text="🗺️  Merge GeoTIFF Satellite Maps",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left")
        self.bnd_tiff_count = tk.Label(tiff_header, text="0 selected",
                                       font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.bnd_tiff_count.pack(side="right")

        tiff_list_frame = tk.Frame(tiff_card, bg=BG_CARD)
        tiff_list_frame.pack(fill="x", pady=(6, 0))
        tiff_scroll = tk.Scrollbar(tiff_list_frame)
        tiff_scroll.pack(side="right", fill="y")
        self.bnd_tiff_listbox = tk.Listbox(
            tiff_list_frame,
            yscrollcommand=tiff_scroll.set,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            font=FONT_SMALL,
            height=2,
            selectmode=tk.EXTENDED,
            selectbackground=ACCENT,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        self.bnd_tiff_listbox.pack(fill="x")
        tiff_scroll.config(command=self.bnd_tiff_listbox.yview)

        # Action Buttons row: prominent Merge button directly on the main bar!
        tiff_btn_row = tk.Frame(tiff_card, bg=BG_CARD)
        tiff_btn_row.pack(fill="x", pady=(8, 0))
        tk.Button(
            tiff_btn_row, text="📂 Select GeoTIFFs", font=FONT_BTN, bg=ACCENT, fg="white",
            relief="flat", cursor="hand2", padx=10, pady=5, command=self._bnd_tiff_browse,
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            tiff_btn_row, text="📁 Load Folder", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
            relief="flat", cursor="hand2", padx=10, pady=5, command=self._bnd_tiff_load_folder,
        ).pack(side="left", padx=(0, 6))
        self.bnd_tiff_merge_btn = tk.Button(
            tiff_btn_row, text="🗺️ Merge GeoTIFFs", font=FONT_BTN, bg=GREEN, fg="white",
            relief="flat", cursor="hand2", padx=14, pady=5, state="disabled", command=self._bnd_tiff_start,
        )
        self.bnd_tiff_merge_btn.pack(side="left", padx=(0, 6))
        self.bnd_tiff_open_btn = tk.Button(
            tiff_btn_row, text="📁 Open Merged", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
            relief="flat", cursor="hand2", padx=10, pady=5, state="disabled", command=self._bnd_tiff_open_output,
        )
        self.bnd_tiff_open_btn.pack(side="left", padx=(0, 6))
        self.bnd_tiff_enhance_btn = tk.Button(
            tiff_btn_row, text="🪄 Enhance Lines", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_WHITE,
            relief="flat", cursor="hand2", padx=10, pady=5, command=self._bnd_tiff_enhance_action,
        )
        self.bnd_tiff_enhance_btn.pack(side="left", padx=(0, 6))
        tk.Button(
            tiff_btn_row, text="Clear", font=FONT_BTN, bg=RED, fg="white",
            relief="flat", cursor="hand2", padx=8, pady=5, command=self._bnd_tiff_clear,
        ).pack(side="left")

        # Blend mode selection row (inline, compact)
        blend_row = tk.Frame(tiff_card, bg=BG_CARD)
        blend_row.pack(fill="x", pady=(6, 0))
        tk.Label(blend_row, text="Seam Blend:", font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(0, 6))
        self.bnd_tiff_blend_mode = tk.StringVar(value="feather")
        for b_mode, b_label in [("feather", "Feathering (Smooth Seam)"), ("average", "Average"), ("replace", "Overlay Top")]:
            tk.Radiobutton(
                blend_row, text=b_label, variable=self.bnd_tiff_blend_mode, value=b_mode,
                font=FONT_SMALL, bg=BG_CARD, fg=TEXT_WHITE, selectcolor=BG_INPUT,
                activebackground=BG_CARD, activeforeground=TEXT_WHITE,
            ).pack(side="left", padx=(0, 8))
        self.bnd_tiff_enhance_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            blend_row, text="🌟 High-Contrast Vivid Lines", variable=self.bnd_tiff_enhance_var,
            font=FONT_SMALL, bg=BG_CARD, fg=GREEN, selectcolor=BG_INPUT,
            activebackground=BG_CARD, activeforeground=GREEN,
        ).pack(side="left", padx=(10, 0))

        # Export log — packed AFTER all cards and bnd_bar
        log_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=18, pady=12)
        log_card.pack(fill="both", expand=True)
        tk.Label(log_card, text="📡  Export Log",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(log_card, text="KML  •  KMZ  •  GeoJSON",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        bnd_log_frame = tk.Frame(log_card, bg=BG_CARD)
        bnd_log_frame.pack(fill="both", expand=True)
        bnd_scroll = tk.Scrollbar(bnd_log_frame)
        bnd_scroll.pack(side="right", fill="y")
        self.bnd_log = tk.Text(bnd_log_frame, yscrollcommand=bnd_scroll.set,
                               bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_MONO,
                               relief="flat", bd=0, highlightthickness=0,
                               state="disabled", wrap="word")
        self.bnd_log.pack(fill="both", expand=True)
        bnd_scroll.config(command=self.bnd_log.yview)
        self.bnd_log.tag_config("success", foreground=GREEN)
        self.bnd_log.tag_config("error",   foreground=RED)
        self.bnd_log.tag_config("info",    foreground=ACCENT)
        self.bnd_log.tag_config("orange",  foreground=ORANGE)
        self.bnd_log.tag_config("warn",    foreground=YELLOW)
        self.bnd_log.tag_config("normal",  foreground=TEXT_WHITE)

        self.bnd_progress_var = tk.DoubleVar()
        ttk.Progressbar(log_card, variable=self.bnd_progress_var,
                        maximum=100).pack(fill="x", pady=(12, 4))

        bnd_info_bar = tk.Frame(log_card, bg=BG_CARD)
        bnd_info_bar.pack(fill="x")

        self.bnd_eta_label = tk.Label(
            bnd_info_bar,
            text="",
            font=FONT_SUB,
            bg=BG_CARD,
            fg="#3498db"
        )
        self.bnd_eta_label.pack(side="left")

        self.bnd_prog_label = tk.Label(
            bnd_info_bar,
            text="Ready",
            font=FONT_SUB,
            bg=BG_CARD,
            fg=TEXT_GRAY
        )
        self.bnd_prog_label.pack(side="right")


    def _bnd_format_duration(self, seconds: float) -> str:
        """Format seconds into human-readable duration."""
        seconds = max(0, float(seconds))
        if seconds < 60:
            return f"{int(round(seconds))}s"
        if seconds < 3600:
            m = int(seconds // 60)
            s = int(round(seconds % 60))
            return f"{m}m {s:02d}s" if s > 0 else f"{m}m"
        h = int(seconds // 3600)
        m = int(round((seconds % 3600) // 60))
        return f"{h}h {m:02d}m"

    def _bnd_update_queue_display(self):
        """Update queue count and estimated time."""
        count = len(self.bnd_queries)
        if count == 0:
            self.bnd_count.config(text="0 items", fg=TEXT_GRAY)
            if not getattr(self, "bnd_trace_running", False):
                self.bnd_eta_label.config(text="")
        else:
            est_sec = count * getattr(self, "bnd_last_avg_time", 15.0)
            est_str = self._bnd_format_duration(est_sec)
            self.bnd_count.config(
                text=f"{count} items (~{est_str})",
                fg=ORANGE
            )
            if not getattr(self, "bnd_trace_running", False):
                self.bnd_eta_label.config(
                    text=f"⏱ Expected completion time: ~{est_str} (~{getattr(self, 'bnd_last_avg_time', 15.0):.0f}s/boundary)",
                    fg="#3498db"
                )

    def _bnd_tick_timer(self):
        """Live 1-second timer ticker updating ETA and elapsed time during boundary tracing."""
        if not getattr(self, "bnd_trace_running", False):
            return

        elapsed = time.time() - getattr(self, "bnd_trace_start_time", time.time())
        total = getattr(self, "bnd_trace_total", 1)
        done = getattr(self, "bnd_trace_current_idx", 0)
        durations = getattr(self, "bnd_trace_item_durations", [])

        if durations:
            avg_per_item = sum(durations) / len(durations)
        else:
            avg_per_item = getattr(self, "bnd_last_avg_time", 15.0)

        remaining_items = max(0, total - done)
        current_item_elapsed = time.time() - getattr(self, "bnd_current_item_start", time.time())
        est_remaining_for_current = max(0.0, avg_per_item - current_item_elapsed)
        eta_seconds = (remaining_items - 1) * avg_per_item + est_remaining_for_current if remaining_items > 0 else 0

        eta_str = self._bnd_format_duration(eta_seconds)
        elapsed_str = self._bnd_format_duration(elapsed)

        self.bnd_eta_label.config(
            text=f"⏱ Est. Remaining: ~{eta_str}  •  Elapsed: {elapsed_str}  •  ~{avg_per_item:.1f}s/boundary",
            fg="#3498db"
        )
        self.root.after(1000, self._bnd_tick_timer)


    # ── Boundary Actions ──────────────────────

    def _bnd_add_entry(self):
        query = self.bnd_entry.get().strip()
        if not query: return
        param = self.bnd_type.get()

        if param == "Pincode":
            full_query = query

        elif param == "District":
            state_code = self.bnd_state_var.get().strip().upper()
            if state_code and len(state_code) == 2:
                full_query = f"{query}, {state_code}"   # e.g. "Agra, UP"
            else:
                self._bnd_log(
                    f"⚠️  No state code for '{query}' — may return wrong district.\n"
                    f"   Enter 2-letter code (UP, DL, MH...) in State Code field.\n",
                    "warn"
                )
                full_query = query
            self.bnd_state_var.set("")

        elif param == "Locality":
            district = self.bnd_loc_district_var.get().strip()
            state_code = self.bnd_loc_state_var.get().strip().upper()
            formatted_q = format_block_query(query, parent=district, default_city=state_code)
            if state_code and len(state_code) == 2 and state_code not in formatted_q:
                full_query = f"{formatted_q}, {state_code}"
            else:
                full_query = formatted_q
            self.bnd_loc_district_var.set("")
            self.bnd_loc_state_var.set("")

        elif param == "State":
            full_query = f"{query} State" if "state" not in query.lower() else query

        elif param == "City":
            full_query = format_block_query(query)

        else:
            full_query = format_block_query(query)

        self.bnd_queries.append(full_query)
        self.bnd_listbox.insert(tk.END, f"  {len(self.bnd_queries)}.  {full_query}  [{param}]")
        self._bnd_update_queue_display()
        self.bnd_entry.delete(0, tk.END)
        self._bnd_log(f"➕ Added: {full_query} [{param}]\n", "info")

    def _bnd_browse(self):
        fp = filedialog.askopenfilename(
            title="Select query file",
            initialdir=get_downloads_dir(),
            filetypes=[("All supported", "*.csv *.txt *.xlsx *.xls"), ("CSV Files", "*.csv"), ("Excel Files", "*.xlsx *.xls"), ("Text Files", "*.txt")])
        if not fp: return
        try:
            # Check if file has polygon status column (e.g. Area Explorer export)
            filter_status = None
            if fp.lower().endswith((".xlsx", ".xls", ".csv")):
                try:
                    df_test = pd.read_excel(fp) if fp.lower().endswith((".xlsx", ".xls")) else pd.read_csv(fp)
                    st_col = next((c for c in df_test.columns if _normalize_location_col(c) in ("polygon_status", "status")), None)
                    if st_col is not None:
                        total_rows = len(df_test)
                        found_cnt = sum(df_test[st_col].astype(str).str.upper() == "FOUND")
                        missing_cnt = sum(df_test[st_col].astype(str).str.upper().isin(("NOT_FOUND", "NEEDS_RETRY")))
                        if found_cnt > 0 and missing_cnt > 0:
                            ans = messagebox.askyesno(
                                "Missing Polygons Detected",
                                f"Excel file contains {total_rows} total areas:\n\n"
                                f"• Already has polygon (FOUND): {found_cnt}\n"
                                f"• Missing polygons (NOT_FOUND / NEEDS_RETRY): {missing_cnt}\n\n"
                                f"Would you like to import ONLY the {missing_cnt} missing areas?\n\n"
                                f"(Click 'Yes' to import only missing areas, or 'No' to import all {total_rows} areas)"
                            )
                            if ans:
                                filter_status = ("NOT_FOUND", "NEEDS_RETRY")
                except Exception:
                    pass

            items = read_locations_from_file(fp, filter_status=filter_status)
            if not items:
                param = self.bnd_type.get()
                tpl = BND_SAMPLE_TEMPLATES.get(param, BND_SAMPLE_TEMPLATES["District"])
                messagebox.showwarning(
                    "No Locations Found",
                    f"No location entries could be found in:\n{os.path.basename(fp)}\n\n"
                    f"Expected format for {param}:\n{tpl['hint']}\n\n"
                    f"You can click '{tpl['btn_text']}' to export the sample pattern."
                )
                return

            for item in items:
                self.bnd_queries.append(item)
                self.bnd_listbox.insert(tk.END, f"  {len(self.bnd_queries)}.  {item}")
            self._bnd_update_queue_display()
            filter_desc = " [Missing Only]" if filter_status else ""
            self.bnd_file_label.config(text=f"✅  {os.path.basename(fp)}{filter_desc} ({len(items)} items)", fg=GREEN)
            self._bnd_log(f"✅ Loaded {len(items)} boundary queries from {os.path.basename(fp)}{filter_desc}\n", "success")
        except Exception as e:
            messagebox.showerror("File Error", f"Error reading file:\n\n{e}")

    def _bnd_create_sample(self):
        param = self.bnd_type.get()
        tpl = BND_SAMPLE_TEMPLATES.get(param, BND_SAMPLE_TEMPLATES["District"])
        fp = filedialog.asksaveasfilename(
            title=f"Save Sample {param} CSV Template",
            initialdir=get_downloads_dir(),
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=tpl["filename"]
        )
        if not fp:
            return
        try:
            with open(fp, "w", encoding="utf-8") as f:
                f.write(tpl["content"])
            messagebox.showinfo(
                "Sample Created",
                f"Sample {param} template created successfully:\n\n{fp}\n\n"
                f"Pattern:\n{tpl['hint']}\n\n"
                "You can edit this CSV in Excel or Notepad and import it directly."
            )
            if os.path.exists(fp):
                os.startfile(fp)
        except Exception as exc:
            messagebox.showerror("Error", f"Could not create sample file:\n{exc}")

    def _bnd_clear(self):
        self.bnd_queries.clear()
        self.bnd_listbox.delete(0, tk.END)
        self._bnd_update_queue_display()

    def _bnd_pick_folder(self):
        folder = filedialog.askdirectory(title="Select output folder", initialdir=self.bnd_out_var.get() or get_downloads_dir())
        if folder:
            self.bnd_out_var.set(folder)
            self._bnd_sync_reset_approval()

    # ── Multi-area boundary synchronization ─────────────────────

    def _bnd_sync_script_path(self):
        """Locate the tested multi-area sync tool beside frontend.py."""
        project_dir = Path(__file__).resolve().parent
        candidates = [
            project_dir / "sync_boundaries.py",
            project_dir / "sync_boundaries_multi.py",
        ]

        for candidate in candidates:
            if candidate.is_file():
                return candidate

        return candidates[0]

    def _bnd_sync_signature(self):
        """
        Record path, file size and modification time.

        Apply is allowed only when the exact files that passed the safety
        check are still selected and have not changed.
        """
        signature = []

        for filepath in sorted(
            self.bnd_sync_files,
            key=lambda value: os.path.normcase(os.path.abspath(value)),
        ):
            absolute_path = os.path.abspath(filepath)

            try:
                stat = os.stat(absolute_path)
                signature.append((
                    os.path.normcase(absolute_path),
                    stat.st_size,
                    stat.st_mtime_ns,
                ))
            except OSError:
                signature.append((
                    os.path.normcase(absolute_path),
                    -1,
                    -1,
                ))

        output_dir = os.path.normcase(
            os.path.abspath(
                self.bnd_out_var.get().strip() or "boundaries"
            )
        )

        return tuple(signature), output_dir

    def _bnd_sync_reset_approval(self):
        self.bnd_sync_checked_signature = None

        if hasattr(self, "bnd_sync_apply_btn"):
            self.bnd_sync_apply_btn.config(
                state="disabled",
                text="🔗 Sync & Export",
            )

    def _bnd_sync_refresh_list(self):
        self.bnd_sync_listbox.delete(0, tk.END)

        for index, filepath in enumerate(
            self.bnd_sync_files,
            start=1,
        ):
            self.bnd_sync_listbox.insert(
                tk.END,
                f"  {index}. {os.path.basename(filepath)}",
            )

        count = len(self.bnd_sync_files)
        self.bnd_sync_count.config(
            text=f"{count} selected",
            fg=ACCENT if count >= 2 else TEXT_GRAY,
        )

        self._bnd_sync_reset_approval()

    def _bnd_sync_add_files(self, filepaths):
        existing = {
            os.path.normcase(os.path.abspath(filepath))
            for filepath in self.bnd_sync_files
        }

        added = 0

        for filepath in filepaths:
            absolute_path = os.path.abspath(filepath)
            extension = os.path.splitext(absolute_path)[1].lower()

            if extension not in {".geojson", ".json"}:
                continue

            normalized = os.path.normcase(absolute_path)

            if normalized in existing:
                continue

            self.bnd_sync_files.append(absolute_path)
            existing.add(normalized)
            added += 1

        self._bnd_sync_refresh_list()

        if added:
            self._bnd_log(
                f"🔗 Added {added} boundary file(s) for synchronization.\n",
                "info",
            )

    def _bnd_sync_browse(self):
        initial_folder = self.bnd_out_var.get().strip() or "boundaries"

        if not os.path.isdir(initial_folder):
            initial_folder = get_downloads_dir()

        selected = filedialog.askopenfilenames(
            title="Select two or more boundary GeoJSON files",
            initialdir=initial_folder,
            filetypes=[
                ("GeoJSON files", "*.geojson"),
                ("JSON files", "*.json"),
                ("All files", "*.*"),
            ],
        )

        if selected:
            self._bnd_sync_add_files(selected)

    def _bnd_sync_load_folder(self):
        folder = self.bnd_out_var.get().strip() or os.path.join(get_downloads_dir(), "JarvisMaps_Boundaries")

        if not os.path.isdir(folder):
            folder = filedialog.askdirectory(
                title="Select folder containing boundary GeoJSON files",
                initialdir=get_downloads_dir()
            )

        if not folder:
            return

        excluded_markers = (
            "_synced",
            "shared_edge",
            "shared_edges",
            "sync_report",
            "overlap_report",
        )

        files = []

        for path in sorted(Path(folder).glob("*.geojson")):
            lowered_name = path.name.lower()

            if any(
                marker in lowered_name
                for marker in excluded_markers
            ):
                continue

            files.append(str(path.resolve()))

        if not files:
            messagebox.showinfo(
                "No GeoJSON Files",
                "No original boundary GeoJSON files were found in this folder.",
            )
            return

        self._bnd_sync_add_files(files)

    def _bnd_sync_remove_selected(self):
        selected_indexes = list(
            self.bnd_sync_listbox.curselection()
        )

        if not selected_indexes:
            return

        for index in reversed(selected_indexes):
            del self.bnd_sync_files[index]

        self._bnd_sync_refresh_list()

    def _bnd_sync_clear(self):
        self.bnd_sync_files.clear()
        self._bnd_sync_refresh_list()

    def _bnd_sync_start(self, apply=False):
        if self.bnd_sync_is_running:
            messagebox.showwarning(
                "Synchronization Running",
                "A boundary synchronization job is already running.",
            )
            return

        if self.bnd_trace_running:
            messagebox.showwarning(
                "Boundary Tracing Running",
                "Wait for boundary tracing to finish before synchronization.",
            )
            return

        if len(self.bnd_sync_files) < 2:
            messagebox.showwarning(
                "Select Boundaries",
                "Select at least two GeoJSON boundary files.",
            )
            return

        missing_files = [
            filepath
            for filepath in self.bnd_sync_files
            if not os.path.isfile(filepath)
        ]

        if missing_files:
            messagebox.showerror(
                "Missing Boundary File",
                "These files are missing:\n\n"
                + "\n".join(
                    os.path.basename(filepath)
                    for filepath in missing_files
                ),
            )
            self._bnd_sync_reset_approval()
            return

        script_path = self._bnd_sync_script_path()

        if not script_path.is_file():
            messagebox.showerror(
                "Sync Tool Missing",
                "Place 'sync_multiple_boundaries.py' in the same folder "
                "as frontend.py.",
            )
            return

        current_signature = self._bnd_sync_signature()

        if apply and current_signature != self.bnd_sync_checked_signature:
            messagebox.showwarning(
                "Safety Check Required",
                "The selected files or output folder changed. "
                "Run Safety Check again before Sync & Export.",
            )
            self._bnd_sync_reset_approval()
            return

        if apply:
            confirmed = messagebox.askyesno(
                "Confirm Synchronization",
                "Create new synchronized KML, KMZ and GeoJSON files?\n\n"
                "Original boundary files will not be changed.",
            )

            if not confirmed:
                return
        else:
            self._bnd_sync_reset_approval()

        self.bnd_sync_is_running = True
        self.bnd_sync_check_btn.config(
            state="disabled",
            text="⏳ Checking..." if not apply else "🛡 Safety Check",
        )
        self.bnd_sync_apply_btn.config(
            state="disabled",
            text="⏳ Syncing..." if apply else "🔗 Sync & Export",
        )
        self.bnd_sync_open_btn.config(state="disabled")
        self.bnd_start_btn.config(state="disabled")
        self.bnd_status.config(
            text=(
                "🔗 Synchronizing boundaries..."
                if apply
                else "🛡 Checking boundary safety..."
            ),
            fg=ACCENT,
        )

        self._bnd_log(
            "\n"
            + ("🔗 SYNC & EXPORT\n" if apply else "🛡 SAFETY CHECK\n"),
            "info",
        )

        for filepath in self.bnd_sync_files:
            self._bnd_log(
                f"   • {os.path.basename(filepath)}\n",
                "normal",
            )

        selected_files = list(self.bnd_sync_files)

        threading.Thread(
            target=self._bnd_sync_run,
            args=(apply, current_signature, selected_files),
            daemon=True,
        ).start()

    def _bnd_sync_run(self, apply, checked_signature, selected_files):
        script_path = self._bnd_sync_script_path()
        output_dir = os.path.abspath(
            self.bnd_out_var.get().strip() or "boundaries"
        )

        os.makedirs(output_dir, exist_ok=True)

        output_lines = []
        safe_result = False
        synced_output_dir = None
        return_code = 0

        def _handle_line(line):
            nonlocal safe_result, synced_output_dir
            output_lines.append(line)
            upper_line = line.upper()

            if "RESULT: SAFE TO SYNCHRONIZE" in upper_line:
                safe_result = True
                tag = "success"
            elif (
                "BLOCKED FOR SAFETY" in upper_line
                or upper_line.startswith("ERROR")
                or "TRACEBACK" in upper_line
            ):
                tag = "error"
            elif (
                "WARNING" in upper_line
                or "CHANGED TOO MUCH" in upper_line
            ):
                tag = "warn"
            elif (
                "CREATED:" in upper_line
                or "ORIGINAL FILES WERE NOT MODIFIED" in upper_line
            ):
                tag = "success"
            elif (
                "OVERLAP" in upper_line
                or "AREA CHANGE" in upper_line
                or "SHARED EDGE" in upper_line
            ):
                tag = "info"
            else:
                tag = "normal"

            if line.startswith("Output folder:"):
                synced_output_dir = line.split(":", 1)[1].strip()

            self._bnd_log(line + "\n", tag)

        # In a frozen executable, sys.executable points to JarvisMapsAI.exe, not python.exe.
        # Spawning it relaunches the GUI application. Run in-process for 100% frozen safety.
        is_frozen = getattr(sys, "frozen", False)
        ran_in_process = False

        if is_frozen or not script_path.is_file():
            try:
                import sync_boundaries
                from contextlib import redirect_stdout, redirect_stderr

                class _StreamEmitter:
                    def __init__(self, callback):
                        self.callback = callback
                        self.buf = ""
                    def write(self, text):
                        self.buf += text
                        while "\n" in self.buf:
                            l, self.buf = self.buf.split("\n", 1)
                            self.callback(l.rstrip("\r"))
                    def flush(self):
                        if self.buf:
                            self.callback(self.buf.rstrip("\r"))
                            self.buf = ""

                cli_args = [*selected_files, "--output-dir", output_dir]
                if apply:
                    cli_args.append("--apply")

                emitter = _StreamEmitter(_handle_line)
                with redirect_stdout(emitter), redirect_stderr(emitter):
                    return_code = sync_boundaries.main(cli_args)
                emitter.flush()
                ran_in_process = True
            except Exception as exc:
                if is_frozen:
                    return_code = -1
                    self._bnd_log(f"❌ Synchronization error: {exc}\n", "error")
                    ran_in_process = True

        if not ran_in_process:
            command = [
                sys.executable,
                str(script_path),
                *selected_files,
                "--output-dir",
                output_dir,
            ]
            if apply:
                command.append("--apply")

            try:
                creation_flags = 0
                if os.name == "nt":
                    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

                process = subprocess.Popen(
                    command,
                    cwd=str(script_path.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creation_flags,
                )

                if process.stdout is not None:
                    for raw_line in process.stdout:
                        _handle_line(raw_line.rstrip("\r\n"))

                return_code = process.wait()
            except Exception as exc:
                return_code = -1
                self._bnd_log(f"❌ Synchronization error: {exc}\n", "error")

        success = (
            return_code == 0
            and safe_result
        )

        self.root.after(
            0,
            self._bnd_sync_done,
            apply,
            success,
            checked_signature,
            synced_output_dir,
            return_code,
        )

    def _bnd_sync_done(
        self,
        apply,
        success,
        checked_signature,
        synced_output_dir,
        return_code,
    ):
        self.bnd_sync_is_running = False
        self.bnd_sync_check_btn.config(
            state="normal",
            text="🛡 Safety Check",
        )
        self.bnd_start_btn.config(
            state="normal",
            text="▶  Trace Boundaries",
        )

        if success and not apply:
            if self._bnd_sync_signature() == checked_signature:
                self.bnd_sync_checked_signature = checked_signature
                self.bnd_sync_apply_btn.config(
                    state="normal",
                    text="🔗 Sync & Export",
                )
                self.bnd_status.config(
                    text="✅ Safety check passed — ready to sync",
                    fg=GREEN,
                )
                self._bnd_log(
                    "✅ Safety check passed. Sync & Export is now enabled.\n",
                    "success",
                )
            else:
                self._bnd_sync_reset_approval()
                self.bnd_status.config(
                    text="⚠️ Files changed — run Safety Check again",
                    fg=YELLOW,
                )
                self._bnd_log(
                    "⚠️ The selected files changed during the check. "
                    "Run Safety Check again.\n",
                    "warn",
                )

        elif success and apply:
            self.bnd_sync_checked_signature = None
            self.bnd_sync_apply_btn.config(
                state="disabled",
                text="🔗 Sync & Export",
            )

            if synced_output_dir and os.path.isdir(synced_output_dir):
                self.bnd_sync_last_output_dir = synced_output_dir
                self.bnd_sync_open_btn.config(
                    state="normal",
                    fg=GREEN,
                )

            self.bnd_status.config(
                text="✅ Boundaries synchronized and exported",
                fg=GREEN,
            )
            self._bnd_log(
                "✅ Synchronization completed. Open only the new "
                "*_synced.kml files in Google Earth.\n",
                "success",
            )

        else:
            self._bnd_sync_reset_approval()
            self.bnd_status.config(
                text="❌ Synchronization blocked or failed",
                fg=RED,
            )

            if return_code == 2:
                self._bnd_log(
                    "🛡 No files were changed because the safety "
                    "checks blocked this operation.\n",
                    "warn",
                )
            else:
                self._bnd_log(
                    f"❌ Sync tool exited with code {return_code}.\n",
                    "error",
                )

    def _bnd_sync_open_output(self):
        folder = self.bnd_sync_last_output_dir

        if folder and os.path.isdir(folder):
            os.startfile(folder)
            return

        messagebox.showinfo(
            "Not Found",
            "No synchronized output folder has been created yet.",
        )

    # ─────────────────────────────────────────────────────────
    #  GEOTIFF MOSAIC MERGER METHODS
    # ─────────────────────────────────────────────────────────

    def _bnd_tiff_refresh_list(self):
        if not hasattr(self, "bnd_tiff_listbox") or self.bnd_tiff_listbox is None:
            return

        self.bnd_tiff_listbox.delete(0, tk.END)
        for p in self.bnd_tiff_files:
            self.bnd_tiff_listbox.insert(tk.END, f"  {os.path.basename(p)}  ({p})")

        count = len(self.bnd_tiff_files)
        self.bnd_tiff_count.config(text=f"{count} selected")

        if count >= 2 and not self.bnd_tiff_is_running:
            self.bnd_tiff_merge_btn.config(state="normal", bg=GREEN, fg="white")
        else:
            self.bnd_tiff_merge_btn.config(state="disabled", bg=BG_INPUT, fg=TEXT_GRAY)

    def _bnd_tiff_add_files(self, filepaths):
        existing = set(self.bnd_tiff_files)
        added = 0
        for p in filepaths:
            abs_p = os.path.abspath(p)
            if abs_p.lower().endswith((".tif", ".tiff")) and os.path.isfile(abs_p) and abs_p not in existing:
                self.bnd_tiff_files.append(abs_p)
                existing.add(abs_p)
                added += 1
        self._bnd_tiff_refresh_list()
        return added

    def _bnd_tiff_browse(self):
        files = filedialog.askopenfilenames(
            title="Select GeoTIFF Files to Merge",
            initialdir=self.bnd_out_var.get() or get_downloads_dir(),
            filetypes=[("GeoTIFF Images", "*.tif *.tiff"), ("All files", "*.*")],
        )
        if files:
            self._bnd_tiff_add_files(files)

    def _bnd_tiff_load_folder(self):
        folder = self.bnd_out_var.get().strip() or os.path.join(get_downloads_dir(), "JarvisMaps_Boundaries")
        if not os.path.isabs(folder):
            folder = os.path.abspath(folder)

        if not os.path.isdir(folder):
            messagebox.showinfo("Folder Not Found", f"The folder does not exist:\n{folder}")
            return

        found = []
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith((".tif", ".tiff")) and not f.startswith("merged_"):
                    found.append(os.path.join(root, f))

        if not found:
            messagebox.showinfo("No GeoTIFFs Found", f"No .tif files found in:\n{folder}")
            return

        added = self._bnd_tiff_add_files(found)
        self._bnd_log(f"📁 Loaded {added} GeoTIFF file(s) from {folder}\n", "info")

    def _bnd_tiff_remove_selected(self):
        sel = list(self.bnd_tiff_listbox.curselection())
        for idx in reversed(sel):
            if idx < len(self.bnd_tiff_files):
                del self.bnd_tiff_files[idx]
        self._bnd_tiff_refresh_list()

    def _bnd_tiff_clear(self):
        self.bnd_tiff_files.clear()
        self._bnd_tiff_refresh_list()

    def _bnd_tiff_start(self):
        if self.bnd_tiff_is_running:
            messagebox.showwarning("Busy", "A GeoTIFF merge is already running.")
            return

        if len(self.bnd_tiff_files) < 2:
            messagebox.showwarning("Select GeoTIFFs", "Please select at least 2 GeoTIFF files to merge.")
            return

        folder = self.bnd_out_var.get().strip() or os.path.join(get_downloads_dir(), "JarvisMaps_Boundaries")
        if not os.path.isabs(folder):
            folder = os.path.abspath(folder)
        os.makedirs(folder, exist_ok=True)

        out_path = filedialog.asksaveasfilename(
            title="Save Merged GeoTIFF As",
            initialdir=folder,
            initialfile="merged_geotiff.tif",
            defaultextension=".tif",
            filetypes=[("GeoTIFF Image", "*.tif"), ("All files", "*.*")],
        )

        if not out_path:
            return

        self.bnd_tiff_is_running = True
        self.bnd_tiff_merge_btn.config(state="disabled", bg=BG_INPUT, fg=TEXT_GRAY)
        self.bnd_status.config(text="⚙️  Merging GeoTIFFs...", fg=YELLOW)

        blend_mode = self.bnd_tiff_blend_mode.get()
        enhance_lines = self.bnd_tiff_enhance_var.get()
        inputs = list(self.bnd_tiff_files)

        thread = threading.Thread(
            target=self._bnd_tiff_run,
            args=(inputs, out_path, blend_mode, enhance_lines),
            daemon=True,
        )
        thread.start()

    def _bnd_tiff_run(self, input_files, output_path, blend_mode, enhance_lines=False):
        try:
            self._bnd_log("\n════════════════════════════════════════════════════════\n", "info")
            self._bnd_log("🗺️  STARTING GEOTIFF MOSAIC MERGE\n", "orange")
            self._bnd_log(f"   Inputs: {len(input_files)} GeoTIFF files\n", "normal")
            for f in input_files:
                self._bnd_log(f"   • {os.path.basename(f)}\n", "normal")
            self._bnd_log(f"   Blend Mode: {blend_mode}\n", "normal")
            self._bnd_log(f"   Enhance Lines: {'Yes (Vivid High-Contrast Red)' if enhance_lines else 'No'}\n", "normal")
            self._bnd_log(f"   Destination: {output_path}\n\n", "normal")

            def _log_callback(msg):
                tag = "info" if any(w in msg for w in ["Placed", "Combined", "Successfully", "Preserved", "Applied", "Adaptively", "Loaded", "Large", "Canvas", "Notice"]) else "normal"
                self._bnd_log(f"{msg}\n", tag)

            result = merge_geotiffs(
                input_paths=input_files,
                output_path=output_path,
                blend_mode=blend_mode,
                enhance_lines=enhance_lines,
                log=_log_callback,
            )

            self._bnd_log(f"\n✅ Merged GeoTIFF created successfully!\n", "success")
            self._bnd_log(f"   Dimensions: {result['width']} x {result['height']} px\n", "success")
            if result.get("rescaled"):
                self._bnd_log(f"   Resolution: Adaptively scaled by {result.get('rescale_factor', 1.0):.2f}x (Safe {result['width']}x{result['height']}px)\n", "info")
            self._bnd_log(f"   Bounds: Lng [{result['min_lng']:.5f}, {result['max_lng']:.5f}], Lat [{result['min_lat']:.5f}, {result['max_lat']:.5f}]\n", "success")
            self._bnd_log(f"   File: {output_path}\n", "success")
            self._bnd_log("════════════════════════════════════════════════════════\n\n", "info")

            self.root.after(0, lambda: self._bnd_tiff_done(output_path, None))
        except Exception as e:
            self._bnd_log(f"\n❌ Failed to merge GeoTIFF files: {e}\n", "error")
            self.root.after(0, lambda: self._bnd_tiff_done(None, str(e)))

    def _bnd_tiff_done(self, output_path, error):
        self.bnd_tiff_is_running = False
        self._bnd_tiff_refresh_list()
        self.bnd_status.config(text="⬤  Ready", fg=GREEN)

        if error:
            messagebox.showerror("Merge Failed", f"Failed to merge GeoTIFF files:\n\n{error}")
        else:
            self.bnd_last_merged_tiff = output_path
            self.bnd_tiff_open_btn.config(state="normal", bg=ACCENT, fg="white")
            messagebox.showinfo(
                "Merge Complete",
                f"Successfully created merged GeoTIFF:\n\n{output_path}\n\nYou can open it in QGIS, ArcGIS, or Google Earth.",
            )

    def _bnd_tiff_open_output(self):
        target = self.bnd_last_merged_tiff
        if target and os.path.isfile(target):
            try:
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(target)}"])
            except Exception:
                os.startfile(os.path.dirname(target))
        elif target and os.path.isdir(os.path.dirname(target)):
            os.startfile(os.path.dirname(target))
        else:
            messagebox.showinfo("Not Found", "No merged GeoTIFF has been created yet.")

    def _bnd_tiff_enhance_action(self):
        # Pick target: either selected item in listbox, or last merged file, or browse
        target = None
        sel = list(self.bnd_tiff_listbox.curselection())
        if sel and sel[0] < len(self.bnd_tiff_files):
            target = self.bnd_tiff_files[sel[0]]
        elif self.bnd_last_merged_tiff and os.path.isfile(self.bnd_last_merged_tiff):
            target = self.bnd_last_merged_tiff
        elif os.path.isfile(os.path.join("output", "merged_geotiff.tif")):
            target = os.path.abspath(os.path.join("output", "merged_geotiff.tif"))
        elif os.path.isfile("merged_geotiff.tif"):
            target = os.path.abspath("merged_geotiff.tif")
        else:
            target = filedialog.askopenfilename(
                title="Select GeoTIFF to Enhance Lines",
                initialdir=self.bnd_out_var.get() or get_downloads_dir(),
                filetypes=[("GeoTIFF Images", "*.tif *.tiff"), ("All files", "*.*")],
            )

        if not target or not os.path.isfile(target):
            messagebox.showinfo("Select File", "Please select a GeoTIFF file from the list or file dialog to enhance.")
            return

        base, ext = os.path.splitext(target)
        out_p = f"{base}_enhanced{ext}"
        try:
            self._bnd_log(f"🪄 Enhancing boundary lines in {os.path.basename(target)}...\n", "info")
            enhanced_path = enhance_geotiff_lines(
                input_path=target,
                output_path=out_p,
                color=(255, 30, 30),
                thickness=1,
                logger=lambda msg: self._bnd_log(f"{msg}\n", "info"),
            )
            self._bnd_log(f"✅ Enhanced GeoTIFF saved -> {enhanced_path}\n", "success")
            self.bnd_last_merged_tiff = enhanced_path
            self.bnd_tiff_open_btn.config(state="normal", bg=ACCENT, fg="white")
            messagebox.showinfo("Line Enhancement Complete", f"Enhanced GeoTIFF created:\n\n{enhanced_path}\n\nBoundary lines are now bold, high-contrast, and easy to detect!")
        except Exception as e:
            self._bnd_log(f"❌ Line enhancement failed: {e}\n", "error")
            messagebox.showerror("Enhancement Failed", str(e))

    def _bnd_start(self):
        if self.bnd_sync_is_running:
            messagebox.showwarning(
                "Synchronization Running",
                "Wait for boundary synchronization to finish.",
            )
            return
        if self.bnd_trace_running:
            messagebox.showwarning(
                "Boundary Tracing Running",
                "Boundary tracing is already running.",
            )
            return
        if not self.bnd_queries:
            messagebox.showwarning("Empty Queue",
                "Please add at least one location to the queue.")
            return

        try:
            self.bnd_trace_running = True
            self.bnd_trace_start_time = time.time()
            self.bnd_trace_total = len(self.bnd_queries)
            self.bnd_trace_current_idx = 0
            self.bnd_trace_item_durations = []
            self.bnd_current_item_start = time.time()

            # Set headless state in Playwright pipeline
            is_headless = self.bnd_headless_var.get() if hasattr(self, "bnd_headless_var") else True
            try:
                import playwright_pipeline
                playwright_pipeline.BOUNDARY_HEADLESS = is_headless
            except Exception:
                pass

            self.bnd_start_btn.config(state="disabled", text="⏳ Tracing...")
            self.bnd_open_btn.config(state="disabled")
            if hasattr(self, "cb_bnd_headless"):
                self.cb_bnd_headless.config(state="disabled")
            self.bnd_progress_var.set(0)

            initial_est = self.bnd_trace_total * getattr(self, "bnd_last_avg_time", 15.0)
            initial_est_str = self._bnd_format_duration(initial_est)
            self.bnd_status.config(
                text=f"🔄  Tracing boundaries... (Est. ~{initial_est_str})",
                fg=ORANGE
            )
            self.bnd_eta_label.config(
                text=f"⏱ Est. Remaining: ~{initial_est_str}  •  Starting...",
                fg="#3498db"
            )
            self.bnd_prog_label.config(text=f"0/{self.bnd_trace_total} done (0%)")

            self.root.after(1000, self._bnd_tick_timer)
            threading.Thread(target=self._bnd_run, daemon=True).start()
        except Exception as exc:
            self.bnd_trace_running = False
            self.bnd_start_btn.config(state="normal", text="▶  Trace Boundaries")
            self.bnd_open_btn.config(state="normal" if os.path.exists(self.bnd_out_var.get() or "boundaries") else "disabled")
            if hasattr(self, "cb_bnd_headless"):
                self.cb_bnd_headless.config(state="normal")
            messagebox.showerror("Execution Error", f"Could not start boundary tracing:\n{exc}")

    def _bnd_run(self):
        total = len(self.bnd_queries)
        output_dir = self.bnd_out_var.get() or "boundaries"
        success_count = 0

        # Enforce headless mode in worker thread
        is_headless = self.bnd_headless_var.get() if hasattr(self, "bnd_headless_var") else True
        try:
            import playwright_pipeline
            playwright_pipeline.BOUNDARY_HEADLESS = is_headless
        except Exception:
            pass

        initial_est = total * getattr(self, "bnd_last_avg_time", 15.0)
        self._bnd_log(
            f"🚀 Starting boundary extraction for {total} location(s)\n"
            f"   ⏱ Expected total completion time: ~{self._bnd_format_duration(initial_est)} "
            f"(~{getattr(self, 'bnd_last_avg_time', 15.0):.0f}s per boundary)\n",
            "info"
        )

        for i, query in enumerate(self.bnd_queries, 1):
            item_start = time.time()
            self.bnd_current_item_start = item_start
            self._bnd_log(
                f"\n🗺️  [{i}/{total}] {query}\n",
                "orange",
            )

            try:
                result = export_boundary(
                    query,
                    output_dir,
                )

            except Exception as exc:
                result = None

                self._bnd_log(
                    f"   ❌ Boundary extraction error: {exc}\n",
                    "error",
                )

            item_duration = time.time() - item_start
            self.bnd_trace_item_durations.append(item_duration)
            self.bnd_trace_current_idx = i
            self.bnd_last_avg_time = sum(self.bnd_trace_item_durations) / len(self.bnd_trace_item_durations)

            if not isinstance(result, dict):
                self._bnd_log(
                    f"   ❌ Boundary extraction returned no result. ({item_duration:.1f}s)\n",
                    "error",
                )

            elif result.get("status") == "Success":
                success_count += 1

                display_name = str(
                    result.get(
                        "display_name",
                        query,
                    )
                )

                self._bnd_log(
                    f"   ✅ {display_name[:55]} ({item_duration:.1f}s)\n",
                    "success",
                )

                geojson_path = result.get("geojson_path")
                kml_path = result.get("kml_path")
                kmz_path = result.get("kmz_path")

                if geojson_path:
                    self._bnd_log(
                        "   📄 GeoJSON → "
                        f"{os.path.basename(geojson_path)}\n",
                        "normal",
                    )

                if kml_path:
                    self._bnd_log(
                        "   🗺️  KML    → "
                        f"{os.path.basename(kml_path)}\n",
                        "normal",
                    )

                if kmz_path:
                    self._bnd_log(
                        "   📦 KMZ    → "
                        f"{os.path.basename(kmz_path)}\n",
                        "normal",
                    )

            else:
                error_message = (
                    result.get("error")
                    or result.get("status")
                    or "Boundary extraction failed"
                )

                self._bnd_log(
                    f"   ❌ {error_message} ({item_duration:.1f}s)\n",
                    "error",
                )

            # Persist boundary result into database
            try:
                if isinstance(result, dict):
                    save_boundary_result(result)
                else:
                    save_boundary_result({
                        "query": query,
                        "display_name": query,
                        "status": "Failed",
                        "error": "Boundary extraction returned no result",
                    })
            except Exception:
                pass

            pct = (
                i / total
            ) * 100

            self.root.after(
                0,
                self.bnd_progress_var.set,
                pct,
            )

            self.root.after(
                0,
                self.bnd_prog_label.config,
                {
                    "text": f"{i}/{total} done ({int(pct)}%)",
                },
            )

        total_elapsed = time.time() - self.bnd_trace_start_time
        avg_time = total_elapsed / max(1, total)

        if success_count == total:
            completion_tag = "success"
            completion_icon = "✅"
        elif success_count > 0:
            completion_tag = "orange"
            completion_icon = "⚠️"
        else:
            completion_tag = "error"
            completion_icon = "❌"

        self._bnd_log(
            f"\n{completion_icon} Done — "
            f"{success_count}/{total} boundaries saved "
            f"to '{output_dir}' in {self._bnd_format_duration(total_elapsed)} "
            f"(avg {avg_time:.1f}s/boundary)\n",
            completion_tag,
        )

        self.root.after(
            0,
            self._bnd_done,
            success_count,
            total,
            total_elapsed,
        )

    def _bnd_done(self, success, total, total_elapsed=None):
        self.bnd_trace_running = False
        self.bnd_start_btn.config(
            state="normal",
            text="▶  Trace Boundaries",
        )
        if hasattr(self, "cb_bnd_headless"):
            self.cb_bnd_headless.config(state="normal")

        if total_elapsed is None:
            total_elapsed = time.time() - getattr(self, "bnd_trace_start_time", time.time())

        elapsed_str = self._bnd_format_duration(total_elapsed)
        avg = total_elapsed / max(1, total)

        if success == total:
            status_text = (
                f"✅  {success}/{total} boundaries saved in {elapsed_str}"
            )
            status_color = GREEN

        elif success > 0:
            status_text = (
                f"⚠️  {success}/{total} boundaries saved in {elapsed_str}"
            )
            status_color = ORANGE

        else:
            status_text = (
                f"❌  0/{total} boundaries saved in {elapsed_str}"
            )
            status_color = RED

        self.bnd_status.config(
            text=status_text,
            fg=status_color,
        )

        self.bnd_eta_label.config(
            text=f"✅ Finished in {elapsed_str} (avg {avg:.1f}s/boundary)",
            fg=GREEN
        )
        self.bnd_prog_label.config(text=f"{total}/{total} done (100%)")

        self.bnd_progress_var.set(100)

        self.bnd_open_btn.config(
            state=(
                "normal"
                if os.path.exists(
                    self.bnd_out_var.get()
                    or "boundaries"
                )
                else "disabled"
            ),
            fg=(
                GREEN
                if success > 0
                else TEXT_GRAY
            ),
        )

    def _bnd_open_folder(self):
        folder = self.bnd_out_var.get() or "boundaries"
        if os.path.exists(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo("Not Found", f"Folder '{folder}' doesn't exist yet.")

    def _bnd_log(self, message: str, tag: str = "normal"):
        def _write():
            self.bnd_log.config(state="normal")
            self.bnd_log.insert(tk.END, message, tag)
            self.bnd_log.see(tk.END)
            self.bnd_log.config(state="disabled")
        self.root.after(0, _write)




    # ══════════════════════════════════════════
    #  TAB 3 — AMENITY FINDER
    # ══════════════════════════════════════════

    def _build_amenity_tab(self, parent):
        self.amenity_results = []

        # ── FULL-PAGE SCROLL CONTAINER ─────────────────────────────
        # The entire Amenity Finder content scrolls vertically as one page.
        # The results table keeps its own internal scrollbars.
        page_shell = tk.Frame(parent, bg=BG_DARK)
        page_shell.pack(fill="both", expand=True)

        page_canvas = tk.Canvas(page_shell, bg=BG_DARK, highlightthickness=0, bd=0)
        page_scroll = tk.Scrollbar(page_shell, orient="vertical", command=page_canvas.yview)
        page_canvas.configure(yscrollcommand=page_scroll.set)
        page_scroll.pack(side="right", fill="y")
        page_canvas.pack(side="left", fill="both", expand=True)

        page_frame = tk.Frame(page_canvas, bg=BG_DARK)
        page_window = page_canvas.create_window((0, 0), window=page_frame, anchor="nw")

        def _update_scrollregion(event=None):
            page_canvas.configure(scrollregion=page_canvas.bbox("all"))

        def _fit_page_width(event):
            page_canvas.itemconfigure(page_window, width=event.width)

        page_frame.bind("<Configure>", _update_scrollregion)
        page_canvas.bind("<Configure>", _fit_page_width)

        def _on_mousewheel(event):
            if event.delta:
                page_canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        def _bind_mousewheel(event=None):
            page_canvas.bind_all("<MouseWheel>", _on_mousewheel)
            page_canvas.bind_all("<Button-4>", lambda e: page_canvas.yview_scroll(-3, "units"))
            page_canvas.bind_all("<Button-5>", lambda e: page_canvas.yview_scroll(3, "units"))

        def _unbind_mousewheel(event=None):
            page_canvas.unbind_all("<MouseWheel>")
            page_canvas.unbind_all("<Button-4>")
            page_canvas.unbind_all("<Button-5>")

        page_canvas.bind("<Enter>", _bind_mousewheel)
        page_canvas.bind("<Leave>", _unbind_mousewheel)

        # ── Bottom toolbar (now part of the scrollable page) ────────
        amt_bar = tk.Frame(page_frame, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=14)
        amt_bar.pack(fill="x", side="bottom", pady=(10, 0))
        self.amt_status = tk.Label(amt_bar, text="⬤  Ready",
                                   font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.amt_status.pack(side="left")
        self.amt_kml_btn = tk.Button(amt_bar, text="🗺️ Export KML",
                                     font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                     relief="flat", cursor="hand2", padx=14, pady=8,
                                     state="disabled", command=self._amt_export_kml)
        self.amt_kml_btn.pack(side="right", padx=(8, 0))
        self.amt_excel_btn = tk.Button(amt_bar, text="📊 Export Excel",
                                       font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                       relief="flat", cursor="hand2", padx=14, pady=8,
                                       state="disabled", command=self._amt_export_excel)
        self.amt_excel_btn.pack(side="right", padx=(8, 0))
        self.amt_search_btn = tk.Button(amt_bar, text="▶  Search Amenities",
                                        font=FONT_BTN, bg="#8e44ad", fg="white",
                                        relief="flat", cursor="hand2", padx=22, pady=8,
                                        command=self._amt_search)
        self.amt_search_btn.pack(side="right")

        # ── Body ──────────────────────────────
        body = tk.Frame(page_frame, bg=BG_DARK, height=720)
        body.pack(fill="both", expand=True, padx=30, pady=20)

        left, right = self._split(body, 360)

        # ── LEFT: Search Controls ─────────────

        # Amenity selector
        a_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        a_card.pack(fill="x", pady=(0, 16))
        tk.Label(a_card, text="🏷️  Amenity Type",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(a_card, text="Select category then specific type",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))

        # Category checkbox dropdown (multi-select)
        tk.Label(a_card, text="Category:", font=FONT_SUB,
                 bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")
        categories = list(AMENITY_CATEGORIES.keys())
        self.amt_cat_menu = MultiSelectDropdown(
            a_card, values=categories, default_selected=[categories[0]],
            command=self._amt_on_category_change
        )
        self.amt_cat_menu.pack(fill="x", pady=(4, 10))

        # Amenity checkbox dropdown (multi-select)
        tk.Label(a_card, text="Amenity:", font=FONT_SUB,
                 bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")
        first_cat = categories[0]
        first_amenities = AMENITY_CATEGORIES.get(first_cat, [])
        self.amt_type_menu = MultiSelectDropdown(
            a_card, values=first_amenities,
            default_selected=first_amenities[:1],
            command=None
        )
        self.amt_type_menu.pack(fill="x", pady=(4, 0))

        # Area input
        b_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        b_card.pack(fill="x", pady=(0, 16))
        tk.Label(b_card, text="📍  Area",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(b_card, text="City, District, State or Pincode",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))

        area_row = tk.Frame(b_card, bg=BG_CARD)
        area_row.pack(fill="x")
        self.amt_area_entry = tk.Entry(area_row, font=FONT_NORMAL, bg=BG_INPUT,
                                       fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                                       relief="flat", bd=0, highlightthickness=1,
                                       highlightbackground=BG_INPUT,
                                       highlightcolor="#8e44ad")
        self.amt_area_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self.amt_area_entry.bind("<Return>", lambda e: self._amt_search())
        tk.Button(area_row, text="🎤", font=FONT_BTN, bg="#6c3483", fg="white",
                  relief="flat", cursor="hand2", padx=12, pady=6,
                  command=self._amt_voice).pack(side="left")

        # Area level
        tk.Label(b_card, text="Level:", font=FONT_SUB,
                 bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(10, 6))
        self.amt_level_var = tk.StringVar(value="City")
        level_row = tk.Frame(b_card, bg=BG_CARD)
        level_row.pack(fill="x")
        for level in ["City", "District", "State", "Pincode"]:
            tk.Radiobutton(level_row, text=f" {level}", variable=self.amt_level_var,
                           value=level, font=FONT_SUB, bg=BG_CARD,
                           fg=TEXT_WHITE, selectcolor=BG_INPUT,
                           activebackground=BG_CARD,
                           activeforeground=TEXT_WHITE).pack(side="left", padx=(0, 12))

        # Limit
        c_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        c_card.pack(fill="x", pady=(0, 16))
        tk.Label(c_card, text="🔢  Result Limit",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(c_card, text="Max number of results to fetch",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        self.amt_limit_var = tk.StringVar(value="50")
        limit_row = tk.Frame(c_card, bg=BG_CARD)
        limit_row.pack(fill="x")
        tk.Entry(limit_row, textvariable=self.amt_limit_var, font=FONT_NORMAL,
                 bg=BG_INPUT, fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                 relief="flat", bd=0, width=8).pack(side="left", ipady=7, padx=(0, 10))
        for val in ["20", "50", "100", "200"]:
            tk.Button(limit_row, text=val, font=FONT_SMALL, bg=BG_INPUT,
                      fg=TEXT_GRAY, relief="flat", cursor="hand2", padx=10, pady=5,
                      command=lambda v=val: self.amt_limit_var.set(v)).pack(side="left", padx=3)

        # Browser Visibility Mode (Headless Toggle)
        amt_mode_row = tk.Frame(c_card, bg=BG_CARD)
        amt_mode_row.pack(fill="x", pady=(12, 0))

        self.amt_headless_var = tk.BooleanVar(value=False)

        def _update_amt_headless_hint():
            if self.amt_headless_var.get():
                self.lbl_amt_headless_hint.config(
                    text="⚡ Headless ON: Browser runs silently in background",
                    fg="#f59e0b"
                )
            else:
                self.lbl_amt_headless_hint.config(
                    text="👁️ Headless OFF: Browser window will open visibly",
                    fg="#10b981"
                )

        self.cb_amt_headless = tk.Checkbutton(
            amt_mode_row,
            text="Headless Mode (Hide Browser Window)",
            variable=self.amt_headless_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=_update_amt_headless_hint
        )
        self.cb_amt_headless.pack(side="left", padx=(0, 10))

        self.lbl_amt_headless_hint = tk.Label(
            amt_mode_row,
            text="👁️ Headless OFF: Browser window will open visibly",
            font=FONT_SMALL,
            bg=BG_CARD,
            fg="#10b981"
        )
        self.lbl_amt_headless_hint.pack(side="left")

        # Business Status filter
        d_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        d_card.pack(fill="x")
        tk.Label(d_card, text="🕒  Business Status",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(d_card, text="Filter results by current opening status",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        self.amt_status_filter_var = tk.StringVar(value="All")
        self.amt_status_filter_menu = ttk.Combobox(
            d_card, textvariable=self.amt_status_filter_var,
            values=["All", "Open", "Closes Soon", "Opens Soon", "Closed", "Open 24 Hours"],
            state="readonly", font=FONT_NORMAL
        )
        self.amt_status_filter_menu.pack(fill="x", pady=(4, 0))
        self.amt_status_filter_menu.bind("<<ComboboxSelected>>", lambda e: self._amt_apply_status_filter())

        # ── RIGHT: Results Table ──────────────

        res_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        res_card.pack(fill="both", expand=True)

        res_header = tk.Frame(res_card, bg=BG_CARD)
        res_header.pack(fill="x")
        tk.Label(res_header, text="📋  Results",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left")
        self.amt_count_label = tk.Label(res_header, text="0 results",
                                        font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.amt_count_label.pack(side="right")

        # Progress bar
        self.amt_progress_var = tk.DoubleVar()
        self.amt_progress = ttk.Progressbar(res_card, variable=self.amt_progress_var,
                                            maximum=100, mode="indeterminate")
        self.amt_progress.pack(fill="x", pady=(10, 8))

        # Treeview table
        tree_frame = tk.Frame(res_card, bg=BG_CARD)
        tree_frame.pack(fill="both", expand=True)

        tree_scroll_y = tk.Scrollbar(tree_frame)
        tree_scroll_y.pack(side="right", fill="y")
        tree_scroll_x = tk.Scrollbar(tree_frame, orient="horizontal")
        tree_scroll_x.pack(side="bottom", fill="x")

        style = ttk.Style()
        style.configure("Amenity.Treeview",
                        background=BG_INPUT, foreground=TEXT_WHITE,
                        fieldbackground=BG_INPUT, rowheight=28,
                        font=FONT_SMALL)
        style.configure("Amenity.Treeview.Heading",
                        background=BG_CARD, foreground=TEXT_WHITE,
                        font=FONT_LABEL)
        style.map("Amenity.Treeview",
                  background=[("selected", ACCENT)])

        cols = ("#", "Name", "Latitude", "Longitude", "Address", "Phone", "Status")
        self.amt_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                     yscrollcommand=tree_scroll_y.set,
                                     xscrollcommand=tree_scroll_x.set,
                                     style="Amenity.Treeview")

        col_widths = {"#": 40, "Name": 200, "Latitude": 100,
                      "Longitude": 100, "Address": 250, "Phone": 120, "Status": 110}
        for col in cols:
            self.amt_tree.heading(col, text=col)
            self.amt_tree.column(col, width=col_widths.get(col, 100), anchor="w")

        self.amt_tree.pack(fill="both", expand=True)
        tree_scroll_y.config(command=self.amt_tree.yview)
        tree_scroll_x.config(command=self.amt_tree.xview)

        # Update scrollregion
        page_frame.update_idletasks()
        page_canvas.configure(scrollregion=page_canvas.bbox("all"))

    # ── Amenity Actions ───────────────────────

    def _amt_on_category_change(self, event=None):
        """Update amenity checklist from all selected categories."""
        categories = self.amt_cat_menu.get_selected()
        amenities = []
        for cat in categories:
            for amenity in AMENITY_CATEGORIES.get(cat, []):
                if amenity not in amenities:
                    amenities.append(amenity)

        current = set(self.amt_type_menu.get_selected())
        selected = [a for a in amenities if a in current]
        if not selected and amenities:
            selected = amenities[:1]
        self.amt_type_menu.set_values(amenities, selected)

    def _amt_voice(self):
        """Voice input for area field."""
        def on_result(text):
            if text:
                def _fill():
                    self.amt_area_entry.delete(0, tk.END)
                    self.amt_area_entry.insert(0, text)
                self.root.after(0, _fill)
        press_to_talk(on_result, duration=4)

    def _amt_search(self):
        area = self.amt_area_entry.get().strip()
        categories = self.amt_cat_menu.get_selected()
        amenities = self.amt_type_menu.get_selected()
        level = self.amt_level_var.get()

        if not area:
            messagebox.showwarning("Missing Area", "Please enter an area name.")
            return
        if not categories:
            messagebox.showwarning("Missing Category", "Please select at least one category.")
            return
        if not amenities:
            messagebox.showwarning("Missing Amenity", "Please select at least one amenity type.")
            return

        try:
            limit = int(self.amt_limit_var.get())
        except ValueError:
            limit = 50

        self.amenity_results = []
        self.amt_status_filter_var.set("All")
        for row in self.amt_tree.get_children():
            self.amt_tree.delete(row)
        self.amt_count_label.config(text="Searching...", fg=ACCENT)
        self.amt_search_btn.config(state="disabled", text="⏳ Searching...")
        self.amt_excel_btn.config(state="disabled")
        self.amt_kml_btn.config(state="disabled")
        self.amt_progress.start(10)
        self.amt_status.config(
            text=f"🔄  Searching {len(amenities)} amenity type(s) in {area}...", fg=ACCENT)

        headless = self.amt_headless_var.get()
        threading.Thread(
            target=self._amt_run,
            args=(amenities, area, level, limit, headless),
            daemon=True
        ).start()

    def _amt_run(self, amenities, area, level, limit, headless=False):
        def log(msg, tag="normal"):
            self._amt_log(msg, tag)

        all_results = []
        # Support "Area, District" disambiguation
        search_area = area
        search_dist = None
        if "," in area:
            parts = [p.strip() for p in area.split(",") if p.strip()]
            if len(parts) >= 2:
                search_area = parts[0]
                search_dist = parts[1]

        for amenity in amenities:
            try:
                results = fetch_amenities(
                    amenity, search_area, level, limit,
                    district=search_dist, log_callback=log,
                    headless=headless,
                ) or []
                for r in results:
                    r.setdefault("amenity", amenity)
                all_results.extend(results)
            except Exception as exc:
                log(f"⚠️ {amenity}: {exc}", "error")

        # De-duplicate results returned by overlapping searches; drop blank/junk records.
        unique = []
        seen = set()
        for r in all_results:
            name = str(r.get("name", "")).strip()
            lat = str(r.get("latitude", "")).strip()
            lng = str(r.get("longitude", "")).strip()
            if not name or not _is_valid_place_name(name):
                continue
            if not lat or not lng or lat.lower() in ("n/a", "none") or lng.lower() in ("n/a", "none"):
                continue
            key = (name.lower(), lat, lng)
            if key not in seen:
                seen.add(key)
                unique.append(r)

        results = unique[:limit] if limit > 0 else unique
        self.amenity_results = results

        if results:
            try:
                for r in results:
                    r["area"] = area
                count = save_amenity_results(results)
                print(f"   💾 Saved {count} amenities to database")
            except Exception as e:
                print(f"   ⚠️  DB save error: {e}")

        self.root.after(0, self._amt_show_results, results, amenities, area)

    def _amt_show_results(self, results, amenities, area):
        self.amt_progress.stop()
        self.amt_progress_var.set(100)
        self.amt_search_btn.config(state="normal", text="▶  Search Amenities")

        for row in self.amt_tree.get_children():
            self.amt_tree.delete(row)

        label = ", ".join(amenities[:3]) + (f" + {len(amenities)-3} more" if len(amenities) > 3 else "")
        if not results:
            self.amt_count_label.config(text="0 results", fg=RED)
            self.amt_status.config(
                text=f"⚠️  No selected amenities found in {area}", fg=YELLOW)
            return

        self.amenity_results = results
        self._amt_render_filtered_results()

        count = len(self._amt_get_filtered_results())
        self.amt_count_label.config(text=f"{count} results", fg=GREEN)
        self.amt_status.config(
            text=f"✅  Found {count} result(s) for {label} in {area}", fg=GREEN)
        self.amt_excel_btn.config(state="normal", fg=GREEN)
        self.amt_kml_btn.config(state="normal", fg=GREEN)

    def _amt_normalize_status(self, result):
        """Return a clean business-opening status from a scraper result.

        The backend may expose the value under different keys. Older runs can
        also incorrectly put values such as 'Closes soon' in category, so we
        recognize those values here without treating them as amenity types.
        """
        status_values = {
            "open": "Open",
            "closes soon": "Closes Soon",
            "opens soon": "Opens Soon",
            "closed": "Closed",
            "open 24 hours": "Open 24 Hours",
            "24 hours": "Open 24 Hours",
        }
        for key in ("business_status", "opening_status", "status"):
            value = str(result.get(key, "") or "").strip()
            normalized = status_values.get(value.lower())
            if normalized:
                return normalized

        # Backward compatibility with the bad category value seen in the log.
        category = str(result.get("category", "") or "").strip()
        normalized = status_values.get(category.lower())
        if normalized:
            return normalized

        # Some scrapers expose a boolean instead.
        if result.get("open_now") is True:
            return "Open"
        if result.get("open_now") is False:
            return "Closed"
        return "Unknown"

    def _amt_get_filtered_results(self):
        selected = self.amt_status_filter_var.get() if hasattr(self, "amt_status_filter_var") else "All"
        if selected == "All":
            return list(self.amenity_results)
        return [r for r in self.amenity_results
                if self._amt_normalize_status(r) == selected]

    def _amt_render_filtered_results(self):
        """Render the current amenity results using the selected status filter."""
        for row in self.amt_tree.get_children():
            self.amt_tree.delete(row)

        filtered = self._amt_get_filtered_results()
        for i, r in enumerate(filtered, 1):
            self.amt_tree.insert("", "end", values=(
                i,
                r.get("name", "Unknown"),
                r.get("latitude", ""),
                r.get("longitude", ""),
                r.get("address", "N/A"),
                r.get("phone", ""),
                self._amt_normalize_status(r),
            ))

        self.amt_count_label.config(
            text=f"{len(filtered)} results",
            fg=GREEN if filtered else RED
        )

    def _amt_apply_status_filter(self):
        """Apply status filtering locally without starting another Maps search."""
        if not self.amenity_results:
            return
        self._amt_render_filtered_results()
        selected = self.amt_status_filter_var.get()
        count = len(self._amt_get_filtered_results())
        self.amt_status.config(
            text=f"🔎  Showing {count} result(s) — Status: {selected}",
            fg=GREEN if count else YELLOW
        )

    def _amt_log(self, message: str, tag: str = "normal"):
        """Update amenity progress and status text."""
        try:
            clean_msg = message.strip()
            def _write():
                if hasattr(self, "amt_status"):
                    self.amt_status.config(text=f"🔄  {clean_msg}", fg=ACCENT)
            self.root.after(0, _write)
        except Exception:
            pass

    def _amt_export_excel(self):
        if not self.amenity_results:
            messagebox.showinfo("Export Excel", "No amenity results available to export. Please perform a search first.")
            return

        amenities = self.amt_type_menu.get_selected()
        if amenities:
            amenity_part = "_".join(amenities[:2])
            if len(amenities) > 2:
                amenity_part += f"_{len(amenities)}types"
        else:
            amenity_part = "amenities"

        area = self.amt_area_entry.get().strip() or "area"
        clean_area = "".join(c for c in area if c.isalnum() or c in "_ -").strip()[:25]
        safe_prefix = f"amenity_{amenity_part}_{clean_area}"[:60]

        try:
            results_to_save = self._amt_get_filtered_results() if hasattr(self, "_amt_get_filtered_results") else self.amenity_results
            if not results_to_save:
                results_to_save = self.amenity_results

            fp = get_output_filename(safe_prefix)
            save_amenities_to_excel(results_to_save, amenity_part, area, fp)
            self.amt_status.config(text=f"✅  Saved: {os.path.basename(fp)} ({len(results_to_save)} rows)", fg=GREEN)

            # Auto-open exported Excel, with explorer select fallback
            try:
                os.startfile(fp)
            except Exception:
                try:
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(fp)])
                except Exception:
                    pass
        except Exception as e:
            self.amt_status.config(text=f"❌  Export failed: {str(e)[:40]}", fg=RED)
            messagebox.showerror("Export Failed", f"Could not save amenities to Excel:\n\n{e}")

    def _amt_export_kml(self):
        if not self.amenity_results:
            messagebox.showinfo("Export KML", "No amenity results available to export. Please perform a search first.")
            return

        amenities = self.amt_type_menu.get_selected()
        if amenities:
            amenity_part = "_".join(amenities[:2])
            if len(amenities) > 2:
                amenity_part += f"_{len(amenities)}types"
        else:
            amenity_part = "amenities"

        area = self.amt_area_entry.get().strip() or "area"
        clean_area = "".join(c for c in area if c.isalnum() or c in "_ -").strip()[:25]

        from tkinter.filedialog import asksaveasfilename
        fp = asksaveasfilename(
            initialdir=get_downloads_dir(),
            defaultextension=".kml",
            filetypes=[("KML", "*.kml")],
            initialfile=f"{amenity_part}_{clean_area}.kml"
        )
        if fp:
            try:
                results_to_save = self._amt_get_filtered_results() if hasattr(self, "_amt_get_filtered_results") else self.amenity_results
                if not results_to_save:
                    results_to_save = self.amenity_results

                save_amenities_to_kml(results_to_save, amenity_part, area, fp)
                self.amt_status.config(text=f"✅  KML saved: {os.path.basename(fp)}", fg=GREEN)
            except Exception as e:
                self.amt_status.config(text=f"❌  KML export failed: {str(e)[:40]}", fg=RED)
                messagebox.showerror("KML Export Failed", f"Could not save KML:\n\n{e}")

    # ══════════════════════════════════════════
    #  TAB 4 — Area Explorer
    # ══════════════════════════════════════════

    def _area_export_excel(self):

        if not self.area_results:
            return

        file_path = filedialog.asksaveasfilename(
            title="Save Excel",
            initialdir=get_downloads_dir(),
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")]
        )

        if not file_path:
          return

        try:
            save_area_to_excel(
                self.area_results,
                file_path
            )
            self.area_status.config(
                text="✅ Excel exported"
            )
        except Exception as e:
            messagebox.showerror(
                "Export Failed",
                str(e)
            )

    def _open_boundary_validator(self):
        """Switch to the dedicated Google Maps Boundary Validator tab (Tab 6)."""
        try:
            self.notebook.select(5)
            self._val_on_tab_focused()
        except Exception:
            pass

    def _area_send_to_boundary_tracer(self, validated_only=False):
        """Transfer area results to Boundary Tracer (Tab 2) queue."""
        if not self.area_results:
            messagebox.showinfo("No Areas", "Please search or load areas first.")
            return

        items = self.area_results
        if validated_only:
            items = [r for r in items if r.get("has_boundary") is True]
            if not items:
                messagebox.showwarning(
                    "No Validated Boundaries",
                    "No areas have confirmed Google Maps boundaries yet. Run 'Validate Boundaries' first."
                )
                return

        added_count = 0
        existing = {q.strip().casefold() for q in self.bnd_queries}
        for item in items:
            name = item.get("name", "")
            parent = item.get("parent_area", "")
            query = item.get("query") or build_boundary_query(name, parent)
            if query and query.strip().casefold() not in existing:
                self.bnd_queries.append(query.strip())
                existing.add(query.strip().casefold())
                added_count += 1

        self._bnd_update_queue_display()
        try:
            self.notebook.select(1)
        except Exception:
            pass

        self._bnd_log(
            f"📥 Imported {added_count} area(s) from Area Explorer into Boundary Tracer queue.\n",
            "info"
        )
        messagebox.showinfo(
            "Transferred to Tracer",
            f"Transferred {added_count} area(s) to Boundary Tracer (Tab 2) queue!"
        )

    def _area_apply_filter(self):
        """Filter Area Explorer results treeview by polygon retrieval status."""
        filter_mode = getattr(self, "area_filter_var", None)
        mode = filter_mode.get() if filter_mode else "all"

        for item in self.area_tree.get_children():
            self.area_tree.delete(item)

        if not self.area_results:
            self.area_count_label.config(text="0 Areas")
            return

        disp_cnt = 0
        for i, area in enumerate(self.area_results, start=1):
            p_status = area.get("polygon_status") or ("FOUND" if area.get("has_boundary") is True else ("NOT_FOUND" if area.get("has_boundary") is False else "NEEDS_RETRY"))
            if mode == "found" and p_status != "FOUND":
                continue
            if mode == "retry" and p_status != "NEEDS_RETRY":
                continue
            if mode == "none" and p_status != "NOT_FOUND":
                continue

            disp_cnt += 1
            if p_status == "FOUND":
                status_text = "✅ FOUND"
                tag = "polygon_found"
            elif p_status == "NEEDS_RETRY":
                status_text = "⏳ NEEDS_RETRY"
                tag = "polygon_retry"
            else:
                status_text = "❌ NOT_FOUND"
                tag = "polygon_none"

            self.area_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    area.get("name", ""),
                    area.get("parent_area", ""),
                    area.get("type", "Area"),
                    status_text,
                    area.get("geometry_type", "Point"),
                    area.get("latitude", ""),
                    area.get("longitude", "")
                ),
                tags=(tag,)
            )

        self.area_count_label.config(
            text=f"{disp_cnt} of {len(self.area_results)} Areas"
        )

    def _build_area_tab(self, parent):
        self.area_results = []

        # =========================
        # Bottom Toolbar
        # =========================
        area_bar = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=30, pady=14)
        area_bar.pack(fill="x", side="bottom")

        self.area_status = tk.Label(
            area_bar,
            text="⬤  Ready",
            font=FONT_SUB,
            bg=BG_CARD,
            fg=TEXT_GRAY
        )
        self.area_status.pack(side="left")

        self.area_geojson_btn = tk.Button(
            area_bar,
            text="🌐 Export GeoJSON",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_GRAY,
            relief="flat",
            cursor="hand2",
            padx=14,
            pady=8,
            state="disabled",
            command=self._area_export_geojson
        )
        self.area_geojson_btn.pack(side="right", padx=(8, 0))

        self.area_kml_btn = tk.Button(
            area_bar,
            text="🗺️ Export KML",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_GRAY,
            relief="flat",
            cursor="hand2",
            padx=14,
            pady=8,
            state="disabled",
            command=self._area_export_kml
        )
        self.area_kml_btn.pack(side="right", padx=(8, 0))

        self.area_excel_btn = tk.Button(
            area_bar,
            text="📊 Export Excel",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_GRAY,
            relief="flat",
            cursor="hand2",
            padx=14,
            pady=8,
            state="disabled",
            command=self._area_export_excel
        )
        self.area_excel_btn.pack(side="right", padx=(8, 0))

        self.area_send_bnd_btn = tk.Button(
            area_bar,
            text="📥 Send to Tracer",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_GRAY,
            relief="flat",
            cursor="hand2",
            padx=14,
            pady=8,
            state="disabled",
            command=self._area_send_to_boundary_tracer
        )
        self.area_send_bnd_btn.pack(side="right", padx=(8, 0))

        self.area_search_btn = tk.Button(
            area_bar,
            text="▶  Search Areas",
            font=FONT_BTN,
            bg="#3498db",
            fg="white",
            relief="flat",
            cursor="hand2",
            padx=22,
            pady=8,
            command=self._area_search
        )
        self.area_search_btn.pack(side="right")

        # =========================
        # Main Body
        # =========================
        body = tk.Frame(parent, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=30, pady=20)

        left, right = self._split(body, 360)

        # =========================
        # Variables
        # =========================
        self.area_search_by_var = tk.StringVar(value="City")
        self.area_city_var = tk.StringVar()
        self.area_state_var = tk.StringVar()
        self.area_loc_district_var = tk.StringVar()
        self.area_loc_state_var = tk.StringVar()
        self.area_strict_var = tk.BooleanVar(value=True)
        self.area_limit_var = tk.StringVar(value="5000")
        self.area_max_var = tk.BooleanVar(value=True)

        self.area_types = AREA_TYPES

        # =========================
        # LEFT: Scrollable Container & Cards
        # =========================
        left_canvas = tk.Canvas(left, bg=BG_DARK, highlightthickness=0, bd=0)
        self.area_left_canvas = left_canvas
        left_scroll = tk.Scrollbar(left, orient="vertical", command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_scroll.set)

        left_content = tk.Frame(left_canvas, bg=BG_DARK)
        left_window = left_canvas.create_window((0, 0), window=left_content, anchor="nw")

        def _on_left_configure(event=None):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))

        def _on_left_canvas_configure(event):
            left_canvas.itemconfigure(left_window, width=event.width)

        left_content.bind("<Configure>", _on_left_configure)
        left_canvas.bind("<Configure>", _on_left_canvas_configure)

        def _on_left_mousewheel(event):
            if event.delta:
                left_canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        left_canvas.bind("<Enter>", lambda e: left_canvas.bind_all("<MouseWheel>", _on_left_mousewheel))
        left_canvas.bind("<Leave>", lambda e: left_canvas.unbind_all("<MouseWheel>"))

        left_scroll.pack(side="right", fill="y")
        left_canvas.pack(side="left", fill="both", expand=True)

        # ── Card 1: Search By Selector ────────────────────────
        search_by_card = tk.Frame(left_content, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=14)
        search_by_card.pack(fill="x", pady=(0, 12))

        tk.Label(
            search_by_card,
            text="🔎  Search By",
            font=FONT_LABEL,
            bg=BG_CARD,
            fg=TEXT_WHITE
        ).pack(anchor="w")

        tk.Label(
            search_by_card,
            text="Choose geographic parameter type",
            font=FONT_SUB,
            bg=BG_CARD,
            fg=TEXT_GRAY
        ).pack(anchor="w", pady=(2, 10))

        radio_row = tk.Frame(search_by_card, bg=BG_CARD)
        radio_row.pack(fill="x")
        for label in ["District", "City", "State", "Locality", "Pincode"]:
            tk.Radiobutton(
                radio_row,
                text=f" {label}",
                variable=self.area_search_by_var,
                value=label,
                font=FONT_NORMAL,
                bg=BG_CARD,
                fg=TEXT_WHITE,
                selectcolor=BG_INPUT,
                activebackground=BG_CARD,
                activeforeground=TEXT_WHITE,
                command=self._on_area_search_by_change
            ).pack(side="left", padx=(0, 8))

        # ── Card 2: Location & Confinement Entry ──────────────
        loc_card = tk.Frame(left_content, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=14)
        loc_card.pack(fill="x", pady=(0, 12))

        self.lbl_area_loc_title = tk.Label(
            loc_card,
            text="🏙️  City Name",
            font=FONT_LABEL,
            bg=BG_CARD,
            fg=TEXT_WHITE
        )
        self.lbl_area_loc_title.pack(anchor="w")

        self.lbl_area_loc_hint = tk.Label(
            loc_card,
            text="Enter any city name (e.g. Delhi, Mumbai, Bengaluru)",
            font=FONT_SUB,
            bg=BG_CARD,
            fg=TEXT_GRAY
        )
        self.lbl_area_loc_hint.pack(anchor="w", pady=(2, 8))

        self.area_city_entry = tk.Entry(
            loc_card,
            textvariable=self.area_city_var,
            font=FONT_NORMAL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            insertbackground=TEXT_WHITE,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BG_INPUT,
            highlightcolor=ACCENT
        )
        self.area_city_entry.pack(fill="x", ipady=8)
        self.area_city_entry.bind("<Return>", lambda e: self._area_search())

        # Dynamic State Code frame (District, City)
        self.area_state_frame = tk.Frame(loc_card, bg=BG_CARD)
        self.area_state_frame.pack(fill="x", pady=(8, 0))

        state_row = tk.Frame(self.area_state_frame, bg=BG_CARD)
        state_row.pack(fill="x")
        tk.Label(state_row, text="State Code:", font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(0, 8))
        self.area_state_entry = tk.Entry(
            state_row,
            textvariable=self.area_state_var,
            font=FONT_NORMAL, bg=BG_INPUT, fg=TEXT_WHITE,
            insertbackground=TEXT_WHITE, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BG_INPUT,
            highlightcolor=ACCENT, width=6
        )
        self.area_state_entry.pack(side="left", ipady=6)
        tk.Label(state_row, text="e.g. DL  UP  MH  RJ  GJ", font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(10, 0))

        # Dynamic Locality frame (Locality mode)
        self.area_loc_frame = tk.Frame(loc_card, bg=BG_CARD)
        # Hidden initially, packed in Locality mode

        loc_row1 = tk.Frame(self.area_loc_frame, bg=BG_CARD)
        loc_row1.pack(fill="x", pady=(0, 6))
        tk.Label(loc_row1, text="District:", font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY, width=10, anchor="w").pack(side="left")
        self.area_loc_dist_entry = tk.Entry(
            loc_row1, textvariable=self.area_loc_district_var,
            font=FONT_NORMAL, bg=BG_INPUT, fg=TEXT_WHITE,
            insertbackground=TEXT_WHITE, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BG_INPUT,
            highlightcolor=ACCENT, width=18
        )
        self.area_loc_dist_entry.pack(side="left", ipady=6)
        tk.Label(loc_row1, text="e.g. South West Delhi", font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(8, 0))

        loc_row2 = tk.Frame(self.area_loc_frame, bg=BG_CARD)
        loc_row2.pack(fill="x")
        tk.Label(loc_row2, text="State Code:", font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY, width=10, anchor="w").pack(side="left")
        self.area_loc_state_entry = tk.Entry(
            loc_row2, textvariable=self.area_loc_state_var,
            font=FONT_NORMAL, bg=BG_INPUT, fg=TEXT_WHITE,
            insertbackground=TEXT_WHITE, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BG_INPUT,
            highlightcolor=ACCENT, width=6
        )
        self.area_loc_state_entry.pack(side="left", ipady=6)
        tk.Label(loc_row2, text="e.g. DL", font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(8, 0))

        # Strict Confinement Checkbox
        confinement_frame = tk.Frame(loc_card, bg=BG_CARD)
        confinement_frame.pack(fill="x", pady=(10, 0))

        self.cb_area_strict = tk.Checkbutton(
            confinement_frame,
            text="🔒 Strict Boundary Confinement",
            variable=self.area_strict_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg="#38bdf8",
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground="#38bdf8",
        )
        self.cb_area_strict.pack(anchor="w")

        tk.Label(
            confinement_frame,
            text="Extract ONLY places strictly inside this boundary (filters out outside places)",
            font=FONT_SMALL,
            bg=BG_CARD,
            fg=TEXT_GRAY,
            wraplength=310,
            justify="left"
        ).pack(anchor="w", padx=(24, 0), pady=(1, 4))

        # Quick Presets / Chips
        chips_frame = tk.Frame(loc_card, bg=BG_CARD)
        chips_frame.pack(fill="x", pady=(8, 0))

        quick_presets = [
            ("🇮🇳 Whole India", "India", "City", ""),
            ("Delhi (DL)", "Delhi", "City", "DL"),
            ("South Delhi (DL)", "South Delhi", "District", "DL"),
            ("Mumbai (MH)", "Mumbai", "City", "MH"),
            ("Bengaluru (KA)", "Bengaluru", "City", "KA"),
            ("Jaipur (RJ)", "Jaipur", "City", "RJ"),
            ("Lucknow (UP)", "Lucknow", "City", "UP"),
            ("Pune (MH)", "Pune", "City", "MH"),
        ]
        chip_row1 = tk.Frame(chips_frame, bg=BG_CARD)
        chip_row1.pack(fill="x")
        chip_row2 = tk.Frame(chips_frame, bg=BG_CARD)
        chip_row2.pack(fill="x", pady=(4, 0))

        for idx, (label, loc_name, stype, sc) in enumerate(quick_presets):
            parent_row = chip_row1 if idx < 4 else chip_row2
            btn = tk.Button(
                parent_row,
                text=label,
                font=("Segoe UI", 8),
                bg=BG_INPUT,
                fg="#38bdf8" if "Delhi" in label or "India" in label else TEXT_GRAY,
                activebackground=ACCENT,
                activeforeground=TEXT_WHITE,
                relief="flat",
                bd=0,
                padx=6,
                pady=2,
                cursor="hand2",
                command=lambda ln=loc_name, st=stype, s_code=sc: self._set_area_preset(ln, st, s_code)
            )
            btn.pack(side="left", padx=(0, 4), pady=(2, 2))

        # Area Type Card (Multi-Select)
        type_card = tk.Frame(left_content, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=14)
        type_card.pack(fill="x", pady=(0, 12))

        tk.Label(
            type_card,
            text="📂  Area Types",
            font=FONT_LABEL,
            bg=BG_CARD,
            fg=TEXT_WHITE
        ).pack(anchor="w")

        tk.Label(
            type_card,
            text="Select one or multiple area categories",
            font=FONT_SUB,
            bg=BG_CARD,
            fg=TEXT_GRAY
        ).pack(anchor="w", pady=(2, 10))

        self.area_type_var = tk.StringVar(value=", ".join(AREA_TYPES))
        self.area_type_menu = MultiSelectDropdown(
            type_card,
            values=AREA_TYPES,
            default_selected=AREA_TYPES,
            bg=BG_CARD,
            button_bg=BG_INPUT,
            command=lambda: self.area_type_var.set(", ".join(self.area_type_menu.get_selected()))
        )
        self.area_type_menu.pack(fill="x", pady=(0, 8))
        self.area_type_combo = self.area_type_menu  # backward compatibility

        # Quick action buttons row
        type_btn_row = tk.Frame(type_card, bg=BG_CARD)
        type_btn_row.pack(fill="x")

        def _select_full_data():
            self.area_type_menu.select_all()
            _set_limit("max")

        tk.Button(
            type_btn_row,
            text="🌟 Full City Data",
            font=FONT_SMALL,
            bg="#2563eb",
            fg="white",
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=3,
            command=_select_full_data
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            type_btn_row,
            text="☑ Select All",
            font=FONT_SMALL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=3,
            command=self.area_type_menu.select_all
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            type_btn_row,
            text="☐ Clear",
            font=FONT_SMALL,
            bg=BG_INPUT,
            fg=TEXT_GRAY,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=3,
            command=self.area_type_menu.clear
        ).pack(side="left")

        # Limit Card
        limit_card = tk.Frame(left_content, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=14)
        limit_card.pack(fill="x", pady=(0, 12))

        tk.Label(
            limit_card,
            text="🔢  Result Limit",
            font=FONT_LABEL,
            bg=BG_CARD,
            fg=TEXT_WHITE
        ).pack(anchor="w")

        tk.Label(
            limit_card,
            text="Maximum number of areas to fetch",
            font=FONT_SUB,
            bg=BG_CARD,
            fg=TEXT_GRAY
        ).pack(anchor="w", pady=(2, 8))

        def _on_max_toggle():
            if self.area_max_var.get():
                self.area_limit_entry.config(state="disabled", fg=TEXT_GRAY)
                self.area_limit_hint.config(text="⚡ Maximum: Fetching ALL areas in city")
            else:
                self.area_limit_entry.config(state="normal", fg=TEXT_WHITE)
                self.area_limit_hint.config(text="")

        def _set_limit(val):
            if val == "max":
                self.area_max_var.set(True)
            else:
                self.area_max_var.set(False)
                self.area_limit_var.set(str(val))
            _on_max_toggle()

        # Checkbox for Maximum / All
        max_cb = tk.Checkbutton(
            limit_card,
            text="Fetch Maximum (All areas in city)",
            variable=self.area_max_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            anchor="w",
            command=_on_max_toggle
        )
        max_cb.pack(fill="x", pady=(0, 6))

        self.area_limit_entry = tk.Entry(
            limit_card,
            textvariable=self.area_limit_var,
            font=FONT_NORMAL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            insertbackground=TEXT_WHITE,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BG_INPUT,
            highlightcolor=ACCENT
        )
        self.area_limit_entry.pack(fill="x", ipady=8)

        # Quick preset buttons: 250, 500, 1000, 5000, Max
        preset_row = tk.Frame(limit_card, bg=BG_CARD)
        preset_row.pack(fill="x", pady=(8, 0))

        for p_label, p_val in [("250", 250), ("500", 500), ("1000", 1000), ("5000", 5000), ("🚀 All / Max", "max")]:
            tk.Button(
                preset_row,
                text=p_label,
                font=FONT_SMALL,
                bg=BG_INPUT,
                fg=TEXT_WHITE if p_val != "max" else "#3498db",
                relief="flat",
                cursor="hand2",
                padx=6,
                pady=2,
                command=lambda v=p_val: _set_limit(v)
            ).pack(side="left", padx=(0, 4))

        self.area_limit_hint = tk.Label(
            limit_card,
            text="",
            font=FONT_SMALL,
            bg=BG_CARD,
            fg="#2ecc71"
        )
        self.area_limit_hint.pack(anchor="w", pady=(4, 0))
        _on_max_toggle()

        # Data Source Engine Card
        engine_card = tk.Frame(left_content, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=14)
        engine_card.pack(fill="x", pady=(0, 12))

        tk.Label(
            engine_card,
            text="🌐  Data Source Engine",
            font=FONT_LABEL,
            bg=BG_CARD,
            fg=TEXT_WHITE
        ).pack(anchor="w")

        tk.Label(
            engine_card,
            text="Select search engine for area discovery",
            font=FONT_SUB,
            bg=BG_CARD,
            fg=TEXT_GRAY
        ).pack(anchor="w", pady=(2, 8))

        self.area_source_var = tk.StringVar(value="hybrid")
        src_row = tk.Frame(engine_card, bg=BG_CARD)
        src_row.pack(fill="x", pady=(0, 8))

        tk.Radiobutton(
            src_row,
            text="🌟 Hybrid (Max Data)",
            variable=self.area_source_var,
            value="hybrid",
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg="#10b981",
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground="#10b981",
        ).pack(side="left", padx=(0, 6))

        tk.Radiobutton(
            src_row,
            text="🌐 Google Maps",
            variable=self.area_source_var,
            value="google_maps",
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg="#38bdf8",
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground="#38bdf8",
        ).pack(side="left", padx=(0, 6))

        tk.Radiobutton(
            src_row,
            text="🗺️ OSM",
            variable=self.area_source_var,
            value="osm",
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
        ).pack(side="left")

        # Headless mode toggle for Area Explorer
        area_mode_row = tk.Frame(engine_card, bg=BG_CARD)
        area_mode_row.pack(fill="x", pady=(6, 0))

        self.area_headless_var = tk.BooleanVar(value=True)

        def _update_area_headless_hint():
            if self.area_headless_var.get():
                self.lbl_area_headless_hint.config(
                    text="⚡ Headless ON: Silent background search",
                    fg="#f59e0b"
                )
            else:
                self.lbl_area_headless_hint.config(
                    text="👁️ Headless OFF: Visible browser window",
                    fg="#10b981"
                )

        self.cb_area_headless = tk.Checkbutton(
            area_mode_row,
            text="Headless Mode",
            variable=self.area_headless_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=_update_area_headless_hint
        )
        self.cb_area_headless.pack(side="left", padx=(0, 10))

        self.lbl_area_headless_hint = tk.Label(
            area_mode_row,
            text="⚡ Headless ON: Silent background search",
            font=FONT_SMALL,
            bg=BG_CARD,
            fg="#f59e0b"
        )
        self.lbl_area_headless_hint.pack(side="left")

        # =========================
        # RIGHT: Results Card
        # =========================
        res_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        res_card.pack(fill="both", expand=True)

        header = tk.Frame(res_card, bg=BG_CARD)
        header.pack(fill="x", pady=(0, 10))

        tk.Label(
            header,
            text="📋  Discovered Areas",
            bg=BG_CARD,
            fg=TEXT_WHITE,
            font=FONT_LABEL
        ).pack(side="left")

        self.area_count_label = tk.Label(
            header,
            text="0 Areas",
            bg=BG_CARD,
            fg=TEXT_GRAY,
            font=FONT_SUB
        )
        self.area_count_label.pack(side="right")

        # Filter row for validation status
        self.area_filter_var = tk.StringVar(value="all")
        filter_row = tk.Frame(res_card, bg=BG_CARD)
        filter_row.pack(fill="x", pady=(0, 8))

        tk.Label(
            filter_row,
            text="Filter View:",
            font=FONT_SMALL,
            bg=BG_CARD,
            fg=TEXT_GRAY
        ).pack(side="left", padx=(0, 8))

        for f_label, f_val in [
            ("All", "all"),
            ("🟩 FOUND", "found"),
            ("🟧 NEEDS_RETRY", "retry"),
            ("⬜ NOT_FOUND", "none"),
        ]:
            tk.Radiobutton(
                filter_row,
                text=f_label,
                value=f_val,
                variable=self.area_filter_var,
                font=FONT_SMALL,
                bg=BG_CARD,
                fg=TEXT_WHITE,
                selectcolor=BG_INPUT,
                activebackground=BG_CARD,
                activeforeground=TEXT_WHITE,
                command=self._area_apply_filter
            ).pack(side="left", padx=(0, 10))

        self.area_progress = ttk.Progressbar(
            res_card,
            mode="indeterminate"
        )
        self.area_progress.pack(fill="x", pady=(0, 12))

        tree_frame = tk.Frame(res_card, bg=BG_CARD)
        tree_frame.pack(fill="both", expand=True)

        tree_scroll_y = ttk.Scrollbar(tree_frame)
        tree_scroll_y.pack(side="right", fill="y")

        tree_scroll_x = ttk.Scrollbar(
            tree_frame,
            orient="horizontal"
        )
        tree_scroll_x.pack(side="bottom", fill="x")

        self.area_tree = ttk.Treeview(
            tree_frame,
            columns=("name", "parent_area", "type", "polygon_status", "geom_type", "latitude", "longitude"),
            show="headings",
            selectmode="extended",
            yscrollcommand=tree_scroll_y.set,
            xscrollcommand=tree_scroll_x.set,
            style="Amenity.Treeview"
        )

        tree_scroll_y.config(command=self.area_tree.yview)
        tree_scroll_x.config(command=self.area_tree.xview)

        self.area_tree.heading("name", text="Area Name")
        self.area_tree.heading("parent_area", text="Parent Area / Sub-Locality")
        self.area_tree.heading("type", text="Type")
        self.area_tree.heading("polygon_status", text="Polygon Status")
        self.area_tree.heading("geom_type", text="Geometry")
        self.area_tree.heading("latitude", text="Latitude")
        self.area_tree.heading("longitude", text="Longitude")

        self.area_tree.column("name", width=200, anchor="w")
        self.area_tree.column("parent_area", width=160, anchor="w")
        self.area_tree.column("type", width=90, anchor="center")
        self.area_tree.column("polygon_status", width=130, anchor="center")
        self.area_tree.column("geom_type", width=95, anchor="center")
        self.area_tree.column("latitude", width=85, anchor="center")
        self.area_tree.column("longitude", width=85, anchor="center")

        self.area_tree.tag_configure("polygon_found", foreground="#10b981")
        self.area_tree.tag_configure("polygon_retry", foreground="#f59e0b")
        self.area_tree.tag_configure("polygon_none", foreground="#94a3b8")

        def _copy_selected_areas(event=None):
            selected = self.area_tree.selection()
            if not selected:
                return
            lines = []
            for item_id in selected:
                vals = self.area_tree.item(item_id, "values")
                if vals:
                    lines.append("\t".join(str(v) for v in vals))
            if lines:
                self.root.clipboard_clear()
                self.root.clipboard_append("\n".join(lines))
                self.area_status.config(text=f"📋 Copied {len(lines)} area(s) to clipboard")

        self.area_tree.bind("<Control-c>", _copy_selected_areas)

        self.area_tree.pack(fill="both", expand=True)

    def _on_area_search_by_change(self):
        st = self.area_search_by_var.get()
        self.area_state_frame.pack_forget()
        self.area_loc_frame.pack_forget()

        if st == "District":
            self.lbl_area_loc_title.config(text="🏛️  District Name")
            self.lbl_area_loc_hint.config(text="Enter district name (e.g. South Delhi, Agra) + state code below")
            self.area_state_frame.pack(fill="x", pady=(8, 0))
        elif st == "Locality":
            self.lbl_area_loc_title.config(text="📍  Locality / Suburb")
            self.lbl_area_loc_hint.config(text="Enter locality name (e.g. Dwarka, Connaught Place) + district & state below")
            self.area_loc_frame.pack(fill="x", pady=(8, 0))
        elif st == "State":
            self.lbl_area_loc_title.config(text="🗺️  State Name")
            self.lbl_area_loc_hint.config(text="Enter state name (e.g. Delhi, Maharashtra, Rajasthan)")
        elif st == "Pincode":
            self.lbl_area_loc_title.config(text="📮  Pincode")
            self.lbl_area_loc_hint.config(text="Enter 6-digit Indian pincode (e.g. 110001, 400001)")
        else:  # City
            self.lbl_area_loc_title.config(text="🏙️  City Name")
            self.lbl_area_loc_hint.config(text="Enter city name (e.g. Delhi, Mumbai, Bengaluru)")
            self.area_state_frame.pack(fill="x", pady=(8, 0))

    def _set_area_preset(self, loc_name, stype, sc):
        self.area_search_by_var.set(stype)
        self._on_area_search_by_change()
        self.area_city_var.set(loc_name)
        if stype == "Locality":
            self.area_loc_state_var.set(sc)
        else:
            self.area_state_var.set(sc)

    def _area_search(self):
        """Start Area Explorer search."""

        search_by = self.area_search_by_var.get()
        raw_location = self.area_city_var.get().strip()
        strict_boundary = self.area_strict_var.get()

        if search_by == "Locality":
            state_code = self.area_loc_state_var.get().strip().upper()
            district = self.area_loc_district_var.get().strip()
            if district and state_code and len(state_code) == 2:
                full_location = f"{raw_location}, {district}, {state_code}"
            elif district:
                full_location = f"{raw_location}, {district}"
            elif state_code:
                full_location = f"{raw_location}, {state_code}"
            else:
                full_location = raw_location
        elif search_by == "District":
            state_code = self.area_state_var.get().strip().upper()
            if state_code and len(state_code) == 2:
                full_location = f"{raw_location}, {state_code}"
            else:
                full_location = raw_location
        elif search_by == "State":
            state_code = ""
            full_location = f"{raw_location} State" if "state" not in raw_location.lower() else raw_location
        elif search_by == "Pincode":
            state_code = self.area_state_var.get().strip().upper()
            full_location = raw_location
        else:  # City
            state_code = self.area_state_var.get().strip().upper()
            if state_code and len(state_code) == 2:
                full_location = f"{raw_location}, {state_code}"
            else:
                full_location = raw_location

        selected_types = self.area_type_menu.get_selected()

        if self.area_max_var.get():
            limit = None
            limit_desc = "all available"
        else:
            try:
                limit = int(self.area_limit_var.get())
                if limit <= 0:
                    limit = None
                    limit_desc = "all available"
                else:
                    limit_desc = f"up to {limit}"
            except ValueError:
                messagebox.showerror(
                    "Invalid Limit",
                    "Please enter a valid number or check 'Fetch Maximum'."
                )
                return

        if not raw_location:
            messagebox.showwarning(
                f"{search_by} Required",
                f"Please enter a {search_by.lower()} name."
            )
            return

        if not selected_types:
            messagebox.showwarning(
                "Area Type Required",
                "Please select at least one area type to search."
            )
            return

        # Clear previous results
        self.area_tree.delete(*self.area_tree.get_children())
        self.area_results = []

        if len(selected_types) == len(AREA_TYPES):
            type_desc = "All Categories (Full City Data)"
        elif len(selected_types) <= 3:
            type_desc = ", ".join(selected_types)
        else:
            type_desc = f"{len(selected_types)} categories"

        # Update UI
        self.area_count_label.config(text="Searching...")
        confinement_str = " [Strict Boundary ON]" if strict_boundary else ""
        self.area_status.config(
            text=f"🔍 Searching {limit_desc} {type_desc} in {full_location}{confinement_str}..."
        )

        self.area_search_btn.config(state="disabled")
        self.area_excel_btn.config(state="disabled")
        self.area_kml_btn.config(state="disabled")
        self.area_geojson_btn.config(state="disabled")

        self.area_progress.start(10)

        source = self.area_source_var.get()
        headless = self.area_headless_var.get()

        # Start background thread
        threading.Thread(
            target=self._area_run,
            args=(full_location, selected_types, limit, source, headless, state_code, strict_boundary, search_by),
            daemon=True
        ).start()

    def _area_run(self, city, area_types, limit, source="hybrid", headless=True, state_code="", strict_boundary=True, location_type="City"):
        """Background thread for Area Explorer."""

        try:
            results = fetch_areas(
                city=city,
                area_type=area_types,
                limit=limit,
                log_callback=self._area_log,
                source=source,
                headless=headless,
                state_code=state_code,
                strict_boundary=strict_boundary,
                location_type=location_type,
            )

            self.area_results = results

            self.root.after(
                0,
                lambda: self._area_show_results(
                    results,
                    city,
                    area_types
                )
            )

        except Exception as e:
            self.root.after(
                0,
                lambda e=e: messagebox.showerror(
                    "Area Explorer",
                    str(e)
                )
            )

        finally:
            self.root.after(0, self.area_progress.stop)
            self.root.after(
                0,
                lambda: self.area_search_btn.config(
                    state="normal"
                )
            )

            if self.area_results:
                self.root.after(
                    0,
                    lambda: self.area_excel_btn.config(
                        state="normal"
                    )
                )
                self.root.after(
                    0,
                    lambda: self.area_kml_btn.config(
                        state="normal"
                    )
                )
                self.root.after(
                    0,
                    lambda: self.area_geojson_btn.config(
                        state="normal"
                    )
                )
                self.root.after(
                    0,
                    lambda: self.area_status.config(
                        text=f"✅ Found {len(self.area_results)} areas"
                    )
                )
            else:
                self.root.after(
                    0,
                    lambda: self.area_status.config(
                        text="⚠ No areas found"
                    )
                )

    def _area_show_results(self, results, city, area_types):
        """Display Area Explorer results in the Treeview."""

        # Clear previous rows
        for item in self.area_tree.get_children():
            self.area_tree.delete(item)

        self.area_results = results

        if not results:
            self.area_count_label.config(text="0 Areas")
            self.area_status.config(text="⚠ No areas found")
            return

        # Insert rows
        for i, area in enumerate(results, start=1):
            p_status = area.get("polygon_status")
            if p_status == "FOUND":
                status_text = "✅ FOUND"
                tag = "polygon_found"
            elif p_status == "NEEDS_RETRY":
                status_text = "⏳ NEEDS_RETRY"
                tag = "polygon_retry"
            else:
                status_text = "❌ NOT_FOUND"
                tag = "polygon_none"

            self.area_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    area.get("name", ""),
                    area.get("parent_area", city),
                    area.get("type", "Area"),
                    status_text,
                    area.get("geometry_type", "Point"),
                    area.get("latitude", ""),
                    area.get("longitude", "")
                ),
                tags=(tag,)
            )

        # Count polygon statuses
        found_cnt = sum(1 for r in results if r.get("polygon_status") == "FOUND")
        retry_cnt = sum(1 for r in results if r.get("polygon_status") == "NEEDS_RETRY")
        none_cnt = sum(1 for r in results if r.get("polygon_status") == "NOT_FOUND")

        # Update UI
        type_str = ", ".join(area_types) if isinstance(area_types, (list, tuple, set)) and len(area_types) <= 2 else (f"{len(area_types)} categories" if isinstance(area_types, (list, tuple, set)) else str(area_types))
        self.area_count_label.config(
            text=f"{len(results)} Areas ({found_cnt} Polygons, {none_cnt} Points)"
        )

        self.area_status.config(
            text=f"✅ {len(results)} areas ({type_str}) in {city} | Polygons: {found_cnt} FOUND, {retry_cnt} NEEDS_RETRY, {none_cnt} NOT_FOUND"
        )

        # Enable export & action buttons
        self.area_excel_btn.config(state="normal")
        self.area_kml_btn.config(state="normal")
        self.area_geojson_btn.config(state="normal")
        self.area_send_bnd_btn.config(state="normal")

        # Stop progress bar
        self.area_progress.stop()

        # Re-enable search button
        self.area_search_btn.config(state="normal")
    
    def _area_export_kml(self):

        if not self.area_results:
            return

        file_path = filedialog.asksaveasfilename(
            title="Save KML",
            initialdir=get_downloads_dir(),
            defaultextension=".kml",
            filetypes=[
                ("KML File", "*.kml"),
                ("All Files", "*.*")
            ]
        )

        if not file_path:
            return

        try:
            save_area_to_kml(
                self.area_results,
                file_path
            )
            self.area_status.config(
                text="✅ KML exported"
            )
        except Exception as e:
            messagebox.showerror(
                "Export Failed",
                str(e)
            )

    def _area_export_geojson(self):
        """Export Area Explorer results to GeoJSON format."""
        if not self.area_results:
            return

        file_path = filedialog.asksaveasfilename(
            title="Save GeoJSON",
            initialdir=get_downloads_dir(),
            defaultextension=".geojson",
            filetypes=[
                ("GeoJSON File", "*.geojson"),
                ("JSON File", "*.json"),
                ("All Files", "*.*")
            ]
        )

        if not file_path:
            return

        try:
            save_area_to_geojson(
                self.area_results,
                file_path
            )
            self.area_status.config(
                text="✅ GeoJSON exported"
            )
        except Exception as e:
            messagebox.showerror(
                "Export Failed",
                str(e)
            )


    def _area_log(self, message, tag="normal"):
        """Log messages from Area Explorer."""
        try:
            clean = str(message).strip()
            def _write():
                if hasattr(self, "area_status") and self.area_status:
                    self.area_status.config(text=f"🔄  {clean}")
            self.root.after(0, _write)
        except Exception:
            pass

        

    # ═════════════════════════════════════════
    #  TAB 5 — BULK RUNNER
    # ═════════════════════════════════════════

    def _build_bulk_tab(self, parent):
        self.bulk_runner         = None
        self.bulk_jobs           = []
        self.bulk_job_bars       = {}
        self.current_bulk_run_id = None

        # ── Bottom bar FIRST ──────────────────
        bulk_bar = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=30, pady=14)
        bulk_bar.pack(fill="x", side="bottom")

        self.bulk_status_label = tk.Label(bulk_bar, text="⬤  Ready",
                                          font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.bulk_status_label.pack(side="left")

        self.bulk_open_btn = tk.Button(bulk_bar, text="📁 Open Output",
                                       font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                       relief="flat", cursor="hand2", padx=16, pady=8,
                                       state="normal",
                                       command=self._bulk_open_output)
        self.bulk_open_btn.pack(side="right", padx=(8, 0))

        self.bulk_stop_btn = tk.Button(bulk_bar, text="⏸  Stop",
                                       font=FONT_BTN, bg=RED, fg="white",
                                       relief="flat", cursor="hand2", padx=16, pady=8,
                                       state="disabled", command=self._bulk_stop)
        self.bulk_stop_btn.pack(side="right", padx=(8, 0))

        self.bulk_resume_btn = tk.Button(bulk_bar, text="▶  Resume",
                                         font=FONT_BTN, bg=YELLOW, fg=BG_DARK,
                                         relief="flat", cursor="hand2", padx=16, pady=8,
                                         state="disabled", command=self._bulk_resume)
        self.bulk_resume_btn.pack(side="right", padx=(8, 0))

        self.bulk_start_btn = tk.Button(bulk_bar, text="⚡  Start Bulk Run",
                                        font=FONT_BTN, bg="#8e44ad", fg="white",
                                        relief="flat", cursor="hand2", padx=22, pady=8,
                                        state="disabled", command=self._bulk_start)
        self.bulk_start_btn.pack(side="right")

        # ── Body ──────────────────────────────
        body = tk.Frame(parent, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=30, pady=20)

        left, right = self._split(body, 360)

        # ── LEFT ──────────────────────────────

        # Jobs file import
        jobs_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        jobs_card.pack(fill="x", pady=(0, 16))
        tk.Label(jobs_card, text="📋  Import Jobs File",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(jobs_card, text="CSV with amenity, area, level, limit columns",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        self.bulk_file_label = tk.Label(jobs_card, text="No file selected",
                                        font=FONT_MONO, bg=BG_INPUT, fg=TEXT_GRAY,
                                        padx=12, pady=10, anchor="w")
        self.bulk_file_label.pack(fill="x", pady=(0, 10))
        btn_row = tk.Frame(jobs_card, bg=BG_CARD)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="📂 Browse", font=FONT_BTN, bg=ACCENT, fg="white",
                  relief="flat", cursor="hand2", padx=14, pady=7,
                  command=self._bulk_browse).pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="📄 Sample CSV", font=FONT_BTN, bg=BG_INPUT,
                  fg=TEXT_GRAY, relief="flat", cursor="hand2", padx=14, pady=7,
                  command=self._bulk_create_sample).pack(side="left")

        # Settings
        settings_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        settings_card.pack(fill="x", pady=(0, 16))
        tk.Label(settings_card, text="⚙️  Settings",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")

        # Parallel browsers
        tk.Label(settings_card, text=f"Parallel Browsers (max: {MAX_BROWSERS})",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(10, 4))
        self.bulk_browsers_var = tk.StringVar(value=str(MAX_BROWSERS))
        browser_row = tk.Frame(settings_card, bg=BG_CARD)
        browser_row.pack(fill="x")
        tk.Entry(browser_row, textvariable=self.bulk_browsers_var,
                 font=FONT_NORMAL, bg=BG_INPUT, fg=TEXT_WHITE,
                 insertbackground=TEXT_WHITE, relief="flat", bd=0, width=5,
                 highlightthickness=1, highlightbackground=BG_INPUT, highlightcolor=ACCENT
                 ).pack(side="left", ipady=7, padx=(0, 8))
        for v in ["2", "4", str(MAX_BROWSERS)]:
            tk.Button(browser_row, text=v, font=FONT_SMALL, bg=BG_INPUT,
                      fg=TEXT_GRAY, relief="flat", cursor="hand2", padx=10, pady=5,
                      command=lambda x=v: self.bulk_browsers_var.set(x)
                      ).pack(side="left", padx=3)

        # Auto-save interval
        tk.Label(settings_card, text="Auto-save every N records",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(10, 4))
        self.bulk_autosave_var = tk.StringVar(value="100")
        save_row = tk.Frame(settings_card, bg=BG_CARD)
        save_row.pack(fill="x")
        tk.Entry(save_row, textvariable=self.bulk_autosave_var,
                 font=FONT_NORMAL, bg=BG_INPUT, fg=TEXT_WHITE,
                 insertbackground=TEXT_WHITE, relief="flat", bd=0, width=5,
                 highlightthickness=1, highlightbackground=BG_INPUT, highlightcolor=ACCENT
                 ).pack(side="left", ipady=7, padx=(0, 8))
        for v in ["50", "100", "500"]:
            tk.Button(save_row, text=v, font=FONT_SMALL, bg=BG_INPUT,
                      fg=TEXT_GRAY, relief="flat", cursor="hand2", padx=10, pady=5,
                      command=lambda x=v: self.bulk_autosave_var.set(x)
                      ).pack(side="left", padx=3)

        # Browser Visibility Mode (Headless Toggle)
        bulk_mode_row = tk.Frame(settings_card, bg=BG_CARD)
        bulk_mode_row.pack(fill="x", pady=(12, 0))

        self.bulk_headless_var = tk.BooleanVar(value=True)

        def _update_bulk_headless_hint():
            if self.bulk_headless_var.get():
                self.lbl_bulk_headless_hint.config(
                    text="⚡ Headless ON: Silent background workers",
                    fg="#f59e0b"
                )
            else:
                self.lbl_bulk_headless_hint.config(
                    text="👁️ Headless OFF: Visible browser windows",
                    fg="#10b981"
                )

        self.cb_bulk_headless = tk.Checkbutton(
            bulk_mode_row,
            text="Headless Mode (Hide Browser Windows)",
            variable=self.bulk_headless_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=_update_bulk_headless_hint
        )
        self.cb_bulk_headless.pack(side="left", padx=(0, 10))

        self.lbl_bulk_headless_hint = tk.Label(
            bulk_mode_row,
            text="⚡ Headless ON: Silent background workers",
            font=FONT_SMALL,
            bg=BG_CARD,
            fg="#f59e0b"
        )
        self.lbl_bulk_headless_hint.pack(side="left")

        # Stats card
        stats_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        stats_card.pack(fill="x")
        tk.Label(stats_card, text="📊  Stats",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        self.bulk_stat_labels = {}
        for key, label in [("collected", "Records Collected"),
                            ("done",      "Jobs Done"),
                            ("failed",    "Jobs Failed"),
                            ("elapsed",   "Time Elapsed"),
                            ("eta",       "ETA")]:
            row = tk.Frame(stats_card, bg=BG_CARD)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=f"{label}:", font=FONT_SMALL,
                     bg=BG_CARD, fg=TEXT_GRAY, width=18, anchor="w").pack(side="left")
            lbl = tk.Label(row, text="—", font=FONT_SMALL,
                           bg=BG_CARD, fg=TEXT_WHITE)
            lbl.pack(side="left")
            self.bulk_stat_labels[key] = lbl

        # ── RIGHT ─────────────────────────────

        # Job queue table
        queue_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        queue_card.pack(fill="x", pady=(0, 16))
        qrow = tk.Frame(queue_card, bg=BG_CARD)
        qrow.pack(fill="x")
        tk.Label(qrow, text="📋  Job Queue",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left")
        self.bulk_job_count = tk.Label(qrow, text="0 jobs",
                                       font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.bulk_job_count.pack(side="right")

        # Treeview for jobs
        tree_frame = tk.Frame(queue_card, bg=BG_CARD)
        tree_frame.pack(fill="x", pady=(10, 0))
        tsb = tk.Scrollbar(tree_frame)
        tsb.pack(side="right", fill="y")

        cols = ("Amenity", "Area", "District", "Level", "Limit", "Status", "Collected")
        self.bulk_tree = ttk.Treeview(tree_frame, columns=cols,
                                      show="headings", height=5,
                                      yscrollcommand=tsb.set,
                                      style="Amenity.Treeview")
        widths = {"Amenity": 100, "Area": 120, "District": 140,
                  "Level": 80, "Limit": 60, "Status": 90, "Collected": 80}
        for col in cols:
            self.bulk_tree.heading(col, text=col)
            self.bulk_tree.column(col, width=widths[col], anchor="w")
        self.bulk_tree.pack(fill="x")
        tsb.config(command=self.bulk_tree.yview)

        # Live log
        log_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        log_card.pack(fill="both", expand=True)
        tk.Label(log_card, text="📡  Live Log",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")

        lf = tk.Frame(log_card, bg=BG_CARD)
        lf.pack(fill="both", expand=True, pady=(10, 0))
        lsb = tk.Scrollbar(lf)
        lsb.pack(side="right", fill="y")
        self.bulk_log = tk.Text(lf, yscrollcommand=lsb.set,
                                bg=BG_INPUT, fg=TEXT_WHITE, font=FONT_MONO,
                                relief="flat", bd=0, highlightthickness=0,
                                state="disabled", wrap="word")
        self.bulk_log.pack(fill="both", expand=True)
        lsb.config(command=self.bulk_log.yview)
        self.bulk_log.tag_config("success", foreground=GREEN)
        self.bulk_log.tag_config("error",   foreground=RED)
        self.bulk_log.tag_config("info",    foreground=ACCENT)
        self.bulk_log.tag_config("warn",    foreground=YELLOW)
        self.bulk_log.tag_config("normal",  foreground=TEXT_WHITE)

        # Overall progress bar
        self.bulk_total_var = tk.DoubleVar()
        ttk.Progressbar(log_card, variable=self.bulk_total_var,
                        maximum=100).pack(fill="x", pady=(10, 4))
        self.bulk_prog_label = tk.Label(log_card, text="Ready",
                                        font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.bulk_prog_label.pack(anchor="e")

    # ── Bulk Actions ──────────────────────────

    def _bulk_browse(self):
        fp = filedialog.askopenfilename(
            title="Select jobs CSV",
            initialdir=get_downloads_dir(),
            filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not fp:
            return
        try:
            self.bulk_jobs = load_jobs_safe(fp)
        except Exception as exc:
            self.bulk_jobs = []
            messagebox.showerror(
                "Jobs File Error",
                "Could not load jobs from file.\n\n"
                f"Reason: {exc}\n\n"
                "Make sure your CSV has columns: amenity, area, level, limit."
            )
            self._bulk_log(f"❌ Error loading jobs: {exc}\n", "error")
            return

        if not self.bulk_jobs:
            # Check if this file has locations/boundaries
            try:
                boundary_items = read_locations_from_file(fp)
            except Exception:
                boundary_items = []

            if boundary_items:
                ans = messagebox.askyesno(
                    "Boundary Locations Detected",
                    f"'{os.path.basename(fp)}' contains {len(boundary_items)} boundary/location queries.\n\n"
                    "Bulk Runner is designed for Amenity searches (e.g. hospitals, schools).\n"
                    "For Boundary extraction, locations are processed in Tab 2 (Boundary Tracer).\n\n"
                    f"Would you like to switch to Tab 2 and import these {len(boundary_items)} locations into the Boundary Queue?"
                )
                if ans:
                    self.notebook.select(1)
                    for item in boundary_items:
                        self.bnd_queries.append(item)
                        self.bnd_listbox.insert(tk.END, f"  {len(self.bnd_queries)}.  {item}")
                    self._bnd_update_queue_display()
                    self.bnd_file_label.config(text=f"✅  {os.path.basename(fp)} ({len(boundary_items)} items)", fg=GREEN)
                    self._bnd_log(f"✅ Loaded {len(boundary_items)} boundary queries from {os.path.basename(fp)}\n", "success")
                return

            messagebox.showerror(
                "Error",
                "No valid jobs found. Required columns: amenity, area."
            )
            return
        self.bulk_file_label.config(
            text=f"✅  {os.path.basename(fp)} — {len(self.bulk_jobs)} jobs", fg=GREEN)
        self.bulk_job_count.config(
            text=f"{len(self.bulk_jobs)} jobs", fg="#8e44ad")
        self._bulk_refresh_tree()
        self.bulk_start_btn.config(state="normal")
        self._bulk_log(f"✅ Loaded {len(self.bulk_jobs)} jobs from {os.path.basename(fp)}\n", "success")

    def _bulk_create_sample(self):
        fp = filedialog.asksaveasfilename(
            initialdir=get_downloads_dir(),
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="bulk_jobs.csv")
        if fp:
            create_sample_jobs_csv(fp)
            messagebox.showinfo("Created",
                f"Sample jobs file created:\n{fp}\n\nEdit it and import it.")
            os.startfile(fp)

    def _bulk_refresh_tree(self):
        for row in self.bulk_tree.get_children():
            self.bulk_tree.delete(row)
        for job in self.bulk_jobs:
            status_icon = {"pending": "⏳", "running": "🔄",
                           "done": "✅", "failed": "❌"}.get(job["status"], "⏳")
            self.bulk_tree.insert("", "end", iid=str(job["id"]), values=(
                job["amenity"], job["area"], job.get("district", ""), job["level"],
                job["limit"], f"{status_icon} {job['status']}",
                job["collected"]
            ))

    def _bulk_start(self):
        if not self.bulk_jobs:
            messagebox.showwarning("No Jobs", "Please import a jobs CSV file first.")
            return
        try:
            browsers   = int(self.bulk_browsers_var.get())
            autosave   = int(self.bulk_autosave_var.get())
        except ValueError:
            browsers, autosave = MAX_BROWSERS, 100

        self.bulk_start_btn.config(state="disabled", text="⚡ Running...")
        self.bulk_stop_btn.config(state="normal")
        self.bulk_resume_btn.config(state="disabled")
        self.bulk_status_label.config(text="🔄  Running...", fg="#8e44ad")

        bulk_output_dir = os.path.join(get_downloads_dir(), "JarvisMaps_Bulk_Output")
        os.makedirs(bulk_output_dir, exist_ok=True)

        self.bulk_runner = BulkRunner(
            jobs             = self.bulk_jobs,
            max_browsers     = browsers,
            autosave_every   = autosave,
            output_dir       = bulk_output_dir,
            log_callback     = self._bulk_log,
            status_callback  = self._bulk_job_status,
            progress_callback= self._bulk_progress,
            done_callback    = self._bulk_done,
            headless         = self.bulk_headless_var.get(),
        )
        self.current_bulk_run_id = self.bulk_runner.run_id
        self.bulk_runner.start(resume=False)
        self._bulk_stats_loop()

    def _bulk_resume(self):
        if not self.bulk_jobs:
            messagebox.showwarning("No Jobs", "Please import jobs file first.")
            return

        bulk_output_dir = os.path.join(get_downloads_dir(), "JarvisMaps_Bulk_Output")
        os.makedirs(bulk_output_dir, exist_ok=True)

        run_id = getattr(self, "current_bulk_run_id", None)
        if not run_id:
            runs_base = Path(bulk_output_dir) / "runs"
            if not runs_base.exists():
                runs_base = Path("bulk_output") / "runs"
            if runs_base.exists():
                candidate_runs = sorted(
                    [d for d in runs_base.iterdir() if d.is_dir()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                for cand in candidate_runs:
                    prog = cand / "progress.json"
                    valid, _ = ProgressManager.validate_resume(prog, self.bulk_jobs)
                    if valid:
                        run_id = cand.name
                        self.current_bulk_run_id = run_id
                        break

        if not run_id:
            messagebox.showerror(
                "Resume Failed",
                "No valid in-progress run found to resume.\n"
                "Please click 'Start Bulk Run' to begin a new run."
            )
            return

        run_prog = Path(bulk_output_dir) / "runs" / run_id / "progress.json"
        if not run_prog.exists():
            run_prog = Path("bulk_output") / "runs" / run_id / "progress.json"
        valid, msg = ProgressManager.validate_resume(run_prog, self.bulk_jobs)
        if not valid:
            messagebox.showerror(
                "Resume Failed",
                f"Cannot resume run '{run_id}':\n{msg}\n"
                "Please click 'Start Bulk Run' instead."
            )
            return

        try:
            browsers = int(self.bulk_browsers_var.get())
            autosave = int(self.bulk_autosave_var.get())
        except ValueError:
            browsers, autosave = MAX_BROWSERS, 100

        self.bulk_start_btn.config(state="disabled", text="⚡ Running...")
        self.bulk_stop_btn.config(state="normal")
        self.bulk_resume_btn.config(state="disabled")
        self.bulk_status_label.config(text=f"▶️  Resuming {run_id}...", fg=YELLOW)

        try:
            self.bulk_runner = BulkRunner(
                jobs             = self.bulk_jobs,
                max_browsers     = browsers,
                autosave_every   = autosave,
                output_dir       = bulk_output_dir,
                log_callback     = self._bulk_log,
                status_callback  = self._bulk_job_status,
                progress_callback= self._bulk_progress,
                done_callback    = self._bulk_done,
                run_id           = run_id,
                headless         = self.bulk_headless_var.get(),
            )
            self.bulk_runner.start(resume=True)
            self._bulk_stats_loop()
        except Exception as exc:
            self.bulk_start_btn.config(state="normal", text="⚡  Start Bulk Run")
            self.bulk_stop_btn.config(state="disabled")
            self.bulk_resume_btn.config(state="normal")
            self.bulk_status_label.config(text="❌  Resume Error", fg=RED)
            messagebox.showerror("Resume Error", str(exc))

    def _bulk_stop(self):
        if self.bulk_runner:
            self.bulk_runner.stop()
        self.bulk_stop_btn.config(state="disabled")
        self.bulk_resume_btn.config(state="normal")
        self.bulk_start_btn.config(state="normal", text="⚡  Start Bulk Run")
        self.bulk_status_label.config(text="⏸️  Stopped", fg=YELLOW)

    def _bulk_job_status(self, job_id, raw_status):
        def _update():
            try:
                job = next(j for j in self.bulk_jobs if j["id"] == job_id)

                state = str(raw_status)
                if "|" in state:
                    state, value = state.split("|", 1)
                    if state == "running":
                        try:
                            job["collected"] = int(value)
                        except ValueError:
                            pass

                status_icon = {
                    "pending": "⏳",
                    "running": "🔄",
                    "done": "✅",
                    "failed": "❌",
                }.get(state, "⏳")

                job["status"] = state

                self.bulk_tree.item(
                    str(job_id),
                    values=(
                        job["amenity"],
                        job["area"],
                        job["level"],
                        job["limit"],
                        f"{status_icon} {state}",
                        job["collected"],
                    ),
                )

            except Exception as e:
                print(f"Error updating bulk job status: {e}")

        self.root.after(0, _update)


    def _bulk_progress(self, done, total, pct, eta):
        def _update():
            self.bulk_total_var.set(pct)
            self.bulk_prog_label.config(
                text=f"{done}/{total} jobs  •  ETA: {BulkRunner.format_time(eta)}")
        self.root.after(0, _update)

    def _bulk_done(self, results):
        def _update():
            self.bulk_start_btn.config(state="normal", text="⚡  Start Bulk Run")
            self.bulk_stop_btn.config(state="disabled")
            self.bulk_resume_btn.config(state="disabled")
            self.bulk_open_btn.config(state="normal", bg=ACCENT2, fg=BG_DARK)
            batch_tag = f" (Batch: {self.current_bulk_run_id})" if getattr(self, "current_bulk_run_id", None) else ""
            self.bulk_status_label.config(
                text=f"✅  Done — {len(results)} total records{batch_tag}", fg=GREEN)
            self.bulk_total_var.set(100)

            # Persist jobs into database
            try:
                for j in getattr(self, "bulk_jobs", []):
                    save_bulk_runner_job(j)
            except Exception:
                pass
        self.root.after(0, _update)

    def _bulk_stats_loop(self):
        """Update stats every 2 seconds while running."""
        if not self.bulk_runner:
            return
        stats = self.bulk_runner.get_stats()
        self.bulk_stat_labels["collected"].config(
            text=str(stats["collected"]), fg=GREEN)
        self.bulk_stat_labels["done"].config(
            text=f"{stats['done']}/{stats['total']}", fg=ACCENT)
        self.bulk_stat_labels["failed"].config(
            text=str(stats["failed"]),
            fg=RED if stats["failed"] > 0 else TEXT_WHITE)
        self.bulk_stat_labels["elapsed"].config(
            text=BulkRunner.format_time(stats["elapsed"]))
        self.bulk_stat_labels["eta"].config(
            text=BulkRunner.format_time(stats["eta"]))

        if self.bulk_runner.is_running:
            self.root.after(2000, self._bulk_stats_loop)

    def _bulk_open_output(self):
        """Open bulk_output folder or active run folder in Windows Explorer."""
        bulk_output_dir = os.path.join(get_downloads_dir(), "JarvisMaps_Bulk_Output")
        run_id = getattr(self, "current_bulk_run_id", None)
        if run_id:
            run_folder = os.path.join(bulk_output_dir, "runs", run_id)
            if os.path.exists(run_folder):
                os.startfile(run_folder)
                return
            old_run_folder = os.path.join("bulk_output", "runs", run_id)
            if os.path.exists(old_run_folder):
                os.startfile(old_run_folder)
                return
        runs_folder = os.path.join(bulk_output_dir, "runs")
        if os.path.exists(runs_folder):
            os.startfile(runs_folder)
        elif os.path.exists(bulk_output_dir):
            os.startfile(bulk_output_dir)
        elif os.path.exists("bulk_output"):
            os.startfile("bulk_output")
        else:
            messagebox.showinfo("Not Found",
                "No output folder yet.\nStart a bulk run first.")

    def _bulk_log(self, message: str, tag: str = "normal"):
        def _write():
            self.bulk_log.config(state="normal")
            self.bulk_log.insert(tk.END, message, tag)
            self.bulk_log.see(tk.END)
            self.bulk_log.config(state="disabled")
        self.root.after(0, _write)


    # ══════════════════════════════════════════
    #  TAB 6 — BOUNDARY VALIDATOR & FILTER
    # ══════════════════════════════════════════

    def _build_validator_tab(self, parent):
        """Build the dedicated Google Maps Boundary Validator & Filter Tab."""
        self.val_source_mode_var = tk.StringVar(value="active")
        self.val_file_path_var = tk.StringVar()
        self.val_output_dir_var = tk.StringVar(value=os.path.join(get_downloads_dir(), "JarvisMaps_Validated_Areas"))
        self.val_filter_var = tk.StringVar(value="all")
        self.val_speed_mode_var = tk.StringVar(value="fast")
        self.val_workers_var = tk.IntVar(value=3)
        self.val_headless_var = tk.BooleanVar(value=False)

        self.val_loaded_records = []
        self.val_validator_engine = None
        self.val_is_validating = False
        self.val_item_durations = []
        self.val_start_time = None
        self.val_current_testing_name = ""
        self.val_timer_job = None

        # 1. Header Banner
        header = tk.Frame(parent, bg=BG_DARK, padx=24, pady=10)
        header.pack(fill="x")

        tk.Label(
            header,
            text="🛡️  Google Maps Boundary Validator & Filter",
            font=FONT_TITLE,
            bg=BG_DARK,
            fg=TEXT_WHITE
        ).pack(anchor="w")

        tk.Label(
            header,
            text="Verify which extracted areas actually have official red outline boundaries in Google Maps and filter or export verified datasets.",
            font=FONT_SUB,
            bg=BG_DARK,
            fg=TEXT_GRAY
        ).pack(anchor="w", pady=(2, 0))

        # 2. Settings Card (Source Data & Config)
        opt_card = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=20, pady=10)
        opt_card.pack(fill="x", padx=24, pady=(0, 10))

        # Source Selection Row
        src_row = tk.Frame(opt_card, bg=BG_CARD)
        src_row.pack(fill="x", pady=(0, 8))

        tk.Label(src_row, text="Source Data:", font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left", padx=(0, 16))

        active_cnt = len(getattr(self, "area_results", []))
        self.val_rb_active = tk.Radiobutton(
            src_row,
            text=f"Active Area Explorer Results ({active_cnt} areas)",
            value="active",
            variable=self.val_source_mode_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=self._val_on_source_change
        )
        self.val_rb_active.pack(side="left", padx=(0, 16))

        self.val_rb_file = tk.Radiobutton(
            src_row,
            text="Upload Area Explorer File (.xlsx, .csv, .txt)",
            value="file",
            variable=self.val_source_mode_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=self._val_on_source_change
        )
        self.val_rb_file.pack(side="left")

        # File & Output Directory Row
        path_row = tk.Frame(opt_card, bg=BG_CARD)
        path_row.pack(fill="x")

        # File selector frame
        self.val_file_frame = tk.Frame(path_row, bg=BG_CARD)
        self.val_file_frame.pack(side="left", fill="x", expand=True, padx=(0, 12))

        tk.Label(self.val_file_frame, text="File Path:", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")
        f_sub = tk.Frame(self.val_file_frame, bg=BG_CARD)
        f_sub.pack(fill="x", pady=(2, 0))

        self.val_file_entry = tk.Entry(
            f_sub,
            textvariable=self.val_file_path_var,
            font=FONT_NORMAL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BG_INPUT,
            highlightcolor=ACCENT
        )
        self.val_file_entry.pack(side="left", fill="x", expand=True, ipady=5)

        self.val_file_btn = tk.Button(
            f_sub,
            text="📂 Browse...",
            font=FONT_SMALL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=4,
            command=self._val_on_browse_file
        )
        self.val_file_btn.pack(side="left", padx=(6, 0))

        # Output Dir frame
        out_frame = tk.Frame(path_row, bg=BG_CARD)
        out_frame.pack(side="left", fill="x", expand=True)

        tk.Label(out_frame, text="📁 Save Boundaries to:", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")
        o_sub = tk.Frame(out_frame, bg=BG_CARD)
        o_sub.pack(fill="x", pady=(2, 0))

        self.val_out_entry = tk.Entry(
            o_sub,
            textvariable=self.val_output_dir_var,
            font=FONT_NORMAL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BG_INPUT,
            highlightcolor=ACCENT
        )
        self.val_out_entry.pack(side="left", fill="x", expand=True, ipady=5)

        self.val_out_btn = tk.Button(
            o_sub,
            text="Browse...",
            font=FONT_SMALL,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=10,
            pady=4,
            command=self._val_on_browse_output_dir
        )
        self.val_out_btn.pack(side="left", padx=(6, 0))

        # Browser Visibility Mode (Headless Toggle)
        mode_row = tk.Frame(opt_card, bg=BG_CARD)
        mode_row.pack(fill="x", pady=(8, 0))

        def _update_headless_hint():
            if self.val_headless_var.get():
                self.val_lbl_headless_hint.config(
                    text="⚡ Headless ON: Browser runs silently in background",
                    fg="#f59e0b"
                )
            else:
                self.val_lbl_headless_hint.config(
                    text="👁️ Headless OFF: Browser window will open visibly on your screen",
                    fg="#10b981"
                )

        self.val_cb_headless = tk.Checkbutton(
            mode_row,
            text="Headless Mode (Hide Browser Window)",
            variable=self.val_headless_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=_update_headless_hint
        )
        self.val_cb_headless.pack(side="left", padx=(0, 12))

        self.val_lbl_headless_hint = tk.Label(
            mode_row,
            text="👁️ Headless OFF: Browser window will open visibly on your screen",
            font=FONT_SMALL,
            bg=BG_CARD,
            fg="#10b981"
        )
        self.val_lbl_headless_hint.pack(side="left")

        # Speed Mode & Parallel Workers Row
        speed_row = tk.Frame(opt_card, bg=BG_CARD)
        speed_row.pack(fill="x", pady=(8, 0))

        tk.Label(speed_row, text="⚡ Speed Mode:", font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left", padx=(0, 8))

        def _on_speed_setting_change(*_):
            self._val_update_metric_cards()

        self.val_rb_fast = tk.Radiobutton(
            speed_row,
            text="⚡ Turbo Fast (~1-2s / area)",
            value="fast",
            variable=self.val_speed_mode_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg="#38bdf8",
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground="#38bdf8",
            command=_on_speed_setting_change
        )
        self.val_rb_fast.pack(side="left", padx=(0, 10))

        self.val_rb_deep = tk.Radiobutton(
            speed_row,
            text="🔬 Deep GIS Trace (~15-20s)",
            value="deep",
            variable=self.val_speed_mode_var,
            font=FONT_NORMAL,
            bg=BG_CARD,
            fg=TEXT_WHITE,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=TEXT_WHITE,
            command=_on_speed_setting_change
        )
        self.val_rb_deep.pack(side="left", padx=(0, 20))

        tk.Label(speed_row, text="⚡ Parallel Workers:", font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left", padx=(0, 8))

        self.val_cb_workers = ttk.Combobox(
            speed_row,
            textvariable=self.val_workers_var,
            values=[1, 2, 3, 4, 5, 6],
            width=3,
            state="readonly"
        )
        self.val_cb_workers.pack(side="left", padx=(0, 6))
        self.val_cb_workers.bind("<<ComboboxSelected>>", lambda e: self._val_update_metric_cards())

        tk.Label(speed_row, text="(3-5x faster parallel validation)", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left")

        # 3. Metric Cards Bar
        metrics_frame = tk.Frame(parent, bg=BG_DARK, padx=24)
        metrics_frame.pack(fill="x", pady=(0, 8))

        def _make_metric_card(parent_f, title, default_val, text_color, sub_text=""):
            card = tk.Frame(parent_f, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=14, pady=6)
            card.pack(side="left", fill="x", expand=True, padx=4)
            tk.Label(card, text=title, font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w")
            lbl = tk.Label(card, text=default_val, font=("Segoe UI", 15, "bold"), bg=BG_CARD, fg=text_color)
            lbl.pack(anchor="w")
            sub_lbl = tk.Label(card, text=sub_text, font=("Segoe UI", 8), bg=BG_CARD, fg=TEXT_GRAY)
            sub_lbl.pack(anchor="w")
            return lbl, sub_lbl

        self.val_lbl_total_cnt, self.val_lbl_total_sub = _make_metric_card(metrics_frame, "Total Areas", "0", TEXT_WHITE, "")
        self.val_lbl_tested_cnt, self.val_lbl_tested_sub = _make_metric_card(metrics_frame, "Tested", "0 / 0", "#38bdf8", "")
        self.val_lbl_valid_cnt, self.val_lbl_valid_sub = _make_metric_card(metrics_frame, "✅ Valid Boundaries", "0", "#10b981", "")
        self.val_lbl_invalid_cnt, self.val_lbl_invalid_sub = _make_metric_card(metrics_frame, "❌ No Boundary", "0", "#ef4444", "")
        self.val_lbl_time_val, self.val_lbl_time_sub = _make_metric_card(metrics_frame, "⏱ Expected Time", "--", "#fbbf24", "~1-2s / area")

        # 4. Progress bar & status
        prog_frame = tk.Frame(parent, bg=BG_DARK, padx=28)
        prog_frame.pack(fill="x", pady=(0, 8))

        self.val_progress_bar = ttk.Progressbar(prog_frame, mode="determinate")
        self.val_progress_bar.pack(fill="x", pady=(0, 4))

        self.val_lbl_progress_text = tk.Label(
            prog_frame,
            text="Ready to validate. Click 'Start Validation' to begin.",
            font=FONT_SMALL,
            bg=BG_DARK,
            fg=TEXT_GRAY
        )
        self.val_lbl_progress_text.pack(anchor="w")

        # 5. Bottom Action Toolbar (Pinned to bottom so it's always visible)
        action_bar = tk.Frame(parent, bg=BG_DARK, padx=24, pady=10)
        action_bar.pack(side="bottom", fill="x")

        # Left controls
        self.val_btn_start = tk.Button(
            action_bar,
            text="▶  Start Validation",
            font=FONT_BTN,
            bg="#10b981",
            fg="white",
            relief="flat",
            cursor="hand2",
            padx=18,
            pady=8,
            command=self._val_start_validation
        )
        self.val_btn_start.pack(side="left", padx=(0, 8))

        self.val_btn_stop = tk.Button(
            action_bar,
            text="⏸  Stop",
            font=FONT_BTN,
            bg="#ef4444",
            fg="white",
            relief="flat",
            cursor="hand2",
            padx=14,
            pady=8,
            state="disabled",
            command=self._val_stop_validation
        )
        self.val_btn_stop.pack(side="left", padx=(0, 8))

        # Right buttons
        self.val_btn_open_folder = tk.Button(
            action_bar,
            text="📁 Open Folder",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=8,
            command=self._val_open_output_folder
        )
        self.val_btn_open_folder.pack(side="right", padx=(6, 0))

        self.val_btn_export_kml = tk.Button(
            action_bar,
            text="🗺️ Export KML",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=8,
            command=self._val_export_kml
        )
        self.val_btn_export_kml.pack(side="right", padx=(6, 0))

        self.val_btn_export_excel = tk.Button(
            action_bar,
            text="📊 Export Excel",
            font=FONT_BTN,
            bg=BG_INPUT,
            fg=TEXT_WHITE,
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=8,
            command=self._val_export_excel
        )
        self.val_btn_export_excel.pack(side="right", padx=(6, 0))

        self.val_btn_send_bnd = tk.Button(
            action_bar,
            text="📥 Send to Boundary Tracer",
            font=FONT_BTN,
            bg="#3b82f6",
            fg="white",
            relief="flat",
            cursor="hand2",
            padx=14,
            pady=8,
            command=self._val_send_to_tracer
        )
        self.val_btn_send_bnd.pack(side="right", padx=(6, 0))

        # 6. Results Table Card (Expands in remaining space)
        table_card = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=16, pady=10)
        table_card.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        # Filter bar inside table card
        filt_bar = tk.Frame(table_card, bg=BG_CARD)
        filt_bar.pack(fill="x", pady=(0, 8))

        tk.Label(filt_bar, text="Filter Results:", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY).pack(side="left", padx=(0, 10))

        for f_label, f_val in [
            ("All", "all"),
            ("✅ Valid Boundaries Only", "valid"),
            ("❌ No Boundary Only", "invalid"),
        ]:
            tk.Radiobutton(
                filt_bar,
                text=f_label,
                value=f_val,
                variable=self.val_filter_var,
                font=FONT_SMALL,
                bg=BG_CARD,
                fg=TEXT_WHITE,
                selectcolor=BG_INPUT,
                activebackground=BG_CARD,
                activeforeground=TEXT_WHITE,
                command=self._val_populate_treeview
            ).pack(side="left", padx=(0, 10))

        self.val_lbl_table_summary = tk.Label(filt_bar, text="Showing 0 areas", font=FONT_SMALL, bg=BG_CARD, fg=TEXT_GRAY)
        self.val_lbl_table_summary.pack(side="right")

        # Treeview
        tree_frame = tk.Frame(table_card, bg=BG_CARD)
        tree_frame.pack(fill="both", expand=True)

        sc_y = ttk.Scrollbar(tree_frame, orient="vertical")
        sc_y.pack(side="right", fill="y")
        sc_x = ttk.Scrollbar(tree_frame, orient="horizontal")
        sc_x.pack(side="bottom", fill="x")

        cols = ("idx", "name", "parent", "type", "query", "status", "points", "source")
        self.val_tree = ttk.Treeview(
            tree_frame,
            columns=cols,
            show="headings",
            yscrollcommand=sc_y.set,
            xscrollcommand=sc_x.set
        )
        sc_y.config(command=self.val_tree.yview)
        sc_x.config(command=self.val_tree.xview)

        self.val_tree.heading("idx", text="#")
        self.val_tree.heading("name", text="Area Name")
        self.val_tree.heading("parent", text="Parent Area / City")
        self.val_tree.heading("type", text="Type")
        self.val_tree.heading("query", text="Google Maps Query")
        self.val_tree.heading("status", text="Boundary Status")
        self.val_tree.heading("points", text="Points")
        self.val_tree.heading("source", text="Details / Source")

        self.val_tree.column("idx", width=45, anchor="center")
        self.val_tree.column("name", width=170, anchor="w")
        self.val_tree.column("parent", width=130, anchor="w")
        self.val_tree.column("type", width=90, anchor="center")
        self.val_tree.column("query", width=180, anchor="w")
        self.val_tree.column("status", width=140, anchor="center")
        self.val_tree.column("points", width=65, anchor="center")
        self.val_tree.column("source", width=140, anchor="w")

        self.val_tree.tag_configure("valid", foreground="#10b981")
        self.val_tree.tag_configure("invalid", foreground="#ef4444")
        self.val_tree.tag_configure("pending", foreground="#94a3b8")
        self.val_tree.tag_configure("testing", foreground="#38bdf8")

        self.val_tree.pack(fill="both", expand=True)

        self._val_load_initial_source()

    def _val_on_tab_focused(self):
        """Called when user switches to Boundary Validator tab."""
        active_cnt = len(getattr(self, "area_results", []))
        if hasattr(self, "val_rb_active"):
            self.val_rb_active.config(text=f"Active Area Explorer Results ({active_cnt} areas)")
            if self.val_source_mode_var.get() == "active" and not self.val_is_validating:
                self.val_loaded_records = load_area_records(self.area_results)
                self._val_populate_treeview()
                self._val_update_metric_cards()

    def _val_format_duration(self, seconds: float) -> str:
        s = max(0, int(seconds))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m:02d}m {s:02d}s"
        elif m > 0:
            return f"{m}m {s:02d}s"
        else:
            return f"{s}s"

    def _val_load_initial_source(self):
        if self.val_source_mode_var.get() == "active" and getattr(self, "area_results", None):
            self.val_loaded_records = load_area_records(self.area_results)
            self._val_populate_treeview()
            self._val_update_metric_cards()
        self._val_on_source_change()

    def _val_on_source_change(self):
        mode = self.val_source_mode_var.get()
        if mode == "file":
            self.val_file_entry.config(state="normal")
            self.val_file_btn.config(state="normal")
            if self.val_file_path_var.get():
                self._val_load_file_records(self.val_file_path_var.get())
        else:
            self.val_file_entry.config(state="disabled")
            self.val_file_btn.config(state="disabled")
            self.val_loaded_records = load_area_records(getattr(self, "area_results", []))
            self._val_populate_treeview()
            self._val_update_metric_cards()

    def _val_on_browse_file(self):
        file_path = filedialog.askopenfilename(
            parent=self.root,
            title="Select Area Explorer Export File",
            initialdir=get_downloads_dir(),
            filetypes=[
                ("Supported Files", "*.xlsx;*.xls;*.csv;*.tsv;*.txt"),
                ("Excel Files", "*.xlsx;*.xls"),
                ("CSV Files", "*.csv;*.tsv"),
                ("Text Files", "*.txt"),
            ]
        )
        if not file_path:
            return
        self.val_file_path_var.set(file_path)
        self._val_load_file_records(file_path)

    def _val_load_file_records(self, file_path: str):
        try:
            records = load_area_records(file_path)
            self.val_loaded_records = records
            self._val_populate_treeview()
            self._val_update_metric_cards()
            self.val_lbl_progress_text.config(
                text=f"Loaded {len(records)} areas from {os.path.basename(file_path)}. Ready to validate."
            )
        except Exception as e:
            messagebox.showerror("Read Error", f"Failed to read file:\n{e}", parent=self.root)

    def _val_on_browse_output_dir(self):
        folder = filedialog.askdirectory(
            parent=self.root,
            title="Select Boundaries Output Directory",
            initialdir=self.val_output_dir_var.get() or get_downloads_dir()
        )
        if folder:
            self.val_output_dir_var.set(folder)

    def _val_populate_treeview(self):
        filt = self.val_filter_var.get()
        for item in self.val_tree.get_children():
            self.val_tree.delete(item)

        disp_count = 0
        for i, r in enumerate(self.val_loaded_records, start=1):
            has_bnd = r.get("has_boundary")
            if filt == "valid" and has_bnd is not True:
                continue
            if filt == "invalid" and has_bnd is not False:
                continue

            disp_count += 1
            status_str = r.get("status", "Pending")
            b_info = r.get("boundary_info") or {}
            pts = _format_points_count(b_info.get("polygon_points")) if has_bnd else "-"
            src = b_info.get("source") or b_info.get("error") or "-"

            tag = "pending"
            if has_bnd is True:
                tag = "valid"
            elif has_bnd is False:
                tag = "invalid"
            elif "Testing" in status_str:
                tag = "testing"

            self.val_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    str(i),
                    r.get("name", ""),
                    r.get("parent_area", ""),
                    r.get("type", "Area"),
                    r.get("query", ""),
                    status_str,
                    pts,
                    src[:35] if src else "-",
                ),
                tags=(tag,)
            )

        self.val_lbl_table_summary.config(text=f"Showing {disp_count} of {len(self.val_loaded_records)} areas")

    def _val_update_metric_cards(self):
        total = len(self.val_loaded_records)
        tested = sum(1 for r in self.val_loaded_records if r.get("has_boundary") is not None)
        valid = sum(1 for r in self.val_loaded_records if r.get("has_boundary") is True)
        invalid = sum(1 for r in self.val_loaded_records if r.get("has_boundary") is False)

        self.val_lbl_total_cnt.config(text=str(total))
        self.val_lbl_tested_cnt.config(text=f"{tested} / {total}")
        self.val_lbl_valid_cnt.config(text=str(valid))
        self.val_lbl_invalid_cnt.config(text=str(invalid))

        # Expected / remaining time calculation
        remaining = max(0, total - tested)
        mode = self.val_speed_mode_var.get()
        workers = max(1, self.val_workers_var.get())
        default_rate = (2.0 / workers) if mode == "fast" else 15.0
        avg_time = (sum(self.val_item_durations) / len(self.val_item_durations)) if self.val_item_durations else default_rate

        if not self.val_is_validating and tested == 0:
            initial_est = (total / workers * 2.0) if mode == "fast" else (total * 15.0)
            sub_hint = f"⚡ ~{default_rate:.1f}s/area ({workers} workers)" if mode == "fast" else "~15s per area"
            self.val_lbl_time_val.config(text=f"~{self._val_format_duration(initial_est)}" if total > 0 else "--")
            self.val_lbl_time_sub.config(text=sub_hint if total > 0 else "")
            if total > 0:
                self.val_lbl_progress_text.config(
                    text=f"Ready to validate {total} areas in {'⚡ Turbo Fast' if mode == 'fast' else '🔬 Deep'} mode. Expected completion time: ~{self._val_format_duration(initial_est)} ({sub_hint}). Click 'Start Validation' to begin."
                )
        elif not self.val_is_validating and tested == total and total > 0:
            total_elapsed = time.time() - (self.val_start_time or time.time())
            self.val_lbl_time_val.config(text=self._val_format_duration(total_elapsed))
            self.val_lbl_time_sub.config(text="Completed in full")
        else:
            est_remaining = max(0, remaining * avg_time)
            self.val_lbl_time_val.config(text=f"~{self._val_format_duration(est_remaining)}")

        pct = (tested / total * 100) if total > 0 else 0
        self.val_progress_bar["value"] = pct

    def _val_start_ticker(self):
        self._val_stop_ticker()
        self._val_tick_timer()

    def _val_tick_timer(self):
        if not self.val_is_validating:
            return

        elapsed = time.time() - (self.val_start_time or time.time())
        tested = sum(1 for r in self.val_loaded_records if r.get("has_boundary") is not None)
        total = len(self.val_loaded_records)
        remaining = max(0, total - tested)
        mode = self.val_speed_mode_var.get()
        workers = max(1, self.val_workers_var.get())
        default_rate = (2.0 / workers) if mode == "fast" else 15.0
        avg_time = (sum(self.val_item_durations) / len(self.val_item_durations)) if self.val_item_durations else default_rate
        est_remaining = max(0, remaining * avg_time)

        self.val_lbl_time_val.config(text=f"~{self._val_format_duration(est_remaining)}")
        self.val_lbl_time_sub.config(text=f"Elapsed: {self._val_format_duration(elapsed)}")

        curr = self.val_current_testing_name or "Validating..."
        self.val_lbl_progress_text.config(
            text=f"Testing [{min(tested+1, total)}/{total}] {curr} | "
                 f"⏱ Remaining: ~{self._val_format_duration(est_remaining)} | "
                 f"Elapsed: {self._val_format_duration(elapsed)} (~{avg_time:.1f}s/area, {workers} workers)"
        )

        self.val_timer_job = self.root.after(1000, self._val_tick_timer)

    def _val_stop_ticker(self):
        if getattr(self, "val_timer_job", None):
            try:
                self.root.after_cancel(self.val_timer_job)
            except Exception:
                pass
            self.val_timer_job = None

    def _val_start_validation(self):
        if not self.val_loaded_records:
            messagebox.showwarning("No Areas", "Please search areas in Area Explorer or load a file first.", parent=self.root)
            return

        out_dir = self.val_output_dir_var.get().strip() or "boundaries/validated_areas"
        os.makedirs(out_dir, exist_ok=True)

        self.val_is_validating = True
        self.val_start_time = time.time()
        self.val_item_durations = []
        self.val_current_testing_name = ""

        self.val_btn_start.config(state="disabled")
        self.val_btn_stop.config(state="normal")
        self.val_rb_active.config(state="disabled")
        self.val_rb_file.config(state="disabled")
        self.val_file_btn.config(state="disabled")

        # Reset item status
        for r in self.val_loaded_records:
            r["has_boundary"] = None
            r["status"] = "Pending"
            r["boundary_info"] = {}
        self._val_populate_treeview()
        self._val_update_metric_cards()

        is_headless = self.val_headless_var.get()
        try:
            import playwright_pipeline
            playwright_pipeline.BOUNDARY_HEADLESS = is_headless
        except Exception:
            pass

        self.val_cb_headless.config(state="disabled")
        self.val_rb_fast.config(state="disabled")
        self.val_rb_deep.config(state="disabled")
        self.val_cb_workers.config(state="disabled")

        self.val_validator_engine = AreaBoundaryValidator(
            records=self.val_loaded_records,
            output_dir=out_dir,
            headless=is_headless,
            workers=self.val_workers_var.get(),
            mode=self.val_speed_mode_var.get(),
            on_progress=self._val_on_worker_progress,
            on_log=self._val_on_worker_log,
            on_complete=self._val_on_worker_complete
        )
        self.val_validator_engine.start()
        self._val_start_ticker()

    def _val_stop_validation(self):
        self._val_stop_ticker()
        if self.val_validator_engine:
            self.val_lbl_progress_text.config(text="Stopping validation after current area...")
            self.val_validator_engine.stop()
            self.val_btn_stop.config(state="disabled")

    def _val_on_worker_log(self, message: str, tag: str = "normal"):
        pass

    def _val_on_worker_progress(self, idx, total, item, stats):
        def _update():
            if 1 <= idx <= len(self.val_loaded_records):
                rec = self.val_loaded_records[idx - 1]
                rec["has_boundary"] = item.get("has_boundary")
                rec["status"] = item.get("status")
                rec["boundary_info"] = item.get("boundary_info")

                elapsed_item = stats.get("elapsed", 15.0)
                self.val_item_durations.append(elapsed_item)
                self.val_current_testing_name = item.get("name", "")

                if self.val_tree.exists(str(idx)):
                    has_bnd = rec.get("has_boundary")
                    b_info = rec.get("boundary_info") or {}
                    pts = _format_points_count(b_info.get("polygon_points")) if has_bnd else "-"
                    src = b_info.get("source") or b_info.get("error") or "-"
                    tag = "valid" if has_bnd is True else ("invalid" if has_bnd is False else "pending")
                    self.val_tree.item(
                        str(idx),
                        values=(
                            str(idx),
                            rec.get("name", ""),
                            rec.get("parent_area", ""),
                            rec.get("type", "Area"),
                            rec.get("query", ""),
                            rec.get("status", ""),
                            pts,
                            src[:35] if src else "-",
                        ),
                        tags=(tag,)
                    )

            self._val_update_metric_cards()

        try:
            self.root.after(0, _update)
        except Exception:
            pass

    def _val_on_worker_complete(self, summary):
        def _done():
            self._val_stop_ticker()
            self.val_is_validating = False
            self.val_btn_start.config(state="normal")
            self.val_btn_stop.config(state="disabled")
            self.val_rb_active.config(state="normal")
            self.val_rb_file.config(state="normal")
            self.val_file_btn.config(state="normal")
            self.val_cb_headless.config(state="normal")
            self.val_rb_fast.config(state="normal")
            self.val_rb_deep.config(state="normal")
            self.val_cb_workers.config(state="normal")

            total_elapsed = time.time() - (self.val_start_time or time.time())
            self.val_loaded_records = summary.get("records", self.val_loaded_records)
            self._val_populate_treeview()
            self._val_update_metric_cards()

            # Persist validated records to database
            try:
                save_validated_boundaries(self.val_loaded_records)
            except Exception:
                pass

            val_cnt = summary.get("validated", 0)
            no_bnd_cnt = summary.get("no_boundary", 0)
            self.val_lbl_time_val.config(text=self._val_format_duration(total_elapsed))
            self.val_lbl_time_sub.config(text="Completed in full")
            self.val_lbl_progress_text.config(
                text=f"🏁 Validation finished in {self._val_format_duration(total_elapsed)}: {val_cnt} confirmed boundaries, {no_bnd_cnt} without boundary."
            )

            if self.val_source_mode_var.get() == "active" and getattr(self, "area_results", None):
                self._val_sync_back_to_app()

            messagebox.showinfo(
                "Validation Complete",
                f"Boundary Validation Finished in {self._val_format_duration(total_elapsed)}!\n\n"
                f"✅ Confirmed Boundaries: {val_cnt}\n"
                f"❌ No Boundary Outlines: {no_bnd_cnt}\n\n"
                f"You can now export validated datasets (Excel, KML) or send confirmed locations directly to Boundary Tracer!",
                parent=self.root
            )

        try:
            self.root.after(0, _done)
        except Exception:
            pass

    def _val_sync_back_to_app(self):
        """Sync validated status into the main Area Explorer results and treeview."""
        if not hasattr(self, "area_results") or not self.area_results:
            return

        lookup = {}
        for r in self.val_loaded_records:
            k = (r.get("name", "").strip().lower(), r.get("parent_area", "").strip().lower())
            lookup[k] = r

        for app_rec in self.area_results:
            k = (app_rec.get("name", "").strip().lower(), app_rec.get("parent_area", "").strip().lower())
            if k in lookup:
                val_rec = lookup[k]
                app_rec["has_boundary"] = val_rec.get("has_boundary")
                app_rec["status"] = val_rec.get("status")

        if hasattr(self, "_area_apply_filter"):
            self._area_apply_filter()

    def _val_send_to_tracer(self):
        validated_items = [r for r in self.val_loaded_records if r.get("has_boundary") is True]
        if not validated_items:
            messagebox.showwarning(
                "No Validated Boundaries",
                "No areas have confirmed Google Maps boundaries yet. Run validation first.",
                parent=self.root
            )
            return

        added_count = 0
        existing = {q.strip().casefold() for q in self.bnd_queries}
        for item in validated_items:
            query = item.get("query") or build_boundary_query(item.get("name", ""), item.get("parent_area", ""))
            if query and query.strip().casefold() not in existing:
                self.bnd_queries.append(query.strip())
                existing.add(query.strip().casefold())
                added_count += 1

        self._bnd_update_queue_display()
        try:
            self.notebook.select(1)
        except Exception:
            pass

        self._bnd_log(
            f"📥 Imported {added_count} validated area boundaries from Validator into Boundary Tracer.\n",
            "info"
        )
        messagebox.showinfo(
            "Transferred to Tracer",
            f"Successfully transferred {added_count} verified area boundaries to Boundary Tracer queue!",
            parent=self.root
        )

    def _val_export_excel(self):
        if not self.val_loaded_records:
            return
        val_items = [r for r in self.val_loaded_records if r.get("has_boundary") is True]
        only_val = True
        if not val_items:
            tested_items = [r for r in self.val_loaded_records if r.get("has_boundary") is not None]
            if not tested_items:
                messagebox.showwarning("No Data", "Please run boundary validation before exporting.", parent=self.root)
                return
            if not messagebox.askyesno(
                "No Confirmed Boundaries",
                "No locations have confirmed Google Maps boundaries yet. Export all tested rows anyway?",
                parent=self.root
            ):
                return
            only_val = False

        file_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save Validated Boundaries to Excel",
            initialdir=get_downloads_dir(),
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")],
            initialfile="validated_boundaries.xlsx"
        )
        if not file_path:
            return

        try:
            export_validated_to_excel(self.val_loaded_records, file_path, only_validated=only_val)
            messagebox.showinfo(
                "Export Complete",
                f"Validated boundaries exported successfully to:\n{file_path}",
                parent=self.root
            )
        except Exception as e:
            messagebox.showerror("Export Failed", str(e), parent=self.root)

    def _val_export_kml(self):
        if not self.val_loaded_records:
            return
        val_items = [r for r in self.val_loaded_records if r.get("has_boundary") is True]
        if not val_items:
            messagebox.showwarning(
                "No Boundaries",
                "No confirmed Google Maps boundaries available to export. Run validation first.",
                parent=self.root
            )
            return

        file_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save Combined Boundaries to KML",
            initialdir=get_downloads_dir(),
            defaultextension=".kml",
            filetypes=[("KML Map File", "*.kml")],
            initialfile="validated_boundaries.kml"
        )
        if not file_path:
            return

        try:
            export_combined_kml(self.val_loaded_records, file_path)
            messagebox.showinfo(
                "Export Complete",
                f"Combined KML map saved successfully to:\n{file_path}",
                parent=self.root
            )
        except Exception as e:
            messagebox.showerror("Export Failed", str(e), parent=self.root)

    def _val_open_output_folder(self):
        out_dir = self.val_output_dir_var.get().strip() or os.path.join(get_downloads_dir(), "JarvisMaps_Validated_Areas")
        os.makedirs(out_dir, exist_ok=True)
        try:
            os.startfile(os.path.abspath(out_dir))
        except Exception as e:
            messagebox.showerror("Open Folder Failed", str(e), parent=self.root)


    # ══════════════════════════════════════════
    #  TAB 7 — HISTORY (SQLite Database Viewer)
    # ══════════════════════════════════════════

    def _build_history_tab(self, parent):
        self.history_records = []
        self.hist_current_page = 1
        self.hist_total_records = 0
        self.hist_tree = None
        self.hist_scroll_y = None
        self.hist_scroll_x = None

        # ── Bottom bar FIRST ──────────────────
        hist_bar = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=30, pady=14)
        hist_bar.pack(fill="x", side="bottom")

        self.hist_status = tk.Label(hist_bar, text="⬤  Ready",
                                    font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.hist_status.pack(side="left")

        tk.Button(hist_bar, text="🔧 Optimize DB", font=FONT_BTN,
                  bg=BG_INPUT, fg=TEXT_GRAY, relief="flat", cursor="hand2",
                  padx=14, pady=8,
                  command=self._hist_optimize).pack(side="right", padx=(8,0))

        self.hist_export_btn = tk.Button(hist_bar, text="📊 Export Selected",
                                         font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                         relief="flat", cursor="hand2", padx=16, pady=8,
                                         state="disabled",
                                         command=self._export_selected_history)
        self.hist_export_btn.pack(side="right", padx=(8, 0))

        self.hist_delete_btn = tk.Button(hist_bar, text="🗑️ Delete Selected",
                                         font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                                         relief="flat", cursor="hand2", padx=16, pady=8,
                                         state="disabled",
                                         command=self._delete_selected_history)
        self.hist_delete_btn.pack(side="right", padx=(8, 0))

        self.hist_clear_btn = tk.Button(hist_bar, text="🔥 Clear Table",
                                        font=FONT_BTN, bg=RED, fg="white",
                                        relief="flat", cursor="hand2", padx=16, pady=8,
                                        command=self._clear_all_history)
        self.hist_clear_btn.pack(side="right")

        # ── Body ──────────────────────────────
        body = tk.Frame(parent, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=30, pady=20)

        left, right = self._split(body, 320)

        # ── LEFT ──────────────────────────────

        # Table selector
        filter_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        filter_card.pack(fill="x", pady=(0, 16))
        tk.Label(filter_card, text="🗄️  Database Table",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(filter_card, text="Select history type to view",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        self.hist_table_var = tk.StringVar(value="Coordinates Extractor")
        table_menu = ttk.Combobox(filter_card, textvariable=self.hist_table_var,
                                  values=list(TAB_TABLE_CONFIG.keys()),
                                  state="readonly", font=FONT_NORMAL)
        table_menu.pack(fill="x")
        table_menu.bind("<<ComboboxSelected>>", lambda e: self._hist_reset_and_load())

        # Search filter
        search_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        search_card.pack(fill="x", pady=(0, 16))
        tk.Label(search_card, text="🔍  Filter",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(search_card, text="Search by name, place or address",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        self.hist_search_entry = tk.Entry(search_card, font=FONT_NORMAL, bg=BG_INPUT,
                                          fg=TEXT_WHITE, insertbackground=TEXT_WHITE,
                                          relief="flat", bd=0, highlightthickness=1,
                                          highlightbackground=BG_INPUT,
                                          highlightcolor=ACCENT)
        self.hist_search_entry.pack(fill="x", ipady=8, pady=(0, 10))
        self.hist_search_entry.bind("<Return>", lambda e: self._hist_reset_and_load())
        btn_row = tk.Frame(search_card, bg=BG_CARD)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Filter", font=FONT_BTN, bg=ACCENT, fg="white",
                  relief="flat", cursor="hand2", padx=16, pady=7,
                  command=self._hist_reset_and_load).pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="Reset", font=FONT_BTN, bg=BG_INPUT, fg=TEXT_GRAY,
                  relief="flat", cursor="hand2", padx=16, pady=7,
                  command=self._hist_reset_filter).pack(side="left")

        # Pagination
        page_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        page_card.pack(fill="x", pady=(0, 16))
        tk.Label(page_card, text="📄  Pagination",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        tk.Label(page_card, text=f"Showing {PAGE_SIZE} records per page",
                 font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", pady=(2, 10))
        nav_row = tk.Frame(page_card, bg=BG_CARD)
        nav_row.pack(fill="x")
        self.hist_prev_btn = tk.Button(nav_row, text="◀ Prev", font=FONT_BTN,
                                       bg=BG_INPUT, fg=TEXT_GRAY, relief="flat",
                                       cursor="hand2", padx=12, pady=7,
                                       command=self._hist_prev_page)
        self.hist_prev_btn.pack(side="left", padx=(0, 8))
        self.hist_page_label = tk.Label(nav_row, text="Page 1",
                                        font=FONT_SUB, bg=BG_CARD, fg=TEXT_WHITE)
        self.hist_page_label.pack(side="left", expand=True)
        self.hist_next_btn = tk.Button(nav_row, text="Next ▶", font=FONT_BTN,
                                       bg=BG_INPUT, fg=TEXT_GRAY, relief="flat",
                                       cursor="hand2", padx=12, pady=7,
                                       command=self._hist_next_page)
        self.hist_next_btn.pack(side="left")

        # DB Stats
        stats_card = tk.Frame(left, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        stats_card.pack(fill="x")
        tk.Label(stats_card, text="ℹ️  Database Stats",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(anchor="w")
        self.db_info_label = tk.Label(stats_card, text="Loading...",
                                      font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY,
                                      justify="left", anchor="w", wraplength=260)
        self.db_info_label.pack(fill="x", pady=(8, 0))

        # ── RIGHT ─────────────────────────────
        res_card = tk.Frame(right, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1, padx=22, pady=18)
        res_card.pack(fill="both", expand=True)

        res_header = tk.Frame(res_card, bg=BG_CARD)
        res_header.pack(fill="x")
        tk.Label(res_header, text="📋  History Log",
                 font=FONT_LABEL, bg=BG_CARD, fg=TEXT_WHITE).pack(side="left")
        self.hist_count_label = tk.Label(res_header, text="0 records",
                                         font=FONT_SUB, bg=BG_CARD, fg=TEXT_GRAY)
        self.hist_count_label.pack(side="right")

        self.hist_tree_frame = tk.Frame(res_card, bg=BG_CARD)
        self.hist_tree_frame.pack(fill="both", expand=True, pady=(10, 0))

        # Initial load
        self._load_history_data()

    # ── History Actions ───────────────────────

    def _hist_reset_and_load(self):
        self.hist_current_page = 1
        self._load_history_data()

    def _hist_reset_filter(self):
        self.hist_search_entry.delete(0, tk.END)
        self.hist_current_page = 1
        self._load_history_data()

    def _hist_prev_page(self):
        if self.hist_current_page > 1:
            self.hist_current_page -= 1
            self._load_history_data()

    def _hist_next_page(self):
        import math
        total_pages = max(1, math.ceil(self.hist_total_records / PAGE_SIZE))
        if self.hist_current_page < total_pages:
            self.hist_current_page += 1
            self._load_history_data()

    def _load_history_data(self):
        import math

        # Destroy old treeview
        if self.hist_tree:
            self.hist_tree.destroy()
            self.hist_tree = None
        if self.hist_scroll_y:
            self.hist_scroll_y.destroy()
        if self.hist_scroll_x:
            self.hist_scroll_x.destroy()

        table_choice = self.hist_table_var.get()
        search_query = self.hist_search_entry.get().strip()
        page         = self.hist_current_page

        # Recreate scrollbars
        self.hist_scroll_y = tk.Scrollbar(self.hist_tree_frame)
        self.hist_scroll_y.pack(side="right", fill="y")
        self.hist_scroll_x = tk.Scrollbar(self.hist_tree_frame, orient="horizontal")
        self.hist_scroll_x.pack(side="bottom", fill="x")

        config = TAB_TABLE_CONFIG.get(table_choice, TAB_TABLE_CONFIG["Coordinates Extractor"])
        self.hist_current_table_config = config

        try:
            self.hist_total_records = config["count_fn"](search_query)
            self.history_records    = config["query_fn"](search_term=search_query, page=page, page_size=PAGE_SIZE)
            cols      = config["columns"]
            col_widths= config["widths"]
        except Exception as e:
            messagebox.showerror("DB Error", str(e))
            return

        # Build treeview
        self.hist_tree = ttk.Treeview(self.hist_tree_frame, columns=cols,
                                      show="headings",
                                      yscrollcommand=self.hist_scroll_y.set,
                                      xscrollcommand=self.hist_scroll_x.set,
                                      style="Amenity.Treeview")
        for col in cols:
            self.hist_tree.heading(col, text=col)
            self.hist_tree.column(col, width=col_widths.get(col, 100), anchor="w")
        self.hist_tree.pack(fill="both", expand=True)
        self.hist_scroll_y.config(command=self.hist_tree.yview)
        self.hist_scroll_x.config(command=self.hist_tree.xview)

        # Populate rows
        id_col = config["id_col"]
        row_builder = config["row_builder"]
        offset = (page - 1) * PAGE_SIZE
        for i, row in enumerate(self.history_records, offset + 1):
            values = row_builder(i, row)
            iid_val = str(row.get(id_col) or row.get("id") or i)
            self.hist_tree.insert("", "end", iid=iid_val, values=values)

        # Update pagination
        total_pages = max(1, math.ceil(self.hist_total_records / PAGE_SIZE))
        self.hist_page_label.config(
            text=f"Page {page} / {total_pages}")
        self.hist_prev_btn.config(
            state="normal" if page > 1 else "disabled")
        self.hist_next_btn.config(
            state="normal" if page < total_pages else "disabled")

        # Update counts
        showing = len(self.history_records)
        self.hist_count_label.config(
            text=f"{self.hist_total_records:,} total — showing {showing}",
            fg=GREEN if self.hist_total_records > 0 else TEXT_GRAY)

        # Disable action buttons until selection
        self.hist_export_btn.config(state="disabled", bg=BG_INPUT, fg=TEXT_GRAY)
        self.hist_delete_btn.config(state="disabled", bg=BG_INPUT, fg=TEXT_GRAY)

        def on_select(event):
            selected = self.hist_tree.selection()
            if selected:
                self.hist_export_btn.config(state="normal", bg=ACCENT, fg="white")
                self.hist_delete_btn.config(state="normal", bg=RED, fg="white")
            else:
                self.hist_export_btn.config(state="disabled", bg=BG_INPUT, fg=TEXT_GRAY)
                self.hist_delete_btn.config(state="disabled", bg=BG_INPUT, fg=TEXT_GRAY)

        self.hist_tree.bind("<<TreeviewSelect>>", on_select)

        # Update DB stats for all tabs
        try:
            stats = get_db_stats()
            self.db_info_label.config(
                text=(
                    f"📦 DB Size: {stats.get('size', '0 MB')}\n\n"
                    f"📍 Coordinates: {stats.get('coordinates', 0):,}\n"
                    f"🗺️ Boundaries: {stats.get('boundaries', 0):,}\n"
                    f"🏨 Amenities: {stats.get('amenities', 0):,}\n"
                    f"⚡ Bulk Jobs: {stats.get('bulk_jobs', 0):,}\n"
                    f"🌍 Areas: {stats.get('areas', 0):,}\n"
                    f"🛡️ Validated: {stats.get('validated', 0):,}\n"
                    f"🧱 Checkpoints: {stats.get('checkpoints', 0):,}\n"
                    f"────────────────────\n"
                    f"📊 Total Records: {stats.get('total', 0):,}"
                ),
                fg=TEXT_WHITE)
        except Exception:
            pass

    def _delete_selected_history(self):
        if not self.hist_tree:
            return
        selected = self.hist_tree.selection()
        if not selected:
            return
        table_choice = self.hist_table_var.get()
        config = TAB_TABLE_CONFIG.get(table_choice, TAB_TABLE_CONFIG["Coordinates Extractor"])
        confirm = messagebox.askyesno(
            "Confirm Delete",
            f"Delete {len(selected)} selected record(s) from '{table_choice}'?"
        )
        if not confirm:
            return
        delete_records(config["table"], list(selected), id_col=config["id_col"])
        self.hist_status.config(
            text=f"🗑️ Deleted {len(selected)} record(s)", fg=GREEN)
        self._load_history_data()

    def _export_selected_history(self):
        if not self.hist_tree:
            return
        selected = self.hist_tree.selection()
        if not selected:
            return
        table_choice = self.hist_table_var.get()
        config = TAB_TABLE_CONFIG.get(table_choice, TAB_TABLE_CONFIG["Coordinates Extractor"])
        id_col = config["id_col"]
        safe_name = table_choice.lower().replace(" ", "_")
        fp = filedialog.asksaveasfilename(
            initialdir=get_downloads_dir(),
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile=f"{safe_name}_history.xlsx")
        if not fp:
            return
        rows_data = [
            r for r in self.history_records
            if str(r.get(id_col) or r.get("id") or "") in selected
        ]
        if not rows_data:
            return
        try:
            df = pd.DataFrame(rows_data)
            for c in ["id", "geojson", "bbox_json", "vernacular_json"]:
                if c in df.columns:
                    df = df.drop(columns=[c])
            df.columns = [str(c).replace("_", " ").title() for c in df.columns]
            with pd.ExcelWriter(fp, engine="openpyxl") as writer:
                sheet = table_choice[:31]
                df.to_excel(writer, sheet_name=sheet, index=False)
                ws = writer.sheets[sheet]
                for col in ws.columns:
                    max_len = max(len(str(c.value)) if c.value else 0 for c in col)
                    ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)
            self.hist_status.config(
                text=f"✅ Exported {len(rows_data)} rows", fg=GREEN)
            os.startfile(fp)
        except Exception as e:
            messagebox.showerror("Export Error", str(e))

    def _clear_all_history(self):
        table_choice = self.hist_table_var.get()
        config = TAB_TABLE_CONFIG.get(table_choice, TAB_TABLE_CONFIG["Coordinates Extractor"])
        confirm = messagebox.askyesno(
            "Confirm Clear",
            f"⚠️ Delete ALL records in '{table_choice}' ({config['table']})? This cannot be undone."
        )
        if not confirm:
            return
        clear_table(config["table"])
        self.hist_status.config(text="🔥 Table cleared", fg=GREEN)
        self.hist_current_page = 1
        self._load_history_data()

    def _hist_optimize(self):
        self.hist_status.config(text="🔧 Optimizing...", fg=YELLOW)
        def _run():
            optimize_db()
            self.root.after(0, self.hist_status.config,
                            {"text": "✅ Database optimized", "fg": GREEN})
            self.root.after(0, self._load_history_data)
        threading.Thread(target=_run, daemon=True).start()

    # ══════════════════════════════════════════
    #  VOICE INPUT METHODS
    # ══════════════════════════════════════════

    # ── Extractor Tab Voice ───────────────────

    def _ext_press_to_talk(self):
        """Record once and fill the search box."""
        self.ext_mic_btn.config(state="disabled", text="🔴  Recording...")
        self.ext_voice_status.config(text="Speak now...", fg=RED)

        def on_result(text):
            self.root.after(0, self._ext_on_voice_result, text)

        press_to_talk(on_result, duration=5)

    def _ext_on_voice_result(self, text):
        self.ext_mic_btn.config(state="normal", text="🎤  Press to Talk")
        if text:
            self.single_entry.delete(0, tk.END)
            self.single_entry.insert(0, text)
            self.ext_voice_status.config(text=f"✅ {text} — searching...", fg=GREEN)
            # Auto trigger search after voice input
            self.root.after(500, self._search_single_now)
        else:
            self.ext_voice_status.config(text="⚠️  Not recognized", fg=YELLOW)

    def _ext_toggle_wake(self):
        """Start/stop always-listening wake word mode."""
        if not self.wake_active:
            self._start_wake_listener(
                entry_widget=self.single_entry,
                status_label=self.ext_voice_status,
                wake_btn=self.ext_wake_btn,
                auto_search=True   # Auto click Search & Save after voice
            )
        else:
            self._stop_wake_listener(
                status_label=self.ext_voice_status,
                wake_btn=self.ext_wake_btn
            )

    # ── Boundary Tab Voice ────────────────────

    def _bnd_press_to_talk(self):
        """Record once and fill the boundary search box."""
        self.bnd_mic_btn.config(state="disabled", text="🔴  Recording...")
        self.bnd_voice_status.config(text="Speak now...", fg=RED)

        def on_result(text):
            self.root.after(0, self._bnd_on_voice_result, text)

        press_to_talk(on_result, duration=5)

    def _bnd_on_voice_result(self, text):
        self.bnd_mic_btn.config(state="normal", text="🎤  Press to Talk")
        if text:
            self.bnd_entry.delete(0, tk.END)
            self.bnd_entry.insert(0, text)
            self.bnd_voice_status.config(text=f"✅ {text}", fg=GREEN)
        else:
            self.bnd_voice_status.config(text="⚠️  Not recognized", fg=YELLOW)

    def _bnd_toggle_wake(self):
        """Start/stop always-listening wake word mode for boundary tab."""
        if not self.wake_active:
            self._start_wake_listener(
                entry_widget=self.bnd_entry,
                status_label=self.bnd_voice_status,
                wake_btn=self.bnd_wake_btn
            )
        else:
            self._stop_wake_listener(
                status_label=self.bnd_voice_status,
                wake_btn=self.bnd_wake_btn
            )

    # ── Shared Wake Word Logic ────────────────

    def _start_wake_listener(self, entry_widget, status_label, wake_btn, auto_search=False):
        """Start wake word listener — fills entry_widget when query heard."""
        self.wake_active = True
        wake_btn.config(text="🔇  Stop Listening", bg=RED, fg="white")

        def on_query(text):
            if text:
                def _fill():
                    entry_widget.delete(0, tk.END)
                    entry_widget.insert(0, text)
                    status_label.config(text=f"✅ {text} — searching...", fg=GREEN)
                    if auto_search:
                        self.root.after(500, self._search_single_now)
                self.root.after(0, _fill)

        def on_status(text):
            self.root.after(0, status_label.config, {"text": text, "fg": TEXT_GRAY})

        self.wake_listener = WakeWordListener(
            callback=on_query,
            status_callback=on_status
        )
        self.wake_listener.start()
        status_label.config(text="👂 Say 'Jarvis' to activate...", fg=ACCENT)

    def _stop_wake_listener(self, status_label, wake_btn):
        """Stop the wake word listener."""
        self.wake_active = False
        wake_btn.config(text="👂  Start Listening", bg=BG_INPUT, fg=TEXT_GRAY)
        if self.wake_listener:
            self.wake_listener.stop()
            self.wake_listener = None
        status_label.config(text="", fg=TEXT_GRAY)

# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main():
    try:
        init_db()
    except Exception as e:
        pass

    # Ensure desktop shortcut with custom icon is created on any laptop
    try:
        from utils.shortcut_helper import ensure_desktop_shortcut
        ensure_desktop_shortcut()
    except Exception:
        pass

    if "--reset-password" in sys.argv:
        reset_master_password()
        print("[INFO] Master password has been reset. You can now configure a new password.")

    root = tk.Tk()

    # Dark title bar attribute on Windows if available
    try:
        root.update_idletasks()
        from ctypes import byref, c_int, windll
        hwnd = root.winfo_id()
        dark_mode = c_int(1)
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            20,
            byref(dark_mode),
            4,
        )
    except Exception:
        pass

    def _on_authenticated():
        app = JarvisApp(root)
        try:
            from ctypes import byref, c_int, windll
            hwnd = root.winfo_id()
            dark_mode = c_int(1)
            windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                20,
                byref(dark_mode),
                4,
            )
        except Exception:
            pass

    # Display startup security lock screen directly in root
    StartupLockDialog(root, on_success=_on_authenticated)
    root.mainloop()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()