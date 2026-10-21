import tempfile
import unittest
from pathlib import Path

from forge.agent import Agent, AgentError


class Provider:
    def __init__(self, messages):
        self.responses = iter(messages)

    def complete(self, model, messages, tools):
        return next(self.responses)


class Executor:
    schemas = []

    def __init__(self, root):
        self.root = root
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return "file contents"


def tool(arguments='{"path":"file.txt"}'):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call1", "type": "function", "function": {"name": "read_file", "arguments": arguments}}
    ]}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.executor = Executor(Path(self.temp.name))

    def test_executes_tool_and_preserves_history_across_runs(self):
        events = []
        agent = Agent(Provider([tool(), {"content": "Done"}, {"content": "Again"}]),
                      "local-model", self.executor, on_event=lambda *event: events.append(event))
        self.assertEqual(agent.run("Read file"), "Done")
        self.assertEqual(self.executor.calls, [("read_file", {"path": "file.txt"})])
        self.assertEqual(agent.messages[3]["content"], "file contents")
        self.assertEqual(agent.run("Continue"), "Again")
        self.assertEqual([event[0] for event in events], ["tool_start", "tool_result"])

    def test_malformed_arguments_return_error_to_model(self):
        agent = Agent(Provider([tool("[1]"), {"content": "Recovered"}]), "model", self.executor)
        self.assertEqual(agent.run("Read"), "Recovered")
        self.assertIn("must decode to an object", agent.messages[3]["content"])
        self.assertEqual(self.executor.calls, [])

    def test_step_limit_keeps_tool_results(self):
        agent = Agent(Provider([tool()]), "model", self.executor, max_steps=1)
        with self.assertRaisesRegex(AgentError, "step limit"):
            agent.run("Continue indefinitely")
        self.assertEqual(agent.messages[-1]["role"], "tool")

    def test_project_guidance_is_bounded_lower_priority_data(self):
        (self.executor.root / "AGENTS.md").write_text("x" * 20_000)
        agent = Agent(Provider([]), "model", self.executor)
        self.assertEqual(agent.messages[1]["role"], "user")
        self.assertIn("truncated", agent.messages[1]["content"])
        self.assertLess(len(agent.messages[1]["content"]), 17_000)

    def test_tool_call_limit(self):
        response = tool()
        response["tool_calls"] *= 17
        agent = Agent(Provider([response]), "model", self.executor)
        with self.assertRaisesRegex(AgentError, "more than 16"):
            agent.run("Run tools")
        self.assertEqual(self.executor.calls, [])


if __name__ == "__main__":
    unittest.main()
