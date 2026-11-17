"""
amenity_finder.py
-----------------
Searches Google Maps for amenities using Selenium.
Extracts: name, lat, lng, address, rating, phone, opening hours.
Falls back to Overpass API if Selenium fails.
"""
from urllib.parse import quote_plus
from pathlib import Path
from typing import Any, Callable, Optional, Union
import difflib
import math
import os
import re
import time
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


OVERPASS_URL  = "https://overpass-api.de/api/interpreter"
# The main endpoint returns 504 under load often enough that a single
# attempt is not a fallback at all.
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS       = {"User-Agent": "JarvisMapsAutomation/1.0"}
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "in").strip().lower()


# ─────────────────────────────────────────────
#  AMENITY CATEGORIES
# ─────────────────────────────────────────────

AMENITY_CATEGORIES = {
    "🏥 Healthcare & Diagnostics": [
        "hospital", "clinic", "dentist", "doctor", "veterinary",
        "nursing_home", "blood_bank", "pharmacy", "chemist",
        "diabetologist", "ent_specialist", "cardiologist", "dermatologist",
        "gastroenterologist", "general_practitioner", "medical_specialist",
        "dental_clinic", "eye_care", "medical_center", "medical_lab",
        "holistic_medicine_practitioner", "childrens_hospital", "pediatrician",
        "animal_hospital", "veterinary_clinic", "herbal_medicine_store",
        "ayurvedic_pharmacy", "homeopathic_pharmacy",
        "diagnostic_center", "radiology_center", "physiotherapy_clinic",
        "orthopedic_clinic", "dialysis_center", "maternity_hospital",
        "psychiatry_clinic", "optical_store",
    ],
    "🏫 Education & Coaching": [
        "school", "college", "university", "kindergarten", "preschool",
        "library", "training", "driving_school", "music_school",
        "educational_institution", "beauty_school", "dance_school",
        "learning_center", "institute_of_technology",
        "institute_of_management", "institute_of_science",
        "coaching_center", "tuition_center", "polytechnic",
        "vocational_training_institute", "daycare", "boarding_school",
        "language_school",
    ],
    "🏦 Finance & Banking": [
        "bank", "atm", "bureau_de_change", "money_transfer",
        "insurance", "microfinance",
    ],
    "🚨 Emergency Services": [
        "police", "fire_station", "ambulance_station",
        "ranger_station", "rescue_station",
    ],
    "🍽️ Food & Dining": [
        "restaurant", "cafe", "fast_food", "food_court",
        "bar", "pub", "ice_cream", "juice_bar", "sweet_shop",
        "dhaba", "cloud_kitchen", "coffee_shop", "bistro",
        "microbrewery", "rooftop_restaurant", "catering_service",
    ],
    "⚡ Transport & Mobility": [
        "fuel", "parking", "bus_station", "taxi",
        "car_wash", "car_rental", "charging_station",
        "metro_station", "railway_station", "bus_stop",
        "ev_charging_station", "cng_station", "car_repair",
        "auto_garage", "tyre_shop", "auto_stand", "bike_rental", "toll_plaza",
    ],
    "🚗 Automobile": [
        "spare_parts_dealer",
        "automobile_shop",
        "auto_parts_store",
        "car_dealership",
        "two_wheeler_dealer",
        "used_car_dealer",
        "car_accessories_shop",
        "car_repair",
        "auto_garage",
        "tyre_shop",
        "car_wash",
        "car_detailing_studio",
        "car_battery_store",
        "auto_electrical_shop",
        "motorcycle_repair",
        "cng_fitment_center",
    ],
    "🏛️ Government & Civic Services": [
        "courthouse", "post_office", "townhall", "embassy",
        "prison", "government", "polling_station",
        "passport_office", "rto_office", "sub_registrar_office",
        "municipal_corporation", "public_service_center",
        "electricity_bill_office", "lpg_gas_agency",
    ],
    "🛕 Religion & Worship": [
        "place_of_worship", "temple", "mosque", "church",
        "gurudwara", "monastery", "ashram",
    ],
    "🌳 Parks & Recreation": [
        "park", "stadium", "cinema", "theatre", "gym",
        "swimming_pool", "sports_centre", "arts_centre",
    ],
    "🛒 Shopping & Retail": [
        "marketplace", "supermarket", "mall",
        "department_store", "convenience_store", "bakery", "electronics",
        "mobile_phone", "clothes", "jewelry", "furniture", "books", "hardware", "gift",
        "stationery_shop", "footwear_shop", "dairy_booth", "pet_shop",
        "meat_shop", "tailor_and_boutique", "auto_parts_store", "florist", "electrical_store",
    ],
    "🏨 Accommodation & Stays": [
        "hotel", "luxury_hotel", "budget_hotel", "heritage_hotel",
        "resort", "resort_hotel", "villa", "cottage",
        "homestay", "bed_and_breakfast", "farmhouse_resort", "service_villa",
        "guest_house", "lodge", "motel",
        "dharamshala", "ashram_stay",
        "hostel", "working_women_hostel", "dormitory",
        "pg_accommodation", "co_living_space",
        "serviced_apartment", "campground_glamping",
    ],
    "🤝 Social & Community": [
        "community_centre", "social_facility", "shelter",
        "orphanage", "old_age_home", "ngo_office",
    ],
    "🏗️ Public Utilities": [
        "water_point", "waste_disposal", "recycling",
        "toilets", "drinking_water", "telephone_booth", "post_box",
    ],
    "💼 Professional & Corporate": [
        "lawyer", "accountant", "travel_agency",
        "real_estate_agency", "courier", "warehouse", "logistics",
        "coworking_space", "it_company", "software_company", "ca_firm",
        "law_firm", "printing_press", "advertising_agency", "cold_storage",
        "security_services_agency",
    ],
    "⚽ Sports & Fitness": [
        "playground", "cricket_ground", "football_ground",
        "tennis_court", "basketball_court",
        "badminton_court", "swimming_academy", "yoga_center",
        "martial_arts_dojo", "turf_ground", "skating_rink",
        "table_tennis_hall", "gaming_zone",
    ],
    "🏛️ Landmarks & Monuments": [
        "monument", "memorial", "viewpoint", "sculpture",
        "fountain",
    ],
    "🎡 Tourist Attractions": [
        "zoo", "aquarium", "theme_park", "water_park", "national_park", "tourist_information",
    ],
    "🏖️ Coastal & Waterfront": [
        "beach", "lighthouse", "pier", "harbour", "marina",
    ],
    "✈️ Aviation & Airports": [
        "airport", "heliport",
    ],
    "🎨 Culture & Museums": [
        "museum", "art_gallery", "planetarium",
    ],
    "💇 Beauty & Personal Care": [
        "beauty_salon", "spa", "barber",
    ],
    "🏘️ Residential Complexes": [
        "apartment", "apartment_complex", "housing_complex",
        "residential_building", "residential_society", "gated_community",
    ],
    "🎉 Events & Banquets": [
        "banquet_hall",
        "marriage_hall",
        "wedding_venue",
        "party_hall",
        "convention_center",
    ],
    "✨ Other Services": [
        "conference_centre", "exhibition_centre", "crematorium",
        "nightclub", "internet_cafe", "laundry", "photo_studio",
    ],
    "🏭 PSUs & Industrial Establishments": [
        "all_psus_and_industries",
        "public_sector_undertaking",
        "oil_and_gas_plant",
        "power_and_energy_plant",
        "cement_plant",
        "defense_and_ordnance_factory",
        "research_and_development_institute",
        "food_corporation_godown",
        "central_public_works_mes",
        "railway_and_transport_corporation",
    ],
}

# ─────────────────────────────────────────────
#  DISTRICT-SPECIFIC ESTABLISHMENTS REGISTRY
# ─────────────────────────────────────────────

DISTRICT_ESTABLISHMENTS = {
    "Almora": [
        "Indian Medicines Pharmaceutical Corporation Limited (IMPCL)",
        "The Regional Research Institute of Himalayan Flora",
        "Almora Magnesite Limited",
        "Ranidhara Road GE MES Ranikhet",
        "The Executive Engineer CPWD Almora",
        "G.B. Pant National Institute of Himalayan Environment",
        "State Bank of India Regional Business Office Almora",
    ],
    "Bareilly": [
        "HPCL Bareilly",
        "IOCL Bareilly",
        "Power Grid Corporation of India Limited (PGCIL) Bareilly",
        "Food Corporation Of India FCI Bareilly",
        "Central Avian Research Institute (CARI) Bareilly",
        "IFFCO Aonla Unit Bareilly",
    ],
    "Dehradun": [
        "ONGC Ltd Dehradun",
        "Forest Research Institute FRI Dehradun",
        "THDCIL Rishikesh",
        "RVNL Rishikesh",
        "NTPC Ltd Dehradun",
        "Instruments Research & Development Establishment (IRDE)",
        "India Optel Limited (IOL) Dehradun",
        "DEAL (DRDO) Dehradun",
        "ITDR (DRDO) Dehradun",
        "AIIMS Rishikesh",
        "Airport Authority of India Dehradun Airport",
    ],
    "Gautam Buddha Nagar": [
        "IOCL Noida",
        "BPCL Noida",
        "HPCL Noida",
        "Gail Gas Limited Noida",
        "Gail Training Institute GTI Noida",
        "GAIL (INDIA) LIMITED Corporate Office Noida",
        "Ambuja Cements Ltd Noida",
        "NTPC Limited Noida",
        "Vodafone Idea Limited Noida",
        "Bharti Airtel Limited Noida",
        "ATC Telecom Infrastructure Private Limited Noida",
        "INDUS TOWERS LIMITED Noida",
        "Ultratech Cement Ltd Dadri",
        "Ambuja Cement Greater Noida",
        "Jewar Airport Noida International Airport",
        "FCI Noida",
        "DMRC Noida",
        "NBCC (I) Ltd Noida",
        "NMRC Noida",
        "CONCOR Container Corporation of India Greater Noida",
        "Oil India Ltd Noida",
        "Dedicated Freight Corridor Corporation of India Limited (DFCCIL) Noida",
    ],
    "Ghaziabad": [
        "LPG Bottling Plant IOCL BPCL HPCL Ghaziabad",
        "Central Public Works Department CPWD Ghaziabad",
        "National Capital Region Transport Corporation NCRTC Ghaziabad",
        "Bharat Electronics Limited (BEL) Ghaziabad",
        "Central Electronics Limited (CEL) Sahibabad",
        "BSNL Ghaziabad",
        "ESIC Hospital Ghaziabad",
        "Ordnance Factory Muradnagar",
        "Hindon Airport Air Force Station Ghaziabad",
    ],
    "Meerut": [
        "CPWD Meerut",
        "Meerut National Capital Region Transport Corporation NCRTC",
        "Power Grid Corporation of India Limited (PGCIL) Meerut",
        "Cantonment Board Meerut",
        "MES Meerut",
        "ICAR Central Institute for Research on Cattle Meerut",
    ],
    "Nainital": [
        "Indian Oil Corporation Limited IOCL Nainital",
        "Central Institute of Temperate Horticulture Mukteshwar",
        "CPWD Haldwani",
        "Electronic Services and Training Centre ESTC Ramnagar",
    ],
    "Pauri Garhwal": [
        "Bharat Electronics Ltd (BEL) Kotdwar",
        "CPWD Pauri Garhwal",
        "IOCL Pauri Garhwal",
        "Rail Vikas Nigam Limited RVNL Rishikesh Karnprayag Project",
    ],
    "Udham Singh Nagar": [
        "NHAI Project Implementation Unit Rudrapur",
        "IOCL Sitarganj",
        "State Infrastructure and Industrial Development Corporation of Uttarakhand (SIDCUL) Rudrapur Pantnagar",
        "BPCL Rudrapur",
        "IOCL LPG Bottling Plant Kashipur",
        "HPCL Udham Singh Nagar",
    ],
    "Amroha": [
        "IRB HAPUR MORADABAD TOLLWAY LIMITED Amroha",
    ],
    "Budaun": [
        "HPCL Compressed Bio Gas Plant Budaun",
        "Food Corporation of India FCI Budaun",
        "BSNL Budaun",
    ],
    "Bulandshahr": [
        "Nuclear Power Corporation of India Ltd Narora Atomic Power Station",
        "THDC Khurja Super Thermal Power Project",
        "Ultratech Cement Sikandrabad Bulandshahr",
        "Shree Cement Bulandshahr",
        "Kanodia Cement Bulandshahr",
    ],
    "Chamoli": [
        "THDCIL Pipalkoti Vishnugad Pipalkoti Hydro Electric Project",
        "NTPC Tapovan Vishnugad Hydro Power Project Chamoli",
    ],
    "Hapur": [
        "Food Corporation of India FCI Hapur",
        "MOTHER DAIRY FRUIT & VEGETABLE PVT LTD Pilkhuwa Hapur",
    ],
    "Haridwar": [
        "BHEL Ltd Ranipur Haridwar",
        "Ambuja Cement Ltd Haridwar",
        "Ultratech Cement Ltd Haridwar",
        "Shree Cement Ltd Haridwar",
    ],
    "Moradabad": [
        "Northern Railway Divisional Railway Manager DRM Office Moradabad",
        "IOCL Moradabad",
        "BSNL Moradabad",
        "Brass Handicrafts Export Development Centre Moradabad",
    ],
    "Pithoragarh": [
        "Power Grid Corporation of India Limited (PGCIL) Pithoragarh",
        "NHPC Dharchula Dhauliganga Power Station",
    ],
    "Rampur": [
        "IOCL Bottling Plant Rampur",
        "Food Corporation of India FCI Rampur",
        "BSNL Rampur",
    ],
    "Shahjahanpur": [
        "Ordnance Clothing Factory OCF Shahjahanpur",
        "Diesel Locomotive Modernisation Works Railway Shahjahanpur",
        "BPCL Shahjahanpur",
        "IOCL Shahjahanpur",
    ],
    "Tehri Garhwal": [
        "THDCIL Tehri Hydro Power Complex Tehri Dam",
        "THDCIL Koteshwar Hydro Electric Project",
    ],
}

