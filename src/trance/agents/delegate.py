"""Hand a whole step to Claude Code, instead of driving it round by round.

The ordinary path is trance's loop: one model call, one tool call, repeat. That
is what makes remits enforceable and context measurable — and it is precisely
what the `claude` CLI will not do, because it throttles programmatic use and an
agent loop is five or more calls per step.

So for that backend the step is delegated: one call, Claude Code's own loop, its
own tools, its own context management. Measured at three internal turns for a
one-line edit, in a single call the throttle allows.

It is still a trance step, but the control is post-hoc, and that is the deal
this backend offers. It runs with its own tools — measured with the MCP bridge
that used to sit here, the connector was never the cost, and its live remit
enforcement bought little that the git diff at the end does not prove better: a
write outside the remit fails the step, whoever made it and however. A role
that may write nothing gets read-only tools (Read, Grep, Glob) and cannot touch
the project at all, which is the shape a reviewer or a checker wants. A role
that writes gets edit permission plus a Bash allowlist shaped from the same
command list every other agent answers to.

The person choosing this model is told the same thing where they choose it:
control here is checked from the diff afterwards, not enforced as it happens.

A verifier's turn carries the step's diff up front, which is what makes review
on this backend affordable: with the change already in the prompt there is
nothing to go exploring for, and the measured thirty-turn wander becomes a
couple of turns of reading.

Everything else stays trance's: which step runs, in what order, with which
prompt, judged by the same OUTCOME line and the same checks.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from .. import vcs
from ..events import summarize_messages
from ..providers.base import BackendError, Cancelled, clear_inflight, register_inflight
from ..providers.claudecode_client import _is_abort, _why

#: How long a delegated step may take. Not the model's timeout: that one bounds
#: a single call — a question and an answer — while this bounds an entire step,
#: with its own loop of reads, edits and test runs inside it. Ten minutes is
#: generous for the first and routinely short for the second, which is how a
#: tester running a suite ended with "did not finish within 600s" and nothing to
#: show for it.
DELEGATED_TIMEOUT_S = 3600.0

#: Internal turns above which the step gets flagged to the user. Measured: a
#: one-line edit is ~3 turns, an ordinary step 10-30, and the runs that ate a
#: subscription limit in an afternoon were 64-83 turns each, re-reading the
#: whole conversation every turn.
TURNS_WORTH_A_LOOK = 40

#: The read-only slice of Claude Code's own tools: enough to judge work, no way
#: to change it. What a verifier or any write-nothing role runs with.
READONLY_TOOLS = "Read,Grep,Glob"

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

    prompt = _prompt(role=role, task=task, goal=goal, placement=placement,
                     memory=memory, steering=steering or [])
    command = [binary, "-p", prompt, "--output-format", "json"]
    if role.paths:
        # A writer: its own edit tools, and Bash shaped from the same command
        # list every other agent answers to. The remit is checked from the
        # diff when it finishes — that is the deal, and the person who chose
        # this model was told so where they chose it.
        command += ["--permission-mode", "acceptEdits",
                    # Kept hermetic: a step is about this project, and a web
                    # search inside one is spend nobody asked for.
                    "--disallowedTools", "WebSearch", "WebFetch"]
        programs = _programs(role)
        if programs:
            command += ["--allowedTools",
                        *(f"Bash({prog}:*)" for prog in programs)]
    else:
        # No remit means no writes, enforced the only way this backend can
        # enforce anything: by not handing over the tools that write.
        command += ["--tools", READONLY_TOOLS]
    if config.model:
        command += ["--model", config.model]

    bus.emit("delegated", session_id, agent=role.name, step_id=step_id, payload={
        "model": config.model or "claude code default",
        # The stats page marks who is answering right now. A delegated run
        # emits nothing else until it finishes, so this event *is* the "in
        # flight" signal, and it has to name the preset the ledger keys by.
        "preset": config.preset,
        # The prompt, up front. The run takes anywhere from minutes to an
        # hour and used to record what was sent only when it came back —
        # so for the whole of it there was nothing to inspect.
        "messages": [{"role": "user", "content": prompt}],
        "command": [c for c in command if c != prompt],
        "summary": summarize_messages([{"role": "user", "content": prompt}]),
        "message": (
            f"{role.name} is running this step inside Claude Code: one call, its "
            + ("own tools — writes are judged against the remit from the diff when "
               "it finishes." if role.paths else
               "read-only tools (Read, Grep, Glob) — it cannot change the project.")),
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
    register_inflight(session_id, handle)
    # An explicitly longer model timeout is respected; a shorter one is not
    # applied to something it was never measuring.
    limit = max(float(config.timeout_s or 0), DELEGATED_TIMEOUT_S)
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as exc:
        handle.abort()
        # It ran for an hour: say what it managed, rather than only that it
        # stopped. The files are on disk behind the checkpoint.
        touched = _touched(project, before)
        raise BackendError(
            f"Claude Code did not finish this step within {limit / 60:.0f} minutes. "
            f"It changed {len(touched) or 'no'} file(s)"
            + (f" ({', '.join(touched[:4])})" if touched else "")
            + ". The work is on disk behind this step's checkpoint; re-run the step "
            + "to carry on, or split it into smaller ones.") from exc
    finally:
        clear_inflight(session_id, handle)

    handle.finished = True
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
    touched = _touched(project, before)
    outside = [p for p in touched if not _may_write(role, p)]

    turns = int(body.get("num_turns") or 1)
    if turns > TURNS_WORTH_A_LOOK:
        # Not an error — the work may be fine — but 80 internal turns re-read
        # this conversation 80 times, and each *retry* of the step pays all of
        # it again. Measured live: three retries of one delegated step cost
        # 6.5M input tokens between them. The person watching should see that
        # while it is one step, not on the statistics page a day later.
        bus.emit("warning", session_id, agent=role.name, step_id=step_id, payload={
            "message": (
                f"This delegated step took {turns} internal turns and "
                f"{usage.get('prompt_tokens', 0):,} input tokens "
                f"({usage.get('cache_read_tokens', 0):,} of them cache re-reads). "
                f"A step this size is cheaper split into smaller ones — and every "
                f"retry of it pays the whole amount again."),
        })

    bus.emit("model_call", session_id, agent=role.name, step_id=step_id, payload={
        "round": 1, "model": config.model, "preset": config.preset,
        "delegated": True, "turns": body.get("num_turns"),
        "messages": [{"role": "user", "content": prompt}],
        "response_text": text, "reasoning": "", "tool_calls": [],
        "finish_reason": body.get("stop_reason") or "stop", "usage": usage,
        "summary": summarize_messages([{"role": "user", "content": prompt}]),
    })
    for path in touched:
        bus.emit("file_written", session_id, agent=role.name, step_id=step_id,
                 payload={"path": path, "delegated": True})

    return {"text": text, "files_written": touched, "remit_violations": outside,
            "usage": usage, "turns": turns}


# ------------------------------------------------------------------ helpers


def _binary() -> str:
    from ..providers.claudecode_client import available

    found = available()
    if not found:
        raise BackendError("the `claude` CLI is not on trance's PATH")
    return found


def _programs(role) -> list[str]:
    """The programs this agent may run, from the same lists as everywhere else."""
    from .tools import command_list

    if role.commands:
        return sorted({str(c).strip() for c in role.commands if str(c).strip()})
    return sorted(command_list(role.command_list).allowed)


WRITER_BRIEF = """

