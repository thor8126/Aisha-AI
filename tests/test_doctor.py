from __future__ import annotations

import contextlib
import io
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aisha.system import doctor


class DoctorTests(unittest.TestCase):
    def test_environment_report_never_contains_secret_values(self) -> None:
        token = "super-secret-token-value"
        voice = "super-secret-voice-value"
        checks = doctor.check_environment({"TOKEN": token, "ELEVEN_LAB": voice, "BASE_URL": "https://example.com"})
        rendered = " ".join(f"{item.name} {item.detail}" for item in checks)
        self.assertNotIn(token, rendered)
        self.assertNotIn(voice, rendered)
        self.assertTrue(all(item.status == "PASS" for item in checks))

    def test_dotenv_parser_reads_keys_without_mutating_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aisha-doctor-test-") as directory:
            path = Path(directory) / ".env"
            path.write_text("TOKEN=abc123\nBASE_URL=https://example.test\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                values = doctor._dotenv_config(path)
                self.assertNotIn("TOKEN", os.environ)
        self.assertEqual(values["TOKEN"], "abc123")
        self.assertEqual(values["BASE_URL"], "https://example.test")

    def test_sanitized_environment_removes_common_secret_names(self) -> None:
        values = {
            "SAFE_SETTING": "kept",
            "TOKEN": "hidden",
            "APP_API_KEY": "hidden",
            "LOGIN_PASSWORD": "hidden",
            "SERVICE_SECRET": "hidden",
        }
        with patch.dict(os.environ, values, clear=True):
            sanitized = doctor._sanitized_subprocess_environment()
        self.assertEqual(sanitized, {"SAFE_SETTING": "kept"})

    def test_online_endpoint_guard_blocks_private_resolution_without_network(self) -> None:
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))]
        with patch("aisha.system.doctor.socket.getaddrinfo", return_value=private):
            with self.assertRaisesRegex(ValueError, "local or non-public"):
                doctor._validate_online_endpoint("https://private.example.test")
        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            doctor._validate_online_endpoint("http://example.test")

    def test_online_probe_uses_small_request_and_does_not_report_response_text(self) -> None:
        secret = "secret-token-for-test"
        fake_response = SimpleNamespace(content=[SimpleNamespace(type="text", text="sensitive model output")])
        fake_messages = SimpleNamespace(create=lambda **kwargs: fake_response)
        fake_client = SimpleNamespace(messages=fake_messages)
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        with patch("aisha.system.doctor.socket.getaddrinfo", return_value=public), patch(
            "anthropic.Anthropic", return_value=fake_client
        ) as constructor:
            check = doctor.check_online_model(
                {"TOKEN": secret, "BASE_URL": "https://api.example.test", "AISHA_MODEL": "test-model"}
            )

        self.assertEqual(check.status, "PASS")
        self.assertNotIn(secret, check.detail)
        self.assertNotIn("sensitive model output", check.detail)
        constructor.assert_called_once_with(
            base_url="https://api.example.test", api_key=secret, timeout=20.0, max_retries=0
        )
        request = fake_messages.create
        self.assertTrue(callable(request))

    def test_main_exit_code_tracks_only_critical_failures(self) -> None:
        warning_only = [doctor.Check("optional", "WARN", "missing")]
        critical_failure = [doctor.Check("required", "FAIL", "missing", critical=True)]
        with patch("aisha.system.doctor.run_checks", return_value=warning_only), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.main([]), 0)
        with patch("aisha.system.doctor.run_checks", return_value=critical_failure), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.main([]), 1)


if __name__ == "__main__":
    unittest.main()