DISTRICT_ALIASES = {
    "almora": "Almora", "ranikhet": "Almora",
    "bareilly": "Bareilly", "aonla": "Bareilly",
    "dehradun": "Dehradun", "rishikesh": "Dehradun",
    "gautam buddha nagar": "Gautam Buddha Nagar", "gb nagar": "Gautam Buddha Nagar", "gautam budh nagar": "Gautam Buddha Nagar",
    "noida": "Gautam Buddha Nagar", "greater noida": "Gautam Buddha Nagar", "dadri": "Gautam Buddha Nagar", "jewar": "Gautam Buddha Nagar",
    "ghaziabad": "Ghaziabad", "sahibabad": "Ghaziabad", "muradnagar": "Ghaziabad", "modinagar": "Ghaziabad",
    "meerut": "Meerut",
    "nainital": "Nainital", "haldwani": "Nainital", "mukteshwar": "Nainital", "ramnagar": "Nainital",
    "pauri": "Pauri Garhwal", "pauri garhwal": "Pauri Garhwal", "kotdwar": "Pauri Garhwal", "kotdwara": "Pauri Garhwal", "srinagar garhwal": "Pauri Garhwal",
    "udham singh nagar": "Udham Singh Nagar", "us nagar": "Udham Singh Nagar", "u s nagar": "Udham Singh Nagar",
    "rudrapur": "Udham Singh Nagar", "kashipur": "Udham Singh Nagar", "sitarganj": "Udham Singh Nagar", "pantnagar": "Udham Singh Nagar", "khatima": "Udham Singh Nagar",
    "amroha": "Amroha",
    "budaun": "Budaun", "badaun": "Budaun",
    "bulandshahr": "Bulandshahr", "bulandshahar": "Bulandshahr", "narora": "Bulandshahr", "khurja": "Bulandshahr", "sikandrabad": "Bulandshahr",
    "chamoli": "Chamoli", "pipalkoti": "Chamoli", "gopeshwar": "Chamoli", "joshimath": "Chamoli", "tapovan": "Chamoli",
    "hapur": "Hapur", "pilkhuwa": "Hapur",
    "haridwar": "Haridwar", "hardwar": "Haridwar", "roorkee": "Haridwar", "ranipur": "Haridwar",
    "moradabad": "Moradabad",
    "pithoragarh": "Pithoragarh", "dharchula": "Pithoragarh",
    "rampur": "Rampur",
    "shahjahanpur": "Shahjahanpur",
    "tehri": "Tehri Garhwal", "tehri garhwal": "Tehri Garhwal", "new tehri": "Tehri Garhwal", "koteshwar": "Tehri Garhwal",
}

PSU_AMENITIES = {
    "all_psus_and_industries",
    "public_sector_undertaking",
    "oil_and_gas_plant",
    "power_and_energy_plant",
    "cement_plant",
    "defense_and_ordnance_factory",
    "research_and_development_institute",
    "food_corporation_godown",
    "central_public_works_mes",
    "railway_and_transport_corporation",
}


def match_district_establishments(area: str, district: str = None) -> tuple[str | None, list[str]]:
    """
    Checks if an area or district matches any of the registered 20 industrial/PSU districts.
    Returns (matched_district_name, list_of_establishments) or (None, []).
    """
    candidates = []
    if district:
        candidates.append(re.sub(r"[^a-z0-9 ]+", " ", (district or "").lower()).strip())
    if area:
        for part in area.split(","):
            candidates.append(re.sub(r"[^a-z0-9 ]+", " ", (part or "").lower()).strip())
        candidates.append(re.sub(r"[^a-z0-9 ]+", " ", (area or "").lower()).strip())

    for c in candidates:
        if not c:
            continue
        for dist_key in DISTRICT_ESTABLISHMENTS:
            norm_key = re.sub(r"[^a-z0-9 ]+", " ", dist_key.lower()).strip()
            if norm_key == c or norm_key in c:
                return dist_key, DISTRICT_ESTABLISHMENTS[dist_key]
        if c in DISTRICT_ALIASES:
            matched_key = DISTRICT_ALIASES[c]
            return matched_key, DISTRICT_ESTABLISHMENTS.get(matched_key, [])
        for alias, dist_key in DISTRICT_ALIASES.items():
            if alias in c:
                return dist_key, DISTRICT_ESTABLISHMENTS.get(dist_key, [])

    return None, []


def filter_establishments_by_amenity(est_list: list[str], amenity: str) -> list[str]:
    """Filters a district's establishments to match a specific amenity subcategory if applicable."""
    amenity_lower = amenity.lower()
    if amenity_lower in ("all_psus_and_industries", "public_sector_undertaking"):
        return est_list

    keywords_map = {
        "cement_plant": ("cement",),
        "oil_and_gas_plant": ("iocl", "hpcl", "bpcl", "gail", "ongc", "oil", "lpg", "gas", "petroleum", "bottling"),
        "power_and_energy_plant": ("power", "grid", "pgcil", "ntpc", "thdc", "nhpc", "nuclear", "energy", "hydro"),
        "defense_and_ordnance_factory": ("ordnance", "drdo", "deal", "itdr", "irde", "bel", "cel", "mes", "cantonment", "military"),
        "research_and_development_institute": ("research", "institute", "flora", "pant", "cari", "icar", "horticulture", "training"),
        "food_corporation_godown": ("fci", "food", "dairy", "grain"),
        "central_public_works_mes": ("cpwd", "mes", "public works"),
        "railway_and_transport_corporation": ("rvnl", "ncrtc", "dmrc", "nmrc", "railway", "locomotive", "tollway", "concor", "dfccil", "nhai", "transport"),
    }
    targets = keywords_map.get(amenity_lower, ())
    if not targets:
        return est_list

    matched = [e for e in est_list if any(t in e.lower() for t in targets)]
    return matched if matched else est_list


ALL_AMENITIES = list(dict.fromkeys(a for v in AMENITY_CATEGORIES.values() for a in v))


# ─────────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────────

def fetch_amenities(amenity: str, area: str, level: str,
                    limit: int = 50, district: str = None, log_callback=None,
                    *, radius_km: float | None = None,
                    strict_category: bool = True,
                    fetch_details: bool = True,
                    headless: bool | None = None) -> list:
    def log(msg, tag="normal"):
        print(f"   {msg}")
        if log_callback:
            log_callback(f"   {msg}\n", tag)

    # Support "Area, District" — pass full string to Nominatim for disambiguation
    original_area = area
    area = _normalize_city_spelling(area)
    if area != original_area:
        log(f"ℹ️  Standardized area '{original_area}' → '{area}'", "info")

    amenity_term = amenity.replace("_", " ").strip()
    query = f"{amenity_term} in {area}"
    if district:
        query = f"{amenity_term} in {area}, {district}"
    log(f"🔍 Searching: {query} (limit: {limit})", "info")

    # ── Targeted District Establishments Routing ─────────────────────────
    if amenity in PSU_AMENITIES:
        matched_dist, dist_ests = match_district_establishments(area, district)
        if matched_dist and dist_ests:
            targeted_ests = filter_establishments_by_amenity(dist_ests, amenity)
            log(f"🏭 Matched registered district '{matched_dist}' ({len(targeted_ests)} target establishment(s) for '{amenity}')", "info")
            try:
                log(f"🌐 Querying registered {matched_dist} establishments via Google Maps...", "info")
                targeted_results = []
                for est_name in targeted_ests:
                    if len(targeted_results) >= limit:
                        break
                    est_query = f"{est_name} {matched_dist}"
                    log(f"   🔎 Searching: {est_query}...", "info")
                    try:
                        sub_res = _selenium_search(
                            est_query, amenity, area, limit=3, log=log,
                            radius_km=radius_km or 80.0,
                            strict_category=False,
                            fetch_details=fetch_details,
                            district=matched_dist,
                            headless=headless,
                        )
                        for r in sub_res:
                            if not _find_nearby(targeted_results, r.get("latitude", 0), r.get("longitude", 0), 150, r.get("name", "")):
                                r["amenity"] = amenity
                                targeted_results.append(r)
                    except Exception as sub_err:
                        log(f"   ⚠️ Could not fetch '{est_name}': {str(sub_err)[:80]}", "warn")

                if targeted_results:
                    log(f"✅ Found {len(targeted_results)} verified district establishments for {matched_dist}", "success")
                    return targeted_results
            except Exception as e:
                log(f"⚠️ Targeted district search encountered an issue: {str(e)[:80]} — falling back to general search...", "warn")

    try:
        log("🌐 Opening Google Maps via Selenium...", "info")
        results = _selenium_search(
            query, amenity, area, limit, log,
            radius_km=radius_km,
            strict_category=strict_category,
            fetch_details=fetch_details,
            district=district,
            headless=headless,
        )
        if results:
            log(f"✅ Selenium found {len(results)} results", "success")
            return results
        else:
            log("⚠️  Selenium returned 0 results — trying Overpass...", "warn")
    except Exception as e:
        log(f"⚠️  Selenium failed: {str(e)[:80]} — trying Overpass...", "warn")

    try:
        log("🌐 Querying Overpass API (fallback)...", "info")
        results = _overpass_search(amenity, area, limit, log, district=district)
        if results:
            log(f"✅ Overpass found {len(results)} results", "success")
            return results
    except Exception as e:
        log(f"❌ Overpass also failed: {str(e)[:80]}", "error")

    log("❌ No results found from any source", "error")
    return []


# ─────────────────────────────────────────────
#  SELENIUM SCRAPER
# ─────────────────────────────────────────────

def _selenium_search(
    query: str,
    amenity: str,
    area: str,
    limit: int,
    log,
    progress_callback=None,
    *,
    radius_km: float | None = None,
    strict_category: bool = True,
    fetch_details: bool = True,
    district: str | None = None,
    headless: bool | None = None,
) -> list:
    """
    Scrape Google Maps results panel.

    Coordinates come from each result card's own place anchor href
    (!3d<lat>!4d<lng>), never from the map viewport centre. Results outside
    the area radius, of the wrong category, or duplicated by position are
    dropped and logged.
    """
    from automation.maps_bot import setup_driver

    driver = setup_driver(headless=headless)
    try:
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(30)

        entries: list[dict] = []
        results: list[dict] = []

        # Anchor for the distance gate. Resolved once, before scraping.
        area = _normalize_city_spelling(area)
        anchor = _resolve_area_anchor(area, district=district, log=log)
        if radius_km is None:
            radius_km = anchor["radius_km"] if anchor else DEFAULT_RADIUS_KM

        # ── Step 1: Load results panel ────────────────────────────────────
        query = _qualify_query(query, area, anchor, log)
        search_url = _build_search_url(query, anchor)
        driver.get(search_url)

        try:
            WebDriverWait(driver, 15).until(
                lambda d: (
                    bool(d.find_elements(By.CSS_SELECTOR, "div[role='feed']"))
                    or bool(
                        d.find_elements(
                            By.CSS_SELECTOR,
                            "h1.DUwDvf, h1.fontHeadlineLarge",
                        )
                    )
                )
            )
        except TimeoutException:
            log("Google Maps result panel timed out", "warn")
            return []

        if anchor is None:
            v_lat, v_lng, v_source = _extract_coords(driver.current_url, allow_viewport_fallback=True)
            if v_lat is not None and v_lng is not None:
                eff_radius = radius_km or DEFAULT_RADIUS_KM
                anchor = {
                    "lat": v_lat,
                    "lng": v_lng,
                    "radius_km": eff_radius,
                    "source": "google_viewport",
                    "bbox": (
                        v_lat - eff_radius / _KM_PER_DEGREE_LAT,
                        v_lat + eff_radius / _KM_PER_DEGREE_LAT,
                        v_lng - eff_radius / (_KM_PER_DEGREE_LAT * math.cos(math.radians(v_lat))),
                        v_lng + eff_radius / (_KM_PER_DEGREE_LAT * math.cos(math.radians(v_lat))),
                    ),
                }
                log(f"   📍 Fallback viewport anchor resolved from map ({v_lat:.4f}, {v_lng:.4f})", "info")

        log("📄 Collecting places from results panel...", "info")

        # The feed container appears before its cards do. Harvesting on the
        # bare container is what produced empty runs, so wait for links.
        try:
            WebDriverWait(driver, 10).until(
                lambda d: len(
                    d.find_elements(By.CSS_SELECTOR, "a.hfpxzc")
                )
                > 0
            )
        except TimeoutException:
            log(
                "   ⚠️  No place links rendered after 10s — "
                "falling back to card titles",
                "warn",
            )

        panel = None
        for sel in ["div[role='feed']", "div.m6QErb.DxyBCb", "div.m6QErb"]:
            try:
                panel = driver.find_element(By.CSS_SELECTOR, sel)
                break
            except Exception:
                continue

        if not panel:
            # Single direct place result — no feed rendered.
            direct = _direct_place_entry(driver)
            if direct:
                entries = [direct]
                log(f"Direct place result found: {direct['name']}", "info")
            else:
                log("Results panel not found", "warn")
                return []

        # ── Step 2: Scroll and harvest cards ──────────────────────────────
        if panel:
            prev_count = 0
            no_change_count = 0
            NO_CHANGE_LIMIT = 3
            # Raw results are heavily filtered, so scroll far past `limit`.
            max_scrolls = min(40, max(10, limit))
            scroll_attempts = 0
            by_href: dict[str, dict] = {}
            last_gated_count = -1
            good_count = 0

            while scroll_attempts < max_scrolls:
                harvested = _harvest_cards(driver)

                # An empty first pass usually means the cards are still
                # painting, not that the area has nothing.
                if not harvested and not by_href:
                    time.sleep(2.0)
                    harvested = _harvest_cards(driver)

                for card in harvested:
                    key = card.get("href") or card.get("name")
                    if key and key not in by_href:
                        by_href[key] = card

                current_count = len(by_href)

                # Re-evaluate gates only when new cards were actually harvested
                if current_count != last_gated_count:
                    good_count = len(
                        _apply_gates(
                            list(by_href.values()),
                            amenity=amenity,
                            area=area,
                            anchor=anchor,
                            radius_km=radius_km,
                            strict_category=strict_category,
                            limit=limit,
                            log=None,
                        )[0]
                    )
                    last_gated_count = current_count

                log(
                    f"   Collected {current_count} places "
                    f"({good_count} usable)...",
                    "normal",
                )

                if good_count >= limit:
                    log("   ✅ Reached limit", "info")
                    break

                # Plenty of results, none of them inside the area: Google
                # is looking somewhere else entirely. Scrolling further
                # only collects more of the wrong city.
                if good_count == 0 and current_count >= 20:
                    log(
                        f"   ⚠️  {current_count} places found, none inside "
                        f"{area} — stopping early",
                        "warn",
                    )
                    break

                if current_count == prev_count:
                    no_change_count += 1
                    if no_change_count >= NO_CHANGE_LIMIT:
                        log(
                            f"   📄 No new results after {NO_CHANGE_LIMIT} "
                            "scrolls — stopping",
                            "info",
                        )
                        break
                else:
                    no_change_count = 0

                prev_count = current_count

                try:
                    driver.execute_script(
                        "arguments[0].scrollTop += 1000", panel
                    )
                    time.sleep(1.5)

                    page_src = driver.page_source
                    end_indicators = [
                        "You've reached the end",
                        "No more results",
                        "नतीजे खत्म",
                        "सभी नतीजे दिखाए",
                        'role="img" aria-label="End',
                        "section-no-result",
                    ]
                    if any(ind in page_src for ind in end_indicators):
                        log("   📄 End of results detected", "info")
                        break
                except Exception:
                    pass

                scroll_attempts += 1

            entries = list(by_href.values())

        log(f"   📋 Total raw places collected: {len(entries)}", "info")

        if not entries:
            return []

        # ── Step 3: Filter (coords / distance / category / duplicates) ────
        kept, dropped = _apply_gates(
            entries,
            amenity=amenity,
            area=area,
            anchor=anchor,
            radius_km=radius_km,
            strict_category=strict_category,
            limit=limit,
            log=log,
        )

        log(
            "   🧹 Filter summary — kept {k}, dropped: "
            "{n} no-coords, {j} junk-names, {f} too-far, {c} wrong-category, "
            "{d} duplicates".format(
                k=len(kept),
                n=dropped["no_coords"],
                j=dropped.get("junk_name", 0),
                f=dropped["too_far"],
                c=dropped["wrong_category"],
                d=dropped["duplicate"],
            ),
            "info",
        )

        unverified = sum(
            1 for entry in kept if entry.get("category_status") == "unverified"
        )
        if unverified and strict_category:
            if fetch_details:
                log(
                    f"   🔎 {unverified} result(s) had no usable category in "
                    "the panel — verifying type on their place pages",
                    "info",
                )
            else:
                log(
                    f"   ⚠️  {unverified} result(s) have an unconfirmed type "
                    "and fetch_details is off — they are kept but flagged "
                    "'unverified'",
                    "warn",
                )

        if not kept and entries:
            _diagnose_empty_result(
                entries, amenity, area, anchor, radius_km, dropped, log
            )

        # ── Step 4: Optional detail pass (address / phone / hours) ────────
        job_start = time.time()
        MAX_JOB_TIME = 600
        unconfirmed_kept: list[str] = []

        for entry in kept:
            record = {
                "name": entry.get("name", "Unknown"),
                "amenity": amenity,
                "latitude": entry["latitude"],
                "longitude": entry["longitude"],
                "address": entry.get("address") or "N/A",
                "rating": entry.get("rating") or "N/A",
                "phone": "N/A",
                "opening_hours": "N/A",
                "category": entry.get("category") or "N/A",
                "category_source": (
                    "panel"
                    if entry.get("category_status") == "match"
                    else "unverified"
                ),
                "coordinate_source": "exact_place",
                "distance_km": entry.get("distance_km", "N/A"),
                "maps_url": entry.get("href", ""),
                "aliases": ", ".join(entry.get("aliases", [])) or "",
            }

            if fetch_details and entry.get("href"):
                if time.time() - job_start > MAX_JOB_TIME:
                    log(
                        "⏰ Detail pass timeout — remaining rows keep "
                        "panel data only.",
                        "warn",
                    )
                    fetch_details = False
                else:
                    detail = _fetch_place_details(
                        driver, entry["href"], log
                    )
                    for key in ("address", "phone", "opening_hours"):
                        if detail.get(key):
                            record[key] = detail[key]
                    if detail.get("rating"):
                        record["rating"] = detail["rating"]

                    # The place page carries the authoritative category.
                    # Anything the panel could not confirm is settled here,
                    # and in strict mode "could not confirm" means dropped,
                    # never assumed correct.
                    page_category = detail.get("category", "")
                    unconfirmed = (
                        entry.get("category_status") == "unverified"
                    )

                    if page_category:
                        record["category"] = page_category
                        record["category_source"] = "place_page"

                    if strict_category and unconfirmed:
                        if page_category:
                            if _hit_category(amenity, page_category):
                                record["category_source"] = "place_page"
                            else:
                                log(
                                    f"   ⛔ {record['name'][:40]}: place "
                                    f"page says '{page_category[:30]}', "
                                    f"not '{amenity}' — dropped",
                                    "warn",
                                )
                                continue
                        else:
                            # No category anywhere. Nothing is deleted on
                            # the strength of a name alone. Measured over
                            # real runs this rule fired four times and was
                            # wrong four times -- every "Hotel ... Airport"
                            # in Delhi is a hotel named after the airport
                            # it serves. The result is kept and flagged so
                            # the doubt is visible rather than acted on.
                            conflict = _conflicting_type(
                                record["name"], amenity
                            )
                            if conflict:
                                log(
                                    f"   ⚠️  {record['name'][:40]}: name "
                                    f"mentions a {conflict} and no category "
                                    "was published — kept, flagged "
                                    "'unverified'",
                                    "warn",
                                )

                            record["category_source"] = "unverified"
                            unconfirmed_kept.append(record["name"])

            results.append(record)

            if progress_callback:
                progress_callback(len(results))

            log(
                f"   ✅ [{len(results)}/{len(kept)}] {record['name'][:35]} → "
                f"{record['latitude']}, {record['longitude']}",
                "success",
            )

        if unconfirmed_kept:
            log(
                f"   ⚠️  {len(unconfirmed_kept)} result(s) kept without a "
                f"confirmed type (column category_source='unverified'): "
                + ", ".join(name[:28] for name in unconfirmed_kept[:5])
                + (" ..." if len(unconfirmed_kept) > 5 else ""),
                "warn",
            )

    finally:
        driver.quit()
        log("🔒 Browser closed", "info")

    return results


