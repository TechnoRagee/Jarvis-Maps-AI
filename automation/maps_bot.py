"""
maps_bot.py

Selenium helper for Google Maps coordinate and address extraction.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Iterable
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# Single switch for the whole app. Default is Headless OFF (browser visible).
# Set MAPS_BOT_HEADLESS=1 in the environment to run headlessly.
HEADLESS = os.getenv("MAPS_BOT_HEADLESS", "0").strip() in ("1", "true", "True")
PAGE_LOAD_TIMEOUT = 40
SCRIPT_TIMEOUT = 25
ELEMENT_WAIT_TIMEOUT = 15
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720
MAPS_URL = "https://www.google.com/maps"

# @lat,lng is the map viewport centre, NOT the place pin. Results that fall back
# to it are tagged coordinate_source="viewport_center" -- treat as approximate.
# Set MAPS_BOT_STRICT_COORDS=1 to reject them and fail instead.
ALLOW_VIEWPORT_FALLBACK = os.getenv(
    "MAPS_BOT_STRICT_COORDS", "0"
).strip() not in ("1", "true", "True")


if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def setup_driver(
    *,
    headless: bool | None = None,
    lightweight: bool = False,
    width: int = WINDOW_WIDTH,
    height: int = WINDOW_HEIGHT,
) -> webdriver.Chrome:
    """
    Create Chrome WebDriver.

    lightweight=True disables image loading and is useful for bulk coordinate
    extraction. Keep it False when screenshots or map imagery are required.
    """
    use_headless = HEADLESS if headless is None else headless

    options = Options()
    options.page_load_strategy = "eager"

    if use_headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        # Google serves a degraded/consent-heavy page to the HeadlessChrome UA.
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )

    options.add_argument("--disable-dev-shm-usage")

    options.add_argument(f"--window-size={width},{height}")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-sync")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--no-first-run")
    options.add_argument("--log-level=3")

    options.add_experimental_option(
        "excludeSwitches",
        ["enable-logging"],
    )

    preferences = {
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
    }

    if lightweight:
        preferences["profile.managed_default_content_settings.images"] = 2

    options.add_experimental_option("prefs", preferences)

    driver = webdriver.Chrome(
        service=Service(log_output=os.devnull),
        options=options,
    )
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(SCRIPT_TIMEOUT)
    driver.set_window_size(width, height)
    return driver


def dismiss_consent_popup(driver: webdriver.Chrome) -> bool:
    selectors = [
        "button[aria-label='Accept all']",
        "button[jsname='higCR']",
        "form[action*='consent'] button",
    ]

    for selector in selectors:
        try:
            button = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            button.click()
            time.sleep(0.8)
            return True
        except Exception:
            continue

    return False


def extract_coordinates_from_url(
    url: str,
    *,
    allow_viewport_fallback: bool = ALLOW_VIEWPORT_FALLBACK,
) -> tuple[float | None, float | None, str]:
    """
    Return latitude, longitude and coordinate source.
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
        viewport_match = re.search(
            r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
            url,
        )
        if viewport_match:
            return (
                float(viewport_match.group(1)),
                float(viewport_match.group(2)),
                "viewport_center",
            )

    return None, None, "not_found"


def extract_address(driver: webdriver.Chrome) -> str:
    selectors = [
        "button[data-item-id='address'] .fontBodyMedium",
        "button[data-item-id='address']",
        "[data-tooltip='Copy address']",
        ".rogA2c .fontBodyMedium",
        "button[aria-label*='Address']",
        ".Io6YTe.fontBodyMedium.kR99db.fdkmkc",
        ".Io6YTe.fontBodyMedium",
    ]

    for selector in selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                address = element.text.strip()
                if address:
                    return address

                aria_label = (element.get_attribute("aria-label") or "").strip()
                if aria_label:
                    cleaned = re.sub(
                        r"^Address:\s*",
                        "",
                        aria_label,
                        flags=re.IGNORECASE,
                    ).strip()
                    if cleaned:
                        return cleaned
        except Exception:
            continue

    return "Address not found"


def _page_indicates_no_result(driver: webdriver.Chrome) -> bool:
    try:
        page_text = driver.page_source.lower()
    except Exception:
        return False

    return any(
        phrase in page_text
        for phrase in (
            "can't find",
            "couldn't find",
            "no results found",
            "did not match any locations",
        )
    )


