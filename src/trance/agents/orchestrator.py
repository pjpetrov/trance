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
                                "on_fail": {"type": "string", "enum": workers},
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
        "round": 1, "model": config.model, "preset": config.preset,
        "base_url": config.base_url,
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
        # Each piece is checked the way the original was: the pieces are the
        # same work, by the same agent, and the chain came from that agent.
        for piece in pieces:
            piece["check"] = step.get("check")
            piece["checks"] = list(step.get("checks") or [])
            piece["checks_seeded"] = bool(step.get("checks_seeded"))
            if not piece.get("on_fail"):
                piece["on_fail"] = step.get("on_fail")
        return pieces
    return []


def propose_flow_tool(roles: list, loop_names: list | None = None) -> dict:
    """Build the proposal schema from the live agent library.

    Built dynamically, not from a hardcoded list, for two reasons: custom agent
    types must be proposable, and `gates` must be constrained to agents that can
    actually verify. A free-text field here is how a flow ended up "verified by
    orchestrator" — an agent with no tools at all.
    """
    workers = [r.name for r in roles if r.name != "orchestrator"]
    verifiers = [r.name for r in roles if r.verifier]
    loop_names = list(loop_names or [])
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
                    "requirements": {
                        "type": "array",
                        "description": (
                            "What the finished thing must do, as statements that can be "
                            "checked one at a time. These are read by the tester, who "
                            "writes tests for them, and by the visual tester, who looks "
                            "for them on screen — so write each one so that looking at "
                            "the running app, or at a test result, settles it. "
                            "'Exactly four ghosts are visible once the game starts' can "
                            "be checked; 'the game feels good' cannot. Six to twelve of "
                            "these is usually right."
                        ),
                        "items": {"type": "string"},
                    },
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
                                "loop": {
                                    "type": "string",
                                    "description": (
                                        "Run a named loop for this step instead of a single "
                                        "agent. Use one when the work needs two agents "
                                        "handing back and forth until it is right — a "
                                        "tester finding bugs for a developer to fix. Leave "
                                        "`role` out when you set this."
                                    ),
                                    "enum": loop_names,
                                } if loop_names else {"type": "string", "enum": []},
                                "task": {
                                    "type": "string",
                                    "description": ("One concrete, verifiable piece of work, "
                                                    "naming the files it touches."),
                                },
                                "points": {
                                    "type": "integer",
                                    "description": POINTS_SCALE,
                                    "enum": list(POINTS),
                                },
                            },
                            "required": ["task", "points"],
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
    loops=None,
    settings=None,
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
        + "\n\nDo not choose who checks the work. Every agent carries the checks its "
          "own work needs, set once where the agent is configured, and they are put on "
          "each step for you. A plan that picked a verifier per step picked it from a "
          "sentence, not from what the project has, and picked differently every time "
          "it was asked.\n\n"
          "If the check passes, the flow moves on. If the step itself does not succeed, "
          "the same agent tries again — as many times as that agent is configured for, "
          "which is not yours to decide — and running out halts the run.\n\n"
          "A step is one agent trying something. When the work needs two agents "
          "handing back and forth — a tester that finds a bug for a developer to fix, "
          "then tests again — set `loop` on the step instead of `role`.\n\n"
          "END THE PLAN BY VERIFYING IT. The last step must run the tests against "
          "what was built, and a loop is the right shape for it: a plain tester step "
          "reports the bug and stops, where a loop gets it fixed and tested again."
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
    loop_names = [l.name for l in (loops.all() if loops else [])]
    if loop_names:
        system += ("\n\nLoops you can put on a step instead of an agent:\n"
                   + "\n".join(f"- {l.name}: {l.description or '(no description)'} "
                                f"[{' → '.join(l.roles())}]"
                                for l in loops.all()))

    full = [{"role": "system", "content": system}] + messages

    client = client_for(config)
    response = client.complete(full, tools=[propose_flow_tool(roles, loop_names)])

    bus.emit(
        "model_call", session_id, agent="orchestrator",
        payload={
            "round": 1, "model": config.model, "preset": config.preset,
        "base_url": config.base_url,
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
        proposal = ensure_checks(_normalize(call.arguments, roles), roles=roles)
        proposal = ensure_frame(
            proposal,
            opening=getattr(settings, "plan_open", "") or "",
            closing=getattr(settings, "plan_close", "") or "",
            roles=roles, loops=loops)
        proposal["requirements"] = [
            str(item).strip()
            for item in (call.arguments.get("requirements") or [])
            if str(item).strip()
        ][:20]
        proposal = ensure_final_check(proposal, loops=loops, roles=roles)

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
        loop = (raw.get("loop") or "").strip()
        task = (raw.get("task") or "").strip()
        if not task:
            continue
        if loop:
            role = ""
        elif role not in known or role == "orchestrator":
            continue

        # Whatever it named, it does not decide this. Checks belong to the
        # agent — one place, visible, edited by a person — and a model asked to
        # pick one per step answers from the shape of the sentence in front of
        # it, differently each time it is asked.
        proposed = raw.get("check") or raw.get("verify_with")
        if isinstance(proposed, list):
            proposed = proposed[0] if proposed else None
        check = None
        if proposed:
            dropped.append(f"{proposed} (checks come from the agent, not the plan)")

        # Older proposals (and older saved flows) may still carry one.
        fixer = raw.get("on_fail")
        if fixer and (fixer not in known or fixer == "orchestrator" or not check):
            fixer = None

        steps.append({
            "role": role, "loop": loop, "task": task, "check": check, "on_fail": fixer,
            # 0 = however many tries that agent gets. How patient to be with an
            # agent is a property of the agent, set once where it is known, and
            # a plan that stamps a number on every step quietly overrides it.
            "max_loops": 0,
            "points": _points(raw.get("points")),
        })

    team = [n for n in (arguments.get("team") or []) if n in known and n != "orchestrator"]
    for step in steps:
        for name in [step.get("role"), step.get("check"), step.get("on_fail")]:
            if not name:
                continue
            if name not in team:
                team.append(name)

    return {
        "summary": arguments.get("summary", ""), "team": team, "steps": steps,
        "dropped_checks": dropped,
    }


#: The floor, applied by rule to every planned step that writes files: an agent
#: reporting SUCCESS is the one claim in this system with no evidence behind
#: it. The reviewer — the same check the developer carries — reads the diff
#: and judges whether the claim is true.
DEFAULT_CHECK = "reviewer"
def ensure_checks(proposal: dict, *, roles=None) -> dict:
    """Put the fact check on every planned step that writes files.

    Not a choice, and deliberately not the planner's. An agent reporting
    SUCCESS is the only claim in this system with no evidence behind it, and
    the commonest way a run goes wrong is a step that said it wrote a file and
    did not. So it is always the same check, applied by rule — where asking a
    model for one per step got a verifier picked from the shape of a sentence,
    and a different one each time the same plan was proposed.

    Everything else the step is checked by comes from its agent, and lands on
    the step when the plan is read. This is only the floor.
    """
    by_name = {r.name: r for r in (roles or [])}
    checker = by_name.get(DEFAULT_CHECK)
    if checker is None or not checker.verifier:
        return proposal

    added = []
    for step in proposal.get("steps") or []:
        if step.get("loop"):
            continue                     # a loop carries its own wiring
        role = by_name.get(step.get("role") or "")
        if role is None or "files" not in role.toolsets:
            continue                     # nothing written, nothing to check
        step["check"] = DEFAULT_CHECK
        step["checks"] = [DEFAULT_CHECK]
        added.append(step.get("task", ""))

    if added:
        proposal["added_checks"] = len(added)
        team = list(proposal.get("team") or [])
        if DEFAULT_CHECK not in team:
            team.append(DEFAULT_CHECK)
        proposal["team"] = team
    return proposal


def ensure_frame(proposal: dict, *, opening: str = "", closing: str = "",
                 roles=None, loops=None) -> dict:
    """Open and close a generated plan with what the project always wants.

    Prompting the orchestrator for this is a hope; the user watched it propose
    a plan without their planner and without a final visual pass, twice. These
    are settings, enforced by rule — the same shape as the fact check: what a
    plan must always have is not the model's to forget.

    A name the project does not actually have is skipped rather than fatal:
    the defaults may name an agent only some projects define.
    """
    steps = proposal.get("steps")
    if not steps:
        return proposal
    by_name = {r.name: r for r in (roles or [])}
    known_loops = {l.name for l in (loops.all() if loops else [])}

    if opening in by_name and (steps[0].get("role") or "") != opening:
        steps.insert(0, {
            "role": opening, "loop": "", "task": (
                "Go over this request and the plan below before anyone builds. "
                "Check the steps against the code as it actually is, and use "
                "remember to write down the decisions and pitfalls the team "
                "must follow. Do not implement anything."),
            "check": None, "checks": [], "on_fail": None, "max_loops": 0,
            "points": 1,
        })
        proposal["opened_with"] = opening

    is_loop = closing in known_loops
    if closing and (is_loop or closing in by_name):
        last = steps[-1]
        already = (last.get("loop") == closing if is_loop
                   else last.get("role") == closing)
        if not already:
            steps.append({
                "role": "" if is_loop else closing,
                "loop": closing if is_loop else "",
                "task": ("Visually verify everything this plan built, in the running "
                         "app: work through what was asked for and judge it by what "
                         "is actually on screen."),
                "check": None, "checks": [], "on_fail": None, "max_loops": 0,
                "points": 2,
            })
            proposal["closed_with"] = closing
    return proposal


def ensure_final_check(proposal: dict, *, loops=None, roles=None) -> dict:
    """Guarantee the plan ends by verifying what it built.

    Asking for it in the prompt is not enough: a model that has just written a
    convincing ten-step plan is exactly the one that stops at the last feature.
    A run that ends with nobody having run the tests is the failure this whole
    system exists to prevent, so it is added rather than requested.
    """
    steps = list(proposal.get("steps") or [])
    if not steps:
        return proposal

    available = {l.name: l for l in (loops.all() if loops else [])}
    verifiers = {r.name for r in (roles or []) if r.verifier}

    def verifies(step: dict) -> bool:
        """Whether this step actually exercises what was built.

        A fact check does not count. It confirms the files exist, which is
        worth having on every step and is not the same as anyone having run
        the thing — and now that it is added by default, counting it would
        mean no plan ever gets a final test.
        """
        loop = available.get(step.get("loop") or "")
        if loop is not None:
            return any(node.role in verifiers for node in loop.nodes)
        if step.get("role") in verifiers:
            return True
        check = step.get("check")
        return bool(check) and check != DEFAULT_CHECK

    if verifies(steps[-1]):
        return proposal

    # Prefer a loop that ends by testing: a plain tester step reports the bug
    # and stops, where a loop gets it fixed and tested again.
    loop_name = next((name for name, loop in available.items()
                      if any(n.role in verifiers for n in loop.nodes)
                      and any(n.role not in verifiers for n in loop.nodes)), "")
    tail = {
        "role": "" if loop_name else next(iter(verifiers & {"tester"}), ""),
        "loop": loop_name,
        "task": ("Verify the whole thing works end to end: run the tests, and if there "
                 "is no test for what was just built, write one. Report exactly what "
                 "you observed."),
        "check": None, "on_fail": None, "max_loops": 0, "points": 3,
    }
    if not tail["loop"] and not tail["role"]:
        return proposal            # nothing here can verify; do not invent one

    steps.append(tail)
    proposal["steps"] = steps
    proposal["added_final_check"] = tail["loop"] or tail["role"]

    team = list(proposal.get("team") or [])
    wanted = (available[loop_name].roles() if loop_name else [tail["role"]])
    for name in wanted:
        if name and name not in team:
            team.append(name)
    proposal["team"] = team
    return proposal


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


#: What the model may hand back for "how do I run this". A tool rather than
#: prose, so the answer is a command trance can execute rather than a paragraph
#: someone has to read and retype.
def run_command_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "how_to_run",
            "description": "State the one command that starts this project's dev server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description":
                                "The command, exactly as it would be typed."},
                    "dir": {"type": "string", "description":
                            "Directory to run it in, relative to the project root. "
                            "Empty for the root."},
                    "why": {"type": "string", "description":
                            "One sentence: where this came from — the README, a "
                            "script in package.json, the framework's convention."},
                    "static_instead": {"type": "boolean", "description":
                                       "True when this project is plain files and "
                                       "needs no server at all."},
                },
                "required": ["command", "why"],
            },
        },
    }


