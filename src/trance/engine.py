"""Flow engine: executes a session's steps, one at a time, steerably.

Sequential by design. The user's stated requirement is an *ordered* pipeline
(backend → test → frontend → test), and order is exactly what parallelism
destroys. Concurrency here would also make the live view unreadable.

Between every step the engine:
  * honours pause / stop,
  * picks up flow edits the user made in the UI,
  * re-indexes the project so the next agent gets curated context,
  * checks the finished step for remit violations and lets the orchestrator
    intervene.
"""

from __future__ import annotations

import difflib
import os
import threading
import traceback
from pathlib import Path

from .agents.runner import run_agent
from .config import Config
from .curator.walker import CuratorConfig, curate
from .db import GraphDB
from .events import EventBus
from .flow import Attempt, Step
from .indexer.service import default_db_path, index_repo
from .session import Session
from .worker.tools import ContextTools


def check_project_dir(raw: str) -> tuple[str | None, str]:
    """Validate a project directory before anything is created.

    A typo in this path used to surface as a PermissionError traceback partway
    into a run — after the orchestrator conversation and the flow were already
    built. Catching it at creation time turns that into one clear sentence.

    Returns (error_message_or_None, normalized_path).
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return (f"{raw!r} must be an absolute path (it resolves to {path.resolve()}).",
                str(path))
    path = Path(os.path.normpath(path))

    if path.exists():
        if not path.is_dir():
            return f"{path} exists but is not a directory.", str(path)
        if not os.access(path, os.W_OK):
            return f"{path} is not writable by this process.", str(path)
        return None, str(path)

    # Doesn't exist yet: find the deepest ancestor that does, and check we could
    # actually create the rest under it.
    ancestor = next((p for p in path.parents if p.exists()), None)
    if ancestor is None:
        return f"No part of {path} exists and none can be created.", str(path)
    if not os.access(ancestor, os.W_OK):
        missing = path.relative_to(ancestor)
        hint = ""
        siblings = _similar(ancestor, path.relative_to(ancestor).parts[0])
        if siblings:
            hint = f" Did you mean {', '.join(siblings)}?"
        return (f"Cannot create {path}: the nearest existing directory is {ancestor}, "
                f"which is not writable, so {missing} cannot be made there.{hint}"), str(path)
    return None, str(path)


def _similar(parent: Path, name: str) -> list[str]:
    """Close matches for a mistyped path segment, to catch /home/ppetrov."""
    try:
        entries = [p.name for p in parent.iterdir() if p.is_dir()]
    except OSError:
        return []
    return [str(parent / m) for m in difflib.get_close_matches(name, entries, n=2, cutoff=0.7)]


class FlowEngine:
    def __init__(self, session: Session, config: Config, bus: EventBus, on_change=None):
        self.session = session
        self.config = config
        self.bus = bus
        self.on_change = on_change or (lambda: None)
        self.project = Path(session.project_dir).expanduser().resolve()

    # ----------------------------------------------------------------- run

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name=f"flow-{self.session.id}", daemon=True)
        self.session._thread = thread
        thread.start()
        return thread

    def _run(self) -> None:
        session = self.session
        session.status = "running"
        session._stop.clear()
        self._emit("run_started", payload={"steps": len(session.flow.steps)})
        self.on_change()

        try:
            problem, _ = check_project_dir(str(self.project))
            if problem:
                raise RuntimeError(problem)
            self.project.mkdir(parents=True, exist_ok=True)
            while True:
                session.wait_if_paused()
                if session.stopping:
                    self._emit("run_stopped", payload={"reason": "stopped by user"})
                    session.status = "ready"
                    break

                step = session.flow.next_pending()
                if step is None:
                    session.status = "finished"
                    self._emit("run_finished", payload=session.flow.progress)
                    break

                try:
                    self._execute(step)
                except Exception as exc:  # noqa: BLE001
                    # One bad step (backend 500, context overflow, timeout) must
                    # not take the whole pipeline down — mark it and carry on.
                    step.status = "failed"
                    step.summary = f"{type(exc).__name__}: {exc}"
                    self._emit("step_failed", agent=step.role, step_id=step.id,
                               payload={"reason": step.summary,
                                        "traceback": traceback.format_exc()})
                self.on_change()
        except Exception as exc:  # noqa: BLE001 - surface everything to the UI
            session.status = "error"
            session.error = f"{type(exc).__name__}: {exc}"
            self._emit("error", payload={"message": session.error, "traceback": traceback.format_exc()})
        finally:
            session.status = "finished" if session.status == "running" else session.status
            self.on_change()

    # ---------------------------------------------------------------- step

    def _execute(self, step: Step) -> None:
        session = self.session
        role = session.role(step.role)
        if role is None:
            step.status = "failed"
            step.summary = f"unknown role {step.role!r}"
            self._emit("step_failed", step_id=step.id, payload={"reason": step.summary})
            return

        for attempt_n in range(1, step.max_attempts + 1):
            session.wait_if_paused()
            if session.stopping:
                step.status = "pending"
                return

            step.status = "running"
            attempt = Attempt(n=attempt_n)
            step.attempts.append(attempt)
            self._emit("step_started", agent=role.name, step_id=step.id,
                       payload={"task": step.task, "attempt": attempt_n, "role": role.to_dict()})
            self.on_change()

            bundle_text, bundle_meta = self._curate(step, role)
            if bundle_meta:
                self._emit("context_bundle", agent=role.name, step_id=step.id, payload=bundle_meta)

            steering = list(step.steering)
            step.steering.clear()
            if attempt_n > 1 and step.attempts[-2].feedback:
                steering.append(
                    "Your previous attempt was rejected by the verifier. Fix exactly this:\n"
                    + step.attempts[-2].feedback
                )

            turn = run_agent(
                role=role, task=step.task, project=self.project, config=self.config.for_role(role),
                bus=self.bus, session_id=session.id, step_id=step.id,
                context_bundle=bundle_text, steering=steering, history=session.history,
                graph_tools=self._graph_tools(role), should_stop=lambda: session.stopping,
            )
            attempt.worker_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
            attempt.files_written = turn.files_written
            step.summary = _summarize(turn.text)

            if turn.remit_violations:
                self._supervise(step, role, turn.remit_violations)

            self._record_history(role.name, step, turn)
            self._reindex()

            verdict = self._verify(step, attempt)
            if verdict != "FAIL":
                # "UNKNOWN" means verification did not happen — visible as
                # blocked, not quietly green.
                step.status = "blocked" if verdict == "UNKNOWN" else "done"
                self._emit("step_finished", agent=role.name, step_id=step.id,
                           payload={"status": step.status, "attempt": attempt_n,
                                    "files": turn.files_written, "summary": step.summary,
                                    "usage": turn.usage, "tool_calls": turn.tool_calls,
                                    "verdict": verdict})
                return

            self._emit("step_retry", agent=role.name, step_id=step.id,
                       payload={"attempt": attempt_n, "feedback": attempt.feedback})

        step.status = "failed"
        self._emit("step_failed", agent=role.name, step_id=step.id,
                   payload={"reason": "verifier kept failing", "attempts": len(step.attempts)})

    # ------------------------------------------------------------- verify

    def _verify(self, step: Step, attempt: Attempt) -> str | None:
        if not step.verify_with:
            return None
        verifier = self.session.role(step.verify_with)
        if verifier is None:
            return None
        if not getattr(verifier, "verifier", False):
            # An agent that cannot inspect anything cannot verify anything; it
            # would return a verdict it has no means of having checked.
            self._emit("warning", agent=verifier.name, step_id=step.id, payload={
                "message": (
                    f"{verifier.title} is not marked as a verifier, so it was not asked to "
                    f"check this step. Tick 'can verify' on that agent, or pick one of the "
                    f"agents that can inspect results."
                ),
            })
            return "UNKNOWN"

        self.session.wait_if_paused()
        if self.session.stopping:
            return None

        step.status = "verifying"
        self._emit("step_verifying", agent=verifier.name, step_id=step.id,
                   payload={"verifier": verifier.name})
        self.on_change()

        turn = run_agent(
            role=verifier,
            task=(
                f"Verify this work by another agent:\n\n{step.task}\n\n"
                f"What they reported:\n{step.summary}\n\n"
                f"Files they changed: {', '.join(attempt.files_written) or 'none'}"
            ),
            project=self.project, config=self.config.for_role(verifier), bus=self.bus,
            session_id=self.session.id, step_id=step.id, history=self.session.history,
            graph_tools=self._graph_tools(verifier), should_stop=lambda: self.session.stopping,
        )
        attempt.verifier_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
        # A verifier that produced no parseable verdict has NOT passed anything.
        # Defaulting to PASS here would let a confused verifier wave work
        # through silently, which is the worst failure this system can have.
        attempt.verdict = turn.verdict or "UNKNOWN"
        attempt.feedback = turn.text if attempt.verdict == "FAIL" else ""
        self._emit("verdict", agent=verifier.name, step_id=step.id,
                   payload={"verdict": attempt.verdict, "detail": _summarize(turn.text),
                            "wrote_files": attempt.files_written})
        if attempt.verdict == "UNKNOWN":
            self._emit("warning", agent=verifier.name, step_id=step.id, payload={
                "message": (
                    f"{verifier.title} returned no VERDICT line, so this step is unverified. "
                    "Treating it as blocked rather than passed — rerun the step or check the "
                    "verifier's model supports tool calling."
                ),
            })
        return attempt.verdict

    # --------------------------------------------------------- supervision

    def _supervise(self, step: Step, role, violations: list[str]) -> None:
        """An agent tried to write outside its remit — say who should own it."""
        owners: dict[str, str] = {}
        for path in violations:
            owner = next((r.name for r in self.session.team if r.may_write(path)), None)
            owners[path] = owner or "unassigned"
        self._emit(
            "supervision", agent="orchestrator", step_id=step.id,
            payload={
                "offender": role.name,
                "violations": owners,
                "message": (
                    f"{role.title} tried to write {', '.join(violations)}, outside its remit. "
                    + "; ".join(f"{p} belongs to {o}" for p, o in owners.items())
                ),
            },
        )

    # ------------------------------------------------------------- context

    def _graph_tools(self, role):
        if "graph" not in role.toolsets:
            return None
        db_path = default_db_path(self.project)
        if not db_path.exists():
            return None
        return ContextTools(GraphDB(db_path), self.project)

    def _curate(self, step: Step, role) -> tuple[str, dict | None]:
        """Curated context for steps that name an entry point in existing code."""
        if not step.entry:
            return "", None
        db_path = default_db_path(self.project)
        if not db_path.exists():
            return "", None
        db = GraphDB(db_path)
        try:
            bundle = curate(db, self.project, step.task, step.entry,
                            CuratorConfig(**vars(self.config.curator)))
        except LookupError:
            return "", {"entry": step.entry, "error": "entry point not found in the index"}
        finally:
            db.close()
        stats = bundle.stats()
        return bundle.render(), {
            "entry": bundle.entry,
            "stats": stats,
            "items": [
                {"qualname": i.qualname, "file_path": i.file_path, "hops": i.hops,
                 "include": i.include, "kind": i.kind}
                for i in bundle.items
            ],
            "rendered": bundle.render(),
            "unresolved": bundle.unresolved,
        }

    def _reindex(self) -> None:
        """Keep the graph fresh so later agents can curate against new code."""
        try:
            db = GraphDB(default_db_path(self.project))
            result = index_repo(self.project, db)
            counts = db.counts()
            db.close()
        except Exception as exc:  # indexing must never break a run
            self._emit("index", payload={"error": str(exc)})
            return
        self._emit("index", payload={"parsed": result.parsed, "unchanged": result.skipped, **counts})

    # -------------------------------------------------------------- record

    def _record_history(self, role_name: str, step: Step, turn) -> None:
        self.session.history.append({
            "step_id": step.id,
            "role": role_name,
            "task": step.task,
            "summary": step.summary,
            "files": turn.files_written,
        })

    def _emit(self, type_: str, **kwargs) -> None:
        self.bus.emit(type_, self.session.id, **kwargs)


def _summarize(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"