def _diagnose_empty_result(
    entries: list,
    amenity: str,
    area: str,
    anchor: dict | None,
    radius_km: float,
    dropped: dict,
    log,
) -> None:
    """
    Explain why nothing survived, distinguishing the two very different
    causes: an area that genuinely has none of this amenity, versus
    Google having searched a different place with the same name.
    """
    total = len(entries)

    if not anchor or dropped.get("too_far", 0) != total:
        log(
            f"   ℹ️  {total} place(s) were found but none is a '{amenity}' "
            f"inside {area}. Set strict_category=False to inspect them.",
            "warn",
        )
        return

    distances = []
    for entry in entries:
        lat, lng, source = _extract_coords(entry.get("href", ""))
        if lat is None or source != "exact_place":
            continue
        distances.append(_outside_area(anchor, lat, lng, radius_km)[1])

    if not distances:
        return

    nearest = min(distances)

    if nearest > 100:
        log(
            f"   ❌ All {total} results are {nearest:.0f}+ km away. Google "
            f"matched a different '{area}' than the one resolved here "
            f"({anchor['lat']:.4f}, {anchor['lng']:.4f}).",
            "error",
        )
        log(
            "   👉 This name is ambiguous. Re-run with the district or "
            f"state appended, for example '{area}, <district>, <state>'.",
            "error",
        )
    else:
        log(
            f"   ℹ️  {total} result(s) found, nearest {nearest:.1f} km "
            f"outside {area}. If those belong to the area, raise "
            f"radius_km above {nearest:.0f}.",
            "warn",
        )


def _apply_gates(
    entries: list,
    *,
    amenity: str,
    area: str,
    anchor: dict | None,
    radius_km: float,
    strict_category: bool,
    limit: int,
    log=None,
) -> tuple[list, dict]:
    """
    Run every gate over raw panel entries.

    Pure apart from optional logging, so the scroll loop can call it
    silently to ask "do I have enough good results yet?" before deciding
    whether to keep scrolling.
    """
    kept: list[dict] = []
    dropped = {
        "no_coords": 0,
        "junk_name": 0,
        "too_far": 0,
        "wrong_category": 0,
        "duplicate": 0,
    }

    def _drop(bucket: str, message: str) -> None:
        dropped[bucket] += 1
        if log:
            log(message, "warn")

    for entry in entries:
        name = entry.get("name", "?")
        if not _is_valid_place_name(name):
            _drop(
                "junk_name",
                f"   ⛔ {name[:40]}: invalid place name / price badge / noise — dropped",
            )
            continue

        lat, lng, source = _extract_coords(entry.get("href", ""))

        if lat is None or lng is None or source != "exact_place":
            _drop(
                "no_coords",
                f"   ⛔ {name[:40]}: no exact coordinates in place link "
                "— dropped",
            )
            continue

        distance = None
        if anchor:
            outside, distance, mode = _outside_area(
                anchor, lat, lng, radius_km
            )
            if outside:
                if mode == "enclave":
                    reason = (
                        f"sits in an enclave inside {area}, "
                        "which belongs to a different area"
                    )
                elif mode in ("polygon", "boundary"):
                    edge = (
                        "boundary" if mode == "polygon"
                        else "bounding box"
                    )
                    reason = (
                        f"{distance:.1f} km outside the {area} {edge}"
                        if distance
                        else f"outside the {area} {edge}"
                    )
                else:
                    reason = (
                        f"{distance:.1f} km from {area} "
                        f"(limit {radius_km:.0f} km)"
                    )
                _drop("too_far", f"   ⛔ {name[:40]}: {reason} — dropped")
                continue

        keep, reason = _classify(
            amenity, entry.get("category", ""), name, area
        )

        if strict_category and not keep:
            if reason == "is_area":
                detail = f"is the search area '{area}', not an amenity"
            else:
                detail = (
                    f"category '{entry.get('category', '')[:30]}' "
                    f"is not '{amenity}'"
                )
            _drop("wrong_category", f"   ⛔ {name[:40]}: {detail} — dropped")
            continue

        duplicate_of = _find_nearby(kept, lat, lng, DEDUPE_METRES, name)
        if duplicate_of is not None:
            _drop(
                "duplicate",
                f"   ⛔ {name[:40]}: same location as "
                f"'{duplicate_of['name'][:30]}' — merged",
            )
            aliases = duplicate_of.setdefault("aliases", [])
            if entry.get("name") and entry["name"] not in aliases:
                aliases.append(entry["name"])
            continue

        entry["latitude"] = lat
        entry["longitude"] = lng
        entry["category_status"] = reason
        if distance is not None:
            entry["distance_km"] = round(distance, 2)
        kept.append(entry)

        if len(kept) >= limit:
            break

    return kept, dropped


# ─────────────────────────────────────────────
#  PANEL HARVESTING
# ─────────────────────────────────────────────

_HARVEST_JS = r"""
const out = [];
const anchors = document.querySelectorAll("a.hfpxzc");
anchors.forEach(function (a) {
  const href = a.getAttribute("href") || "";
  let name = (a.getAttribute("aria-label") || "").trim();
  const card = a.closest("div.Nv2PK") || a.parentElement;
  let category = "", rating = "", address = "";

  if (card) {
    // 1. Exclude sponsored and advertisement cards
    const isAd = card.querySelector(".k3fvEc, [aria-label*='Sponsored'], [aria-label*='Ad'], [aria-label*='प्रायोजित']") !== null ||
                 card.textContent.trim().startsWith("Sponsored") ||
                 card.textContent.trim().startsWith("Ad ·");
    if (isAd) { return; }

    // 2. Resolve true place name if anchor aria-label was empty or a price tag
    const isPrice = /^[₹$€£¥]\s*[\d,]+/i.test(name) || /^Rs\.?\s*[\d,]+/i.test(name) || /^INR\s*[\d,]+/i.test(name) || /^\d+\s*[₹$€£¥]/i.test(name);
    if (!name || isPrice) {
      const t = card.querySelector("div.qBF1Pd, div.fontHeadlineSmall");
      if (t) { name = t.textContent.trim(); }
    }

    // If still a price tag or contains no letters, skip this price badge pin
    if (/^[₹$€£¥]\s*[\d,]+/i.test(name) || /^Rs\.?\s*[\d,]+/i.test(name) || /^INR\s*[\d,]+/i.test(name) || !/[a-zA-Z\u0900-\u097F]/.test(name)) {
      return;
    }

    // 3. Extract rating
    const r = card.querySelector("span.MW4etd");
    if (r) { rating = r.textContent.trim(); }

    // 4. Collect secondary lines for category & address
    const lines = [];
    card.querySelectorAll("div.W4Efsd").forEach(function (el) {
      const txt = el.textContent.replace(/\s+/g, " ").trim();
      if (txt) { lines.push(txt); }
    });

    let catCandidate = "";
    let addrCandidate = "";

    lines.forEach(function (line) {
      const segments = line.split("·").map(function (s) { return s.trim(); }).filter(Boolean);
      segments.forEach(function (seg) {
        // Skip pure rating numbers or review counts e.g. "4.2 (1,234)" or "(520)"
        if (/^\d+[.,]\d+(\s*\([\d,.]+\))?$/.test(seg) || /^\([\d,.]+\)$/.test(seg)) {
          return;
        }
        // Skip operating hours & status labels
        const sLower = seg.toLowerCase();
        if (sLower === "open" || sLower === "closed" || sLower.startsWith("opens") || sLower.startsWith("closes") || sLower.includes("open 24 hours") || sLower.includes("temporarily closed") || sLower.includes("permanently closed")) {
          return;
        }
        // Skip distance strings e.g. "2.4 km", "800 m"
        if (/^\d+(\.\d+)?\s*(km|m|mi)\b/i.test(seg)) {
          return;
        }
        // Skip pricing e.g. "₹2,000"
        if (/^[₹$€£¥]/.test(seg) || /^Rs\.?/i.test(seg)) {
          return;
        }

        // Category candidate:
        // Accept typical categories (alphabetic, short) OR hotel star ratings (e.g. "3-star hotel", "4-star hotel", "5-star hotel", "7-star hotel")
        const isStarHotel = /^[1-7]\s*[-]?\s*star\s*hotel/i.test(seg);
        if (!catCandidate && ((!/\d/.test(seg) && /[a-zA-Z\u0900-\u097F]/.test(seg)) || isStarHotel) && seg.length < 50) {
          catCandidate = seg;
        } else if (!addrCandidate && (/\d/.test(seg) || seg.includes(",") || seg.length > 8)) {
          addrCandidate = seg;
        } else if (!addrCandidate && seg.length >= 3 && seg !== catCandidate) {
          addrCandidate = seg;
        }
      });
    });

    category = catCandidate;
    address = addrCandidate;
  }

  if (name && href) {
    out.push({
      name: name,
      href: href,
      category: category,
      rating: rating,
      address: address
    });
  }
});
return out;
"""


def _qualify_query(
    query: str,
    area: str,
    anchor: dict | None,
    log,
) -> str:
    """
    Replace a bare area name in the query with its resolved full name.

    Centring the map is not always enough. Google routes lodging searches
    through its hotel vertical, which re-resolves the place from the text
    and ignores the viewport -- which is why "Hospital in Gandhi Nagar"
    returned Hanamkonda correctly while "Hotel in Gandhi Nagar" returned
    Delhi from the very same anchored URL. Naming the district and state
    in the text closes that gap.
    """
    if not anchor:
        return query

    qualified = anchor.get("qualified_name") or ""

    if not qualified or _normalise(qualified) == _normalise(area):
        return query

    if area not in query:
        return query

    rewritten = query.replace(area, qualified, 1)

    log(f"   🎯 Query pinned to '{qualified}'", "info")

    return rewritten


def _search_zoom(anchor: dict | None) -> int:
    """
    Pick a Google Maps zoom level that frames the area.

    Zoom is what tells Google which place a repeated name refers to. A
    tight zoom on a locality keeps the search local; a loose one on a
    state lets it cover the whole thing.
    """
    if not anchor:
        return 11

    span_km = None
    bbox = anchor.get("bbox")

    if bbox:
        south, north, west, east = bbox
        span_km = max(
            _haversine_km(south, west, north, west),
            _haversine_km(south, west, south, east),
        )
    elif anchor.get("radius_km"):
        span_km = anchor["radius_km"] * 2

    if span_km is None:
        return 12

    for threshold, zoom in (
        (5, 14),
        (15, 13),
        (40, 12),
        (100, 11),
        (250, 10),
        (600, 8),
    ):
        if span_km <= threshold:
            return zoom

    return 7


def _build_search_url(query: str, anchor: dict | None) -> str:
    """
    Build a Google Maps search URL centred on the resolved area.

    Without a centre, Google resolves the place name by its own ranking
    while Nominatim resolves it by another, and the two can land in
    different states. "Gandhi Nagar" is a locality in Telangana to
    Nominatim and the Gujarat capital to Google, so every result then
    falls a thousand kilometres outside the boundary and is discarded.
    Pinning the viewport makes both agree.
    """
    base = "https://www.google.com/maps/search/" + quote_plus(query)

    if not anchor:
        return base

    return (
        f"{base}/@{anchor['lat']:.6f},{anchor['lng']:.6f},"
        f"{_search_zoom(anchor)}z"
    )


