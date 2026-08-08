"""Hand a whole step to Claude Code, instead of driving it round by round.

The ordinary path is trance's loop: one model call, one tool call, repeat. That
is what makes remits enforceable and context measurable — and it is precisely
what the `claude` CLI will not do, because it throttles programmatic use and an
agent loop is five or more calls per step.

So for that backend the step is delegated: one call, Claude Code's own loop, its
own tools, its own context management. Measured at three internal turns for a
one-line edit, in a single call the throttle allows.

What that costs, stated rather than hidden:

* **The remit is checked afterwards, not enforced during.** Claude Code writes
  files directly. So the step runs between two git checkpoints, and what it
  touched is read back from git — anything outside the remit fails the step and
  is named. The work is still on disk and still revertible; it is caught, not
  prevented.
* **Context is theirs.** trance's curated bundle goes in as the prompt, but
  Claude Code reads what it likes after that, and carries ~40,000 tokens of its
  own preamble and tools. The whole thesis of this project does not apply to
  this backend. It is here because a subscription is cheaper than an API bill,
  which is a different kind of saving.

Everything else stays trance's: which step runs, in what order, with which
prompt, judged by the same OUTCOME line and the same checks.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from fnmatch import fnmatch
from pathlib import Path

from .. import vcs
from ..providers.base import BackendError, Cancelled, clear_inflight, register_inflight
from ..providers.claudecode_client import DEFAULT_TIMEOUT_S, _is_abort, _why

#: Claude Code's own tools, used only when trance cannot serve its own — an
#: unindexed project has no MCP server to offer. They edit files directly, so
#: the remit is checked from the diff afterwards rather than enforced.
FALLBACK_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "TodoWrite"]

#: trance's tools, over MCP. With these the delegated step is a trance step:
#: a write outside the remit is refused as it happens, reads are deduplicated,
#: commands are checked against the allowlist, and every call reaches the
#: console. Claude Code's own file tools are switched off — two ways to write
#: the same file is two sets of rules for it.
AGENT_TOOLS = ["read_file", "write_file", "edit_file", "append_file", "replace_symbol",
               "list_files", "run_command", "stop_command", "check_file", "check_files",
               "remember"]
GRAPH_TOOLS = ["get_definition", "search_symbols", "get_callers", "get_callees"]

GRAPH_BRIEF = """

## The project's call graph

This project is indexed. Before reading a file to find something in it, ask:

- `get_definition("handleOrder")` — one symbol's source, or a file's outline
- `search_symbols("checkout")` — identifiers containing that text
- `get_callers("total")` — what calls it, before you change its signature
- `get_callees("total")` — what it calls

A 33KB file is about 8,400 tokens; the function you want is 150. Read whole
files when you genuinely need them, not to find one thing.
"""

#: How trance reads the result, the same line every other agent ends with.
OUTCOME_CONTRACT = """

When you are done, end your reply with exactly one of:

OUTCOME: SUCCESS
OUTCOME: FAILED — <one line saying what stopped you>

