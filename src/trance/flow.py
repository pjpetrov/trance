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
class Attempt:
    """One pass of a step: the work, and the verdict on it."""

    n: int
    worker_event_id: str | None = None
    verifier_event_id: str | None = None
    verdict: str | None = None  # PASS | FAIL | None
    feedback: str = ""
    files_written: list[str] = field(default_factory=list)


@dataclass
class Step:
    role: str
    task: str
    id: str = field(default_factory=new_step_id)
    #: Role that checks this step's work. Its FAIL sends us back around.
    verify_with: str | None = None
    #: How many times to retry when the verifier fails.
    max_attempts: int = 2
    #: Entry point for the context curator. Blank = no curated bundle (new code).
    entry: str = ""
    status: StepStatus = "pending"
    attempts: list[Attempt] = field(default_factory=list)
    #: User steering notes queued for this step's next prompt.
    steering: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        data = dict(data)
        data.pop("attempts", None)
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

    def replace_pending(self, steps: list[Step]) -> list[str]:
        """Swap in a new set of not-yet-started steps, preserving history.

        Called when the user edits the flow mid-run. Steps that already ran are
        immutable — rewriting history would make the trace a lie.
        """
        finished = [s for s in self.steps if s.status not in ("pending",)]
        finished_ids = {s.id for s in finished}
        incoming = [s for s in steps if s.id not in finished_ids]
        # Carry over any live objects so queued steering survives the edit.
        existing = {s.id: s for s in self.steps if s.status == "pending"}
        merged = []
        for step in incoming:
            current = existing.get(step.id)
            if current is not None:
                current.role, current.task = step.role, step.task
                current.verify_with, current.max_attempts = step.verify_with, step.max_attempts
                current.entry = step.entry
                merged.append(current)
            else:
                merged.append(step)
        self.steps = finished + merged
        return [s.id for s in merged]

    @property
    def progress(self) -> dict:
        counts: dict[str, int] = {}
        for step in self.steps:
            counts[step.status] = counts.get(step.status, 0) + 1
        return {"total": len(self.steps), **counts}


def build_flow(spec: list[dict]) -> Flow:
    """Build a Flow from the orchestrator's proposal (or the UI's editor)."""
    return Flow(steps=[Step.from_dict(item) for item in spec])
