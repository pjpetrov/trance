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
from .memory import ProjectMemory
from .roles import BUILTIN_ROLES

#: Fibonacci-ish, because the gaps are the point: the difference between 1 and
#: 2 is real, between 8 and 9 is noise. Anchored to concrete work so the numbers
#: mean the same thing across runs and across models.
POINTS = (1, 2, 3, 5, 8, 13)

POINTS_SCALE = (
    "How big this step is, on this scale — estimate honestly, not optimistically:\n"
    "1 = one small edit to one file (change a constant, add a field).\n"
    "2 = one focused change to one file.\n"
    "3 = one file written or reworked end to end.\n"
    "5 = two or three files that have to agree with each other.\n"
    "8 = a whole feature across several files, or work you cannot fully "
    "picture yet.\n"
    "13 = too big to describe as one task; it needs breaking up."
)

#: Splitting is the orchestrator estimating its own work again, so it can loop.
#: Two passes is enough to get an 8 down to 2s and 3s; past that it starts
#: inventing filler steps to satisfy the number.
MAX_SPLIT_ROUNDS = 2


def split_step_tool(roles: list, threshold: int) -> dict:
    """Schema for breaking one oversized step into smaller ones."""
    workers = [r.name for r in roles if r.name != "orchestrator"]
    verifiers = [r.name for r in roles if r.verifier]
    return {
        "type": "function",
        "function": {
            "name": "split_step",
            "description": (
                f"Break one step into smaller steps, each worth {threshold} points or "
                f"less. Keep the same total scope — do not drop work, do not add work, "
                f"and do not turn one step into ten trivial ones."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": ("The replacement steps, in the order they must "
                                        "run. Each stands on its own and is worth doing."),
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "enum": workers},
                                "task": {
                                    "type": "string",
                                    "description": ("One concrete piece of work, naming "
                                                    "the files it touches."),
                                },
                                "check": {"type": "string", "enum": verifiers},
                                "on_fail": {"type": "string", "enum": workers},
                                "max_loops": {"type": "integer"},
                                "points": {"type": "integer", "description": POINTS_SCALE,
                                           "enum": list(POINTS)},
                            },
                            "required": ["role", "task", "points"],
                        },
                    },
                },
                "required": ["steps"],
            },
        },
    }


def split_oversized(proposal: dict, *, roles: list, config: ModelConfig, bus: EventBus,
                    session_id: str, threshold: int, project_dir: Path | None = None) -> dict:
    """Replace every step over `threshold` with the smaller steps it becomes.

    A step nobody can hold in their head is where agents drift: the model
    tackles the part it understood and reports success on the whole thing. The
    estimate is the trigger, but the split is the point — asking for a number
    only changes anything if something acts on it.
    """
    if threshold <= 0:
        return proposal

    steps = list(proposal.get("steps") or [])
    split_log: list[dict] = []

    for _ in range(MAX_SPLIT_ROUNDS):
        oversized = [s for s in steps if (s.get("points") or 0) > threshold]
        if not oversized:
            break
        rebuilt: list[dict] = []
        for step in steps:
            if (step.get("points") or 0) <= threshold:
                rebuilt.append(step)
                continue
            pieces = _ask_for_split(step, roles=roles, config=config, bus=bus,
                                    session_id=session_id, threshold=threshold,
                                    project_dir=project_dir)
            if len(pieces) < 2:
                # A refusal to split is information: the step may genuinely be
                # atomic. Keep it, flagged, rather than forcing a fake break.
                rebuilt.append(step)
                continue
            split_log.append({"task": step["task"], "points": step.get("points"),
                              "into": [p["task"] for p in pieces]})
            rebuilt.extend(pieces)
        steps = rebuilt

    proposal["steps"] = steps
    if split_log:
        proposal["split"] = split_log
        bus.emit("steps_split", session_id, agent="orchestrator",
                 payload={"threshold": threshold, "split": split_log,
                          "steps": len(steps)})
    return proposal


