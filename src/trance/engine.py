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

from .agents.handoff import Handoff, build as build_handoff
from .agents.runner import run_agent
from .config import Config
from .curator.walker import CuratorConfig, curate
from .db import GraphDB
from .events import EventBus
from .flow import Attempt, GateResult, Step
from .indexer.service import default_db_path, index_repo
from .session import Session
from .agents.tools import stop_background
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
        """Run one block, looping worker -> check -> fixer until it passes.

        The loop can only be left by succeeding. Exhausting `max_loops` halts
        the whole run rather than moving on, because a later step that depends
        on this one would otherwise build on work that never passed its check.
        """
        session = self.session
        role = session.role(step.role)
        if role is None:
            step.status = "failed"
            step.summary = f"unknown role {step.role!r}"
            self._emit("step_failed", step_id=step.id, payload={"reason": step.summary})
            return

        limit = step.loop_limit
        feedback = ""
        #: What the previous pass did, carried into the next one.
        carry: Handoff | None = None

        for loop in range(1, limit + 1):
            session.wait_if_paused()
            if session.stopping:
                step.status = "pending"
                return

            step.status = "running"
            attempt = Attempt(n=loop)
            step.attempts.append(attempt)
            self._emit("step_started", agent=role.name, step_id=step.id, payload={
                "task": step.task, "attempt": loop, "loop": loop, "of": limit,
                "role": role.to_dict(),
            })
            self.on_change()

            bundle_text, bundle_meta = self._curate(step, role)
            if bundle_meta:
                self._emit("context_bundle", agent=role.name, step_id=step.id, payload=bundle_meta)

            steering = list(step.steering)
            step.steering.clear()
            if feedback:
                # A re-run starts a fresh conversation, so this agent has no
                # memory of its own previous pass either. Hand it back.
                replay = (f"\n\nWhat was done on that pass:\n{carry.body}"
                          if carry and carry.body else "")
                steering.append(
                    f"This step did not succeed on the previous pass. What was "
                    f"reported:\n{feedback}{replay}"
                )

            turn = run_agent(
                role=role, task=step.task, project=self.project,
                config=self.config.for_role(role), bus=self.bus,
                session_id=session.id, step_id=step.id, context_bundle=bundle_text,
                steering=steering, history=session.history,
                graph_tools=self._graph_tools(role),
                should_stop=lambda: session.stopping,
            )
            attempt.worker_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
            attempt.files_written = turn.files_written
            step.summary = _summarize(turn.text)

            if turn.remit_violations:
                self._supervise(step, role, turn.remit_violations)
            self._record_history(role.name, step, turn)
            # Anything the agent left running would hold its port against the
            # next step, and nothing else reaps it.
            for command in stop_background():
                self._emit("background_stopped", agent=role.name, step_id=step.id,
                           payload={"command": command,
                                    "message": f"Stopped leftover background process: {command}"})
            self._reindex()

            # Two independent questions. The step's own outcome: did the work
            # succeed? A tester that writes a good test and finds a real bug did
            # its job and the step still failed. And the fact check: is the
            # agent's report true?
            outcome, reason = turn.outcome
            attempt.outcome = outcome
            attempt.outcome_reason = reason
            self._emit("step_outcome", agent=role.name, step_id=step.id, payload={
                "outcome": outcome, "reason": reason, "loop": loop, "of": limit,
                "reported": turn.reported_outcome,
            })

            integrity = self._run_check(step, attempt)

            if integrity == "FAIL" and outcome == "SUCCESS":
                # It claimed the work was done and the check says otherwise.
                # Looping would just repeat a report we cannot trust.
                step.status = "failed"
                self._emit("step_failed", agent=role.name, step_id=step.id, payload={
                    "reason": (f"{role.title} reported success but {step.checker} found "
                               f"otherwise: {attempt.feedback[:200]}"),
                    "attempts": len(step.attempts), "halts_flow": True, "lied": True,
                })
                self._halt(step, lied=True)
                return

            if outcome == "SUCCESS":
                step.status = "blocked" if integrity == "UNKNOWN" else "done"
                self._emit("step_finished", agent=role.name, step_id=step.id, payload={
                    "status": step.status, "attempt": loop, "files": turn.files_written,
                    "summary": step.summary, "usage": turn.usage,
                    "tool_calls": turn.tool_calls, "outcome": outcome,
                    "integrity": integrity,
                })
                return

            # The step reported a problem — that is what opens the loop, whether
            # or not a fact check ran.
            # The fixer acts on what the step reported, not on the fact
            # check — the check only ever decides whether to halt.
            feedback = reason or step.summary
            if loop >= limit:
                break

            # The loop: an agent tries to fix what the check found, then the
            # block runs again.
            carry = build_handoff(turn.transcript, turn.text)
            self._run_fixer(step, attempt, feedback, loop, limit, carry)

        step.status = "failed"
        last = step.attempts[-1] if step.attempts else None
        self._emit("step_failed", agent=role.name, step_id=step.id, payload={
            "reason": (f"the step never reported success in {limit} loop(s)"
                       + (f" — last: {last.outcome_reason}" if last and last.outcome_reason
                          else "")),
            "attempts": len(step.attempts), "max_loops": limit, "halts_flow": True,
        })
        self._halt(step)

    def _run_fixer(self, step: Step, attempt: Attempt, feedback: str,
                   loop: int, limit: int, handoff: Handoff | None = None) -> None:
        """Hand the failure to the fixing agent before the block runs again."""
        fixer = self.session.role(step.fixer)
        if fixer is None or fixer.name == step.role:
            self._emit("step_retry", agent=step.role, step_id=step.id, payload={
                "attempt": loop, "feedback": feedback,
                "message": f"{step.role} will try again (loop {loop + 1} of {limit}).",
            })
            return

        self.session.wait_if_paused()
        if self.session.stopping:
            return

        self._emit("fixing", agent=fixer.name, step_id=step.id, payload={
            "message": (f"{fixer.title} will try to fix what {step.role} reported, "
                        f"then {step.role} runs again (loop {loop + 1} of {limit})."),
            "loop": loop, "of": limit,
            # Shown in the step detail: exactly what the fixer was told, so a
            # fixer that flails is diagnosable without guessing.
            "handoff": handoff.body if handoff else "",
            "handoff_chars": handoff.chars if handoff else 0,
        })
        self.on_change()

        # The fixer starts cold, so what the failing agent learned has to travel
        # with the request. Without the commands it ran and their output, the
        # fixer's first move is always to reproduce a failure that was already
        # reproduced a minute ago.
        replay = (f"\n\n## What {step.role} actually did, in order\n{handoff.body}"
                  if handoff and handoff.body else "")
        turn = run_agent(
            role=fixer,
            task=(
                f"Another agent's work failed its check. Fix it.\n\n"
                f"## What the step was asked to do\n{step.task}\n\n"
                f"## What {step.role} reported\n{step.summary}\n\n"
                f"## Files it changed\n{', '.join(attempt.files_written) or 'none'}\n\n"
                f"## What went wrong\n{feedback}"
                f"{replay}\n\n"
                f"The replay above is what already happened — do not repeat it to "
                f"find out what is broken. Read the files you need, fix the cause of "
                f"that objection, and change only what is needed; {step.role} will run "
                f"again afterwards and the check will be repeated."
            ),
            project=self.project, config=self.config.for_role(fixer), bus=self.bus,
            session_id=self.session.id, step_id=step.id, history=self.session.history,
            graph_tools=self._graph_tools(fixer),
            should_stop=lambda: self.session.stopping,
        )
        attempt.fix_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
        attempt.fix_summary = _summarize(turn.text)
        attempt.files_written += turn.files_written
        self._record_history(fixer.name, step, turn)
        self._emit("fixed", agent=fixer.name, step_id=step.id, payload={
            "summary": attempt.fix_summary, "files": turn.files_written,
        })

    def _halt(self, step: Step, lied: bool = False) -> None:
        """Stop the run: later steps would build on work that is not there."""
        self.session.stop()
        self.session.status = "error"
        if lied:
            self.session.error = (
                f"Halted at the {step.role} step: it reported success, but "
                f"{step.checker} found the work was not actually done."
            )
            hint = ("Open the step to see what was claimed and what was found. "
                    "A report that cannot be trusted is not worth retrying — "
                    "check the model and prompt for that agent.")
        else:
            self.session.error = (
                f"Halted at the {step.role} step: it never reported success within "
                f"{step.loop_limit} loop(s)."
            )
            hint = ("Raise the loop limit, change the fixing agent, or edit the step "
                    "and re-run it.")
        self._emit("run_halted", agent=step.role, step_id=step.id, payload={
            "message": self.session.error, "hint": hint, "lied": lied,
        })

    # ------------------------------------------------------------- verify

    def _run_check(self, step: Step, attempt: Attempt) -> str | None:
        """Run each gate in order; the first FAIL stops the chain.

        Gates are sequential on purpose. If the tester fails there is no point
        asking the reviewer to read code that does not work yet, and the
        feedback the worker gets should be about one thing.
        """
        checks = [step.checker] if step.checker else []
        if not checks:
            self._emit("verification_skipped", agent=step.role, step_id=step.id, payload={
                "message": ("No fact check on this step, so the agent's own report of "
                            "the outcome is taken at face value."),
            })
            return None

        for index, name in enumerate(checks):
            if self.session.stopping:
                return None
            self.session.wait_if_paused()

            gate = self.session.role(name)
            if gate is None:
                self._emit("warning", step_id=step.id, payload={
                    "message": f"Check {name!r} is not a known agent; skipping it."})
                continue
            if not getattr(gate, "verifier", False):
                self._emit("warning", agent=gate.name, step_id=step.id, payload={
                    "message": (
                        f"{gate.title} is not marked as a verifier, so it was not asked to "
                        f"check this step. Tick 'can verify' on that agent, or choose one "
                        f"that can inspect results."),
                })
                attempt.gate_results.append(GateResult(gate=name, verdict="UNKNOWN"))
                continue

            step.status = "verifying"
            self._emit("step_verifying", agent=gate.name, step_id=step.id, payload={
                "verifier": gate.name, "gate": index + 1, "of": len(checks),
                "chain": checks,
            })
            self.on_change()

            turn = run_agent(
                role=gate,
                task=self._gate_task(step, attempt, gate, index, checks),
                project=self.project, config=self.config.for_role(gate), bus=self.bus,
                session_id=self.session.id, step_id=step.id, history=self.session.history,
                graph_tools=self._graph_tools(gate),
                should_stop=lambda: self.session.stopping,
            )
            verdict = turn.verdict or "UNKNOWN"
            result = GateResult(
                gate=name, verdict=verdict,
                feedback=turn.text if verdict != "PASS" else "",
                event_id=turn.model_event_ids[-1] if turn.model_event_ids else None,
            )
            attempt.gate_results.append(result)
            attempt.verifier_event_id = result.event_id

            self._emit("verdict", agent=gate.name, step_id=step.id, payload={
                "verdict": verdict, "gate": name, "position": index + 1, "of": len(checks),
                "detail": _summarize(turn.text), "wrote_files": attempt.files_written,
            })

            if verdict == "FAIL":
                attempt.verdict, attempt.feedback = "FAIL", turn.text
                self._emit("gate_failed", agent=gate.name, step_id=step.id, payload={
                    "gate": name,
                    "message": (
                        f"{gate.title} rejected the work, so it goes back to "
                        f"{step.role} to fix. The whole chain "
                        f"({' → '.join(checks)}) runs again afterwards."),
                })
                return "FAIL"
            if verdict == "UNKNOWN":
                self._emit("warning", agent=gate.name, step_id=step.id, payload={
                    "message": (
                        f"{gate.title} returned no VERDICT line, so this step is unverified. "
                        f"Treating it as blocked rather than passed."),
                })

        verdicts = [g.verdict for g in attempt.gate_results]
        attempt.verdict = "UNKNOWN" if "UNKNOWN" in verdicts else "PASS"
        attempt.feedback = ""
        return attempt.verdict

    def _gate_task(self, step: Step, attempt: Attempt, gate, index: int, checks: list) -> str:
        earlier = [g for g in attempt.gate_results if g.verdict == "PASS"]
        passed = (f"\n\nAlready passed: {', '.join(g.gate for g in earlier)}."
                  if earlier else "")
        return (
            f"Verify this work by another agent (check {index + 1} of {len(checks)}):\n\n"
            f"{step.task}\n\n"
            f"What they reported:\n{step.summary}\n\n"
            f"Files they changed: {', '.join(attempt.files_written) or 'none'}{passed}"
        )

    def _verify(self, step: Step, attempt: Attempt) -> str | None:
        """Backwards-compatible entry point."""
        return self._run_check(step, attempt)

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
