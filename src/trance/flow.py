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
    #: Whether this attempt ran on the agent's backup model.
    on_backup: bool = False
    #: The commit taken before this attempt, and the one taken after it.
    checkpoint: str = ""
    commit: str = ""
    reverted: bool = False
    #: The step's own outcome, as reported by the agent that did the work.
    outcome: str = ""
    outcome_reason: str = ""

    @property
    def failed_gate(self) -> str | None:
        return next((g.gate for g in self.gate_results if g.verdict == "FAIL"), None)

    @classmethod
    def from_dict(cls, data: dict) -> "Attempt":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        clean["gate_results"] = [GateResult(**{k: v for k, v in (g or {}).items()
                                               if k in GateResult.__dataclass_fields__})
                                 for g in (data or {}).get("gate_results") or []]
        return cls(**clean)


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
    #: Every check to run after the work, in order — a chain, because one step
    #: usually wants more than one kind of proof. A factchecker confirms the
    #: files exist; a regression check confirms nothing that used to work
    #: stopped. The first FAIL sends the step back and the whole chain runs
    #: again afterwards, so a fix that breaks an earlier check cannot pass.
    #:
    #: The engine has always looped over a list here; it was only ever handed
    #: one, because `check` held a single name.
    checks_chain: list[str] = field(default_factory=list)
    #: Whether this step's chain has already been filled in from its agent's
    #: standing checks. Once it has, the chain on the step is the whole truth:
    #: taking a check off in the plan means it does not run, rather than being
    #: quietly put back by the agent that suggested it.
    checks_seeded: bool = False
    #: Who tries to fix a failed check. Empty means this step's own role has
    #: another go. Either way the block then runs again.
    on_fail: str | None = None
    #: How many times the block may run before the flow is halted, overriding
    #: the agent's own count. 0 means "however many tries that agent gets",
    #: which is the usual case — the agent knows what it is worth retrying.
    max_loops: int = 0
    #: Start on the agent's backup model rather than working up to it. Set by
    #: "rerun on the backup" — when you already know the usual model cannot do
    #: this, spending its tries first is only slower.
    start_on_backup: bool = False
    #: Roll the project back to the checkpoint taken before this step when it
    #: fails. Off by default: throwing away work is not something to do on a
    #: guess, and a half-finished step is often still worth reading.
    revert_on_fail: bool = False
    #: The inverse commit of the last user revert of this step, so "I reverted
    #: by mistake" has something to re-apply. Cleared when it is applied back.
    reverted_sha: str = ""
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
    #: Screenshots the user attached to the request this step came from,
    #: as paths under .trance/shots. The evidence belongs to the task, not the
    #: run: retries, fixers and checks all see the same pictures.
    images: list[str] = field(default_factory=list)
    #: Working time this step has cost, in seconds, across every attempt,
    #: fixer pass and check. What "how long was this iteration worked on"
    #: sums over.
    seconds: float = 0.0
    #: How many times this step has been *executed*, which is not how many
    #: attempts it has made. One run holds every retry and, for a loop step,
    #: every block of every agent in it — it is one press of the button, and it
    #: is the unit anybody actually means by "what happened last time".
    runs: int = 0
    #: User steering notes waiting to reach this step's agent.
    steering: list[str] = field(default_factory=list)

    def take_steering(self) -> list[str]:
        """Hand over every waiting note, exactly once."""
        notes, self.steering = list(self.steering), []
        return notes
    summary: str = ""

    @property
    def checker(self) -> str | None:
        """The first reality check, however it was configured.

        Kept because a trace, a badge and half the API say "the checker" and
        mean the one whose verdict decides the attempt.
        """
        return ((self.checks_chain[0] if self.checks_chain else None)
                or self.check or (self.gates[0] if self.gates else None)
                or self.verify_with)

    @property
    def fixer(self) -> str:
        """Who addresses a failed check — a chosen agent, else this step's role."""
        return self.on_fail or self.role

    @property
    def loop_limit(self) -> int:
        """The override, or 2 when nothing knows better. The engine prefers the
        agent's own count when this step does not name one."""
        return max(1, self.max_loops or self.max_attempts or 2)

    @property
    def overrides_tries(self) -> bool:
        return bool(self.max_loops)

    @property
    def checks(self) -> list[str]:
        """Every check this step runs, in order.

        Falls back through the older shapes so a plan written before chains
        existed still verifies the way it always did.
        """
        if self.checks_chain:
            return [name for name in self.checks_chain if name]
        one = self.checker
        return [one] if one else []

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
        data["overrides_tries"] = self.overrides_tries
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        data = dict(data)
        # What the step already did, restored. Dropping it meant a restart left
        # every finished step with no history and no record of what it cost.
        attempts = [Attempt.from_dict(a) for a in (data.pop("attempts", None) or [])
                    if isinstance(a, dict)]
        for derived in ("checker", "fixer", "loop_limit", "runs_a_loop",
                        "overrides_tries"):
            data.pop(derived, None)
        # Older shapes fold into `check`.
        # A chain sent as `checks` is the real field now; the singular shapes
        # below are what older plans and older clients say.
        # `checks` is the field clients send; `checks_chain` is how it is
        # stored, and to_dict emits both. So presence decides, not truth: a
        # client saying "checks: []" is taking every check off, and reading
        # that as "said nothing" left the stored chain in place — which is how
        # a check removed on the plan screen came back on the next read.
        said_checks = "checks" in data
        chain = [name for name in (data.pop("checks", None) or []) if name]
        if said_checks:
            data["checks_chain"] = chain
        if not data.get("check"):
            gates = data.get("gates") or []
            data["check"] = (gates[0] if gates else None) or data.get("verify_with")
        known = {f for f in cls.__dataclass_fields__}
        step = cls(**{k: v for k, v in data.items() if k in known})
        step.attempts = attempts
        return step


