"""
watchdog.py — Process Watchdog for Aisha AI
Auto-restarts Aisha if she crashes, with exponential backoff to prevent restart loops.
Logs crash events to logs/watchdog.log.

Usage:
    python watchdog.py              (launches aisha.py with auto-restart)
    python watchdog.py --max 10     (max 10 restarts before giving up)
    python watchdog.py --text       (passes --text flag to aisha.py)
"""

import os
import sys
import time
import subprocess
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

from aisha.paths import PROJECT_ROOT
BASE_DIR = PROJECT_ROOT
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Watchdog-specific log (separate from Aisha's main log)
wdlog = logging.getLogger("watchdog")
wdlog.setLevel(logging.INFO)
_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "watchdog.log"),
    maxBytes=512 * 1024,  # 512 KB
    backupCount=2,
    encoding="utf-8",
)
_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
wdlog.addHandler(_handler)

# Backoff config
INITIAL_BACKOFF = 2.0        # seconds
MAX_BACKOFF = 300.0          # 5 min cap
BACKOFF_MULTIPLIER = 2.0
COOLDOWN_RESET = 600.0       # If Aisha runs 10+ min without crash, reset backoff


def run_watchdog(max_restarts=50, extra_args=None):
    """Launch and monitor Aisha, auto-restarting on crash with exponential backoff."""
    python_exe = sys.executable
    # Launch the package entry point (python -m aisha) from the project root.
    cmd = [python_exe, "-m", "aisha"]
    if extra_args:
        cmd.extend(extra_args)

    crash_count = 0
    backoff = INITIAL_BACKOFF

    wdlog.info(f"Watchdog started. Max restarts: {max_restarts}. Command: {' '.join(cmd)}")
    print(f"🐕 Watchdog active. Monitoring Aisha with max {max_restarts} restarts.")

    while crash_count < max_restarts:
        start_time = time.time()
        wdlog.info(f"Launching Aisha (attempt #{crash_count + 1})...")

        try:
            # Ensure the package on src/ is importable for `python -m aisha`.
            env = dict(os.environ)
            src_dir = os.path.join(BASE_DIR, "src")
            env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
            result = subprocess.run(
                cmd,
                cwd=BASE_DIR,
                env=env,
                # Don't capture stdout/stderr — let them flow to console/log normally
            )
            exit_code = result.returncode
        except KeyboardInterrupt:
            wdlog.info("Watchdog stopped by user (Ctrl+C).")
            print("\n🐕 Watchdog stopped by user.")
            return
        except Exception as e:
            exit_code = -1
            wdlog.error(f"Failed to launch Aisha: {e}")

        run_duration = time.time() - start_time

        # Clean exit (exit code 0) — user quit intentionally, don't restart
        if exit_code == 0:
            wdlog.info(f"Aisha exited cleanly (code 0) after {run_duration:.1f}s. No restart needed.")
            print("✅ Aisha exited cleanly. Watchdog stopping.")
            return

        # Crash detected
        crash_count += 1
        wdlog.warning(
            f"Aisha crashed! Exit code: {exit_code} | "
            f"Runtime: {run_duration:.1f}s | Crash #{crash_count}/{max_restarts}"
        )

        # If Aisha ran for a long time before crashing, reset backoff (it was stable)
        if run_duration >= COOLDOWN_RESET:
            backoff = INITIAL_BACKOFF
            wdlog.info(f"Aisha ran {run_duration:.0f}s (stable). Backoff reset to {INITIAL_BACKOFF}s.")

        print(f"⚠️ Aisha crashed (code {exit_code}). Restarting in {backoff:.0f}s... [{crash_count}/{max_restarts}]")

        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            wdlog.info("Watchdog stopped during backoff wait (Ctrl+C).")
            print("\n🐕 Watchdog stopped.")
            return

        # Exponential backoff with cap
        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)

    wdlog.error(f"Max restarts ({max_restarts}) reached. Watchdog giving up.")
    print(f"❌ Aisha crashed {max_restarts} times. Watchdog giving up. Check logs/watchdog.log")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Aisha AI Process Watchdog")
    parser.add_argument("--max", type=int, default=50, help="Max restart attempts (default: 50)")
    parser.add_argument("--text", action="store_true", help="Pass --text to Aisha")
    parser.add_argument("--cli", action="store_true", help="Pass --cli to Aisha")
    args, unknown = parser.parse_known_args()

    extra = unknown[:]
    if args.text:
        extra.append("--text")
    if args.cli:
        extra.append("--cli")

    run_watchdog(max_restarts=args.max, extra_args=extra if extra else None)
