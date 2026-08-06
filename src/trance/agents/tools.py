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
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..model import estimate_tokens
from .memory import ProjectMemory
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

@dataclass
class RunningCommand:
    """A command in flight.

    The pgid is recorded separately because the process we hold may already
    have exited: `node server.js &` makes bash fork and return immediately,
    leaving node alive in the same group holding our output pipe. Cancelling
    has to target the group, not the process.
    """

    command_id: str
    proc: subprocess.Popen
    pgid: int
    command: str
    background: bool = False
    log_path: str = ""


_RUNNING: dict[str, RunningCommand] = {}
_RUNNING_LOCK = threading.Lock()


def running_commands() -> list[str]:
    with _RUNNING_LOCK:
        return list(_RUNNING)


def background_commands() -> list[dict]:
    with _RUNNING_LOCK:
        return [{"command_id": r.command_id, "command": r.command, "log": r.log_path}
                for r in _RUNNING.values() if r.background]


def kill_group(pgid: int) -> bool:
    """Kill a whole process group. Returns whether anything was signalled."""
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def cancel_command(command_id: str) -> bool:
    """Kill a running command and everything it spawned.

    Deliberately does not check `proc.poll()`: with `cmd &` the shell exits at
    once while the real process lives on in the same group, and that is exactly
    the case worth cancelling.
    """
    with _RUNNING_LOCK:
        entry = _RUNNING.get(command_id)
    if entry is None:
        return False
    killed = kill_group(entry.pgid)
    if not killed:
        try:
            entry.proc.kill()
            killed = True
        except ProcessLookupError:
            killed = False
    if entry.background:
        # Nothing is waiting on a background process, so it has to deregister
        # itself here — a foreground one is popped when communicate() returns.
        with _RUNNING_LOCK:
            _RUNNING.pop(command_id, None)
    return killed


def stop_background(_reason: str = "") -> list[str]:
    """Kill every background process. Called when a step ends.

    A server left running would hold its port against the next step, and
    nothing else ever reaps it.
    """
    with _RUNNING_LOCK:
        entries = [r for r in _RUNNING.values() if r.background]
    stopped = []
    for entry in entries:
        kill_group(entry.pgid)
        stopped.append(entry.command)
        with _RUNNING_LOCK:
            _RUNNING.pop(entry.command_id, None)
    return stopped


def command_policy() -> CommandPolicy:
    return _POLICY


def set_command_policy(policy: CommandPolicy) -> CommandPolicy:
    """Install the global policy (the server does this at startup)."""
    global _POLICY
    _POLICY = policy
    return _POLICY


#: A fence the model wrapped the whole file in, or a comment line that is just
#: the file's own name. Both are chat habits leaking into a tool argument, and
#: in a .js file `# server/app.js` is a syntax error rather than a blemish.
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*[ \t]*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)
_NAME_COMMENT = re.compile(
    r"^[ \t]*(?://|\#|/\*|<!--|--)[ \t]*(?P<name>[\w./\\-]+)[ \t]*(?:\*/|-->)?[ \t]*$")


