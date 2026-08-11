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

from .. import paths
from ..model import estimate_tokens
from .memory import ProjectMemory
from .roles import AgentRole

#: Deliberately small. A single 100KB read is ~25k tokens and will blow a 64k
#: context window on its own — the runner trims, but not overflowing in the
#: first place is cheaper and keeps the model's attention on relevant code.
MAX_READ_BYTES = 24_000
#: Below this a whole file is cheap enough that an outline would just cost a
#: round trip. Above it, an indexed file answers with its shape first.
OUTLINE_OVER_BYTES = 4_000
#: How much of the top of the file comes with the outline. Imports, requires and
#: module constants are what an agent actually needs alongside the symbol list,
#: and they are always at the top.
OUTLINE_HEAD_LINES = 30
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
#: Every named list, so a role can point at one. The tool layer holds a copy
#: rather than reaching for the store: it is the enforcement point and must work
#: in the CLI and the tests, where no store exists.
_LISTS: dict[str, CommandPolicy] = {}
#: What the default list is called when it travels with the others.
DEFAULT_LIST_NAME = "default"

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


def set_command_lists(lists: dict) -> None:
    """Install the named allowlists (the server does this at startup)."""
    global _LISTS
    _LISTS = dict(lists or {})


def command_lists() -> dict:
    """Every named list, plus the default under its own name.

    Read by anything that has to carry the policy somewhere else — a delegated
    step's tool server runs in its own process and cannot reach this one.
    """
    return {**_LISTS, DEFAULT_LIST_NAME: _POLICY}


