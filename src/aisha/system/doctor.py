"""Safe readiness checks for the Aisha desktop assistant.

The default diagnostic is offline and read-only.  Pass ``--online`` to make one
small, harmless model request using the configured Anthropic-compatible API.
Secret values are never included in reports.
"""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


from aisha.paths import PROJECT_ROOT
ROOT = Path(PROJECT_ROOT)
DEFAULT_BASE_URL = "https://agentrouter.org"
DEFAULT_MODEL = "deepseek-v4-flash"

# Core modules, resolved under the src/ package after the restructure.
CORE_FILES = (
    "src/aisha/system/actions.py",
    "src/aisha/core/agent.py",
    "src/aisha/app.py",
    "src/aisha/ui/gui.py",
    "src/aisha/tools/registry.py",
    "src/aisha/core/memory.py",
    "src/aisha/system/startup.py",
    "src/aisha/core/tasks.py",
)

DEPENDENCIES = (
    ("PyQt6", "PyQt6", True),
    ("faster-whisper", "faster_whisper", False),
    ("openai", "openai", True),
    ("anthropic", "anthropic", True),
    ("python-dotenv", "dotenv", True),
    ("requests", "requests", True),
    ("sounddevice", "sounddevice", True),
    ("openai-whisper", "whisper", True),
    ("numpy", "numpy", True),
    ("elevenlabs", "elevenlabs", False),
    ("pygame", "pygame", True),
    ("pyttsx3", "pyttsx3", True),
    ("pyautogui", "pyautogui", True),
    ("Pillow", "PIL", True),
)

