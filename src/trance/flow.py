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
    #: Each gate that ran, in order, until one failed.
    gate_results: list[GateResult] = field(default_factory=list)

    @property
    def failed_gate(self) -> str | None:
        return next((g.gate for g in self.gate_results if g.verdict == "FAIL"), None)


@dataclass
class Step:
    role: str
    task: str
    id: str = field(default_factory=new_step_id)
    #: Ordered checks run after the work. Each must PASS before the next runs;
    #: the first FAIL sends its feedback back to this step's own role, which
    #: then redoes the work and the whole chain runs again. That is the loop:
    #: develop -> test -> fix -> test -> review -> fix -> test -> review -> done.
    gates: list[str] = field(default_factory=list)
    #: Legacy single-gate field, still honoured when `gates` is empty.
    verify_with: str | None = None
    #: How many times the work may be redone before the step is failed.
    max_attempts: int = 2
    #: Entry point for the context curator. Blank = no curated bundle (new code).
    entry: str = ""
    status: StepStatus = "pending"
    attempts: list[Attempt] = field(default_factory=list)
    #: User steering notes queued for this step's next prompt.
    steering: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def checks(self) -> list[str]:
        """The gate chain, however it was configured."""
        if self.gates:
            return list(self.gates)
        return [self.verify_with] if self.verify_with else []

    def to_dict(self) -> dict:
        data = asdict(self)
        data["checks"] = self.checks
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        data = dict(data)
        data.pop("attempts", None)
        data.pop("checks", None)
        # A single verify_with is just a one-gate chain.
        if not data.get("gates") and data.get("verify_with"):
            data["gates"] = [data["verify_with"]]
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
                or existing.task != incoming.task
                or existing.checks != incoming.checks
                or existing.entry != incoming.entry
            )
            existing.role, existing.task = incoming.role, incoming.task
            existing.gates = list(incoming.gates)
            existing.verify_with = incoming.verify_with
            existing.max_attempts = incoming.max_attempts
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