def _harvest_cards(driver) -> list:
    """Pull name + place href + category from every visible result card."""
    try:
        raw = driver.execute_script(_HARVEST_JS) or []
    except Exception:
        raw = []

    cards = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        href = (item.get("href") or "").strip()
        if not name and not href:
            continue
        cards.append(
            {
                "name": name or "Unknown",
                "href": href,
                "category": _strip_leading_rating(
                    (item.get("category") or "").strip()
                ),
                "rating": (item.get("rating") or "").strip(),
                "address": (item.get("address") or "").strip(),
            }
        )

    if cards:
        return cards

    # Fallback: older DOM without a.hfpxzc anchors.
    fallback = []
    for el in driver.find_elements(
        By.CSS_SELECTOR,
        "div.qBF1Pd, div.fontHeadlineSmall, span.fontHeadlineSmall",
    ):
        try:
            name = el.text.strip()
            if name and len(name) > 2:
                fallback.append(
                    {
                        "name": name,
                        "href": "",
                        "category": "",
                        "rating": "",
                        "address": "",
                    }
                )
        except Exception:
            continue
    return fallback


def _strip_leading_rating(text: str) -> str:
    """Remove a leading '4.3 (1,234)' fragment from a category string."""
    return re.sub(
        r"^\d+[.,]\d+\s*(\([\d,.]+\))?\s*", "", text
    ).strip(" ·-")


def _direct_place_entry(driver) -> dict | None:
    """Build an entry when Google opens a single place instead of a feed."""
    name = ""
    for title in driver.find_elements(
        By.CSS_SELECTOR, "h1.DUwDvf, h1.fontHeadlineLarge"
    ):
        name = title.text.strip()
        if name:
            break

    if not name:
        return None

    category = _get_text(driver, ["button.DkEaL", "span.DkEaL"])

    return {
        "name": name,
        "href": driver.current_url,
        "category": category,
        "rating": _get_text(driver, ["div.F7nice span[aria-hidden='true']"]),
        "address": "",
    }


def _read_place_category(driver) -> str:
    """
    Read a place's category from its own page.

    Takes the first candidate that is an actual business type. Google
    reuses the category button's class for "Add website" style prompts on
    incomplete listings, so every candidate is screened before use.
    """
    selectors = [
        "button.DkEaL",
        "div.LBgpqf button.DkEaL",
        "div.skqShb button.DkEaL",
        "button[jsaction*='category']",
        "button[jsaction*='rating.category']",
        "span.DkEaL",
        "div.fontBodyMedium span.YhemCb",
        # Hotel and lodging pages often render the type as plain text
        # beside the star rating rather than as a category button.
        "div.skqShb span.YhemCb",
        "div.LBgpqf div.fontBodyMedium span",
    ]

    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue

        for element in elements:
            for value in (
                _safe_text(element),
                _safe_attribute(element, "aria-label"),
            ):
                if not value:
                    continue
                if _looks_like_action_label(value):
                    continue
                if _is_noise_category(value):
                    continue
                return value

    return ""


def _safe_text(element) -> str:
    try:
        return (element.text or "").strip()
    except Exception:
        return ""


def _safe_attribute(element, attribute: str) -> str:
    try:
        return (element.get_attribute(attribute) or "").strip()
    except Exception:
        return ""


def _fetch_place_details(driver, href: str, log) -> dict:
    """
    Open a place's own Maps link and read detail fields.

    The href already identifies the place, so this never re-searches by
    name and can never land on the wrong place.
    """
    detail: dict = {}

    try:
        driver.get(href)
    except TimeoutException:
        log("   ⚠️  Timeout loading place details", "warn")
        return detail
    except Exception:
        return detail

    try:
        WebDriverWait(driver, 8).until(
            lambda d: bool(
                d.find_elements(
                    By.CSS_SELECTOR,
                    "button[data-item-id='address'], h1.DUwDvf",
                )
            )
        )
    except Exception:
        pass

    detail["address"] = _get_text(
        driver,
        [
            "button[data-item-id='address'] .fontBodyMedium",
            "button[data-item-id='address']",
        ],
    )
    detail["phone"] = _get_text(
        driver,
        [
            "button[data-item-id^='phone'] .fontBodyMedium",
            "[data-item-id*='phone'] .fontBodyMedium",
        ],
    )
    detail["rating"] = _get_text(
        driver,
        ["div.F7nice span[aria-hidden='true']", "div.F7nice"],
    )
    detail["opening_hours"] = _get_text(
        driver,
        ["div.t39EBf.GUrTXd span", ".o0Svhf span"],
    )
    detail["category"] = _read_place_category(driver)

    return {k: v for k, v in detail.items() if v}


def _get_text(driver, selectors: list) -> str:
    for sel in selectors:
        try:
            el   = driver.find_element(By.CSS_SELECTOR, sel)
            text = el.text.strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def _extract_coords(
    url: str,
    *,
    allow_viewport_fallback: bool = False,
) -> tuple[float | None, float | None, str]:
    """
    Return (latitude, longitude, source) from a Google Maps URL.

    !3d/!4d is the place pin. @lat,lng is only the viewport centre and is a
    different place whenever the search missed, so it is rejected by default.
    """
    url = url or ""

    exact_match = re.search(
        r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)",
        url,
    )
    if exact_match:
        return (
            float(exact_match.group(1)),
            float(exact_match.group(2)),
            "exact_place",
        )

    if allow_viewport_fallback:
        center_match = re.search(
            r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
            url,
        )
        if center_match:
            return (
                float(center_match.group(1)),
                float(center_match.group(2)),
                "viewport_center",
            )

    return None, None, "not_found"


# ─────────────────────────────────────────────
#  GEO GATES
# ─────────────────────────────────────────────

# A circle around a centroid cannot describe an administrative area: a
# state is 1000 km across, a town is 3 km, and neither is round. The gate
# therefore tests containment in the area's own bounding box, grown by
# BOUNDARY_BUFFER_KM so that places just outside the line still count.
# The radius values are only a fallback for areas with no bounding box.
DEFAULT_RADIUS_KM = 15.0
MIN_RADIUS_KM = 5.0
MAX_RADIUS_KM = 250.0
BOUNDARY_BUFFER_KM = 10.0
# Below this, a bounding box is a point and should not be treated as area.
DEGENERATE_BBOX_KM = 1.0
DEDUPE_METRES = 250.0
_KM_PER_DEGREE_LAT = 111.32


def _haversine_km(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    """Great-circle distance in kilometres."""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _bbox_contains(
    bbox: tuple,
    lat: float,
    lng: float,
    buffer_km: float = 0.0,
) -> bool:
    """Point-in-box test with an optional buffer expressed in kilometres."""
    south, north, west, east = bbox

    if buffer_km:
        d_lat = buffer_km / _KM_PER_DEGREE_LAT
        mid_lat = math.radians((south + north) / 2)
        cos_lat = max(0.01, math.cos(mid_lat))
        d_lng = buffer_km / (_KM_PER_DEGREE_LAT * cos_lat)
        south, north = south - d_lat, north + d_lat
        west, east = west - d_lng, east + d_lng

    return south <= lat <= north and west <= lng <= east


def _distance_outside_bbox(bbox: tuple, lat: float, lng: float) -> float:
    """Kilometres from the point to the nearest edge of the box (0 if inside)."""
    south, north, west, east = bbox

    d_lat_deg = 0.0
    if lat < south:
        d_lat_deg = south - lat
    elif lat > north:
        d_lat_deg = lat - north

    d_lng_deg = 0.0
    if lng < west:
        d_lng_deg = west - lng
    elif lng > east:
        d_lng_deg = lng - east

    mid_lat = math.radians(max(min(lat, north), south))
    d_lat_km = d_lat_deg * _KM_PER_DEGREE_LAT
    d_lng_km = d_lng_deg * _KM_PER_DEGREE_LAT * math.cos(mid_lat)

    return math.hypot(d_lat_km, d_lng_km)


def _point_in_ring(ring: list, lat: float, lng: float) -> bool:
    """Ray-casting test for a single closed ring of (lng, lat) pairs."""
    inside = False
    count = len(ring)

    for index in range(count):
        lng1, lat1 = ring[index]
        lng2, lat2 = ring[(index + 1) % count]

        if (lat1 > lat) != (lat2 > lat):
            if lat2 != lat1:
                crossing = (lng2 - lng1) * (lat - lat1) / (lat2 - lat1) + lng1
                if lng < crossing:
                    inside = not inside

    return inside


def _polygon_state(polygon: list, lat: float, lng: float) -> str:
    """
    Locate a point relative to a polygon with holes.

    Returns "inside", "hole", or "outside". Each polygon is
    [outer_ring, hole_ring, ...]. The hole case is reported separately
    because a hole is an enclave belonging to a different administrative
    area -- Delhi inside Uttar Pradesh -- and no distance tolerance should
    ever pull it back in.
    """
    in_hole = False

    for rings in polygon:
        if not rings:
            continue
        if not _point_in_ring(rings[0], lat, lng):
            continue
        if any(_point_in_ring(hole, lat, lng) for hole in rings[1:]):
            in_hole = True
            continue
        return "inside"

    return "hole" if in_hole else "outside"


def _point_in_polygon(polygon: list, lat: float, lng: float) -> bool:
    """True only when the point is inside the shape and not in a hole."""
    return _polygon_state(polygon, lat, lng) == "inside"


def _segment_distance_km(
    lat: float, lng: float,
    lat1: float, lng1: float,
    lat2: float, lng2: float,
    cos_lat: float,
) -> float:
    """Point-to-segment distance using a local equirectangular projection."""
    x = lng * cos_lat * _KM_PER_DEGREE_LAT
    y = lat * _KM_PER_DEGREE_LAT
    x1 = lng1 * cos_lat * _KM_PER_DEGREE_LAT
    y1 = lat1 * _KM_PER_DEGREE_LAT
    x2 = lng2 * cos_lat * _KM_PER_DEGREE_LAT
    y2 = lat2 * _KM_PER_DEGREE_LAT

    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)

    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))

    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def _distance_to_polygon(
    polygon: list,
    lat: float,
    lng: float,
    *,
    outer_only: bool = False,
) -> float:
    """
    Shortest distance in kilometres from a point to a polygon edge.

    With outer_only=True the hole rings are ignored, so the distance
    describes how far the point is from the area's real outer boundary.
    """
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    best = float("inf")

    for rings in polygon:
        selected = rings[:1] if outer_only else rings
        for ring in selected:
            count = len(ring)
            for index in range(count):
                lng1, lat1 = ring[index]
                lng2, lat2 = ring[(index + 1) % count]
                distance = _segment_distance_km(
                    lat, lng, lat1, lng1, lat2, lng2, cos_lat
                )
                if distance < best:
                    best = distance
                    if best < 0.05:
                        return best

    return 0.0 if best == float("inf") else best


def _parse_geojson_polygon(geometry: dict) -> list | None:
    """Normalise a GeoJSON Polygon/MultiPolygon into [[ring, hole...], ...]."""
    if not isinstance(geometry, dict):
        return None

    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")

    if not coordinates:
        return None

    try:
        if kind == "Polygon":
            return [[[(float(p[0]), float(p[1])) for p in ring]
                     for ring in coordinates]]
        if kind == "MultiPolygon":
            return [[[(float(p[0]), float(p[1])) for p in ring]
                     for ring in polygon]
                    for polygon in coordinates]
    except (TypeError, ValueError, IndexError):
        return None

    return None


def _outside_area(
    anchor: dict,
    lat: float,
    lng: float,
    radius_km: float,
) -> tuple[bool, float, str]:
    """
    Test a point against the search area.

    Returns (is_outside, distance_km, mode). `mode` is "boundary" when the
    area's bounding box was used and "radius" when it fell back to a circle.
    """
    bbox = anchor.get("bbox")
    polygon = anchor.get("polygon")
    buffer_km = anchor.get("buffer_km", BOUNDARY_BUFFER_KM)

    if polygon:
        # Cheap box rejection first; the polygon walk is the expensive part.
        if bbox and not _bbox_contains(bbox, lat, lng, buffer_km):
            return True, _distance_outside_bbox(bbox, lat, lng), "polygon"

        state = _polygon_state(polygon, lat, lng)

        if state == "inside":
            return False, 0.0, "polygon"

        if state == "hole":
            # An enclave is a different area, however close the border is.
            # Applying the buffer here would let Delhi's airport register
            # as an Uttar Pradesh airport.
            return True, _distance_to_polygon(polygon, lat, lng), "enclave"

        distance = _distance_to_polygon(polygon, lat, lng, outer_only=True)
        return distance > buffer_km, distance, "polygon"

    if bbox:
        inside = _bbox_contains(bbox, lat, lng, buffer_km)
        return (
            not inside,
            _distance_outside_bbox(bbox, lat, lng),
            "boundary",
        )

    distance = _haversine_km(anchor["lat"], anchor["lng"], lat, lng)
    return distance > radius_km, distance, "radius"


NAME_MATCH_RATIO = 0.85

# Words that appear in half the listings in a category and therefore carry
# no identifying power. "Hotel Oasis" and "Hotel Royal Residency" share
# only the word "hotel", which must not make them the same place.
NAME_GENERIC_WORDS = frozenset(
    {
        "the", "a", "an", "of", "and", "at", "in", "on", "for",
        "hotel", "hotels", "inn", "lodge", "motel", "resort", "resorts",
        "guest", "house", "guesthouse", "hostel", "stay", "stays",
        "rooms", "room", "residency", "restaurant", "cafe", "bar",
        "airport", "airports", "station", "hospital", "clinic",
        "school", "college", "bank", "atm", "store", "shop", "mall",
        "ltd", "pvt", "limited", "co", "company",
    }
)


def _name_tokens(name: str) -> set:
    """Distinctive words of a name, with generic filler removed."""
    tokens = {t for t in _normalise(name).split() if len(t) > 1}
    distinctive = tokens - NAME_GENERIC_WORDS
    # If a name is nothing but filler, fall back to using all of it.
    return distinctive or tokens


def _name_similarity(first: str, second: str) -> float:
    """
    Similarity of two place names, 0.0 to 1.0.

    Uses containment of distinctive tokens rather than overlap, so
    "Taj Mahal Palace" still matches "The Taj Mahal Palace Hotel" while
    "Carlton Hotel - Behind Taj Mahal Palace" does not: naming a landmark
    is not the same as being it.
    """
    clean_first = _normalise(first)
    clean_second = _normalise(second)

    if not clean_first or not clean_second:
        return 0.0
    if clean_first == clean_second:
        return 1.0

    tokens_first = _name_tokens(first)
    tokens_second = _name_tokens(second)

    if not tokens_first or not tokens_second:
        return 0.0

    shared = len(tokens_first & tokens_second)
    smaller = min(len(tokens_first), len(tokens_second))

    # A single shared word is usually a place name doing the work of a
    # coincidence: "Hotel Delhi Guest House" and "Hotel Golden Oasis
    # Delhi" share only "delhi", and "Le ROI Express Paharganj" shares
    # only "paharganj" with its neighbour. Two names on the same street
    # will collide on the locality every time, so identity needs more.
    if shared < 2:
        if smaller == 1 and len(tokens_first) == len(tokens_second) == 1:
            return difflib.SequenceMatcher(
                None, next(iter(tokens_first)), next(iter(tokens_second))
            ).ratio()
        return 0.0

    return shared / smaller


