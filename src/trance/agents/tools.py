"""Tools the working agents get: files, commands, and the code graph.

Two safety properties matter here, because these agents write to disk and run
processes:

1. Every path is resolved and confined to the project directory. A path that
   escapes it is refused, not clamped.
2. Writes are additionally checked against the *role's remit*. A backend agent
   editing `frontend/` is refused and the refusal is reported — that is the
   mechanism behind "the orchestrator notices an agent overtaking another's
   duties".
3. Commands come from an allowlist of test/build runners. This is not a sandbox;
   it is a guard rail against an agent improvising `rm -rf`.
"""

from __future__ import annotations

import difflib
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..model import estimate_tokens
from .roles import AgentRole

#: Deliberately small. A single 100KB read is ~25k tokens and will blow a 64k
#: context window on its own — the runner trims, but not overflowing in the
#: first place is cheaper and keeps the model's attention on relevant code.
MAX_READ_BYTES = 24_000
MAX_COMMAND_OUTPUT = 6_000
MAX_LISTED_FILES = 300
COMMAND_TIMEOUT_S = 180

#: First token of a command must be one of these.
ALLOWED_COMMANDS = {
    "pytest", "python", "python3", "npm", "npx", "node", "yarn", "pnpm",
    "tsc", "vitest", "jest", "ruff", "mypy", "ls", "cat", "make",
}


#: A diff longer than this is truncated for display. It is never sent to the
#: model, so this only bounds what the UI has to render.
MAX_DIFF_LINES = 400


@dataclass
class ToolOutcome:
    text: str
    ok: bool = True
    files_written: list[str] = field(default_factory=list)
    #: Set when a write was refused because it fell outside the role's remit.
    remit_violation: str | None = None
    #: Structured detail for the UI only — diffs, exit codes, command output.
    #: Deliberately NOT part of `text`: the model does not need to re-read a
    #: diff of what it just wrote, and it would cost context to show it.
    detail: dict = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


