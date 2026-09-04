"""Tests for exe-critical modules: aisha.paths (root resolution) and
aisha.config (.env loading + first-run template). If these break, the
packaged app can't find its assets or start up."""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest


class PathsTests(unittest.TestCase):
    def test_project_root_has_assets(self):
        import aisha.paths as p
        importlib.reload(p)
        # Running from source, the resolved root must contain assets/.
        self.assertTrue(os.path.isdir(p.ASSETS_DIR),
                        f"assets not found under root: {p.PROJECT_ROOT}")

    def test_helpers_join_from_root(self):
        import aisha.paths as p
        importlib.reload(p)
        self.assertEqual(p.root("x", "y"), os.path.join(p.PROJECT_ROOT, "x", "y"))
        self.assertEqual(p.asset("live2d"), os.path.join(p.ASSETS_DIR, "live2d"))

    def test_aisha_root_env_override(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "assets"), exist_ok=True)
        os.environ["AISHA_ROOT"] = d
        try:
            import aisha.paths as p
            importlib.reload(p)
            self.assertEqual(os.path.normpath(p.PROJECT_ROOT), os.path.normpath(d))
        finally:
            os.environ.pop("AISHA_ROOT", None)
            importlib.reload(__import__("aisha.paths", fromlist=["paths"]))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self._tmp, "assets"), exist_ok=True)
        os.environ["AISHA_ROOT"] = self._tmp
        # Reload so config/paths pick up the temp root.
        import aisha.paths, aisha.config
        importlib.reload(aisha.paths)
        importlib.reload(aisha.config)
        self.config = aisha.config

    def tearDown(self):
        os.environ.pop("AISHA_ROOT", None)
        os.environ.pop("TOKEN", None)
        import aisha.paths, aisha.config
        importlib.reload(aisha.paths)
        importlib.reload(aisha.config)

    def test_first_run_creates_env_template(self):
        os.environ.pop("TOKEN", None)
        status = self.config.load_config()
        self.assertTrue(status["created_template"])
        self.assertTrue(os.path.isfile(status["env_path"]))
        # Template must contain the key placeholders users fill in.
        text = open(status["env_path"], encoding="utf-8").read()
        self.assertIn("TOKEN=", text)
        self.assertIn("ELEVEN_LAB=", text)

    def test_missing_required_key_is_reported(self):
        os.environ.pop("TOKEN", None)
        status = self.config.load_config()
        # Fresh template has an empty TOKEN → required key missing.
        self.assertFalse(status["has_required"])
        self.assertIn("TOKEN", status["missing"])

    def test_existing_env_is_not_overwritten(self):
        # Pre-create a .env with a real token.
        env_path = os.path.join(self._tmp, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write('TOKEN="already-here"\n')
        status = self.config.load_config()
        self.assertFalse(status["created_template"])  # didn't clobber
        self.assertTrue(status["has_required"])
        self.assertEqual(os.getenv("TOKEN"), "already-here")


if __name__ == "__main__":
    unittest.main()
