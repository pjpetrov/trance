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
    model_event_ids: list[str] = field(default_factory=list)

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

        Returns ("SUCCESS", "") when the agent says the work is done, or
        ("FAILED", reason) when it reports anything else. An agent that never
        says is taken at its word — the fact check exists to catch that.
        """
        for line in reversed(self.text.splitlines()):
            stripped = line.strip()
            if not stripped.upper().startswith(OUTCOME_MARKER):
                continue
            body = stripped[len(OUTCOME_MARKER):].strip()
            if body.upper().startswith(OUTCOME_SUCCESS):
                return OUTCOME_SUCCESS, ""
            return "FAILED", body or "no reason given"
        return OUTCOME_SUCCESS, ""

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
) -> AgentTurn:
    model_config = config
    client = client_for(model_config)
    def notify(kind: str, payload: dict) -> None:
        bus.emit(kind, session_id, agent=role.name, step_id=step_id, payload=payload)

    tools = AgentTools(project, role, graph_tools, notify=notify)
    specs = tools.specs()

    user_parts = [f"## Task\n{task}"]
    # Derived from the tool layer, so what the agent is told always matches what
    # the tool layer will actually allow.
    user_parts.append("## Your permissions (enforced by the system)\n" + permissions_brief(role))
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
            },
        )
        turn.model_event_ids.append(event.id)
        turn.rounds = round_n
        turn.text = response.text or turn.text

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
