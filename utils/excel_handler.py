"""
excel_handler.py - Fixed version (handles empty/missing data.xlsx)
"""

import os
import pandas as pd
from datetime import datetime
from utils.db_handler import save_coordinate_results


OUTPUT_FILE       = "data.xlsx"       # default fallback
BATCH_INPUT_FILE  = "input.xlsx"
SHEET_NAME        = "MapData"
COLUMNS           = ["Place", "Latitude", "Longitude", "Address", "Status", "Timestamp"]

def get_user_downloads_dir() -> str:
    """Returns the user's standard Downloads folder on Windows/macOS/Linux."""
    import sys
    if sys.platform == "win32":
        try:
            import winreg
            sub_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                val, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
                expanded = os.path.expandvars(val)
                if os.path.isdir(expanded):
                    return expanded
        except Exception:
            pass
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    return downloads if os.path.isdir(downloads) else os.path.expanduser("~")

OUTPUT_DIR = get_user_downloads_dir()


def get_output_filename(prefix: str = "coordinates") -> str:
    """
    Generates a timestamped filename saved directly into the user's Downloads folder.
    Examples:
        C:/Users/<User>/Downloads/coordinates_20240115_103045.xlsx
        C:/Users/<User>/Downloads/amenity_hospital_Delhi_20240115_103045.xlsx
    """
    out_dir = get_user_downloads_dir()
    os.makedirs(out_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in prefix)
    return os.path.join(out_dir, f"{safe}_{timestamp}.xlsx")


def save_to_excel(results: list, filepath: str = OUTPUT_FILE) -> None:
    if not results:
        print("⚠️  No results to save.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    processed_results = [
        {**r, "Timestamp": r.get("Timestamp") or timestamp}
        for r in results
    ]

    # Save to SQLite Database
    try:
        save_coordinate_results(processed_results)
    except Exception as db_err:
        print(f"⚠️  Failed to save to SQLite database: {db_err}")

    new_df = pd.DataFrame(processed_results)
    new_df = new_df.rename(columns={
        "place":     "Place",
        "latitude":  "Latitude",
        "longitude": "Longitude",
        "address":   "Address",
        "status":    "Status",
    })
    new_df = new_df.reindex(columns=COLUMNS)

    # FIX: Only read existing file if it exists AND has valid content (size > 0)
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        try:
            existing_df = pd.read_excel(filepath, sheet_name=SHEET_NAME, engine="openpyxl")
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)
            print(f"📎 Appending {len(results)} row(s) to existing file: {filepath}")
        except Exception:
            # File is corrupted or invalid — start fresh
            combined_df = new_df
            print(f"📄 Creating new output file: {filepath}")
    else:
        combined_df = new_df
        print(f"📄 Creating new output file: {filepath}")

    with pd.ExcelWriter(filepath, engine="openpyxl", mode="w") as writer:
        combined_df.to_excel(writer, sheet_name=SHEET_NAME, index=False)
        worksheet = writer.sheets[SHEET_NAME]
        for col in worksheet.columns:
            max_len = max(
                len(str(cell.value)) if cell.value else 0
                for cell in col
            )
            worksheet.column_dimensions[col[0].column_letter].width = max_len + 4

    print(f"✅ Data saved to: {filepath}  ({len(combined_df)} total rows)")

def read_locations_from_excel(filepath: str = BATCH_INPUT_FILE) -> list:
    if not os.path.exists(filepath):
        print(f"❌ Input file not found: {filepath}")
        return []

    try:
        df = pd.read_excel(filepath, engine="openpyxl")

        location_col = None
        for col in df.columns:
            if col.strip().lower() == "location":
                location_col = col
                break

        if location_col is None:
            print(f"❌ No 'Location' column found in {filepath}")
            print(f"   Found columns: {list(df.columns)}")
            return []

        locations = df[location_col].dropna().astype(str).str.strip().tolist()
        locations = [loc for loc in locations if loc]

        print(f"📋 Loaded {len(locations)} location(s) from {filepath}")
        return locations

    except Exception as e:
        print(f"❌ Error reading {filepath}: {e}")
        return []


def create_sample_input_file(filepath: str = BATCH_INPUT_FILE) -> None:
    sample_data = {
        "Location": [
            "SBI Bank Sector 18 Noida",
            "Apollo Hospital Delhi",
            "HDFC Bank Connaught Place Delhi",
            "Indira Gandhi International Airport Delhi",
            "India Gate New Delhi",
        ]
    }
    df = pd.DataFrame(sample_data)

    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Locations", index=False)
        ws = writer.sheets["Locations"]
        ws.column_dimensions["A"].width = 45

    print(f"✅ Sample input file created: {filepath}")
    print(f"   Edit it to add your own locations, then run batch mode.")