def command_list(name: str | None) -> CommandPolicy:
    """The named list, falling back to the default policy."""
    return _LISTS.get(name or "") or _POLICY


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
                 memory=None, approve=None, session_id: str = "", step_id: str = "",
                 reindex=None, vision_config=None):
        self.project = Path(project).resolve()
        #: The model that answers `look` — the agent's own. None only when this
        #: is built outside a run; the browser tools still open pages and probe
        #: canvases then, and only the question-asking is refused.
        self.vision_config = vision_config
        self._visual = None
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
        #: Re-reads the repo into the graph. The index is a snapshot from before
        #: this step, so an agent asking about a file it just wrote gets nothing.
        self._reindex = reindex
        self._wrote_since_index = False
        #: Re-indexes spent this turn. A write earns one; one more covers files
        #: changed by something else — a command, another agent, the user. Past
        #: that a miss is a real miss, and a model guessing names would
        #: otherwise re-index once per guess.
        self._reindexes_left = 1
        #: Called with (event_type, payload) so a long command is visible while
        #: it runs instead of only when it finishes.
        self.notify = notify or (lambda kind, payload: None)

    def _may_reindex(self) -> bool:
        """Spend a re-index, if one is left."""
        if self._wrote_since_index:
            self._wrote_since_index = False
            return True
        if self._reindexes_left > 0:
            self._reindexes_left -= 1
            return True
        return False

    @property
    def command_policy(self) -> CommandPolicy:
        """Which list applies to this agent: the one it names, else the default."""
        return command_list(getattr(self.role, "command_list", ""))

    @property
    def allowed_commands(self) -> set[str]:
        """This agent's allowlist — its own programs, else its named list."""
        return set(getattr(self.role, "commands", None) or self.command_policy.allowed)

    @property
    def shell_enabled(self) -> bool:
        """Whether this agent may use pipes, redirects and `&&`."""
        role_setting = getattr(self.role, "shell", None)
        return self.command_policy.shell if role_setting is None else bool(role_setting)

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
                _fn("read_file",
                    "Read a file. A large indexed file answers with its outline — the "
                    "symbols it defines and their line numbers — because you almost "
                    "always want one of them, and get_definition returns that exactly. "
                    "Small files come back whole.",
                    {"path": {"type": "string", "description": "Path relative to the project root."},
                     "start_line": {"type": "integer",
                                    "description": ("First line to return, 1-based. Pages "
                                                    "through a file past a truncated read.")},
                     "full": {"type": "boolean",
                              "description": ("Return every line instead of an outline. Needed "
                                              "only when you are about to rewrite the file.")}},
                    ["path"]),
                _fn("write_file",
                    "Create or overwrite a file with complete contents. Parent directories "
                    "are created automatically — never call mkdir first.",
                    {"path": {"type": "string"}, "content": {"type": "string",
                     "description": "The ENTIRE file contents. Not a diff, not a fragment."}},
                    ["path", "content"]),
                _fn("edit_file",
                    "Change part of a file: replace an exact snippet with new text. "
                    "USE THIS for any change to an existing file — rewriting a whole "
                    "file to change a few lines costs the whole file in output and gets "
                    "cut off. The snippet must appear exactly once; include a line "
                    "either side if it would not.",
                    {"path": {"type": "string"},
                     "find": {"type": "string",
                              "description": ("The exact text to replace, copied from "
                                              "read_file — indentation and all.")},
                     "replace": {"type": "string", "description": "What to put there."}},
                    ["path", "find", "replace"]),
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
                {"command": {"type": "string", "description": (
                     "e.g. 'pytest -q'. Something that finishes on its own. "
                     "Ending it with & does NOT put it in the background: the "
                     "shell keeps the pipe open, so this call blocks until the "
                     "timeout kills the lot — use the background flag instead.")},
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
        if "browser" in self.role.toolsets:
            out += [
                _fn("open_page",
                    "Open this project in a real browser and report what happened: "
                    "console errors, failed requests, whether a canvas exists and "
                    "whether it painted. Call this first. The page stays open, so "
                    "later calls act on the same running app.",
                    {"path": {"type": "string",
                              "description": ("The HTML file to open, relative to the "
                                              "project root. Omit to use the project's "
                                              "index.html.")}},
                    []),
                _fn("press_key",
                    "Send a key to the page, as a keyboard would. Use this to get past "
                    "a title screen or menu — an app waiting on 'press SPACE' shows you "
                    "nothing about itself until you do — and to drive it once running. "
                    "The result says whether the page received the key and whether the "
                    "picture changed, so you never have to assume either.",
                    {"key": {"type": "string",
                             "description": ("Space, Enter, Escape, ArrowUp, ArrowDown, "
                                             "ArrowLeft, ArrowRight, Tab, or a single "
                                             "character such as w or 1.")},
                     "times": {"type": "integer",
                               "description": "How many times to press it. Default 1."},
                     "hold": {"type": "integer",
                              "description": (
                                  "Animation frames to hold the key down for. Default 8, "
                                  "which is enough for a game that checks the keyboard "
                                  "each frame to notice. Raise it to move something a "
                                  "long way in one press — 60 frames is about a second "
                                  "of holding the key.")}},
                    ["key"]),
                _fn("wait",
                    "Let the app run for a number of animation frames before you judge "
                    "it. A screen is not finished the moment it appears — characters "
                    "enter, animations play, a countdown runs — so judging the frame "
                    "straight after a keypress can fail an app that is working. 60 "
                    "frames is about a second. Use 120-240 after starting something.",
                    {"frames": {"type": "integer",
                                "description": "Animation frames to let run. Default 120."}},
                    []),
                _fn("check_canvas",
                    "Check the canvas without a screenshot: whether it painted at all "
                    "(a single flat colour means it did not), whether the picture is "
                    "still changing (an unchanged one means the render loop is dead), "
                    "and any errors since the last check. Cheap — prefer it over `look` "
                    "for anything it can answer.",
                    {"frames": {"type": "integer",
                                "description": "Frames to watch for movement. Default 30."}},
                    []),
                _fn("look",
                    "Take a screenshot and ask a vision model about it. Costs a model "
                    "call, so ask about what only a picture can settle — layout, "
                    "overlap, what is drawn where. Ask specific, answerable questions, "
                    "not whether it looks good.",
                    {"question": {"type": "string",
                                  "description": ("What you want to know about what is "
                                                  "on screen right now.")},
                     "checks": {"type": "array", "items": {"type": "string"},
                                "description": ("Specific things to verify, one per "
                                                "entry. The task's acceptance criteria "
                                                "belong here.")},
                     "whole_page": {"type": "boolean",
                                    "description": ("Photograph the whole page instead "
                                                    "of just the canvas. Default false.")}},
                    ["question"]),
                _fn("watch",
                    "Take a burst of screenshots over a span of time and ask the vision "
                    "model about the whole sequence. Use this for anything about motion "
                    "— does it move, flicker, animate smoothly, snap back — which a "
                    "single screenshot cannot show and two endpoints cannot prove. "
                    "Costs one vision call carrying every frame, so use `look` when a "
                    "single picture answers the question.",
                    {"question": {"type": "string",
                                  "description": ("What you want to know about how the "
                                                  "screen behaves over time.")},
                     "checks": {"type": "array", "items": {"type": "string"},
                                "description": ("Specific things to verify, one per "
                                                "entry.")},
                     "frames": {"type": "integer",
                                "description": ("Animation frames to spread the burst "
                                                "over. 60 is about a second. "
                                                "Default 180.")},
                     "shots": {"type": "integer",
                               "description": "Pictures to take. Default 8, max 24."}},
                    ["question"]),
            ]
        if ("graph" in self.role.toolsets and self.graph is not None
                and "files" in self.role.toolsets):
            out.append(_fn(
                "replace_symbol",
                "Replace one indexed function or class with new source, without "
                "quoting the old code back. The cheapest way to change a function in "
                "a large file.",
                {"symbol": {"type": "string",
                            "description": "Its name, or path/to/file.js::name."},
                 "source": {"type": "string",
                            "description": "The complete new definition, including "
                                           "its signature line."}},
                ["symbol", "source"]))
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
            "edit_file": self.edit_file,
            "replace_symbol": self.replace_symbol,
            "list_files": self.list_files,
            "run_command": self.run_command,
            "stop_command": self.stop_command,
            "check_file": self.check_file,
            "check_files": self.check_files,
            "remember": self.remember,
            "open_page": self.open_page,
            "press_key": self.press_key,
            "wait": self.wait,
            "check_canvas": self.check_canvas,
            "look": self.look,
            "watch": self.watch,
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
            if not result.hit and self._reindex is not None and self._may_reindex():
                # It may be asking about something written a moment ago — by
                # itself, or by a command it ran. Re-indexing is incremental, so
                # this costs the files that actually changed.
                try:
                    self._reindex()
                except Exception:                  # indexing must never break a tool
                    pass
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
        """The file a path names, or None if it is not in this project.

        Written how the model wrote it: `/src/app.js`, `src/./app.js` and
        `src/app.js` are one file, and only the last used to work.
        """
        return paths.inside(self.project, path)

    def _outline(self, rel: str, text: str) -> ToolOutcome | None:
        """The shape of a big file instead of all of it.

        A 10KB file is ~2,600 tokens; its outline is ~150. The agent nearly
        always wants one function out of it, and get_definition gives exactly
        that. Whole-file reads stay available — `full=true` — because an agent
        about to rewrite a file genuinely needs every line of it.
        """
        if self.graph is None:
            return None
        try:
            symbols = self.graph.db.symbols_in_file(rel)
        except Exception:                                  # never break a read
            return None
        named = [s for s in symbols if s.kind != "variable"]
        if not named:
            return None

        lines = text.splitlines()
        first = min((s.start_line for s in named), default=len(lines) + 1)
        head = lines[:max(0, min(first - 1, OUTLINE_HEAD_LINES))]
        listing = "\n".join(
            f"  {s.kind} {s.qualname.split('::', 1)[-1]}  (lines {s.start_line}-{s.end_line})"
            for s in symbols[:60])
        body = (
            f"# {rel} — outline ({len(lines)} lines, {len(symbols)} symbols)\n\n"
            + ("Top of the file:\n" + "\n".join(head) + "\n\n" if head else "")
            + f"Defines:\n{listing}\n\n"
            + "This file is large, so you have its shape rather than all of it. "
              "get_definition('<name>') returns any of those in full; read_file with "
              "start_line pages through it; read_file with full=true gives the whole "
              "file, which you need only if you are about to rewrite it."
        )
        return ToolOutcome(body, detail={"kind": "read", "path": rel, "bytes": len(text),
                                         "outline": True, "symbols": len(symbols),
                                         "lines": len(lines)})

    def edit_file(self, path: str, find: str, replace: str) -> ToolOutcome:
        """Replace one exact snippet, leaving the rest of the file untouched.

        Rewriting a whole file to change ten lines costs the whole file in
        output tokens — a 600-line module is most of a reply on its own, and the
        call gets cut off mid-string. This is the way out: the cost of an edit
        becomes the size of the edit.

        The snippet must appear exactly once. Refusing an ambiguous match is the
        point: "replace the first one" is a guess about which one the agent
        meant, and silently editing the wrong occurrence is worse than failing.
        """
        target = self._resolve(path)
        if target is None:
            return ToolOutcome(f"Refused: {path!r} is outside the project directory.", ok=False)
        if not target.is_file():
            return ToolOutcome(f"{path} does not exist — use write_file to create it.",
                               ok=False)
        if not find:
            return ToolOutcome("`find` is required: give the exact text to replace.",
                               ok=False)

        text = target.read_text(encoding="utf8", errors="replace")
        found = text.count(find)
        if found == 0:
            return ToolOutcome(
                f"Not found in {path}. The text must match exactly, including "
                f"indentation and line breaks — read_file the part you are changing "
                f"and copy it from there rather than retyping it.", ok=False,
                detail={"kind": "edit_miss", "path": path})
        if found > 1:
            return ToolOutcome(
                f"That text appears {found} times in {path}, so I cannot tell which "
                f"one you mean. Include a line or two around it to make it unique.",
                ok=False, detail={"kind": "edit_ambiguous", "path": path, "count": found})

        return self._put(path, text.replace(find, replace, 1), append=False, kind="edit")

    def replace_symbol(self, symbol: str, source: str) -> ToolOutcome:
        """Replace one indexed function or class with new source.

        The same saving as edit_file without having to quote the old code back:
        the graph already knows where the symbol starts and ends.
        """
        if self.graph is None:
            return ToolOutcome("No code graph available — use edit_file instead.", ok=False)
        matches = self.graph.db.find_symbols(symbol)
        if not matches:
            return ToolOutcome(
                f"No symbol named {symbol!r} is indexed. search_symbols finds what is, "
                f"and the project map in your prompt lists it.", ok=False,
                detail={"kind": "edit_miss", "symbol": symbol})
        if len(matches) > 1:
            where = ", ".join(m.qualname for m in matches[:6])
            return ToolOutcome(
                f"{symbol!r} matches {len(matches)} symbols — name one exactly: {where}",
                ok=False, detail={"kind": "edit_ambiguous", "symbol": symbol})

        found = matches[0]
        target = self._resolve(found.file_path)
        if target is None or not target.is_file():
            return ToolOutcome(f"{found.file_path} is no longer there — re-read it.",
                               ok=False)
        raw = target.read_bytes()
        # Byte offsets, because that is what the parser recorded; slicing the
        # decoded text would drift on any non-ASCII character in the file.
        updated = raw[:found.start_byte] + source.encode("utf8") + raw[found.end_byte:]
        return self._put(found.file_path, updated.decode("utf8", errors="replace"),
                         append=False, kind="edit")

    def read_file(self, path: str, start_line: int = 1, full: bool = False) -> ToolOutcome:  # noqa: D401
        target = self._resolve(path)
        if target is None:
            return ToolOutcome(f"Refused: {path!r} is outside the project directory.", ok=False)
        if not target.is_file():
            return ToolOutcome(f"{path} does not exist. Use list_files to see what does.", ok=False)
        raw = target.read_bytes()
        text = raw.decode("utf8", errors="replace")
        rel = target.relative_to(self.project).as_posix()
        start = max(1, int(start_line or 1))

        if not full and start == 1 and len(raw) > OUTLINE_OVER_BYTES:
            outline = self._outline(rel, text)
            if outline is not None:
                return outline

        lines = text.splitlines()

        # A file bigger than the cap used to return its first 24KB and nothing
        # else, so an agent that needed line 900 could only read the same first
        # 24KB again. Paging is the way out of that.
        body = "\n".join(lines[start - 1:])
        shown = body[:MAX_READ_BYTES]
        last = start - 1 + len(shown.splitlines())
        header = f"# {path} (lines {start}-{last} of {len(lines)})"
        if len(body) > MAX_READ_BYTES:
            shown += (f"\n… truncated at {MAX_READ_BYTES} bytes. Continue with "
                      f"read_file(path={path!r}, start_line={last + 1}), or fetch one "
                      f"symbol with get_definition instead of the whole file.")
        return ToolOutcome(f"{header}\n{shown}",
                           detail={"kind": "read", "path": path, "bytes": len(raw),
                                   "start_line": start, "last_line": last,
                                   "lines": len(lines)})

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

    def _put(self, path: str, content: str, *, append: bool, kind: str = "") -> ToolOutcome:
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
        self._wrote_since_index = True

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
        verb = ("Edited" if kind == "edit" else
                "Appended to" if append else ("Updated" if existed else "Created"))
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

    # ------------------------------------------------------------- browser
    #
    # The one toolset whose absence is normal. Every failure below reports
    # itself as a tool result the agent can read and act on, never as an
    # exception that ends the step: "there is no browser here" is information,
    # and an agent that learns it can still say so in its verdict.

    @property
    def visual(self):
        """The step's browser session, started on first use."""
        if self._visual is None:
            from .visual import VisualSession

            self._visual = VisualSession(
                self.project, session_id=self.session_id, step_id=self.step_id,
                cancel_token=self.session_id)
        return self._visual

    def close(self) -> None:
        """Release anything the step held open. Safe to call twice."""
        if self._visual is not None:
            self._visual.close()
            self._visual = None

    def open_page(self, path: str = "") -> ToolOutcome:
        from ..browser import BrowserUnavailable

        try:
            found = self.visual.open(path)
        except BrowserUnavailable as exc:
            return ToolOutcome(f"Could not open the page: {exc}", ok=False,
                               detail={"kind": "page_failed", "error": str(exc)})

        probe, errors = found["probe"], found["errors"]
        lines = [f"Opened {found['page']} at {found['url']}"]
        if found["frames"] < found["asked_frames"]:
            lines.append(f"WARNING: only {found['frames']} of {found['asked_frames']} "
                         "animation frames ran — the page's main thread is blocked or "
                         "the tab is being throttled. (An app whose own draw loop has "
                         "died still produces frames; check_canvas is what finds that.)")
        if found.get("dev_server"):
            lines.append(f"The project's own dev server is running behind this page "
                         f"({found['dev_server']}); it stops when your step ends.")
        if probe.get("canvas"):
            lines.append(f"Canvas: {probe.get('w')}x{probe.get('h')}"
                         + (f" ({probe['count']} canvases, largest measured)"
                            if probe.get("count", 1) > 1 else ""))
            if probe.get("uniform") is True:
                lines.append("The canvas is a single flat colour — nothing has been drawn on it.")
            elif probe.get("uniform") is False:
                lines.append("The canvas has been painted.")
            else:
                lines.append(f"Could not read the canvas back ({probe.get('note') or 'unknown'}); "
                             "use look to see it instead.")
        else:
            lines.append("No canvas on this page.")
        lines.append(_render_page_errors(errors))
        return ToolOutcome("\n".join(lines), ok=True,
                           detail={"kind": "page", "page": found["page"], "url": found["url"],
                                   "frames": found["frames"], "asked_frames": found["asked_frames"],
                                   "needs_build": found["needs_build"], "errors": errors,
                                   "canvas": probe.get("canvas", False),
                                   "size": (f"{probe.get('w')}x{probe.get('h')}"
                                            if probe.get("canvas") else ""),
                                   "blank": probe.get("uniform")})

    def press_key(self, key: str, times: int = 1, hold: int = 0) -> ToolOutcome:
        from ..browser import BrowserUnavailable

        try:
            times = max(1, min(int(times or 1), 100))
        except (TypeError, ValueError):
            times = 1
        try:
            hold = max(0, min(int(hold or 0), 600))
        except (TypeError, ValueError):
            hold = 0
        try:
            result = self.visual.press(str(key), times, hold_frames=hold or None)
        except (BrowserUnavailable, ValueError) as exc:
            return ToolOutcome(f"Could not press {key!r}: {exc}", ok=False,
                               detail={"kind": "key_failed", "key": str(key), "error": str(exc)})

        delivered, changed = bool(result.get("delivered")), result.get("changed")
        held = result.get("held_frames")
        lines = [f"Pressed {key}" + (f" {times} times" if times > 1 else "")
                 + (f", held for {held} frames each" if held else "") + "."]
        # Said in full because the two halves have different fixes, and because
        # an agent told only "pressed" has no reason to believe anything
        # happened — which is exactly how a working keypress got reported as a
        # dead one.
        if not delivered:
            lines.append("The page did not receive the key at all. That is a browser "
                         "problem, not the app ignoring you.")
        elif changed is True:
            lines.append(f"The page received it and the screen changed over "
                         f"{result.get('frames', 0)} frames — the app responded.")
        elif changed is False:
            lines.append(f"The page received it but the screen did not change over "
                         f"{result.get('frames', 0)} frames. Either the app does not act "
                         f"on this key in its current state, or it needs a different one.")
        else:
            lines.append("The page received it, but the screen could not be compared.")
        # The measured comparison, in full. "Changed" alone cannot tell a moving
        # starfield from a screen transition, and 0.5% versus 14% is exactly
        # that difference.
        described = (result.get("diff") or {}).get("described")
        if described:
            lines.append(described)
        probe = result.get("probe") or {}
        if probe.get("uniform") is True:
            lines.append("The canvas is a single flat colour — nothing is drawn on it.")
        return ToolOutcome(
            " ".join(lines), ok=True,
            detail={"kind": "key", "key": str(key), "times": times,
                    "delivered": delivered, "changed": changed,
                    "held_frames": held,
                    "frames": result.get("frames", 0), "diff": result.get("diff"),
                    "shot_before": result.get("shot_before", ""),
                    "shot_after": result.get("shot_after", "")})

    def wait(self, frames: int = 120) -> ToolOutcome:
        from ..browser import BrowserUnavailable

        try:
            frames = max(1, min(int(frames or 120), 1200))
        except (TypeError, ValueError):
            frames = 120
        try:
            found = self.visual.wait(frames)
        except BrowserUnavailable as exc:
            return ToolOutcome(f"Could not wait: {exc}", ok=False,
                               detail={"kind": "wait_failed", "error": str(exc)})

        lines = [f"Let {found['frames']} animation frames run"
                 + (f" (asked for {found['asked_frames']})" if found["stalled"] else "") + "."]
        if found["stalled"]:
            # Careful what this means. The frame counter runs on its own
            # requestAnimationFrame chain, so it keeps counting after the app's
            # draw loop stops — falling short means the *browser* stopped
            # producing frames, which is a blocked main thread, not a dead
            # render loop. That one shows up as the picture not changing.
            lines.append("The page stopped producing frames before that — its main "
                         "thread is blocked or the tab was throttled.")
        lines.append("The screen changed while waiting." if found["changed"]
                     else "The screen did not change at all while waiting."
                     if found["changed"] is False else "")
        described = (found.get("diff") or {}).get("described")
        if described:
            lines.append(described)
        lines.append(_render_page_errors(found["errors"]))
        return ToolOutcome("\n".join(l for l in lines if l), ok=True,
                           detail={"kind": "wait", "frames": found["frames"],
                                   "asked_frames": found["asked_frames"],
                                   "changed": found["changed"], "stalled": found["stalled"],
                                   "errors": found["errors"], "diff": found.get("diff"),
                                   "shot_before": found.get("shot_before", ""),
                                   "shot_after": found.get("shot_after", "")})

    def check_canvas(self, frames: int = 30) -> ToolOutcome:
        from ..browser import BrowserUnavailable

        try:
            frames = max(2, min(int(frames or 30), 600))
        except (TypeError, ValueError):
            frames = 30
        try:
            found = self.visual.check(frames)
        except BrowserUnavailable as exc:
            return ToolOutcome(f"Could not check the canvas: {exc}", ok=False,
                               detail={"kind": "canvas_failed", "error": str(exc)})

        if not found["canvas"]:
            lines = ["There is no canvas on this page."]
        else:
            lines = [f"Canvas {found['size']}"
                     + (f" ({found['canvases']} on the page)" if found["canvases"] > 1 else "")]
            lines.append("BLANK — a single flat colour, nothing drawn." if found["blank"] is True
                         else "Painted." if found["blank"] is False
                         else f"Could not read the canvas back ({found['note'] or 'unknown'}).")
            lines.append("FROZEN — the picture did not change over "
                         f"{found['frames']} frames; the render loop is not running."
                         if found["moving"] is False
                         else f"Moving — the picture changed over {found['frames']} frames."
                         if found["moving"] else "Could not tell whether the picture is changing.")
        lines.append(_render_page_errors(found["errors"]))
        # A blank or frozen canvas is a finding, not a broken tool: the call
        # worked. `ok` stays true so the UI shows it as an answer, and the words
        # carry the bad news.
        return ToolOutcome("\n".join(lines), ok=True,
                           detail={"kind": "canvas", **{k: found[k] for k in
                                   ("canvas", "canvases", "size", "blank", "moving", "frames",
                                    "note", "errors")}})

    def look(self, question: str, checks: list | None = None,
             whole_page: bool = False) -> ToolOutcome:
        """Screenshot the app and ask the vision model about it."""
        from ..browser import BrowserUnavailable
        from ..vision import VisionUnavailable, look as ask_vision

        if self.vision_config is None:
            return ToolOutcome(
                "No model is available to look at the screen. Use check_canvas for what "
                "can be measured without one, and say in your verdict that the visual "
                "check could not be made.",
                ok=False, detail={"kind": "look_failed", "error": "no vision model"})
        if isinstance(checks, str):
            checks = [checks]
        checks = [str(c) for c in (checks or [])][:12]

        try:
            png, meta = self.visual.capture(whole_page=bool(whole_page))
        except BrowserUnavailable as exc:
            return ToolOutcome(f"Could not take a screenshot: {exc}", ok=False,
                               detail={"kind": "look_failed", "error": str(exc)})
        shot = self.visual.save(png)
        try:
            seen = ask_vision(png, str(question), self.vision_config,
                              checks=checks, cancel_token=self.session_id)
        except VisionUnavailable as exc:
            # The picture was taken and is worth keeping even though nothing
            # judged it — it is the one artefact that lets a person check.
            return ToolOutcome(
                f"The screenshot was taken but the vision model could not answer: {exc}",
                ok=False,
                detail={"kind": "screenshot", "shot": shot, "question": str(question),
                        "checks": checks, "answer": "", "error": str(exc), **meta})
        return ToolOutcome(
            seen["answer"], ok=True,
            detail={"kind": "screenshot", "shot": shot, "question": str(question),
                    "checks": checks, "answer": seen["answer"], "prompt": seen["prompt"],
                    "model": seen["model"], "preset": seen["preset"],
                    "usage": seen["usage"], **meta})

    def watch(self, question: str, checks: list | None = None,
              frames: int = 180, shots: int = 8) -> ToolOutcome:
        """Film the screen and ask the vision model about the sequence."""
        from ..browser import BrowserUnavailable
        from ..vision import VisionUnavailable, look_sequence

        if isinstance(checks, str):
            checks = [checks]
        checks = [str(c) for c in (checks or [])][:12]
        try:
            frames = max(2, min(int(frames or 180), 1200))
        except (TypeError, ValueError):
            frames = 180
        try:
            shots = max(2, min(int(shots or 8), 24))
        except (TypeError, ValueError):
            shots = 8

        try:
            made = self.visual.film(frames=frames, shots=shots)
        except BrowserUnavailable as exc:
            return ToolOutcome(f"Could not film the screen: {exc}", ok=False,
                               detail={"kind": "watch_failed", "error": str(exc)})

        pngs = made.pop("pngs")
        detail = {"kind": "film", "shots": made["shots"], "question": str(question),
                  "checks": checks, "frames": made["frames"],
                  "frames_between": made["frames_between"], "motion": made["motion"],
                  "moving": made["moving"], "answer": ""}
        # What was measured, said even when a model also answers: "changed
        # between every pair" and "froze after frame 3" are facts the fractions
        # carry and prose can garble.
        moved = sum(1 for f in made["motion"] if f > 0)
        measured = (f"{len(pngs)} frames over {made['frames']} animation frames; "
                    f"the screen changed in {moved} of {len(made['motion'])} intervals.")

        if self.vision_config is None:
            return ToolOutcome(
                measured + " No vision model is available to judge the sequence — "
                "the frames are saved, and the numbers above are what can be said.",
                ok=True, detail=detail)
        try:
            seen = look_sequence(pngs, str(question), self.vision_config, checks=checks,
                                 frames_between=made["frames_between"],
                                 cancel_token=self.session_id)
        except VisionUnavailable as exc:
            return ToolOutcome(
                measured + f" The vision model could not answer: {exc}",
                ok=False, detail={**detail, "error": str(exc)})
        detail.update({"answer": seen["answer"], "prompt": seen["prompt"],
                       "model": seen["model"], "preset": seen["preset"],
                       "usage": seen["usage"]})
        return ToolOutcome(f"{measured}\n\n{seen['answer']}", ok=True, detail=detail)

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
            # Said plainly, because the obvious reading of a killed command is
            # "it crashed, run it again" — and running it again is the one
            # response that wastes another three minutes of the step.
            text = (f"$ {command}\nSTOPPED BY THE USER after {elapsed}s. The person "
                    f"watching this run killed it deliberately; it did not fail on "
                    f"its own and it did not finish, so any output below is partial "
                    f"and nothing it would have done has been done.\n\n"
                    f"Do not run it again. Either it was taking too long — in which "
                    f"case a narrower command, or background=true if it never exits, "
                    f"is what was wanted — or it was the wrong thing to run. Work with "
                    f"what you have, or say in your report that you needed it.\n\n"
                    f"{output}")
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
            target = paths.inside(self.project, log_path)
            text = target.read_text(encoding="utf8", errors="replace") if target else ""
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


#: How many of a page's complaints to quote. A broken import can log the same
#: error every frame, and a hundred identical lines tell the agent nothing the
#: first three did not.
MAX_PAGE_ERRORS = 6


def _render_page_errors(errors: dict) -> str:
    """The page's complaints, deduped, as one readable block."""
    lines: list[str] = []
    for label, key in (("exception", "exceptions"), ("console error", "console"),
                       ("failed request", "failed_requests")):
        seen = list(dict.fromkeys(errors.get(key) or []))
        for text in seen[:MAX_PAGE_ERRORS]:
            lines.append(f"  {label}: {text}")
        if len(seen) > MAX_PAGE_ERRORS:
            lines.append(f"  ... and {len(seen) - MAX_PAGE_ERRORS} more {label}s")
    if not lines:
        return "No console errors, exceptions or failed requests."
    return "Errors on the page:\n" + "\n".join(lines)


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
            "and never runs. So CHANGE files with edit_file (replace an exact snippet) "
            "or replace_symbol (swap one function or class) — the cost of an edit is "
            "then the size of the edit, not the size of the file. write_file is for "
            "creating a file or replacing all of it; if that file is long, write the "
            "first section and append_file the rest."
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
        allowed = sorted(getattr(role, "commands", None)
                         or command_list(getattr(role, "command_list", "")).allowed)
        where = f"in {role.workdir}" if getattr(role, "workdir", "") else "in the project root"
        role_shell = getattr(role, "shell", None)
        listed = command_list(getattr(role, "command_list", ""))
        shell_on = listed.shell if role_shell is None else bool(role_shell)
        lines.append(
            f"You may run commands {where}, limited to these programs: " + ", ".join(allowed)
            + (". Pipes, redirects and && work, and every program in the line is checked "
               "against that list." if shell_on
               else ". One plain command per call — pipes and redirects are not available.")
            + f" Commands time out after {COMMAND_TIMEOUT_S}s."
        )
    else:
        lines.append("You may NOT run commands. You cannot execute tests, builds, or scripts.")

    if "browser" in role.toolsets:
        lines.append(
            "You may open this project in a real headless browser (open_page), send it keys "
            "(press_key), measure its canvas (check_canvas) and photograph it for a vision "
            "model to describe (look), or film a short burst for questions about motion "
            "(watch). A project that needs its own dev server (Vite and friends) gets its "
            "dev script from package.json started behind the page and stopped with your "
            "step; anything else is served statically from the folder holding the page."
        )

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
