"""Workspace tools. File containment is enforced; approved shell commands are unsandboxed."""
from __future__ import annotations

import json
import difflib
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
from typing import Callable

MAX_FILE_BYTES = 256_000
MAX_OUTPUT_BYTES = 32_000
MAX_ENTRIES = 1000
COMMAND_TIMEOUT = 30
_IGNORED = {'.git', '.aws', '.ssh', '.codex', '.agents', '.venv', 'venv',
            'node_modules', '__pycache__', '.pytest_cache'}


def _private(part: str) -> bool:
    return (part in _IGNORED or part == '.env' or part.startswith('.env.')
            or part.endswith(('.pem', '.key')))


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': required, 'additionalProperties': False}}}


_STRING = {'type': 'string'}


class ToolExecutor:
    """Execute bounded file tools with explicit approval for every mutation."""

    schemas = [
        _schema('read_file', 'Read a UTF-8 workspace file (up to 256 KB).', {'path': _STRING}, ['path']),
        _schema('list_files', 'List workspace files recursively, excluding secrets and dependencies.', {'path': _STRING}, []),
        _schema('search_files', 'Find literal text in UTF-8 files; returns matching lines.', {'pattern': _STRING, 'path': _STRING}, ['pattern']),
        _schema('write_file', 'Create or overwrite a UTF-8 file after user approval.', {'path': _STRING, 'content': _STRING}, ['path', 'content']),
        _schema('replace_text', 'Replace one unique literal occurrence after approval.', {'path': _STRING, 'old': _STRING, 'new': _STRING}, ['path', 'old', 'new']),
        _schema('run_command', 'Run an UNSANDBOXED shell command when enabled and approved. Commands can access files outside the workspace.', {'command': _STRING}, ['command']),
    ]

    def __init__(self, root: Path, approve: Callable[[str], bool], allow_shell: bool = False):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('Workspace root must be a directory')
        self.approve = approve
        self.allow_shell = allow_shell

    def _path(self, path: str) -> Path:
        if not isinstance(path, str):
            raise ValueError('path must be a string')
        candidate = self.root / path
        if any(_private(part) for part in Path(path).parts):
            raise ValueError('Sensitive or excluded path is not accessible')
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError:
            raise ValueError('Path escapes the workspace') from None
        if any(_private(part) for part in relative.parts):
            raise ValueError('Sensitive or excluded path is not accessible')
        return resolved

    def _read(self, path: Path) -> str:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('Only regular files are supported')
        with path.open('rb') as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('File exceeds 256 KB limit')
        if b'\0' in data:
            raise ValueError('Binary file is not supported')
        return data.decode('utf-8')

    def _files(self, base: Path):
        if base.is_file():
            yield base
            return
        if not base.is_dir():
            raise ValueError('Directory does not exist')
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not _private(d) and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                file = Path(directory) / name
                if not _private(name) and not file.is_symlink():
                    yield file

    def _approved(self, message: str):
        if not self.approve(message):
            raise PermissionError('User denied approval')

    def execute(self, name: str, arguments: dict) -> str:
        try:
            if not isinstance(arguments, dict):
                raise ValueError('Tool arguments must be an object')
            schema = next((s['function']['parameters'] for s in self.schemas if s['function']['name'] == name), None)
            if schema is None:
                raise ValueError(f'Unknown tool: {name}')
            if set(arguments) - set(schema['properties']):
                raise ValueError('Unexpected tool argument')
            if any(key not in arguments for key in schema['required']):
                raise ValueError('Missing required tool argument')
            if any(not isinstance(value, str) for value in arguments.values()):
                raise ValueError('Tool argument values must be strings')
            result = getattr(self, '_' + name)(**arguments)
            return json.dumps({'ok': True, **result}, ensure_ascii=False)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            return json.dumps({'ok': False, 'error': str(exc)})

    def _read_file(self, path: str) -> dict:
        return {'content': self._read(self._path(path))}

    def _list_files(self, path: str = '.') -> dict:
        files = []
        for file in self._files(self._path(path)):
            if len(files) >= MAX_ENTRIES:
                return {'files': files, 'truncated': True}
            files.append(str(file.relative_to(self.root)))
        return {'files': files, 'truncated': False}

    def _search_files(self, pattern: str, path: str = '.') -> dict:
        if not pattern:
            raise ValueError('Search pattern must not be empty')
        matches = []
        size = 0
        for index, file in enumerate(self._files(self._path(path))):
            if index >= MAX_ENTRIES:
                return {'matches': matches, 'truncated': True}
            try:
                content = self._read(file)
            except (OSError, ValueError, UnicodeError):
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                if pattern in line:
                    match = {'path': str(file.relative_to(self.root)), 'line': line_number, 'text': line[:2000]}
                    size += len(json.dumps(match).encode())
                    if size > MAX_OUTPUT_BYTES or len(matches) >= MAX_ENTRIES:
                        return {'matches': matches, 'truncated': True}
                    matches.append(match)
        return {'matches': matches, 'truncated': False}

    def _write_file(self, path: str, content: str) -> dict:
        target = self._path(path)
        if len(content.encode()) > MAX_FILE_BYTES:
            raise ValueError('Content exceeds 256 KB limit')
        previous = self._read(target) if target.exists() else ''
        relative = str(target.relative_to(self.root))
        # Keep the entire proposal reviewable; never approve an omitted diff.
        preview = ''.join(difflib.unified_diff(
            previous.splitlines(keepends=True), content.splitlines(keepends=True),
            fromfile=relative if target.exists() else '/dev/null', tofile=relative))
        if len(preview) > 16_000:
            raise ValueError('Change preview exceeds 16000 characters; use smaller replacements')
        self._approved(f'Write {relative} ({len(content.encode())} bytes)?\n{preview}')
        target = self._path(path)
        if target.exists() and self._read(target) != previous:
            raise ValueError('File changed during approval; retry to review the new diff')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return {'path': str(target.relative_to(self.root)), 'bytes': len(content.encode())}

    def _replace_text(self, path: str, old: str, new: str) -> dict:
        if not old:
            raise ValueError('old must not be empty')
        content = self._read(self._path(path))
        if content.count(old) != 1:
            raise ValueError('old must occur exactly once in the file')
        return self._write_file(path, content.replace(old, new, 1))

    def _run_command(self, command: str) -> dict:
        if not self.allow_shell:
            raise PermissionError('Shell execution is disabled; enable it explicitly')
        if not command.strip():
            raise ValueError('Command must not be empty')
        self._approved(f'Run UNSANDBOXED shell command (can access outside workspace):\n{command}')
        process = subprocess.Popen(command, shell=True, cwd=self.root, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        output = bytearray()
        timed_out = truncated = False
        deadline = time.monotonic() + COMMAND_TIMEOUT
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(remaining, .1)):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        available = MAX_OUTPUT_BYTES - len(output)
                        output.extend(chunk[:available])
                        if len(chunk) > available:
                            truncated = True
                            break
                    if truncated:
                        break
                if not truncated and not timed_out:
                    try:
                        process.wait(timeout=max(.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out = True
        finally:
            if process.poll() is None or truncated or timed_out:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
        return {'output': output.decode('utf-8', errors='replace'), 'exit_code': process.returncode,
                'timed_out': timed_out, 'truncated': truncated}
