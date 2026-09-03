"""
actions.py — Comprehensive Windows System Action Handlers for Aisha
Supports:
- Application Launch & Termination
- Media Playback & Volume Control (Play/Pause, Next, Prev, Volume %, Mute)
- Web & YouTube Search, URL Navigation
- Window Management (Show Desktop, Minimize, Close Window)
- System Power & Security (Lock Screen, Sleep, Shutdown, Restart)
- Screen Capture & Typing Automation
- System Information (Time, Date, Battery)
"""

import os
import re
import time
import json
import subprocess
import webbrowser
import urllib.parse
from datetime import datetime

# Common Windows application mapping
APP_MAP = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "browser": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "explorer": "explorer",
    "file explorer": "explorer",
    "files": "explorer",
    "cmd": "cmd",
    "command prompt": "cmd",
    "terminal": "wt",
    "windows terminal": "wt",
    "powershell": "powershell",
    "task manager": "taskmgr",
    "settings": "ms-settings:",
    "paint": "mspaint",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "spotify": "spotify",
    "discord": "discord",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "whatsapp": "whatsapp:",
    "whatsapp web": "https://web.whatsapp.com",
    "telegram": "telegram:",
    "spotify": "spotify:",
    "steam": "steam:",
}


def parse_action(response_text):
    """Try to extract a JSON action from the LLM response.
    Returns (action_dict, None) if valid, or (None, response_text) if it's just text.
    """
    text = response_text.strip()

    # 1. Direct JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "action" in data:
            return data, None
    except json.JSONDecodeError:
        pass

    # 2. Embedded JSON in markdown code blocks ```json ... ```
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and "action" in data:
                return data, None
        except json.JSONDecodeError:
            pass

    # 3. Embedded JSON { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict) and "action" in data:
                return data, None
        except json.JSONDecodeError:
            pass

    return None, text


def execute_action(action_data):
    """Execute a system action and return natural spoken Hindi confirmation."""
    action = action_data.get("action", "").lower().strip()
    params = action_data.get("params", {})

    try:
        # App Management
        if action == "open_app":
            return _open_app(params.get("name", ""))
        elif action == "close_app":
            return _close_app(params.get("name", ""))

        # Web & Search
        elif action == "open_url":
            return _open_url(params.get("url", ""))
        elif action == "web_search":
            return _web_search(params.get("query", ""))
        elif action == "youtube_search" or action == "play_music":
            return _youtube_search(params.get("query", "") or params.get("song", ""))

        # Media Control
        elif action == "media_play_pause":
            return _media_control("play_pause")
        elif action == "media_next":
            return _media_control("next")
        elif action == "media_prev":
            return _media_control("prev")
        elif action == "play_spotify":
            return _play_spotify(params.get("query", "") or params.get("song", ""))

        # Audio Volume
        elif action == "set_volume":
            return _set_volume(params.get("level", 50))
        elif action == "volume_up":
            return _volume_change("up")
        elif action == "volume_down":
            return _volume_change("down")
        elif action == "mute":
            return _mute()

        # Window & Desktop Management
        elif action == "show_desktop" or action == "minimize_all":
            return _show_desktop()
        elif action == "close_window":
            return _close_active_window()
        elif action == "maximize_window":
            return _snap_window("maximize")
        elif action == "minimize_window":
            return _snap_window("minimize")
        elif action == "snap_window":
            return _snap_window(params.get("position", "left"))
        elif action == "switch_app":
            return _switch_app()
        elif action == "screenshot":
            return _screenshot()

        # Typing & Keyboard
        elif action == "type_text":
            return _type_text(params.get("text", ""))

        # System & Info
        elif action == "get_time":
            return _get_time()
        elif action == "get_date":
            return _get_date()
        elif action == "lock_screen":
            return _lock_screen()
        elif action == "shutdown":
            return _shutdown()
        elif action == "restart":
            return _restart()
        elif action == "cancel_shutdown":
            return _cancel_shutdown()
        elif action == "open_folder":
            return _open_folder(params.get("name", "downloads"))

        else:
            return f"Arey, mujhe abhi {action} karna nahi aata."

    except Exception as e:
        return f"Arey yaar, kuch dikkat aa gayi: {str(e)}"


# --- Handlers ---

