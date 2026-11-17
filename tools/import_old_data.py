import os
import re
import pandas as pd
from datetime import datetime
from utils.db_handler import init_db, save_coordinate_results, save_amenity_results, get_connection

def parse_amenity_filename(filename: str) -> tuple[str, str]:
    """
    Parses amenity type and area from filename.
    Examples:
        amenity_hospital_Delhi_20240115_103045.xlsx -> ('hospital', 'Delhi')
        hospital_Delhi_chunk1.xlsx -> ('hospital', 'Delhi')
    """
    name_no_ext = os.path.splitext(filename)[0]
    
    # Check for prefix 'amenity_'
    if name_no_ext.startswith("amenity_"):
        parts = name_no_ext.split("_")
        # format: amenity_{type}_{area}_{date}_{time}
        if len(parts) >= 4:
            amenity = parts[1]
            area = parts[2]
            return amenity, area
            
    # Check for bulk runner chunk format: {amenity}_{area}_chunk{n}
    if "_chunk" in name_no_ext:
        parts = name_no_ext.split("_chunk")[0].split("_")
        if len(parts) >= 2:
            return parts[0], "_".join(parts[1:])
            
    return "unknown", "unknown"

def import_old_records():
    print("=== JARVIS MAPS DATABASE MIGRATION TOOL ===")
    init_db()
    
    # Load all existing records to prevent duplicate insertions (bypass pagination limit)
    print("\nReading existing database records to avoid duplicates...")
    with get_connection() as conn:
        coord_rows = conn.execute("SELECT place, latitude, longitude FROM coordinate_results").fetchall()
        amenity_rows = conn.execute("SELECT name, latitude, longitude FROM amenity_results").fetchall()

    existing_coords = {
        (str(r["place"]).strip().lower(), round(float(r["latitude"]), 5) if r["latitude"] is not None else None, round(float(r["longitude"]), 5) if r["longitude"] is not None else None)
        for r in coord_rows if r["place"]
    }
    existing_amenities = {
        (str(r["name"]).strip().lower(), round(float(r["latitude"]), 5) if r["latitude"] is not None else None, round(float(r["longitude"]), 5) if r["longitude"] is not None else None)
        for r in amenity_rows if r["name"]
    }
    
    imported_coords_count = 0
    imported_amenities_count = 0
    
    # 1. Check data.xlsx in root
    root_files = ["data.xlsx"]
    for rf in root_files:
        if os.path.exists(rf):
            print(f"\nScanning root file: {rf}")
            try:
                df = pd.read_excel(rf, engine="openpyxl")
                df.columns = [c.strip().lower() for c in df.columns]
                
                coords_to_insert = []
                for _, row in df.iterrows():
                    place = str(row.get("place", row.get("location", ""))).strip()
                    if not place or place.lower() == "nan":
                        continue
                        
                    lat = row.get("latitude")
                    lng = row.get("longitude")
                    addr = str(row.get("address", "N/A")).strip()
                    status = str(row.get("status", "Success")).strip()
                    ts = str(row.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).strip()
                    
                    # Convert pandas float types if needed
                    try:
                        lat_val = float(lat) if pd.notna(lat) else None
                        lng_val = float(lng) if pd.notna(lng) else None
                    except ValueError:
                        lat_val, lng_val = None, None
                        
                    key = (place.lower(), round(lat_val, 5) if lat_val else None, round(lng_val, 5) if lng_val else None)
                    if key not in existing_coords:
                        coords_to_insert.append({
                            "place": place,
                            "latitude": lat_val,
                            "longitude": lng_val,
                            "address": addr,
                            "status": status,
                            "timestamp": ts
                        })
                        existing_coords.add(key)
                        
                if coords_to_insert:
                    save_coordinate_results(coords_to_insert)
                    imported_coords_count += len(coords_to_insert)
                    print(f"  -> Imported {len(coords_to_insert)} coordinates from {rf}")
                else:
                    print("  -> No new coordinates found in this file.")
            except Exception as e:
                print(f"  ❌ Error reading {rf}: {e}")

    # Helper function to scan a directory
    def scan_directory(dir_path):
        nonlocal imported_coords_count, imported_amenities_count
        if not os.path.exists(dir_path):
            return
            
        print(f"\nScanning directory: {dir_path}")
        for file in os.listdir(dir_path):
            if not file.endswith(".xlsx"):
                continue
                
            filepath = os.path.join(dir_path, file)
            filename = os.path.basename(filepath)
            
            # Determine if coordinate file or amenity file
            is_amenity = False
            if filename.startswith("amenity_") or "_chunk" in filename or "bulk_" in filename:
                is_amenity = True
                
            try:
                df = pd.read_excel(filepath, engine="openpyxl")
                df.columns = [c.strip().lower() for c in df.columns]
                
                # Check actual columns if names are ambiguous
                if "amenity" in df.columns or "phone" in df.columns or "rating" in df.columns:
                    is_amenity = True
                    
                if is_amenity:
                    # Parse amenity/area details from filename
                    fname_amenity, fname_area = parse_amenity_filename(filename)
                    
                    amenities_to_insert = []
                    for _, row in df.iterrows():
                        name = str(row.get("name", "")).strip()
                        if not name or name.lower() == "nan":
                            continue
                            
                        # Extract row values
                        amenity = str(row.get("amenity", fname_amenity)).strip()
                        area = str(row.get("area", fname_area)).strip()
                        lat = row.get("latitude")
                        lng = row.get("longitude")
                        addr = str(row.get("address", "N/A")).strip()
                        rating = str(row.get("rating", "N/A")).strip()
                        phone = str(row.get("phone", "N/A")).strip()
                        hours = str(row.get("opening hours", row.get("opening_hours", "N/A"))).strip()
                        ts = str(row.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).strip()
                        
                        try:
                            lat_val = float(lat) if pd.notna(lat) else None
                            lng_val = float(lng) if pd.notna(lng) else None
                        except ValueError:
                            lat_val, lng_val = None, None
                            
                        key = (name.lower(), round(lat_val, 5) if lat_val else None, round(lng_val, 5) if lng_val else None)
                        if key not in existing_amenities:
                            amenities_to_insert.append({
                                "name": name,
                                "amenity": amenity,
                                "area": area,
                                "latitude": lat_val,
                                "longitude": lng_val,
                                "address": addr,
                                "rating": rating,
                                "phone": phone,
                                "opening_hours": hours,
                                "timestamp": ts
                            })
                            existing_amenities.add(key)
                            
                    if amenities_to_insert:
                        save_amenity_results(amenities_to_insert)
                        imported_amenities_count += len(amenities_to_insert)
                        print(f"  -> Imported {len(amenities_to_insert)} amenities from {filename}")
                else:
                    # Coordinates file
                    coords_to_insert = []
                    for _, row in df.iterrows():
                        place = str(row.get("place", row.get("location", ""))).strip()
                        if not place or place.lower() == "nan":
                            continue
                            
                        lat = row.get("latitude")
                        lng = row.get("longitude")
                        addr = str(row.get("address", "N/A")).strip()
                        status = str(row.get("status", "Success")).strip()
                        ts = str(row.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).strip()
                        
                        try:
                            lat_val = float(lat) if pd.notna(lat) else None
                            lng_val = float(lng) if pd.notna(lng) else None
                        except ValueError:
                            lat_val, lng_val = None, None
                            
                        key = (place.lower(), round(lat_val, 5) if lat_val else None, round(lng_val, 5) if lng_val else None)
                        if key not in existing_coords:
                            coords_to_insert.append({
                                "place": place,
                                "latitude": lat_val,
                                "longitude": lng_val,
                                "address": addr,
                                "status": status,
                                "timestamp": ts
                            })
                            existing_coords.add(key)
                            
                    if coords_to_insert:
                        save_coordinate_results(coords_to_insert)
                        imported_coords_count += len(coords_to_insert)
                        print(f"  -> Imported {len(coords_to_insert)} coordinates from {filename}")
            except Exception as e:
                print(f"  ❌ Error reading {filename}: {e}")

    scan_directory("excel_outputs")
    scan_directory("bulk_output")
    
    print("\n=== MIGRATION COMPLETED ===")
    print(f"Total new Coordinate records imported: {imported_coords_count}")
    print(f"Total new Amenity records imported: {imported_amenities_count}")
    print("===========================")

if __name__ == "__main__":
    import_old_records()
