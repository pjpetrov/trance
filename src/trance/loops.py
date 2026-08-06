"""Loops: a reusable block of agents wired by outcome.

A step's built-in retry answers one question — "this agent failed, try again,
maybe with a fixer." That is enough for a one-shot task and not enough for the
shape that actually recurs: tester finds a bug, developer fixes it, tester runs
again, and round until it passes or a count runs out.

A loop names that shape once. It is a small state machine:

    node = an agent + the prompt that focuses it on its part
    exit  = one of SUCCESS / FAILED / CHECK_FAILED on that node
    edge  = where each exit goes: another node, or out of the loop

The engine walks it. `max_visits` on an edge is what makes it finite: follow
that edge more than N times and the loop stops rather than turning forever.

Deliberately not a general graph language. Every edge is labelled by an outcome
the engine already computes, so a loop cannot express a condition trance has no
way to evaluate — the thing that makes a visual editor lie.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field

#: The three ways a node can finish. They are exactly the outcomes the engine
#: already produces for a step, so nothing here needs new machinery to decide.
SUCCESS, FAILED, CHECK_FAILED = "SUCCESS", "FAILED", "CHECK_FAILED"
EXITS = (SUCCESS, FAILED, CHECK_FAILED)

#: Where an edge can point instead of at a node.
EXIT_LOOP = "exit"          # done, the step succeeded
FAIL_LOOP = "fail"          # done, the step failed
STOP = (EXIT_LOOP, FAIL_LOOP)

#: A loop that never stops is worse than one that stops early.
DEFAULT_MAX_VISITS = 3
HARD_VISIT_CEILING = 20


def new_node_id() -> str:
    return f"n_{uuid.uuid4().hex[:6]}"


@dataclass
class Edge:
    """What happens on one outcome of one node."""

    #: A node id, or EXIT_LOOP / FAIL_LOOP.
    target: str = FAIL_LOOP
    #: How many times this edge may be followed before the loop gives up.
    #: Only meaningful when it points back at a node.
    max_visits: int = DEFAULT_MAX_VISITS

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "Edge":
        if isinstance(data, str):                 # shorthand: just a target
            return cls(target=data)
        data = data or {}
        return cls(target=data.get("target") or FAIL_LOOP,
                   max_visits=max(1, min(HARD_VISIT_CEILING,
                                         int(data.get("max_visits") or DEFAULT_MAX_VISITS))))


@dataclass
class LoopNode:
    """One agent's turn inside a loop."""

    role: str
    id: str = field(default_factory=new_node_id)
    #: Appended to the step's own task. This is what tells the tester it is
    #: testing rather than building, when both share the step's prompt.
    focus: str = ""
    #: Optional check on this node's work, as on a step.
    check: str | None = None
    #: Undo this block's changes when it does not succeed. Useful mid-loop: a
    #: fixer that made things worse should not hand its mess to the next agent.
    revert_on_fail: bool = False
    #: outcome -> Edge. A missing exit fails the loop, which is the safe default:
    #: an unrouted outcome means the author did not think about it.
    on: dict[str, Edge] = field(default_factory=dict)

    def edge(self, outcome: str) -> Edge:
        return self.on.get(outcome) or Edge(target=FAIL_LOOP)

    def to_dict(self) -> dict:
        return {"id": self.id, "role": self.role, "focus": self.focus,
                "check": self.check, "revert_on_fail": self.revert_on_fail,
                "on": {k: e.to_dict() for k, e in self.on.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> "LoopNode":
        return cls(
            id=data.get("id") or new_node_id(),
            role=data.get("role") or "",
            focus=data.get("focus") or "",
            check=data.get("check") or None,
            revert_on_fail=bool(data.get("revert_on_fail")),
            on={k: Edge.from_dict(v) for k, v in (data.get("on") or {}).items()
                if k in EXITS},
        )


@dataclass
class Loop:
    """A named, reusable block a step can run instead of a single agent."""

    name: str
    description: str = ""
    #: Extra instruction given to every agent in the loop, on top of the step's
    #: own task — what this loop is for, as opposed to what this task is.
    prompt: str = ""
    nodes: list[LoopNode] = field(default_factory=list)
    #: Where execution begins. Empty = the first node.
    start: str = ""
    #: Total node visits before the loop is abandoned, whatever the edges say.
    max_steps: int = 12

    @property
    def entry(self) -> LoopNode | None:
        if not self.nodes:
            return None
        return self.node(self.start) or self.nodes[0]

    def node(self, node_id: str | None) -> LoopNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def roles(self) -> list[str]:
        """Every agent this loop can call, including its checks."""
        names: list[str] = []
        for node in self.nodes:
            for name in (node.role, node.check):
                if name and name not in names:
                    names.append(name)
        return names

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "prompt": self.prompt,
                "start": self.start or (self.nodes[0].id if self.nodes else ""),
                "max_steps": self.max_steps,
                "nodes": [n.to_dict() for n in self.nodes],
                "roles": self.roles()}

    @classmethod
    def from_dict(cls, data: dict) -> "Loop":
        return cls(
            name=(data.get("name") or "").strip(),
            description=data.get("description") or "",
            prompt=data.get("prompt") or "",
            nodes=[LoopNode.from_dict(n) for n in (data.get("nodes") or [])],
            start=data.get("start") or "",
            max_steps=max(1, min(60, int(data.get("max_steps") or 12))),
        )


def validate(loop: Loop, known_roles, verifiers) -> str | None:
    """Return an error message, or None if the loop can actually run.

    Checked here rather than at run time because a loop that dead-ends is only
    discovered halfway through a run otherwise, with the work already done.
    """
    if not loop.name or " " in loop.name:
        return "name must be non-empty and contain no spaces"
    if not loop.nodes:
        return "a loop needs at least one agent"

    ids = {n.id for n in loop.nodes}
    for node in loop.nodes:
        if node.role not in known_roles:
            return f"unknown agent {node.role!r}"
        if node.check and node.check not in verifiers:
            return (f"{node.check!r} cannot check work — pick an agent marked "
                    f"'can verify', or none")
        for outcome, edge in node.on.items():
            if outcome not in EXITS:
                return f"unknown outcome {outcome!r} on the {node.role} block"
            if edge.target not in STOP and edge.target not in ids:
                return f"the {outcome} arrow on the {node.role} block points nowhere"
        if node.check is None and CHECK_FAILED in node.on:
            return (f"the {node.role} block routes CHECK_FAILED but has no check — "
                    f"that outcome can never happen")

    if loop.start and loop.start not in ids:
        return "the start block is not in this loop"

    # A loop with no way out runs until max_steps and then reports failure,
    # which looks like a broken agent rather than a broken plan.
    reachable_exit = any(edge.target == EXIT_LOOP
                         for node in loop.nodes for edge in node.on.values())
    if not reachable_exit:
        return ("nothing in this loop exits successfully — give at least one outcome "
                "the 'leave the loop' action")
    return None
