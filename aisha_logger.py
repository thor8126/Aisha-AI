"""
aisha_logger.py — Structured File Logging for Aisha AI
Centralized logging with automatic file rotation.

All modules should use:
    from aisha_logger import log
    log.info("message")
    log.warning("message")
    log.error("message")

Logs are written to: logs/aisha.log (rotated at 2MB, keeps 3 backups)
Console output is NOT affected — print() statements continue working normally.
"""

import os
import logging
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "aisha.log")

# Create the main logger
log = logging.getLogger("aisha")
log.setLevel(logging.DEBUG)

# Prevent duplicate handlers on re-import
if not log.handlers:
    # File handler: detailed logs with rotation
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,   # 2 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(file_handler)

    # OPTIONAL: Console handler (only for WARNING+ so it doesn't flood terminal)
    # Uncomment if you want warnings/errors also printed to console
    # console_handler = logging.StreamHandler()
    # console_handler.setLevel(logging.WARNING)
    # console_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    # log.addHandler(console_handler)


def log_latency(operation: str, duration_ms: float, **extra):
    """Log a performance measurement for tracking response times."""
    parts = [f"{operation}: {duration_ms:.0f}ms"]
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    log.info(" | ".join(parts))
