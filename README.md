# Forge

A small, extensible terminal coding agent that runs against your choice of open-source language model. MIT licensed, Python 3.10+, and no runtime Python dependencies or proprietary SDKs. Linux is the supported platform; shell execution uses POSIX process controls.

Forge connects to an inference server, asks the model what to do, executes approved tools, returns their results to the model, and repeats until the task is finished or the step limit is reached. It can inspect projects, search text, propose and apply edits, and optionally execute tests or other commands.

## Quick start

Start an [Ollama](https://github.com/ollama/ollama) server and load a tool-capable coding model of your choice:

```bash
ollama serve
# In another terminal; replace MODEL with an actual model tag:
ollama pull MODEL
```

Install Forge from this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
forge --list-models
forge --model MODEL --save-config
```

Without `--model`, Forge displays the server's models and asks you to choose. The model must support OpenAI-style function/tool calls. Model availability, licenses, hardware requirements, and tool-call quality depend on your chosen model and server. Forge does not download model weights or start inference servers.

You can also run directly without installation:

```bash
PYTHONPATH=src python3 -m forge --model MODEL
```

## Tasks and conversation

```bash
forge --model MODEL --root /path/to/project
forge --model MODEL "Inspect this project and explain its architecture"
forge --model MODEL --allow-shell "Fix the failing tests and run them"
```

The interactive session retains conversation context. `/models` lists models, `/model NAME` changes model and resets context, `/reset` clears the conversation, and `/exit` quits. Ctrl-C stops the process. One-shot tasks return a nonzero exit status on errors or step exhaustion.

## Choose your server

| Server | Option | Default API URL |
| --- | --- | --- |
| Ollama | `--provider ollama` | `http://localhost:11434/v1` |
| llama.cpp | `--provider llamacpp` | `http://localhost:8080/v1` |
| vLLM | `--provider vllm` | `http://localhost:8000/v1` |
| Other compatible server | `--base-url URL` | Your URL, including `/v1` |

For llama.cpp, enable tool calling using the server's `--jinja` option and an appropriate model/template; see its [function calling guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md). Ollama documents its supported API features in its [OpenAI compatibility guide](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx). For vLLM, configure a suitable [tool parser and chat template](https://docs.vllm.ai/en/latest/features/tool_calling/) for your selected model. Compatible API syntax alone does not guarantee a model will call tools reliably.

```bash
forge --base-url http://localhost:8080/v1 --model YOUR_SERVED_MODEL
```

Configuration precedence is CLI flags, environment, saved config, then defaults. `--provider` is an explicit endpoint flag and overrides the environment. Environment variables:

- `FORGE_BASE_URL`: compatible API endpoint.
- `FORGE_MODEL`: model ID.
- `FORGE_API_KEY`: optional bearer token, never saved by Forge.

`--save-config` saves only the selected model and endpoint to `~/.config/forge/config.json`. Use `--config PATH` to choose another file. Remote endpoints with API keys require HTTPS. Requests send project content and tool results to the configured server, so use a local server when you want local inference.

## Tools and permissions

Read, list, and search tools operate inside `--root`. File changes request approval and show the proposed change. Shell execution requires `--allow-shell` plus approval for every command. Approvals default to no, and are denied when input is not a terminal. There is deliberately no automatic approval flag.

**Shell execution is not sandboxed.** Commands inherit your user permissions and environment and can access files or networks beyond the workspace. Use a container or a restricted operating-system account when executing untrusted commands. File-path checks are application safeguards, not an OS sandbox; do not run concurrently with an adversary changing workspace symlinks. Read tool output and proposed actions before approving them.

The loop defaults to 20 model calls per task; change it with `--max-steps`. Model requests time out after 120 seconds; use `--timeout SECONDS` for slower inference. Shell commands time out after 30 seconds. Tool output and file sizes are bounded. Approval diffs larger than 16,000 characters are rejected; ask for smaller edits. Conversation history stays in memory and is not persisted. This first version does not include streaming, automatic context compaction, MCP, background subagents, or session recovery. Long conversations may exceed your model's context window; use `/reset` to start fresh.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests use fake models and local HTTP servers; no model download or external API account is required.

The implementation separates `provider.py` (HTTP protocol), `agent.py` (agent loop), `tools.py` (execution and permissions), and `cli.py` (terminal and configuration). Add new tools by extending the executor's schemas and dispatch; replace the provider to use another inference protocol.