class AgentTools:
    def __init__(self, project: Path, role: AgentRole, graph_tools=None):
        self.project = Path(project).resolve()
        self.role = role
        self.graph = graph_tools

    # ------------------------------------------------------------- schema

    def specs(self) -> list[dict]:
        out: list[dict] = []
        if "files" in self.role.toolsets:
            out += [
                _fn("read_file", "Read a file from the project.",
                    {"path": {"type": "string", "description": "Path relative to the project root."}}, ["path"]),
                _fn("write_file", "Create or overwrite a file with complete contents.",
                    {"path": {"type": "string"}, "content": {"type": "string",
                     "description": "The ENTIRE file contents. Not a diff, not a fragment."}},
                    ["path", "content"]),
                _fn("list_files", "List project files, optionally under a subdirectory.",
                    {"subdir": {"type": "string", "description": "Optional subdirectory."}}, []),
            ]
        if "inspect" in self.role.toolsets:
            out += [
                _fn("check_file",
                    "Check whether a file exists and whether it has content. Returns its size "
                    "and line count. Does NOT return the file's contents.",
                    {"path": {"type": "string", "description": "Path relative to the project root."}},
                    ["path"]),
                _fn("check_files",
                    "Check several files at once. Prefer this over repeated check_file calls.",
                    {"paths": {"type": "array", "items": {"type": "string"},
                               "description": "Paths relative to the project root."}},
                    ["paths"]),
                _fn("list_files", "List project files, optionally under a subdirectory.",
                    {"subdir": {"type": "string"}}, []),
            ]
        if "commands" in self.role.toolsets:
            out.append(_fn(
                "run_command",
                f"Run a command in the project root. Allowed programs: {', '.join(sorted(ALLOWED_COMMANDS))}.",
                {"command": {"type": "string", "description": "e.g. 'pytest -q'"}}, ["command"]))
        if "graph" in self.role.toolsets and self.graph is not None:
            out += self.graph_specs()
        return out

    def graph_specs(self) -> list[dict]:
        from ..worker.tools import specs as graph_specs

        return graph_specs()

    # -------------------------------------------------------------- call

    def call(self, name: str, arguments: dict) -> ToolOutcome:
        handlers = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "list_files": self.list_files,
            "run_command": self.run_command,
            "check_file": self.check_file,
            "check_files": self.check_files,
        }
        # A tool the role was not granted must be refused even if the model
        # invents the name — specs() omitting it is not enough on its own.
        granted = {spec["function"]["name"] for spec in self.specs()}
        if name in handlers and name not in granted:
            return ToolOutcome(
                f"Refused: you do not have the {name!r} tool. "
                f"Available: {', '.join(sorted(granted))}.", ok=False)
        if name in handlers:
            try:
                return handlers[name](**arguments)
            except TypeError as exc:
                return ToolOutcome(f"Bad arguments for {name}: {exc}", ok=False)
        if self.graph is not None:
            result = self.graph.call(name, arguments)
            return ToolOutcome(result.text, ok=result.hit)
        return ToolOutcome(f"No such tool: {name}", ok=False)

    # ------------------------------------------------------------- files

    def _resolve(self, path: str) -> Path | None:
        candidate = (self.project / path).resolve()
        if candidate == self.project or self.project in candidate.parents:
            return candidate
        return None  # escaped the project root

    def read_file(self, path: str) -> ToolOutcome:  # noqa: D401
        target = self._resolve(path)
        if target is None:
            return ToolOutcome(f"Refused: {path!r} is outside the project directory.", ok=False)
        if not target.is_file():
            return ToolOutcome(f"{path} does not exist. Use list_files to see what does.", ok=False)
        raw = target.read_bytes()
        data = raw[:MAX_READ_BYTES].decode("utf8", errors="replace")
        if len(raw) > MAX_READ_BYTES:
            data += (f"\n… truncated at {MAX_READ_BYTES} bytes of {len(raw)}. "
                     "Use the graph tools to fetch a specific symbol instead of the whole file.")
        return ToolOutcome(f"# {path}\n{data}",
                           detail={"kind": "read", "path": path, "bytes": len(raw)})

    def write_file(self, path: str, content: str) -> ToolOutcome:
        target = self._resolve(path)
        if target is None:
            return ToolOutcome(f"Refused: {path!r} is outside the project directory.", ok=False)
        rel = target.relative_to(self.project).as_posix()
        if not self.role.may_write(rel):
            message = (
                f"Refused: {rel} is outside your remit ({', '.join(self.role.paths) or 'none'}). "
                f"You are the {self.role.title}. Report that this file needs changing and which "
                f"role should own it — do not work around this."
            )
            return ToolOutcome(message, ok=False, remit_violation=rel)

        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        previous = ""
        if existed:
            try:
                previous = target.read_text(encoding="utf8")
            except OSError:
                previous = ""
        target.write_text(content, encoding="utf8")

        diff_lines = list(difflib.unified_diff(
            previous.splitlines(), content.splitlines(),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=3,
        ))
        truncated = len(diff_lines) > MAX_DIFF_LINES
        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

        verb = "Updated" if existed else "Created"
        return ToolOutcome(
            f"{verb} {rel} ({len(content)} bytes).",
            files_written=[rel],
            detail={
                "kind": "write", "path": rel, "created": not existed,
                "bytes": len(content), "added": added, "removed": removed,
                "diff": "\n".join(diff_lines[:MAX_DIFF_LINES]),
                "truncated": truncated,
            },
        )

    def list_files(self, subdir: str = "") -> ToolOutcome:
        root = self._resolve(subdir) if subdir else self.project
        if root is None or not root.exists():
            return ToolOutcome(f"No such directory: {subdir!r}", ok=False)
        skip = {".git", "node_modules", "__pycache__", ".venv", ".trance", "dist", ".pytest_cache"}
        found = []
        for path in sorted(root.rglob("*")):
            if any(part in skip for part in path.parts):
                continue
            if path.is_file():
                found.append(path.relative_to(self.project).as_posix())
        if not found:
            return ToolOutcome("(project is empty)")
        listing = "\n".join(found[:MAX_LISTED_FILES])
        if len(found) > MAX_LISTED_FILES:
            listing += f"\n… and {len(found) - MAX_LISTED_FILES} more"
        return ToolOutcome(listing)

    # --------------------------------------------------------- inspect

    def check_file(self, path: str) -> ToolOutcome:
        """Existence and size only — never the contents."""
        stat = self._stat(path)
        return ToolOutcome(_render_stat(stat), ok=True,
                           detail={"kind": "check", "files": [stat]})

    def check_files(self, paths: list) -> ToolOutcome:
        if isinstance(paths, str):
            paths = [paths]
        stats = [self._stat(p) for p in list(paths)[:60]]
        return ToolOutcome("\n".join(_render_stat(s) for s in stats), ok=True,
                           detail={"kind": "check", "files": stats})

    def _stat(self, path: str) -> dict:
        target = self._resolve(path)
        if target is None:
            return {"path": path, "error": "outside the project directory"}
        if not target.exists():
            return {"path": path, "exists": False}
        if target.is_dir():
            return {"path": path, "exists": True, "is_dir": True}
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return {"path": path, "exists": True, "error": str(exc)}
        text = raw.decode("utf8", errors="replace")
        return {
            "path": path, "exists": True, "size_bytes": len(raw),
            "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "blank": not text.strip(),
        }

    # ---------------------------------------------------------- commands

    def run_command(self, command: str) -> ToolOutcome:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return ToolOutcome(f"Could not parse command: {exc}", ok=False)
        if not parts:
            return ToolOutcome("Empty command.", ok=False)
        if parts[0] not in ALLOWED_COMMANDS:
            return ToolOutcome(
                f"Refused: {parts[0]!r} is not an allowed program. "
                f"Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}.",
                ok=False,
            )
        try:
            proc = subprocess.run(
                parts, cwd=self.project, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return ToolOutcome(f"Timed out after {COMMAND_TIMEOUT_S}s: {command}", ok=False)
        except FileNotFoundError:
            return ToolOutcome(f"{parts[0]} is not installed on this machine.", ok=False)

        output = (proc.stdout + proc.stderr).strip() or "(no output)"
        if len(output) > MAX_COMMAND_OUTPUT:
            half = MAX_COMMAND_OUTPUT // 2
            output = output[:half] + "\n… (trimmed) …\n" + output[-half:]
        return ToolOutcome(
            f"$ {command}\nexit={proc.returncode}\n{output}",
            ok=proc.returncode == 0,
            detail={"kind": "command", "command": command,
                    "exit_code": proc.returncode, "output": output},
        )


def _render_stat(s: dict) -> str:
    if s.get("error"):
        return f"{s['path']}: ERROR — {s['error']}"
    if not s.get("exists"):
        return f"{s['path']}: MISSING"
    if s.get("is_dir"):
        return f"{s['path']}: directory"
    state = "EMPTY" if s["blank"] else "has content"
    return f"{s['path']}: exists, {s['size_bytes']} bytes, {s['lines']} lines — {state}"


def permissions_brief(role: AgentRole) -> str:
    """Render the agent's *enforced* permissions, for its own prompt.

    Generated from the same constants the tool layer enforces with — the remit
    globs, the toolset list, the command allowlist, the read cap. Hand-written
    prompt text drifts from the code and then the agent is told one thing while
    the tool boundary does another; deriving it means the description is wrong
    only if the enforcement is.
    """
    lines: list[str] = []

    if "files" in role.toolsets:
        if role.paths:
            lines.append("You may CREATE or MODIFY files matching:")
            lines += [f"  {p}" for p in role.paths]
            lines.append(
                "  Any write outside these globs is REFUSED by the system — the file is not "
                "written and you get an error back. You cannot work around it, and retrying "
                "will not help. Say which file needs changing and which role owns it."
            )
        else:
            lines.append("You may NOT write any file. Every write attempt is refused.")
        lines.append(
            f"You may READ any file in the project, including outside your write remit. "
            f"Reads are truncated at {MAX_READ_BYTES:,} bytes."
        )
    elif "inspect" not in role.toolsets:
        lines.append("You have NO file access: you cannot read or write files.")

    if "inspect" in role.toolsets:
        if "files" not in role.toolsets:
            lines.append("You cannot read file contents, create files, or modify files.")
        lines.append(
            "You may check whether files exist and whether they have content "
            "(check_file, check_files, list_files). You get the size and line count only — "
            "NOT the contents. You cannot read, write, or run anything."
        )

    if "graph" in role.toolsets:
        lines.append(
            "You may query the indexed code graph (get_definition, get_callers, get_callees, "
            "search_symbols) across the whole project."
        )

    if "commands" in role.toolsets:
        lines.append(
            "You may run commands in the project root, limited to: "
            + ", ".join(sorted(ALLOWED_COMMANDS))
            + f". Anything else is refused. Commands time out after {COMMAND_TIMEOUT_S}s."
        )
    else:
        lines.append("You may NOT run commands. You cannot execute tests, builds, or scripts.")

    lines.append(
        "Everything is confined to the project directory; a path that escapes it is refused."
    )
    return "\n".join(lines)


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }
