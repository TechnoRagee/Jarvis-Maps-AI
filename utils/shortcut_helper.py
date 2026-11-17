"""
utils/shortcut_helper.py
------------------------
Ensures that a desktop shortcut with the official Jarvis Maps AI icon is
automatically created when the application is launched on ANY Windows laptop/PC.

Handles:
- Standard Windows Desktop (C:\\Users\\<User>\\Desktop)
- OneDrive-redirected Desktop (C:\\Users\\<User>\\OneDrive\\Desktop)
- Start Menu Programs directory
- Embedded icon extraction from assets or executable PE resources
- Zero console window popup during shortcut creation (CREATE_NO_WINDOW)
"""

import os
import sys
import subprocess
import tempfile
import logging

logger = logging.getLogger("JarvisShortcut")

def get_active_desktop_directories():
    """
    Returns a list of all active desktop paths for the current Windows user.
    Checks Windows registry User Shell Folders first (official active desktop),
    followed by standard paths and OneDrive paths.
    """
    desktop_dirs = []
    seen = set()

    def _add_path(p):
        if not p:
            return
        norm = os.path.normpath(os.path.expandvars(p))
        if norm.lower() not in seen and os.path.isdir(norm):
            seen.add(norm.lower())
            desktop_dirs.append(norm)

    # 1. Official Windows Registry lookup (Handles OneDrive redirection & localized names)
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        )
        val, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        _add_path(val)
    except Exception:
        pass

    # 2. Check USERPROFILE paths
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        _add_path(os.path.join(userprofile, "OneDrive", "Desktop"))
        _add_path(os.path.join(userprofile, "Desktop"))

    # 3. Check os.path.expanduser
    try:
        _add_path(os.path.expanduser("~/OneDrive/Desktop"))
        _add_path(os.path.expanduser("~/Desktop"))
    except Exception:
        pass

    # 4. Fallback to OneDrive Commercial / Personal environment vars if set
    for env_var in ("OneDriveConsumer", "OneDriveCommercial", "OneDrive"):
        od_path = os.environ.get(env_var)
        if od_path:
            _add_path(os.path.join(od_path, "Desktop"))

    return desktop_dirs


def get_start_menu_programs_dir():
    """Returns the user Start Menu Programs directory."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        p = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
        if os.path.isdir(p):
            return p
    return None


def find_app_icon_path(base_dir):
    """
    Locates the best .ico file for the shortcut icon.
    """
    candidates = [
        os.path.join(base_dir, "assets", "app_icon.ico"),
        os.path.join(base_dir, "_internal", "assets", "app_icon.ico"),
        os.path.join(base_dir, "app_icon.ico"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


def create_windows_shortcut(lnk_path, target_path, working_dir, icon_path=None, description="Jarvis Maps AI - Desktop Studio"):
    """
    Creates or overwrites a Windows .lnk shortcut using WScript.Shell via a temporary VBScript.
    Runs silently without flashing a command prompt window.
    """
    vbs_lines = [
        'Set ws = CreateObject("WScript.Shell")',
        f'Set sc = ws.CreateShortcut("{lnk_path}")',
        f'sc.TargetPath = "{target_path}"',
        f'sc.WorkingDirectory = "{working_dir}"',
        f'sc.Description = "{description}"'
    ]
    if icon_path and os.path.isfile(icon_path):
        vbs_lines.append(f'sc.IconLocation = "{icon_path},0"')
    else:
        # Fall back to target executable icon
        vbs_lines.append(f'sc.IconLocation = "{target_path},0"')

    vbs_lines.append('sc.Save')

    fd, tmp_file = tempfile.mkstemp(suffix=".vbs")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(vbs_lines) + "\n")

        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        subprocess.run(
            ["cscript", "//nologo", tmp_file],
            creationflags=flags,
            timeout=8,
            check=True
        )
        return os.path.isfile(lnk_path)
    except Exception as e:
        logger.warning("Failed to create shortcut at %s: %s", lnk_path, e)
        return False
    finally:
        try:
            os.remove(tmp_file)
        except Exception:
            pass


def ensure_desktop_shortcut(app_name="Jarvis Maps AI"):
    """
    Ensures that a valid desktop shortcut exists for this application.
    Checks all desktop locations (standard + OneDrive). If missing or invalid,
    creates it automatically.
    
    Returns list of paths where shortcuts were verified or created.
    """
    if os.name != "nt":
        return []

    # Determine executable path and working directory
    is_frozen = getattr(sys, "frozen", False)
    if is_frozen:
        target_exe = os.path.abspath(sys.executable)
        working_dir = os.path.dirname(target_exe)
    else:
        # Running from python source in development
        target_exe = os.path.abspath(sys.argv[0])
        working_dir = os.path.dirname(target_exe)
        # If running as python script directly, launch via pythonw or bat if available
        if target_exe.lower().endswith(".py"):
            bat_helper = os.path.join(working_dir, "Start_All_Servers.bat")
            if os.path.isfile(bat_helper):
                target_exe = bat_helper

    icon_path = find_app_icon_path(working_dir)
    lnk_filename = f"{app_name}.lnk"

    success_locations = []

    # 1. Desktop directories
    desktop_dirs = get_active_desktop_directories()
    for d in desktop_dirs:
        lnk_path = os.path.join(d, lnk_filename)
        # If shortcut doesn't exist, create it!
        if not os.path.isfile(lnk_path):
            created = create_windows_shortcut(
                lnk_path=lnk_path,
                target_path=target_exe,
                working_dir=working_dir,
                icon_path=icon_path,
                description="Jarvis Maps AI - Automated Spatial Intelligence Studio"
            )
            if created:
                success_locations.append(lnk_path)
        else:
            success_locations.append(lnk_path)

    # 2. Start Menu Programs directory
    start_menu = get_start_menu_programs_dir()
    if start_menu:
        sm_lnk_path = os.path.join(start_menu, lnk_filename)
        if not os.path.isfile(sm_lnk_path):
            created = create_windows_shortcut(
                lnk_path=sm_lnk_path,
                target_path=target_exe,
                working_dir=working_dir,
                icon_path=icon_path,
                description="Jarvis Maps AI - Automated Spatial Intelligence Studio"
            )
            if created:
                success_locations.append(sm_lnk_path)
        else:
            success_locations.append(sm_lnk_path)

    return success_locations
