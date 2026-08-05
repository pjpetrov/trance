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
import re
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

#: Default allowlist. An agent may narrow or extend it via `role.commands`.
#: A parsed token that only means something to a shell. Without a shell these
#: are passed through as literal arguments instead of being interpreted.
_SHELL_TOKEN = re.compile(r"^(\|\|?|&&?|;|<|\d?>>?.*|.*[`]|.*\$\(.*)$")

#: VAR=value prefixes are not the program being run.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: Operators that start a new command; a redirect target is not a program.
_STARTS_COMMAND = {"|", "||", "&&", ";", "&", "\n"}
_REDIRECTS = {">", ">>", "<", "<<", "2>", "2>>"}


def programs_in(command: str) -> list[str]:
    """Every program a shell string would actually invoke.

    `pytest -q | head -5 && echo done` runs three programs; checking only the
    first word would let anything through after a pipe. Tokenised with shlex in
    punctuation mode so operators are found without splitting inside quotes —
    the `;` in `python3 -c 'import sys; sys.exit(1)'` is not an operator.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return ["<unparsable>"]

    found: list[str] = []
    expect_program = True
    skip_next = False
    for token in tokens:
        if skip_next:                       # the target of a redirect
            skip_next = False
            continue
        if token in _REDIRECTS:
            skip_next = True
            continue
        if token in _STARTS_COMMAND:
            expect_program = True
            continue
        if not expect_program or _ENV_ASSIGN.match(token):
            continue
        found.append(token)
        expect_program = False
    return found


ALLOWED_COMMANDS = {
    # test + build runners
    "pytest", "python", "python3", "pip", "npm", "npx", "node", "yarn", "pnpm",
    "tsc", "vitest", "jest", "eslint", "ruff", "mypy", "make", "go", "cargo",
    # reading and navigating the project
    "ls", "cat", "head", "tail", "wc", "find", "grep", "rg", "diff", "pwd",
    "stat", "file", "tree", "sort", "uniq", "cut", "awk", "sed", "which", "env",
    # small edits to scaffolding
    "mkdir", "touch", "cp", "mv", "echo", "printf", "true", "false", "test",
    # version control, read-mostly
    "git",
}


@dataclass
class CommandPolicy:
    """Global command settings, editable in the UI and persisted."""

    allowed: list = field(default_factory=lambda: sorted(ALLOWED_COMMANDS))
    #: Whether agents may use pipes, redirects and `&&` by default.
    shell: bool = True

    def to_dict(self) -> dict:
        return {"allowed": sorted(self.allowed), "shell": self.shell}


_POLICY = CommandPolicy()


def command_policy() -> CommandPolicy:
    return _POLICY


def set_command_policy(policy: CommandPolicy) -> CommandPolicy:
    """Install the global policy (the server does this at startup)."""
    global _POLICY
    _POLICY = policy
    return _POLICY


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

    @property
    def allowed_commands(self) -> set[str]:
        """This agent's allowlist — its own if set, otherwise the global one."""
        return set(getattr(self.role, "commands", None) or command_policy().allowed)

    @property
    def shell_enabled(self) -> bool:
        """Whether this agent may use pipes, redirects and `&&`."""
        role_setting = getattr(self.role, "shell", None)
        return command_policy().shell if role_setting is None else bool(role_setting)

    @property
    def command_cwd(self) -> Path:
        """Where commands run. A role may pin itself to a subdirectory."""
        sub = (getattr(self.role, "workdir", "") or "").strip()
        if not sub:
            return self.project
        target = self._resolve(sub)
        return target if target is not None and target.is_dir() else self.project

    # ------------------------------------------------------------- schema

    def specs(self) -> list[dict]:
        out: list[dict] = []
        if "files" in self.role.toolsets:
            out += [
                _fn("read_file", "Read a file from the project.",
                    {"path": {"type": "string", "description": "Path relative to the project root."}}, ["path"]),
                _fn("write_file",
                    "Create or overwrite a file with complete contents. Parent directories "
                    "are created automatically — never call mkdir first.",
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
                f"Run a command in {self.role.workdir or 'the project root'}. "
                + (f"Pipes, redirects and && are supported. " if self.shell_enabled
                   else "One plain command per call — no pipes or redirects. ")
                + f"Allowed programs: {', '.join(sorted(self.allowed_commands))}. "
                f"You do not need mkdir before write_file — it creates parent directories.",
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
            problem = self._argument_problem(name, arguments)
            if problem:
                return ToolOutcome(problem, ok=False)
            try:
                return handlers[name](**arguments)
            except TypeError as exc:
                return ToolOutcome(
                    f"{name} could not be called with those arguments ({exc}). "
                    f"{self._usage(name)}", ok=False)
        if self.graph is not None:
            result = self.graph.call(name, arguments)
            return ToolOutcome(result.text, ok=result.hit)
        return ToolOutcome(f"No such tool: {name}", ok=False)

    def _schema(self, name: str) -> dict:
        for spec in self.specs():
            if spec["function"]["name"] == name:
                return spec["function"]["parameters"]
        return {}

    def _usage(self, name: str) -> str:
        schema = self._schema(name)
        props = schema.get("properties", {})
        required = schema.get("required", [])
        args = ", ".join(
            f'"{k}": <{v.get("type", "value")}>' + ("" if k in required else "  (optional)")
            for k, v in props.items()
        )
        return f"Expected arguments: {{{args}}}."

    def _argument_problem(self, name: str, arguments: dict) -> str | None:
        """A readable complaint about arguments, before Python raises TypeError.

        The raw TypeError ("missing 2 required positional arguments") leaks the
        Python signature and reads as an internal fault rather than something
        the agent can fix.
        """
        schema = self._schema(name)
        if not schema:
            return None
        props, required = schema.get("properties", {}), schema.get("required", [])
        missing = [k for k in required if k not in arguments]
        unknown = [k for k in arguments if k not in props]
        if not missing and not unknown:
            return None

        parts = []
        if missing:
            parts.append(f"missing required argument(s): {', '.join(missing)}")
        if unknown:
            parts.append(f"unexpected argument(s): {', '.join(unknown)}")
        got = ", ".join(sorted(arguments)) or "none"
        return (f"{name} was called with {got}, but {' and '.join(parts)}. "
                f"Nothing was executed. {self._usage(name)}")

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

    def _run_via_shell(self, command: str) -> ToolOutcome:
        """Run through a shell, but still check every program it would invoke."""
        programs = programs_in(command)
        missing = _shell_missing(programs, self.allowed_commands)
        if missing:
            return ToolOutcome(
                f"Refused: {', '.join(repr(m) for m in missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not in this agent's allowlist. "
                f"Nothing was executed. Allowed: {', '.join(sorted(self.allowed_commands))}.",
                ok=False,
            )
        try:
            proc = subprocess.run(
                ["bash", "-c", command], cwd=self.command_cwd, capture_output=True,
                text=True, timeout=COMMAND_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return ToolOutcome(f"Timed out after {COMMAND_TIMEOUT_S}s: {command}", ok=False)
        except FileNotFoundError:
            return ToolOutcome("bash is not available on this machine.", ok=False)

        output = (proc.stdout + proc.stderr).strip() or "(no output)"
        if len(output) > MAX_COMMAND_OUTPUT:
            half = MAX_COMMAND_OUTPUT // 2
            output = output[:half] + "\n… (trimmed) …\n" + output[-half:]
        return ToolOutcome(
            f"$ {command}\nexit={proc.returncode}\n{output}",
            ok=proc.returncode == 0,
            detail={"kind": "command", "command": command,
                    "exit_code": proc.returncode, "output": output, "shell": True},
        )

    # ---------------------------------------------------------- commands

    def run_command(self, command: str) -> ToolOutcome:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return ToolOutcome(f"Could not parse command: {exc}", ok=False)
        if not parts:
            return ToolOutcome("Empty command.", ok=False)

        if self.shell_enabled:
            return self._run_via_shell(command)

        # Checked on parsed tokens, not the raw string: a `;` inside a quoted
        # argument (python3 -c 'import sys; sys.exit(1)') is not an operator.
        shell_bits = [p for p in parts if _SHELL_TOKEN.match(p)]
        if shell_bits:
            return ToolOutcome(
                f"Refused: commands run directly, not through a shell, so "
                f"{', '.join(sorted(set(shell_bits)))} would be passed as a literal "
                f"argument rather than interpreted. Nothing was executed.\n\n"
                f"Run one plain command per call. To check whether a path exists use "
                f"list_files or read_file rather than `ls … || echo …` — a non-zero "
                f"exit code already tells you it is missing.",
                ok=False,
            )
        if parts[0] not in self.allowed_commands:
            return ToolOutcome(
                f"Refused: {parts[0]!r} is not an allowed program for this agent. "
                f"Allowed: {', '.join(sorted(self.allowed_commands))}. "
                f"(write_file creates parent directories on its own, so you never "
                f"need mkdir to write into a new folder.)",
                ok=False,
            )
        try:
            proc = subprocess.run(
                parts, cwd=self.command_cwd, capture_output=True, text=True,
                timeout=COMMAND_TIMEOUT_S
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


def _shell_missing(programs: list[str], allowed: set[str]) -> list[str]:
    return sorted({p for p in programs if p not in allowed})


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
        lines.append(
            "write_file creates any missing parent directories, so you never need to "
            "create a folder before writing into it."
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
        allowed = sorted(getattr(role, "commands", None) or command_policy().allowed)
        where = f"in {role.workdir}" if getattr(role, "workdir", "") else "in the project root"
        role_shell = getattr(role, "shell", None)
        shell_on = command_policy().shell if role_shell is None else bool(role_shell)
        lines.append(
            f"You may run commands {where}, limited to these programs: " + ", ".join(allowed)
            + (". Pipes, redirects and && work, and every program in the line is checked "
               "against that list." if shell_on
               else ". One plain command per call — pipes and redirects are not available.")
            + f" Commands time out after {COMMAND_TIMEOUT_S}s."
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
