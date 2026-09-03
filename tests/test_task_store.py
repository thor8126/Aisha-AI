from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aisha-store-test-")
        self.data_dir = Path(self.temporary.name)
        self.store = TaskStore(self.data_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_task_crud_persists_and_returns_defensive_copies(self) -> None:
        created = self.store.create_task(
            "  Finish report  ",
            details="  include sources  ",
            due_at="2026-09-01T18:30:00+05:30",
            priority="high",
        )
        self.assertTrue(created["id"].startswith("task_"))
        self.assertEqual(created["title"], "Finish report")
        self.assertEqual(created["details"], "include sources")
        self.assertEqual(created["due_at"], "2026-09-01T18:30:00+05:30")

        created["title"] = "mutated outside the store"
        self.assertEqual(self.store.list_tasks()[0]["title"], "Finish report")

        completed = self.store.update_task(created["id"], status="completed", priority="low")
        self.assertEqual(completed["status"], "completed")
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(len(self.store.list_tasks("completed")), 1)

        reopened = self.store.update_task(created["id"], status="open", due_at="")
        self.assertIsNone(reopened["completed_at"])
        self.assertIsNone(reopened["due_at"])

        reloaded = TaskStore(self.data_dir)
        self.assertEqual(reloaded.list_tasks("all")[0]["status"], "open")
        deleted = reloaded.delete_task(created["id"])
        self.assertEqual(deleted["id"], created["id"])
        self.assertEqual(reloaded.list_tasks("all"), [])

    def test_task_validation_and_missing_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "title"):
            self.store.create_task("   ")
        with self.assertRaisesRegex(ValueError, "Priority"):
            self.store.create_task("x", priority="urgent")
        with self.assertRaisesRegex(ValueError, "ISO date/time"):
            self.store.create_task("x", due_at="tomorrow morning")
        with self.assertRaisesRegex(ValueError, "Status"):
            self.store.list_tasks("unknown")
        with self.assertRaisesRegex(KeyError, "No task found"):
            self.store.update_task("task_missing", status="completed")

    def test_notes_are_searchable_case_insensitively_and_persist(self) -> None:
        tags = [f" tag-{index} " for index in range(12)] + ["  "]
        first = self.store.add_note(" Project Atlas ", "Call Priya about the launch", tags)
        self.store.add_note("Shopping", "Buy coffee", ["personal"])

        by_content = self.store.search_notes("PRIYA")
        by_tag = self.store.search_notes("tag-4")
        self.assertEqual([item["id"] for item in by_content], [first["id"]])
        self.assertEqual([item["id"] for item in by_tag], [first["id"]])
        self.assertEqual(len(first["tags"]), 10)
        self.assertEqual(first["tags"][0], "tag-0")

        first["content"] = "external mutation"
        self.assertIn("Priya", self.store.search_notes("Priya")[0]["content"])
        self.assertEqual(len(TaskStore(self.data_dir).search_notes()), 2)

        deleted = self.store.delete_note(first["id"])
        self.assertEqual(deleted["title"], "Project Atlas")
        with self.assertRaisesRegex(ValueError, "both a title and content"):
            self.store.add_note("title", " ")

    def test_due_reminders_are_delivered_once(self) -> None:
        due = self.store.schedule_reminder("Join meeting", "2026-08-31T11:59:00+00:00")
        future = self.store.schedule_reminder("Future task", "2026-08-31T12:01:00+00:00")
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

        delivered = self.store.pop_due_reminders(now)
        self.assertEqual([item["id"] for item in delivered], [due["id"]])
        self.assertEqual(delivered[0]["status"], "delivered")
        self.assertIsNotNone(delivered[0]["delivered_at"])
        self.assertEqual(self.store.pop_due_reminders(now), [])
        self.assertEqual([item["id"] for item in self.store.list_reminders()], [future["id"]])
        self.assertEqual([item["id"] for item in TaskStore(self.data_dir).list_reminders("delivered")], [due["id"]])

    def test_corrupt_state_is_quarantined_and_partial_state_is_repaired(self) -> None:
        self.store.state_file.write_text("{not-json", encoding="utf-8")
        recovered = TaskStore(self.data_dir)
        self.assertEqual(recovered.list_tasks("all"), [])
        backups = list(self.data_dir.glob("state.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not-json")

        recovered.state_file.write_text(json.dumps({"tasks": "wrong", "notes": []}), encoding="utf-8")
        repaired = TaskStore(self.data_dir)
        self.assertEqual(repaired.list_tasks("all"), [])
        self.assertEqual(repaired.search_notes(), [])
        self.assertEqual(repaired.list_reminders("all"), [])

    def test_concurrent_writes_leave_valid_complete_state(self) -> None:
        def create(index: int) -> str:
            return self.store.create_task(f"Task {index}")["id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = list(pool.map(create, range(16)))

        self.assertEqual(len(set(identifiers)), 16)
        saved = json.loads(self.store.state_file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["tasks"]), 16)
        self.assertFalse(self.store.state_file.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
