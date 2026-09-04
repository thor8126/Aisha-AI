from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aisha.tools.registry import ToolRegistry
from aisha.core.tasks import TaskStore


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aisha-tools-test-")
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = TaskStore(self.workspace / ".assistant-state")
        self.registry = ToolRegistry(workspace=self.workspace, task_store=self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def execute(self, name: str, arguments: dict) -> dict:
        return json.loads(self.registry.execute(name, arguments))

    def test_definitions_are_unique_and_include_local_internet_and_state_tools(self) -> None:
        definitions = self.registry.definitions
        names = [item["name"] for item in definitions]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), self.registry.names)
        self.assertTrue(
            {
                "read_file",
                "write_file",
                "search_web",
                "fetch_url",
                "windows_action",
                "task_manager",
                "notes",
                "reminders",
            }.issubset(names)
        )
        self.assertTrue(all(item["input_schema"]["additionalProperties"] is False for item in definitions))

    def test_relative_paths_resolve_from_workspace_and_safe_file_round_trip(self) -> None:
        expected = (self.workspace / "folder" / "note.txt").resolve()
        self.assertEqual(self.registry._resolve_path("folder/note.txt", allow_missing=True), expected)

        written = self.execute("write_file", {"path": "folder/note.txt", "content": "alpha\nbeta"})
        self.assertTrue(written["ok"])
        self.assertEqual(written["data"]["characters"], 10)

        read = self.execute("read_file", {"path": "folder/note.txt", "start_line": 2})
        self.assertTrue(read["ok"])
        self.assertEqual(read["data"]["content"], "2: beta")
        self.assertEqual(read["data"]["total_lines"], 2)

        duplicate = self.execute("write_file", {"path": "folder/note.txt", "content": "replacement"})
        self.assertFalse(duplicate["ok"])
        self.assertIn("already exists", duplicate["error"])
        self.assertEqual(expected.read_text(encoding="utf-8"), "alpha\nbeta")

    def test_sensitive_files_and_protected_roots_are_blocked(self) -> None:
        secret_file = self.workspace / ".env"
        secret_file.write_text("TOKEN=never-return-this", encoding="utf-8")

        read = self.execute("read_file", {"path": ".env"})
        write = self.execute("write_file", {"path": ".env", "content": "x", "mode": "overwrite"})
        delete_workspace = self.execute("delete_path", {"path": str(self.workspace)})
        delete_state = self.execute("delete_path", {"path": str(self.store.data_dir)})

        self.assertFalse(read["ok"])
        self.assertFalse(write["ok"])
        self.assertFalse(delete_workspace["ok"])
        self.assertFalse(delete_state["ok"])
        self.assertTrue(self.workspace.is_dir())
        self.assertTrue(self.store.data_dir.is_dir())
        self.assertEqual(secret_file.read_text(encoding="utf-8"), "TOKEN=never-return-this")

        listing = self.execute("list_directory", {"path": ".", "recursive": True})
        listed_paths = {item["path"] for item in listing["data"]["entries"]}
        self.assertNotIn(str(secret_file), listed_paths)
        self.assertFalse(any(str(self.store.data_dir) in path for path in listed_paths))

    def test_confirmation_policy_distinguishes_read_only_and_mutating_actions(self) -> None:
        new_file = self.workspace / "new.txt"
        existing_file = self.workspace / "existing.txt"
        existing_file.write_text("original", encoding="utf-8")

        self.assertIsNone(self.registry.confirmation_summary("run_shell", {"command": "Get-ChildItem ."}))
        self.assertIsNotNone(self.registry.confirmation_summary("run_shell", {"command": "Set-Content x.txt changed"}))
        self.assertIsNone(self.registry.confirmation_summary("write_file", {"path": str(new_file), "mode": "create"}))
        self.assertIsNotNone(
            self.registry.confirmation_summary("write_file", {"path": str(existing_file), "mode": "overwrite"})
        )
        self.assertIsNotNone(self.registry.confirmation_summary("delete_path", {"path": str(existing_file)}))
        self.assertIsNotNone(self.registry.confirmation_summary("clipboard", {"action": "read"}))
        self.assertIsNone(self.registry.confirmation_summary("clipboard", {"action": "write", "text": "safe"}))

        complex_read = "Get-ChildItem .; Remove-Item x"
        self.assertFalse(self.registry._is_read_only_shell(complex_read))
        self.assertFalse(self.registry._is_read_only_shell("git status && whoami"))

    def test_created_path_is_copied_to_clipboard_best_effort(self) -> None:
        with patch.object(self.registry, "_clipboard") as clipboard:
            self.assertTrue(self.registry._copy_created_path("C:\\AishaFiles\\notes.docx"))
            clipboard.assert_called_once_with(
                {"action": "write", "text": "C:\\AishaFiles\\notes.docx"}
            )

        with patch.object(self.registry, "_clipboard", side_effect=RuntimeError("unavailable")):
            self.assertFalse(self.registry._copy_created_path("C:\\AishaFiles\\notes.docx"))

    def test_task_note_and_reminder_state_flows_through_json_tool_results(self) -> None:
        created = self.execute("task_manager", {"action": "create", "title": "Pay bill", "priority": "high"})
        task_id = created["data"]["id"]
        listed = self.execute("task_manager", {"action": "list", "status": "open"})
        updated = self.execute(
            "task_manager", {"action": "update", "task_id": task_id, "status": "completed"}
        )
        note = self.execute(
            "notes", {"action": "add", "title": "Router", "content": "Restart steps", "tags": ["home"]}
        )
        reminder = self.execute(
            "reminders",
            {"action": "schedule", "text": "Call back", "remind_at": "2026-09-01T10:00:00+05:30"},
        )

        self.assertEqual(listed["data"][0]["id"], task_id)
        self.assertEqual(updated["data"]["status"], "completed")
        self.assertTrue(note["data"]["id"].startswith("note_"))
        self.assertTrue(reminder["data"]["id"].startswith("rem_"))
        self.assertEqual(len(TaskStore(self.store.data_dir).list_tasks("completed")), 1)

    def test_secret_values_are_redacted_and_audit_hashes_payloads(self) -> None:
        secret = "unit-test-secret-12345"
        with patch.dict(os.environ, {"UNIT_TEST_API_KEY": secret}, clear=False):
            registry = ToolRegistry(workspace=self.workspace, task_store=self.store)
        self.assertEqual(registry._redact(f"Bearer {secret} and {secret}"), "Bearer [REDACTED] and [REDACTED]")

        result = json.loads(
            registry.execute("write_file", {"path": "audit.txt", "content": "private payload", "mode": "create"})
        )
        self.assertTrue(result["ok"])
        records = [json.loads(line) for line in registry.audit_file.read_text(encoding="utf-8").splitlines()]
        record = records[-1]
        self.assertEqual(record["tool"], "write_file")
        self.assertEqual(record["arguments"]["content"]["characters"], len("private payload"))
        self.assertEqual(len(record["arguments"]["content"]["sha256"]), 12)
        self.assertNotIn("private payload", registry.audit_file.read_text(encoding="utf-8"))

    def test_environment_sanitizer_removes_credentials(self) -> None:
        injected = {
            "SAFE_TEST_SETTING": "visible",
            "TOKEN": "hidden-token",
            "SERVICE_PASSWORD": "hidden-password",
            "CUSTOM_SECRET": "hidden-secret",
            "CUSTOM_API_KEY": "hidden-key",
        }
        with patch.dict(os.environ, injected, clear=False):
            sanitized = self.registry._sanitized_environment()
        self.assertEqual(sanitized["SAFE_TEST_SETTING"], "visible")
        for key in injected:
            if key != "SAFE_TEST_SETTING":
                self.assertNotIn(key, sanitized)

    def test_public_url_guard_blocks_local_addresses_without_live_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "Local network"):
            self.registry._validate_public_url("http://localhost/private")

        private_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch("aisha.tools.registry.socket.getaddrinfo", return_value=private_result):
            with self.assertRaisesRegex(ValueError, "Local/private"):
                self.registry._validate_public_url("https://internal.example.test/")

        public_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("aisha.tools.registry.socket.getaddrinfo", return_value=public_result):
            self.registry._validate_public_url("https://public.example.test/resource")

    def test_unknown_tools_and_invalid_arguments_return_structured_errors(self) -> None:
        unknown = self.execute("does_not_exist", {"value": 1})
        invalid = self.execute("read_file", {"path": "missing.txt"})
        self.assertEqual(unknown, {"ok": False, "error": "Unknown tool 'does_not_exist'"})
        self.assertFalse(invalid["ok"])
        self.assertIn("missing.txt", invalid["error"])


if __name__ == "__main__":
    unittest.main()
