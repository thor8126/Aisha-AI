"""
secure_keys.py — Secure API Key Manager for Aisha AI
Stores and retrieves API keys from Windows Credential Manager instead of plaintext .env.

Usage:
    python secure_keys.py store           Store all keys from .env into Credential Manager
    python secure_keys.py status          Check which keys are stored securely
    python secure_keys.py clear           Remove all stored keys from Credential Manager

Once stored, Aisha will auto-load keys from Credential Manager at startup.
The .env file can then be cleared of sensitive values (keep only non-secret config).
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Key names we manage (maps env var name → credential service name)
MANAGED_KEYS = {
    "TOKEN": "AishaAI/TOKEN",
    "ELEVEN_LAB": "AishaAI/ELEVEN_LAB",
    "AGENTROUTER_TOKEN": "AishaAI/AGENTROUTER_TOKEN",
    "NVIDIA_API_KEY": "AishaAI/NVIDIA_API_KEY",
}


def _get_keyring():
    """Import keyring, raising a clear error if not installed."""
    try:
        import keyring
        return keyring
    except ImportError:
        print("❌ 'keyring' package not found. Install it with: pip install keyring")
        sys.exit(1)


def store_keys_from_env():
    """Read current .env values and store them securely in Windows Credential Manager."""
    keyring = _get_keyring()
    from dotenv import dotenv_values

    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        print("❌ .env file not found.")
        return

    env_vals = dotenv_values(env_path)
    stored = 0

    for env_var, service_name in MANAGED_KEYS.items():
        value = env_vals.get(env_var, "").strip()
        if value and len(value) > 5:
            try:
                keyring.set_password(service_name, "api_key", value)
                masked = value[:8] + "..." + value[-4:]
                print(f"  ✅ {env_var} → Credential Manager ({masked})")
                stored += 1
            except Exception as e:
                print(f"  ⚠️ Failed to store {env_var}: {e}")
        else:
            print(f"  ⏭️ {env_var} — not set in .env, skipping")

    if stored > 0:
        print(f"\n🔐 {stored} key(s) stored securely in Windows Credential Manager.")
        print("💡 You can now remove those values from .env — Aisha will auto-load from Credential Manager.")
    else:
        print("\n⚠️ No keys were stored. Make sure .env has valid API key values.")


def get_secure_key(env_var: str) -> str | None:
    """Retrieve a key from Credential Manager. Returns None if not found or keyring unavailable."""
    service_name = MANAGED_KEYS.get(env_var)
    if not service_name:
        return None
    try:
        import keyring
        value = keyring.get_password(service_name, "api_key")
        return value if value else None
    except Exception:
        return None


def load_secure_env():
    """Load API keys: prefer Credential Manager, fall back to .env values.
    Call this at Aisha startup to transparently use secure storage when available.
    """
    loaded_from_secure = []
    for env_var in MANAGED_KEYS:
        # Only override if not already set in environment (or empty)
        current = os.environ.get(env_var, "").strip()
        if not current:
            secure_val = get_secure_key(env_var)
            if secure_val:
                os.environ[env_var] = secure_val
                loaded_from_secure.append(env_var)

    if loaded_from_secure:
        print(f"🔐 Loaded {len(loaded_from_secure)} key(s) from Credential Manager: {', '.join(loaded_from_secure)}")

    return loaded_from_secure


def show_status():
    """Show which keys are stored in Credential Manager."""
    keyring = _get_keyring()
    print("🔐 Secure Key Storage Status:")
    print("-" * 50)
    for env_var, service_name in MANAGED_KEYS.items():
        try:
            val = keyring.get_password(service_name, "api_key")
            if val:
                masked = val[:8] + "..." + val[-4:]
                print(f"  ✅ {env_var}: {masked}")
            else:
                print(f"  ❌ {env_var}: not stored")
        except Exception as e:
            print(f"  ⚠️ {env_var}: error ({e})")


def clear_keys():
    """Remove all stored keys from Credential Manager."""
    keyring = _get_keyring()
    removed = 0
    for env_var, service_name in MANAGED_KEYS.items():
        try:
            keyring.delete_password(service_name, "api_key")
            print(f"  🗑️ {env_var} removed from Credential Manager")
            removed += 1
        except keyring.errors.PasswordDeleteError:
            print(f"  ⏭️ {env_var} — not stored, skipping")
        except Exception as e:
            print(f"  ⚠️ {env_var}: {e}")

    print(f"\n🗑️ {removed} key(s) removed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python secure_keys.py [store|status|clear]")
        sys.exit(1)

    action = sys.argv[1].lower()
    if action == "store":
        store_keys_from_env()
    elif action == "status":
        show_status()
    elif action == "clear":
        clear_keys()
    else:
        print(f"Unknown action: {action}")
        print("Usage: python secure_keys.py [store|status|clear]")