def _find_nearby(
    records: list[dict],
    lat: float,
    lng: float,
    metres: float,
    name: str = "",
) -> dict | None:
    """
    Find a record that is the same place as the given one.

    Proximity alone is not identity. In a dense district a hundred
    distinct businesses share a block, so a merge also requires the names
    to agree; otherwise neighbours silently delete each other.
    """
    # Fast bounding-box pre-filter before trigonometric Haversine calculation
    max_lat_delta = metres / 110000.0
    cos_lat = max(0.01, abs(math.cos(math.radians(lat))))
    max_lng_delta = metres / (110000.0 * cos_lat)

    for record in records:
        existing_lat = record.get("latitude")
        existing_lng = record.get("longitude")
        if existing_lat is None or existing_lng is None:
            continue

        if abs(existing_lat - lat) > max_lat_delta or abs(existing_lng - lng) > max_lng_delta:
            continue

        distance_m = (
            _haversine_km(existing_lat, existing_lng, lat, lng) * 1000
        )
        if distance_m > metres:
            continue

        if not name:
            return record

        if _name_similarity(record.get("name", ""), name) >= NAME_MATCH_RATIO:
            return record

    return None


def _state_of(place: dict) -> str:
    """Best guess at the state from a Nominatim display name."""
    parts = [
        part.strip()
        for part in str(place.get("display_name") or "").split(",")
        if part.strip()
    ]
    parts = [
        part for part in parts
        if part.lower() != "india" and not part.replace(" ", "").isdigit()
    ]
    return parts[-1] if parts else ""


def _qualified_place_name(display_name, area: str) -> str:
    """
    Shorten a Nominatim display name to something usable as a search term.

    "Gandhi Nagar, Hanamkonda, Hanamkonda mandal, Hanumakonda, Telangana,
    506001, India" becomes "Gandhi Nagar, Hanamkonda, Telangana": enough
    to pin the place, short enough not to confuse a text search.
    """
    if not display_name:
        return area

    parts = [
        part.strip()
        for part in str(display_name).split(",")
        if part.strip()
    ]

    # Drop the country and any postcode; they add no disambiguation.
    parts = [
        part for part in parts
        if part.lower() != "india" and not part.replace(" ", "").isdigit()
    ]

    if not parts:
        return area

    keep = [parts[0]]

    # The last part is normally the state, which is the strongest single
    # disambiguator, followed by whatever district-level name precedes it.
    if len(parts) >= 2:
        state = parts[-1]
        middle = parts[1] if len(parts) > 2 else ""

        seen = {_normalise(keep[0])}
        for candidate in (middle, state):
            clean = _normalise(candidate)
            if candidate and clean not in seen:
                keep.append(candidate)
                seen.add(clean)

    return ", ".join(keep)


COMMON_CITY_ALIASES = {
    "ahemdabad": "Ahmedabad",
    "ahmadabad": "Ahmedabad",
    "ahmdabad": "Ahmedabad",
    "bengluru": "Bengaluru",
    "banglore": "Bengaluru",
    "bangalore": "Bengaluru",
    "gurgoan": "Gurugram",
    "gurgaon": "Gurugram",
    "culcutta": "Kolkata",
    "kolkatta": "Kolkata",
    "bombay": "Mumbai",
    "madras": "Chennai",
    "baroda": "Vadodara",
    "banaras": "Varanasi",
    "benaras": "Varanasi",
    "allahabad": "Prayagraj",
    "trichy": "Tiruchirappalli",
    "trivandrum": "Thiruvananthapuram",
    "cochin": "Kochi",
    "pondicherry": "Puducherry",
    "vizag": "Visakhapatnam",
    "poona": "Pune",
    "gauhati": "Guwahati",
    "mysore": "Mysuru",
    "mangalore": "Mangaluru",
    "belgaum": "Belagavi",
    "hubli": "Hubballi",
    "calicut": "Kozhikode",
    "cannanore": "Kannur",
    "palghat": "Palakkad",
    "alleppey": "Alappuzha",
    "quilon": "Kollam",
    "simla": "Shimla",
}


def _normalize_city_spelling(text: str) -> str:
    """Normalize common city typos, old names, and phonetic variations."""
    if not text:
        return text
    clean = text.strip()
    low = clean.lower()
    if low in COMMON_CITY_ALIASES:
        return COMMON_CITY_ALIASES[low]
    for typo, canonical in COMMON_CITY_ALIASES.items():
        pattern = r"\b" + re.escape(typo) + r"\b"
        if re.search(pattern, clean, flags=re.IGNORECASE):
            return re.sub(pattern, canonical, clean, flags=re.IGNORECASE)
    return clean