## Your tools, and the rules they cannot enforce

You are working with your own tools in the project directory. Two rules are
checked from the git diff when you finish, because nothing can enforce them
while you work:

- Write only inside your remit (listed above). A write outside it fails the
  step, whoever made it and however.
- Run only the programs your Bash permissions allow. Do not work around a
  refused command; report what you could not run.

Every turn you take re-sends this whole conversation, so wandering is
expensive: grep for the thing you need rather than reading whole files, make
the change, run the check, and stop.
"""

READONLY_BRIEF = """

## Your tools

You have read-only tools: Read, Grep, Glob. You cannot change files and you
cannot run commands — deliberately. Your job is judgement, and the feedback you
return is the product. Every turn re-sends this whole conversation, so read
what the task hands you before going looking for more.
"""


def _prompt(*, role, task: str, goal: str, placement: str, memory, steering) -> str:
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

    parts.append((WRITER_BRIEF if role.paths else READONLY_BRIEF).strip())
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
    return {"prompt_tokens": prompt,
            "completion_tokens": int(raw.get("output_tokens") or 0),
            # Kept apart because they are not the same money: a cache read is
            # about a tenth of a fresh token, and for a delegated step it is
            # most of the number — the same conversation read back on every
            # internal turn.
            "cache_read_tokens": int(raw.get("cache_read_input_tokens") or 0)}
