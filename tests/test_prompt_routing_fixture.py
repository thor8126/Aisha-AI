from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assistant_tools import ToolRegistry
from task_store import TaskStore


FIXTURE = Path(__file__).parent / "fixtures" / "prompt_routing_cases.json"


class PromptRoutingFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.cases = cls.payload["cases"]

    def test_fixture_has_twenty_well_formed_unique_cases(self) -> None:
        self.assertEqual(self.payload["version"], 1)
        self.assertEqual(len(self.cases), 20)
        identifiers = [case["id"] for case in self.cases]
        self.assertEqual(len(identifiers), len(set(identifiers)))

        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertRegex(case["id"], r"^[a-z0-9_]+$")
                self.assertTrue(case["prompt"].strip())
                self.assertTrue(case["expectation"].strip())
                self.assertTrue(case["expected_tools"])
                self.assertTrue(all(isinstance(name, str) and name for name in case["expected_tools"]))
                self.assertTrue(case["tags"])
                confirmation = case["confirmation"]
                self.assertIn(confirmation["tool"], case["expected_tools"])
                self.assertIsInstance(confirmation["arguments"], dict)
                self.assertIsInstance(confirmation["required"], bool)

    def test_fixture_covers_requested_routing_categories_and_registered_tools(self) -> None:
        required_tags = {
            "current_time",
            "web_search",
            "web_fetch",
            "read_file",
            "write_file",
            "copy_path",
            "delete_path",
            "shell",
            "python",
            "apps",
            "tasks",
            "notes",
            "reminders",
            "memory",
            "clipboard",
            "prompt_injection",
            "failure_handling",
            "confirmation",
        }
        actual_tags = {tag for case in self.cases for tag in case["tags"]}
        self.assertTrue(required_tags.issubset(actual_tags), required_tags - actual_tags)

        with tempfile.TemporaryDirectory(prefix="aisha-routing-tools-") as directory:
            root = Path(directory)
            registry = ToolRegistry(workspace=root, task_store=TaskStore(root / ".state"))
            routed_tools = {name for case in self.cases for name in case["expected_tools"]}
            self.assertTrue(routed_tools.issubset(registry.names), routed_tools - registry.names)

    def test_declared_confirmation_expectations_match_registry_policy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aisha-routing-policy-") as directory:
            root = Path(directory)
            registry = ToolRegistry(workspace=root, task_store=TaskStore(root / ".state"))
            for case in self.cases:
                for relative_path in case.get("prepare_existing", []):
                    path = root / relative_path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture", encoding="utf-8")

            for case in self.cases:
                with self.subTest(case=case["id"]):
                    confirmation = case["confirmation"]
                    warning = registry.confirmation_summary(confirmation["tool"], confirmation["arguments"])
                    self.assertEqual(warning is not None, confirmation["required"])

    def test_adversarial_and_failure_cases_encode_safe_outcomes(self) -> None:
        by_id = {case["id"]: case for case in self.cases}
        injection = by_id["web_prompt_injection"]["expectation"].casefold()
        failure = by_id["web_failure_handling"]["expectation"].casefold()
        self.assertIn("untrusted", injection)
        self.assertIn("ignore instructions", injection)
        self.assertIn("real error", failure)
        self.assertIn("never claim", failure)


if __name__ == "__main__":
    unittest.main()