Say SUCCESS only if you finished the task and checked your own work. A wrong
success costs the next agent more than an honest failure.
"""


def delegated(kind: str) -> bool:
    """Whether this model runs the step itself rather than answering calls."""
    return kind == "claudecode"


class _Killable:
    """A running `claude -p`, in the shape the stop button already knows.

    Stop aborts every model call a session has open by shutting its socket. A
    delegated step is not a socket, it is a process — and one that runs for
    minutes — so it registers here and Stop kills the process group. Without it
    Stop said "stopping after the current agent turn" and then waited for a turn
    nothing could interrupt.
    """

    def __init__(self, proc):
        self.proc = proc
        self.aborted = False
        self.finished = False
        #: How many logged calls have been reported, so the catch-up at the end
        #: does not announce everything the watcher already did.
        self.reported = 0

    def abort(self) -> None:
        self.aborted = True
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except Exception:
                pass


def run_delegated(*, role, task: str, project: Path, config, bus, session_id: str,
                  step_id: str, memory=None, goal: str = "", placement: str = "",
                  steering: list[str] | None = None) -> dict:
    """Run one step inside Claude Code. Returns what the engine needs to judge it.

    The return is deliberately plain — text, files, usage, violations — so the
    caller treats it exactly like a turn from trance's own loop.
    """
    binary = _binary()
    before = vcs.head(project)

    indexed = _indexed(project)
    # Wound forward before the run, so only this step's calls are reported.
    seen_before = len(_lookups_logged(project))

    role_file = _write_role(project, role)
    prompt = _prompt(role=role, task=task, goal=goal, placement=placement,
                     memory=memory, steering=steering or [], indexed=indexed,
                     through_trance=True)
    served = AGENT_TOOLS + (GRAPH_TOOLS if indexed else [])
    command = [binary, "-p", prompt,
               "--output-format", "json",
               "--permission-mode", "acceptEdits",
               # Its own file tools off, ours on. Everything it does then passes
               # through the remit, the allowlist and the console.
               "--tools", "",
               "--allowedTools", *[f"mcp__trance__{name}" for name in served],
               "--mcp-config", json.dumps(_mcp_config(project, role_file)),
               "--strict-mcp-config"]
    if config.model:
        command += ["--model", config.model]

    bus.emit("delegated", session_id, agent=role.name, step_id=step_id, payload={
        "model": config.model or "claude code default",
        "message": (f"{role.name} is running this step inside Claude Code — one call, "
                    f"its own tools. What it changes is checked against the remit "
                    f"afterwards."),
    })

    # Its own process group, so Stop can take the whole tree down: the CLI
    # spawns children, and killing only the parent leaves them running.
    try:
        proc = subprocess.Popen(command, cwd=str(project), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise BackendError(f"could not run the claude CLI: {exc}") from exc

    handle = _Killable(proc)
    handle.reported = seen_before
    register_inflight(session_id, handle)
    # Tail the tool log while it runs. A delegated step took minutes and said
    # nothing; now each call appears as it is made, the same as any other agent.
    watcher = threading.Thread(
        target=_follow_calls,
        args=(project, seen_before, bus, session_id, step_id, role.name, handle),
        daemon=True, name=f"delegate-{step_id}")
    watcher.start()
    try:
        out, err = proc.communicate(timeout=config.timeout_s or DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        handle.abort()
        raise BackendError(
            f"Claude Code did not finish the step within "
            f"{config.timeout_s or DEFAULT_TIMEOUT_S:.0f}s") from exc
    finally:
        clear_inflight(session_id, handle)

    handle.finished = True
    watcher.join(timeout=2)
    if handle.aborted:
        raise Cancelled("the delegated step was stopped")

    done = subprocess.CompletedProcess(command, proc.returncode, out or "", err or "")

    if _is_abort(done.stdout) or done.returncode != 0:
        raise BackendError(_why(done.stderr or done.stdout, done.returncode))
    try:
        body = json.loads(done.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise BackendError(f"claude -p returned something that is not JSON: "
                           f"{(done.stdout or '')[:200]}") from exc
    if body.get("is_error"):
        raise BackendError(_why(body.get("result") or "", done.returncode))

    text = body.get("result") or ""
    usage = _usage(body.get("usage") or {})

    # Anything the watcher had not reached by the time the process exited.
    _report_calls(_lookups_logged(project)[handle.reported:], bus, session_id,
                  step_id, role.name)
    touched = _touched(project, before)
    outside = [p for p in touched if not _may_write(role, p)]

    bus.emit("model_call", session_id, agent=role.name, step_id=step_id, payload={
        "round": 1, "model": config.model, "preset": config.preset,
        "delegated": True, "turns": body.get("num_turns"),
        "messages": [{"role": "user", "content": prompt}],
        "response_text": text, "reasoning": "", "tool_calls": [],
        "finish_reason": body.get("stop_reason") or "stop", "usage": usage,
        "summary": {"est_tokens": usage.get("prompt_tokens", 0)},
    })
    for path in touched:
        bus.emit("file_written", session_id, agent=role.name, step_id=step_id,
                 payload={"path": path, "delegated": True})

    return {"text": text, "files_written": touched, "remit_violations": outside,
            "usage": usage, "turns": int(body.get("num_turns") or 1)}


# ------------------------------------------------------------------ helpers


def _binary() -> str:
    from ..providers.claudecode_client import available

    found = available()
    if not found:
        raise BackendError("the `claude` CLI is not on trance's PATH")
    return found


def _follow_calls(project: Path, start: int, bus, session_id: str, step_id: str,
                  agent: str, handle) -> None:
    """Report tool calls as the delegated step makes them."""
    seen = start
    while not handle.finished and not handle.aborted:
        rows = _lookups_logged(project)
        if len(rows) > seen:
            _report_calls(rows[seen:], bus, session_id, step_id, agent)
            seen = len(rows)
            handle.reported = seen
        time.sleep(0.4)
    handle.reported = max(getattr(handle, "reported", start), seen)


def _report_calls(rows: list[dict], bus, session_id: str, step_id: str,
                  agent: str) -> None:
    for row in rows:
        if row.get("event"):                  # a notification from the tool layer
            bus.emit(row["event"], session_id, agent=agent, step_id=step_id,
                     payload=row.get("payload") or {})
            continue
        bus.emit("tool_call", session_id, agent=agent, step_id=step_id, payload={
            "name": row.get("name") or "tool", "arguments": row.get("arguments") or {},
            "ok": bool(row.get("hit")), "result_tokens": (row.get("chars") or 0) // 4,
            "result": row.get("result") or ("answered" if row.get("hit") else "no match"),
            "detail": {"kind": "delegated", "via": "mcp"},
        })


def _lookups_logged(project: Path) -> list[dict]:
    """Every graph lookup the MCP server has written down for this project."""
    from ..mcp_server import CALL_LOG

    path = Path(project) / ".trance" / CALL_LOG
    try:
        lines = path.read_text(encoding="utf8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _indexed(project: Path) -> bool:
    """Is there a graph to offer? A fresh project has none yet."""
    from ..indexer.service import default_db_path

    return default_db_path(project).exists()


def _mcp_config(project: Path, role_file: Path) -> dict:
    """trance's tool server, described the way Claude Code expects to be told.

    Run with this interpreter rather than whatever `python` resolves to: trance
    may be in a virtualenv the CLI's environment knows nothing about.
    """
    return {"mcpServers": {"trance": {
        "command": sys.executable,
        "args": ["-m", "trance.mcp_server", str(project), str(role_file)],
    }}}


def _write_role(project: Path, role) -> Path:
    """The agent's definition, where the tool server can read it.

    Its remit travels with it, and so does the command list it is pointed at:
    the server runs in its own process, so anything it is not told falls back to
    trance's built-in defaults — which is not what the person editing the
    allowlist in the UI meant.
    """
    from .tools import command_lists

    lists = command_lists()
    payload = {
        "role": role.to_dict(),
        "commands": {name: policy.to_dict() for name, policy in lists.items()},
    }
    path = Path(project) / ".trance" / "mcp-role.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf8")
    return path


TOOLS_BRIEF = """

