import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.cli import main


class FakeResponse(io.BytesIO):
    pass


class CLIIntegrationTests(unittest.TestCase):
    def test_http_protocol_through_agent_to_workspace_tool(self):
        requests = []

        def respond(request, timeout):
            payload = json.loads(request.data)
            requests.append(payload)
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "read-1", "type": "function", "function": {
                        "name": "read_file", "arguments": '{"path":"hello.txt"}'}}]}
            else:
                result = json.loads(payload["messages"][-1]["content"])
                self.assertEqual(result["content"], "hello from workspace")
                message = {"role": "assistant", "content": "Read hello.txt successfully."}
            return FakeResponse(json.dumps({"choices": [{"message": message}]}).encode())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hello.txt").write_text("hello from workspace")
            output = io.StringIO()
            with patch("urllib.request.OpenerDirector.open", side_effect=respond), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--root", directory, "--config", str(root / "config.json"), "--model", "my-open-model", "Read hello.txt"])
            self.assertEqual(code, 0)
            self.assertIn("Read hello.txt successfully", output.getvalue())
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]["model"], "my-open-model")
            self.assertTrue(requests[0]["tools"])

    def test_config_roundtrip_never_saves_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            with patch.dict("os.environ", {"FORGE_API_KEY": "test-secret"}), patch("forge.cli.Agent.run", return_value="Done"), contextlib.redirect_stdout(io.StringIO()):
                code = main(["--root", directory, "--config", str(config), "--model", "chosen", "--base-url", "http://localhost:1234/v1", "--save-config", "hello"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(config.read_text()), {"base_url": "http://localhost:1234/v1", "model": "chosen"})

    def test_denied_write_does_not_change_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "file.txt"
            file.write_text("original")
            responses = [
                {"role": "assistant", "content": None, "tool_calls": [{"id": "write-1", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"file.txt","content":"changed"}'}}]},
                {"role": "assistant", "content": "User denied edit."},
            ]
            with patch("forge.cli.OpenAIProvider.complete", side_effect=responses), patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--root", directory, "--config", str(root / "config.json"), "--model", "chosen", "edit file"])
            self.assertEqual(code, 0)
            self.assertEqual(file.read_text(), "original")

    def test_bad_config_returns_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"model": []}')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--config", str(config), "hello"]), 1)


if __name__ == "__main__":
    unittest.main()
