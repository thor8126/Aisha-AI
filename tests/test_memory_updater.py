"""Tests for data integrity in memory.py and version logic in updater.py."""
from __future__ import annotations

import os
import tempfile
import unittest

from aisha.core import memory as mem
from aisha.system import updater


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig = mem.MEMORY_FILE
        mem.MEMORY_FILE = os.path.join(self._tmp, "memory.json")

    def tearDown(self):
        mem.MEMORY_FILE = self._orig

    def test_load_missing_returns_default(self):
        data = mem.load()
        self.assertIsInstance(data, dict)
        # Default memory has the standard keys.
        self.assertIn("conversation_history", data)

    def test_save_then_load_roundtrip(self):
        data = mem.load()
        data["user_name"] = "Boss"
        data.setdefault("facts", []).append("likes chai")
        mem.save(data)
        again = mem.load()
        self.assertEqual(again["user_name"], "Boss")
        self.assertIn("likes chai", again["facts"])

    def test_corrupt_file_falls_back_to_default(self):
        with open(mem.MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write("{ this is : not valid json ][")
        data = mem.load()  # must not raise
        self.assertIsInstance(data, dict)
        self.assertIn("conversation_history", data)

    def test_missing_keys_are_merged_from_defaults(self):
        # A partial file (only one key) should still load with all defaults present.
        with open(mem.MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write('{"user_name": "X"}')
        data = mem.load()
        self.assertEqual(data["user_name"], "X")
        self.assertIn("conversation_history", data)  # filled from default

    def test_save_is_atomic_no_tmp_left_behind(self):
        data = mem.load()
        mem.save(data)
        self.assertFalse(os.path.exists(mem.MEMORY_FILE + ".tmp"))
        self.assertTrue(os.path.exists(mem.MEMORY_FILE))

    def test_history_does_not_grow_unbounded(self):
        data = mem.load()
        for i in range(200):
            mem.update_from_conversation(data, f"user {i}", f"reply {i}")
        # Rotation must keep the stored history bounded (not all 200 turns).
        self.assertLess(len(data.get("conversation_history", [])), 200)


class UpdaterVersionTests(unittest.TestCase):
    def test_is_newer_semantics(self):
        self.assertTrue(updater.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(updater.is_newer("v1.1.0", "1.0.9"))
        self.assertTrue(updater.is_newer("2.0.0", "1.9.9"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0.1"))
        self.assertFalse(updater.is_newer("v1.0.0", "v1.0.0"))

    def test_parse_tolerates_prefixes_and_missing_parts(self):
        self.assertEqual(updater._parse("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater._parse("1.2"), (1, 2, 0))
        self.assertEqual(updater._parse("1"), (1, 0, 0))
        self.assertEqual(updater._parse(""), (0, 0, 0))

    def test_apply_update_is_noop_from_source(self):
        # Not frozen (running from source) → apply_update must safely do nothing.
        self.assertFalse(updater.is_frozen())
        self.assertFalse(updater.apply_update({"asset": "http://example/x.zip"}))


if __name__ == "__main__":
    unittest.main()
