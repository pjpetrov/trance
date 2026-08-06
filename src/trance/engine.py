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
from .agents.memory import COMPACT_PROMPT, ProjectMemory, write_plan
from .agents.runner import OUTCOME_SUCCESS, run_agent
from .config import Config
from .providers import client_for
from .curator.walker import CuratorConfig, curate
from .db import GraphDB
from .events import EventBus
from .agents.roles import BUILTIN_ROLES
from .flow import Attempt, GateResult, Step
from .loops import CHECK_FAILED, EXIT_LOOP, FAILED, FAIL_LOOP, SUCCESS
from .indexer.service import default_db_path, index_repo
from .session import Session
from .agents.tools import stop_background
from .worker.tools import ContextTools, project_map


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
    def __init__(self, session: Session, config: Config, bus: EventBus, on_change=None,
                 approve=None, loops=None):
        self.session = session
        self.config = config
        self.bus = bus
        self.on_change = on_change or (lambda: None)
        self.project = Path(session.project_dir).expanduser().resolve()
        #: The team's shared notebook, in the project so the user can read it.
        self.memory = ProjectMemory(self.project)
        #: Asks the user before a refusal becomes final. None = refuse outright.
        self.approve = approve
        #: The loop library, for steps that run one instead of a single agent.
        self.loops = loops

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
            # Index up front. Indexing only *after* each step meant the first
            # agent of a run never had a graph to query, and on a fresh project
            # was not even given the graph tools.
            self._reindex()
            self._write_plan()
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
                self._compact_memory()
                self._write_plan()      # keep the ticks in step with the run
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
        if step.runs_a_loop:
            return self._execute_loop(step)
        return self._execute_agent(step)

    # ---------------------------------------------------------------- loops

    def _execute_loop(self, step: Step) -> None:
        """Walk a named loop: each agent's outcome decides who runs next.

        The step's own retry answers "try again, maybe with a fixer". A loop
        answers the shape that actually recurs — tester finds a bug, developer
        fixes it, tester runs again — by naming it once and reusing it.
        """
        loop = self.loops.get(step.loop) if self.loops else None
        if loop is None:
            step.status = "failed"
            step.summary = f"unknown loop {step.loop!r}"
            self._emit("step_failed", step_id=step.id, payload={"reason": step.summary})
            self._halt(step)
            return

        node = loop.entry
        visits: dict[tuple[str, str], int] = {}
        carry: Handoff | None = None
        walked = 0

        while node is not None and walked < loop.max_steps:
            self.session.wait_if_paused()
            if self.session.stopping:
                step.status = "pending"
                return

            role = self.session.role(node.role) or BUILTIN_ROLES.get(node.role)
            if role is None:
                step.status = "failed"
                step.summary = f"the {step.loop} loop names an unknown agent {node.role!r}"
                self._emit("step_failed", step_id=step.id, payload={"reason": step.summary})
                self._halt(step)
                return

            walked += 1
            step.status = "running"
            attempt = Attempt(n=len(step.attempts) + 1)
            step.attempts.append(attempt)
            self._emit("loop_node", agent=role.name, step_id=step.id, payload={
                "loop": loop.name, "node": node.id, "role": role.name,
                "visit": walked, "of": loop.max_steps, "focus": node.focus,
                "message": f"{loop.name}: {role.name} (block {walked} of at most {loop.max_steps})",
            })
            self.on_change()

            steering = list(step.steering)
            step.steering.clear()
            if carry and carry.body:
                steering.append(f"What the previous block did:\n{carry.body}")

            turn = run_agent(
                role=role,
                # Three prompts, narrowing: what the project is, what this step
                # asks for, and what this agent's part in the loop is.
                task=f"{step.task}\n\n## Your part in this block\n{node.focus or role.description}",
                project=self.project, config=self.config.for_role(role), bus=self.bus,
                session_id=self.session.id, step_id=step.id,
                steering=steering, history=self.session.history,
                graph_tools=self._graph_tools(role),
                should_stop=lambda: self.session.stopping,
                memory=self.memory, project_map=self._project_map(role, step.task),
                goal=self._loop_goal(loop), placement=self._placement(step),
                approve=self.approve,
            )
            attempt.worker_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
            attempt.files_written = turn.files_written
            attempt.refused_paths = list(dict.fromkeys(turn.remit_violations))
            step.summary = _summarize(turn.text)
            self._record_history(role.name, step, turn)
            for command in stop_background():
                self._emit("background_stopped", agent=role.name, step_id=step.id,
                           payload={"command": command,
                                    "message": f"Stopped leftover background process: {command}"})
            self._reindex()

            outcome, reason = turn.outcome
            attempt.outcome, attempt.outcome_reason = outcome, reason
            exit_name = SUCCESS if outcome == OUTCOME_SUCCESS else FAILED
            if outcome == OUTCOME_SUCCESS and node.check:
                step.check = node.check          # so _run_check knows who to ask
                if self._run_check(step, attempt) == "FAIL":
                    exit_name = CHECK_FAILED
            self._emit("step_outcome", agent=role.name, step_id=step.id, payload={
                "outcome": outcome, "reason": reason, "loop": walked,
                "of": loop.max_steps, "reported": turn.reported_outcome,
                "exit": exit_name,
            })

            carry = build_handoff(turn.transcript, turn.text)
            edge = node.edge(exit_name)
            if edge.target == EXIT_LOOP:
                step.status = "done"
                self._emit("step_finished", agent=role.name, step_id=step.id, payload={
                    "status": "done", "attempt": attempt.n, "files": turn.files_written,
                    "summary": step.summary, "usage": turn.usage, "loop": loop.name,
                    "tool_calls": turn.tool_calls, "outcome": outcome,
                })
                return
            if edge.target == FAIL_LOOP:
                break

            key = (node.id, exit_name)
            visits[key] = visits.get(key, 0) + 1
            if visits[key] > edge.max_visits:
                self._emit("loop_exhausted", agent=role.name, step_id=step.id, payload={
                    "loop": loop.name, "edge": f"{node.role} {exit_name}",
                    "max_visits": edge.max_visits,
                    "message": (f"{node.role}'s {exit_name} route has been taken "
                                f"{edge.max_visits} times — the loop is not converging."),
                })
                break
            node = loop.node(edge.target)

        step.status = "failed"
        last = step.attempts[-1] if step.attempts else None
        self._emit("step_failed", agent=step.role, step_id=step.id, payload={
            "reason": (f"the {step.loop} loop ended without success"
                       + (f" — last: {last.outcome_reason}" if last and last.outcome_reason
                          else "")),
            "attempts": len(step.attempts), "halts_flow": True, "loop": step.loop,
        })
        self._halt(step)

    def _loop_goal(self, loop) -> str:
        """The project goal, plus what this loop is for."""
        parts = [p for p in (self.session.goal, loop.prompt) if p]
        return "\n\n".join(parts)

    def _execute_agent(self, step: Step) -> None:
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
                memory=self.memory, project_map=self._project_map(role, step.task),
                goal=session.goal, placement=self._placement(step),
                approve=self.approve,
            )
            attempt.worker_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
            attempt.files_written = turn.files_written
            step.summary = _summarize(turn.text)

            attempt.refused_paths = list(dict.fromkeys(turn.remit_violations))
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

        # Every loop has failed. Before halting, one attempt with whatever the
        # user configured as their stronger model — the loop varied the prompt
        # and the fixer, and the model is the one thing it never varied.
        if self._escalate(step, feedback, carry):
            return

        step.status = "failed"
        last = step.attempts[-1] if step.attempts else None
        self._emit("step_failed", agent=role.name, step_id=step.id, payload={
            "reason": (f"the step never reported success in {limit} loop(s)"
                       + (f" — last: {last.outcome_reason}" if last and last.outcome_reason
                          else "")),
            "attempts": len(step.attempts), "max_loops": limit, "halts_flow": True,
        })
        self._halt(step)

    def _escalate(self, step: Step, feedback: str, carry) -> bool:
        """One final attempt on the escalation model. True if it succeeded.

        Bounded to exactly one: escalation that can itself loop is just a longer
        loop with a bigger bill.
        """
        preset = self.config.escalation_preset
        if not preset or step.escalated:
            return False
        role = self.session.role(self.config.escalation_role or step.role)
        if role is None:
            return False

        step.escalated = True
        step.status = "running"
        attempt = Attempt(n=len(step.attempts) + 1)
        step.attempts.append(attempt)
        model_config = self.config.resolve(self.config.worker, preset=preset)
        self._emit("escalated", agent=role.name, step_id=step.id, payload={
            "model": model_config.model, "preset": preset, "role": role.name,
            "after_loops": step.loop_limit,
            "message": (f"{step.loop_limit} loops did not fix this — trying once more "
                        f"with {model_config.model}."),
        })
        self.on_change()

        # What every previous attempt reported, not just the last. The point of
        # a stronger model here is to see the pattern across the failures.
        tried = "\n".join(
            f"  attempt {a.n}: {a.outcome or 'no outcome'} — {a.outcome_reason or a.feedback or '?'}"
            for a in step.attempts[:-1])
        turn = run_agent(
            role=role,
            task=(
                f"{step.task}\n\n"
                f"## This has already failed {len(step.attempts) - 1} times\n{tried}\n\n"
                f"Last reported: {feedback}\n\n"
                f"Do not repeat the approach that failed. Work out why it keeps failing "
                f"before changing anything — reproduce it, read the code that is actually "
                f"running, and check assumptions the earlier attempts took for granted. "
                f"A different design that works beats the intended one that does not."),
            project=self.project, config=model_config, bus=self.bus,
            session_id=self.session.id, step_id=step.id,
            history=self.session.history, graph_tools=self._graph_tools(role),
            should_stop=lambda: self.session.stopping,
            memory=self.memory, project_map=self._project_map(role, step.task),
            goal=self.session.goal, placement=self._placement(step),
            approve=self.approve,
            steering=[carry.body] if carry and carry.body else None,
        )
        attempt.worker_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
        attempt.files_written = turn.files_written
        attempt.outcome, attempt.outcome_reason = turn.outcome
        step.summary = _summarize(turn.text)
        self._record_history(role.name, step, turn)
        self._reindex()

        if attempt.outcome != OUTCOME_SUCCESS:
            self._emit("escalation_failed", agent=role.name, step_id=step.id, payload={
                "reason": attempt.outcome_reason, "model": model_config.model})
            return False

        integrity = self._run_check(step, attempt)
        step.status = "blocked" if integrity == "UNKNOWN" else "done"
        self._emit("step_finished", agent=role.name, step_id=step.id, payload={
            "status": step.status, "attempt": attempt.n, "files": turn.files_written,
            "summary": step.summary, "usage": turn.usage, "escalated": True,
            "tool_calls": turn.tool_calls, "outcome": attempt.outcome,
            "integrity": integrity,
        })
        return step.status == "done"

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
            memory=self.memory, project_map=self._project_map(fixer, step.task),
            goal=self.session.goal,
            approve=self.approve,
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
            # A step asking for files its agent may not write cannot succeed at
            # any loop limit. Say so, and name who can, instead of suggesting
            # the user retry something that is impossible by construction.
            refused = [p for a in step.attempts for p in a.refused_paths]
            if refused:
                owners: dict[str, list[str]] = {}
                for path in dict.fromkeys(refused):
                    owners.setdefault(self._owner_of(path) or "no agent", []).append(path)
                who = "; ".join(f"{name} owns {', '.join(paths)}"
                                for name, paths in owners.items())
                self.session.error += (
                    f" It was refused writes outside its remit: "
                    f"{', '.join(dict.fromkeys(refused))}.")
                hint = (f"Looping cannot fix this — the writes are refused by the system, "
                        f"not failing. {who}. Reassign this step, or split off the part "
                        f"{step.role} does own. Add the owning agent to the team in "
                        f"👥 Agents if it is not there.")
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
                memory=self.memory, project_map=self._project_map(gate, step.task),
                goal=self.session.goal,
                approve=self.approve,
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

    def _owner_of(self, path: str) -> str | None:
        """Which agent may write this path — on the team, or in the library.

        Looking only at the team is how "nobody owns .gitignore" became
        "unassigned" when the answer was "add devops to the team".
        """
        on_team = next((r.name for r in self.session.team if r.may_write(path)), None)
        if on_team:
            return on_team
        return next((name for name, role in BUILTIN_ROLES.items() if role.may_write(path)), None)

    def _supervise(self, step: Step, role, violations: list[str]) -> None:
        """An agent tried to write outside its remit — say who should own it."""
        owners: dict[str, str] = {}
        for path in violations:
            owners[path] = self._owner_of(path) or "unassigned"
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

    def _write_plan(self) -> None:
        write_plan(self.project, self.session.goal, self.session.flow.steps)

    def _compact_memory(self) -> None:
        """Keep the shared memory small enough to belong in every prompt.

        Between steps, never during one: an agent whose facts changed mid-turn
        would be reasoning from two different memories in the same conversation.
        """
        if not self.memory.oversized():
            return
        model_config = self.config.for_orchestrator()

        def rewrite(text: str) -> str:
            response = client_for(model_config).complete([
                {"role": "system", "content": COMPACT_PROMPT},
                {"role": "user", "content": text},
            ])
            return response.text

        result = self.memory.compact(rewrite)
        self._emit("memory_compacted", agent="orchestrator", payload=result)

    def _placement(self, step: Step) -> str:
        """Where this step sits, and — deliberately — what comes after it.

        Hiding the next step does not stop an agent running ahead; it only
        stops it knowing what its output has to serve, so it invents an
        interface the next agent cannot use. Naming the next step and saying
        whose it is turns "I may as well add this too" into a boundary, which
        is the same reason the remit is stated rather than merely enforced.
        """
        steps = self.session.flow.steps
        try:
            index = steps.index(step)
        except ValueError:
            return ""

        lines = [f"This is step {index + 1} of {len(steps)}."]
        following = steps[index + 1:index + 3]
        if following:
            lines.append("After you, in order:")
            lines += [f"  {i + index + 2}. {s.role} — {s.task}" for i, s in enumerate(following)]
            lines.append(
                f"That work belongs to {', '.join(sorted({s.role for s in following}))}, not "
                f"to you. Do not start it. What you should take from it is what your work "
                f"has to expose so the next step can build on it — if you are choosing a "
                f"name, a route or a format they will have to match, write it down with "
                f"remember.")
        else:
            lines.append("This is the last step: nothing runs after it, so leave the "
                         "project in a finished state rather than a handover.")
        return "\n".join(lines)

    def _project_map(self, role, task: str = "") -> str:
        """What is indexed, for a role that can query it."""
        if "graph" not in role.toolsets:
            return ""
        db_path = default_db_path(self.project)
        if not db_path.exists():
            return ""
        db = GraphDB(db_path)
        try:
            return project_map(db, focus=task)
        finally:
            db.close()

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
