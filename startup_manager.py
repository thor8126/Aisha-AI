r"""
startup_manager.py — Windows Startup & Background Service Controller for Aisha AI.
Manages:
- Windows Registry Auto-Start (HKCU\Software\Microsoft\Windows\CurrentVersion\Run)
- Silent Background VBScript Launcher generation
- Desktop & Start Menu Shortcuts
"""

from __future__ import annotations

import os
import sys
import winreg

APP_NAME = "AishaAI"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def get_silent_launcher_path() -> str:
    """Path to the silent VBS launcher."""
    return os.path.join(BASE_DIR, "AishaSilent.vbs")


def generate_silent_launcher() -> str:
    """Generate a VBS launcher that runs Aisha completely silently in background without a black CMD box."""
    vbs_path = get_silent_launcher_path()
    python_exe = sys.executable
    # Prefer pythonw.exe if available for zero-console execution
    pythonw_exe = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
    if not os.path.exists(pythonw_exe):
        pythonw_exe = python_exe

    aisha_script = os.path.join(BASE_DIR, "watchdog.py")

    # Fallback to aisha.py directly if watchdog.py doesn't exist
    if not os.path.exists(aisha_script):
        aisha_script = os.path.join(BASE_DIR, "aisha.py")

    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "{BASE_DIR}"
WshShell.Run """{pythonw_exe}"" ""{aisha_script}""", 0, False
'''
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(vbs_content)

    return vbs_path


def is_startup_enabled() -> bool:
    """Check if Aisha is set to run automatically on Windows startup."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val and ("AishaSilent.vbs" in val or "aisha.py" in val))
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def enable_startup() -> bool:
    """Register Aisha into Windows CurrentVersion\\Run registry."""
    try:
        launcher_path = generate_silent_launcher()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE)
        # Wrap in wscript for .vbs execution
        cmd = f'wscript.exe "{launcher_path}"'
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"Error enabling auto-start: {e}")
        return False


def disable_startup() -> bool:
    """Remove Aisha from Windows CurrentVersion\\Run registry."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"Error disabling auto-start: {e}")
        return False


def toggle_startup() -> bool:
    """Toggle auto-start state and return new state."""
    if is_startup_enabled():
        disable_startup()
        return False
    else:
        enable_startup()
        return True


def create_desktop_shortcut() -> bool:
    """Create a desktop shortcut to launch Aisha silently."""
    try:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.exists(desktop):
            return False

        launcher_path = generate_silent_launcher()
        shortcut_vbs = os.path.join(desktop, "Aisha AI.vbs")
        
        content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "{BASE_DIR}"
WshShell.Run "wscript.exe ""{launcher_path}""", 0, False
'''
        with open(shortcut_vbs, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"Error creating desktop shortcut: {e}")
        return False


if __name__ == "__main__":
    generate_silent_launcher()
    status = is_startup_enabled()
    print(f"Aisha Auto-Start Status: {'[ENABLED]' if status else '[DISABLED]'}")
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ("--enable", "enable"):
            enable_startup()
            print("Auto-start on Windows boot has been [ENABLED]")
        elif arg in ("--disable", "disable"):
            disable_startup()
            print("Auto-start on Windows boot has been [DISABLED]")
        elif arg in ("--toggle", "toggle"):
            new_state = toggle_startup()
            print(f"Auto-start toggled to: {'[ENABLED]' if new_state else '[DISABLED]'}")
        elif arg in ("--shortcut", "shortcut"):
            create_desktop_shortcut()
            print("Desktop shortcut created successfully!")
