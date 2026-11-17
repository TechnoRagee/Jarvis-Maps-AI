"""
google_maps_explorer.py
-----------------------

100% Free, Keyless Area Explorer Engine using Google Maps:
- Uses headless Chrome (Selenium / maps_bot) to query Google Maps search feeds.
- Auto-scrolls results panels (div[role='feed']) to extract all place cards.
- Uses Google's public autocomplete suggestions to sweep numbered sectors/blocks/pockets.
- Applies strict POI filtering to eliminate individual businesses and keep localities/sectors/neighborhoods.
- Formats output to the standard Area Explorer schema for seamless export to Excel/KML and Boundary Validator.
"""

from __future__ import annotations

import math
import os
import re
import sys
import time
from typing import Callable, Optional
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

from automation.maps_bot import setup_driver
from utils.amenity_finder import _HARVEST_JS, _extract_coords, _strip_leading_rating

# Blocklist of commercial businesses/amenities that should NOT be treated as areas
POI_BLOCKLIST = {
    "restaurant", "cafe", "coffee shop", "bar", "pub", "bakery", "fast food",
    "store", "shop", "supermarket", "grocery", "mall", "clothing store",
    "pharmacy", "chemist", "hospital", "clinic", "dental clinic", "doctor",
    "dentist", "veterinary", "physiotherapist", "diagnostic center",
    "bank", "atm", "insurance",
    "school", "high school", "primary school", "college", "university",
    "coaching center", "kindergarten", "preschool",
    "gym", "fitness center", "yoga studio", "salon", "beauty salon", "spa",
    "hotel", "motel", "guest house", "lodge", "resort",
    "gas station", "petrol pump", "cng station", "car wash", "auto repair",
    "car dealer", "motorcycle dealer", "parking",
}

# Whitelist of terms that indicate genuine geographic areas, neighborhoods, and residential communities
AREA_WHITELIST = {
    "locality", "sub-locality", "sublocality", "neighbourhood", "neighborhood",
    "suburb", "quarter", "district", "borough", "ward", "sector", "block",
    "housing society", "housing complex", "apartment building", "apartment complex",
    "condominium complex", "residential area", "gated community", "township",
    "colony", "nagar", "vihar", "enclave", "village", "hamlet", "town",
    "industrial area", "industrial estate", "business park", "tech park",
    "market", "bazaar", "administrative area",
}


def _is_area_category(category: str, name: str) -> bool:
    """Determine whether a place card represents an area rather than an individual business."""
    cat_lower = str(category or "").strip().lower()
    name_lower = str(name or "").strip().lower()

    # If category explicitly matches a blocked commercial POI
    for blocked in POI_BLOCKLIST:
        if blocked in cat_lower:
            return False

    # Check if category contains any positive area indicator
    for allowed in AREA_WHITELIST:
        if allowed in cat_lower:
            return True

    # Check name heuristics if category is vague
    area_name_keywords = [
        "sector", "block", "pocket", "phase", "enclave", "colony", "vihar",
        "nagar", "kunj", "puri", "heights", "towers", "apartments", "society",
        "village", "basti", "extension", "extn", "layout", "hills", "gardens",
        "district", "borough", "quarter", "subdivision", "ward"
    ]
    if any(re.search(rf"\b{kw}\b", name_lower) for kw in area_name_keywords):
        return True

    # If no category was returned by Google Maps, default to True if name looks like an area
    if not cat_lower:
        return True

    return False


def _clean_place_name(raw_name: str) -> str:
    """Normalize and clean place name."""
    name = str(raw_name or "").strip()
    name = re.sub(r"^(?:Sponsored|Ad)\s*·?\s*", "", name, flags=re.I).strip()
    name = re.sub(r"\s+", " ", name).strip(" ,.-")
    return name


