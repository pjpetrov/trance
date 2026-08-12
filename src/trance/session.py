"""A project session: the chat with the orchestrator, the team, the flow, the run.

One session == one project == one directory == one live view in the UI.
"""

from __future__ import annotations

import json
import time
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .agents.roles import AgentRole, BUILTIN_ROLES, default_team
from .flow import Flow

SessionStatus = ("planning", "ready", "running", "paused", "finished", "error")


@dataclass
class ChatMessage:
    role: str  # "user" | "orchestrator"
    content: str
    #: Screenshots attached to this message, as paths under the project's
    #: .trance/shots. A picture of the bug is often the whole report, and
    #: describing one in words is work the user should not have to do.
    images: list[str] = field(default_factory=list)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    #: Addressable, so a reply can be pointed at from elsewhere — the commits
    #: its plan produced, for one.
    id: str = field(default_factory=lambda: f"m_{uuid.uuid4().hex[:10]}")
    #: For an orchestrator reply that proposed work: where the code stood when
    #: it said so. Everything committed after this, up to the next proposal, is
    #: what that reply turned into.
    base: str = ""
    #: The steps that reply added, by id.
    steps: list[str] = field(default_factory=list)


@dataclass
class Session:
    name: str
    project_dir: str
    id: str = field(default_factory=lambda: f"s_{uuid.uuid4().hex[:10]}")
    status: str = "planning"
    #: What the whole project is, in the orchestrator's words. Every agent gets
    #: this: one that does not know the goal makes locally sensible, globally
    #: wrong choices — an API shape nothing downstream can use.
    goal: str = ""
    #: What the finished thing must do, written so each one can be settled by
    #: looking. The tester writes tests for these and the visual tester looks
    #: for them on screen, so they are the closest thing the run has to a
    #: definition of done.
    requirements: list[str] = field(default_factory=list)
    chat: list[ChatMessage] = field(default_factory=list)
    team: list[AgentRole] = field(default_factory=default_team)
    flow: Flow = field(default_factory=Flow)
    #: Rolling record of what each step produced, fed to later agents.
    history: list[dict] = field(default_factory=list)
    #: Seconds this flow has actually been working, across every start, pause,
    #: halt and rerun. A total rather than a stopwatch: a run that stopped
    #: twice and was restarted did not take a fresh five minutes, it took the
    #: sum of what it spent, and that is the number worth knowing.
    run_seconds: float = 0.0
    #: Working time by agent name, in seconds — who the run_seconds went to.
    agent_seconds: dict = field(default_factory=dict)
    #: Line comments the user has left but not yet sent as work.
    review: list[dict] = field(default_factory=list)
    #: Reviews already turned into steps, with the commit range that answered
    #: them — so "what did it do about my comments" has an exact answer.
    reviews: list[dict] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # Runtime-only; never serialized.
    _pause: threading.Event = field(default_factory=threading.Event, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: object = field(default=None, repr=False)
    #: Waits for a stopping engine to exit, then starts a fresh one. A stop only
    #: takes effect when the current model call returns, which can be minutes.
    _handover: object = field(default=None, repr=False)
    #: When the current working stretch began, or None while not working.
    _clock_from: float | None = field(default=None, repr=False)

    # ---------------------------------------------------------------- state

    def role(self, name: str) -> AgentRole | None:
        return next((r for r in self.team if r.name == name), None) or BUILTIN_ROLES.get(name)

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def pause(self) -> None:
        self._pause.set()
        self.stop_clock()          # paused is not working

    def resume(self) -> None:
        self._pause.clear()
        self.start_clock()

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()
        self.stop_clock()

    # ---------------------------------------------------------------- clock

    def start_clock(self) -> None:
        if self._clock_from is None:
            self._clock_from = time.monotonic()

    def stop_clock(self) -> None:
        """Bank the current stretch. Monotonic, so a clock change cannot
        make a run appear to have taken a negative amount of time."""
        if self._clock_from is not None:
            self.run_seconds += max(0.0, time.monotonic() - self._clock_from)
            self._clock_from = None

    @property
    def working(self) -> bool:
        return self._clock_from is not None

    @property
    def elapsed(self) -> float:
        """Everything banked, plus the stretch in progress."""
        if self._clock_from is None:
            return self.run_seconds
        return self.run_seconds + max(0.0, time.monotonic() - self._clock_from)

    def clear_stop(self) -> None:
        """Allow a stopped session to run again.

        `stop()` makes the engine thread exit, so resuming needs both this and
        a fresh engine — the old one is gone.
        """
        self._stop.clear()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def wait_if_paused(self) -> None:
        while self._pause.is_set() and not self._stop.is_set():
            self._pause.wait(0.2)

    # ------------------------------------------------------------ serialize

    def to_dict(self, include_flow: bool = True) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "project_dir": self.project_dir,
            "status": self.status,
            "goal": self.goal,
            "paused": self.paused,
            "chat": [vars(m) for m in self.chat],
            "team": [r.to_dict() for r in self.team],
            "history": self.history,
            "run_seconds": round(self.elapsed, 1),
            "agent_seconds": {name: round(spent, 1)
                              for name, spent in self.agent_seconds.items()},
            "working": self.working,
            "review": self.review,
            "reviews": self.reviews,
            "error": self.error,
            "created_at": self.created_at,
        }
        if include_flow:
            data["flow"] = self.flow.to_dict()
            data["progress"] = self.flow.progress
        return data

    @property
    def store_dir(self) -> Path:
        """Where this session's state lives: inside its own project.

        Everything about a session — the chat, the flow, the trace — belongs
        to the project it drove, so the session list is whatever the current
        workspace holds: a fresh workspace starts empty, and a project carried
        elsewhere carries its history with it.
        """
        return Path(self.project_dir) / ".trance" / "sessions" / self.id

    def save(self) -> Path:
        path = self.store_dir / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Session":
        data = json.loads(Path(path).read_text(encoding="utf8"))
        session = cls(
            id=data["id"], name=data["name"], project_dir=data["project_dir"],
            status=data.get("status", "planning"), created_at=data.get("created_at", ""),
            history=data.get("history", []), goal=data.get("goal", ""),
            review=data.get("review", []), reviews=data.get("reviews", []),
            run_seconds=float(data.get("run_seconds") or 0.0),
            agent_seconds=dict(data.get("agent_seconds") or {}),
        )
        session.chat = [ChatMessage(**m) for m in data.get("chat", [])]
        session.team = [AgentRole.from_dict(r) for r in data.get("team", [])] or default_team()
        session.flow = Flow.from_dict(data.get("flow", {}))

        # Nothing is executing at load time, so a step saved mid-flight — the
        # process was killed, restarted, or crashed — is not running whatever
        # the file says. Left alone it is worse than wrong: it shows as running
        # forever, next_pending() skips it because it is not pending, and a
        # second stranded step means the flow appears to be running two at once.
        for step in session.flow.steps:
            if step.status in Flow.LOCKED:
                step.status = "pending"
        return session


