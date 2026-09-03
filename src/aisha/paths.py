"""Central path resolver for the Aisha package.

After the src/ restructure, modules can no longer anchor asset/data paths to
their own __file__ (that points inside src/aisha/...). Everything that lives at
the project root — assets/, logs/, .env, memory.json, .assistant_data/ — is
resolved here relative to the project root instead.

PROJECT_ROOT is:
  • the folder that contains `assets/` when running from source (dev), or
  • the executable's folder when frozen by PyInstaller.
Override with the AISHA_ROOT environment variable if needed.
"""

from __future__ import annotations

import os
import sys


def _detect_root() -> str:
    env = os.getenv("AISHA_ROOT")
    if env and os.path.isdir(env):
        return os.path.abspath(env)

    # Frozen (PyInstaller) → the folder next to the executable.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)

    # From source: this file is  <root>/src/aisha/paths.py  → go up 3 levels.
    here = os.path.dirname(os.path.abspath(__file__))          # .../src/aisha
    root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))  # project root
    # Sanity: prefer a parent that actually has assets/, else fall back to cwd.
    if os.path.isdir(os.path.join(root, "assets")):
        return root
    if os.path.isdir(os.path.join(os.getcwd(), "assets")):
        return os.getcwd()
    return root


PROJECT_ROOT = _detect_root()

ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
DATA_DIR = os.path.join(PROJECT_ROOT, ".assistant_data")


def root(*parts: str) -> str:
    """Path joined from the project root."""
    return os.path.join(PROJECT_ROOT, *parts)


def asset(*parts: str) -> str:
    """Path joined from the assets/ folder."""
    return os.path.join(ASSETS_DIR, *parts)
