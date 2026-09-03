"""Aisha auto-updater — checks GitHub Releases and installs updates cleanly.

Design (no-conflict, in-place update for a one-folder PyInstaller build):
  1. Ask the GitHub Releases API for the latest release.
  2. If its tag (vX.Y.Z) is newer than the bundled version.py VERSION, offer it.
  3. Download the release's "Aisha-AI-windows.zip" asset to a temp folder.
  4. Extract it, then write a small .bat that (after this app exits) copies the
     new files OVER the current install dir and relaunches Aisha.exe.
     Copying after exit avoids the "file in use" conflict entirely, and copying
     OVER the same folder means no duplicate/parallel installs.

Usage:
    from updater import check_for_update, apply_update
    info = check_for_update()          # returns dict or None
    if info: apply_update(info)        # downloads + schedules swap + exits

Safe to import when packaged (frozen) or running from source. When running from
source it just reports availability and does nothing destructive.
"""

from __future__ import annotations

import io
import os
import sys
import json
import tempfile
import subprocess
import urllib.request
import urllib.error
import zipfile

try:
    from version import VERSION, GITHUB_OWNER, GITHUB_REPO
except Exception:
    VERSION, GITHUB_OWNER, GITHUB_REPO = "0.0.0", "thor8126", "Aisha-AI"

RELEASE_ASSET_NAME = "Aisha-AI-windows.zip"
_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


# ----------------------------------------------------------------------
# Version helpers
# ----------------------------------------------------------------------
def _parse(v: str) -> tuple:
    """'v1.2.3' or '1.2.3' -> (1, 2, 3). Non-numeric parts become 0."""
    v = (v or "").strip().lstrip("vV")
    out = []
    for part in v.split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def is_newer(remote: str, local: str) -> bool:
    return _parse(remote) > _parse(local)


def is_frozen() -> bool:
    """True when running from a PyInstaller build (not source)."""
    return getattr(sys, "frozen", False)


def install_dir() -> str:
    """The folder that would be replaced on update (the app root)."""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------
# Check GitHub for a newer release
# ----------------------------------------------------------------------
def check_for_update(timeout: float = 8.0) -> dict | None:
    """Return {version, notes, url, asset} if a newer release exists, else None."""
    try:
        req = urllib.request.Request(_API, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{GITHUB_REPO}-updater",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    tag = data.get("tag_name", "")
    if not tag or not is_newer(tag, VERSION):
        return None

    # Find the windows zip asset.
    asset_url = None
    for asset in data.get("assets", []):
        if asset.get("name", "").lower() == RELEASE_ASSET_NAME.lower():
            asset_url = asset.get("browser_download_url")
            break

    return {
        "version": tag.lstrip("vV"),
        "notes": (data.get("body") or "").strip()[:800],
        "url": data.get("html_url", ""),
        "asset": asset_url,
    }


# ----------------------------------------------------------------------
# Download + schedule an in-place swap
# ----------------------------------------------------------------------
def apply_update(info: dict, on_progress=None) -> bool:
    """Download the update zip, then schedule a post-exit in-place swap.

    Returns True if the swap was scheduled (caller should then exit the app).
    Only works in a frozen build — from source there is nothing to swap.
    """
    if not is_frozen():
        return False
    asset = info.get("asset")
    if not asset:
        return False

    tmp = tempfile.mkdtemp(prefix="aisha_update_")
    zip_path = os.path.join(tmp, RELEASE_ASSET_NAME)

    # Download with basic progress reporting.
    try:
        req = urllib.request.Request(asset, headers={"User-Agent": f"{GITHUB_REPO}-updater"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(zip_path, "wb") as f:
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if on_progress and total:
                    on_progress(done / total)
    except Exception:
        return False

    # Extract into tmp/new
    new_dir = os.path.join(tmp, "new")
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(new_dir)
    except Exception:
        return False

    # If the zip contains a single top-level folder, use that as the source root.
    entries = [os.path.join(new_dir, e) for e in os.listdir(new_dir)]
    if len(entries) == 1 and os.path.isdir(entries[0]):
        new_dir = entries[0]

    target = install_dir()
    exe_name = os.path.basename(sys.executable)

    # A batch script that runs AFTER this process exits: wait, robocopy the new
    # files over the install dir (in place — no parallel install), relaunch.
    bat = os.path.join(tmp, "apply_update.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            f'set "SRC={new_dir}"\r\n'
            f'set "DST={target}"\r\n'
            "echo Updating Aisha...\r\n"
            ":waitloop\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            # Wait until the exe is no longer locked (app fully exited).
            f'2>nul (>>"%DST%\\{exe_name}" call ) && goto docopy || goto waitloop\r\n'
            ":docopy\r\n"
            # /E all subdirs, /IS include same, /R:2 retries — mirror new over old.
            'robocopy "%SRC%" "%DST%" /E /IS /IT /R:2 /W:1 >nul\r\n'
            f'start "" "%DST%\\{exe_name}"\r\n'
            # Clean up the temp folder.
            f'rmdir /S /Q "{tmp}" >nul 2>&1\r\n'
        )

    # Launch the batch detached so it survives our exit.
    try:
        subprocess.Popen(
            ["cmd", "/c", bat],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    except Exception:
        return False
    return True


# ----------------------------------------------------------------------
# CLI: `python updater.py` just checks and prints
# ----------------------------------------------------------------------
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print(f"Current version: {VERSION}")
    info = check_for_update()
    if info:
        print(f"Update available: {info['version']}")
        print(f"Notes: {info['notes'][:200]}")
        print(f"Release: {info['url']}")
    else:
        print("You are on the latest version (or GitHub is unreachable).")
