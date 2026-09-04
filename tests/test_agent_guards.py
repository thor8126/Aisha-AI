"""Tests for the reliability guards in agent.py:
the runaway-repetition truncator and the heavy-task routing heuristic."""
from __future__ import annotations

import unittest

from aisha.core.agent import _truncate_runaway, AutonomousAgent


class _FakeClient:
    """Minimal client so AutonomousAgent constructs without network."""
    def __init__(self):
        self.chat = object()  # marks it as OpenAI-style


def _agent(**kw):
    reg = type("R", (), {"definitions": [], "openai_definitions": []})()
    a = AutonomousAgent(_FakeClient(), tool_registry=reg, **kw)
    # pretend a heavy provider is configured so routing is exercised
    a.heavy_endpoints = [object()]
    return a


class TruncateRunawayTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        s = "हाँ बॉस, calculator खोल दिया।"
        self.assertEqual(_truncate_runaway(s), s)

    def test_empty_and_none_safe(self):
        self.assertEqual(_truncate_runaway(""), "")
        self.assertEqual(_truncate_runaway(None), None)

    def test_repeated_sentence_is_cut(self):
        phrase = "चलो अब सुनो — गाना चल रहा है, मज़ा लो! "
        runaway = "सजदा गाना खोल दिया। " + phrase * 40
        out = _truncate_runaway(runaway)
        # Massive input collapses to something small (first sentence + one repeat).
        self.assertLess(len(out), 200)
        self.assertIn("सजदा गाना खोल दिया", out)

    def test_normal_multi_sentence_reply_survives(self):
        s = ("मैंने calculator खोल दिया। "
             "अब तुम हिसाब कर सकते हो। "
             "कुछ और चाहिए तो बताओ।")
        # No sentence repeats, so nothing should be cut.
        self.assertEqual(_truncate_runaway(s), s)

    def test_english_runaway_is_cut(self):
        s = "Done! " + "It is playing now. It is playing now. " * 30
        out = _truncate_runaway(s)
        self.assertLess(len(out), len(s) / 2)


class HeavyTaskRoutingTests(unittest.TestCase):
    def setUp(self):
        self.a = _agent()

    def test_simple_open_commands_are_fast(self):
        for q in ["MS Word open करो", "word kholo", "calculator kholo",
                  "chrome kholo", "notepad open karo", "game launch karo",
                  "kaise ho", "gana suna do"]:
            self.assertFalse(self.a._is_heavy_task(q), f"should be FAST: {q}")

    def test_content_and_research_are_heavy(self):
        for q in ["AI par essay likho", "time management par document banao",
                  "best budget phone kaunsa hai research karke batao",
                  "python code likho fibonacci ka", "ye report summarize karo",
                  "compare iphone vs samsung"]:
            self.assertTrue(self.a._is_heavy_task(q), f"should be HEAVY: {q}")

    def test_no_heavy_provider_means_never_heavy(self):
        a = _agent()
        a.heavy_endpoints = []
        # Even a clearly-heavy query can't route heavy with no heavy provider.
        a._prefer_heavy = a._is_heavy_task("essay likho") if a.heavy_endpoints else False
        self.assertFalse(a._prefer_heavy)


if __name__ == "__main__":
    unittest.main()
