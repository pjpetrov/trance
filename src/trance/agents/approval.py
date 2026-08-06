"""Asking the user before refusing an agent's action.

A remit or an allowlist is a good default and a bad absolute. The tester that
tried to write `jest.config.js` was not overstepping in any way that mattered —
it needed a test runner config, nobody had given it one, and the refusal cost a
whole step and eventually the run. But loosening the remit in advance is worse:
the point of the boundary is that it holds when the agent is wrong.

So the boundary now asks. The agent's thread blocks, the user gets the exact
action with three answers — once, always, no — and the run continues from
whichever they pick. `always` writes the decision back into the policy, so the
same question is asked once per project rather than once per step.

Blocking a worker thread on a human is only safe with a way out: every request
has a deadline, and reaching it denies, which is what would have happened
anyway. An unattended run therefore behaves exactly as it did before, just
slower by the timeout.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

#: What the user can answer.
ONCE, ALWAYS, DENY = "once", "always", "deny"
DECISIONS = (ONCE, ALWAYS, DENY)

#: How long a blocked agent waits. Long enough to walk back to the screen,
#: short enough that an overnight run is not held up for hours per question.
DEFAULT_TIMEOUT_S = 300.0


@dataclass
class ApprovalRequest:
    """One refused action, waiting on an answer."""

    id: str
    kind: str                    # "write" | "command"
    agent: str
    session_id: str
    step_id: str
    #: What the agent tried to do, in the words the user needs to judge it.
    subject: str                 # the path, or the command line
    detail: dict = field(default_factory=dict)
    decision: str = ""
    _answered: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "agent": self.agent,
                "step_id": self.step_id, "subject": self.subject,
                "detail": self.detail, "decision": self.decision}

    @property
    def allowed(self) -> bool:
        return self.decision in (ONCE, ALWAYS)


class ApprovalBroker:
    """Routes a refusal to the user and blocks until they answer.

    One per session. `on_request` publishes to the UI; `on_always` is what
    actually widens the policy, which lives in the server rather than here —
    this class knows how to ask, not what a permission is.
    """

    def __init__(self, on_request=None, on_resolved=None, on_always=None,
                 timeout_s: float = DEFAULT_TIMEOUT_S, enabled: bool = True):
        self.on_request = on_request or (lambda request: None)
        self.on_resolved = on_resolved or (lambda request: None)
        self.on_always = on_always or (lambda request: None)
        self.timeout_s = timeout_s
        self.enabled = enabled
        self._pending: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()
        #: Set when the run is stopping, so nothing is left blocked on a user
        #: who has already walked away.
        self._abandon = threading.Event()

    # ---------------------------------------------------------------- ask

    def ask(self, *, kind: str, agent: str, session_id: str, step_id: str,
            subject: str, detail: dict | None = None) -> ApprovalRequest:
        request = ApprovalRequest(
            id=f"ap_{uuid.uuid4().hex[:10]}", kind=kind, agent=agent,
            session_id=session_id, step_id=step_id, subject=subject,
            detail=detail or {})
        if not self.enabled or self._abandon.is_set():
            request.decision = DENY
            return request

        with self._lock:
            self._pending[request.id] = request
        self.on_request(request)
        try:
            answered = request._answered.wait(self.timeout_s)
            if not answered:
                # The deadline denies. Doing anything else would mean an
                # unattended run could take an action nobody sanctioned.
                request.decision = DENY
                request.detail["timed_out"] = True
        finally:
            with self._lock:
                self._pending.pop(request.id, None)
        self.on_resolved(request)
        return request

    # ------------------------------------------------------------- answer

    def resolve(self, request_id: str, decision: str) -> ApprovalRequest | None:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        with self._lock:
            request = self._pending.get(request_id)
        if request is None:
            return None
        request.decision = decision
        if decision == ALWAYS:
            # Widen the policy before releasing the agent, so the action it
            # retries is already permitted rather than racing the write.
            self.on_always(request)
        request._answered.set()
        return request

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            return list(self._pending.values())

    def abandon(self) -> None:
        """Deny everything outstanding — the run is stopping."""
        self._abandon.set()
        with self._lock:
            waiting = list(self._pending.values())
        for request in waiting:
            request.decision = DENY
            request.detail["abandoned"] = True
            request._answered.set()

    def revive(self) -> None:
        self._abandon.clear()
