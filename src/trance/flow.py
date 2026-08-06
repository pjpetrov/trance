"""The flow: which agent works, in what order, and who verifies it.

Deliberately a flat ordered list rather than a general DAG. The thing users
actually want to express is "backend, then test it, then frontend, then test it,
and loop back if the test fails" — a list with a per-step verifier and an
attempt limit says that exactly, and it stays drawable and drag-reorderable in a
UI. A general graph would be more expressive and much worse to steer.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

StepStatus = Literal["pending", "running", "verifying", "done", "failed", "skipped", "blocked"]


def new_step_id() -> str:
    return f"step_{uuid.uuid4().hex[:8]}"


@dataclass
class GateResult:
    """One check run against one attempt."""

    gate: str
    verdict: str            # PASS | FAIL | UNKNOWN
    feedback: str = ""
    event_id: str | None = None


@dataclass
class Attempt:
    """One pass of a step: the work, and every check run against it."""

    n: int
    worker_event_id: str | None = None
    verifier_event_id: str | None = None
    verdict: str | None = None  # the deciding verdict for this attempt
    feedback: str = ""
    files_written: list[str] = field(default_factory=list)
    #: Each check that ran against this pass.
    gate_results: list[GateResult] = field(default_factory=list)
    #: What the fixing agent did, when one ran.
    fix_event_id: str | None = None
    fix_summary: str = ""
    #: Writes the tool layer refused because they were outside the remit. Kept
    #: because a step that failed for this reason is fixed by reassigning it,
    #: not by looping — and only these paths say which agent to reassign it to.
    refused_paths: list[str] = field(default_factory=list)
    #: How full the window was on this attempt's last call — the same numbers
    #: the live gauge shows, kept so a finished step can still show them.
    context: dict = field(default_factory=dict)
    #: The step's own outcome, as reported by the agent that did the work.
    outcome: str = ""
    outcome_reason: str = ""

    @property
    def failed_gate(self) -> str | None:
        return next((g.gate for g in self.gate_results if g.verdict == "FAIL"), None)


@dataclass
class Step:
    #: The agent that does the work. Ignored when `loop` is set.
    role: str
    task: str
    #: Run a named loop instead of a single agent. A one-shot task wants an
    #: agent; anything with a "and check it, and fix it, and check again" shape
    #: wants a loop, and expressing that with one step's retry never quite fit.
    loop: str = ""
    id: str = field(default_factory=new_step_id)
    #: Optional reality check run after the work. PASS lets the flow move on;
    #: anything else opens the block's internal loop.
    check: str | None = None
    #: Who tries to fix a failed check. Empty means this step's own role has
    #: another go. Either way the block then runs again.
    on_fail: str | None = None
    #: How many times the block may run before the flow is halted. The loop can
    #: only be left by succeeding — exhausting it stops the run.
    max_loops: int = 2
    #: Whether the escalation attempt has already been spent on this step.
    #: One per step: escalation that can itself loop is a longer loop with a
    #: bigger bill.
    escalated: bool = False
    #: The orchestrator's size estimate (see orchestrator.POINTS). 0 = unrated.
    #: A step nobody can hold in their head is where agents drift, so this is
    #: what the splitter acts on.
    points: int = 0

    #: Legacy fields, still read when `check` is unset.
    gates: list[str] = field(default_factory=list)
    verify_with: str | None = None
    max_attempts: int = 2
    #: Entry point for the context curator. Blank = no curated bundle (new code).
    entry: str = ""
    status: StepStatus = "pending"
    attempts: list[Attempt] = field(default_factory=list)
    #: User steering notes queued for this step's next prompt.
    steering: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def checker(self) -> str | None:
        """The reality check, however it was configured."""
        return self.check or (self.gates[0] if self.gates else None) or self.verify_with

    @property
    def fixer(self) -> str:
        """Who addresses a failed check — a chosen agent, else this step's role."""
        return self.on_fail or self.role

    @property
    def loop_limit(self) -> int:
        return max(1, self.max_loops or self.max_attempts or 1)

    @property
    def checks(self) -> list[str]:
        """Legacy accessor; the UI and traces now use `checker`."""
        return [self.checker] if self.checker else []

    @property
    def runs_a_loop(self) -> bool:
        return bool(self.loop)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["runs_a_loop"] = self.runs_a_loop
        data["checks"] = self.checks
        data["checker"] = self.checker
        data["fixer"] = self.fixer
        data["loop_limit"] = self.loop_limit
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        data = dict(data)
        data.pop("attempts", None)
        for derived in ("checks", "checker", "fixer", "loop_limit", "runs_a_loop"):
            data.pop(derived, None)
        # Older shapes fold into `check`.
        if not data.get("check"):
            gates = data.get("gates") or []
            data["check"] = (gates[0] if gates else None) or data.get("verify_with")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Flow:
    steps: list[Step] = field(default_factory=list)
    #: Index of the step currently executing, or -1.
    cursor: int = -1

    def to_dict(self) -> dict:
        return {"steps": [s.to_dict() for s in self.steps], "cursor": self.cursor}

    @classmethod
    def from_dict(cls, data: dict) -> "Flow":
        return cls(steps=[Step.from_dict(s) for s in data.get("steps", [])])

    def find(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def next_pending(self) -> Step | None:
        return next((s for s in self.steps if s.status == "pending"), None)

    #: A step whose agent is mid-flight cannot be edited — everything else can.
    LOCKED = ("running", "verifying")
    #: Statuses that mean "this will not run again unless you say so".
    TERMINAL = ("done", "failed", "blocked", "skipped")

    def apply_edits(self, steps: list[Step]) -> dict:
        """Apply an edited step list, keeping only in-flight steps immutable.

        The event trace is append-only regardless; a step is a *plan*, and a
        plan you cannot correct after it failed is not much use. So a failed,
        blocked or finished step is editable, and changing what it says re-queues
        it — editing a step you already ran means you want it run again.
        """
        locked = {s.id: s for s in self.steps if s.status in self.LOCKED}
        current = {s.id: s for s in self.steps}
        result: list[Step] = []
        requeued: list[str] = []

        for incoming in steps:
            existing = current.get(incoming.id)
            if existing is None:
                result.append(incoming)          # a brand new step
                continue
            if existing.id in locked:
                result.append(existing)          # mid-flight: untouchable
                continue

            changed = (
                existing.role != incoming.role
                or existing.loop != incoming.loop
                or existing.task != incoming.task
                or existing.checker != incoming.checker
                or existing.fixer != incoming.fixer
                or existing.entry != incoming.entry
            )
            existing.role, existing.task = incoming.role, incoming.task
            existing.loop = incoming.loop
            existing.check = incoming.checker
            existing.on_fail = incoming.on_fail
            existing.max_loops = incoming.loop_limit
            existing.gates, existing.verify_with = [], None
            existing.entry = incoming.entry
            if changed and existing.status in self.TERMINAL:
                existing.status = "pending"
                existing.attempts = []           # a re-queued step starts clean
                requeued.append(existing.id)
            result.append(existing)

        # A locked step can never be dropped by an edit that raced with it.
        kept = {s.id for s in result}
        for step_id, step in locked.items():
            if step_id not in kept:
                result.append(step)

        self.steps = result
        return {"requeued": requeued, "locked": list(locked)}

    def replace_pending(self, steps: list[Step]) -> list[str]:
        """Backwards-compatible wrapper around apply_edits()."""
        self.apply_edits(steps)
        return [s.id for s in self.steps if s.status == "pending"]

    @property
    def progress(self) -> dict:
        counts: dict[str, int] = {}
        for step in self.steps:
            counts[step.status] = counts.get(step.status, 0) + 1
        return {"total": len(self.steps), **counts}


def build_flow(spec: list[dict]) -> Flow:
    """Build a Flow from the orchestrator's proposal (or the UI's editor)."""
    return Flow(steps=[Step.from_dict(item) for item in spec])