class SessionStore:
    """In-memory registry of the workspace's sessions.

    Session state lives inside each project (`.trance/sessions/<id>/`), so
    the store *discovers* rather than owns: it scans the workspace's projects
    on startup, and what it finds is what the UI lists. `root` is the old
    flat store, kept only to migrate what it still holds and to receive the
    trace of a session that has been deleted mid-run.
    """

    def __init__(self, root: Path, workspace: Path | None = None):
        self.root = Path(root)
        self.workspace = Path(workspace) if workspace else self.root
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._adopt_legacy()
        self._load_existing()

    def _adopt_legacy(self) -> None:
        """Move sessions stored the old way — one global dir keyed by id —
        into their project, where they belong.

        The old layout is why a new workspace greeted its user with every
        session from every other one: the list came from a global dir that
        changing the workspace never touched. Whole-directory moves, so the
        event trace travels with the state. A session whose project is gone
        stays where it is, listed nowhere: there is nothing to attach it to,
        and deleting it is not this code's decision to make.
        """
        for path in sorted(self.root.glob("*/session.json")):
            try:
                session = Session.load(path)
                target = session.store_dir
                if target.exists():                # adopted on an earlier start
                    shutil.rmtree(path.parent, ignore_errors=True)
                    continue
                if not Path(session.project_dir).is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path.parent), str(target))
            except Exception:                      # noqa: BLE001 — a broken entry
                continue                           # must not block the rest

    def _load_existing(self) -> None:
        for path in sorted(self.workspace.glob("*/.trance/sessions/*/session.json")):
            try:
                session = Session.load(path)
            except Exception:
                continue
            # A renamed or moved project still holds its sessions; the stored
            # project_dir is the only thing pointing at the old name.
            lives_in = path.parents[3]
            if Path(session.project_dir).resolve() != lives_in.resolve():
                session.project_dir = str(lives_in)
            if session.status in ("running", "paused"):
                session.status = "ready"  # nothing is running after a restart
            self._sessions[session.id] = session

    def create(self, name: str, project_dir: str) -> Session:
        session = Session(name=name, project_dir=project_dir)
        with self._lock:
            self._sessions[session.id] = session
        session.save()
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def all(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)

    def save(self, session: Session) -> None:
        session.save()

    def delete(self, session_id: str) -> bool:
        """Forget a session and remove it from disk.

        Popping the in-memory entry alone is not a delete: `_load_existing`
        rebuilds the store from disk on startup, so a session deleted in the UI
        would reappear after a restart.
        """
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.stop()
        shutil.rmtree(session.store_dir, ignore_errors=True)
        # And whatever the old flat layout still holds under this id.
        shutil.rmtree(self.root / session_id, ignore_errors=True)
        return True
