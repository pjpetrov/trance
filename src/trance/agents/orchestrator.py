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

PROPOSE_FLOW_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_flow",
        "description": (
            "Propose the agent team and the ordered work plan. Call this once you understand "
            "the project well enough to name concrete tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One paragraph: what is being built."},
                "team": {
                    "type": "array",
                    "description": "Which roles this project needs.",
                    "items": {"type": "string", "enum": list(BUILTIN_ROLES)},
                },
                "steps": {
                    "type": "array",
                    "description": "Ordered work. Put the backend before the frontend that calls it.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": list(BUILTIN_ROLES)},
                            "task": {
                                "type": "string",
                                "description": "One concrete, verifiable piece of work, naming the files it touches.",
                            },
                            "verify_with": {
                                "type": "string",
                                "description": "Role that checks this step, usually 'tester'. Omit for planning steps.",
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
) -> dict:
    """One orchestrator turn. Returns {'text', 'proposal'|None}."""
    role = BUILTIN_ROLES["orchestrator"]
    existing = _describe_project(project_dir)
    system = role.system_prompt + (
        f"\n\nAvailable roles: {', '.join(f'{r.name} ({r.description})' for r in BUILTIN_ROLES.values())}"
        f"\n\nProject directory: {project_dir}\n{existing}"
    )
    full = [{"role": "system", "content": system}] + messages

    client = client_for(config)
    response = client.complete(full, tools=[PROPOSE_FLOW_TOOL])

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
            proposal = _normalize(call.arguments)

    text = response.text.strip()
    if proposal and not text:
        text = proposal.get("summary", "Here is the plan I propose.")
    return {"text": text, "proposal": proposal}


def _normalize(arguments: dict) -> dict:
    """Coerce a model proposal into something the flow editor can render."""
    steps = []
    for raw in arguments.get("steps") or []:
        role = raw.get("role")
        task = (raw.get("task") or "").strip()
        if role not in BUILTIN_ROLES or not task:
            continue
        verify = raw.get("verify_with")
        steps.append({
            "role": role,
            "task": task,
            "verify_with": verify if verify in BUILTIN_ROLES else None,
            "max_attempts": 2,
        })
    team = [name for name in (arguments.get("team") or []) if name in BUILTIN_ROLES]
    # Anything a step needs must be on the team, whatever the model listed.
    for step in steps:
        for name in (step["role"], step.get("verify_with")):
            if name and name not in team:
                team.append(name)
    return {"summary": arguments.get("summary", ""), "team": team, "steps": steps}


def _describe_project(project_dir: Path) -> str:
    path = Path(project_dir)
    if not path.exists():
        return "The directory does not exist yet — this is a brand new project."
    files = [p for p in path.rglob("*") if p.is_file() and ".git" not in p.parts][:40]
    if not files:
        return "The directory is empty — this is a brand new project."
    listing = "\n".join(f"- {p.relative_to(path)}" for p in files)
    return f"Existing files:\n{listing}"