def _resolve_area_anchor(
    area: str,
    *,
    district: str | None = None,
    log: Any = None,
    country_code: str | None = None,
) -> dict | None:
    """
    Resolve the search area to a centre point and a sensible radius.

    The radius is derived from the area's own bounding box (half its
    diagonal) plus a margin, clamped to MIN/MAX, so a small town gets a
    tight gate and a large district gets a wide one.
    """
    def _log(message, tag="info"):
        if log:
            log(message, tag)

    original_area = area
    area = _normalize_city_spelling(area)
    if area != original_area:
        _log(
            f"ℹ️  Standardized area '{original_area}' → '{area}' "
            f"for accurate geocoding & boundaries",
            "info",
        )

    search_query = f"{area}, {district}" if district else area
    cc = (country_code or DEFAULT_COUNTRY_CODE).strip().lower()

    try:
        params = {
            "q": search_query,
            "format": "json",
            "limit": 10,
            "polygon_geojson": 1,
            # Simplify the outline; full state borders are megabytes.
            "polygon_threshold": 0.003,
        }
        if cc:
            params["countrycodes"] = cc

        response = requests.get(
            NOMINATIM_URL,
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        places = response.json()
    except Exception as error:
        _log(
            f"⚠️  Could not resolve '{search_query}' for distance filter: "
            f"{str(error)[:60]} — distance gate fallback active",
            "warn",
        )
        places = []

    if not places and district:
        try:
            params["q"] = area
            res = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=10)
            places = res.json()
        except Exception:
            places = []

    if not places and "," in area:
        try:
            params["q"] = area.split(",")[0].strip()
            res = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=10)
            places = res.json()
        except Exception:
            places = []

    if not places:
        _log(
            f"⚠️  '{search_query}' not found in Nominatim — using map viewport anchor",
            "info",
        )
        return None

    def _has_outline(candidate: dict) -> bool:
        geometry = candidate.get("geojson") or {}
        return geometry.get("type") in ("Polygon", "MultiPolygon")

    # Rank by importance, but a slightly less prominent match that carries a
    # real outline beats a prominent one that only has a bounding box.
    places.sort(
        key=lambda p: (
            _has_outline(p),
            float(p.get("importance", 0) or 0),
        ),
        reverse=True,
    )
    place = places[0]

    try:
        centre_lat = float(place["lat"])
        centre_lng = float(place["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    radius_km = DEFAULT_RADIUS_KM
    box = None
    polygon = _parse_geojson_polygon(place.get("geojson") or {})
    raw_bbox = place.get("boundingbox") or []

    if len(raw_bbox) == 4:
        try:
            # Nominatim order is [south, north, west, east].
            south, north, west, east = (float(v) for v in raw_bbox)
            diagonal = _haversine_km(south, west, north, east)
            radius_km = max(
                MIN_RADIUS_KM,
                min(MAX_RADIUS_KM, diagonal / 2 + MIN_RADIUS_KM),
            )

            # Postcodes and villages often resolve to a single node, whose
            # "bounding box" is a few metres across. Treating that as an
            # area means the buffer alone decides what is inside, which is
            # arbitrary. A stated radius is more honest and tunable.
            if diagonal < DEGENERATE_BBOX_KM:
                box = None
                if not polygon:
                    _log(
                        f"ℹ️  '{search_query}' resolved to a point, not an "
                        f"area — using a {radius_km:.0f} km radius instead",
                        "warn",
                    )
            else:
                box = (south, north, west, east)
        except (TypeError, ValueError):
            box = None

    _log(
        f"📍 Area anchor: {centre_lat:.5f}, {centre_lng:.5f} — "
        f"{str(place.get('display_name', area))[:60]}",
        "info",
    )

    # Many Indian place names repeat across states. Show what else this
    # name matched so a wrong pick is caught at the top of the run rather
    # than inferred from a thousand discarded results at the bottom.
    alternatives = []
    chosen_state = _state_of(place)

    for candidate in places[1:]:
        state = _state_of(candidate)
        if state and state != chosen_state and state not in alternatives:
            alternatives.append(state)

    if district:
        resolved = _normalise(str(place.get("display_name") or ""))
        if _normalise(district) and _normalise(district) not in resolved:
            _log(
                f"⚠️  District '{district}' does not appear in the resolved "
                f"place. Nominatim returned "
                f"'{_qualified_place_name(place.get('display_name'), area)}'"
                " — check the district is right for this area.",
                "warn",
            )

    if alternatives:
        _log(
            f"⚠️  '{search_query}' also matches {', '.join(alternatives[:3])}"
            f" — using {chosen_state or 'the top match'}. Append the state "
            "if that is not the one you meant.",
            "warn",
        )

    if polygon:
        vertices = sum(len(ring) for rings in polygon for ring in rings)
        if vertices <= 8:
            # A handful of points is a placeholder square drawn around a
            # node, not a surveyed border. Say so rather than implying
            # the gate is more precise than it is.
            _log(
                f"📐 Boundary: rough outline only, {vertices} points "
                f"+ {BOUNDARY_BUFFER_KM:.0f} km buffer — small locality, "
                "consider naming its district",
                "warn",
            )
        else:
            _log(
                f"📐 Boundary: real outline, {vertices} points "
                f"+ {BOUNDARY_BUFFER_KM:.0f} km buffer",
                "info",
            )
    elif box:
        span_ns = _haversine_km(box[0], box[2], box[1], box[2])
        span_ew = _haversine_km(box[0], box[2], box[0], box[3])
        _log(
            f"📐 Boundary: bounding box only, {span_ns:.0f} x "
            f"{span_ew:.0f} km + {BOUNDARY_BUFFER_KM:.0f} km buffer",
            "warn",
        )
    else:
        _log(
            "📐 No boundary available — falling back to a "
            f"{radius_km:.0f} km radius",
            "warn",
        )

    return {
        "lat": centre_lat,
        "lng": centre_lng,
        "radius_km": radius_km,
        "bbox": box,
        "polygon": polygon,
        "buffer_km": BOUNDARY_BUFFER_KM,
        "display_name": str(place.get("display_name") or search_query),
        "qualified_name": _qualified_place_name(
            place.get("display_name"), area
        ),
    }


# ─────────────────────────────────────────────
#  CATEGORY GATE
# ─────────────────────────────────────────────

# Google's displayed category rarely equals the internal amenity key.
CATEGORY_SYNONYMS = {
    "airport": ("airport", "aerodrome", "airfield", "airline", "terminal"),
    "heliport": ("heliport", "helipad", "helicopter"),
    "chemist": ("pharmacy", "chemist", "drug store", "medical store"),
    "pharmacy": ("pharmacy", "chemist", "drug store", "medical store"),
    "fuel": ("gas station", "petrol pump", "filling station", "fuel"),
    "doctor": ("doctor", "physician", "medical", "clinic"),
    "place_of_worship": (
        "temple", "mosque", "church", "gurudwara",
        "place of worship", "shrine",
    ),
    "gurudwara": ("gurudwara", "gurdwara", "sikh temple"),
    "railway_station": ("train station", "railway station", "railway"),
    "metro_station": ("metro station", "subway station", "metro"),
    "bus_station": (
        "bus station", "bus stand", "bus terminal", "bus depot", "bus stop",
    ),
    "bus_stop": ("bus stop", "bus station", "bus stand"),
    "fast_food": ("fast food", "burger", "pizza", "sandwich"),
    "mall": ("shopping mall", "mall", "shopping centre", "shopping center"),
    "supermarket": ("supermarket", "grocery", "grocery store"),
    "convenience_store": ("convenience store", "grocery", "general store"),
    "gym": ("gym", "fitness", "fitness center", "fitness centre"),
    "hotel": ("hotel", "lodging", "inn"),
    "guest_house": ("guest house", "lodging", "guesthouse"),
    "school": ("school",),
    "college": ("college", "institute"),
    "atm": ("atm",),
    "bank": ("bank",),
    "police": ("police", "police station"),
    "fire_station": ("fire station", "fire department"),
    "post_office": ("post office", "courier"),
    "cinema": (
        "cinema", "movie theater", "movie theatre", "multiplex",
        "film", "theater", "theatre",
    ),
    "beauty_salon": ("beauty salon", "salon", "beauty parlour", "beauty"),
    "barber": ("barber", "hair salon", "hairdresser"),
    "real_estate_agency": ("real estate", "estate agent", "property"),
    "apartment": ("apartment", "apartment complex", "housing", "residential"),
    "apartment_complex": ("apartment", "housing complex", "residential"),

    # Labels used in bulk job files, with the category names Google
    # actually publishes for them.
    "temple": (
        "temple", "hindu temple", "jain temple", "buddhist temple",
        "shrine", "mandir", "place of worship",
    ),
    "mosque": ("mosque", "masjid", "islamic", "place of worship"),
    "church": (
        "church", "cathedral", "chapel", "catholic", "protestant",
        "place of worship",
    ),
    "zoo": ("zoo", "zoological", "wildlife park", "animal park", "safari"),
    "aquarium": ("aquarium", "marine", "oceanarium"),
    "theme_park": (
        "theme park", "amusement park", "amusement center",
        "amusement centre", "fun park",
    ),
    "water_park": ("water park", "waterpark", "aqua park", "amusement park"),
    "national_park": (
        "national park", "wildlife sanctuary", "nature preserve",
        "national forest", "nature reserve", "park",
    ),
    "bus_stand": ("bus station", "bus stand", "bus stop", "bus terminal"),
    "university": ("university", "college", "institute", "campus"),
    "stadium": ("stadium", "arena", "sports complex"),
    "museum": ("museum", "art gallery", "heritage"),
    "library": ("library",),
    "park": ("park", "garden", "public park"),
    "police_station": ("police", "police station"),
    "petrol_pump": ("gas station", "petrol pump", "filling station"),
    "ambulance_station": ("ambulance", "ambulance service", "emergency medical service", "ems"),
    "polling_station": ("polling station", "polling place", "polling booth", "election office", "voting"),
    "kindergarten": ("kindergarten", "preschool", "pre-school", "nursery school", "playschool"),
    "preschool": ("preschool", "pre-school", "kindergarten", "nursery school", "playschool"),
    "driving_school": ("driving school", "driving instruction", "driving academy"),
    "music_school": ("music school", "music academy", "music instructor", "music classes"),
    "dance_school": ("dance school", "dance academy", "dance studio", "dance classes"),
    "institute_of_technology": ("institute of technology", "engineering college", "polytechnic", "technology institute"),
    "institute_of_management": ("institute of management", "business school", "management institute", "b-school"),
    "institute_of_science": ("institute of science", "science college", "science institute", "research institute"),

    # Healthcare additions
    "diagnostic_center": ("diagnostic center", "diagnostic centre", "pathology lab", "diagnostic lab", "medical laboratory", "blood testing", "laboratory"),
    "radiology_center": ("radiology center", "radiology centre", "x-ray lab", "mri center", "ct scan center", "imaging center", "diagnostic center"),
    "physiotherapy_clinic": ("physiotherapist", "physiotherapy clinic", "rehabilitation center", "physical therapy clinic", "physiotherapy"),
    "orthopedic_clinic": ("orthopedic clinic", "orthopedic surgeon", "orthopedist", "bone specialist", "clinic"),
    "dialysis_center": ("dialysis center", "dialysis centre", "nephrologist", "kidney care", "medical clinic"),
    "maternity_hospital": ("maternity hospital", "gynecologist", "obstetrician-gynecologist", "women's health clinic", "hospital"),
    "psychiatry_clinic": ("psychiatrist", "psychiatric hospital", "mental health clinic", "counselor", "psychotherapy"),
    "optical_store": ("optician", "optometrist", "optical store", "eyewear store", "eye care", "optical shop"),

    # Education additions
    "coaching_center": ("coaching center", "coaching centre", "coaching institute", "coaching class", "academy", "training centre", "tutoring service", "education center"),
    "tuition_center": ("tuition", "tuition centre", "tuition center", "tutoring service", "private tutor", "learning center", "education center"),
    "polytechnic": ("polytechnic institute", "polytechnic college", "polytechnic", "technical school", "engineering college"),
    "vocational_training_institute": ("vocational training school", "training institute", "vocational school", "technical education", "training center"),
    "daycare": ("day care center", "daycare", "child care agency", "crèche", "preschool"),
    "boarding_school": ("boarding school", "residential school", "school"),
    "language_school": ("language school", "english language school", "foreign language institute", "language classes"),

    # Corporate & Professional additions
    "coworking_space": ("coworking space", "shared office", "business center", "virtual office", "executive suite"),
    "it_company": ("software company", "it services", "computer consultant", "tech company", "information technology company"),
    "software_company": ("software company", "software developer", "computer software store", "tech company"),
    "ca_firm": ("chartered accountant", "accounting firm", "tax consultant", "accountant", "certified public accountant"),
    "law_firm": ("law firm", "lawyer", "attorney", "legal services", "advocate", "solicitor"),
    "printing_press": ("commercial printer", "print shop", "digital printing service", "invitation printing", "printer"),
    "advertising_agency": ("advertising agency", "marketing agency", "digital marketing agency", "media company"),
    "cold_storage": ("cold storage facility", "warehouse", "logistics service", "refrigerated storage"),
    "security_services_agency": ("security guard service", "security service", "security system supplier"),

    # Transport additions
    "ev_charging_station": ("electric vehicle charging station", "ev charging", "charging station", "ev charging station"),
    "cng_station": ("cng filling station", "cng station", "gas station", "petrol pump"),
    "car_repair": ("car repair", "auto repair shop", "mechanic", "garage", "car service", "auto body shop"),
    "auto_garage": ("auto repair shop", "car repair", "motorcycle repair shop", "mechanic", "garage"),
    "tyre_shop": ("tire shop", "tyre shop", "wheel alignment service", "tire repair shop"),
    "auto_stand": ("auto rickshaw stand", "taxi stand", "auto stand", "transportation service"),
    "bike_rental": ("motorcycle rental agency", "bicycle rental service", "bike hire", "bike rental"),
    "toll_plaza": ("toll plaza", "toll booth", "toll road", "tollgate"),
    "car_wash": ("car wash", "pressure wash", "car washing", "auto detailing", "car cleaning service", "car detailing service"),

    # Automobile additions
    "spare_parts_dealer": (
        "auto parts store", "auto parts market", "spare parts store",
        "motorcycle parts store", "car accessories store", "auto body parts supplier",
        "truck parts supplier", "wholesaler", "dealer", "spare parts", "auto parts",
    ),
    "automobile_shop": (
        "automobile shop", "car accessories store", "auto parts store",
        "car dealer", "motor vehicle dealer", "auto repair shop", "car repair",
        "motorcycle dealer", "motorcycle shop", "auto accessories", "car accessories",
    ),
    "car_dealership": (
        "car dealer", "motor vehicle dealer", "car showroom", "car company",
        "volkswagen dealer", "toyota dealer", "hyundai dealer", "tata motors dealer",
        "maruti suzuki dealer", "mahindra dealer", "honda car dealer", "kia dealer",
        "car sales", "automobile dealer",
    ),
    "two_wheeler_dealer": (
        "motorcycle dealer", "motor scooter dealer", "two wheeler dealer",
        "hero dealer", "honda motorcycle dealer", "bajaj dealer", "tvs dealer",
        "yamaha dealer", "royal enfield dealer", "scooter dealer", "bike showroom",
    ),
    "used_car_dealer": (
        "used car dealer", "car dealer", "used motorcycle dealer",
        "pre-owned car dealer", "auto broker", "used car showroom",
    ),
    "car_accessories_shop": (
        "car accessories store", "car stereo store", "car security system installer",
        "auto parts store", "car accessories", "car decor", "car seat covers",
    ),
    "car_battery_store": (
        "car battery store", "battery store", "battery wholesaler",
        "battery manufacturer", "auto electrical shop", "battery dealer",
    ),
    "auto_electrical_shop": (
        "auto electrical service", "car battery store", "auto repair shop",
        "electric motor repair shop", "auto electrician",
    ),
    "cng_fitment_center": (
        "cng fitment center", "alternative fuel station", "gas installation service",
        "auto repair shop", "cng kit",
    ),
    "car_detailing_studio": (
        "car detailing service", "car wash", "auto restoration service",
        "paint protection film", "auto body shop", "detailing studio",
    ),
    "motorcycle_repair": (
        "motorcycle repair shop", "motorcycle shop", "scooter repair shop",
        "mechanic", "bike repair", "two wheeler service",
    ),

    # Civic additions
    "passport_office": ("passport office", "passport seva kendra", "passport agent", "government office"),
    "rto_office": ("regional transport office", "transport office", "motor vehicle department", "rto", "government office"),
    "sub_registrar_office": ("sub registrar office", "registration office", "government office", "land registry office", "sub-registrar"),
    "municipal_corporation": ("municipal corporation office", "city government office", "local government office", "town hall", "municipal office"),
    "public_service_center": ("citizen service center", "seva kendra", "government office", "csc", "public service"),
    "electricity_bill_office": ("electric utility company", "electricity board office", "power station", "electricity bill"),
    "lpg_gas_agency": ("gas agency", "gas cylinder supplier", "bottled gas supplier", "lpg distributor", "gas company"),

    # Food additions
    "dhaba": ("dhaba", "punjabi dhaba", "highway dhaba", "restaurant", "family restaurant", "eatery"),
    "cloud_kitchen": ("delivery restaurant", "cloud kitchen", "ghost kitchen", "meal delivery", "food delivery"),
    "coffee_shop": ("coffee shop", "cafe", "espresso bar", "coffee store", "coffeeshop"),
    "bistro": ("bistro", "french restaurant", "restaurant", "cafe"),
    "microbrewery": ("brewpub", "microbrewery", "brewery", "pub", "bar"),
    "rooftop_restaurant": ("restaurant", "fine dining restaurant", "bar", "lounge", "rooftop"),
    "catering_service": ("caterer", "catering food and drink supplier", "wedding buffet", "catering service"),

    # Sports additions
    "badminton_court": ("badminton court", "badminton club", "badminton academy", "sports club", "sports complex"),
    "swimming_academy": ("swimming school", "swimming facility", "swimming pool", "swim club"),
    "yoga_center": ("yoga studio", "yoga center", "yoga retreat", "meditation center", "yoga instructor"),
    "martial_arts_dojo": ("martial arts school", "karate school", "taekwondo school", "judo club", "dojo"),
    "turf_ground": ("sports complex", "football field", "cricket ground", "box cricket", "sports club"),
    "skating_rink": ("roller skating rink", "ice skating rink", "skate park"),
    "table_tennis_hall": ("table tennis facility", "table tennis club", "sports complex"),
    "gaming_zone": ("video arcade", "gaming lounge", "esports arena", "amusement center"),

    # Retail additions
    "stationery_shop": ("stationery store", "office supply store", "pen store", "book store", "stationery shop"),
    "footwear_shop": ("shoe store", "boot store", "footwear wholesaler", "footwear shop"),
    "dairy_booth": ("dairy store", "milk delivery service", "dairy supplier", "milk booth"),
    "pet_shop": ("pet store", "pet shop", "pet supply", "aquarium shop", "pet breeder"),
    "meat_shop": ("butcher shop", "meat products store", "poultry store", "fish store", "meat shop"),
    "tailor_and_boutique": ("tailor", "custom tailor", "boutique", "women's clothing store", "dressmaker"),
    "auto_parts_store": ("auto parts store", "car accessories store", "motorcycle parts store", "spare parts store"),
    "florist": ("florist", "flower designer", "flower delivery", "flower market"),
    "electrical_store": ("electrical supply store", "electronics store", "lighting store", "electrical shop"),

    # Accommodation additions
    "hotel": (
        "hotel", "lodging", "inn", "hotel stay", "rooms",
        "1-star hotel", "2-star hotel", "3-star hotel", "4-star hotel", "5-star hotel", "7-star hotel",
        "1 star hotel", "2 star hotel", "3 star hotel", "4 star hotel", "5 star hotel", "7 star hotel",
        "star hotel", "boutique hotel", "business hotel", "extended stay hotel",
    ),
    "motel": ("motel", "motor inn", "highway hotel", "lodging", "inn"),
    "hostel": ("hostel", "youth hostel", "backpackers hostel", "student hostel", "dormitory"),
    "guest_house": ("guest house", "lodging", "guesthouse", "bed and breakfast"),
    "resort": ("resort", "resort hotel", "holiday resort", "wellness resort"),
    "lodge": ("lodge", "lodging", "safari lodge", "mountain lodge", "guest house"),
    "dharamshala": ("dharamshala", "dharmashala", "ashram", "pilgrim hostel", "sarai", "guest house", "place of worship"),
    "homestay": ("homestay", "bed and breakfast", "guest house", "farmstay", "cottage", "village stay"),
    "serviced_apartment": ("serviced apartment", "extended stay hotel", "holiday apartment", "apartment hotel", "apartment"),
    "farmhouse_resort": ("resort hotel", "resort", "farmstay", "retreat center", "farmhouse", "agro tourism"),
    "bed_and_breakfast": ("bed & breakfast", "bed and breakfast", "inn", "guesthouse", "b&b"),
    "dormitory": ("dormitory", "hostel", "student dormitory", "dorm"),
    "luxury_hotel": (
        "5 star hotel", "4 star hotel", "luxury hotel", "boutique hotel",
        "palace hotel", "grand hotel", "hotel", "resort hotel",
    ),
    "budget_hotel": (
        "budget hotel", "economy hotel", "oyo", "treebo", "fabhotel",
        "hotel", "inn", "lodge", "motel",
    ),
    "heritage_hotel": (
        "heritage hotel", "palace hotel", "haveli", "heritage resort",
        "historic hotel", "fort hotel", "hotel",
    ),
    "resort_hotel": (
        "resort hotel", "resort", "eco resort", "jungle resort",
        "nature resort", "beach resort", "hill resort", "spa resort",
    ),
    "villa": (
        "villa", "luxury villa", "pool villa", "holiday home",
        "vacation home", "service villa", "cottage",
    ),
    "cottage": (
        "cottage", "chalet", "wooden cottage", "log cabin",
        "mountain cottage", "bungalow", "holiday home",
    ),
    "service_villa": (
        "service villa", "serviced villa", "holiday villa",
        "farmhouse", "villa", "homestay",
    ),
    "ashram_stay": (
        "ashram", "spiritual retreat", "yoga ashram", "meditation center",
        "dharamshala", "pilgrim guest house",
    ),
    "working_women_hostel": (
        "working women hostel", "girls hostel", "ladies hostel",
        "women hostel", "paying guest for women", "hostel",
    ),
    "pg_accommodation": (
        "paying guest", "pg", "pg accommodation", "boys pg",
        "girls pg", "co living pg", "student pg", "paying guest accommodation",
    ),
    "co_living_space": (
        "co living", "coliving space", "shared living", "co-living",
        "managed accommodation", "executive pg", "hostel",
    ),
    "campground_glamping": (
        "campground", "camping", "glamping", "luxury tents",
        "camp site", "desert camp", "riverside camp", "tent stay",
    ),

    # Events & Banquets additions
    "banquet_hall": (
        "banquet hall", "banquet halls", "banquet", "marriage hall", "wedding venue",
        "event venue", "party hall", "function room facility", "convention center",
        "wedding buffet", "community hall", "kalyana mandapam", "marriage palace",
        "function hall", "event management company",
    ),
    "marriage_hall": (
        "marriage hall", "kalyana mandapam", "marriage palace", "wedding venue",
        "banquet hall", "wedding hall", "shadi hall", "event venue", "community hall",
        "function hall", "marriage celebrant",
    ),
    "wedding_venue": (
        "wedding venue", "wedding chapel", "wedding planner", "banquet hall",
        "marriage hall", "event venue", "wedding buffet", "resort", "farmhouse",
        "marriage palace", "kalyana mandapam",
    ),
    "party_hall": (
        "party hall", "banquet hall", "event venue", "birthday party hall",
        "function room facility", "community center", "party planner",
    ),
    "convention_center": (
        "convention center", "convention centre", "conference center",
        "conference centre", "exhibition center", "auditorium", "event venue",
    ),

    # PSUs & Industrial additions
    "all_psus_and_industries": (
        "public sector", "government undertaking", "factory", "plant",
        "industrial plant", "corporation", "limited", "establishment",
        "depot", "workshop", "power station", "substation", "manufacturing",
        "government office", "corporate office",
    ),
    "public_sector_undertaking": (
        "psu", "public sector undertaking", "government office", "corporate office",
        "central government office", "corporation", "undertaking", "state undertaking",
    ),
    "oil_and_gas_plant": (
        "lpg bottling plant", "bottling plant", "gas plant", "oil depot",
        "petroleum", "iocl", "hpcl", "bpcl", "gail", "ongc", "oil refinery",
        "gas agency", "gas cylinder supplier", "cng station", "oil company",
    ),
    "power_and_energy_plant": (
        "power station", "thermal power plant", "hydroelectric power plant",
        "power grid", "pgcil", "ntpc", "thdcil", "nhpc", "substation",
        "electric utility company", "nuclear power station", "power plant",
    ),
    "cement_plant": (
        "cement manufacturer", "cement factory", "cement plant",
        "ambuja cement", "ultratech cement", "shree cement", "kanodia cement",
        "building materials store", "concrete supplier", "cement dealer",
    ),
    "defense_and_ordnance_factory": (
        "ordnance factory", "defense establishment", "drdo", "deal", "irde",
        "bel", "bharat electronics", "cel", "military base", "cantonment",
        "mes", "military engineer services",
    ),
    "research_and_development_institute": (
        "research institute", "institute", "research foundation",
        "botanical garden", "himalayan flora", "gb pant",
        "forest research institute", "icar", "cari", "training center",
    ),
    "food_corporation_godown": (
        "food corporation of india", "fci", "godown", "warehouse",
        "grain storage", "mother dairy", "dairy plant",
    ),
    "central_public_works_mes": (
        "cpwd", "central public works department", "mes",
        "military engineer services", "public works department",
        "government office",
    ),
    "railway_and_transport_corporation": (
        "ncrtc", "rvnl", "dmrc", "nmrc", "dfccil", "concor",
        "railway office", "railway station", "metro station",
        "nhai", "transportation service", "toll plaza",
    ),
}


# A name containing the amenity word is not proof of type: "Noida Airport
# Inn" is a hotel, "Airport Road Clinic" is a clinic. When any of these
# words appear, the name is not accepted as evidence on its own.
NAME_CONFLICT_WORDS = (
    "hotel", "inn", "resort", "lodge", "hostel", "guest house",
    "guesthouse", "motel", "residency", "suites", "apartments",
    "taxi", "cab", "cabs", "car rental", "rent a car", "travels",
    "travel", "tours", "tour", "transfer", "transport", "shuttle",
    "parking", "road", "marg", "highway", "chowk", "nagar", "colony",
    "restaurant", "cafe", "bar", "dhaba", "kitchen", "bakery",
    "shop", "store", "mart", "agency", "services", "service",
    "solutions", "consultancy", "consultants", "office", "clinic",
    "hospital", "school", "academy", "society", "plaza", "tower",
    "express", "logistics", "courier", "packers", "movers",
)


# Words that reliably name a business type, grouped by the amenity family
# they belong to. Used only to reject a result whose name announces a
# different type than the one requested.
TYPE_FAMILIES = {
    "lodging": (
        "hotel", "inn", "lodge", "motel", "resort", "hostel",
        "guest house", "guesthouse", "dormitory", "homestay",
        "serviced apartment", "bed and breakfast", "farmhouse",
        "villa", "cottage", "dharamshala", "ashram", "paying guest",
        "pg", "co living", "campground", "camping", "glamping", "chalet",
    ),
    "aviation": ("airport", "airstrip", "aerodrome", "airfield", "heliport"),
    "food": (
        "restaurant", "cafe", "dhaba", "bakery", "bar", "pub", "canteen",
        "bistro", "microbrewery", "coffee shop", "cloud kitchen",
    ),
    "medical": (
        "hospital", "clinic", "nursing home", "pharmacy", "chemist",
        "diagnostic center", "pathology lab", "physiotherapy",
    ),
    "education": (
        "school", "college", "university", "academy",
        "coaching", "tuition", "polytechnic", "daycare",
    ),
    "finance": ("bank", "atm", "insurance", "microfinance"),
    "fuel": ("petrol pump", "gas station", "filling station", "cng station", "charging station"),
    "transport": (
        "bus stand", "bus station", "bus stop", "railway station",
        "metro station", "taxi", "car rental", "car repair", "garage",
    ),
    "retail": (
        "supermarket", "mall", "grocery", "pharmacy", "store",
        "pet shop", "footwear", "stationery",
    ),
    "worship": ("temple", "mosque", "church", "gurudwara", "mandir", "ashram"),
    "leisure": (
        "zoo", "aquarium", "museum", "cinema", "theme park",
        "water park", "gaming zone", "stadium", "sports complex",
    ),
    "corporate": (
        "coworking", "software company", "it services", "office",
        "law firm", "ca firm", "accounting firm",
    ),
    "civic": (
        "passport office", "rto office", "municipal corporation",
        "court", "police", "post office",
    ),
    "sports": (
        "badminton", "tennis", "cricket", "football", "swimming",
        "gym", "yoga", "turf",
    ),
    "automobile": (
        "spare parts", "auto parts", "automobile", "car accessories",
        "car dealer", "car showroom", "motorcycle dealer", "bike dealer",
        "two wheeler", "used car", "car repair", "auto garage",
        "car service", "tyre shop", "tire shop", "car wash",
        "car battery", "auto electrical", "detailing", "mechanic",
        "scooter", "motorcycle", "wheel alignment", "cng fitment",
    ),
    "events": (
        "banquet", "marriage hall", "wedding venue", "party hall",
        "convention center", "kalyana mandapam", "marriage palace",
        "function hall", "event venue", "shadi hall", "wedding chapel",
    ),
    "industry": (
        "psu", "factory", "plant", "refinery", "substation",
        "power station", "cement", "ordnance", "institute",
        "corporation", "establishment", "depot", "workshop",
    ),
}

# Which family each amenity key belongs to.
AMENITY_FAMILY = {
    "hotel": "lodging", "guest_house": "lodging", "hostel": "lodging",
    "motel": "lodging", "resort": "lodging", "apartment": "lodging",
    "homestay": "lodging", "serviced_apartment": "lodging",
    "farmhouse_resort": "lodging", "bed_and_breakfast": "lodging", "dormitory": "lodging",
    "lodge": "lodging", "dharamshala": "lodging",
    "luxury_hotel": "lodging", "budget_hotel": "lodging", "heritage_hotel": "lodging",
    "resort_hotel": "lodging", "villa": "lodging", "cottage": "lodging",
    "service_villa": "lodging", "ashram_stay": "lodging",
    "working_women_hostel": "lodging", "pg_accommodation": "lodging",
    "co_living_space": "lodging", "campground_glamping": "lodging",
    "banquet_hall": "events", "marriage_hall": "events",
    "wedding_venue": "events", "party_hall": "events",
    "convention_center": "events",
    "airport": "aviation", "heliport": "aviation",
    "restaurant": "food", "cafe": "food", "fast_food": "food", "bar": "food",
    "dhaba": "food", "cloud_kitchen": "food", "coffee_shop": "food",
    "bistro": "food", "microbrewery": "food", "rooftop_restaurant": "food", "catering_service": "food",
    "hospital": "medical", "clinic": "medical", "doctor": "medical",
    "pharmacy": "medical", "chemist": "medical", "ambulance_station": "medical",
    "diagnostic_center": "medical", "radiology_center": "medical",
    "physiotherapy_clinic": "medical", "orthopedic_clinic": "medical",
    "dialysis_center": "medical", "maternity_hospital": "medical",
    "psychiatry_clinic": "medical", "optical_store": "medical",
    "school": "education", "college": "education", "university": "education",
    "kindergarten": "education", "preschool": "education",
    "coaching_center": "education", "tuition_center": "education",
    "polytechnic": "education", "vocational_training_institute": "education",
    "daycare": "education", "boarding_school": "education", "language_school": "education",
    "bank": "finance", "atm": "finance",
    "fuel": "fuel",
    "bus_station": "transport", "bus_stop": "transport",
    "railway_station": "transport", "metro_station": "transport",
    "bus_stand": "transport",
    "ev_charging_station": "transport", "cng_station": "fuel",
    "car_repair": "automobile", "auto_garage": "automobile",
    "tyre_shop": "automobile", "auto_stand": "transport",
    "bike_rental": "transport", "toll_plaza": "transport",
    "spare_parts_dealer": "automobile", "automobile_shop": "automobile",
    "car_dealership": "automobile", "two_wheeler_dealer": "automobile",
    "used_car_dealer": "automobile", "car_accessories_shop": "automobile",
    "car_battery_store": "automobile", "auto_electrical_shop": "automobile",
    "cng_fitment_center": "automobile", "car_detailing_studio": "automobile",
    "motorcycle_repair": "automobile", "car_wash": "automobile",
    "supermarket": "retail", "mall": "retail",
    "convenience_store": "retail", "stationery_shop": "retail",
    "footwear_shop": "retail", "dairy_booth": "retail", "pet_shop": "retail",
    "meat_shop": "retail", "tailor_and_boutique": "retail",
    "auto_parts_store": "automobile", "florist": "retail", "electrical_store": "retail",
    "temple": "worship", "mosque": "worship", "church": "worship",
    "gurudwara": "worship", "place_of_worship": "worship",
    "cinema": "leisure", "zoo": "leisure", "aquarium": "leisure",
    "theme_park": "leisure", "water_park": "leisure",
    "national_park": "leisure", "museum": "leisure", "park": "leisure",
    "badminton_court": "sports", "swimming_academy": "sports",
    "yoga_center": "sports", "martial_arts_dojo": "sports",
    "turf_ground": "sports", "skating_rink": "sports",
    "table_tennis_hall": "sports", "gaming_zone": "sports",
    "passport_office": "civic", "rto_office": "civic",
    "sub_registrar_office": "civic", "municipal_corporation": "civic",
    "public_service_center": "civic", "electricity_bill_office": "civic",
    "lpg_gas_agency": "civic",
    "coworking_space": "corporate", "it_company": "corporate",
    "software_company": "corporate", "ca_firm": "corporate",
    "law_firm": "corporate", "printing_press": "corporate",
    "advertising_agency": "corporate", "cold_storage": "corporate",
    "security_services_agency": "corporate",
    "all_psus_and_industries": "industry",
    "public_sector_undertaking": "industry",
    "oil_and_gas_plant": "industry",
    "power_and_energy_plant": "industry",
    "cement_plant": "industry",
    "defense_and_ordnance_factory": "industry",
    "research_and_development_institute": "industry",
    "food_corporation_godown": "industry",
    "central_public_works_mes": "industry",
    "railway_and_transport_corporation": "industry",
}


# A name conflict annotates a result; it never removes one.
NAME_CONFLICT_IS_ADVISORY = True


def _conflicting_type(name: str, amenity: str) -> str:
    """
    Return the conflicting type named in `name`, or "" when there is none.

    This is a disambiguator, not a filter. It only speaks when the name
    already looks like the requested amenity, and asks whether some other
    type word overrules that: "Noida Airport Inn" says airport but means
    inn. When the requested word is absent the name carries no claim to
    judge -- "Novotel Mumbai International Airport" is named after the
    airport it serves, and only its category can settle what it is.
    """
    clean_name = f" {_normalise(name)} "
    own_family = AMENITY_FAMILY.get(_amenity_key(amenity))

    own_words = tuple(TYPE_FAMILIES.get(own_family, ())) if own_family else ()
    amenity_word = _normalise(amenity.replace("_", " "))

    if amenity_word and f" {amenity_word} " in clean_name:
        return ""
    for word in own_words:
        if f" {_normalise(word)} " in clean_name:
            return ""

    # The name claims the requested type nowhere. If it names some other
    # type instead, that is a real contradiction rather than a guess.
    #
    # Word order deliberately plays no part here. "Hotel Classic Plaza at
    # Delhi Airport" and "Airport Hotel Grand" are both hotels, and any
    # rule that reads the last type word as the real one would keep one
    # and delete the other for no reason a person would accept.
    for family, words in TYPE_FAMILIES.items():
        if family == own_family:
            continue
        for word in words:
            if f" {_normalise(word)} " in clean_name:
                return word

    return ""


def _name_has_conflict(clean_name: str, amenity_tokens: tuple) -> bool:
    """True when the name carries a different business type than requested."""
    for word in NAME_CONFLICT_WORDS:
        if word in amenity_tokens:
            continue
        if f" {word} " in f" {clean_name} ":
            return True
    return False


def _amenity_key(amenity: str) -> str:
    """
    Canonical lookup key for an amenity.

    The tables are keyed "post_office", but a job file may say
    "Post Office " with a capital and a trailing space. Without folding
    those together every synonym is missed silently and the gate falls
    back to bare word matching, so "Movie theater" fails to satisfy
    "Cinema".
    """
    return "_".join(_normalise(amenity).split())


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


# Buttons Google renders in the category slot when a listing is missing
# data. Reading one of these as a category is what deleted real hotels.
_PRICE_PATTERN = re.compile(
    r"^([₹$€£¥]\s*[\d,]+|Rs\.?\s*[\d,]+|INR\s*[\d,]+|\d+\s*[₹$€£¥])",
    re.IGNORECASE,
)
_GENERIC_PLACEHOLDERS = {
    "unknown", "n/a", "na", "null", "none", "unnamed", "undefined",
    "place", "amenity", "test", "demo", "untitled", "dot",
}
_UI_ACTION_WORDS = {
    "sponsored", "ad", "ad ·", "results for", "directions", "website",
    "search this area", "suggest an edit", "you've reached the end",
    "view larger map", "see more", "add a missing place", "add missing information",
    "claim this business", "write a review",
}


def _is_valid_place_name(name: Any) -> bool:
    """
    Validate that a scraped string represents a genuine place/business name.
    Rejects:
      - Hotel price badges (e.g. '₹2,440', '$120', 'Rs. 1500')
      - Pure numbers, prices, or currency fragments
      - Short or punctuation-only strings ('.', '-', etc.)
      - Generic placeholder names ('Unknown', 'N/A', 'none', 'null')
      - Google Maps UI labels ('Sponsored', 'Ad', 'Results for...', 'Directions')
    """
    if not name or not isinstance(name, str):
        return False
    clean = name.strip()
    if len(clean) < 3:
        return False
    # Must contain at least one alphabetic or script character (not pure numbers/symbols)
    if not any(c.isalpha() for c in clean):
        return False
    # Reject currency/price badge patterns
    if _PRICE_PATTERN.match(clean):
        return False
    lower = clean.lower()
    if lower in _GENERIC_PLACEHOLDERS:
        return False
    if lower in _UI_ACTION_WORDS:
        return False
    for prefix in ("results for ", "showing results for ", "sponsored ·", "ad ·", "search for "):
        if lower.startswith(prefix):
            return False
    # Punctuation/symbols only
    if re.fullmatch(r"[\s\.\-_/\\,;:!?'\"#%&*+=@^`~|]+", clean):
        return False
    return True


is_valid_place_name = _is_valid_place_name


CATEGORY_ACTION_LABELS = (
    "add website", "add place", "add a label", "add missing information",
    "add phone", "add hours", "add business hours", "add photo",
    "add photos", "claim this business", "suggest an edit",
    "write a review", "send to phone", "share", "directions",
    "save", "nearby", "order online", "book online", "website",
)


def _looks_like_action_label(text: str) -> bool:
    """True for UI affordances masquerading as a category."""
    clean = _normalise(text)
    if not clean:
        return True
    if clean.startswith("add "):
        return True
    return clean in CATEGORY_ACTION_LABELS


# Strings Google puts where a category would sit, that are not categories.
CATEGORY_NOISE = (
    "no reviews", "reviews", "open", "closed", "opens", "closes",
    "temporarily closed", "permanently closed", "open 24 hours",
    "sponsored", "ad", "hours might differ", "price",
)


def _is_noise_category(category: str) -> bool:
    """True when the scraped 'category' is rating/status text, not a type."""
    clean = _normalise(category)
    if not clean:
        return True
    if _looks_like_action_label(category):
        return True
    if clean in CATEGORY_NOISE:
        return True
    # Bare numbers, prices, or star counts.
    if not any(character.isalpha() for character in clean):
        return True
    return False


def _hit_category(amenity: str, category: str) -> bool:
    """Match an amenity against a category string only, ignoring the name."""
    clean_category = _normalise(category)
    if not clean_category:
        return False

    for synonym in CATEGORY_SYNONYMS.get(_amenity_key(amenity), ()):
        if _normalise(synonym) in clean_category:
            return True

    tokens = [t for t in _normalise(amenity.replace("_", " ")).split() if t]
    return bool(tokens) and all(
        token in clean_category for token in tokens
    )


def _category_matches(
    amenity: str,
    category: str,
    name: str,
    area: str,
) -> bool:
    """Backwards-compatible wrapper returning only the verdict."""
    return _classify(amenity, category, name, area)[0]


def _classify(
    amenity: str,
    category: str,
    name: str,
    area: str,
) -> tuple[bool, str]:
    """
    Decide whether a result really is the requested amenity type.

    Returns (keep, reason). Reason is one of:
      "match"            - category or name confirms the type
      "is_area"          - the result is the search area itself
      "type_mismatch"    - a real category was read and it disagrees
      "unverified"       - no usable category; kept, needs place-page check
    """
    clean_name = _normalise(name)
    clean_area = _normalise(area)

    # The area itself is a locality, never an amenity.
    if clean_name and clean_name == clean_area:
        return False, "is_area"

    tokens = [t for t in _normalise(amenity.replace("_", " ")).split() if t]
    synonyms = CATEGORY_SYNONYMS.get(_amenity_key(amenity), ())

    def _hit(haystack: str) -> bool:
        for synonym in synonyms:
            if _normalise(synonym) in haystack:
                return True
        return bool(tokens) and all(token in haystack for token in tokens)

    # A name may confirm the type, but only when it carries no competing
    # business word. "Chandigarh Airport" counts; "Noida Airport Inn"
    # does not, and must be settled by the place page.
    if _hit(clean_name) and not _name_has_conflict(clean_name, tuple(tokens)):
        return True, "match"

    if _is_noise_category(category):
        return True, "unverified"

    # A real category is authoritative and is judged on its own. Folding
    # the name into the haystack let "Noida Airport Inn" satisfy an
    # airport search on the strength of its own name while its category
    # plainly read "Hotel".
    if _hit_category(amenity, category):
        return True, "match"

    return False, "type_mismatch"


# ─────────────────────────────────────────────
#  OVERPASS FALLBACK
# ─────────────────────────────────────────────

def _is_within_bbox(lat: float, lng: float, bbox: tuple) -> bool:
    """Check if coordinates are within bbox (s, n, w, e) - works for ANY city."""
    if not bbox or len(bbox) != 4:
        return True  # No bbox = accept all
    s, n, w, e = bbox
    return float(s) <= lat <= float(n) and float(w) <= lng <= float(e)


def _filter_results_by_bbox(results: list, bbox: tuple) -> list:
    """Filter results by geographic bbox - DYNAMIC for any city."""
    if not bbox:
        return results
    return [r for r in results if _is_within_bbox(
        r.get("latitude", 0), r.get("longitude", 0), bbox
    )]


def _overpass_request(query: str, log) -> list:
    """
    Run an Overpass query against each mirror in turn.

    Returns an empty list rather than raising: a fallback that throws
    turns a recoverable miss into a crash further up the call stack.
    """
    last_error = ""

    for attempt, endpoint in enumerate(OVERPASS_MIRRORS, 1):
        try:
            response = requests.post(
                endpoint,
                data={"data": query},
                headers=HEADERS,
                timeout=90,
            )
            response.raise_for_status()
            return response.json().get("elements", [])
        except Exception as error:
            last_error = str(error)[:80]
            host = endpoint.split("/")[2]
            log(f"   ⚠️  Overpass mirror {attempt} ({host}) failed: "
                f"{last_error}", "warn")
            if attempt < len(OVERPASS_MIRRORS):
                time.sleep(2.0 * attempt)

    log(f"   ❌ All Overpass mirrors failed: {last_error}", "error")
    return []


def _sanitise_overpass(text: str) -> str:
    """Strip characters that are special in Overpass QL to prevent injection."""
    return re.sub(r'["\[\];\\]', '', str(text or '').strip())


def _overpass_search(amenity: str, area: str,
                     limit: int, log, district: str = None) -> list:
    amenity = _sanitise_overpass(amenity)
    area_id = _get_area_id(area, district=district)
    bbox_filter = _get_bbox(area, district=district)  # ALWAYS capture bbox

    extra_clauses = ""
    if amenity in ("banquet_hall", "marriage_hall", "wedding_venue", "party_hall"):
        extra_clauses = """
  node["amenity"="events_venue"](area.searchArea);
  way["amenity"="events_venue"](area.searchArea);
  node["community_centre"~"marriage_hall|banquet_hall"](area.searchArea);
  way["community_centre"~"marriage_hall|banquet_hall"](area.searchArea);
"""

    if area_id:
        query = f"""
[out:json][timeout:60];
area({area_id})->.searchArea;
(
  node["amenity"="{amenity}"](area.searchArea);
  way["amenity"="{amenity}"](area.searchArea);
  relation["amenity"="{amenity}"](area.searchArea);
{extra_clauses}

  node["shop"="{amenity}"](area.searchArea);
  way["shop"="{amenity}"](area.searchArea);
  relation["shop"="{amenity}"](area.searchArea);

  node["tourism"="{amenity}"](area.searchArea);
  way["tourism"="{amenity}"](area.searchArea);
  relation["tourism"="{amenity}"](area.searchArea);

  node["leisure"="{amenity}"](area.searchArea);
  way["leisure"="{amenity}"](area.searchArea);
  relation["leisure"="{amenity}"](area.searchArea);

  node["historic"="{amenity}"](area.searchArea);
  way["historic"="{amenity}"](area.searchArea);
  relation["historic"="{amenity}"](area.searchArea);

  node["natural"="{amenity}"](area.searchArea);
  way["natural"="{amenity}"](area.searchArea);
  relation["natural"="{amenity}"](area.searchArea);

  node["man_made"="{amenity}"](area.searchArea);
  way["man_made"="{amenity}"](area.searchArea);
  relation["man_made"="{amenity}"](area.searchArea);
);
out center {limit};
"""
    else:
        if not bbox_filter:
            return []
        s, n, w, e = bbox_filter
        extra_bbox = ""
        if amenity in ("banquet_hall", "marriage_hall", "wedding_venue", "party_hall"):
            extra_bbox = f"""
  node["amenity"="events_venue"]({s},{w},{n},{e});
  way["amenity"="events_venue"]({s},{w},{n},{e});
  node["community_centre"~"marriage_hall|banquet_hall"]({s},{w},{n},{e});
  way["community_centre"~"marriage_hall|banquet_hall"]({s},{w},{n},{e});
"""
        query = f"""
[out:json][timeout:60];
(
   node["amenity"="{amenity}"]({s},{w},{n},{e});
  way["amenity"="{amenity}"]({s},{w},{n},{e});
  relation["amenity"="{amenity}"]({s},{w},{n},{e});
{extra_bbox}

  node["shop"="{amenity}"]({s},{w},{n},{e});
  way["shop"="{amenity}"]({s},{w},{n},{e});
  relation["shop"="{amenity}"]({s},{w},{n},{e});

  node["tourism"="{amenity}"]({s},{w},{n},{e});
  way["tourism"="{amenity}"]({s},{w},{n},{e});
  relation["tourism"="{amenity}"]({s},{w},{n},{e});

  node["leisure"="{amenity}"]({s},{w},{n},{e});
  way["leisure"="{amenity}"]({s},{w},{n},{e});
  relation["leisure"="{amenity}"]({s},{w},{n},{e});

  node["historic"="{amenity}"]({s},{w},{n},{e});
  way["historic"="{amenity}"]({s},{w},{n},{e});
  relation["historic"="{amenity}"]({s},{w},{n},{e});

  node["natural"="{amenity}"]({s},{w},{n},{e});
  way["natural"="{amenity}"]({s},{w},{n},{e});
  relation["natural"="{amenity}"]({s},{w},{n},{e});

  node["man_made"="{amenity}"]({s},{w},{n},{e});
  way["man_made"="{amenity}"]({s},{w},{n},{e});
  relation["man_made"="{amenity}"]({s},{w},{n},{e});
);
out center {limit};
"""

    elements = _overpass_request(query, log)
    if not elements:
        return []

    results = []
    for el in elements:
        tags = el.get("tags", {})
        name = (tags.get("name") or tags.get("name:en") or
                tags.get("operator") or "")
        if not name or not _is_valid_place_name(name):
            continue
        if el.get("type") == "node":
            lat, lng = el.get("lat"), el.get("lon")
        else:
            c = el.get("center", {})
            lat, lng = c.get("lat"), c.get("lon")
        if not lat or not lng:
            continue
        addr_parts = [tags.get(k) for k in [
            "addr:housenumber", "addr:street",
            "addr:suburb", "addr:city"
        ] if tags.get(k)]
        results.append({
            "name":          name,
            "amenity":       amenity,
            "latitude":      round(lat, 7),
            "longitude":     round(lng, 7),
            "address":       ", ".join(addr_parts) or "N/A",
            "rating":        "N/A",
            "phone":         tags.get("phone", "N/A"),
            "opening_hours": tags.get("opening_hours", "N/A"),
        })
    
    # Filter results by geographic bbox (works for any city)
    results = _filter_results_by_bbox(results, bbox_filter)
    return results


def _get_area_id(area: str, district: str | None = None, country_code: str | None = None) -> str | None:
    """Get area ID from Nominatim, with optional district filter."""
    try:
        # Build query: "area, district" if district provided
        search_query = f"{area}, {district}" if district else area
        cc = (country_code or DEFAULT_COUNTRY_CODE).strip().lower()
        
        params = {"q": search_query, "format": "json", "limit": 10}
        if cc:
            params["countrycodes"] = cc

        resp = requests.get(NOMINATIM_URL, params=params,
                            headers=HEADERS, timeout=15)
        places = resp.json()
        
        # Sort by importance (highest first)
        places.sort(
            key=lambda p: float(p.get("importance", 0)),
            reverse=True
        )
        
        # Prefer relation types
        for place in places:
            if place.get("osm_type") == "relation":
                return str(int(place["osm_id"]) + 3600000000)
        
    except Exception:
        pass
    return None


def _get_bbox(area: str, district: str | None = None, country_code: str | None = None) -> tuple[float, float, float, float] | None:
    """Get bbox from Nominatim, with optional district filter."""
    try:
        # Build query: "area, district" if district provided
        search_query = f"{area}, {district}" if district else area
        cc = (country_code or DEFAULT_COUNTRY_CODE).strip().lower()
        
        params = {"q": search_query, "format": "json", "limit": 10}
        if cc:
            params["countrycodes"] = cc

        resp = requests.get(NOMINATIM_URL, params=params,
                            headers=HEADERS, timeout=15)
        data = resp.json()
        
        if not data:
            return None
        
        # Sort by importance (highest first)
        data.sort(
            key=lambda p: float(p.get("importance", 0)),
            reverse=True
        )
        
        # Return bbox of most important result
        for place in data:
            bb = place.get("boundingbox", [])
            if len(bb) == 4:
                return tuple(float(v) for v in bb)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
#  EXPORT FUNCTIONS
# ─────────────────────────────────────────────

def save_amenities_to_excel(
    results: list,
    amenity: str,
    area: str,
    output_path: str,
    run_id: str | None = None,
) -> str:
    output_file = Path(output_path).resolve()
    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        from utils.db_handler import save_amenity_results

        enriched = []

        for result in results:
            record = result.copy()

            if not record.get("amenity"):
                record["amenity"] = amenity

            if not record.get("area"):
                record["area"] = area

            if run_id and not record.get("run_id"):
                record["run_id"] = run_id

            enriched.append(record)

        save_amenity_results(enriched, run_id=run_id)

    except Exception as db_error:
        print(f"DB save failed: {db_error}")

    import pandas as pd

    records_to_save = enriched if "enriched" in locals() and enriched else results
    dataframe = pd.DataFrame(records_to_save)
    if run_id and "run_id" not in dataframe.columns:
        dataframe["run_id"] = run_id

    columns = [
        "run_id",
        "name",
        "amenity",
        "category",
        "area",
        "district",
        "latitude",
        "longitude",
        "distance_km",
        "address",
        "rating",
        "phone",
        "opening_hours",
        "coordinate_source",
        "category_source",
        "aliases",
        "maps_url",
    ]

    columns = [
        column
        for column in columns
        if column in dataframe.columns
    ]

    dataframe = dataframe[columns]

    dataframe.columns = [
        "Run ID" if column == "run_id" else column.replace("_", " ").title()
        for column in columns
    ]

    with pd.ExcelWriter(
        output_file,
        engine="openpyxl",
    ) as writer:
        dataframe.to_excel(
            writer,
            sheet_name="Amenities",
            index=False,
        )

        worksheet = writer.sheets["Amenities"]

        for column in worksheet.columns:
            maximum_length = max(
                len(str(cell.value))
                if cell.value is not None
                else 0
                for cell in column
            )

            worksheet.column_dimensions[
                column[0].column_letter
            ].width = min(
                maximum_length + 4,
                50,
            )

    print(f"Excel saved: {output_file}")
    print(f"File exists: {output_file.exists()}")

    return str(output_file)






def save_amenities_to_kml(results: list, amenity: str,
                           area: str, output_path: str) -> str:
    def esc(t):
        return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    placemarks = []
    for r in results:
        if not r.get("latitude") or not r.get("longitude"):
            continue
        desc = ""
        if r.get("address") and r["address"] != "N/A":
            desc += f"Address: {esc(r['address'])}&#10;"
        if r.get("rating") and r["rating"] != "N/A":
            desc += f"Rating: {esc(r['rating'])}&#10;"
        if r.get("phone") and r["phone"] != "N/A":
            desc += f"Phone: {esc(r['phone'])}&#10;"
        if r.get("opening_hours") and r["opening_hours"] != "N/A":
            desc += f"Hours: {esc(r['opening_hours'])}"
        placemarks.append(f"""  <Placemark>
    <name>{esc(r['name'])}</name>
    <description>{desc}</description>
    <styleUrl>#pin</styleUrl>
    <Point>
      <coordinates>{r['longitude']},{r['latitude']},0</coordinates>
    </Point>
  </Placemark>""")

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{esc(amenity.title())} in {esc(area)}</name>
  <Style id="pin">
    <IconStyle>
      <color>ff0000ff</color><scale>1.0</scale>
      <Icon>
        <href>http://maps.google.com/mapfiles/ms/icons/red-dot.png</href>
      </Icon>
    </IconStyle>
  </Style>
{chr(10).join(placemarks)}
</Document>
</kml>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(kml)
    print(f"   💾 KML: {output_path}")
    return output_path


# ─────────────────────────────────────────────
#  PUBLIC SEARCH ALIASES
# ─────────────────────────────────────────────
search_amenities_selenium = _selenium_search
search_amenities_overpass = _overpass_search