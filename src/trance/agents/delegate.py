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
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

from .. import vcs
from ..providers.base import BackendError
from ..providers.claudecode_client import DEFAULT_TIMEOUT_S, _is_abort, _why

#: What it may use. Enough to read, edit and run the project's own checks;
#: nothing that reaches outside the directory it was pointed at.
TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "TodoWrite"]

#: The index, handed over as an MCP server — this is what MCP is for, and it is
#: the one thing a delegated agent would otherwise lose. Without it Claude Code
#: greps and reads whole files, which is the habit this whole project exists to
#: break.
GRAPH_TOOLS = ["mcp__trance__get_definition", "mcp__trance__search_symbols",
               "mcp__trance__get_callers", "mcp__trance__get_callees"]

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
    prompt = _prompt(role=role, task=task, goal=goal, placement=placement,
                     memory=memory, steering=steering or [], indexed=indexed)
    allowed = TOOLS + (GRAPH_TOOLS if indexed else [])
    command = [binary, "-p", prompt,
               "--output-format", "json",
               "--permission-mode", "acceptEdits",
               "--allowedTools", *allowed]
    if indexed:
        command += ["--mcp-config", json.dumps(_mcp_config(project)),
                    "--strict-mcp-config"]
    if config.model:
        command += ["--model", config.model]

    bus.emit("delegated", session_id, agent=role.name, step_id=step_id, payload={
        "model": config.model or "claude code default",
        "message": (f"{role.name} is running this step inside Claude Code — one call, "
                    f"its own tools. What it changes is checked against the remit "
                    f"afterwards."),
    })

    try:
        done = subprocess.run(command, cwd=str(project), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL,
                              timeout=config.timeout_s or DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise BackendError(
            f"Claude Code did not finish the step within "
            f"{config.timeout_s or DEFAULT_TIMEOUT_S:.0f}s") from exc
    except OSError as exc:
        raise BackendError(f"could not run the claude CLI: {exc}") from exc

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


def _indexed(project: Path) -> bool:
    """Is there a graph to offer? A fresh project has none yet."""
    from ..indexer.service import default_db_path

    return default_db_path(project).exists()


def _mcp_config(project: Path) -> dict:
    """The graph server, described the way Claude Code expects to be told.

    Run with this interpreter rather than whatever `python` resolves to: trance
    may be in a virtualenv the CLI's environment knows nothing about.
    """
    return {"mcpServers": {"trance": {
        "command": sys.executable,
        "args": ["-m", "trance.mcp_server", str(project)],
    }}}


def _prompt(*, role, task: str, goal: str, placement: str, memory, steering,
            indexed: bool = False) -> str:
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
