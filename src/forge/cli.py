"""Terminal entry point and configuration."""
import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .agent import Agent, AgentError
from .provider import OpenAIProvider, ProviderError
from .tools import ToolExecutor

PRESETS = {"ollama": "http://localhost:11434/v1", "llamacpp": "http://localhost:8080/v1", "vllm": "http://localhost:8000/v1"}


def parser():
    p = argparse.ArgumentParser(description="Forge — terminal coding agent for your choice of model")
    p.add_argument("prompt", nargs="?", help="Run one task; omit for interactive chat")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--provider", choices=PRESETS, help="Local server preset")
    p.add_argument("--base-url", help="OpenAI-compatible API URL including /v1")
    p.add_argument("--model", help="Model identifier served by your endpoint")
    p.add_argument("--root", type=Path, default=Path.cwd(), help="Workspace (default: current directory)")
    p.add_argument("--config", type=Path, default=Path.home() / ".config/forge/config.json")
    p.add_argument("--save-config", action="store_true", help="Save endpoint and selected model; never API keys")
    p.add_argument("--list-models", action="store_true", help="List models available from the server")
    p.add_argument("--allow-shell", action="store_true", help="Enable shell tool with per-command approval (not sandboxed)")
    p.add_argument("--max-steps", type=int, default=20, help="Maximum model calls per task")
    p.add_argument("--timeout", type=float, default=120, help="Model request timeout in seconds")
    return p


def load_config(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Config must be a JSON object")
    for key in ("base_url", "model"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValueError(f"Config {key} must be a nonempty string")
    return value


def approve(description):
    print(f"\nApproval requested: {description}", file=sys.stderr)
    if not sys.stdin.isatty():
        print("Denied: approvals require an interactive terminal.", file=sys.stderr)
        return False
    try:
        return input("Allow? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def event(kind, text):
    print(f"[{kind}] {text}", file=sys.stderr)


def choose_model(provider):
    models = provider.list_models()
    if not models:
        raise ValueError("Server has no models. Load a model in your inference server first.")
    if not sys.stdin.isatty():
        raise ValueError("Select a model using --model or FORGE_MODEL (see --list-models).")
    for index, model in enumerate(models, 1):
        print(f"  {index}. {model}")
    choice = input("Choose model number or identifier: ").strip()
    if choice.isdecimal() and 1 <= int(choice) <= len(models):
        return models[int(choice) - 1]
    if choice in models:
        return choice
    raise ValueError("Choose an available model number or identifier")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.max_steps < 1:
            raise ValueError("--max-steps must be positive")
        root = args.root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("--root must be a directory")
        config = load_config(args.config)
        base_url = args.base_url or (PRESETS[args.provider] if args.provider else None) or os.getenv("FORGE_BASE_URL") or config.get("base_url") or PRESETS["ollama"]
        provider = OpenAIProvider(base_url, api_key=os.getenv("FORGE_API_KEY"), timeout=args.timeout)
        if args.list_models:
            for model in provider.list_models():
                print(model)
            return 0
        model = args.model or os.getenv("FORGE_MODEL") or config.get("model") or choose_model(provider)
        if args.save_config:
            args.config.parent.mkdir(parents=True, exist_ok=True)
            args.config.write_text(json.dumps({"base_url": base_url, "model": model}, indent=2) + "\n")
        executor = ToolExecutor(root, approve=approve, allow_shell=args.allow_shell)
        agent = Agent(provider, model, executor, max_steps=args.max_steps, on_event=event)
        if args.prompt is not None:
            print(agent.run(args.prompt))
            return 0
        if not sys.stdin.isatty():
            raise ValueError("Provide a prompt argument when input is not a terminal")
        print(f"Forge {__version__} | {model} | {root}\n/help for commands. Ctrl-D to exit.")
        while True:
            try:
                prompt = input("\nforge> ").strip()
            except EOFError:
                print()
                break
            if not prompt:
                continue
            if prompt in {"/quit", "/exit"}:
                break
            if prompt == "/help":
                print("/models — list models\n/model NAME — switch model and reset context\n/reset — clear conversation\n/exit — quit")
            elif prompt == "/models":
                print("\n".join(provider.list_models()))
            elif prompt.startswith("/model "):
                model = prompt[7:].strip()
                if not model:
                    print("Usage: /model NAME")
                    continue
                agent = Agent(provider, model, executor, max_steps=args.max_steps, on_event=event)
                print(f"Model: {model}; conversation reset")
            elif prompt == "/reset":
                agent = Agent(provider, model, executor, max_steps=args.max_steps, on_event=event)
                print("Conversation reset")
            elif prompt.startswith("/"):
                print("Unknown command. Use /help.")
            else:
                try:
                    print(agent.run(prompt))
                except (AgentError, ProviderError) as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    agent = Agent(provider, model, executor, max_steps=args.max_steps, on_event=event)
                    print("Conversation reset after failure.", file=sys.stderr)
        return 0
    except (OSError, ValueError, AgentError, ProviderError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