def how_to_run(project: Path, *, config: ModelConfig, bus: EventBus,
               session_id: str = "") -> dict:
    """Work out what starts this project, by reading what it says about itself.

    A dev command is not always `npm run dev`: monorepos put it behind a
    workspace filter, Python projects have no package.json at all, and plenty
    of READMEs name a script that exists precisely because the obvious one does
    not work. Detecting it from package.json alone gets the common case and
    quietly gets the interesting ones wrong.

    Proposed, never run: the answer comes back for a person to confirm, because
    the one thing worse than not knowing the command is running the wrong one
    on someone's machine.
    """
    readme = ""
    for name in ("README.md", "readme.md", "README", "README.rst"):
        found = project / name
        if found.is_file():
            try:
                readme = found.read_text(encoding="utf8", errors="replace")[:6000]
            except OSError:
                readme = ""
            break

    manifest = ""
    package = project / "package.json"
    if package.is_file():
        try:
            manifest = package.read_text(encoding="utf8", errors="replace")[:3000]
        except OSError:
            manifest = ""

    listing = ", ".join(sorted(entry.name for entry in list(project.iterdir())[:60]
                               if not entry.name.startswith("."))) or "(empty)"

    role = BUILTIN_ROLES["orchestrator"]
    prompt = (
        "How is this project's dev server started? Answer by calling how_to_run.\n\n"
        f"Files at the root: {listing}\n\n"
        + (f"README:\n{readme}\n\n" if readme else "There is no README.\n\n")
        + (f"package.json:\n{manifest}\n\n" if manifest else "")
        + "Give the command that serves the app for a browser to open — not the "
          "build, not the tests. If the README names one, prefer it over anything "
          "you infer: it is the one the author says works. If this is plain HTML "
          "and JavaScript with no build step, set static_instead and say so."
    )
    messages = [{"role": "system", "content": role.system_prompt},
                {"role": "user", "content": prompt}]
    response = client_for(config).complete(messages, tools=[run_command_tool()])

    proposal: dict = {}
    for call in response.tool_calls or []:
        if call.name == "how_to_run" and not call.malformed:
            proposal = dict(call.arguments or {})
            break

    bus.emit("model_call", session_id or "preview", agent="orchestrator", payload={
        "round": 1, "model": config.model, "preset": config.preset,
        "base_url": config.base_url, "messages": messages,
        "response_text": response.text, "reasoning": response.reasoning,
        "tool_calls": [{"name": c.name, "arguments": c.arguments}
                       for c in (response.tool_calls or [])],
        "finish_reason": response.finish_reason, "usage": response.usage,
        "asked": "how to run this project",
    })
    return {
        "command": (proposal.get("command") or "").strip(),
        "dir": (proposal.get("dir") or "").strip(),
        "why": (proposal.get("why") or "").strip(),
        "static_instead": bool(proposal.get("static_instead")),
        "read_readme": bool(readme),
    }


