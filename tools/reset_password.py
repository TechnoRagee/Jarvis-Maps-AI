"""
Master Password Reset Utility for Jarvis Maps AI.
Run this script whenever you have forgotten your password.
"""
import sys
import os
import getpass

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.db_handler import (
    has_master_password,
    set_master_password,
    reset_master_password,
    set_owner_recovery_key,
    verify_owner_recovery_key,
)

def main():
    print("=" * 55)
    print("       JARVIS MAPS AI - PASSWORD RESET TOOL")
    print("=" * 55)

    print("\nSelect an option:")
    print("  [1] Set a new master password in this terminal")
    print("  [2] Completely clear password (next app launch asks to create new)")
    print("  [3] Set/Change Secret Owner Recovery Key (for Ctrl+Alt+R in EXE)")
    print("  [4] Cancel")

    choice = input("\nEnter choice [1/2/3/4]: ").strip()

    if choice == "1":
        new_pwd = getpass.getpass("Enter new password (min 3 chars): ").strip()
        if len(new_pwd) < 3:
            print("[ERROR] Password must be at least 3 characters long.")
            return
        confirm_pwd = getpass.getpass("Confirm new password: ").strip()
        if new_pwd != confirm_pwd:
            print("[ERROR] Passwords do not match.")
            return
        if set_master_password(new_pwd):
            print("\n[SUCCESS] Master password updated successfully!")
            print("You can now open 'python frontend.py' with your new password.")
        else:
            print("[ERROR] Failed to update password.")

    elif choice == "2":
        if reset_master_password():
            print("\n[SUCCESS] Master password has been cleared!")
            print("Next time you launch the application, you will be prompted to create a new password.")
        else:
            print("[ERROR] Failed to clear password.")

    elif choice == "3":
        new_key = getpass.getpass("Enter new confidential Owner Recovery Key (min 4 chars): ").strip()
        if len(new_key) < 4:
            print("[ERROR] Owner Recovery Key must be at least 4 characters long.")
            return
        confirm_key = getpass.getpass("Confirm new Owner Recovery Key: ").strip()
        if new_key != confirm_key:
            print("[ERROR] Keys do not match.")
            return
        if set_owner_recovery_key(new_key):
            print("\n[SUCCESS] Owner Recovery Key updated and hashed in database!")
            print("Use this key with Ctrl+Alt+R on the startup screen to reset passwords anytime.")
        else:
            print("[ERROR] Failed to save Owner Recovery Key.")

    else:
        print("Cancelled.")

if __name__ == "__main__":
    main()

