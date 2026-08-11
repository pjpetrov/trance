"""Loops: a reusable block of agents wired by outcome.

A step's built-in retry answers one question — "this agent failed, try again,
maybe with a fixer." That is enough for a one-shot task and not enough for the
shape that actually recurs: tester finds a bug, developer fixes it, tester runs
again, and round until it passes or a count runs out.

A loop names that shape once. It is a small state machine:

    node = an agent + the prompt that focuses it on its part
    exit  = one of SUCCESS / FAILED / CHECK_FAILED on that node
    route = where each exit goes: another node, or out of the loop

The engine walks it. `max_visits` on a route is what makes it finite: follow
that route more than N times and the loop stops rather than turning forever.

An exit may have more than one route, in tiers. The first covers the first N
times that exit is taken, the next covers the ones after it, and running past
the last one ends the loop. That is what lets a loop change tactic instead of
repeating one: the first three failures go back to the developer, the next two
go to a senior agent, and the sixth stops. A route may also ask for its target's
*backup* model, so "try again" and "try again with something stronger" are
different arrows rather than the same one hoping for a different result.

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
    """One route out of one outcome: where it goes, and for how many turns."""

    #: A node id, or EXIT_LOOP / FAIL_LOOP.
    target: str = FAIL_LOOP
    #: How many times this route may be followed before the next tier takes
    #: over — or, if it is the last, before the loop gives up. Only meaningful
    #: when it points back at a node.
    max_visits: int = DEFAULT_MAX_VISITS
    #: Run the target on its backup model rather than its usual one. The point
    #: of a later tier is usually that the earlier one did not work, and the
    #: model is the one thing an ordinary retry never changes.
    backup: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "Edge":
        if isinstance(data, str):                 # shorthand: just a target
            return cls(target=data)
        data = data or {}
        return cls(target=data.get("target") or FAIL_LOOP,
                   max_visits=max(1, min(HARD_VISIT_CEILING,
                                         int(data.get("max_visits") or DEFAULT_MAX_VISITS))),
                   backup=bool(data.get("backup")))


@dataclass
class LoopNode:
    """One agent's turn inside a loop."""

    role: str
    id: str = field(default_factory=new_node_id)
    #: Appended to the step's own task. This is what tells the tester it is
    #: testing rather than building, when both share the step's prompt.
    focus: str = ""
    #: Optional check on this node's work, as on a step. Legacy single form;
    #: `checks` is the chain that actually runs.
    check: str | None = None
    #: Every check this node's work runs, in order — the same chips a step
    #: carries. Copied once from the node's agent, then the loop's own to edit.
    checks_chain: list[str] = field(default_factory=list)
    #: Whether the chain has been filled in from the agent's standing checks.
    #: Once it has, the chain here is the whole truth — a check taken off in
    #: the loops editor stays off.
    checks_seeded: bool = False
    #: Undo this block's changes when it does not succeed. Useful mid-loop: a
    #: fixer that made things worse should not hand its mess to the next agent.
    revert_on_fail: bool = False
    #: outcome -> the routes for it, in order. A missing exit fails the loop,
    #: which is the safe default: an unrouted outcome means the author did not
    #: think about it. One route is the ordinary case; several are tiers.
    on: dict[str, list[Edge]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Written as a single Edge almost everywhere, since one route is the
        # common case. Normalise so everything downstream sees a list.
        self.on = {k: ([v] if isinstance(v, Edge) else list(v))
                   for k, v in (self.on or {}).items()}

    @property
    def checks(self) -> list[str]:
        """Every check this node runs, falling back to the legacy single one."""
        if self.checks_chain:
            return [name for name in self.checks_chain if name]
        return [self.check] if self.check else []

    def routes(self, outcome: str) -> list[Edge]:
        return self.on.get(outcome) or []

    def edge(self, outcome: str) -> Edge:
        """The first route for an outcome — what an unvisited node will do."""
        routes = self.routes(outcome)
        return routes[0] if routes else Edge(target=FAIL_LOOP)

    def route(self, outcome: str, taken: int) -> Edge | None:
        """Which route applies the `taken`-th time this exit is taken (0-based).

        None means every tier is spent, and the loop is over: this is the "and
        after that, halt" end of a tiered exit.
        """
        for edge in self.routes(outcome):
            if edge.target in STOP:
                return edge          # leaving never runs out
            if taken < edge.max_visits:
                return edge
            taken -= edge.max_visits
        return None

    def allowance(self, outcome: str) -> int:
        """How many times this exit may be taken in all, across its tiers."""
        return sum(e.max_visits for e in self.routes(outcome) if e.target not in STOP)

    def to_dict(self) -> dict:
        return {"id": self.id, "role": self.role, "focus": self.focus,
                "check": self.check, "checks": self.checks,
                "checks_seeded": self.checks_seeded,
                "revert_on_fail": self.revert_on_fail,
                "on": {k: [e.to_dict() for e in routes] for k, routes in self.on.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> "LoopNode":
        return cls(
            id=data.get("id") or new_node_id(),
            role=data.get("role") or "",
            focus=data.get("focus") or "",
            check=data.get("check") or None,
            # Presence decides, as on a step: "checks: []" is taking them all
            # off, not saying nothing.
            checks_chain=[str(n) for n in data["checks"] if n]
                         if "checks" in data else [],
            checks_seeded=bool(data.get("checks_seeded")),
            revert_on_fail=bool(data.get("revert_on_fail")),
            on={k: [Edge.from_dict(e) for e in (v if isinstance(v, list) else [v])]
                for k, v in (data.get("on") or {}).items() if k in EXITS},
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
        for outcome, routes in node.on.items():
            if outcome not in EXITS:
                return f"unknown outcome {outcome!r} on the {node.role} block"
            for n, edge in enumerate(routes, start=1):
                where = (f"the {outcome} arrow on the {node.role} block"
                         if len(routes) == 1 else
                         f"{outcome} arrow {n} on the {node.role} block")
                if edge.target not in STOP and edge.target not in ids:
                    return f"{where} points nowhere"
                # Anything after a route that leaves the loop can never run.
                if edge.target in STOP and n < len(routes):
                    left = len(routes) - n
                    return (f"{where} leaves the loop, so the {left} "
                            f"arrow{'' if left == 1 else 's'} after it can never "
                            f"be taken")
        if node.check is None and CHECK_FAILED in node.on:
            return (f"the {node.role} block routes CHECK_FAILED but has no check — "
                    f"that outcome can never happen")

    if loop.start and loop.start not in ids:
        return "the start block is not in this loop"

    # A loop with no way out runs until max_steps and then reports failure,
    # which looks like a broken agent rather than a broken plan.
    reachable_exit = any(edge.target == EXIT_LOOP for node in loop.nodes
                         for routes in node.on.values() for edge in routes)
    if not reachable_exit:
        return ("nothing in this loop exits successfully — give at least one outcome "
                "the 'leave the loop' action")
    return None