def draft_agent_prompt(name: str, *, description: str = "", goal: str = "",
                       config: ModelConfig, bus: EventBus, session_id: str = "") -> str:
    """Write a system prompt for an agent, from its name and what it is for.

    A blank box is the hardest part of adding an agent: the shape of a prompt
    that works here — who it is, what it must not do, how to report — is not
    obvious from the box. This produces a draft to edit, never a finished thing:
    the person adding the agent knows what it is for and the model does not.
    """
    role = BUILTIN_ROLES["orchestrator"]
    about = f"\n\nWhat the user says it is for: {description}" if description else ""
    project = f"\n\nThe project: {goal}" if goal else ""
    prompt = (
        f"Write the system prompt for a coding agent called {name!r} on this team."
        f"{about}{project}\n\n"
        "Write the prompt itself and nothing else — no preamble, no explanation, no "
        "markdown fences. Address the agent as 'You'.\n\n"
        "It must cover, in this order and in a few short lines each:\n"
        "- what it is and which part of the project it owns\n"
        "- what it does, specifically — 'write the HTTP handlers', not 'do backend work'\n"
        "- what it must not do: the mistake this kind of agent actually makes\n"
        "- that it should fetch symbols rather than read whole files, and change only "
        "what the task asks for\n"
        "- that it must end with 'OUTCOME: SUCCESS' or 'OUTCOME: FAILED — why', and that "
        "a wrong success costs the next agent more than an honest failure\n\n"
        "Keep it under 200 words. Be concrete about this agent; anything true of every "
        "agent is wasted context."
    )
    messages = [{"role": "system", "content": role.system_prompt},
                {"role": "user", "content": prompt}]
    response = client_for(config).complete(messages)
    bus.emit("model_call", session_id or "agents", agent="orchestrator", payload={
        "round": 1, "model": config.model, "preset": config.preset,
        "base_url": config.base_url,
        "drafting_prompt_for": name, "messages": messages,
        "response_text": response.text, "reasoning": response.reasoning,
        "tool_calls": [], "finish_reason": response.finish_reason,
        "usage": response.usage, "summary": summarize_messages(messages),
    })
    return (response.text or "").strip()
