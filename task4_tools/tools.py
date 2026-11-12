"""Task 4 - validated, contained tool layer for the debugging agent.

Implements ToolRegistry(ToolRegistryBase) from tool_contract.py. See that file's docstring
for the rules the tests check. Design summary (for the video / README):

- Every tool has a strict JSON schema (additionalProperties: false) validated with `jsonschema`
  before any logic runs. Invalid JSON, unknown tool names, wrong/missing/extra arguments all turn
  into ToolResult(ok=False, error=...) - execute() never raises.
- Containment ("least privilege"):
    * every path argument is resolved with Path.resolve() (which follows symlinks and collapses
      "..") and then checked to still be inside the resolved workspace root. A string prefix check
      is not enough (e.g. "/workspace-evil" has the prefix "/workspace" but is not inside it, and a
      symlink can point outside even though the *name* of the link is inside) - we only trust the
      final, resolved path.
    * absolute paths are rejected outright, and any raw path/glob/pattern string containing ".."
      is rejected before we even try to resolve it.
    * edit_file additionally requires the resolved path's first path component (relative to the
      workspace) to be "src", and the file must already exist - tests, data, config and ISSUE.md
      can never be touched by this tool, no matter what path games are played.
    * run_tests/run_cli never use shell=True, build argv lists themselves (the model only supplies
      a `target` string or a list of plain string arguments - no argument is ever concatenated into
      a shell command line), run with a timeout, and get a minimal subprocess environment that does
      not inherit UPB_AI_GATEWAY_KEY or anything else from our own process environment.
    * every tool's output is capped at MAX_OUTPUT_CHARS; if content had to be cut, `truncated=True`
      is set so the model (and the grader) can tell.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import jsonschema

from tool_contract import (
    DEFAULT_TIMEOUT_S,
    MAX_LIST_ENTRIES,
    MAX_OUTPUT_CHARS,
    MAX_READ_LINES,
    MAX_SEARCH_RESULTS,
    ToolRegistryBase,
    ToolResult,
)

# -------------------------------------------------------------------- schemas

SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the workspace (relative paths), optionally filtered by a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list, relative to the workspace root (default: '.')."},
                    "pattern": {"type": "string", "description": "Optional glob pattern, e.g. '*.py' or 'src/**/*.py'."},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                f"Read a text file from the workspace, with line numbers. "
                f"Returns at most {MAX_READ_LINES} lines; use start_line/end_line to page through a longer file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, relative to the workspace root."},
                    "start_line": {"type": "integer", "minimum": 1, "description": "First line to return (1-based, inclusive)."},
                    "end_line": {"type": "integer", "minimum": 1, "description": "Last line to return (1-based, inclusive)."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search the workspace for lines matching a regular expression. Returns 'path:line: text' matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "A Python regular expression."},
                    "glob": {"type": "string", "description": "Optional glob to restrict which files are searched, e.g. '**/*.py'."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS, "description": "Upper bound on returned matches."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the pytest test suite (or one target test file/test) and return a structured pass/fail summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Optional: 'tests/test_x.py' or 'tests/test_x.py::test_name'. Default: the whole suite."},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace one exact occurrence of old_str with new_str in an existing file below src/. "
                "old_str must match exactly once in the file. Returns a unified diff of the change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path below src/, relative to the workspace root."},
                    "old_str": {"type": "string", "description": "Exact text to replace; must occur exactly once."},
                    "new_str": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_cli",
            "description": "Run 'python -m tinyledger <args>' in the workspace and return exit code, stdout and stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "Plain CLI arguments, e.g. [\"report\", \"data/sample.csv\", \"--month\", \"2026-03\"].",
                    },
                },
                "required": ["args"],
                "additionalProperties": False,
            },
        },
    },
]

_SCHEMA_BY_NAME = {s["function"]["name"]: s["function"]["parameters"] for s in SCHEMAS}


class PathError(ValueError):
    """Raised internally for any path/containment problem; always caught and turned into a ToolResult."""


class ToolRegistry(ToolRegistryBase):
    def __init__(self, workspace: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        super().__init__(workspace, timeout_s)
        self._root = self.workspace.resolve()

    # ------------------------------------------------------------- interface

    def schemas(self) -> list[dict]:
        return [dict(s) for s in SCHEMAS]

    def execute(self, name: str, arguments: str) -> ToolResult:
        try:
            return self._execute(name, arguments)
        except Exception as exc:  # belt and braces: execute() must never raise
            return ToolResult(ok=False, error=f"internal error: {exc}")

    # ------------------------------------------------------------ dispatch

    def _execute(self, name: str, arguments: str) -> ToolResult:
        if name not in _SCHEMA_BY_NAME:
            return ToolResult(ok=False, error=f"unknown tool: {name}")

        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return ToolResult(ok=False, error=f"invalid JSON arguments: {exc}")
        if not isinstance(args, dict):
            return ToolResult(ok=False, error="arguments must be a JSON object")

        try:
            jsonschema.validate(args, _SCHEMA_BY_NAME[name])
        except jsonschema.ValidationError as exc:
            return ToolResult(ok=False, error=f"invalid arguments for {name}: {exc.message}")

        try:
            if name == "list_files":
                return self._list_files(args.get("path", "."), args.get("pattern"))
            if name == "read_file":
                return self._read_file(args["path"], args.get("start_line"), args.get("end_line"))
            if name == "search_code":
                return self._search_code(args["pattern"], args.get("glob"), args.get("max_results"))
            if name == "run_tests":
                return self._run_tests(args.get("target"))
            if name == "edit_file":
                return self._edit_file(args["path"], args["old_str"], args["new_str"])
            if name == "run_cli":
                return self._run_cli(args["args"])
        except PathError as exc:
            return ToolResult(ok=False, error=str(exc))
        return ToolResult(ok=False, error=f"unknown tool: {name}")

    # --------------------------------------------------------------- paths

    def _resolve(self, raw: str, *, must_exist: bool = False, must_be_file: bool = False) -> Path:
        """Resolve a workspace-relative path and verify containment. Raises PathError on any violation."""
        if raw is None:
            raise PathError("path is required")
        text = str(raw)
        if ".." in Path(text).parts:
            raise PathError(f"path escapes the workspace: {raw!r}")
        if Path(text).is_absolute() or (len(text) >= 2 and text[1] == ":"):  # posix absolute or a drive letter
            raise PathError(f"absolute paths are not allowed: {raw!r}")
        candidate = (self._root / text).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise PathError(f"path escapes the workspace: {raw!r}")
        if must_exist and not candidate.exists():
            raise PathError(f"no such file: {raw!r}")
        if must_be_file and not candidate.is_file():
            raise PathError(f"not a file: {raw!r}")
        return candidate

    def _check_glob(self, pattern: str | None) -> None:
        if pattern and (".." in pattern or pattern.startswith("/")):
            raise PathError(f"pattern escapes the workspace: {pattern!r}")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()

    # --------------------------------------------------------------- tools

    def _list_files(self, path: str, pattern: str | None) -> ToolResult:
        self._check_glob(pattern)
        base = self._resolve(path, must_exist=True)
        if not base.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {path!r}")

        entries: list[str] = []
        for p in sorted(base.rglob("*")):
            if ".git" in p.relative_to(self._root).parts:
                continue
            if not p.is_file():
                continue
            rel = self._relative(p)
            if pattern and not p.match(pattern) and not Path(rel).match(pattern):
                continue
            entries.append(rel)

        truncated = len(entries) > MAX_LIST_ENTRIES
        return ToolResult(ok=True, output=entries[:MAX_LIST_ENTRIES], truncated=truncated)

    def _read_file(self, path: str, start_line: int | None, end_line: int | None) -> ToolResult:
        target = self._resolve(path, must_exist=True, must_be_file=True)
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult(ok=False, error=f"could not read {path!r}: {exc}")

        n = len(lines)
        start = max(1, start_line or 1)
        end = min(n, end_line or n)
        if start > n:
            return ToolResult(ok=True, output=f"(file has only {n} lines)", truncated=False)

        truncated = (end - start + 1) > MAX_READ_LINES
        end = min(end, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i:>5}: {lines[i - 1]}" for i in range(start, end + 1))
        body = self._cap(body)
        return ToolResult(ok=True, output=body, truncated=truncated or len(body) >= MAX_OUTPUT_CHARS)

    def _search_code(self, pattern: str, glob: str | None, max_results: int | None) -> ToolResult:
        self._check_glob(glob)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult(ok=False, error=f"invalid regular expression: {exc}")

        limit = min(max_results or MAX_SEARCH_RESULTS, MAX_SEARCH_RESULTS)
        matches: list[str] = []
        truncated = False
        candidates = self._root.glob(glob) if glob else self._root.rglob("*")
        for p in sorted(candidates):
            if not p.is_file() or ".git" in p.relative_to(self._root).parts:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = self._relative(p)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append(f"{rel}:{lineno}: {line.strip()[:300]}")
            if truncated:
                break

        output = "\n".join(matches) if matches else "(no matches)"
        return ToolResult(ok=True, output=self._cap(output), truncated=truncated)

    def _run_tests(self, target: str | None) -> ToolResult:
        if target:
            file_part = target.split("::", 1)[0]
            self._resolve(file_part, must_exist=True)

        with tempfile.TemporaryDirectory() as tmp:
            junit_path = Path(tmp) / "report.xml"
            cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit_path}"]
            if target:
                cmd.append(target)
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=self._root,
                    env=self._subprocess_env(),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(ok=False, error=f"run_tests timed out after {self.timeout_s}s")

            if not junit_path.exists():
                tail = self._cap((proc.stdout or "") + (proc.stderr or ""))
                return ToolResult(ok=False, error=f"pytest did not produce a report (exit {proc.returncode}): {tail}")
            summary = self._parse_junit(junit_path)

        return ToolResult(ok=True, output=summary)

    def _edit_file(self, path: str, old_str: str, new_str: str) -> ToolResult:
        target = self._resolve(path, must_exist=True, must_be_file=True)
        rel_parts = target.relative_to(self._root).parts
        if not rel_parts or rel_parts[0] != "src":
            return ToolResult(ok=False, error="edit_file may only change existing files below src/")

        content = target.read_text(encoding="utf-8")
        count = content.count(old_str)
        if count != 1:
            return ToolResult(ok=False, error=f"old_str must match exactly once, found {count} occurrence(s)")

        new_content = content.replace(old_str, new_str, 1)
        target.write_text(new_content, encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        return ToolResult(ok=True, output=self._cap(diff))

    def _run_cli(self, args: list[Any]) -> ToolResult:
        if not all(isinstance(a, str) for a in args):
            return ToolResult(ok=False, error="all run_cli args must be strings")
        for arg in args:
            if ".." in arg or arg.startswith("/"):
                raise PathError(f"argument looks like a path escaping the workspace: {arg!r}")

        cmd = [sys.executable, "-m", "tinyledger", *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=self._root,
                env=self._subprocess_env(),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"run_cli timed out after {self.timeout_s}s")

        output = {
            "exit_code": proc.returncode,
            "stdout": self._cap(proc.stdout),
            "stderr": self._cap(proc.stderr),
        }
        return ToolResult(ok=True, output=output, truncated=len(proc.stdout) + len(proc.stderr) > MAX_OUTPUT_CHARS)

    # -------------------------------------------------------------- helpers

    def _subprocess_env(self) -> dict[str, str]:
        """Minimal environment: no gateway key, no secrets, just enough to run Python."""
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": "src",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def _cap(self, text: str) -> str:
        return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS]

    @staticmethod
    def _parse_junit(xml_path: Path) -> dict:
        root = ET.parse(xml_path).getroot()
        suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
        passed = failed = skipped = 0
        failing_tests: list[dict] = []
        for suite in suites:
            for case in suite.findall("testcase"):
                classname = case.get("classname", "")
                name = case.get("name", "")
                full_name = f"{classname}::{name}" if classname else name
                failure = case.find("failure")
                error = case.find("error")
                skip = case.find("skipped")
                if failure is not None or error is not None:
                    failed += 1
                    node = failure if failure is not None else error
                    message = (node.get("message") or "").strip()
                    traceback_text = (node.text or "").strip()
                    failing_tests.append(
                        {"name": full_name, "message": message[:500], "traceback": traceback_text[:1500]}
                    )
                elif skip is not None:
                    skipped += 1
                else:
                    passed += 1
        total = passed + failed + skipped
        return {"total": total, "passed": passed, "failed": failed, "skipped": skipped, "failing_tests": failing_tests}