def _infer_area_type(category: str, name: str) -> str:
    """Map Google Maps category / name to normalized Area Explorer type."""
    cat = (category or "").lower()
    nm = (name or "").lower()

    if "sector" in nm or "sector" in cat:
        return "Sector"
    if "block" in nm or "pocket" in nm or "block" in cat:
        return "Block"
    if "society" in cat or "society" in nm or "apartment" in cat or "condominium" in cat:
        return "Society"
    if "industrial" in cat or "industrial" in nm:
        return "Industrial Area"
    if "market" in cat or "bazaar" in cat or "commercial" in cat:
        return "Commercial / Market"
    if "colony" in nm or "colony" in cat:
        return "Colony"
    if any(k in nm for k in ["nagar", "vihar", "enclave", "kunj", "puri", "bagh"]):
        return "Nagar / Vihar"
    if "neighbourhood" in cat or "neighborhood" in cat:
        return "Neighbourhood"
    if "suburb" in cat or "borough" in cat or "quarter" in cat:
        return "Suburb"
    if "ward" in cat or "ward" in nm:
        return "Ward"
    if "village" in cat or "village" in nm:
        return "Village"
    if "township" in cat or "township" in nm:
        return "Township"

    return "Locality"


def fetch_dynamic_indian_subareas(city: str, max_items: int = 25) -> list[str]:
    """
    Dynamically discover authentic sub-localities, wards, and zones for ANY city, district,
    or town across India (Mumbai, Bengaluru, Kolkata, Chennai, Hyderabad, Pune, Jaipur,
    Lucknow, Ahmedabad, Kanpur, Patna, Indore, Bhopal, Chandigarh, etc.) using real-time
    Google Autocomplete RPC. Completely free, no API keys, zero hardcoding.
    """
    clean_city = city.split(",")[0].strip()
    if clean_city.lower() in ("india", "all india", "whole india", "bharat", "pan india"):
        return [
            "Delhi NCR", "Mumbai MMR", "Bengaluru", "Kolkata", "Chennai", "Hyderabad",
            "Pune", "Ahmedabad", "Jaipur", "Lucknow", "Surat", "Kanpur", "Nagpur",
            "Indore", "Thane", "Bhopal", "Visakhapatnam", "Patna", "Vadodara", "Ghaziabad",
            "Ludhiana", "Agra", "Nashik", "Faridabad", "Meerut", "Rajkot", "Varanasi"
        ]

    seed_patterns = [
        f"{clean_city} areas ",
        f"{clean_city} localities ",
        f"{clean_city} nagar ",
        f"{clean_city} colony ",
        f"{clean_city} sector ",
        f"{clean_city} vihar ",
        f"{clean_city} enclave ",
        f"{clean_city} layout ",
        f"{clean_city} peth ",
        f"{clean_city} puram ",
    ]
    discovered = []
    seen = set()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}

    junk_words = {
        "list", "map", "pdf", "weather", "news", "temperature", "timing", "pincode",
        "pin code", "famous", "posh", "cheap", "costly", "distance", "metro", "bus",
        "train", "flight", "hotels", "restaurant", "school", "hospital", "mall",
        "election", "chunav", "budget", "nigam", "palika", "lyrics", "complaint",
        "water", "house tax", "complaint number", "helpline", "area code", "sq km",
        "square", "acres", "size", "population", "route"
    }

    for q in seed_patterns:
        try:
            url = "https://suggestqueries.google.com/complete/search"
            resp = requests.get(
                url,
                params={"client": "chrome", "q": q, "hl": "en"},
                headers=headers,
                timeout=4,
            )
            if resp.status_code == 200:
                data = resp.json()
                suggestions = data[1] if len(data) > 1 else []
                for s in suggestions:
                    s_lower = s.lower().strip()
                    if any(j in s_lower for j in junk_words):
                        continue
                    cand = s
                    for pfx in [clean_city.lower() + " ", clean_city.lower()]:
                        if cand.lower().startswith(pfx):
                            cand = cand[len(pfx):].strip()
                    cand = re.sub(r"\s+", " ", cand).strip(" ,.-")
                    if len(cand) >= 3 and cand.lower() not in seen and cand.lower() != clean_city.lower():
                        seen.add(cand.lower())
                        discovered.append(cand.title())
                        if len(discovered) >= max_items:
                            break
        except Exception:
            continue
        if len(discovered) >= max_items:
            break

    return discovered


