"""Agent roles.

A role is a named specialist: its own system prompt, its own model settings, and
— importantly — a *remit*: the path globs it is allowed to touch. The remit is
what makes "this agent is overstepping into another's duties" a mechanical check
instead of a judgement call.

Roles are data. The orchestrator proposes a set of them per project, the user
edits them in the UI, and the flow engine executes them.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Toolsets a role can be granted.
#:   files    read/write/list within the role's remit
#:   graph    get_definition / get_callers / get_callees over the indexed repo
#:   commands run an allowlisted command (tests, builds) inside the project
#:   inspect  file *metadata* only — exists / size / line count. No contents,
#:            no writes, no commands. For agents that judge whether work
#:            happened without being able to do the work themselves.
#:   browser  open the project in a real headless browser, drive it with keys,
#:            and look at it with a vision model. The only toolset that can
#:            check a canvas game, where the DOM is one element and every real
#:            thing is pixels. Needs Chrome on the machine; without one the
#:            toolset reports itself unavailable and nothing else changes.
TOOLSETS = ("files", "graph", "commands", "inspect", "browser")


@dataclass
class AgentRole:
    name: str
    title: str
    description: str
    system_prompt: str
    #: Globs this role may write to. Empty means "no writes" (advisory roles).
    paths: list[str] = field(default_factory=list)
    toolsets: list[str] = field(default_factory=lambda: ["files", "graph"])
    #: Programs this agent may run. Empty = whatever `command_list` resolves to.
    commands: list[str] = field(default_factory=list)
    #: Named allowlist this agent uses. Empty = the default list.
    command_list: str = ""
    #: Directory (relative to the project) that commands run in. Empty = the
    #: project root. Confined to the project either way.
    workdir: str = ""
    #: Pipes / redirects / &&. None = follow the global policy.
    shell: bool | None = None
    #: May this agent be chosen to verify another step? Only agents that can
    #: actually inspect the result should be — an agent with no tools would
    #: return a verdict it has no way to have checked.
    verifier: bool = False
    #: Verifiers that run after every step this agent does, whatever the plan
    #: says. Set once, rather than ticked onto each of twenty steps — "after
    #: each step, check nothing broke" is a property of the agent's work, not
    #: of one task, and a check added by hand per step stops being added.
    #:
    #: They run in addition to the step's own; the step's are what the plan
    #: chose for this task, these are what this agent always wants.
    checks: list[str] = field(default_factory=list)
    #: Named model preset (provider + model in one). The normal way to assign
    #: a model to an agent; provider/model below stay for older configs.
    preset: str | None = None
    #: A stronger model for this agent to fall back to when it keeps failing.
    #: The loop varies the prompt and what it was told; this varies the one
    #: thing a retry otherwise never changes.
    backup_preset: str | None = None
    #: Tries on the usual model before the backup takes over. Four, measured:
    #: two was the commonest way a nearly-done step died — "never reported
    #: success within 2 loop(s)" with the fix half a try away.
    tries: int = 4
    #: Tries on the backup after that. Ignored without a backup model, so an
    #: agent with none simply gets `tries` and stops.
    backup_tries: int = 2
    #: Legacy name for `tries`, still read from older stored agents.
    backup_after: int = 0

    @property
    def total_tries(self) -> int:
        """How many attempts this agent gets before a step gives up on it."""
        main = max(1, self.tries or 1)
        return main + (max(0, self.backup_tries) if self.backup_preset else 0)
    #: Named provider. None = the configured worker default.
    provider: str | None = None
    #: Per-role model overrides; None means "use the provider's default".
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    #: How many tool rounds this agent gets in one attempt. 0 = the default.
    #: A tester that runs one command needs three; an agent building a feature
    #: file by file needs twenty, and hitting the wall mid-way is how a step
    #: ends with half the work done and a summary of what it meant to do.
    #:
    #: Measured on one real repair loop before these were raised: the dev roles
    #: hit their 24 in seven blocks out of eight and the visual tester hit its
    #: 16 in every single one, ending "analyzed all source files but ran out of
    #: tool rounds before implementing any fixes; no files were modified". The
    #: window was not the constraint — context at the ceiling was 54-73% of
    #: budget — so the count was cutting work short with room to spare, and
    #: each restart re-read the same files: 389 lookups over that step, of
    #: which only 144 were distinct.
    tool_rounds: int = 0
    color: str = "#7aa2f7"
    #: Off means the agent is kept but out of play: the orchestrator cannot
    #: put it on a plan, no step's check chain runs it, and a step or loop
    #: node that names it fails saying so rather than running it. Cheaper
    #: than deleting when the disagreement is "not now", not "not ever" —
    #: a reviewer that is too much for this project keeps its wiring for
    #: the next.
    enabled: bool = True

    def may_write(self, rel_path: str) -> bool:
        if not self.paths:
            return False
        return any(fnmatch.fnmatch(rel_path, pattern) for pattern in self.paths)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRole":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        # `backup_after` was the switch point before it was called `tries`.
        if clean.get("backup_after") and "tries" not in data:
            clean["tries"] = clean["backup_after"]
        return cls(**clean)


#: What an agent *is*, as opposed to how a user wired it in (model, checks,
#: retries — RESET_KEEPS in the store). These are the fields "differs from the
#: original" is judged on, and the fields a reset actually replaces.
DEFINITION_FIELDS = ("title", "description", "system_prompt", "paths",
                     "toolsets", "verifier")


def definition_differs(role, original) -> list[str]:
    """Which definition fields differ between a copy and its original.

    The answer a marker needs: not merely "is it different" but *where*, so a
    tooltip can say "prompt, toolsets" and the person knows whether that is
    their edit or a frozen copy of an older shipped version — both look the
    same from here, and both are worth flagging.
    """
    if original is None:
        return []
    return [field for field in DEFINITION_FIELDS
            if getattr(role, field, None) != getattr(original, field, None)]


#: The shipped roster, loaded from data rather than written in code: the
#: defaults are JSON in trance/defaults/, the same shape every store speaks —
#: so adjusting what a fresh install provisions is editing a file, and the
#: files can be copied straight into a .trance dir and be ready. Loaded once
#: at import; everything downstream (the overlay, PROTECTED, the owner
#: lookup) sees ordinary AgentRole objects exactly as before.
_DEFAULTS = Path(__file__).resolve().parent.parent / "defaults"


def _load_shipped() -> tuple[dict[str, AgentRole], list[str]]:
    data = json.loads((_DEFAULTS / "agents.json").read_text(encoding="utf8"))
    roles = {raw["name"]: AgentRole.from_dict(raw) for raw in data["agents"]}
    return roles, list(data.get("default_team") or [])


BUILTIN_ROLES, _DEFAULT_TEAM = _load_shipped()


def default_team() -> list[AgentRole]:
    return [BUILTIN_ROLES[name] for name in _DEFAULT_TEAM if name in BUILTIN_ROLES]
