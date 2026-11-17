"""
main.py
-------
Jarvis Maps Automation — Entry Point

Run this file to start the bot.
On launch, you choose the mode:
  [1] Single Mode  — type one location, get results instantly
  [2] Batch Mode   — reads all locations from input.xlsx, processes all
  [3] Setup        — creates a sample input.xlsx to get you started
"""

from automation.maps_bot import setup_driver, search_location, search_multiple_locations
from utils.excel_handler import save_to_excel, read_locations_from_excel, create_sample_input_file


def print_banner():
    print("""
╔══════════════════════════════════════════════╗
║        🗺️   JARVIS MAPS AUTOMATION           ║
║     Selenium + Python + Google Maps          ║
╚══════════════════════════════════════════════╝
    """)


def run_single_mode():
    print("\n📍 SINGLE MODE — Type a location to search\n")
    all_results = []
    driver = setup_driver()

    try:
        while True:
            query = input("Enter location (or 'done' to finish): ").strip()

            if query.lower() in ("done", "exit", "quit", "q"):
                break

            if not query:
                print("⚠️  Please enter a location name.")
                continue

            result = search_location(driver, query)

            if result is None:
                print("⚠️  No result returned, skipping.")
                continue

            all_results.append(result)

            print(f"\n   📊 Result Summary:")
            print(f"      Place     : {result['place']}")
            print(f"      Latitude  : {result['latitude']}")
            print(f"      Longitude : {result['longitude']}")
            print(f"      Address   : {result['address']}")
            print(f"      Status    : {result['status']}")
            print()

    finally:
        driver.quit()
        print("🔒 Browser closed.\n")

    if all_results:
        save_to_excel(all_results)
    else:
        print("ℹ️  No data collected.")


def run_batch_mode():
    print("\n📋 BATCH MODE — Reading locations from input.xlsx\n")
    locations = read_locations_from_excel()

    if not locations:
        print("❌ No locations to process. Exiting batch mode.")
        print("   Tip: Run option [3] Setup to create a sample input.xlsx")
        return

    print(f"\n   Found {len(locations)} location(s) to search:")
    for i, loc in enumerate(locations, 1):
        print(f"   {i}. {loc}")

    confirm = input(f"\nProceed with searching all {len(locations)} location(s)? (y/n): ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    results = search_multiple_locations(locations)

    print("\n" + "─" * 70)
    print(f"{'PLACE':<30} {'LAT':<12} {'LNG':<12} {'STATUS'}")
    print("─" * 70)
    for r in results:
        print(f"{r['place'][:28]:<30} {str(r['latitude']):<12} {str(r['longitude']):<12} {r['status']}")
    print("─" * 70)

    save_to_excel(results)


def run_setup():
    print("\n⚙️  SETUP — Creating sample input.xlsx\n")
    create_sample_input_file()
    print("\n   Open input.xlsx, replace the sample locations with your own,")
    print("   then come back and run Batch Mode [2].")


def main():
    print_banner()

    while True:
        print("Select Mode:")
        print("  [1] Single Mode   — search one location at a time")
        print("  [2] Batch Mode    — process all locations from input.xlsx")
        print("  [3] Setup         — create a sample input.xlsx file")
        print("  [0] Exit")
        print()

        choice = input("Enter choice: ").strip()

        if choice == "1":
            run_single_mode()
        elif choice == "2":
            run_batch_mode()
        elif choice == "3":
            run_setup()
        elif choice == "0":
            print("\n👋 Goodbye!\n")
            break
        else:
            print("⚠️  Invalid choice. Please enter 1, 2, 3, or 0.\n")


if __name__ == "__main__":
    main()