def _wait_for_maps_result(driver: webdriver.Chrome) -> None:
    WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT).until(
        lambda current_driver: (
            "!3d" in current_driver.current_url
            or bool(
                current_driver.find_elements(
                    By.CSS_SELECTOR,
                    "h1.DUwDvf, h1.fontHeadlineLarge",
                )
            )
            or bool(
                current_driver.find_elements(
                    By.CSS_SELECTOR,
                    "button[data-item-id='address']",
                )
            )
        )
    )


def _search_google_maps_once(
    driver: webdriver.Chrome,
    query: str,
) -> dict:
    search_url = (
        f"{MAPS_URL}/search/"
        f"?api=1&query={quote_plus(query)}"
    )
    print(f"   Searching URL: {search_url}")

    try:
        driver.get(search_url)
    except TimeoutException:
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass

    dismiss_consent_popup(driver)

    try:
        _wait_for_maps_result(driver)
    except TimeoutException:
        pass

    latitude = None
    longitude = None
    coordinate_source = "not_found"

    if _page_indicates_no_result(driver):
        print(f"   Google Maps reported no result for: {query}")
        return {
            "place": query,
            "latitude": None,
            "longitude": None,
            "address": "Address not found",
            "status": "Failed",
            "coordinate_source": "no_result",
            "url": driver.current_url,
        }

    for _ in range(20):
        latitude, longitude, coordinate_source = extract_coordinates_from_url(
            driver.current_url
        )

        if latitude is not None and longitude is not None:
            break

        time.sleep(0.5)

    if latitude is None or longitude is None:
        print(
            f"   Final Google Maps URL: "
            f"{driver.current_url}"
        )

    address = (
        extract_address(driver)
        if latitude is not None and longitude is not None
        else "Address not found"
    )

    return {
        "place": query,
        "latitude": latitude,
        "longitude": longitude,
        "address": address,
        "status": (
            "Success"
            if latitude is not None and longitude is not None
            else "Failed"
        ),
        "coordinate_source": coordinate_source,
        "url": driver.current_url,
    }


def search_location(
    driver: webdriver.Chrome,
    query: str,
    *,
    retries: int = 2,
    retry_delay: float = 2.0,
) -> dict:
    query = query.strip()

    if not query:
        return {
            "place": "",
            "latitude": None,
            "longitude": None,
            "address": "Address not found",
            "status": "Failed",
            "coordinate_source": "not_found",
            "url": "",
            "error": "Query is empty.",
        }

    last_error = ""

    for attempt in range(1, retries + 2):
        try:
            result = _search_google_maps_once(driver, query)

            if result["status"] == "Success":
                print(
                    f"   Coordinates: {result['latitude']}, "
                    f"{result['longitude']}"
                )
                print(f"   Source: {result['coordinate_source']}")
                print(f"   Address: {result['address']}")
                return result

            last_error = "Coordinates not found."

        except Exception as exc:
            last_error = str(exc)[:200]
            print(f"   Attempt {attempt} failed: {last_error}")

        if attempt <= retries:
            time.sleep(retry_delay)

    print(f"   Coordinates not found for: {query}")

    try:
        final_url = driver.current_url if driver else ""
    except Exception:
        final_url = ""

    return {
        "place": query,
        "latitude": None,
        "longitude": None,
        "address": "Address not found",
        "status": "Failed",
        "coordinate_source": "not_found",
        "url": final_url,
        "error": last_error,
    }


def search_multiple_locations(
    queries: Iterable[str],
    *,
    headless: bool | None = None,
    lightweight: bool = False,
    retries: int = 2,
) -> list[dict]:
    """
    Reuse one browser for all supplied location queries.
    """
    driver = setup_driver(
        headless=headless,
        lightweight=lightweight,
    )
    results: list[dict] = []

    try:
        for query in queries:
            clean_query = str(query).strip()
            if not clean_query:
                continue

            results.append(
                search_location(
                    driver,
                    clean_query,
                    retries=retries,
                )
            )
    finally:
        try:
            driver.quit()
        except Exception:
            pass

        print("\nBrowser closed.")

    return results


if __name__ == "__main__":
    sample_queries = [
        "India Gate, New Delhi",
        "Sector 62, Noida, Uttar Pradesh",
    ]

    for result in search_multiple_locations(
        sample_queries,
        lightweight=False,
        retries=1,
    ):
        print(result)