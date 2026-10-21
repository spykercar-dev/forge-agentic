import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from forge.provider import OpenAIProvider, ProviderError


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                cls.last_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.last_auth = self.headers.get("Authorization")
                self.respond()

            def respond(self):
                cls.last_path = self.path
                self.send_response(cls.status)
                if cls.status == 302:
                    self.send_header("Location", "/redirected")
                self.end_headers()
                self.wfile.write(cls.response)

            def log_message(self, *args):
                pass

        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            raise unittest.SkipTest("This sandbox does not permit local HTTP sockets")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).status = 200
        type(self).response = b'{}'
        self.provider = OpenAIProvider(f"http://127.0.0.1:{self.server.server_port}/v1", "secret")

    def reply(self, value):
        type(self).response = json.dumps(value).encode()

    def test_models(self):
        self.reply({"data": [{"id": "qwen"}, {"id": "llama"}, {"id": "qwen"}]})
        self.assertEqual(self.provider.list_models(), ["qwen", "llama"])
        self.assertEqual(self.last_path, "/v1/models")

    def test_completion(self):
        message = {"role": "assistant", "content": "Hello"}
        self.reply({"choices": [{"message": message}]})
        self.assertEqual(self.provider.complete("qwen", [{"role": "user", "content": "Hi"}]), message)
        self.assertEqual(self.last_path, "/v1/chat/completions")
        self.assertEqual(self.last_auth, "Bearer secret")
        self.assertEqual(self.last_body["model"], "qwen")
        self.assertFalse(self.last_body["stream"])

    def test_tool_completion(self):
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
        self.reply({"choices": [{"message": message}]})
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        self.assertEqual(self.provider.complete("qwen", [], tools), message)
        self.assertEqual(self.last_body["tools"], tools)

    def test_invalid_responses(self):
        for response in (b"bad json", b"[]", b'{"error":"secret"}', b'{"data":[{}]}'):
            type(self).response = response
            with self.subTest(response=response), self.assertRaises(ProviderError):
                self.provider.list_models()

    def test_invalid_completion(self):
        for value in ({}, {"choices": []}, {"choices": [{"message": {"role": "user"}}]},
                      {"choices": [{"message": {"role": "assistant", "tool_calls": [None]}}]}):
            self.reply(value)
            with self.subTest(value=value), self.assertRaises(ProviderError):
                self.provider.complete("qwen", [])

    def test_http_error_redacts_body(self):
        type(self).status = 401
        type(self).response = b"secret"
        with self.assertRaises(ProviderError) as caught:
            self.provider.list_models()
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_redirect_rejected(self):
        type(self).status = 302
        with self.assertRaisesRegex(ProviderError, "302"):
            self.provider.list_models()
        self.assertEqual(self.last_path, "/v1/models")

    def test_url_security(self):
        for url in ("http://example.com/v1", "http://user:pass@localhost/v1", "file:///tmp/foo"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                OpenAIProvider(url, "secret")
        OpenAIProvider("https://example.com/v1", "secret")
        OpenAIProvider("http://localhost:11434/v1", "secret")
        OpenAIProvider("http://[::1]:11434/v1", "secret")
        OpenAIProvider("http://example.com/v1")


class ProviderFailureTests(unittest.TestCase):
    def test_timeout(self):
        provider = OpenAIProvider("http://localhost:1234/v1")
        with patch.object(provider._opener, "open", side_effect=TimeoutError):
            with self.assertRaisesRegex(ProviderError, "timed out"):
                provider.list_models()

    def test_connection_error_is_redacted(self):
        provider = OpenAIProvider("http://localhost:1234/v1")
        with patch.object(provider._opener, "open", side_effect=urllib.error.URLError("secret")):
            with self.assertRaises(ProviderError) as caught:
                provider.list_models()
        self.assertNotIn("secret", str(caught.exception))

    def test_url_security_without_network(self):
        for url in ("http://example.com/v1", "http://user:pass@localhost/v1", "file:///tmp/foo"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                OpenAIProvider(url, "secret")


if __name__ == "__main__":
    unittest.main()
