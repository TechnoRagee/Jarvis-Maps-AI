"""
Jarvis Maps AI — Administrator License & IP Blacklist CLI Manager
Run this script to generate customer license keys, inspect blocked IPs, or remove IP bans.
"""
import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from license_server import load_db, save_db, ADMIN_SECRET_KEY, MASTER_PASSCODE
from datetime import timedelta
import secrets

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

def show_menu():
    print("\n" + "=" * 60)
    print("      JARVIS MAPS AI - SECURITY & LICENSE MANAGER")
    print("=" * 60)
    print(" [1] View All Issued Licenses")
    print(" [2] Generate New Customer License Key")
    print(" [3] View Blacklisted IPs (Auto-blocked attackers)")
    print(" [4] Unblock an IP Address")
    print(" [5] View & Unblock Banned Hardware IDs (HWIDs)")
    print(" [6] View Recent Security Audit Logs")
    print(" [7] Exit")
    print("=" * 60)

def main():
    while True:
        show_menu()
        choice = input("Enter choice [1-7]: ").strip()
        db = load_db()

        if choice == "1":
            licenses = db.get("licenses", {})
            print(f"\n--- ISSUED LICENSES ({len(licenses)}) ---")
            for key, data in licenses.items():
                devices = len(data.get("bound_hwids", []))
                max_dev = data.get("max_devices", 1)
                exp = data.get("expires_at") or "LIFETIME"
                print(f"Key: {key}")
                print(f"  Tier: {data.get('tier')} | Status: {data.get('status')} | Devices: {devices}/{max_dev}")
                print(f"  Expires: {exp} | Notes: {data.get('notes', 'N/A')}\n")

        elif choice == "2":
            print("\n--- GENERATE NEW LICENSE KEY ---")
            tier = input("Enter tier name [e.g. Pro Studio / Enterprise]: ").strip() or "Pro Studio"
            days_str = input("Validity in days (0 for Lifetime) [default 365]: ").strip() or "365"
            try:
                days = int(days_str)
            except ValueError:
                days = 365
            devices_str = input("Max Allowed Computers [default 1]: ").strip() or "1"
            try:
                devices = int(devices_str)
            except ValueError:
                devices = 1
            notes = input("Customer Name / Order ID: ").strip()

            entropy = secrets.token_hex(8).upper()
            new_key = f"JM-{entropy[:4]}-{entropy[4:8]}-{entropy[8:12]}-{entropy[12:16]}"
            expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat() if days > 0 else None

            db.setdefault("licenses", {})[new_key] = {
                "tier": tier,
                "status": "ACTIVE",
                "max_devices": devices,
                "bound_hwids": [],
                "created_at": datetime.utcnow().isoformat(),
                "expires_at": expires_at,
                "notes": notes
            }
            save_db(db)
            print(f"\n✅ LICENSE KEY CREATED SUCCESSFULLY:")
            print(f"   Key: {new_key}")
            print(f"   Tier: {tier} | Max PCs: {devices}")
            print(f"   Expires: {expires_at or 'LIFETIME'}")

        elif choice == "3":
            blocked_ips = db.get("blocked_ips", {})
            print(f"\n--- BLACKLISTED IP ADDRESSES ({len(blocked_ips)}) ---")
            if not blocked_ips:
                print("No IP addresses are currently blocked.")
            for ip, info in blocked_ips.items():
                print(f"IP: {ip}")
                print(f"  Blocked At: {info.get('blocked_at')}")
                print(f"  Reason: {info.get('reason')}")
                print(f"  Associated HWID: {info.get('last_hwid', 'N/A')}\n")

        elif choice == "4":
            target_ip = input("Enter IP Address to unblock: ").strip()
            if target_ip in db.get("blocked_ips", {}):
                db["blocked_ips"].pop(target_ip, None)
                db.get("failed_attempts", {}).pop(target_ip, None)
                save_db(db)
                print(f"✅ IP {target_ip} has been UNBLOCKED and removed from blacklist.")
            else:
                print(f"⚠️  IP {target_ip} is not currently blocked.")

        elif choice == "5":
            blocked_hwids = db.get("blocked_hwids", {})
            print(f"\n--- BANNED HARDWARE IDS ({len(blocked_hwids)}) ---")
            if not blocked_hwids:
                print("No hardware devices are currently banned.")
            for hwid, info in blocked_hwids.items():
                print(f"HWID: {hwid} | Blocked: {info.get('blocked_at')}")
            
            unblock_target = input("\nEnter HWID to unban (or leave blank to return): ").strip()
            if unblock_target and unblock_target in blocked_hwids:
                blocked_hwids.pop(unblock_target, None)
                db.get("failed_attempts", {}).pop(unblock_target, None)
                save_db(db)
                print(f"✅ HWID {unblock_target} unbanned!")

        elif choice == "6":
            logs = db.get("audit_logs", [])[-25:]
            print(f"\n--- RECENT SECURITY AUDIT LOGS ({len(logs)}) ---")
            for entry in logs:
                print(f"[{entry.get('timestamp')[:19]}] {entry.get('event')} | IP: {entry.get('ip')} | {entry.get('details')}")

        elif choice == "7":
            print("Exiting manager.")
            break

if __name__ == "__main__":
    main()
