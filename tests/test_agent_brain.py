from __future__ import annotations

import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from agent_brain import AutonomousAgent


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        # The agent intentionally appends to its message list between calls; keep
        # a point-in-time snapshot so assertions reflect what the client received.
        self.calls.append(deepcopy(kwargs))
        if not self.outcomes:
            raise AssertionError("Fake client received an unexpected model call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.messages = FakeMessages(outcomes)


class FakeRegistry:
    def __init__(self, warnings=None, results=None):
        self.definitions = [
            {
                "name": "get_datetime",
                "description": "fake safe tool",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
        self.warnings = warnings or {}
        self.results = results or {}
        self.executed = []

    def confirmation_summary(self, name, arguments):
        return self.warnings.get(name)

    def execute(self, name, arguments):
        self.executed.append((name, arguments))
        return self.results.get(name, json.dumps({"ok": True, "data": {"tool": name}}))


def text_response(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def tool_response(*calls):
    blocks = [
        SimpleNamespace(type="tool_use", id=identifier, name=name, input=arguments)
        for identifier, name, arguments in calls
    ]
    return SimpleNamespace(content=blocks, stop_reason="tool_use")


class TemporaryApiError(RuntimeError):
    status_code = 429


class AutonomousAgentTests(unittest.TestCase):
    def test_native_tool_loop_executes_safe_call_and_returns_final_text(self) -> None:
        client = FakeClient(
            [
                tool_response(("call-1", "get_datetime", {})),
                text_response("Verified final answer"),
            ]
        )
        registry = FakeRegistry(results={"get_datetime": '{"ok": true, "data": {"time": "10:00"}}'})
        agent = AutonomousAgent(client, model="fake-model", tool_registry=registry, max_steps=4, max_tokens=123)

        answer = agent.run_task(
            "What time is it?",
            "Base system prompt",
            conversation_history=[{"user": "Earlier", "assistant": "Hello"}],
        )

        self.assertEqual(answer, "Verified final answer")
        self.assertEqual(registry.executed, [("get_datetime", {})])
        self.assertEqual(len(client.messages.calls), 2)
        first_call = client.messages.calls[0]
        self.assertEqual(first_call["model"], "fake-model")
        self.assertEqual(first_call["max_tokens"], 123)
        self.assertEqual(first_call["tools"], registry.definitions)
        self.assertIn("<runtime_rules>", first_call["system"])
        self.assertEqual(first_call["messages"][-1], {"role": "user", "content": "What time is it?"})

        second_messages = client.messages.calls[1]["messages"]
        self.assertEqual(second_messages[-2]["role"], "assistant")
        self.assertEqual(second_messages[-2]["content"][0]["type"], "tool_use")
        tool_result = second_messages[-1]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "call-1")
        self.assertFalse(tool_result["is_error"])

    def test_dangerous_batch_waits_then_confirmation_executes_every_call(self) -> None:
        client = FakeClient(
            [
                tool_response(
                    ("safe-1", "get_datetime", {}),
                    ("danger-1", "write_file", {"path": "existing.txt", "content": "changed"}),
                ),
                text_response("Both actions are now complete"),
            ]
        )
        registry = FakeRegistry(warnings={"write_file": "Existing file change karni hai"})
        agent = AutonomousAgent(client, tool_registry=registry, max_steps=4)

        paused = agent.run_task("Update the file after checking time", "system")
        # The confirmation prompt is worded with variety, but it must surface the
        # pending action's warning text and leave the batch pending, unexecuted.
        self.assertIn("Existing file change karni hai", paused)
        self.assertTrue(agent.has_pending_confirmation)
        self.assertEqual(registry.executed, [])

        waiting = agent.run_task("what is happening", "ignored system")
        self.assertIn("confirmation ka wait", waiting)
        self.assertEqual(len(client.messages.calls), 1)
        self.assertEqual(registry.executed, [])

        finished = agent.run_task("haan kar do", "ignored system")
        self.assertEqual(finished, "Both actions are now complete")
        self.assertFalse(agent.has_pending_confirmation)
        self.assertEqual(
            registry.executed,
            [
                ("get_datetime", {}),
                ("write_file", {"path": "existing.txt", "content": "changed"}),
            ],
        )

    def test_cancel_discards_pending_batch_without_execution(self) -> None:
        client = FakeClient([tool_response(("danger", "delete_path", {"path": "old.txt"}))])
        registry = FakeRegistry(warnings={"delete_path": "Permanent deletion"})
        agent = AutonomousAgent(client, tool_registry=registry)

        agent.run_task("Delete old.txt", "system")
        answer = agent.run_task("cancel", "system")

        self.assertIn("cancel kar diya", answer)
        self.assertFalse(agent.has_pending_confirmation)
        self.assertEqual(registry.executed, [])
        self.assertEqual(len(client.messages.calls), 1)

    def test_expired_confirmation_is_not_executed(self) -> None:
        client = FakeClient([tool_response(("danger", "move_path", {"source": "a", "destination": "b"}))])
        registry = FakeRegistry(warnings={"move_path": "Move file"})
        agent = AutonomousAgent(client, tool_registry=registry)
        agent.run_task("Move it", "system")
        self.assertIsNotNone(agent.pending)
        agent.pending.created_at -= 301

        answer = agent.run_task("haan kar do", "system")

        self.assertIn("expire", answer)
        self.assertIsNone(agent.pending)
        self.assertEqual(registry.executed, [])
        self.assertEqual(len(client.messages.calls), 1)

    def test_tool_failures_are_marked_for_the_followup_model_call(self) -> None:
        client = FakeClient([tool_response(("bad-1", "get_datetime", {})), text_response("I could not verify it")])
        registry = FakeRegistry(results={"get_datetime": '{"ok": false, "error": "clock unavailable"}'})
        agent = AutonomousAgent(client, tool_registry=registry)

        answer = agent.run_task("Check time", "system")

        self.assertEqual(answer, "I could not verify it")
        result = client.messages.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("clock unavailable", result["content"])

    def test_one_transient_api_failure_is_retried(self) -> None:
        client = FakeClient([TemporaryApiError("rate limited"), text_response("Recovered")])
        agent = AutonomousAgent(client, tool_registry=FakeRegistry())

        with patch("agent_brain.time.sleep") as sleep:
            answer = agent.run_task("Hello", "system")

        self.assertEqual(answer, "Recovered")
        self.assertEqual(len(client.messages.calls), 2)
        sleep.assert_called_once_with(1.0)

    def test_history_is_bounded_and_empty_requests_do_not_call_api(self) -> None:
        client = FakeClient([text_response("Done")])
        agent = AutonomousAgent(client, tool_registry=FakeRegistry())
        history = [
            {"user": f"user-{index}" * 300, "assistant": f"assistant-{index}" * 300}
            for index in range(8)
        ]

        self.assertEqual(agent.run_task("   ", "system"), "Kya karna hai, bas bata do.")
        answer = agent.run_task("current", "system", conversation_history=history)

        self.assertEqual(answer, "Done")
        messages = client.messages.calls[0]["messages"]
        self.assertEqual(len(messages), 11)
        self.assertEqual(messages[0]["role"], "user")
        self.assertLessEqual(len(messages[0]["content"]), 1000)
        self.assertEqual(messages[-1]["content"], "current")

    def test_thread_persists_tool_context_across_turns(self) -> None:
        # Turn 1 runs a tool; turn 2 must SEE that tool_use + tool_result in the
        # thread it sends to the model — real cross-turn awareness, not a one-line
        # text summary rebuilt from scratch.
        client = FakeClient(
            [
                tool_response(("call-1", "get_datetime", {})),
                text_response("WhatsApp खुल गया"),
                text_response("हाँ, अभी भी खुला है"),
            ]
        )
        registry = FakeRegistry(results={"get_datetime": '{"ok": true, "data": {"opened": true}}'})
        agent = AutonomousAgent(client, tool_registry=registry, max_steps=4)

        agent.run_task("WhatsApp open karo", "system")
        agent.run_task("abhi bhi khula hai kya?", "system")

        # The third API call (turn 2) must carry the whole prior thread.
        turn2_messages = client.messages.calls[2]["messages"]
        types = [
            (m["role"], m["content"][0]["type"] if isinstance(m["content"], list) else "text")
            for m in turn2_messages
        ]
        self.assertIn(("assistant", "tool_use"), types)
        self.assertIn(("user", "tool_result"), types)
        self.assertEqual(turn2_messages[-1]["content"], "abhi bhi khula hai kya?")

    def test_reset_conversation_clears_thread(self) -> None:
        client = FakeClient([text_response("one"), text_response("two")])
        agent = AutonomousAgent(client, tool_registry=FakeRegistry())
        agent.run_task("first", "system")
        agent.reset_conversation()
        agent.run_task("second", "system")
        # After reset, turn 2's thread starts fresh (only the new user message).
        self.assertEqual(len(client.messages.calls[1]["messages"]), 1)
        self.assertEqual(client.messages.calls[1]["messages"][0]["content"], "second")

    def test_step_limit_returns_honest_incomplete_message(self) -> None:
        client = FakeClient([tool_response(("only-step", "get_datetime", {}))])
        registry = FakeRegistry()
        agent = AutonomousAgent(client, tool_registry=registry, max_steps=1)

        answer = agent.run_task("Do a longer task", "system")

        self.assertIn("finish nahi kar paayi", answer)
        self.assertEqual(registry.executed, [("get_datetime", {})])


if __name__ == "__main__":
    unittest.main()
