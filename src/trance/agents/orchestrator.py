"""The orchestrator conversation.

The user describes a project in plain language; the orchestrator asks a couple
of focused questions and then proposes a team and a work order via a tool call.
The proposal is data, not prose — it lands in the UI's flow editor where the
user can rearrange it before anything runs.
"""

from __future__ import annotations

from pathlib import Path

from ..config import ModelConfig
from ..events import EventBus, summarize_messages
from ..providers import client_for
from .roles import BUILTIN_ROLES

def propose_flow_tool(roles: list) -> dict:
    """Build the proposal schema from the live agent library.

    Built dynamically, not from a hardcoded list, for two reasons: custom agent
    types must be proposable, and `gates` must be constrained to agents that can
    actually verify. A free-text field here is how a flow ended up "verified by
    orchestrator" — an agent with no tools at all.
    """
    workers = [r.name for r in roles if r.name != "orchestrator"]
    verifiers = [r.name for r in roles if r.verifier]
    return {
        "type": "function",
        "function": {
            "name": "propose_flow",
            "description": (
                "Propose the agent team and the ordered work plan. Call this once you "
                "understand the project well enough to name concrete tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string",
                                "description": "One paragraph: what is being built."},
                    "team": {
                        "type": "array",
                        "description": "Which agents this project needs.",
                        "items": {"type": "string", "enum": workers},
                    },
                    "steps": {
                        "type": "array",
                        "description": ("Ordered work. Put the backend before the frontend "
                                        "that calls it."),
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "enum": workers},
                                "task": {
                                    "type": "string",
                                    "description": ("One concrete, verifiable piece of work, "
                                                    "naming the files it touches."),
                                },
                                "check": {
                                    "type": "string",
                                    "description": (
                                        "Optional reality check run after the work. Passing "
                                        "lets the flow move to the next step. Only these "
                                        "agents can check work — no other value is valid."
                                    ),
                                    "enum": verifiers,
                                },
                                "on_fail": {
                                    "type": "string",
                                    "description": (
                                        "Optional agent that tries to fix a failed check "
                                        "before this step runs again. Omit to let this "
                                        "step's own role have another go."
                                    ),
                                    "enum": workers,
                                },
                                "max_loops": {
                                    "type": "integer",
                                    "description": ("How many times this block may loop "
                                                    "before the run is halted (1-4)."),
                                },
                            },
                            "required": ["role", "task"],
                        },
                    },
                },
                "required": ["summary", "team", "steps"],
            },
        },
    }