def _find_start_menu_shortcut(name):
    """Find a Start Menu .lnk matching the app/game name — this is how Windows
    itself resolves 'open X', so it covers almost every installed app and game."""
    needle = name.lower().strip()
    if not needle:
        return None
    roots = [
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    best = None
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                stem = f[:-4].lower()
                if needle in stem:
                    # Prefer the closest name match (exact > startswith > contains).
                    score = (0 if stem == needle else 1 if stem.startswith(needle) else 2, len(stem))
                    if best is None or score < best[0]:
                        best = (score, os.path.join(dirpath, f))
    return best[1] if best else None


def _find_game_exe(name):
    """Deep-search common game/app locations across all drives for a matching .exe
    (for games installed outside the Start Menu, e.g. on another drive)."""
    needle = name.lower().replace(" ", "").replace("-", "")
    if len(needle) < 3:
        return None
    skip = ("unins", "setup", "crash", "redist", "vcredist", "dxsetup", "installer",
            "helper", "update", "launcher.tmp", "notification")
    subdirs = [
        r"Program Files (x86)\Steam\steamapps\common",
        r"SteamLibrary\steamapps\common",
        r"Program Files\Epic Games",
        r"Program Files (x86)\Epic Games",
        r"Games", r"Program Files", r"Program Files (x86)",
    ]
    roots = []
    for letter in "CDEFGH":
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        for sub in subdirs:
            p = os.path.join(drive, sub)
            if os.path.isdir(p):
                roots.append(p)
    best = None
    for root in roots:
        try:
            base_depth = root.rstrip("\\").count(os.sep)
            for dirpath, dirs, files in os.walk(root):
                if dirpath.count(os.sep) - base_depth > 4:  # bound depth for speed
                    dirs[:] = []
                    continue
                for f in files:
                    if not f.lower().endswith(".exe"):
                        continue
                    stem = f[:-4].lower()
                    if any(s in stem for s in skip):
                        continue
                    if needle in stem.replace(" ", "").replace("-", ""):
                        score = (0 if stem == name.lower() else 1, len(stem))
                        if best is None or score < best[0]:
                            best = (score, os.path.join(dirpath, f))
        except Exception:
            continue
    return best[1] if best else None


def _open_app(name):
    if not name:
        return "Kaun sa app kholna hai, bataya nahi aapne?"

    app_key = name.lower().strip()
    executable = APP_MAP.get(app_key, app_key)

    # 1. Protocol or Windows Settings (e.g. ms-settings:, whatsapp:)
    if executable.startswith("ms-") or executable.endswith(":"):
        try:
            os.startfile(executable)
            return f"Haan bilkul, {name} khol diya maine!"
        except Exception:
            pass

    # 2. Stage 1: os.startfile with shell registration lookup (works for Chrome, Edge, Notepad, Calc, Spotify, etc.)
    try:
        os.startfile(executable)
        return f"Haan bilkul, abhi kholti hoon {name}!"
    except Exception:
        pass

    # 3. Stage 2: Try appending .exe with os.startfile
    if not executable.endswith(".exe"):
        try:
            os.startfile(executable + ".exe")
            return f"Haan bilkul, abhi kholti hoon {name}!"
        except Exception:
            pass

    # 4. Stage 3: PowerShell Start-Process
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Start-Process '{executable}'"],
            capture_output=True,
            timeout=5,
        )
        if res.returncode == 0:
            return f"Haan bilkul, abhi kholti hoon {name}!"
    except Exception:
        pass

    # 5. Stage 4: Common Windows installation directories
    program_dirs = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("APPDATA", ""),
    ]

    known_app_subpaths = {
        "chrome": [
            r"Google\Chrome\Application\chrome.exe",
        ],
        "edge": [
            r"Microsoft\Edge\Application\msedge.exe",
        ],
        "code": [
            r"Programs\Microsoft VS Code\Code.exe",
        ],
        "spotify": [
            r"Spotify\Spotify.exe",
        ],
        "discord": [
            r"Discord\Update.exe --processStart Discord.exe",
        ],
    }

    for sub in known_app_subpaths.get(app_key, []):
        for base in program_dirs:
            if base:
                cand = os.path.join(base, sub)
                if os.path.exists(cand):
                    try:
                        os.startfile(cand)
                        return f"Haan bilkul, abhi kholti hoon {name}!"
                    except Exception:
                        pass

    # 6. Stage 5: Start Menu shortcut (covers almost every installed app & game)
    try:
        lnk = _find_start_menu_shortcut(name)
        if lnk:
            os.startfile(lnk)
            return f"Haan bilkul, abhi kholti hoon {name}!"
    except Exception:
        pass

    # 7. Stage 6: deep-search game/app .exe across drives (finds games in other
    # drives / custom folders). Runs before the cmd fallback because that fallback
    # can't actually verify success.
    try:
        exe = _find_game_exe(name)
        if exe:
            os.startfile(exe)
            return f"Mil gaya! {name} khol rahi hoon ({os.path.dirname(exe)} se)."
    except Exception:
        pass

    # 8. Stage 7: cmd start fallback (last — cannot confirm it worked)
    try:
        subprocess.Popen(f'start "" "{executable}"', shell=True)
        return f"Haan bilkul, abhi kholti hoon {name}!"
    except Exception:
        pass

    return f"Arey, mujhe computer par {name} mila hi nahi. Poora naam ya path bata do?"