def strip_wrappers(content: str, rel: str) -> tuple[str, list[str]]:
    """Remove a code fence, or a first line that is only this file's name.

    Deliberately narrow. A leading `#` comment is stripped only when the rest of
    the line is the path being written — never a shebang, never a real comment —
    because the cost of being wrong here is silently deleting someone's code.
    """
    notes: list[str] = []
    text = content or ""

    fenced = _FENCE.match(text)
    if fenced and not rel.endswith((".md", ".markdown")):
        text = fenced.group("body")
        notes.append("a ``` code fence wrapped around the whole file")

    lines = text.split("\n")
    if lines:
        match = _NAME_COMMENT.match(lines[0])
        candidates = {rel, rel.lstrip("./"), Path(rel).name}
        if match and match.group("name") in candidates:
            lines = lines[1:]
            if lines and not lines[0].strip():
                lines = lines[1:]
            text = "\n".join(lines)
            notes.append(f"a first line that was just the file name ({match.group(0).strip()})")
    return text, notes


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
    def __init__(self, project: Path, role: AgentRole, graph_tools=None, notify=None,
                 memory=None, approve=None, session_id: str = "", step_id: str = ""):
        self.project = Path(project).resolve()
        self.role = role
        self.graph = graph_tools
        #: The team's shared notebook. Every agent may write to it, including
        #: ones with no file access — it is how they talk to each other at all.
        self.memory = memory if memory is not None else ProjectMemory(self.project)
        #: Called before a refusal becomes final: ask(kind=, subject=, detail=)
        #: returns an ApprovalRequest. None means refuse without asking, which
        #: is what the CLI and the tests do.
        self._approve = approve
        self.session_id = session_id
        self.step_id = step_id
        #: Called with (event_type, payload) so a long command is visible while
        #: it runs instead of only when it finishes.
        self.notify = notify or (lambda kind, payload: None)

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
                _fn("append_file",
                    "Add to the END of a file, creating it if absent. Use this when a file is "
                    "too long to emit in one reply: write_file the first section, then "
                    "append_file each remaining section. Never use it to edit existing "
                    "lines — it only ever adds to the end.",
                    {"path": {"type": "string"}, "content": {"type": "string",
                     "description": "Text to add at the end, exactly as it should appear."}},
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
                "stop_command",
                "Stop a process you started with background=true.",
                {"command_id": {"type": "string"}}, ["command_id"]))
            out.append(_fn(
                "run_command",
                f"Run a command in {self.role.workdir or 'the project root'}. "
                + (f"Pipes, redirects and && are supported. " if self.shell_enabled
                   else "One plain command per call — no pipes or redirects. ")
                + f"Allowed programs: {', '.join(sorted(self.allowed_commands))}. "
                f"You do not need mkdir before write_file — it creates parent directories.",
                {"command": {"type": "string", "description": "e.g. 'pytest -q'"},
                 "background": {
                     "type": "boolean",
                     "description": ("Start it and return immediately. Use this for anything "
                                     "that does not exit on its own — a server, a watcher. "
                                     "Output goes to a log file you can read, and you stop it "
                                     "with stop_command. Without this the call blocks until "
                                     "the command exits or times out.")}},
                ["command"]))
        # Any agent that does work gets this: it is the only way one step's
        # decision reaches the next step's agent. An inspect-only agent does not
        # — it was created precisely so a verifier cannot write anything, and
        # shared memory is something the next agent has to act on.
        if {"files", "commands", "graph"} & set(self.role.toolsets):
            out.append(_fn(
                "remember",
                "Write one line to the project's shared memory, which every agent on this "
                "project sees from now on. Use it for a decision others must follow: a route "
                "and its payload, a port, a file layout, the command that runs the tests. "
                "Not for progress reports, and not for anything already obvious from the code.",
                {"note": {"type": "string",
                          "description": "One specific, self-contained sentence."}},
                ["note"]))
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
            "append_file": self.append_file,
            "list_files": self.list_files,
            "run_command": self.run_command,
            "stop_command": self.stop_command,
            "check_file": self.check_file,
            "check_files": self.check_files,
            "remember": self.remember,
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
            # A lookup that found nothing is not a refusal: the tool ran and
            # answered. Saying so lets the UI show a miss as a miss instead of
            # painting it red next to writes that were actually blocked.
            return ToolOutcome(
                result.text, ok=result.hit,
                detail={"kind": "graph", "tool": name, "hit": result.hit,
                        "query": next((str(v) for v in arguments.values()), "")})
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
        return self._put(path, content, append=False)

    def append_file(self, path: str, content: str) -> ToolOutcome:
        """Add to the end of a file, so a large one can be built up in pieces.

        A model cannot emit a file longer than the context it has left — every
        token it generates occupies the same window the prompt sits in. Without
        this, "write it in smaller pieces" is advice an agent cannot follow:
        write_file takes the *whole* file, so section two would have to repeat
        section one and hit the same ceiling.
        """
        return self._put(path, content, append=True)

    def _put(self, path: str, content: str, *, append: bool) -> ToolOutcome:
        target = self._resolve(path)
        if target is None:
            return ToolOutcome(f"Refused: {path!r} is outside the project directory.", ok=False)
        rel = target.relative_to(self.project).as_posix()
        if not self.role.may_write(rel) and not self._ask_user(
                "write", rel,
                {"remit": list(self.role.paths), "agent_title": self.role.title,
                 "append": append, "bytes": len(content or "")}):
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
        content, stripped = strip_wrappers(content, rel)
        final = previous + content if append else content
        target.write_text(final, encoding="utf8")

        diff_lines = list(difflib.unified_diff(
            previous.splitlines(), final.splitlines(),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=3,
        ))
        truncated = len(diff_lines) > MAX_DIFF_LINES
        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

        # Told, not silently fixed: an agent that never learns keeps doing it,
        # and the next thing it wraps might be something we do not detect.
        warning = (f"\n\nNote: I removed {' and '.join(stripped)} before writing. "
                   f"write_file takes the file's exact contents — no fences, no "
                   f"file-name header." if stripped else "")
        verb = "Appended to" if append else ("Updated" if existed else "Created")
        size = (f"{len(content)} bytes added, {len(final)} total" if append
                else f"{len(final)} bytes")
        return ToolOutcome(
            f"{verb} {rel} ({size}, {len(final.splitlines())} lines).{warning}",
            files_written=[rel],
            detail={
                "kind": "write", "path": rel, "created": not existed, "appended": append,
                "stripped": stripped,
                "bytes": len(final), "added": added, "removed": removed,
                "diff": "\n".join(diff_lines[:MAX_DIFF_LINES]),
                "truncated": truncated,
            },
        )

    def _ask_user(self, kind: str, subject: str, detail: dict) -> bool:
        """Let the user overrule a refusal. False keeps the refusal."""
        if self._approve is None:
            return False
        request = self._approve(
            kind=kind, agent=self.role.name, session_id=self.session_id,
            step_id=self.step_id, subject=subject, detail=detail)
        return bool(request is not None and request.allowed)

    def remember(self, note: str) -> ToolOutcome:
        stored, message = self.memory.note(self.role.name, note)
        return ToolOutcome(
            message,
            detail={"kind": "memory", "note": note, "stored": stored,
                    "agent": self.role.name},
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

    def _execute(self, argv: list, command: str, shell: bool,
                 background: bool = False) -> ToolOutcome:
        """Run a command, announcing it first and staying cancellable."""
        command_id = f"cmd_{uuid.uuid4().hex[:10]}"
        started = time.time()

        log_path = ""
        if background:
            logs = self.project / ".trance" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            log_file = logs / f"{command_id}.log"
            log_path = str(log_file.relative_to(self.project))
            sink = open(log_file, "w", encoding="utf8")
        else:
            sink = subprocess.PIPE

        self.notify("command_started", {
            "command_id": command_id, "command": command, "cwd": str(self.command_cwd),
            "timeout_s": None if background else COMMAND_TIMEOUT_S,
            "background": background, "log": log_path,
        })
        try:
            proc = subprocess.Popen(
                argv, cwd=self.command_cwd, stdout=sink,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, start_new_session=True,
            )
        except FileNotFoundError:
            self.notify("command_finished", {"command_id": command_id, "exit_code": None})
            return ToolOutcome(f"{argv[0]} is not installed on this machine.", ok=False)
        finally:
            if background:
                sink.close()

        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        with _RUNNING_LOCK:
            _RUNNING[command_id] = RunningCommand(
                command_id=command_id, proc=proc, pgid=pgid, command=command,
                background=background, log_path=log_path)

        if background:
            # Give it a moment to fall over, so an instant crash is reported as
            # one rather than as a healthy start.
            time.sleep(1.0)
            if proc.poll() is not None:
                with _RUNNING_LOCK:
                    _RUNNING.pop(command_id, None)
                self.notify("command_finished", {
                    "command_id": command_id, "exit_code": proc.returncode, "seconds": 1.0})
                tail = self._tail(log_path)
                return ToolOutcome(
                    f"$ {command}\nStarted in the background but exited immediately "
                    f"(exit={proc.returncode}).\n{tail}", ok=False,
                    detail={"kind": "command", "command": command,
                            "exit_code": proc.returncode, "output": tail, "shell": shell})
            return ToolOutcome(
                f"$ {command}\nRunning in the background (id {command_id}).\n"
                f"Output is being written to {log_path} — read_file it to see what the "
                f"process has printed so far.\n"
                f"It keeps running while you do other things. Test it now (a request, a "
                f"port check), then stop it with stop_command('{command_id}').",
                ok=True,
                detail={"kind": "background", "command": command, "command_id": command_id,
                        "log": log_path})

        timed_out = cancelled = False
        try:
            output = proc.communicate(timeout=COMMAND_TIMEOUT_S)[0] or ""
        except subprocess.TimeoutExpired:
            kill_group(pgid)
            try:
                output = proc.communicate(timeout=5)[0] or ""
            except subprocess.TimeoutExpired:
                output = "(output unavailable — a background process kept the pipe open)"
            timed_out = True
        finally:
            with _RUNNING_LOCK:
                _RUNNING.pop(command_id, None)

        code = proc.returncode
        if code is not None and code < 0 and not timed_out:
            cancelled = True
        elapsed = round(time.time() - started, 1)
        self.notify("command_finished", {
            "command_id": command_id, "exit_code": code, "seconds": elapsed,
            "timed_out": timed_out, "cancelled": cancelled,
        })

        output = (output or "").strip() or "(no output)"
        if len(output) > MAX_COMMAND_OUTPUT:
            half = MAX_COMMAND_OUTPUT // 2
            output = output[:half] + "\n… (trimmed) …\n" + output[-half:]

        if cancelled:
            text = f"$ {command}\nCancelled by the user after {elapsed}s.\n{output}"
        elif timed_out:
            text = (f"$ {command}\nTimed out after {COMMAND_TIMEOUT_S}s and was killed.\n"
                    f"{output}\n\nThis command does not exit on its own. To run a server "
                    f"or watcher, pass background=true — it starts, you keep working, and "
                    f"you stop it with stop_command when you are done.")
        else:
            text = f"$ {command}\nexit={code}\n{output}"

        return ToolOutcome(
            text, ok=(code == 0 and not timed_out and not cancelled),
            detail={"kind": "command", "command": command, "exit_code": code,
                    "output": output, "shell": shell, "seconds": elapsed,
                    "timed_out": timed_out, "cancelled": cancelled,
                    "command_id": command_id})

    def _tail(self, log_path: str, lines: int = 25) -> str:
        try:
            text = (self.project / log_path).read_text(encoding="utf8", errors="replace")
        except OSError:
            return "(no output captured)"
        return "\n".join(text.splitlines()[-lines:]) or "(no output)"

    def _run_via_shell(self, command: str, background: bool = False) -> ToolOutcome:
        """Run through a shell, but still check every program it would invoke."""
        programs = programs_in(command)
        missing = _shell_missing(programs, self.allowed_commands)
        if missing and not self._ask_user(
                "command", command,
                {"programs": missing, "agent_has_own_list": bool(self.role.commands)}):
            return self._refuse_programs(missing, command)
        return self._execute(["bash", "-c", command], command, shell=True,
                             background=background)

    def stop_command(self, command_id: str) -> ToolOutcome:
        """Stop a background process this agent started."""
        if cancel_command(command_id):
            return ToolOutcome(f"Stopped {command_id}.",
                               detail={"kind": "command_stopped", "command_id": command_id})
        return ToolOutcome(f"{command_id} is not running — it may have exited already.",
                           ok=False)


    def _refuse_programs(self, missing: list, command: str) -> ToolOutcome:
        """Refuse, naming the programs so the UI can offer to allow them."""
        return ToolOutcome(
            f"Refused: {', '.join(repr(m) for m in missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not in this agent's allowlist. "
            f"Nothing was executed. Allowed: {', '.join(sorted(self.allowed_commands))}.",
            ok=False,
            detail={"kind": "refused_program", "programs": missing, "command": command,
                    "agent": self.role.name,
                    "agent_has_own_list": bool(getattr(self.role, "commands", None))},
        )

    # ---------------------------------------------------------- commands

    def run_command(self, command: str, background: bool = False) -> ToolOutcome:
        stripped = command.strip()
        if stripped.endswith("&") and not stripped.endswith("&&"):
            # A trailing & used to mean "block for the full timeout, then die":
            # the shell returned at once but the real process held our pipe.
            background = True
            command = stripped[:-1].strip()
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return ToolOutcome(f"Could not parse command: {exc}", ok=False)
        if not parts:
            return ToolOutcome("Empty command.", ok=False)

        if self.shell_enabled:
            return self._run_via_shell(command, background=background)

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
        if parts[0] not in self.allowed_commands and not self._ask_user(
                "command", command,
                {"programs": [parts[0]], "agent_has_own_list": bool(self.role.commands)}):
            return self._refuse_programs([parts[0]], command)
        return self._execute(parts, command, shell=False, background=background)


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
        lines.append(
            "A reply has a length limit, and a tool call that runs past it is cut off "
            "and never runs. For a long file, write_file the first section and then "
            "append_file each remaining section — do not try to emit the whole thing "
            "in one call and do not resend a call that was cut off."
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
            "search_symbols) across the whole project — including files outside your write "
            "remit. Prefer it over reading a whole file to find one symbol."
        )

    if {"files", "commands", "graph"} & set(role.toolsets):
        lines.append(
            "You may write to the project's shared memory with remember. Every agent on this "
            "project reads it from now on, so put decisions there that others must match — "
            "and nothing else."
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
