"""Small, dependency-free client for OpenAI-compatible model servers."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(RuntimeError):
    """An actionable model server failure, safe to display to a user."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials or conversation data to another endpoint.
        return None


class OpenAIProvider:
    """Connect to a server root such as http://localhost:11434/v1."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 120):
        try:
            parsed = urllib.parse.urlsplit(base_url)
            hostname = parsed.hostname
            parsed.port  # Validate malformed ports before making requests.
        except (ValueError, TypeError) as exc:
            raise ProviderError("Invalid model server URL.") from exc
        if (parsed.scheme not in {"http", "https"} or not hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ProviderError("Use an HTTP(S) model server URL without credentials, query, or fragment.")
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.lower() == "localhost"
        if api_key and parsed.scheme != "https" and not loopback:
            raise ProviderError("An API key requires HTTPS for servers outside localhost.")
        if timeout <= 0:
            raise ProviderError("The model server timeout must be positive.")
        if api_key and ("\r" in api_key or "\n" in api_key):
            raise ProviderError("The API key contains invalid characters.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, path: str, payload: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            try:
                data = json.dumps(payload).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ProviderError("The model request contains invalid JSON data.") from exc
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            hints = {401: "Check your API key.", 403: "Access was denied.",
                     404: "Check the base URL and model name.",
                     429: "The server is rate limited; retry later."}
            hint = hints.get(exc.code, "Check that the server supports the OpenAI-compatible API.")
            raise ProviderError(f"Model server returned HTTP {exc.code}. {hint}") from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("Model server request timed out; increase the timeout or try a smaller model.") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ProviderError("Model server request timed out; increase the timeout or try a smaller model.") from None
            raise ProviderError("Cannot reach the model server. Check the base URL and that the server is running.") from None
        except (OSError, ValueError):
            raise ProviderError("Model server connection failed. Check the server configuration.") from None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProviderError("Model server returned invalid JSON.") from None
        if not isinstance(result, dict):
            raise ProviderError("Model server returned an invalid response: expected a JSON object.")
        if "error" in result:
            raise ProviderError("Model server reported an error. Check the model name and server logs.")
        return result

    def list_models(self) -> list[str]:
        result = self._request("/models")
        data = result.get("data")
        if not isinstance(data, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not item["id"] for item in data
        ):
            raise ProviderError("Model server returned an invalid model list.")
        return list(dict.fromkeys(item["id"] for item in data))

    def complete(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                 temperature: float = 0.2) -> dict:
        payload = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
        if tools:
            payload["tools"] = tools
        result = self._request("/chat/completions", payload)
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("Model server returned no completion choices.")
        message = choices[0].get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ProviderError("Model server returned an invalid assistant message.")
        content, calls = message.get("content"), message.get("tool_calls")
        if content is not None and not isinstance(content, str):
            raise ProviderError("Model server returned invalid assistant content.")
        if calls is not None:
            if not isinstance(calls, list):
                raise ProviderError("Model server returned invalid tool calls.")
            for call in calls:
                if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                        or call.get("type") != "function" or not isinstance(call.get("function"), dict)
                        or not isinstance(call["function"].get("name"), str)
                        or not isinstance(call["function"].get("arguments"), str)):
                    raise ProviderError("Model server returned invalid tool calls.")
        if content is None and not calls:
            raise ProviderError("Model server returned an empty assistant message.")
        return message