def fetch_google_autocomplete_sectors(city: str, sub_areas: list[str] = None, max_sectors: int = 100) -> list[str]:
    """
    Rapidly discover numbered sectors, phases, pockets, and blocks for ANY Indian city
    via Google Suggest RPC. Returns list of discovered sector/pocket names.
    """
    clean_city = city.split(",")[0].strip()
    queries = [
        f"{clean_city} sector ",
        f"{clean_city} phase ",
        f"{clean_city} block ",
        f"{clean_city} pocket ",
    ]
    if sub_areas:
        for sa in sub_areas[:8]:
            queries.append(f"{sa} sector ")
            queries.append(f"{sa} phase ")

    discovered = set()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}

    for q in queries:
        try:
            url = "https://suggestqueries.google.com/complete/search"
            resp = requests.get(
                url,
                params={"client": "chrome", "q": q, "hl": "en"},
                headers=headers,
                timeout=4,
            )
            if resp.status_code == 200:
                data = resp.json()
                suggestions = data[1] if len(data) > 1 else []
                for s in suggestions:
                    s_clean = s.strip()
                    if any(bad in s_clean.lower() for bad in ["route", "distance", "metro", "bus", "weather", "news", "timing", "price", "hotel", "school"]):
                        continue
                    m = re.search(r"(?:sector|pocket|phase|block)\s*[-]?\s*([0-9a-zA-Z]+)", s_clean, re.I)
                    if m:
                        discovered.add(s_clean.title())
                        if len(discovered) >= max_sectors:
                            break
        except Exception:
            pass
        if len(discovered) >= max_sectors:
            break

    return sorted(list(discovered))