def _close_app(name):
    if not name:
        return "Kaun sa app band karna hai?"
    app_key = name.lower().strip()
    proc_name = APP_MAP.get(app_key, app_key)
    if not proc_name.endswith(".exe"):
        proc_name += ".exe"
    result = subprocess.run(
        ["taskkill", "/f", "/im", proc_name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        return f"Theek hai, {name} band kar diya maine!"
    return f"{name} ka running process nahi mila."


def _open_url(url):
    if not url:
        return "Kaun si link kholni hai?"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "Yeh valid public web link nahi lag rahi, isliye maine nahi kholi."
    webbrowser.open(url)
    return "Yeh lo, link khol di maine browser mein!"


def _web_search(query):
    if not query:
        return "Kya dhoondhna hai Google par?"
    import urllib.parse
    search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
    webbrowser.open(search_url)
    return f"Chalo, Google par {query} search kar diya hai!"


def _youtube_search(query):
    if not query:
        return "YouTube par kya chalana hai?"
    import urllib.parse
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
    webbrowser.open(url)
    return f"Yeh lo, YouTube par {query} chala diya!"


def _media_control(action_type):
    try:
        import pyautogui
        if action_type == "play_pause":
            pyautogui.press("playpause")
            return "Media play pause kar diya."
        elif action_type == "next":
            pyautogui.press("nexttrack")
            return "Agla gaana chala diya!"
        elif action_type == "prev":
            pyautogui.press("prevtrack")
            return "Pichhla gaana laga diya."
    except Exception:
        return "Media control nahi ho paya."


def _play_spotify(query):
    if not query:
        os.startfile("spotify:")
        return "Spotify open kar diya maine!"
    clean_q = urllib.parse.quote(query.strip())
    try:
        os.startfile(f"spotify:search:{clean_q}")
        return f"Spotify par '{query}' play karne ke liye search khol diya!"
    except Exception:
        webbrowser.open(f"https://open.spotify.com/search/{clean_q}")
        return f"Spotify web par '{query}' search khol diya!"


def _set_volume(level):
    try:
        # Normalize level between 0 and 100
        val = max(0, min(100, int(level)))
        # PowerShell script using SoundVolumeView / Audio endpoint COM
        ps_cmd = f"""
        $obj = New-Object -ComObject WScript.Shell
        # First mute/unmute to reset or press volume down 50 times then up to desired level
        1..50 | ForEach-Object {{ $obj.SendKeys([char]174) }}
        1..{int(val / 2)} | ForEach-Object {{ $obj.SendKeys([char]175) }}
        """
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd], capture_output=True, timeout=5)
        return f"Volume {val}% par set kar diya!"
    except Exception:
        return "Volume set nahi ho paaya."


def _volume_change(direction):
    try:
        key = 175 if direction == "up" else 174
        for _ in range(3):  # 3 steps for noticeable change
            subprocess.run(
                ['powershell', '-Command', f'(New-Object -ComObject WScript.Shell).SendKeys([char]{key})'],
                capture_output=True, timeout=2
            )
        return "Volume badha diya!" if direction == "up" else "Volume thoda kam kar diya."
    except Exception:
        return "Volume change nahi ho paya."


def _mute():
    try:
        subprocess.run(
            ['powershell', '-Command', '(New-Object -ComObject WScript.Shell).SendKeys([char]173)'],
            capture_output=True, timeout=3
        )
        return "Mute toggle kar diya maine."
    except Exception:
        return "Mute toggle nahi ho paya."


def _snap_window(position="left"):
    try:
        import pyautogui
        pos = position.lower().strip()
        if pos in {"left", "baayein", "baye"}:
            pyautogui.hotkey("win", "left")
            return "Window ko left mein snap kar diya!"
        elif pos in {"right", "daayein", "daye"}:
            pyautogui.hotkey("win", "right")
            return "Window ko right mein snap kar diya!"
        elif pos in {"maximize", "max", "bada", "full"}:
            pyautogui.hotkey("win", "up")
            return "Window maximize kar di!"
        elif pos in {"minimize", "min", "chhota"}:
            pyautogui.hotkey("win", "down")
            return "Window minimize kar di!"
        return "Window arrange kar di!"
    except Exception:
        return "Window snap nahi ho paayi."


