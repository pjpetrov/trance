"""The worker agent: curated bundle in, unified diff out.

Everything expensive happens here, so this is the only component that needs a
capable model. It runs a standard tool loop over the graph tools in tools.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ModelConfig
from ..model import ContextBundle
from .client import ChatClient, ChatResponse
from .tools import ContextTools, specs

SYSTEM_PROMPT = """You are a precise coding agent working on an existing codebase.

You have been given a CURATED CONTEXT BUNDLE: the specific functions and classes \
that a call-graph analysis determined are relevant to the task. This is not the \
whole repository — it is deliberately minimal.

Some symbols appear as signatures only (their bodies were elided because they are \
further from the entry point). Any call target the analysis could not resolve is \
listed under "Unresolved references".

If you need code that is not in the bundle, CALL A TOOL — get_definition, \
get_callers, get_callees, or search_symbols. Do not guess at the contents of code \
you have not seen, and do not invent function signatures.

If you cannot complete the task even with the tools, reply with a line starting \
with NEED_CONTEXT: followed by a comma-separated list of the symbols you need.

When you are ready, produce your answer as:
1. A one-paragraph explanation of the change.
2. A unified diff in a ```diff code block, with correct file paths relative to \
the repository root, and enough surrounding context lines to apply cleanly.

Only modify what the task requires."""

NEED_CONTEXT_RE = re.compile(r"^NEED_CONTEXT:\s*(.+)$", re.MULTILINE)
DIFF_BLOCK_RE = re.compile(r"```(?:diff|patch)\n(.*?)```", re.DOTALL)


@dataclass
class ToolInvocation:
    name: str
    arguments: dict
    hit: bool
    result_tokens: int
    result_summary: str
    symbols: list[str] = field(default_factory=list)


@dataclass
class WorkerResult:
    text: str
    diff: str | None
    requested_context: list[str]
    tool_calls: list[ToolInvocation]
    usage: dict[str, int]
    rounds: int
    stop_reason: str
    reasoning: str = ""

    @property
    def needs_more_context(self) -> bool:
        return bool(self.requested_context)


def run(
    bundle: ContextBundle,
    db,
    repo: Path,
    config: ModelConfig,
    on_tool_call=None,
    on_completion=None,
) -> WorkerResult:
    """Run the tool loop to completion.

    `on_tool_call` / `on_completion` are trace hooks — the orchestrator passes
    callbacks that emit events, so this module never imports the trace layer.
    """
    client = ChatClient(config)
    tools = ContextTools(db, repo)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": bundle.render()},
    ]

    invocations: list[ToolInvocation] = []
    totals = {"input_tokens": 0, "output_tokens": 0}
    response: ChatResponse | None = None
    rounds = 0

    for rounds in range(1, config.max_tool_rounds + 1):
        response = client.complete(messages, tools=specs())
        totals["input_tokens"] += response.usage.get("prompt_tokens", 0)
        totals["output_tokens"] += response.usage.get("completion_tokens", 0)
        if on_completion:
            on_completion(response, rounds)

        if not response.tool_calls:
            break

        # Echo the assistant's tool_calls back verbatim; servers validate that
        # every tool result matches a call id from the same message.
        messages.append(response.raw_message)
        for call in response.tool_calls:
            result = tools.call(call.name, call.arguments)
            invocation = ToolInvocation(
                name=call.name,
                arguments=call.arguments,
                hit=result.hit,
                result_tokens=result.tokens,
                result_summary=result.text[:200],
                symbols=result.symbols,
            )
            invocations.append(invocation)
            if on_tool_call:
                on_tool_call(invocation)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result.text}
            )
    else:
        # Loop exhausted. Rather than returning nothing, force one final turn
        # with tools withheld so the model has to answer from what it has.
        messages.append({
            "role": "user",
            "content": (
                "You have used your tool budget. Answer now using only the code you "
                "have already seen. If something remains genuinely unknown, say so "
                "explicitly instead of guessing, and start a line with NEED_CONTEXT: "
                "listing the symbols you still need."
            ),
        })
        response = client.complete(messages, tools=None)
        totals["input_tokens"] += response.usage.get("prompt_tokens", 0)
        totals["output_tokens"] += response.usage.get("completion_tokens", 0)
        if on_completion:
            on_completion(response, rounds + 1)
        text = response.text
        diff_match = DIFF_BLOCK_RE.search(text)
        return WorkerResult(
            text=text,
            diff=diff_match.group(1) if diff_match else None,
            requested_context=[
                part.strip()
                for match in NEED_CONTEXT_RE.findall(text)
                for part in match.split(",")
                if part.strip()
            ],
            tool_calls=invocations,
            usage=totals,
            rounds=rounds,
            stop_reason="max_tool_rounds",
            reasoning=response.reasoning,
        )

    text = response.text if response else ""
    requested = []
    for match in NEED_CONTEXT_RE.findall(text):
        requested += [part.strip() for part in match.split(",") if part.strip()]

    diff_match = DIFF_BLOCK_RE.search(text)
    return WorkerResult(
        text=text,
        diff=diff_match.group(1) if diff_match else None,
        requested_context=requested,
        tool_calls=invocations,
        usage=totals,
        rounds=rounds,
        stop_reason=response.finish_reason if response else "error",
        reasoning=response.reasoning if response else "",
    )