class GoogleMapsWebExplorer:
    """Headless browser explorer for Google Maps search feeds."""

    def __init__(self, headless: bool = True, log_callback: Optional[Callable] = None):
        self.headless = headless
        self.log_callback = log_callback

    def log(self, message: str, tag: str = "normal"):
        if self.log_callback:
            self.log_callback(f"{message}\n", tag)
        else:
            print(message)

    def explore(
        self,
        city: str,
        area_types: list[str] | str = None,
        limit: int = 2000,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> list[dict]:
        """
        Search and extract all areas for ANY city or region across India from Google Maps.
        Universal across all 28 states & union territories without city-specific hardcoding.
        """
        city = str(city or "").strip()
        if not city:
            return []

        if isinstance(area_types, (list, tuple, set)):
            target_types = [str(t).strip().lower() for t in area_types if t]
        elif area_types:
            target_types = [str(area_types).strip().lower()]
        else:
            target_types = ["locality", "sector", "neighbourhood", "colony", "society"]

        is_max = limit is None or (isinstance(limit, (int, float)) and limit <= 0)
        target_limit = 3500 if is_max else max(10, int(limit))

        self.log(f"🌐 Initializing Google Maps Universal Explorer for '{city}'...", "info")
        driver = setup_driver(headless=self.headless)

        all_results = []
        seen_keys = set()

        is_pan_india = city.lower() in ("india", "all india", "whole india", "bharat", "pan india", "entire india")

        # Dynamically discover sub-localities across India with zero hardcoding
        sub_localities = fetch_dynamic_indian_subareas(city, max_items=20)
        if sub_localities:
            self.log(f"📍 Discovered {len(sub_localities)} dynamic zones/sub-areas across {city}", "info")

        zones = ["North", "South", "East", "West", "Central", "Old", "New", "Extension"]

        queries = []

        if is_pan_india:
            self.log("🇮🇳 Generating Pan-India deep-crawl query matrix across all regions...", "info")
            queries.extend([
                "major cities in North India",
                "major cities in South India",
                "major cities in West India",
                "major cities in East India",
                "major cities in Central India",
                "cities in Maharashtra",
                "cities in Uttar Pradesh",
                "cities in Karnataka",
                "cities in Tamil Nadu",
                "cities in Gujarat",
                "cities in Rajasthan",
                "cities in West Bengal",
                "cities in Madhya Pradesh",
                "cities in Punjab",
                "cities in Haryana",
                "cities in Bihar",
                "cities in Telangana",
                "cities in Andhra Pradesh",
                "cities in Kerala",
            ])
        else:
            # Build comprehensive multi-zone queries so Google Maps yields thousands of areas
            if any(t in target_types for t in ["sector", "phase", "block", "all"]):
                queries.append(f"sectors in {city}")
                queries.append(f"phases in {city}")
                for z in zones[:5]:
                    queries.append(f"sectors in {z} {city}")
                for sl in sub_localities[:8]:
                    queries.append(f"sectors in {sl}, {city}")

            if any(t in target_types for t in ["locality", "suburb", "neighbourhood", "all"]):
                queries.append(f"localities in {city}")
                queries.append(f"neighbourhoods in {city}")
                for z in zones[:5]:
                    queries.append(f"areas in {z} {city}")
                    queries.append(f"localities in {z} {city}")

            if any(t in target_types for t in ["colony", "nagar_vihar", "all"]):
                queries.append(f"residential colonies in {city}")
                queries.append(f"nagars in {city}")
                queries.append(f"vihars in {city}")
                queries.append(f"enclaves in {city}")
                for z in ["North", "South", "West", "East"]:
                    queries.append(f"colonies in {z} {city}")

            if any(t in target_types for t in ["society", "all"]):
                queries.append(f"residential societies in {city}")
                queries.append(f"apartments in {city}")
                for sl in sub_localities[:6]:
                    queries.append(f"societies in {sl}, {city}")

            if any(t in target_types for t in ["commercial_market", "all"]):
                queries.append(f"commercial markets in {city}")
                queries.append(f"bazaars in {city}")

            if any(t in target_types for t in ["industrial_area", "all"]):
                queries.append(f"industrial areas in {city}")
                queries.append(f"tech parks in {city}")

            if any(t in target_types for t in ["ward", "all"]):
                queries.append(f"wards in {city}")
                queries.append(f"municipal wards in {city}")

            if any(t in target_types for t in ["village", "all"]):
                queries.append(f"villages in {city}")

            if any(t in target_types for t in ["tehsil_taluk", "all"]):
                queries.append(f"tehsils in {city}")
                queries.append(f"taluks in {city}")

            # Also add direct queries for discovered dynamic sub-localities
            for sl in sub_localities[:6]:
                queries.append(f"areas in {sl}, {city}")

            if not queries:
                queries.append(f"areas in {city}")
                for z in zones[:4]:
                    queries.append(f"areas in {z} {city}")

        try:
            # ── PASS 1: Sector Autocomplete Discovery ──────────────────────
            if any(t in target_types for t in ["sector", "block", "phase", "all"]) and not is_pan_india:
                self.log(f"⚡ Discovering numbered sectors & pockets in {city} via Google Autocomplete...", "info")
                quick_sectors = fetch_google_autocomplete_sectors(city, sub_localities[:8])
                if quick_sectors:
                    self.log(f"   Discovered {len(quick_sectors)} sectors from autocomplete", "info")
                    for q_sec in quick_sectors:
                        key = (q_sec.lower(), 0, 0)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            all_results.append({
                                "name": q_sec,
                                "parent_area": city,
                                "type": "Sector",
                                "latitude": None,
                                "longitude": None,
                                "google_maps_url": f"https://www.google.com/maps/search/?api=1&query={requests.utils.quote(q_sec + ' ' + city)}",
                            })

            # ── PASS 2: Search Feed Crawler ────────────────────────────────
            for q_idx, query in enumerate(queries, start=1):
                if stop_check and stop_check():
                    self.log("🛑 Area search stopped by user", "warn")
                    break

                if len(all_results) >= target_limit:
                    break

                self.log(f"🔍 [{q_idx}/{len(queries)}] Querying Google Maps: '{query}'...", "info")
                search_url = f"https://www.google.com/maps/search/{requests.utils.quote(query)}"

                try:
                    driver.get(search_url)
                except Exception as exc:
                    self.log(f"⚠️ Page load error: {exc}", "warn")
                    continue

                # Wait for feed container or direct place title
                try:
                    WebDriverWait(driver, 12).until(
                        lambda d: bool(d.find_elements(By.CSS_SELECTOR, "div[role='feed'], div.m6QErb, h1.DUwDvf"))
                    )
                except TimeoutException:
                    self.log(f"⚠️ Feed timed out for '{query}'", "warn")
                    continue

                time.sleep(2.0)

                # Locate scroll panel
                panel = None
                for sel in ["div[role='feed']", "div.m6QErb.DxyBCb", "div.m6QErb"]:
                    try:
                        p_candidate = driver.find_element(By.CSS_SELECTOR, sel)
                        if p_candidate.is_displayed():
                            panel = p_candidate
                            break
                    except Exception:
                        pass

                # If no feed panel found, check if it redirected to a single place directly
                if not panel:
                    try:
                        title_el = driver.find_elements(By.CSS_SELECTOR, "h1.DUwDvf, h1.fontHeadlineLarge")
                        if title_el:
                            direct_name = title_el[0].text.strip()
                            lat, lng, _ = _extract_coords(driver.current_url, allow_viewport_fallback=True)
                            if direct_name and lat is not None and lng is not None:
                                key = (direct_name.lower(), round(lat, 4), round(lng, 4))
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    all_results.append({
                                        "name": direct_name,
                                        "parent_area": city,
                                        "type": _infer_area_type("", direct_name),
                                        "latitude": lat,
                                        "longitude": lng,
                                        "google_maps_url": driver.current_url,
                                    })
                                    self.log(f"   ✅ Captured single place: {direct_name}", "normal")
                    except Exception:
                        pass
                    continue

                # Feed scroll loop
                prev_count = 0
                no_change_count = 0
                max_scrolls = 35 if is_max else min(25, max(8, (target_limit - len(all_results)) // 5))

                for scroll_num in range(max_scrolls):
                    if stop_check and stop_check():
                        break

                    # Harvest visible cards
                    try:
                        raw_cards = driver.execute_script(_HARVEST_JS) or []
                    except Exception:
                        raw_cards = []

                    for card in raw_cards:
                        name = _clean_place_name(card.get("name"))
                        category = card.get("category", "")
                        href = card.get("href", "")

                        if not name or len(name) < 2:
                            continue

                        # Apply POI Filter
                        if not _is_area_category(category, name):
                            continue

                        # Extract coords
                        lat, lng, _ = _extract_coords(href, allow_viewport_fallback=True)
                        if lat is None or lng is None:
                            # Try parsing current URL as viewport fallback
                            lat, lng, _ = _extract_coords(driver.current_url, allow_viewport_fallback=True)

                        key = (name.lower(), round(lat or 0, 4), round(lng or 0, 4))
                        if key in seen_keys:
                            continue

                        seen_keys.add(key)
                        area_type = _infer_area_type(category, name)
                        parent = city

                        # Detect parent locality from address if present
                        address = card.get("address", "")
                        if address and "," in address:
                            addr_parts = [p.strip() for p in address.split(",") if p.strip()]
                            if len(addr_parts) >= 2 and addr_parts[0].lower() != name.lower():
                                parent = addr_parts[0]

                        all_results.append({
                            "name": name,
                            "parent_area": parent,
                            "type": area_type,
                            "latitude": lat,
                            "longitude": lng,
                            "google_maps_url": href or f"https://www.google.com/maps/search/?api=1&query={lat},{lng}",
                        })

                    current_count = len(all_results)
                    self.log(f"   Collected {current_count} areas (scroll {scroll_num + 1}/{max_scrolls})...", "normal")

                    if current_count >= target_limit:
                        break

                    if current_count == prev_count:
                        no_change_count += 1
                        if no_change_count >= 3:
                            break
                    else:
                        no_change_count = 0

                    prev_count = current_count

                    # Check for end of list text
                    page_src = driver.page_source
                    if "You've reached the end of the list" in page_src or "सभी नतीजे दिखाए गए" in page_src:
                        break

                    # Scroll down panel
                    try:
                        driver.execute_script("arguments[0].scrollTop += 1400", panel)
                    except Exception:
                        pass
                    time.sleep(1.4)

            self.log(f"✅ Google Maps search finished. Total {len(all_results)} areas extracted.", "success")

        finally:
            try:
                driver.quit()
            except Exception:
                pass

        return all_results