def _switch_app():
    try:
        import pyautogui
        pyautogui.hotkey("alt", "tab")
        return "Dusri app par switch kar diya!"
    except Exception:
        return "App switch nahi ho paayi."


def _show_desktop():
    try:
        import pyautogui
        pyautogui.hotkey("win", "d")
        return "Yeh lo, Desktop dikha diya!"
    except Exception:
        return "Desktop show nahi ho paya."


def _close_active_window():
    try:
        import pyautogui
        pyautogui.hotkey("alt", "f4")
        return "Active window band kar di!"
    except Exception:
        return "Window close nahi ho paayi."


def _screenshot():
    try:
        from PIL import ImageGrab
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        filename = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        filepath = os.path.join(desktop, filename)
        img = ImageGrab.grab()
        img.save(filepath)
        return "Yeh lo, screenshot lekar Desktop par save kar diya maine!"
    except Exception as e:
        return f"Screenshot lene mein problem hui: {str(e)}"


def _type_text(text):
    if not text:
        return "Kya type karoon, bolo?"
    try:
        import pyautogui
        # Set clipboard with full Unicode / Hindi / multi-line support
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            input=text,
            capture_output=True,
            text=True,
            timeout=5,
        )
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "v")
        preview = text[:40] + ("..." if len(text) > 40 else "")
        return f"Haan, type kar diya maine: '{preview}'"
    except Exception:
        try:
            import pyautogui
            pyautogui.typewrite(text, interval=0.01)
            return "Haan, type kar diya maine!"
        except Exception as e2:
            return f"Type karne mein dikkat aayi: {str(e2)}"


def _lock_screen():
    os.system("rundll32.exe user32.dll,LockWorkStation")
    return "Computer lock kar diya maine!"


def _shutdown():
    os.system("shutdown /s /t 30")
    return "Theek hai, computer 30 seconds mein band ho jayega. Cancel karne ke liye 'cancel shutdown' bolo."


def _restart():
    os.system("shutdown /r /t 30")
    return "Haan, computer aadhe minute mein restart ho jayega."


def _cancel_shutdown():
    os.system("shutdown /a")
    return "Theek hai, shutdown cancel kar diya maine!"


def _open_folder(name):
    user_home = os.path.expanduser("~")
    folders = {
        "downloads": os.path.join(user_home, "Downloads"),
        "documents": os.path.join(user_home, "Documents"),
        "desktop": os.path.join(user_home, "Desktop"),
        "pictures": os.path.join(user_home, "Pictures"),
        "music": os.path.join(user_home, "Music"),
        "videos": os.path.join(user_home, "Videos"),
    }
    path = folders.get(name.lower().strip(), user_home)
    if os.path.exists(path):
        os.startfile(path)
        return f"Yeh lo, {name} folder khol diya!"
    return "Folder nahi mil paya."


def _get_time():
    now = datetime.now()
    hour = int(now.strftime("%I"))
    minute = now.strftime("%M")
    am_pm = "subah" if now.strftime("%p") == "AM" else ("dopahar" if hour < 5 else "shaam" if hour < 8 else "raat")
    if minute == "00":
        return f"Abhi {am_pm} ke poore {hour} baje hain."
    else:
        return f"Abhi {hour} bajkar {minute} minute hue hain."


def _get_date():
    now = datetime.now()
    days_hi = {
        "Monday": "Somvaar", "Tuesday": "Mangalvaar", "Wednesday": "Budhvaar",
        "Thursday": "Guruvaar", "Friday": "Shukravaar", "Saturday": "Shanivaar", "Sunday": "Ravivaar"
    }
    months_hi = {
        "January": "January", "February": "February", "March": "March", "April": "April",
        "May": "May", "June": "June", "July": "July", "August": "August",
        "September": "September", "October": "October", "November": "November", "December": "December"
    }
    day_name = days_hi.get(now.strftime("%A"), now.strftime("%A"))
    date_num = now.strftime("%d")
    month_name = months_hi.get(now.strftime("%B"), now.strftime("%B"))
    year = now.strftime("%Y")
    return f"Aaj {day_name} hai, {date_num} {month_name} {year}."


def get_active_window_info() -> dict[str, str]:
    """Retrieve title and process name of the current foreground window on Windows."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"title": "Desktop", "process": "explorer.exe"}

        # Get window text
        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()

        # Get process ID
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        process_name = "Unknown"
        try:
            import psutil
            process_name = psutil.Process(pid.value).name()
        except Exception:
            pass

        return {
            "title": title or "Untitled Window",
            "process": process_name,
            "pid": str(pid.value),
        }
    except Exception:
        return {"title": "Desktop", "process": "explorer.exe", "pid": "0"}

