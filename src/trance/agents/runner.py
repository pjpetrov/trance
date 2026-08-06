"""Run one agent turn, publishing every model call with its full context.

This is the observability seam. `model_call` events carry the complete message
list that went to the model and the complete response that came back — the UI
shows a summary and expands to the verbatim payload on demand.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ModelConfig
from ..events import EventBus, summarize_messages
from ..providers import client_for
from ..worker.client import salvage_tool_calls
from .memory import ProjectMemory
from .roles import AgentRole
from .tools import AgentTools, permissions_brief

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"

#: A working agent ends its reply with this so the step has an outcome of its
#: own. A tester that writes a good test and finds a real bug did its job
#: perfectly and the *step* still failed — those are different things.
OUTCOME_MARKER = "OUTCOME:"
OUTCOME_SUCCESS = "SUCCESS"

#: Marker left in place of a tool result we dropped to stay inside the window.
TRIMMED = "[trimmed to fit the context window — call the tool again if you still need this]"


def _tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("content") or "")) // 4
        if message.get("tool_calls"):
            total += len(str(message["tool_calls"])) // 4
    return total


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
    reported = int((response.usage or {}).get("prompt_tokens") or 0)
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


def fit_context(messages: list[dict], budget: int) -> tuple[list[dict], int]:
    """Drop the oldest tool results until the prompt fits.

    Without this the loop grows without bound: every round appends an assistant
    message plus one result per tool call, and a handful of file reads will
    exceed any window. The system prompt and the original task are never
    dropped — losing those makes the agent forget what it is doing, which is a
    worse failure than losing a stale file listing.
    """
    dropped = 0
    if _tokens(messages) <= budget:
        return messages, dropped
    for message in messages:
        if _tokens(messages) <= budget:
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
    model_event_ids: list[str] = field(default_factory=list)
    #: Everything this agent did, in order — the raw material for the handoff
    #: to a fixer. Never fed back to this agent; the conversation already holds
    #: it, and it is trimmed there.
    transcript: list[dict] = field(default_factory=list)

    @property
    def verdict(self) -> str | None:
        for line in reversed(self.text.splitlines()):
            stripped = line.strip().upper()
            if stripped.startswith("VERDICT:"):
                return VERDICT_PASS if VERDICT_PASS in stripped else VERDICT_FAIL
        return None

    @property
    def outcome(self) -> tuple[str, str]:
        """(outcome, detail) reported by the agent for its own step.

        An agent that never states one is NOT taken as successful. A step that
        described a real defect and then stopped mid-thought was being marked
        done purely because nothing said otherwise, which is the worst way to
        be wrong here.
        """
        for line in reversed(self.text.splitlines()):
            stripped = line.strip()
            if not stripped.upper().startswith(OUTCOME_MARKER):
                continue
            body = stripped[len(OUTCOME_MARKER):].strip()
            if body.upper().startswith(OUTCOME_SUCCESS):
                return OUTCOME_SUCCESS, ""
            return "FAILED", body or "no reason given"
        return "UNSTATED", ("the agent finished without stating an outcome, so there is "
                            "nothing to say the work succeeded")

    @property
    def reported_outcome(self) -> bool:
        return any(l.strip().upper().startswith(OUTCOME_MARKER)
                   for l in self.text.splitlines())


def run_agent(
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
    max_rounds: int = 12,
    should_stop=None,
    memory=None,
    project_map: str = "",
) -> AgentTurn:
    model_config = config
    client = client_for(model_config)
    def notify(kind: str, payload: dict) -> None:
        bus.emit(kind, session_id, agent=role.name, step_id=step_id, payload=payload)

    memory = memory if memory is not None else ProjectMemory(project)
    tools = AgentTools(project, role, graph_tools, notify=notify, memory=memory)
    specs = tools.specs()

    user_parts = [f"## Task\n{task}"]
    # Derived from the tool layer, so what the agent is told always matches what
    # the tool layer will actually allow.
    user_parts.append("## Your permissions (enforced by the system)\n" + permissions_brief(role))
    notes = memory.for_prompt()
    if notes:
        user_parts.append(
            "## Project memory (decisions the team has already made — follow them)\n"
            + notes)
    if project_map:
        # An agent that cannot see what is indexed has no reason to guess a
        # symbol name, so it falls back to reading whole files. Showing the map
        # is what makes the graph tools usable at all.
        user_parts.append(
            "## Project map (already indexed — fetch any of these with "
            "get_definition, no need to read the whole file)\n" + project_map)
    if context_bundle:
        user_parts.append("## Curated context\n" + context_bundle)
    if history:
        user_parts.append("## What happened earlier in this project\n" + _render_history(history))
    for note in steering or []:
        user_parts.append(f"## Steering from the user (follow this)\n{note}")

    messages = [
        {"role": "system", "content": role.system_prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]

    turn = AgentTurn(text="")
    totals = {"input_tokens": 0, "output_tokens": 0}

    for round_n in range(1, max_rounds + 1):
        if should_stop and should_stop():
            turn.stop_reason = "cancelled"
            break

        messages, trimmed = fit_context(messages, model_config.input_budget)
        if trimmed:
            bus.emit("context_trimmed", session_id, agent=role.name, step_id=step_id,
                     payload={"dropped_tool_results": trimmed,
                              "budget": model_config.input_budget,
                              "context_window": model_config.context_window})

        started = time.time()
        response = client.complete(messages, tools=specs or None)
        elapsed_ms = round((time.time() - started) * 1000, 1)
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
                "summary": summarize_messages(messages),
                "context": context_usage(messages, response, model_config),
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
                response.raw_message = {"role": "assistant", "content": response.text}

        if not calls:
            turn.stop_reason = response.finish_reason
            break

        messages.append(response.raw_message)
        for call in calls:
            if call.malformed:
                # Almost always the model ran out of output tokens partway
                # through a large `content` argument. Say so — "missing
                # required argument 'path'" sends it hunting for the wrong bug.
                truncated = response.finish_reason == "length"
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
            _record(turn, call.name, call.arguments, outcome.text, outcome.ok, outcome.detail)
            messages.append({
                "role": "tool", "tool_call_id": call.id, "name": call.name, "content": outcome.text,
            })
    else:
        # Out of rounds: force a final answer with tools withheld.
        messages.append({
            "role": "user",
            "content": "You have used your tool budget. Summarize what you did and what remains, now.",
        })
        messages, _ = fit_context(messages, model_config.input_budget)
        response = client.complete(messages, tools=None)
        totals["input_tokens"] += response.usage.get("prompt_tokens", 0)
        totals["output_tokens"] += response.usage.get("completion_tokens", 0)
        event = bus.emit(
            "model_call", session_id, agent=role.name, step_id=step_id,
            payload={"round": max_rounds + 1, "model": model_config.model,
                     "base_url": model_config.base_url, "messages": messages,
                     "response_text": response.text, "reasoning": response.reasoning,
                     "tool_calls": [], "finish_reason": "max_rounds",
                     "usage": response.usage, "summary": summarize_messages(messages)},
        )
        turn.model_event_ids.append(event.id)
        turn.text = response.text
        turn.stop_reason = "max_rounds"

    # A missing OUTCOME line is usually a slip, not a failure. One short
    # question resolves it without spending a whole loop of the block.
    if turn.stop_reason not in ("cancelled",) and not turn.reported_outcome:
        messages.append({
            "role": "user",
            "content": (
                "You did not end with an outcome line. Reply with exactly one line and "
                "nothing else:\n"
                "  OUTCOME: SUCCESS          — the task is done and you believe it works\n"
                "  OUTCOME: FAILED — <what is wrong>\n"
                "Report FAILED if you did not finish, could not run something, or found a "
                "problem — including one you were not asked to look for."),
        })
        messages, _ = fit_context(messages, model_config.input_budget)
        follow_up = client.complete(messages, tools=None)
        totals["input_tokens"] += follow_up.usage.get("prompt_tokens", 0)
        totals["output_tokens"] += follow_up.usage.get("completion_tokens", 0)
        bus.emit("model_call", session_id, agent=role.name, step_id=step_id, payload={
            "round": turn.rounds + 1, "model": model_config.model,
            "base_url": model_config.base_url, "messages": messages,
            "response_text": follow_up.text, "reasoning": follow_up.reasoning,
            "tool_calls": [], "finish_reason": follow_up.finish_reason,
            "usage": follow_up.usage, "summary": summarize_messages(messages),
            "asked_for_outcome": True,
        })
        if follow_up.text.strip():
            turn.text = f"{turn.text}\n\n{follow_up.text.strip()}"

    turn.usage = totals
    return turn


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