def chat(
    *,
    messages: list[dict],
    project_dir: Path,
    config: ModelConfig,
    bus: EventBus,
    session_id: str,
    roles: list | None = None,
) -> dict:
    """One orchestrator turn. Returns {'text', 'proposal'|None}."""
    roles = list(roles or BUILTIN_ROLES.values())
    role = next((r for r in roles if r.name == "orchestrator"), BUILTIN_ROLES["orchestrator"])
    verifiers = [r for r in roles if r.verifier]
    workers = [r for r in roles if r.name != "orchestrator"]

    system = role.system_prompt + (
        "\n\nAgents you can assign work to:\n"
        + "\n".join(f"- {r.name}: {r.description}" for r in workers)
        + "\n\nOnly these agents can CHECK another agent's work:\n"
        + ("\n".join(f"- {r.name}: {r.description}" for r in verifiers) or "- (none)")
        + "\n\nNever name any other agent as a check — an agent that cannot inspect a "
          "result can only guess at a verdict.\n\n"
          "Each step may have ONE check. If it passes, the flow moves on. If it fails, "
          "the step's `on_fail` agent tries to fix the problem and then the step runs "
          "again — that is the loop, bounded by max_loops. Exhausting the loop halts "
          "the run, so set a check on work that must be right."
        f"\n\nProject directory: {project_dir}\n{_describe_project(project_dir)}"
    )
    full = [{"role": "system", "content": system}] + messages

    client = client_for(config)
    response = client.complete(full, tools=[propose_flow_tool(roles)])

    bus.emit(
        "model_call", session_id, agent="orchestrator",
        payload={
            "round": 1, "model": config.model, "base_url": config.base_url,
            "messages": full, "response_text": response.text, "reasoning": response.reasoning,
            "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in response.tool_calls],
            "finish_reason": response.finish_reason, "usage": response.usage,
            "summary": summarize_messages(full),
        },
    )

    proposal = None
    truncated_call = False
    for call in response.tool_calls:
        if call.name != "propose_flow":
            continue
        if call.malformed:
            truncated_call = True
            continue
        proposal = _normalize(call.arguments, roles)

    text = response.text.strip()
    cut_off = response.finish_reason == "length"

    # A reply cut off mid-sentence — or worse, mid-reasoning — should say so.
    # Showing the fragment reads as an answer the orchestrator meant to give.
    if cut_off or truncated_call:
        limit = config.max_tokens
        notice = (
            f"⚠️ My reply was cut off: it hit the {limit}-token output limit for the "
            f"orchestrator model"
            + (", so the plan it was building never arrived." if truncated_call
               else ".")
            + f"\n\nRaise **max_tokens** for the orchestrator in ⚙ settings (2048 is "
              f"tight for a model that thinks before answering; try 8192), or ask for a "
              f"shorter plan — for example 'just list the steps'."
        )
        text = f"{notice}\n\n---\n\n{text}" if text else notice

    if proposal and not text:
        text = proposal.get("summary", "Here is the plan I propose.")
    if not text:
        text = ("I did not produce a reply. This usually means the model returned only "
                "reasoning and no answer — try asking again, or raise max_tokens.")

    return {"text": text, "proposal": proposal, "truncated": cut_off or truncated_call}


def _normalize(arguments: dict, roles: list) -> dict:
    """Coerce a model proposal into something the flow editor can render.

    The schema constrains `gates` to verifiers, but a model can still emit a
    value outside its own enum — so drop anything that cannot actually check.
    """
    known = {r.name: r for r in roles}
    verifiers = {r.name for r in roles if r.verifier}
    steps, dropped = [], []

    for raw in arguments.get("steps") or []:
        role = raw.get("role")
        task = (raw.get("task") or "").strip()
        if role not in known or role == "orchestrator" or not task:
            continue

        proposed = raw.get("check") or raw.get("verify_with")
        if isinstance(proposed, list):
            proposed = proposed[0] if proposed else None
        check = proposed if proposed in verifiers else None
        if proposed and not check:
            dropped.append(f"{proposed} (as the check on the {role} step)")

        fixer = raw.get("on_fail")
        if fixer and (fixer not in known or fixer == "orchestrator" or not check):
            dropped.append(f"{fixer} (as the fixer on the {role} step)")
            fixer = None

        loops = raw.get("max_loops") or raw.get("max_attempts")
        steps.append({
            "role": role, "task": task, "check": check, "on_fail": fixer,
            "max_loops": max(1, min(4, int(loops) if loops else 2)),
        })

    team = [n for n in (arguments.get("team") or []) if n in known and n != "orchestrator"]
    for step in steps:
        for name in [step["role"], step.get("check"), step.get("on_fail")]:
            if not name:
                continue
            if name not in team:
                team.append(name)

    return {
        "summary": arguments.get("summary", ""), "team": team, "steps": steps,
        "dropped_checks": dropped,
    }


def _describe_project(project_dir: Path) -> str:
    path = Path(project_dir)
    if not path.exists():
        return "The directory does not exist yet — this is a brand new project."
    files = [p for p in path.rglob("*") if p.is_file() and ".git" not in p.parts][:40]
    if not files:
        return "The directory is empty — this is a brand new project."
    listing = "\n".join(f"- {p.relative_to(path)}" for p in files)
    return f"Existing files:\n{listing}"