## Your tools

You have no file tools of your own here. Use these, which belong to the harness
running you:

- `read_file(path)` — a big indexed file answers with its outline; ask for the
  symbol you want rather than the file
- `edit_file(path, old, new)` — replaces an exact snippet; costs the size of the
  change, not of the file
- `write_file(path, content)` / `append_file(path, content)` — a whole file, or
  the next section of one
- `list_files(glob)` — what is there

Writes outside your remit are refused as you make them, so there is nothing to
work around: do the part you own and report what you could not.
"""


def _prompt(*, role, task: str, goal: str, placement: str, memory, steering,
            indexed: bool = False, through_trance: bool = False) -> str:
    """Everything the step needs, in one go — there is no second turn to add to."""
    parts = [role.system_prompt.strip()]
    if goal:
        parts.append(f"## What this project is\n{goal}")
    if placement:
        parts.append(f"## Where this step sits\n{placement}")

    notes = memory.for_prompt() if memory is not None else ""
    if notes:
        parts.append(f"## What the team has agreed\n{notes}")

    if role.paths:
        parts.append(
            "## What you may change\n"
            + "\n".join(f"- {glob}" for glob in role.paths)
            + "\n\nAnything you change outside these fails the step. Read whatever "
              "you need; write only here.")
    else:
        parts.append("## What you may change\nNothing. This step is read-only: "
                     "report what you find, change no files.")

    if through_trance:
        parts.append(TOOLS_BRIEF.strip())
    if indexed:
        parts.append(GRAPH_BRIEF.strip())
    parts.append(f"## Your task\n{task}")
    for hint in steering:
        parts.append(f"## From the user, while you work\n{hint}")
    parts.append(OUTCOME_CONTRACT)
    return "\n\n".join(p for p in parts if p).strip()


def _touched(project: Path, before: str) -> list[str]:
    """What actually changed on disk, from git rather than from the report.

    A model saying what it did is a claim; the diff is the fact, and this
    backend gives no other way to know.
    """
    if not before:
        return []
    changed = list(vcs.changed_between(project, before))
    changed += [p for p in vcs.dirty(project) if p not in changed]
    return sorted(changed)


def _may_write(role, path: str) -> bool:
    return any(fnmatch(path, glob) for glob in (role.paths or []))


def _usage(raw: dict) -> dict:
    prompt = (int(raw.get("input_tokens") or 0)
              + int(raw.get("cache_creation_input_tokens") or 0)
              + int(raw.get("cache_read_input_tokens") or 0))
    return {"prompt_tokens": prompt, "completion_tokens": int(raw.get("output_tokens") or 0)}
