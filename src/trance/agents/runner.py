"""Run one agent turn, publishing every model call with its full context.

This is the observability seam. `model_call` events carry the complete message
list that went to the model and the complete response that came back — the UI
shows a summary and expands to the verbatim payload on demand.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..config import ModelConfig
from ..events import EventBus, summarize_messages
from ..providers import Cancelled, client_for
from ..worker.client import salvage_tool_calls
from . import delegate
from .memory import ProjectMemory
from .roles import AgentRole
from .tools import AgentTools, permissions_brief

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"

#: Tool rounds an agent gets in one attempt, when neither it nor its model says
#: otherwise. Enough to read a few things and write a file in pieces — which
#: was the wrong measure of enough: agents that hit their limit had spent it
#: working out where the code was, and the restart bought them nothing because
#: the next attempt started from the same blank context and read it all again.
DEFAULT_TOOL_ROUNDS = 20

#: A working agent ends its reply with this so the step has an outcome of its
#: own. A tester that writes a good test and finds a real bug did its job
#: perfectly and the *step* still failed — those are different things.
OUTCOME_MARKER = "OUTCOME:"
OUTCOME_SUCCESS = "SUCCESS"

#: Words that stand in for SUCCESS / FAILED when a model does not use the exact
#: keyword. Matched against the FIRST word only, never anywhere in the line:
#: "the feature is not complete" contains "complete", and reading that as
#: success is the one mistake this whole mechanism exists to prevent.
_SUCCESS_WORDS = frozenset({
    "SUCCESS", "SUCCEEDED", "SUCCESSFUL", "DONE", "COMPLETE", "COMPLETED",
    "OK", "PASS", "PASSED", "YES",
})
_FAILURE_WORDS = frozenset({
    "FAILED", "FAIL", "FAILURE", "ERROR", "BLOCKED", "INCOMPLETE", "PARTIAL",
    "UNABLE", "NO", "NOT",
})

#: Marker left in place of a tool result we dropped to stay inside the window.
#: It used to end "call the tool again if you still need this", which is an
#: instruction to refill the window that was just emptied.
TRIMMED = ("[dropped to fit the context window. Work from what you have already "
           "learned; only fetch this again if you cannot proceed without it]")

#: The share of an attempt an agent may spend purely on reading. Past this, if
#: it has still written nothing, the lookup tools are withdrawn for the rest of
#: the attempt.
#:
#: Measured on a repair loop that could not finish: with 24 rounds it made 79-94
#: lookups and no edits; raised to 36 it made 102, then 133 — and still no
#: edits, ending "I have read and analyzed every file in the project thoroughly
#: across 36 rounds, but I have not written any fixes yet." A bigger budget did
#: not buy a decision, it funded more reading. Nothing here forces a good edit,
#: but it does stop the attempt being spent entirely on preparing for one.
LOOKUP_SHARE = 0.6

#: Lookups whose answer depends only on their arguments. Repeating one is always
#: waste; repeating run_command or write_file is not.
_CACHEABLE = frozenset({
    "read_file", "get_definition", "get_callers", "get_callees", "search_symbols",
    "list_files", "check_file", "check_files",
})

#: The tools withdrawn when reading is closed. Everything here answers "what is
#: already there"; check_file and check_files stay, because an agent verifying
#: its own write is doing the opposite of stalling.
_LOOKUP_TOOLS = frozenset({
    "read_file", "get_definition", "get_callers", "get_callees", "search_symbols",
    "list_files",
})


def _lookup_key(name: str, arguments: dict, outcome) -> tuple | None:
    """Identity of a lookup, including what it actually returned.

    The returned size is part of the key so that re-reading a file the agent has
    since rewritten gives it the new contents rather than a pointer to the old.
    """
    if name not in _CACHEABLE:
        return None
    detail = outcome.detail or {}
    signature = tuple(sorted((k, str(v)) for k, v in (arguments or {}).items()))
    return (name, signature, detail.get("bytes"), detail.get("last_line"))


#: Starting guess at characters per token. Prose is about 4; code, JSON and
#: minified assets are denser, so the estimate runs low exactly where contexts
#: get big. Calibrated against the server's own count after the first call.
DEFAULT_CHARS_PER_TOKEN = 3.5
MIN_CHARS_PER_TOKEN = 2.0


def _chars(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("content") or ""))
        if message.get("tool_calls"):
            total += len(str(message["tool_calls"]))
    return total


def _tokens(messages: list[dict], chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    return int(_chars(messages) / max(MIN_CHARS_PER_TOKEN, chars_per_token))


#: Placeholder left where a file's contents used to sit in the conversation.
WRITTEN = "[contents omitted — this file was written to disk; read_file it if you need it]"


def shrink_written_files(messages: list[dict], keep_last: int = 1) -> int:
    """Drop the `content` argument of write calls that already succeeded.

    An agent that writes three 10KB files has 30KB of its own output pinned in
    the conversation forever — the assistant messages holding those calls are
    never trimmed, and the bytes are already on disk. This is the most
    recoverable thing in the window and usually the largest.
    """
    targets: list[tuple[dict, str]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:          # OpenAI shape
            if (call.get("function") or {}).get("name") in ("write_file", "append_file"):
                targets.append((call, "openai"))
        content = message.get("content")
        if isinstance(content, list):                          # Anthropic shape
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") in ("write_file", "append_file")):
                    targets.append((block, "anthropic"))

    shrunk = 0
    for call, shape in targets[:max(0, len(targets) - keep_last)]:
        if shape == "openai":
            raw = (call.get("function") or {}).get("arguments") or ""
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not args.get("content") or args["content"] == WRITTEN:
                continue
            args["content"] = WRITTEN
            call["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
        else:
            args = call.get("input") or {}
            if not args.get("content") or args["content"] == WRITTEN:
                continue
            args["content"] = WRITTEN
        shrunk += 1
    return shrunk


#: Asked once, at the point the agent believes it is done. The examples matter:
#: without them models answer "I remembered to write the tests", which is a
#: progress report and costs every later agent for nothing.
def _remember_prompt(turn, memory) -> str:
    """Ask about the work actually done, not in the abstract.

    "Is there anything to remember?" is a yes/no question, and a model's default
    answer to a yes/no question at the end of a long turn is no — one declined
    on the grounds that "the existing memory notes already cover" it, with the
    memory empty. Naming the files it just wrote, and how many notes there
    really are, removes both escapes.
    """
    written = ", ".join(dict.fromkeys(turn.files_written))
    stored = len(memory.notes()) if memory is not None else 0
    return (
        (f"You wrote: {written}.\n\n" if written else "")
        + f"Project memory currently holds {stored} note(s) — that is all of it. "
        f"Do not assume a fact is already recorded.\n\n"
        "For what you just did, is there a fact the next agent must match? A route "
        "and its payload shape, a port, a script name, where files go, a version or "
        "format they have to agree with. Call remember once per fact, one specific "
        "sentence each.\n\n"
        "If there is genuinely nothing the others need, say so in one line and do "
        "not call it. Either way, end your reply with your OUTCOME line again."
    )


#: The playbook is instructions, not literature; past this it is literature.
PLAYBOOK_MAX_CHARS = 4000


def _playbook(project: Path) -> str:
    try:
        text = (Path(project) / "PLAYBOOK.md").read_text(encoding="utf8",
                                                         errors="replace").strip()
    except OSError:
        return ""
    if len(text) > PLAYBOOK_MAX_CHARS:
        text = text[:PLAYBOOK_MAX_CHARS] + "\n… (clipped — the file goes on)"
    return text


def _with_user_images(text: str, images: list[str], project: Path, config) -> str | list:
    """The user's screenshots, in the shape this model can take.

    A model that can see gets them as image blocks after the prompt — the
    same wire shape the orchestrator and the vision checks already use. One
    that cannot is told they exist and where they are, because an agent
    answering about a screenshot it never received is worse than one that
    says it cannot see. Capped at three: each rides every attempt.
    """
    from ..vision import VISION_KINDS, image_block

    kept = [name for name in images if name][:3]
    if not kept:
        return text
    kind = getattr(config, "kind", "") or "llamacpp"
    if kind not in VISION_KINDS:
        return (text + f"\n\n[{len(kept)} screenshot(s) travel with this task "
                f"({', '.join(kept)}) — attached by the user, or taken in the "
                f"browser by the agent before you — but this model cannot be "
                f"shown images. Ask the visual tester about them, or reason "
                f"from the task text.]")
    blocks: list[dict] = [{"type": "text", "text": (
        text + f"\n\n{len(kept)} screenshot(s) travel with this task — attached "
               f"by the user, or taken in the browser by the agent before you; "
               f"they follow. What they show is part of the task.")}]
    shown = 0
    for name in kept:
        try:
            png = (Path(project) / ".trance" / "shots" / name).read_bytes()
        except OSError:
            continue
        blocks.append(image_block(png, "anthropic" if kind == "anthropic" else "openai"))
        shown += 1
    return blocks if shown else text


def _should_ask_to_remember(turn, role, already_asked: bool) -> bool:
    """Only nudge an agent that did work, has the tool, and wrote nothing."""
    if already_asked or turn.notes_written:
        return False
    if not {"files", "commands", "graph"} & set(role.toolsets):
        return False
    return bool(turn.files_written or turn.tool_calls)


#: Per-entry cap on what the transcript keeps. The full text is in the event
#: trace either way; this copy exists only to be handed to another agent.
MAX_RECORDED_CHARS = 6000


def _record(turn, name: str, arguments: dict, text: str, ok: bool, detail: dict | None) -> None:
    turn.transcript.append({
        "tool": name,
        "arguments": arguments,
        "ok": ok,
        "text": (text or "")[:MAX_RECORDED_CHARS],
        "detail": detail or {},
    })


def context_usage(messages: list[dict], response, config: ModelConfig) -> dict:
    """How full the window is for this call — the number the UI puts on screen.

    The server's own `prompt_tokens` is the truth when it reports one; the
    char/4 estimate is a fallback so the gauge still moves for endpoints that
    return no usage. Which one it is travels with the number, because a user
    deciding whether to raise `context_window` should know if it is a guess.
    """
    # Called before the call as well as after, to size the gauge while the
    # model is still thinking. With no response there is no reported count,
    # so the estimate stands in and says so.
    reported = int(((getattr(response, "usage", None) or {}) if response else {})
                   .get("prompt_tokens") or 0)
    tokens = reported or _tokens(messages)
    window = max(1, config.context_window)
    return {
        "tokens": tokens,
        "window": config.context_window,
        # What the runner actually trims against: the window less the room the
        # model needs to answer. Passing it means the gauge and the trimmer
        # cannot tell different stories.
        "budget": config.input_budget,
        "reserved": config.max_tokens,
        "percent": round(100 * tokens / window, 1),
        "estimated": not reported,
    }


def fit_context(messages: list[dict], budget: int,
                chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> tuple[list[dict], int]:
    """Shrink the prompt until it fits, giving up the cheapest things first.

    Order matters. A file the agent already wrote is on disk and can be read
    back exactly, so its contents go first. Tool results go next — re-callable,
    but a second call costs a round. The system prompt and the original task are
    never dropped; losing those makes the agent forget what it is doing, which
    is worse than losing a stale file listing.
    """
    dropped = 0
    if _tokens(messages, chars_per_token) <= budget:
        return messages, dropped

    dropped += shrink_written_files(messages)
    for message in messages:
        if _tokens(messages, chars_per_token) <= budget:
            break
        if message.get("role") == "tool" and message.get("content") != TRIMMED:
            message["content"] = TRIMMED
            dropped += 1
    return messages, dropped


@dataclass
class AgentTurn:
    text: str
    files_written: list[str] = field(default_factory=list)
    remit_violations: list[str] = field(default_factory=list)
    tool_calls: int = 0
    usage: dict = field(default_factory=dict)
    rounds: int = 0
    stop_reason: str = "stop"
    salvaged_calls: int = 0
    truncated_calls: int = 0
    #: Notes this agent added to the shared memory.
    notes_written: int = 0
    #: Repeat lookups answered with a pointer instead of the content again.
    deduped_lookups: int = 0
    #: Window usage on the last call, for the step to keep after the run moves on.
    context: dict = field(default_factory=dict)
    #: Hints the user sent while this turn was running.
    steering_received: int = 0
    model_event_ids: list[str] = field(default_factory=list)
    #: Everything this agent did, in order — the raw material for the handoff
    #: to a fixer. Never fed back to this agent; the conversation already holds
    #: it, and it is trimmed there.
    transcript: list[dict] = field(default_factory=list)
    #: Screenshots this turn saved (paths under .trance/shots), in order. A
    #: loop hands the last of them to the next block, so a fixer sees what
    #: the tester saw.
    shots: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str | None:
        """PASS or FAIL, read from the word after the marker — not from the line.

        "PASS anywhere in the line" turned

            VERDICT: FAIL — the suite ran: 57 pass, 2 fail

        into a pass, because the count of passing tests is in the reason. A
        tester's whole job is that sentence, and it was being read backwards.
        """
        found = self._verdict_line()
        if found is None:
            return None
        body = found[len("VERDICT:"):].strip()
        first = re.split(r"[^A-Za-z]+", body.upper(), maxsplit=1)[0] if body else ""
        if first == VERDICT_PASS:
            return VERDICT_PASS
        return VERDICT_FAIL          # FAIL, INCOMPLETE, or anything that is not a pass

    @property
    def verdict_reason(self) -> str:
        """Whatever the verdict line said after the word."""
        found = self._verdict_line()
        if not found:
            return ""
        body = found[len("VERDICT:"):].strip()
        return re.sub(r"^[A-Za-z]+\W*", "", body).strip()

    def _verdict_line(self) -> str | None:
        for line in reversed(self.text.splitlines()):
            stripped = line.strip().lstrip("*# ").strip()
            if stripped.upper().startswith("VERDICT:"):
                return stripped
        return None

    @property
    def outcome(self) -> tuple[str, str]:
        """(outcome, detail) reported by the agent for its own step.

        An agent that never states one is NOT taken as successful. A step that
        described a real defect and then stopped mid-thought was being marked
        done purely because nothing said otherwise, which is the worst way to
        be wrong here.

        A failing VERDICT beats a claimed SUCCESS. A tester ends with both — the
        verdict on the code, and the outcome of its own turn — and it reads its
        own turn as a success because it did test the thing. But a block whose
        tester says FAIL is not finished, and treating it as one leaves the loop
        on a green light with red tests.
        """
        verdict = self.verdict
        for line in reversed(self.text.splitlines()):
            stripped = line.strip()
            if not stripped.upper().startswith(OUTCOME_MARKER):
                continue
            body = stripped[len(OUTCOME_MARKER):].strip()
            if not body:
                return "UNCLEAR", "the agent wrote an outcome line with nothing after it"

            first = re.split(r"[^A-Za-z]+", body.upper(), maxsplit=1)[0]
            if first in _SUCCESS_WORDS:
                if verdict == VERDICT_FAIL:
                    return "FAILED", (
                        "the agent reported SUCCESS for its own turn but its VERDICT "
                        "was FAIL — the work it checked is not right yet: "
                        + (self.verdict_reason or "no reason given"))
                return OUTCOME_SUCCESS, ""
            if first in _FAILURE_WORDS:
                return "FAILED", body or "no reason given"
            # Neither. Reading the prose either way is guessing: "Verified the
            # file is complete and correct" was being filed as a failure with
            # itself as the reason, which is how a step that went fine burned
            # its loop limit.
            return "UNCLEAR", body
        # A verdict is an outcome for an agent that was asked for one: a tester
        # that says PASS did its job and the block is done; FAIL is a block that
        # is not. Asking again for the same thing in other words wastes a round
        # and, often enough, gets nothing.
        if verdict == VERDICT_PASS:
            return OUTCOME_SUCCESS, ""
        if verdict == VERDICT_FAIL:
            return "FAILED", self.verdict_reason or "the verdict was FAIL"
        return "UNSTATED", ("the agent finished without stating an outcome, so there is "
                            "nothing to say the work succeeded")

    @property
    def reported_outcome(self) -> bool:
        return any(l.strip().upper().startswith(OUTCOME_MARKER)
                   for l in self.text.splitlines())

    @property
    def needs_outcome(self) -> bool:
        """No line at all, or one whose verdict cannot be read."""
        return self.outcome[0] in ("UNSTATED", "UNCLEAR")


def run_agent(**kwargs) -> AgentTurn:
    """Run one agent turn and release whatever it opened.

    The browser toolset launches a real Chrome and a static server, and both
    outlive any single tool call by design — a game has to still be running
    between the keypress that starts it and the screenshot that judges it. That
    makes the *turn* their owner, and something has to close them on every way
    out of it, including the ones that raise.
    """
    opened: list = []
    turn = None
    try:
        turn = _run_agent(_opened=opened, **kwargs)
        return turn
    finally:
        for closable in opened:
            # Harvested here rather than at each of the turn's returns: the
            # toolset knows what it saved, and this is the one place that sees
            # both it and the finished turn on every way out.
            if turn is not None and hasattr(closable, "shots_taken"):
                turn.shots = closable.shots_taken()
            try:
                closable.close()
            except Exception:              # noqa: BLE001 — teardown never fails a step
                pass


def _run_agent(
    *,
    role: AgentRole,
    task: str,
    project: Path,
    config: ModelConfig,
    bus: EventBus,
    session_id: str,
    step_id: str,
    context_bundle: str = "",
    steering: list[str] | None = None,
    history: list[dict] | None = None,
    graph_tools=None,
    max_rounds: int = 0,
    should_stop=None,
    memory=None,
    project_map: str = "",
    goal: str = "",
    requirements: list[str] | None = None,
    placement: str = "",
    approve=None,
    reindex=None,
    steering_inbox=None,
    #: Screenshots the user attached to the request this step came from —
    #: project-relative paths. Shown as image blocks to a model that can see,
    #: said in words to one that cannot; never silently dropped.
    images: list[str] | None = None,
    #: This turn is a check on someone else's work, so its answer is a VERDICT
    #: line. Two things follow: it is not asked to record decisions — it made
    #: none — and a reply without a verdict is worth one short question rather
    #: than an unverified step.
    verdict_required: bool = False,
    _opened: list | None = None,
) -> AgentTurn:
    model_config = config
    # The agent's own budget, or the default. Deliberately not ModelConfig's
    # max_tool_rounds: that belongs to the older worker path and defaults to 8,
    # so reading it here would quietly cut every agent from twelve rounds to
    # eight while looking like a tidy-up.
    max_rounds = max_rounds or getattr(role, "tool_rounds", 0) or DEFAULT_TOOL_ROUNDS

    # A backend that will not answer round by round runs the step itself. One
    # call instead of a dozen, judged the same way — see agents/delegate.py for
    # what that trades away.
    if delegate.delegated(getattr(model_config, "kind", "")):
        if images:
            # One image per internal turn would multiply badly on this
            # backend; it gets the fact and the paths instead, and can read
            # the files itself if its tools allow.
            task = (task + f"\n\nScreenshot(s) travel with this task — attached "
                    f"by the user, or taken in the browser by the agent before "
                    f"you: "
                    + ", ".join(f".trance/shots/{name}" for name in images[:3])
                    + ". What they show is part of the task.")
        handed = delegate.run_delegated(
            role=role, task=task, project=project, config=model_config, bus=bus,
            session_id=session_id, step_id=step_id, memory=memory, goal=goal,
            placement=placement, steering=steering)
        turn = AgentTurn(text=handed["text"], files_written=handed["files_written"],
                         remit_violations=handed["remit_violations"],
                         usage=handed["usage"], rounds=handed["turns"],
                         tool_calls=handed["turns"])
        if handed["remit_violations"]:
            # It wrote outside its remit. trance could not stop it — this
            # backend edits files itself — so the step fails and names them,
            # with the work still on disk and the checkpoint still behind it.
            turn.text += (
                "\n\nOUTCOME: FAILED — wrote outside this agent's remit: "
                + ", ".join(handed["remit_violations"]))
        return turn

    client = client_for(model_config)
    def notify(kind: str, payload: dict) -> None:
        bus.emit(kind, session_id, agent=role.name, step_id=step_id, payload=payload)

    memory = memory if memory is not None else ProjectMemory(project)
    # Screenshots go to the agent's own model. There used to be one global
    # "vision model" setting for this, which was a second thing to configure
    # and a second thing to get wrong: an agent with the browser toolset needs
    # a model that can see, and that is a property of the agent, not of trance.
    tools = AgentTools(project, role, graph_tools, notify=notify, memory=memory,
                       approve=approve, session_id=session_id, step_id=step_id,
                       reindex=reindex, vision_config=model_config)
    if _opened is not None:
        _opened.append(tools)
    specs = tools.specs()

    user_parts = []
    if goal:
        # The goal comes before the task on purpose: an agent reads the task as
        # an instruction and the goal as the thing the instruction is for.
        user_parts.append("## What this project is\n" + goal)
    user_parts.append(f"## Your task — this and nothing else\n{task}")
    if requirements:
        # Not the task: what the whole project is judged against. A coder builds
        # to these, a tester writes tests for them, and a visual tester looks
        # for them on screen — so all three read the same list rather than each
        # inventing its own idea of done.
        user_parts.append(
            "## What the finished project must do\n"
            + "\n".join(f"- {item}" for item in requirements)
            + "\n\nThese are the acceptance criteria for the project as a whole, not "
              "for your step. Do not try to satisfy all of them; do your task in a way "
              "that does not make any of them harder to reach.")
    if placement:
        user_parts.append("## Where your step sits\n" + placement)
    # What the team already settled comes before what this agent may do: the
    # decisions constrain the work, the toolset only constrains how.
    notes = memory.for_prompt()
    user_parts.append(
        "## Project memory (decisions the team has already made — follow them)\n"
        + (notes if notes else
           # Stated rather than omitted. An agent that cannot tell "empty" from
           # "not shown" will assume a fact is already recorded and skip writing
           # it — one was seen declining to record anything because "the
           # existing memory notes already cover" what it had just decided.
           "(empty — nothing has been written down yet. Anything you decide "
           "that others must match is yours to record.)"))
    # Derived from the tool layer, so what the agent is told always matches what
    # the tool layer will actually allow.
    user_parts.append("## Your permissions (enforced by the system)\n" + permissions_brief(role))
    if project_map:
        # An agent that cannot see what is indexed has no reason to guess a
        # symbol name, so it falls back to reading whole files. Showing the map
        # is what makes the graph tools usable at all.
        user_parts.append(
            "## Project map (already indexed — fetch any of these with "
            "get_definition, no need to read the whole file)\n" + project_map)
    if "browser" in role.toolsets:
        # The team's own instructions for driving the app. The tester has no
        # file tools — judges must not rewrite the evidence — so the playbook
        # arrives in the prompt or not at all. Measured without it: the first
        # minutes of every visual step re-discovered, per run, what the dev
        # knew the moment it built the lobby.
        playbook = _playbook(project)
        if playbook:
            user_parts.append(
                "## How to drive this app (written by the team — follow it, and "
                "report where it is wrong)\n" + playbook)
    if context_bundle:
        user_parts.append("## Curated context\n" + context_bundle)
    if history:
        user_parts.append("## What happened earlier in this project\n" + _render_history(history))
    for note in steering or []:
        user_parts.append(f"## Steering from the user (follow this)\n{note}")

    first_user: str | list = "\n\n".join(user_parts)
    if images:
        first_user = _with_user_images(first_user, images, project, model_config)

    messages = [
        {"role": "system", "content": role.system_prompt},
        {"role": "user", "content": first_user},
    ]

    turn = AgentTurn(text="")
    totals = {"input_tokens": 0, "output_tokens": 0}
    asked_to_remember = False
    #: The answer given just before the memory nudge, until the turn ends.
    report = ""
    #: Lookup key -> the tool message holding its result, so a repeat can point
    #: at it instead of duplicating it.
    seen_lookups: dict[tuple, dict] = {}
    # Calibrated from the endpoint's own prompt_tokens after the first call.
    # Trimming against a guess is how a "55k" prompt arrived as 61k and filled
    # the window it was supposed to stay inside.
    chars_per_token = DEFAULT_CHARS_PER_TOKEN
    #: Set once a reply has come back as nothing but reasoning. From then on the
    #: turn asks with thinking off, rather than paying the whole budget to
    #: rediscover the same thing every round.
    stopped_thinking = False
    stopped_reading = False

    for round_n in range(1, max_rounds + 1):
        if should_stop and should_stop():
            turn.stop_reason = "cancelled"
            break

        # A hint typed while the agent is working reaches it on the next round
        # rather than after the step. Noticing a wrong assumption and having to
        # wait for the block to finish is most of the value gone.
        for note in (steering_inbox() if steering_inbox else []):
            turn.steering_received += 1
            messages.append({
                "role": "user",
                "content": ("The user is watching you work and says:\n\n" + note
                            + "\n\nTake this as correcting what you are doing now."),
            })
            bus.emit("steering_delivered", session_id, agent=role.name, step_id=step_id,
                     payload={"note": note, "round": round_n})

        # Reading is closed once most of the attempt is gone and none of it has
        # reached the disk. Said out loud and taken away at the same time: an
        # agent told to stop reading while the tools are still in front of it
        # reads on, and one whose tools vanish without a word retries them.
        if (not stopped_reading and not turn.files_written and role.paths
                and round_n > max(1, int(max_rounds * LOOKUP_SHARE))):
            stopped_reading = True
            specs = [spec for spec in specs
                     if spec["function"]["name"] not in _LOOKUP_TOOLS]
            # An agent that can run something has a way out that reading never
            # gave it: make the code say what it is doing. One that cannot is
            # only told to decide, because telling it to measure would be
            # telling it to use a tool it does not have.
            can_run = any(spec["function"]["name"] == "run_command" for spec in specs)
            messages.append({"role": "user", "content": (
                f"You have used {round_n - 1} of your {max_rounds} tool rounds and "
                f"have not written anything yet. The tools that only look things up "
                f"are withdrawn for the rest of this attempt.\n\n"
                + ("You have read the code and it has not told you where the fault "
                   "is. Stop reading and make it tell you: add a temporary print or "
                   "assertion where you suspect the problem, or write a small "
                   "throwaway script that exercises just that path, and run it with "
                   "run_command. Read what it actually says, then fix what it shows "
                   "you and take the instrumentation out.\n\n"
                   if can_run else "")
                + f"Otherwise make the smallest change that addresses the task with "
                f"what you already know. If that is not enough to fix it properly, "
                f"write the part you are sure of and say what is left — a partial "
                f"fix that names its own gap is worth more to the next agent than "
                f"a complete description of the problem.")})
            bus.emit("reading_closed", session_id, agent=role.name, step_id=step_id,
                     payload={"round": round_n, "of": max_rounds,
                              "withdrawn": sorted(_LOOKUP_TOOLS),
                              "message": (f"{round_n - 1} of {max_rounds} rounds spent "
                                          f"without writing anything — lookup tools "
                                          f"withdrawn for the rest of this attempt.")})

        messages, trimmed = fit_context(messages, model_config.input_budget, chars_per_token)
        if trimmed:
            bus.emit("context_trimmed", session_id, agent=role.name, step_id=step_id,
                     payload={"dropped_tool_results": trimmed,
                              "budget": model_config.input_budget,
                              "context_window": model_config.context_window})

        started = time.time()
        sent_chars = _chars(messages)
        # Said before the call, not after. A local 27B can spend two minutes on
        # one generation and emits nothing while it does, so the console went
        # quiet and there was no way to tell working from stuck. The matching
        # model_call arrives when it answers.
        thinking = _thinking_state(model_config, stopped_thinking)
        bus.emit("model_waiting", session_id, agent=role.name, step_id=step_id, payload={
            "round": round_n,
            "model": model_config.model,
            "preset": model_config.preset,
            "context": turn.context or context_usage(messages, None, model_config),
            "message": (f"waiting for {model_config.preset or model_config.model}"
                        + ("" if thinking.get("thinking", True) else " · thinking off")),
            **thinking,
        })
        progress = _progress_reporter(client, bus, session_id, role=role,
                                      step_id=step_id, round_n=round_n,
                                      config=model_config)
        try:
            response = client.complete(
                messages, tools=specs or None, cancel_token=session_id,
                **({"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
                   if stopped_thinking else {}),
                **({"on_progress": progress} if progress else {}))
            response, overran = _answer_or_retry_without_thinking(
                response, client=client, messages=messages, specs=specs,
                config=model_config, bus=bus, session_id=session_id,
                role=role, step_id=step_id, round_n=round_n,
                already_overran=stopped_thinking, on_progress=progress)
            stopped_thinking = stopped_thinking or overran
        except Cancelled:
            # Stop, mid-generation. Not an error, and not the agent's doing.
            turn.stop_reason = "cancelled"
            break
        elapsed_ms = round((time.time() - started) * 1000, 1)
        turn.context = context_usage(messages, response, model_config)
        reported = int((response.usage or {}).get("prompt_tokens") or 0)
        if reported > 0 and sent_chars > 0:
            # Keep the densest ratio seen: the budget has to hold for the worst
            # message in the window, not the average one.
            chars_per_token = max(MIN_CHARS_PER_TOKEN,
                                  min(chars_per_token, sent_chars / reported))
        totals["input_tokens"] += response.usage.get("prompt_tokens", 0)
        totals["output_tokens"] += response.usage.get("completion_tokens", 0)

        event = bus.emit(
            "model_call",
            session_id,
            agent=role.name,
            step_id=step_id,
            payload={
                "round": round_n,
                "model": model_config.model,
                "preset": model_config.preset,
                "base_url": model_config.base_url,
                "duration_ms": elapsed_ms,
                # Full fidelity — this is the whole point of the inspector.
                "messages": messages,
                "response_text": response.text,
                "reasoning": response.reasoning,
                "tool_calls": [
                    {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                ],
                "finish_reason": response.finish_reason,
                "usage": response.usage,
                # The same gauge the waiting event carried, now with the count
                # the model reported rather than an estimate of it.
                "context": turn.context,
                **_thinking_state(model_config, stopped_thinking),
                "summary": summarize_messages(messages),
            },
        )
        turn.model_event_ids.append(event.id)
        turn.rounds = round_n
        turn.text = response.text or turn.text

        if response.provider_error == "truncated_tool_call":
            # The endpoint rejected a call the model never finished writing.
            # Tell it what happened and let it try a smaller piece; failing the
            # whole step for this loses everything it did beforehand.
            turn.truncated_calls += 1
            bus.emit("tool_call", session_id, agent=role.name, step_id=step_id, payload={
                "name": "(unfinished)", "arguments": {}, "ok": False,
                "result": ("The endpoint rejected the call: its arguments were cut off "
                           f"at the {model_config.max_tokens}-token output limit."),
                "detail": {"kind": "truncated", "limit": model_config.max_tokens,
                           "attempt": turn.truncated_calls},
            })
            messages.append({
                "role": "user",
                "content": (
                    f"Your last tool call was cut off partway through its arguments — it hit "
                    f"the {model_config.max_tokens}-token output limit — so the endpoint "
                    f"rejected it and nothing ran.\n\n"
                    f"Do not send that call again unchanged; it will be cut off in the "
                    f"same place. Split the file: write_file with the first section only, "
                    f"then append_file for each following section. Keep every call's "
                    f"content well under the limit."),
            })
            if turn.truncated_calls >= 3:
                turn.stop_reason = "truncated_tool_calls"
                turn.text = (turn.text or "") + (
                    "\n\nOUTCOME: FAILED — every attempt to write the file was cut off at "
                    f"the {model_config.max_tokens}-token output limit. Raise max_tokens for "
                    "this agent's model, or split the work into more steps.")
                break
            continue

        calls = response.tool_calls
        if not calls and specs:
            # Some models print the call instead of making it. Recover it rather
            # than letting the agent believe work happened that didn't.
            calls = salvage_tool_calls(response.text, {s["function"]["name"] for s in specs})
            if calls:
                turn.salvaged_calls += len(calls)
                bus.emit("tool_calls_salvaged", session_id, agent=role.name, step_id=step_id,
                         payload={"count": len(calls), "model": model_config.model,
                                  "names": [c.name for c in calls]})
                # Rebuilt, because the printed call is not in `tool_calls` where
                # the next turn expects it — but through replay(), so whatever
                # the provider needs back still comes back.
                response.raw_message = response.replay(text=response.text)

        if not calls:
            # It thinks it is finished. Before letting it go, make it decide once
            # whether anything it just did has to reach the next agent — asking
            # after the step is over is too late, and asking every round would
            # train it to say no.
            if not verdict_required and _should_ask_to_remember(
                    turn, role, asked_to_remember):
                asked_to_remember = True
                # The report it just gave, kept. The nudge produces another
                # reply, and taking the last one as the answer threw away the
                # first: a regression check that had written VERDICT: PASS came
                # back "No new facts to record", so 87 green tests were filed as
                # a step nobody could verify.
                report = response.text
                # Not the reply again. Echoing it printed the agent's answer
                # twice in the console, once under the model's name and once
                # under the preset's, which reads as two calls.
                bus.emit("model_call", session_id, agent=role.name, step_id=step_id,
                         payload={"round": round_n, "model": model_config.model,
                                  "preset": model_config.preset,
                                  "asked_to_remember": True, "messages": [],
                                  "response_text": ("(asked whether anything here has "
                                                    "to reach the next agent)"),
                                  "tool_calls": [], "usage": {}, "summary": {}})
                messages.append(response.replay())
                messages.append({"role": "user",
                                 "content": _remember_prompt(turn, memory)})
                continue
            turn.stop_reason = response.finish_reason
            if report and response.text.strip() != report.strip():
                turn.text = f"{report}\n\n{response.text}".strip()
                report = ""
            break

        # A provider that returned no assistant message would otherwise put an
        # empty dict in the conversation — a message with no role, which some
        # endpoints reject and none can read.
        messages.append(response.replay(calls=calls))

        # Cut at the output limit. This was only ever explained when the cut
        # landed inside a tool call's arguments — but a reply cut anywhere is a
        # reply the model believes it finished, and it will write the same
        # oversized thing again next round unless told.
        if response.finish_reason in ("length", "time") and not any(c.malformed for c in calls):
            turn.truncated_calls += 1
            cut_at = (f"the {int(model_config.timeout_s)}-second time limit"
                      if response.finish_reason == "time"
                      else f"the {model_config.max_tokens}-token output limit")
            told = (
                f"Your reply was cut off at {cut_at} — everything after that point "
                f"was lost, including anything you were part-way through writing.\n\n"
                f"Write in pieces from here on:\n"
                f"- `edit_file` to change part of a file — it costs the size of the "
                f"change, not the size of the file\n"
                f"- `replace_symbol` to swap one function whole\n"
                f"- `write_file` for the first section only, then `append_file` for each "
                f"one after it\n\n"
                f"Do not send the same large call again; it will be cut in the same "
                f"place.")
            bus.emit("truncated", session_id, agent=role.name, step_id=step_id, payload={
                "limit": model_config.max_tokens, "attempt": turn.truncated_calls,
                "message": f"The reply hit {cut_at} and was cut. Told to write incrementally.",
            })
            messages.append({"role": "user", "content": told})

        cut_short = []
        for call in calls:
            if call.malformed:
                # Almost always the model ran out of output tokens partway
                # through a large `content` argument. Say so — "missing
                # required argument 'path'" sends it hunting for the wrong bug.
                truncated = response.finish_reason in ("length", "time")
                if truncated:
                    cut_short.append(call)
                    turn.truncated_calls += 1
                    # The same announcement as a reply cut outside a call. This
                    # is the more expensive case, not the lesser one: minutes of
                    # generation, nothing written, and only a failed tool call
                    # in the console to show for it.
                    bus.emit("truncated", session_id, agent=role.name, step_id=step_id,
                             payload={"limit": model_config.max_tokens,
                                      "attempt": turn.truncated_calls, "call": call.name,
                                      "message": (f"{call.name} was cut off at the "
                                                  f"{model_config.max_tokens}-token output "
                                                  f"limit and did not run.")})
                outcome = _malformed_call_outcome(call, truncated, model_config.max_tokens)
                bus.emit("tool_call", session_id, agent=role.name, step_id=step_id, payload={
                    "name": call.name, "arguments": {}, "ok": False,
                    "result": outcome.text, "result_tokens": outcome.tokens,
                    "detail": {"kind": "malformed", "raw": call.raw_arguments[:2000],
                               "truncated": truncated},
                })
                turn.tool_calls += 1
                _record(turn, call.name, {}, outcome.text, False,
                        {"kind": "malformed", "truncated": truncated})
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "name": call.name, "content": outcome.text})
                continue
            outcome = tools.call(call.name, call.arguments)
            turn.tool_calls += 1
            turn.files_written += outcome.files_written
            if outcome.remit_violation:
                turn.remit_violations.append(outcome.remit_violation)
            bus.emit(
                "tool_call",
                session_id,
                agent=role.name,
                step_id=step_id,
                payload={
                    "name": call.name,
                    "arguments": call.arguments,
                    "ok": outcome.ok,
                    "result": outcome.text,
                    "result_tokens": outcome.tokens,
                    "remit_violation": outcome.remit_violation,
                    "detail": outcome.detail,
                },
            )
            if (outcome.detail or {}).get("kind") == "memory" and outcome.detail.get("stored"):
                turn.notes_written += 1

            # The same file read five times in a row is five copies of it in the
            # window. Point at the copy that is already there — unless it was
            # trimmed away, in which case fetching it again is the right move.
            key = _lookup_key(call.name, call.arguments, outcome)
            earlier = seen_lookups.get(key) if key else None
            if earlier is not None and earlier.get("content") != TRIMMED:
                outcome = replace(
                    outcome,
                    text=(f"You already ran this exact lookup earlier in this "
                          f"conversation and the result is still above — reuse it "
                          f"rather than re-reading. ({call.name})"),
                    detail={**(outcome.detail or {}), "deduped": True})
                turn.deduped_lookups += 1
            _record(turn, call.name, call.arguments, outcome.text, outcome.ok, outcome.detail)
            tool_message = {
                "role": "tool", "tool_call_id": call.id, "name": call.name,
                "content": outcome.text,
            }
            messages.append(tool_message)
            if key is not None and earlier is None:
                seen_lookups[key] = tool_message

        if cut_short:
            _drop_unfinished_call(messages, cut_short, model_config.max_tokens)
    else:
        # Out of rounds: force a final answer with tools withheld.
        messages.append({
            "role": "user",
            "content": (
                f"You have used all {max_rounds} of your tool rounds for this attempt. "
                f"No more tools will run. Summarise what you did and what remains, now. "
                f"If the work is unfinished, say so — a step reported as done and left "
                f"half-written costs the next agent more than an honest failure."),
        })
        messages, _ = fit_context(messages, model_config.input_budget, chars_per_token)
        try:
            response, stopped_thinking = _ask_without_tools(
                client, messages, config=model_config, bus=bus, session_id=session_id,
                role=role, step_id=step_id, round_n=max_rounds + 1,
                stopped_thinking=stopped_thinking)
        except Cancelled:
            turn.stop_reason = "cancelled"
            return turn
        totals["input_tokens"] += response.usage.get("prompt_tokens", 0)
        totals["output_tokens"] += response.usage.get("completion_tokens", 0)
        turn.context = context_usage(messages, response, model_config)
        event = bus.emit(
            "model_call", session_id, agent=role.name, step_id=step_id,
            payload={"round": max_rounds + 1, "model": model_config.model,
                     "base_url": model_config.base_url, "messages": messages,
                     "response_text": response.text, "reasoning": response.reasoning,
                     # Why the round happened and how the reply ended are two
                     # different facts, and writing one over the other cost an
                     # afternoon: a reply cut off at the token limit was filed
                     # as "ran out of rounds", so the console showed the reason
                     # we asked and hid the reason there was no answer.
                     "tool_calls": [], "out_of_rounds": True,
                     "finish_reason": response.finish_reason or "max_rounds",
                     "usage": response.usage, "preset": model_config.preset,
                     "context": turn.context,
                     **_thinking_state(model_config, stopped_thinking),
                     "summary": summarize_messages(messages)},
        )
        turn.model_event_ids.append(event.id)
        turn.text = response.text
        turn.stop_reason = "max_rounds"

    # A missing or unreadable OUTCOME line is usually a slip, not a failure.
    # One short question resolves it without spending a whole loop of the block
    # — and without anyone guessing at what the agent meant.
    if turn.stop_reason not in ("cancelled",) and turn.needs_outcome:
        state, detail = turn.outcome
        opening = ("You did not end with an outcome line."
                   if state == "UNSTATED" else
                   f"Your outcome line did not say SUCCESS or FAILED — it said: "
                   f"{detail!r}. That cannot be read as either, and nobody will "
                   f"guess on your behalf.")
        messages.append({
            "role": "user",
            "content": (
                opening + " Reply with exactly one line and nothing else:\n"
                "  OUTCOME: SUCCESS          — the task is done and you believe it works\n"
                "  OUTCOME: FAILED — <what is wrong>\n"
                "Report FAILED if you did not finish, could not run something, or found a "
                "problem — including one you were not asked to look for. If the work was "
                "already correct and needed no changes, that is SUCCESS."),
        })
        messages, _ = fit_context(messages, model_config.input_budget, chars_per_token)
        try:
            follow_up, stopped_thinking = _ask_without_tools(
                client, messages, config=model_config, bus=bus, session_id=session_id,
                role=role, step_id=step_id, round_n=turn.rounds + 1,
                stopped_thinking=stopped_thinking)
        except Cancelled:
            turn.stop_reason = "cancelled"
            return turn
        totals["input_tokens"] += follow_up.usage.get("prompt_tokens", 0)
        totals["output_tokens"] += follow_up.usage.get("completion_tokens", 0)
        turn.context = context_usage(messages, follow_up, model_config)
        bus.emit("model_call", session_id, agent=role.name, step_id=step_id, payload={
            "round": turn.rounds + 1, "model": model_config.model,
            "base_url": model_config.base_url, "messages": messages,
            "response_text": follow_up.text, "reasoning": follow_up.reasoning,
            "tool_calls": [], "finish_reason": follow_up.finish_reason,
            "usage": follow_up.usage, "preset": model_config.preset,
            "context": turn.context,
            **_thinking_state(model_config, stopped_thinking),
            "summary": summarize_messages(messages),
            "asked_for_outcome": True,
        })
        if follow_up.text.strip():
            turn.text = f"{turn.text}\n\n{follow_up.text.strip()}"
        if turn.needs_outcome:
            # Asked twice and still unreadable. Fail closed — a result nobody
            # can read is not evidence of success — but say that is why, rather
            # than presenting the agent's prose as a failure reason.
            #
            # "Never stated" reads as an agent that ignored the instruction, and
            # for a local thinking model that is usually the wrong story: it
            # returned nothing at all, having spent the reply budget. Say which
            # of the two happened, because the fixes are different — one is a
            # prompt, the other is the model's reply budget.
            silent = (follow_up.finish_reason in ("length", "time")
                      and not (follow_up.text or "").strip())
            turn.text += (
                f"\n\nOUTCOME: FAILED — the model used its whole "
                f"{model_config.max_tokens}-token reply budget without producing "
                f"an answer, so this step has no result to read."
                if silent else
                "\n\nOUTCOME: FAILED — the agent was asked twice and never stated "
                "SUCCESS or FAILED, so there is no readable result for this step.")

    # A check whose verdict cannot be read is a step nobody can sign off, and
    # the engine records that as blocked — the same weight as work that failed.
    # One short question is cheaper than a person going to read the transcript.
    if turn.stop_reason not in ("cancelled",) and verdict_required and turn.verdict is None:
        messages.append({
            "role": "user",
            "content": (
                "You did not end with a verdict line. You are checking someone else's "
                "work, so that line is the whole answer. Reply with exactly one line "
                "and nothing else:\n"
                "  VERDICT: PASS\n"
                "  VERDICT: FAIL — <what is wrong>\n"
                "OUTCOME is not it: that is for the agent doing the work. If what you "
                "found was fine, that is PASS."),
        })
        messages, _ = fit_context(messages, model_config.input_budget, chars_per_token)
        try:
            answer, stopped_thinking = _ask_without_tools(
                client, messages, config=model_config, bus=bus, session_id=session_id,
                role=role, step_id=step_id, round_n=turn.rounds + 2,
                stopped_thinking=stopped_thinking)
        except Cancelled:
            turn.stop_reason = "cancelled"
            return turn
        totals["input_tokens"] += answer.usage.get("prompt_tokens", 0)
        totals["output_tokens"] += answer.usage.get("completion_tokens", 0)
        turn.context = context_usage(messages, answer, model_config)
        bus.emit("model_call", session_id, agent=role.name, step_id=step_id, payload={
            "round": turn.rounds + 2, "model": model_config.model,
            "base_url": model_config.base_url, "messages": messages,
            "response_text": answer.text, "reasoning": answer.reasoning,
            "tool_calls": [], "finish_reason": answer.finish_reason,
            "usage": answer.usage, "preset": model_config.preset,
            "context": turn.context,
            **_thinking_state(model_config, stopped_thinking),
            "summary": summarize_messages(messages),
            "asked_for_verdict": True,
        })
        if answer.text.strip():
            turn.text = f"{turn.text}\n\n{answer.text.strip()}"

    turn.usage = totals
    return turn


#: Backends whose chat template takes `enable_thinking`. Only these can be
#: asked to stop, and only these get the retry below.
THINKING_TOGGLE_KINDS = ("llamacpp", "vllm")


def _thinking_state(config, stopped_thinking: bool) -> dict:
    """Whether this call went out with thinking on — for the backends we set it.

    Recorded because it is the first thing you want when a reply comes back
    empty, and reconstructing it from a `thinking_overran` event several rounds
    earlier is guesswork. Absent for the rest: reporting a state we never set
    would be worse than saying nothing.
    """
    if (getattr(config, "kind", "") or "") not in THINKING_TOGGLE_KINDS:
        return {}
    return {"thinking": not stopped_thinking}


def _progress_reporter(client, bus, session_id, *, role, step_id, round_n, config):
    """A callback the streaming client feeds about once a second.

    Each report becomes a transient `model_progress` event — live on the
    socket, never in history or on disk — under ONE stable id per round, so
    the console holds a single line that updates in place while the model
    generates, instead of a scrolling column of frames. The final model_call
    event remains the durable record of the round.

    Returns None for clients that don't stream (fakes, other providers), and
    they are then called exactly as before.
    """
    if not getattr(client, "supports_progress", False):
        return None
    ident = f"ev_live_{step_id or 'turn'}_{round_n}"

    def report(info: dict) -> None:
        phase = str(info.get("phase") or "thinking")
        bus.emit("model_progress", session_id, id=ident, agent=role.name,
                 step_id=step_id, transient=True, payload={
                     **info,
                     "round": round_n,
                     "model": config.model,
                     "preset": config.preset,
                     "message": f"{phase} · {info.get('tokens', 0)} tokens",
                 })

    return report


def _ask_without_tools(client, messages, *, config, bus, session_id, role, step_id,
                       round_n, stopped_thinking):
    """The two asks made with tools withheld — report now, and state your outcome.

    They used to go out raw, and that is how a step could fail for no reason:
    measured on one session, the model spent all 8,000 tokens thinking about its
    final report, returned an empty string twice, and the step was recorded as
    "asked twice and never stated SUCCESS or FAILED" — an agent that answered
    nothing, reported as an agent that would not follow instructions. These are
    the two calls whose answer decides the step, so they are the last place to
    skip the recovery every other round gets.

    Returns (response, stopped_thinking).
    """
    progress = _progress_reporter(client, bus, session_id, role=role,
                                  step_id=step_id, round_n=round_n, config=config)
    response = client.complete(
        messages, tools=None, cancel_token=session_id,
        **({"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
           if stopped_thinking else {}),
        **({"on_progress": progress} if progress else {}))
    response, overran = _answer_or_retry_without_thinking(
        response, client=client, messages=messages, specs=None, config=config,
        bus=bus, session_id=session_id, role=role, step_id=step_id,
        round_n=round_n, already_overran=stopped_thinking, on_progress=progress)
    return response, stopped_thinking or overran


def _answer_or_retry_without_thinking(response, *, client, messages, specs, config,
                                      bus, session_id, role, step_id, round_n,
                                      already_overran=False, on_progress=None):
    """Recover a round the model spent entirely on thinking.

    `max_tokens` caps *generated* tokens, and reasoning is generated tokens — so
    on a thinking model a long think can consume the whole budget and the answer
    never starts. Measured against the local Qwen at a 600-token cap: 1,878
    characters of reasoning and an empty answer, where the same cap with
    thinking off produced 1,879 characters of answer.

    Raising the cap is not the fix. The input budget is the window less the cap,
    so every token added to the reply is a token taken from what the agent can
    read — and it only moves the wall rather than removing it. Asking again
    without thinking spends the same budget on an answer instead, and costs
    nothing on the rounds where this never happens.

    Once it has happened in a turn, the rest of the turn goes out with
    thinking off. Two policies were tried and this one won on evidence.
    Latching after one overrun looked like undergrading a model that thinks
    long productively — so raising the reply budget with thinking kept on
    was tried instead, and it was worse: thinking expands to fill whatever
    room it is given (the model drafts entire files inside the think), and a
    30k-token generation on local hardware runs half an hour, past the call
    timeout — which reads as a hang. A hard cap with this graceful recovery
    IS the control; the latch bounds the waste to one overrun per turn.
    The original spiral is still real too: measured once, thirty replies in
    a row each burned the whole budget on reasoning and answered nothing.

    Since the client started streaming, a generation can also be cut on wall
    clock (`finish_reason: "time"`) — that cut lands here identically: a
    think too long to wait for is the same failure as a think too big to fit,
    and gets the same recovery and the same latch.

    Returns (response, overran) — the flag is what turns thinking off for the
    remainder of the turn.
    """
    if already_overran:
        # Thinking is already off for this turn; nothing to recover.
        return response, True
    thought = (getattr(response, "reasoning", "") or "").strip()
    # "length" is the server's size cut; "time" is ours — the streaming client
    # cuts a generation that outlives its wall-clock budget. Spent entirely on
    # reasoning, both mean the same thing: the think ate the reply.
    if response.finish_reason not in ("length", "time") or (response.text or "").strip() \
            or not thought:
        return response, False
    if response.tool_calls:
        return response, False                 # it got far enough to act
    if (getattr(config, "kind", "") or "") not in THINKING_TOGGLE_KINDS:
        return response, False

    budget = (f"{int(config.timeout_s)}-second time budget"
              if response.finish_reason == "time"
              else f"{config.max_tokens}-token reply budget")
    bus.emit("thinking_overran", session_id, agent=role.name, step_id=step_id, payload={
        "round": round_n, "limit": config.max_tokens,
        "reasoning_chars": len(thought),
        "message": (f"The model used its whole {budget} thinking and never began "
                    f"an answer. Asking again without thinking."),
    })
    retried = client.complete(
        messages, tools=specs or None, cancel_token=session_id,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        **({"on_progress": on_progress} if on_progress else {}))
    # Keep the thinking that was paid for: it is the most useful record of why
    # the round went the way it did, and it is what the console shows.
    if not (retried.reasoning or "").strip():
        retried.reasoning = thought
    return retried, True


def _drop_unfinished_call(messages: list[dict], cut_short: list,
                          max_tokens: int = 0) -> None:
    """Take a half-written tool call back out of the conversation.

    A call cut off at the output limit leaves arguments that are not JSON. Left
    in the history, some endpoints re-parse it on every later request and refuse
    the lot — llama.cpp 500s — so the next three rounds fail in the same second
    without the model generating a thing, and the step dies reporting that
    "every attempt was cut off" when only the first one ever ran.

    So the assistant's turn is rewritten as what it actually amounted to: a
    sentence saying it tried and was cut. Nothing is lost that was ever usable,
    and the conversation can be sent again.
    """
    ids = {call.id for call in cut_short}
    names = ", ".join(sorted({call.name for call in cut_short if call.name}) or ["a tool"])

    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        keep = [c for c in (message.get("tool_calls") or [])
                if c.get("id") not in ids]
        if len(keep) == len(message.get("tool_calls") or []):
            continue                      # not the turn we are looking for
        note = (f"(I started a {names} call and my reply was cut off at the output "
                f"limit before it was finished, so it did not run.)")
        message["content"] = "\n\n".join(p for p in [message.get("content") or "", note] if p)
        if keep:
            message["tool_calls"] = keep
        else:
            message.pop("tool_calls", None)
        break

    # A tool result with no call to belong to is rejected by strict endpoints.
    messages[:] = [m for m in messages
                   if not (m.get("role") == "tool" and m.get("tool_call_id") in ids)]

    # And then say what went wrong, in a message the model will actually get.
    #
    # The instruction — do not retry the same call, write the file in sections
    # — used to travel as the tool result for the call that was cut, and the
    # line above deletes exactly those. So the one thing that would have
    # stopped it making the same mistake was the one thing thrown away: it
    # appeared in the console, was read by the person watching, and never
    # reached the model.
    #
    # A user turn rather than an assistant one, because the assistant turn is
    # already the last message. Two in a row are refused outright — llama.cpp
    # answers "Cannot have 2 or more assistant messages at the end of the
    # list" with a 400, which reads as the model being unreachable and sends
    # the step to its backup for a fault that has nothing to do with the model.
    messages.append({"role": "user", "content": (
        f"Your {names} call was cut off at the "
        + (f"{max_tokens}-token " if max_tokens else "")
        + "output limit partway through its arguments, so it could not be parsed "
          "and nothing ran.\n\n"
          "Do not retry the same call — it will be cut off again. Write the file "
          "in smaller pieces: one file per call, and split a large file across "
          "calls by writing the first section with write_file and appending the "
          "rest with append_file. If you are changing an existing file, edit_file "
          "or replace_symbol costs the size of the edit rather than the size of "
          "the file.")})


def _malformed_call_outcome(call, truncated: bool, max_tokens: int):
    from .tools import ToolOutcome

    if truncated:
        text = (
            f"Your {call.name} call was cut off: the response hit the {max_tokens}-token "
            f"output limit partway through its arguments, so they could not be parsed. "
            f"Nothing was executed.\n\n"
            f"Do not retry the same call — it will be cut off again. Write the file in "
            f"smaller pieces (one file per call, and split a very large file across calls "
            f"by writing it in sections), or shorten the content."
        )
    else:
        text = (
            f"Your {call.name} call had arguments that were not valid JSON, so they could "
            f"not be parsed and nothing was executed. Send the arguments again as a single "
            f"valid JSON object."
        )
    return ToolOutcome(text, ok=False)


def _render_history(history: list[dict]) -> str:
    lines = []
    for item in history[-8:]:
        files = ", ".join(item.get("files", [])) or "no files"
        lines.append(f"- {item['role']}: {item['summary']} ({files})")
    return "\n".join(lines)