def _ask_for_split(step: dict, *, roles: list, config: ModelConfig, bus: EventBus,
                   session_id: str, threshold: int, project_dir: Path | None) -> list[dict]:
    role = next((r for r in roles if r.name == "orchestrator"), BUILTIN_ROLES["orchestrator"])
    prompt = (
        f"This step came out at {step.get('points')} points, over the limit of "
        f"{threshold}. Break it into smaller steps by calling split_step.\n\n"
        f"Agent: {step['role']}\n"
        f"Task: {step['task']}\n\n"
        f"Each piece must be worth doing on its own and leave the project in a state "
        f"the next step can build on — split by deliverable, not by 'part 1 / part 2'. "
        f"If it genuinely cannot be broken up, call split_step with the single "
        f"original step unchanged."
    )
    messages = [{"role": "system", "content": role.system_prompt},
                {"role": "user", "content": prompt}]
    response = client_for(config).complete(messages,
                                           tools=[split_step_tool(roles, threshold)])
    bus.emit("model_call", session_id, agent="orchestrator", payload={
        "round": 1, "model": config.model, "base_url": config.base_url,
        "splitting": step["task"], "messages": messages,
        "response_text": response.text, "reasoning": response.reasoning,
        "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in response.tool_calls],
        "finish_reason": response.finish_reason, "usage": response.usage,
        "summary": summarize_messages(messages),
    })

    for call in response.tool_calls:
        if call.name != "split_step" or call.malformed:
            continue
        pieces = _normalize({"steps": call.arguments.get("steps") or [],
                             "team": []}, roles)["steps"]
        # Inherit what the split forgot: a piece with no check is not a reason
        # to lose the check the original had.
        for piece in pieces:
            piece.setdefault("check", step.get("check"))
            if not piece.get("check"):
                piece["check"] = step.get("check")
            if not piece.get("on_fail") and piece["check"]:
                piece["on_fail"] = step.get("on_fail")
        return pieces
    return []


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
                                "max_loops": {
                                    "type": "integer",
                                    "description": ("How many times this agent may try "
                                                    "before the run is halted (1-4)."),
                                },
                                "points": {
                                    "type": "integer",
                                    "description": POINTS_SCALE,
                                    "enum": list(POINTS),
                                },
                            },
                            "required": ["role", "task", "points"],
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
        # The remit, not just the description. A step assigned to an agent that
        # cannot write the files it names is refused at the tool boundary and
        # fails the run — and nothing in a one-line description says that
        # backend cannot create package.json.
        "\n\nAgents you can assign work to, and what each may write:\n"
        + "\n".join(_describe_agent(r) for r in workers)
        + "\n\nA write outside an agent's remit is REFUSED by the system, so a step "
          "whose files no agent owns cannot succeed no matter how many times it runs. "
          "Assign each step to the agent that owns the files it touches, and split a "
          "task that spans two remits into two steps."
        + "\n\nOnly these agents can CHECK another agent's work:\n"
        + ("\n".join(f"- {r.name}: {r.description}" for r in verifiers) or "- (none)")
        + "\n\nNever name any other agent as a check — an agent that cannot inspect a "
          "result can only guess at a verdict.\n\n"
          "Each step may have ONE check. If it passes, the flow moves on. If it does "
          "not, the same agent tries again, bounded by max_loops, and exhausting that "
          "halts the run — so set a check on work that must be right.\n\n"
          "A step is one agent trying something. When the work needs two agents "
          "handing back and forth — a tester that finds a bug for a developer to fix, "
          "then tests again — that is a LOOP, not a step, and loops are configured "
          "outside this conversation. Propose the plain steps; the user wires a loop in "
          "where they want one."
        f"\n\nProject directory: {project_dir}\n{_describe_project(project_dir)}"
    )
    # The orchestrator plans against the same facts the team works from. Without
    # this it can propose a step that contradicts a decision already made and
    # already being built on.
    notes = ProjectMemory(project_dir).for_prompt()
    if notes:
        system += (
            "\n\n## Project memory — what the team has already decided\n" + notes
            + "\n\nPlan around these; they are already built on. If one has to change, "
              "make that an explicit step rather than quietly planning against it."
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

        # Older proposals (and older saved flows) may still carry one.
        fixer = raw.get("on_fail")
        if fixer and (fixer not in known or fixer == "orchestrator" or not check):
            fixer = None

        loops = raw.get("max_loops") or raw.get("max_attempts")
        steps.append({
            "role": role, "task": task, "check": check, "on_fail": fixer,
            "max_loops": max(1, min(4, int(loops) if loops else 2)),
            "points": _points(raw.get("points")),
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


def _describe_agent(role) -> str:
    where = ", ".join(role.paths) if role.paths else "nothing — it cannot write files"
    return f"- {role.name}: {role.description}\n    may write: {where}"


def _points(raw) -> int:
    """Snap an estimate to the scale. Off-scale numbers mean nothing to us."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return min(POINTS, key=lambda p: (abs(p - value), p)) if value > 0 else 0


def _describe_project(project_dir: Path) -> str:
    path = Path(project_dir)
    if not path.exists():
        return "The directory does not exist yet — this is a brand new project."
    files = [p for p in path.rglob("*") if p.is_file() and ".git" not in p.parts][:40]
    if not files:
        return "The directory is empty — this is a brand new project."
    listing = "\n".join(f"- {p.relative_to(path)}" for p in files)
    return f"Existing files:\n{listing}"