def merge_checks(own: list[str], always: list[str]) -> list[str]:
    """One chain from a step's own checks and its agent's standing ones.

    The agent's list decides the order of every name in it: that order was
    typed by a person and means something — cheap checks before slow ones, no
    point reviewing code whose tests have not run. A check only this step asked
    for runs first, being the most specific thing about this one task.
    """
    always = list(dict.fromkeys(name for name in always if name))
    first = [name for name in dict.fromkeys(own) if name and name not in always]
    return first + always


def seed_checks(flow: "Flow", checks_of) -> int:
    """Copy each agent's standing checks onto its steps. Returns how many.

    A chain merged at run time is a chain nobody can see or change: the plan
    showed one thing and the engine ran another. Copying it onto the step makes
    the plan honest — and makes it editable, since from then on the step is
    what runs.

    Done once per step, deliberately. Re-seeding on every load would put back
    every check anyone took off, which is the whole point of being able to.
    A step whose agent has no checks stays unseeded, so adding one to that
    agent later still reaches the steps already planned.
    """
    filled = 0
    for step in flow.steps:
        if step.checks_seeded or step.loop or not step.role:
            continue
        always = [name for name in (checks_of(step.role) or []) if name]
        if not always:
            continue
        step.checks_chain = merge_checks(step.checks, always)
        step.checks_seeded = True
        filled += 1
    return filled


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

    def keep_finished(self, proposed: list["Step"]) -> dict:
        """Take a fresh proposal without throwing away what already ran.

        The orchestrator answers "I found bugs" with a whole new plan, and
        replacing the flow with it deleted every finished step — the record of
        what was built, with its attempts, its commits and its trace. Work that
        happened is not a draft.

        So anything already done, failed, blocked or in flight stays where it
        is, and the proposal contributes what is genuinely new. A proposed step
        matching one that already ran is dropped: that is the orchestrator
        describing the past, not asking for it again.
        """
        kept = [s for s in self.steps
                if s.status in self.TERMINAL or s.status in self.LOCKED]
        seen = {(s.role, s.loop, s.task.strip()) for s in kept}
        fresh = [s for s in proposed
                 if (s.role, s.loop, s.task.strip()) not in seen]
        self.steps = kept + fresh
        return {"kept": len(kept), "added": len(fresh),
                "dropped": len(proposed) - len(fresh)}

    def insert_next(self, step: "Step") -> int:
        """Put a step at the front of the queue, after whatever is running.

        For work that is about the code as it stands now — a review naming
        particular lines. Appended at the end it runs after every other pending
        step, by which time the lines it refers to have moved, and it reads as
        if the comment was ignored. Returns where it landed, 1-based.
        """
        after = 0
        for i, existing in enumerate(self.steps):
            if existing.status in self.LOCKED:
                after = i + 1            # never in front of something in flight
            elif existing.status == "pending":
                break
            else:
                after = i + 1            # done, failed, skipped: already behind us
        self.steps.insert(after, step)
        return after + 1

    #: A step whose agent is mid-flight cannot be edited — everything else can.
    LOCKED = ("running", "verifying")
    #: Statuses that mean "this will not run again unless you say so".
    TERMINAL = ("done", "failed", "blocked", "skipped")

    def apply_edits(self, steps: list[Step]) -> dict:
        """Apply an edited step list, keeping only in-flight steps immutable.

        The event trace is append-only regardless; a step is a *plan*, and a
        plan you cannot correct after it failed is not much use. So a failed,
        blocked or finished step is editable, and changing *what it does* —
        its agent, its loop, its task — re-queues it, because editing work you
        already ran means you want it run again. Changing how it is checked or
        how many tries it gets does not: that applies to the next run and is no
        reason to discard the last one.
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

            # Only a change to the *work* re-queues a finished step. The check,
            # the retry limit and the fixer describe how the step is run next
            # time; treating them as edits threw away completed work — a save
            # after the plan changed server-side put every done step back to
            # pending and wiped its history.
            changed = (
                existing.role != incoming.role
                or existing.loop != incoming.loop
                or existing.task != incoming.task
                or existing.entry != incoming.entry
            )
            existing.role, existing.task = incoming.role, incoming.task
            existing.loop = incoming.loop
            existing.check = incoming.checker
            # The whole chain, not just its first name. Copying only `check`
            # meant the chips on the plan screen quietly did nothing to a step
            # that already existed: the second and third check survived the
            # round trip only for steps being created.
            existing.checks_chain = list(incoming.checks)
            # Sticky: once an agent's checks have been copied onto a step, a
            # later edit cannot un-copy them, or the next read would put back
            # the check that was just taken off.
            existing.checks_seeded = incoming.checks_seeded or existing.checks_seeded
            existing.on_fail = incoming.on_fail
            existing.max_loops = incoming.max_loops
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