SECRET_NAMES = {"TOKEN", "ELEVEN_LAB", "ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    critical: bool = False


def _dotenv_config(path: Path) -> dict[str, str]:
    """Read dotenv data without mutating the process environment."""
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values

        parsed = dotenv_values(path)
        return {str(key): str(value or "") for key, value in parsed.items()}
    except Exception:
        # A small fallback keeps the doctor usable before python-dotenv is installed.
        result: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return result
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                result[key] = value.strip().strip("'\"")
        return result


def _config() -> dict[str, str]:
    values = _dotenv_config(ROOT / ".env")
    values.update({key: value for key, value in os.environ.items() if value is not None})
    return values


def check_environment(config: dict[str, str]) -> list[Check]:
    checks: list[Check] = []
    token_present = bool(config.get("TOKEN", "").strip())
    checks.append(
        Check(
            "Environment: TOKEN",
            "PASS" if token_present else "FAIL",
            "configured" if token_present else "missing; model requests cannot run",
            critical=True,
        )
    )

    voice_present = bool(config.get("ELEVEN_LAB", "").strip() or config.get("ELEVENLABS_API_KEY", "").strip())
    checks.append(
        Check(
            "Environment: ElevenLabs key",
            "PASS" if voice_present else "WARN",
            "configured" if voice_present else "missing; offline pyttsx3 voice fallback will be used",
        )
    )

    base_present = bool(config.get("BASE_URL", "").strip())
    checks.append(
        Check(
            "Environment: BASE_URL",
            "PASS",
            "configured" if base_present else "not set; secure AgentRouter default will be used",
        )
    )
    return checks


def check_dependencies() -> list[Check]:
    checks: list[Check] = []
    for package, module, critical in DEPENDENCIES:
        try:
            present = importlib.util.find_spec(module) is not None
        except (ImportError, AttributeError, ValueError):
            present = False
        checks.append(
            Check(
                f"Dependency: {package}",
                "PASS" if present else ("FAIL" if critical else "WARN"),
                "installed" if present else f"missing (import name: {module})",
                critical=critical,
            )
        )
    return checks


def check_ffmpeg() -> Check:
    executable = shutil.which("ffmpeg")
    if not executable:
        return Check("FFmpeg", "WARN", "not found on PATH; Whisper cannot decode most audio formats")
    try:
        result = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        first_line = (result.stdout or result.stderr).splitlines()[0].strip()
        if result.returncode == 0:
            return Check("FFmpeg", "PASS", first_line[:160] or "available")
        return Check("FFmpeg", "WARN", f"found but returned exit code {result.returncode}")
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("FFmpeg", "WARN", f"could not run: {type(exc).__name__}")


def check_microphone() -> Check:
    if importlib.util.find_spec("sounddevice") is None:
        return Check("Microphone", "WARN", "sounddevice is not installed")
    probe = (
        "import json, sounddevice as sd; "
        "devices=sd.query_devices(); "
        "count=sum(1 for d in devices if int(d['max_input_channels']) > 0); "
        "default=sd.default.device; "
        "default_input=int(default[0]) if hasattr(default, '__len__') else int(default); "
        "print(json.dumps({'inputs': count, 'default_input': default_input}))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_sanitized_subprocess_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("Microphone", "WARN", f"device probe failed: {type(exc).__name__}")
    if result.returncode != 0:
        return Check("Microphone", "WARN", "PortAudio could not query input devices")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        count = int(payload["inputs"])
        default_input = int(payload["default_input"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return Check("Microphone", "WARN", "device probe returned an unreadable result")
    if count < 1:
        return Check("Microphone", "WARN", "no input-capable audio device was detected")
    if default_input < 0:
        return Check("Microphone", "WARN", f"{count} input device(s) found, but no default input is selected")
    return Check("Microphone", "PASS", f"{count} input-capable device(s); default input is selected")


def check_compile_health() -> Check:
    failures: list[str] = []
    checked = 0
    for filename in CORE_FILES:
        path = ROOT / filename
        if not path.is_file():
            failures.append(f"{filename}: missing")
            continue
        try:
            source = path.read_text(encoding="utf-8-sig")
            compile(source, str(path), "exec")
            checked += 1
        except (OSError, SyntaxError, UnicodeError) as exc:
            failures.append(f"{filename}: {type(exc).__name__}")
    if failures:
        return Check("Compile health", "FAIL", "; ".join(failures), critical=True)
    return Check("Compile health", "PASS", f"{checked} core Python files compile cleanly")


def check_runtime_health() -> Check:
    modules = "actions, memory, task_store, assistant_tools, agent_brain, aisha"
    probe = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        f"import {modules}; "
        "print('runtime-import-ok')"
    )
    environment = _sanitized_subprocess_environment()
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    environment["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="aisha-doctor-") as data_dir:
            environment["AISHA_DATA_DIR"] = data_dir
            result = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
                env=environment,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("Runtime import", "FAIL", f"probe failed: {type(exc).__name__}", critical=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        summary = tail[-1][:240] if tail else f"exit code {result.returncode}"
        return Check("Runtime import", "FAIL", summary, critical=True)
    return Check("Runtime import", "PASS", "assistant modules import in a clean subprocess")


def _sanitized_subprocess_environment() -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in SECRET_NAMES or upper.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")):
            continue
        clean[key] = value
    return clean


def _validate_online_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("BASE_URL must be a credential-free public HTTPS URL")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("BASE_URL cannot target a local network host")
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    }
    if not addresses:
        raise ValueError("BASE_URL did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError("BASE_URL resolved to a local or non-public address")
    return hostname


def _redact_error(value: object, secrets: Iterable[str]) -> str:
    rendered = str(value)
    for secret in sorted({secret for secret in secrets if len(secret) >= 4}, key=len, reverse=True):
        rendered = rendered.replace(secret, "[REDACTED]")
    rendered = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", rendered)
    return rendered.replace("\r", " ").replace("\n", " ")[:300]


def check_online_model(config: dict[str, str]) -> Check:
    token = config.get("TOKEN", "").strip()
    if not token:
        return Check("Online model", "FAIL", "TOKEN is missing", critical=True)
    base_url = (config.get("BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    model = (config.get("AISHA_MODEL") or config.get("MODEL") or DEFAULT_MODEL).strip()
    secrets = [token, config.get("ELEVEN_LAB", ""), config.get("ELEVENLABS_API_KEY", "")]
    try:
        host = _validate_online_endpoint(base_url)
        import anthropic

        started = time.monotonic()
        client = anthropic.Anthropic(base_url=base_url, api_key=token, timeout=20.0, max_retries=0)
        response = client.messages.create(
            model=model,
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if not getattr(response, "content", None):
            return Check("Online model", "FAIL", f"{host} responded without message content", critical=True)
        return Check("Online model", "PASS", f"harmless API probe succeeded via {host} in {elapsed_ms} ms")
    except Exception as exc:
        return Check("Online model", "FAIL", _redact_error(exc, secrets), critical=True)


def run_checks(online: bool = False) -> list[Check]:
    config = _config()
    checks = check_environment(config)
    checks.extend(check_dependencies())
    checks.append(check_ffmpeg())
    checks.append(check_microphone())
    checks.append(check_compile_health())
    checks.append(check_runtime_health())
    if online:
        checks.append(check_online_model(config))
    else:
        checks.append(Check("Online model", "SKIP", "not requested; run doctor.py --online to test it"))
    return checks


def _print_text(checks: list[Check]) -> None:
    print("Aisha Doctor (secret values are never printed)")
    print("=" * 50)
    for check in checks:
        print(f"[{check.status:4}] {check.name}: {check.detail}")
    counts = {status: sum(check.status == status for check in checks) for status in ("PASS", "WARN", "FAIL", "SKIP")}
    print("-" * 50)
    print("Summary: " + ", ".join(f"{status}={count}" for status, count in counts.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run safe readiness checks for Aisha.")
    parser.add_argument("--online", action="store_true", help="Make one small live model call (may use a tiny amount of API quota).")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    checks = run_checks(online=args.online)
    if args.json:
        print(json.dumps({"checks": [asdict(check) for check in checks]}, indent=2))
    else:
        _print_text(checks)
    return 1 if any(check.status == "FAIL" and check.critical for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
