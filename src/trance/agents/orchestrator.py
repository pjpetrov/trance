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
                                "gates": {
                                    "type": "array",
                                    "description": (
                                        "Checks run after the work, in order. Each must pass "
                                        "before the next runs; the first failure sends the work "
                                        "back to this step's own role and then the whole chain "
                                        "runs again. Only these agents can check work — no "
                                        "other value is valid. Omit for planning steps."
                                    ),
                                    "items": {"type": "string", "enum": verifiers},
                                },
                                "max_attempts": {
                                    "type": "integer",
                                    "description": "How many times the work may be redone (1-4).",
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
          "result can only guess at a verdict. A step's checks run in order and any "
          "failure sends the work back to that step's own role, so "
          "'backend, checked by tester then reviewer' already means "
          "develop → test → fix → test → review → fix → …"
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
    for call in response.tool_calls:
        if call.name == "propose_flow":
            proposal = _normalize(call.arguments, roles)

    text = response.text.strip()
    if proposal and not text:
        text = proposal.get("summary", "Here is the plan I propose.")
    return {"text": text, "proposal": proposal}


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

        proposed = raw.get("gates") or []
        if isinstance(proposed, str):
            proposed = [proposed]
        if not proposed and raw.get("verify_with"):
            proposed = [raw["verify_with"]]

        gates, seen = [], set()
        for name in proposed:
            if name in verifiers and name not in seen:
                gates.append(name)
                seen.add(name)
            elif name not in verifiers:
                dropped.append(f"{name} (on the {role} step)")

        attempts = raw.get("max_attempts")
        steps.append({
            "role": role, "task": task, "gates": gates,
            "verify_with": None,
            "max_attempts": max(1, min(4, int(attempts) if attempts else 2)),
        })

    team = [n for n in (arguments.get("team") or []) if n in known and n != "orchestrator"]
    for step in steps:
        for name in [step["role"], *step["gates"]]:
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
