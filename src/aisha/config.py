"""Environment / API-key handling for both source runs and packaged .exe.

The problem: an end user running the packaged Aisha.exe has no way to edit source
and no shell to `export` variables. So keys must come from a `.env` file that lives
NEXT TO THE EXECUTABLE (not the current working directory, which is unpredictable
when launched from a shortcut).

This module:
  1. Loads `.env` from the project root (source) or the exe's folder (frozen).
  2. Also checks Windows Credential Manager (optional secure storage).
  3. On first run with no keys, writes a `.env` template next to the exe so the
     user has an obvious, documented file to fill in — and returns a flag so the
     UI can show a friendly "add your API keys" message.

Precedence: real environment vars > .env file > Credential Manager fallback.
"""

from __future__ import annotations

import os

from aisha.paths import PROJECT_ROOT

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

# The minimal keys Aisha needs to do anything useful.
REQUIRED_KEYS = ("TOKEN",)          # primary AI provider token
RECOMMENDED_KEYS = ("ELEVEN_LAB",)  # voice (optional; falls back to Windows TTS)

_ENV_TEMPLATE = """# ============================================================
# Aisha AI — your API keys go here. Fill these in, then relaunch.
# This file lives next to Aisha.exe. Never share it — it holds secrets.
# ============================================================

# --- Primary AI (REQUIRED). Get a free key at https://groq.com ---
BASE_URL="https://api.groq.com/openai/v1"
TOKEN=""
AISHA_PRIMARY_MODEL="openai/gpt-oss-20b"

# --- Voice (optional). Blank = Windows built-in TTS. https://elevenlabs.io ---
ELEVEN_LAB=""
ELEVENLABS_VOICE_ID="WUgmmuDCpFXQ4z0NUUYX"
ELEVENLABS_MODEL_ID="eleven_v3"

# --- Strong AI for research/writing (optional). https://bayofassets.com ---
BAYOFASSETS_BASE_URL="https://api.bayofassets.com/v1"
BAYOFASSETS_TOKEN=""
BAYOFASSETS_MODEL="claude-opus-4-8"

# --- Where Aisha saves the files she creates ---
AISHA_OUTPUT_DIR=""

# --- Speech recognition model: tiny | base | small | medium ---
WHISPER_MODEL="base"
"""


def _write_template_if_missing() -> bool:
    """Create a .env template next to the exe if none exists. Returns True if written."""
    if os.path.exists(ENV_PATH):
        return False
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(_ENV_TEMPLATE)
        return True
    except Exception:
        return False


def load_config() -> dict:
    """Load env from .env + Credential Manager. Returns a status dict:
        {created_template: bool, has_required: bool, env_path: str, missing: [..]}
    """
    from dotenv import load_dotenv

    created = _write_template_if_missing()

    # Load the .env that sits next to the exe / at the project root.
    if os.path.exists(ENV_PATH):
        load_dotenv(ENV_PATH, override=False)
    else:
        load_dotenv(override=False)  # fall back to default search

    # Optional: pull keys from Windows Credential Manager if present.
    try:
        from aisha.system.secure_keys import load_secure_env
        load_secure_env()
    except Exception:
        pass

    missing = [k for k in REQUIRED_KEYS if not os.getenv(k, "").strip()]
    return {
        "created_template": created,
        "has_required": not missing,
        "env_path": ENV_PATH,
        "missing": missing,
    }
