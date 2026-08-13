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
import time
import traceback
from pathlib import Path

from .agents.handoff import Handoff, build as build_handoff
from .agents.memory import COMPACT_PROMPT, ProjectMemory, write_plan
from .agents.runner import OUTCOME_SUCCESS, run_agent
from .config import Config
from .providers import BackendError, client_for
from .curator.walker import CuratorConfig, curate
from .db import GraphDB
from .events import EventBus
from .agents.roles import BUILTIN_ROLES
from .flow import Attempt, GateResult, Step, merge_checks
from .loops import CHECK_FAILED, EXIT_LOOP, FAILED, FAIL_LOOP, SUCCESS
from .indexer.service import default_db_path, index_repo
from .session import Session
from . import vcs
from .agents.tools import stop_background, stop_everything
from .worker.tools import ContextTools, project_map


def endpoint_failure(model: str, exc, backup: str, on_backup: bool) -> tuple[bool, str]:
    """Whether the endpoint refused the request, and what to say about it.

    A server that answers with a status was reached. Reporting a 400 as "could
    not be reached" sent a person looking at the network for a fault that was
    in the request — measured once: a conversation with two assistant turns at
    the end, refused in the same millisecond, reported as an unreachable model
    and escalated to a backup that could not have helped.

    Returns (refused, message).
    """
    refused = "returned 4" in str(exc)
    what = "refused the request" if refused else "could not be reached"
    next_step = (f"Trying {backup} next." if backup and not on_backup else "Trying again.")
    # The reason travels with it when there is one: an endpoint that refuses
    # says why, and that sentence is the whole diagnosis.
    detail = f" ({str(exc)[:160]})" if refused else ""
    return refused, f"{model} {what}. {next_step}{detail}"


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
        #: Whether this project can hold checkpoints at all. Decided once, at
        #: run start, so a step never pays for the discovery.
        self._git = False

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
        session.start_clock()
        pending = sum(1 for step in session.flow.steps if step.status == "pending")
        # With a message, because the console only draws events that have one —
        # so pressing Start printed nothing at all, and the first line arrived
        # whenever the first model answered. Half a minute of wondering whether
        # the click had registered is half a minute of clicking it again.
        self._emit("run_started", payload={
            "steps": len(session.flow.steps),
            "run_seconds": round(session.elapsed, 1),
            "message": (f"Run started — {pending} step(s) to do."
                        if pending else "Run started."),
        })
        self.on_change()

        try:
            problem, _ = check_project_dir(str(self.project))
            if problem:
                raise RuntimeError(problem)
            self.project.mkdir(parents=True, exist_ok=True)
            # Index up front. Indexing only *after* each step meant the first
            # agent of a run never had a graph to query, and on a fresh project
            # was not even given the graph tools.
            self._prepare_git()
            self._reindex()
            self._write_plan()
            while True:
                session.wait_if_paused()
                if session.stopping:
                    self._emit("run_stopped", payload={"reason": "stopped by user"})
                    session.status = "ready"
                    self._sweep_processes("the run was stopped")
                    break

                step = session.flow.next_pending()
                if step is None:
                    session.status = "finished"
                    self._emit("run_finished", payload=session.flow.progress)
                    self._sweep_processes("the run finished")
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
            # Whatever ended it — finished, stopped, halted, crashed — the flow
            # is not working any more, so the clock stops.
            session.stop_clock()
            self.on_change()

    # ---------------------------------------------------------------- step

    def _execute(self, step: Step) -> None:
        # One marker per execution, so a step's events can be cut into runs.
        # Nothing else in the stream does this: step_started fires once per
        # attempt and loop_node once per block, so neither says where one press
        # of Start or Rerun ends and the next begins — and "show me the last
        # run of this step" has to key on something.
        step.runs += 1
        self._emit("step_run_started", agent=step.role or step.loop, step_id=step.id,
                   payload={"run": step.runs, "task": step.task,
                            "role": step.role, "loop": step.loop,
                            "message": f"run {step.runs} — {step.loop or step.role}"})
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
            step.summary = (
                f"this step's loop {step.loop!r} no longer exists — it was deleted "
                f"or renamed after the plan was made. Pick another loop for the "
                f"step, or recreate {step.loop!r} in the Loops editor.")
            self._emit("step_failed", step_id=step.id,
                       payload={"reason": step.summary, "message": step.summary})
            self._halt(step)
            return

        node = loop.entry
        visits: dict[tuple[str, str], int] = {}
        #: How many times each agent has run in this block, for its backup.
        runs: dict[str, int] = {}
        carry: Handoff | None = None
        walked = 0
        on_backup_route = False        # set by a route that asks for the backup

        #: What the previous block photographed, for the next one to see.
        carry_shots: list[str] = []

        # "Rerun from this block": re-enter where the user pointed, replaying
        # the handoff that block received the first time — same input, fresh
        # visit budgets, and routing continues normally from its outcome.
        asked, step.resume_node = step.resume_node, ""
        replay, step.resume_handoff = step.resume_handoff, ""
        replay_shots, step.resume_shots = list(step.resume_shots), []
        if asked:
            wanted = loop.node(asked)
            if wanted is not None:
                node = wanted
                carry_shots = replay_shots
                if replay:
                    carry = Handoff(body=replay, chars=len(replay))
                self._emit("loop_resumed", agent=wanted.role, step_id=step.id, payload={
                    "loop": loop.name, "node": wanted.id, "role": wanted.role,
                    "message": (f"Back to {wanted.role}'s block, with the same "
                                f"handoff it had — the loop continues from there."),
                })
            else:
                self._emit("warning", step_id=step.id, payload={
                    "message": (f"The block to rerun is no longer in the "
                                f"{loop.name} loop — starting from the top "
                                f"instead.")})

        while node is not None and walked < loop.max_steps:
            self.session.wait_if_paused()
            if self.session.stopping:
                step.status = "pending"
                return

            role = self.session.role(node.role) or BUILTIN_ROLES.get(node.role)
            if role is None:
                step.status = "failed"
                step.summary = (
                    f"the {step.loop} loop names an agent {node.role!r} that no longer "
                    f"exists — deleted or renamed after the loop was made. Fix the "
                    f"loop's node in the Loops editor, or recreate the agent.")
                self._emit("step_failed", step_id=step.id,
                           payload={"reason": step.summary, "message": step.summary})
                self._halt(step)
                return

            walked += 1
            step.status = "running"
            attempt = Attempt(n=len(step.attempts) + 1, node=node.id)
            attempt.checkpoint = self._checkpoint(
                f"before {role.name} in {loop.name} — block {walked}")
            step.attempts.append(attempt)
            self._emit("loop_node", agent=role.name, step_id=step.id, payload={
                "loop": loop.name, "node": node.id, "role": role.name,
                "attempt": attempt.n,
                "visit": walked, "of": loop.max_steps, "focus": node.focus,
                "message": f"{loop.name}: {role.name} (block {walked} of at most {loop.max_steps})",
            })
            self.on_change()

            steering = step.take_steering()
            if carry and carry.body:
                steering.append(f"What the previous block did:\n{carry.body}")
                attempt.handoff = carry.body
            attempt.shots = list(carry_shots)

            runs[role.name] = runs.get(role.name, 0) + 1
            # A route's backup applies to the block it points at, and only that
            # one: the tier after it may well be an ordinary arrow again.
            forced, on_backup_route = on_backup_route, False
            model_config, on_backup = self._model_for(
                role, step, runs[role.name], force_backup=forced)
            node_t0 = time.monotonic()
            turn = run_agent(
                # The step's own screenshots plus what the previous block just
                # photographed — newest last, and the last three win, because
                # the tester's picture of the failure outranks a chat shot
                # from before the run.
                role=role, images=(list(step.images) + carry_shots)[-3:],
                # Three prompts, narrowing: what the project is, what this step
                # asks for, and what this agent's part in the loop is.
                task=f"{step.task}\n\n## Your part in this block\n{node.focus or role.description}",
                project=self.project, config=model_config, bus=self.bus,
                session_id=self.session.id, step_id=step.id,
                steering=steering, history=self.session.history,
                graph_tools=self._graph_tools(role),
                should_stop=lambda: self.session.stopping,
                memory=self.memory, project_map=self._project_map(role, step.task),
                goal=self._loop_goal(loop), requirements=self.session.requirements,
                placement=self._placement(step),
                approve=self.approve, reindex=self._reindex,
                steering_inbox=step.take_steering,
            )
            self._charge(role.name, step, node_t0)
            attempt.worker_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
            attempt.files_written = turn.files_written
            attempt.context = turn.context
            attempt.on_backup = on_backup
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
            attempt.commit = self._commit_step(step, role.name, outcome)
            exit_name = SUCCESS if outcome == OUTCOME_SUCCESS else FAILED
            node_chain = (list(node.checks) if node.checks_seeded
                          else merge_checks(node.checks, list(getattr(
                              role, "checks", None) or [])))
            if outcome == OUTCOME_SUCCESS and node_chain:
                step.check = node_chain[0]       # so the messages name someone real
                # This node's chain and nothing else — a chain left on the
                # step would run after every node instead. Seeded nodes are
                # the whole truth; unseeded ones still merge the agent's
                # standing checks, exactly as a plan step does.
                if self._run_check(step, attempt, chain=node_chain) == "FAIL":
                    exit_name = CHECK_FAILED
            # A fixer that made things worse should not hand its mess on.
            if exit_name != SUCCESS and node.revert_on_fail:
                self._revert(step, attempt, role.name)
            self._emit("step_outcome", agent=role.name, step_id=step.id, payload={
                "outcome": outcome, "reason": reason, "loop": walked,
                "of": loop.max_steps, "reported": turn.reported_outcome,
                "exit": exit_name, "reverted": attempt.reverted,
            })

            carry = build_handoff(turn.transcript, turn.text)
            # The last shots are where the evidence of a failure sits, and two
            # is already most of a small model's patience. A block that took
            # none passes none on — a fixer's turn must not relay the tester's
            # pictures as if they showed its fix.
            carry_shots = list(turn.shots)[-2:]
            key = (node.id, exit_name)
            edge = node.route(exit_name, visits.get(key, 0))
            if edge is None:
                # Every tier of this exit is spent. This is the deliberate end
                # of a tiered route, not a bug: the plan said stop here.
                allowed = node.allowance(exit_name)
                self._emit("loop_exhausted", agent=role.name, step_id=step.id, payload={
                    "loop": loop.name, "edge": f"{node.role} {exit_name}",
                    "max_visits": allowed,
                    # Two different truths: a spent budget, and an exit nobody
                    # wired. "Taken 0 time(s), not converging" was the second
                    # dressed as the first.
                    "message": (
                        f"{node.role}'s {exit_name} exit has no route in this "
                        f"loop — the loop stops here. Add one in the Loops "
                        f"editor." if allowed == 0 else
                        f"{node.role}'s {exit_name} route has been taken "
                        f"{allowed} time(s) — the loop is not converging."),
                })
                break
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

            visits[key] = visits.get(key, 0) + 1
            on_backup_route = edge.backup
            node = loop.node(edge.target)
            if on_backup_route and node is not None:
                self._emit("loop_route", agent=role.name, step_id=step.id, payload={
                    "loop": loop.name, "to": node.role, "backup": True,
                    "message": (f"{exit_name} #{visits[key]} routes to {node.role} "
                                f"on its backup model."),
                })

        step.status = "failed"
        last = step.attempts[-1] if step.attempts else None
        self._emit("step_failed", agent=step.role, step_id=step.id, payload={
            "reason": (f"the {step.loop} loop ended without success"
                       + (f" — last: {last.outcome_reason}" if last and last.outcome_reason
                          else "")),
            "attempts": len(step.attempts), "halts_flow": True, "loop": step.loop,
        })
        self._halt(step)

    def _charge(self, name: str, step: Step | None, started: float) -> None:
        """Book the time an agent just spent, to the agent and to the step.

        The session clock already counts the whole run; this says who it went
        to — which is the difference between "7h 53m" and knowing the visual
        tester ate five of them.
        """
        spent = max(0.0, time.monotonic() - started)
        self.session.agent_seconds[name] = (
            self.session.agent_seconds.get(name, 0.0) + spent)
        if step is not None:
            step.seconds += spent

    def _sweep_processes(self, why: str) -> None:
        """Kill everything any agent left running, and say so.

        Stop means stop: a process an agent started answering to nobody is not
        a feature. The per-step reap catches background commands; this catches
        whatever slipped past it, on every way out of a run.
        """
        for command in stop_everything():
            self._emit("background_stopped", payload={
                "command": command,
                "message": f"Stopped a process an agent left running ({why}): {command}",
            })

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
            step.summary = (
                f"this step's agent {step.role!r} no longer exists — it was deleted "
                f"or renamed after the plan was made. Assign another agent to the "
                f"step, or recreate {step.role!r} in the Agents editor.")
            self._emit("step_failed", step_id=step.id,
                       payload={"reason": step.summary, "message": step.summary})
            return

        limit = self._tries_for(role, step)
        feedback = ""
        #: Set when the endpoint itself failed, so the next try uses the backup
        #: whatever the try count says.
        endpoint_down = False
        #: What the previous pass did, carried into the next one.
        carry: Handoff | None = None

        for loop in range(1, limit + 1):
            session.wait_if_paused()
            if session.stopping:
                step.status = "pending"
                return

            step.status = "running"
            attempt = Attempt(n=loop)
            attempt.checkpoint = self._checkpoint(f"before {role.name} — {step.task[:60]}")
            step.attempts.append(attempt)
            self._emit("step_started", agent=role.name, step_id=step.id, payload={
                "task": step.task, "attempt": loop, "loop": loop, "of": limit,
                "role": role.to_dict(),
            })
            self.on_change()

            bundle_text, bundle_meta = self._curate(step, role)
            if bundle_meta:
                self._emit("context_bundle", agent=role.name, step_id=step.id, payload=bundle_meta)

            steering = step.take_steering()
            if feedback:
                # A re-run starts a fresh conversation, so this agent has no
                # memory of its own previous pass either. Hand it back.
                replay = (f"\n\nWhat was done on that pass:\n{carry.body}"
                          if carry and carry.body else "")
                steering.append(
                    f"This step did not succeed on the previous pass. What was "
                    f"reported:\n{feedback}{replay}"
                )

            model_config, on_backup = self._model_for(
                role, step, loop, force_backup=endpoint_down or step.start_on_backup)
            worker_t0 = time.monotonic()
            try:
                turn = run_agent(
                    role=role, task=step.task, project=self.project,
                    images=step.images,
                    config=model_config, bus=self.bus,
                    session_id=session.id, step_id=step.id, context_bundle=bundle_text,
                    steering=steering, history=session.history,
                    graph_tools=self._graph_tools(role),
                    should_stop=lambda: session.stopping,
                    memory=self.memory, project_map=self._project_map(role, step.task),
                    goal=session.goal, placement=self._placement(step),
                    approve=self.approve, reindex=self._reindex,
                    steering_inbox=step.take_steering,
                )
            except BackendError as exc:
                self._charge(role.name, step, worker_t0)
                # The endpoint failed, not the agent. That is a failed try, not
                # a failed step — and the next one goes to the backup, because a
                # model that is down does not recover from being asked again.
                endpoint_down = True
                attempt.outcome, attempt.on_backup = "FAILED", on_backup
                attempt.outcome_reason = f"the model endpoint failed: {exc}"
                feedback = attempt.outcome_reason
                backup = getattr(role, "backup_preset", "") or ""
                refused, note = endpoint_failure(
                    model_config.model, exc, backup, on_backup)
                self._emit("model_unreachable", agent=role.name, step_id=step.id, payload={
                    "error": str(exc), "model": model_config.model,
                    "attempt": loop, "of": limit, "backup": backup, "refused": refused,
                    "message": note,
                })
                self.on_change()
                if loop >= limit:
                    break
                continue
            self._charge(role.name, step, worker_t0)
            attempt.worker_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
            attempt.files_written = turn.files_written
            attempt.context = turn.context
            attempt.on_backup = on_backup
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
            # Commit first, then decide. A reverted step still has its work in
            # history, which is the difference between undoing and destroying.
            attempt.commit = self._commit_step(step, role.name, outcome)
            if outcome != OUTCOME_SUCCESS and step.revert_on_fail:
                self._revert(step, attempt, role.name)
            self._emit("step_outcome", agent=role.name, step_id=step.id, payload={
                "outcome": outcome, "reason": reason, "loop": loop, "of": limit,
                "reported": turn.reported_outcome,
            })

            # Only a claim of success is worth checking. The check exists to
            # ask "is that true?", and an agent that already said it failed has
            # not made a claim to test — running the checker there costs a model
            # call to confirm what was just admitted.
            integrity = self._run_check(step, attempt) if outcome == OUTCOME_SUCCESS else None

            if integrity == "FAIL" and outcome == "SUCCESS":
                # It reported success and the check disagrees. Usually that is
                # something forgotten rather than something invented — a file
                # not written, a claim about work that stopped half way — and
                # the agent can put it right if it is told what was missing.
                # So this opens the loop like any other failure, and only the
                # loop running out halts the flow.
                #
                # Named by whichever gate actually failed, not the first in
                # the chain. Measured live: the factchecker passed, the
                # reviewer rejected — and every message said "factchecker
                # found otherwise", sending the person reading it to the
                # wrong verdict.
                objector = next(
                    (g.gate for g in reversed(attempt.gate_results)
                     if g.verdict == "FAIL"), step.checker)
                attempt.outcome = "CHECK_FAILED"
                attempt.outcome_reason = (
                    f"{objector} checked the work and disagrees: {attempt.feedback}")
                self._emit("check_failed", agent=role.name, step_id=step.id, payload={
                    "checker": objector, "detail": attempt.feedback,
                    "attempt": loop, "of": limit,
                    # The truth about what happens next, in the same breath as
                    # the rejection. "Goes back to be fixed" was promised at
                    # the exact moment no tries remained, one line before
                    # step_failed.
                    "message": (f"{role.title} reported success but {objector} found "
                                f"otherwise. "
                                + ("Trying again." if loop < limit else
                                   f"No tries left ({loop} of {limit} spent) — "
                                   f"the step fails.")),
                })
                feedback = (
                    f"You reported SUCCESS, and {objector} checked and disagreed:\n\n"
                    f"{attempt.feedback}\n\n"
                    f"Do not report success again until that is actually true. If it is "
                    f"something you did not finish, finish it; if you believe the check "
                    f"is wrong, say precisely what you did and where the file is.")
                if loop >= limit:
                    break
                # The same agent tries again, not the fixer. A check reports on
                # whether the work is there, and the one who should make it
                # there is whoever said it was.
                carry = build_handoff(turn.transcript, turn.text)
                self._emit("step_retry", agent=role.name, step_id=step.id, payload={
                    "attempt": loop, "feedback": feedback,
                    "message": (f"{role.name} will try again with what {objector} "
                                f"found (try {loop + 1} of {limit})."),
                })
                continue

            if outcome == "SUCCESS":
                # Spent: a later ordinary rerun starts on the usual model again.
                step.start_on_backup = False
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
        # A step whose last word was a failed check halts as one that lied: it
        # had its chances to make the report true and did not.
        lied = bool(last and last.outcome == "CHECK_FAILED")
        self._emit("step_failed", agent=role.name, step_id=step.id, payload={
            "reason": (f"the step never reported success in {limit} tr(y/ies)"
                       + (f" — last: {last.outcome_reason}" if last and last.outcome_reason
                          else "")),
            "attempts": len(step.attempts), "max_loops": limit, "halts_flow": True,
            "lied": lied,
        })
        self._halt(step, lied=lied)

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
        escalation_t0 = time.monotonic()
        turn = run_agent(
            role=role, images=step.images,
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
            goal=self.session.goal, requirements=self.session.requirements,
            placement=self._placement(step),
            approve=self.approve, reindex=self._reindex,
            steering_inbox=step.take_steering,
            steering=[carry.body] if carry and carry.body else None,
        )
        attempt.worker_event_id = turn.model_event_ids[-1] if turn.model_event_ids else None
        attempt.files_written = turn.files_written
        attempt.context = turn.context
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
        fixer_t0 = time.monotonic()
        turn = run_agent(
            role=fixer, images=step.images,
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
            approve=self.approve, reindex=self._reindex,
            steering_inbox=step.take_steering,
        )
        self._charge(fixer.name, step, fixer_t0)
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
                f"Halted at the {step.role} step: it kept reporting success, and "
                f"{step.checker} kept finding the work was not actually done."
            )
            hint = ("Open the step to see what was claimed and what was found. It was "
                    "sent back with the check's findings and still did not make them "
                    "true, so look at the model and the prompt for that agent.")
        elif step.runs_a_loop:
            # A loop step's budget is its edges' visits, not the step's loop
            # limit — "within 2 loop(s)" on a fourteen-block loop was a number
            # from a different machine.
            self.session.error = (
                f"Halted at the {step.loop} loop: it ended without success. "
                f"The last block's console says which exit stopped it."
            )
            hint = ("Rerun the failed block from its ↻ (with or without the "
                    "rewind), widen that edge's visits in the Loops editor, or "
                    "change the fixing agent.")
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

    def _run_check(self, step: Step, attempt: Attempt,
                   chain: list[str] | None = None) -> str | None:
        """Run each gate in order; the first FAIL stops the chain.

        Gates are sequential on purpose. If the tester fails there is no point
        asking the reviewer to read code that does not work yet, and the
        feedback the worker gets should be about one thing.

        `chain` names the checks outright, for a caller that knows better than
        the step does — a loop, whose nodes carry their own.
        """
        # Checks come from two places: the plan knows this step writes files,
        # the agent knows its work is the kind that breaks other things.
        #
        # Once the step's chain has been seeded from the agent, the step is the
        # whole answer — a check taken off in the plan has to stay off, or the
        # control is decoration. Merging is for the steps that predate seeding,
        # and for anything built without going past the plan.
        if chain is not None:
            checks = [name for name in chain if name]
        elif step.checks_seeded:
            checks = list(step.checks)
        else:
            worker = self.session.role(step.role) if step.role else None
            checks = merge_checks(
                step.checks, list(getattr(worker, "checks", None) or []))
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

            gate_t0 = time.monotonic()
            turn = run_agent(
                role=gate, images=step.images,
                task=self._gate_task(step, attempt, gate, index, checks),
                project=self.project, config=self.config.for_role(gate), bus=self.bus,
                session_id=self.session.id, step_id=step.id, history=self.session.history,
                graph_tools=self._graph_tools(gate),
                should_stop=lambda: self.session.stopping,
                memory=self.memory, project_map=self._project_map(gate, step.task),
                goal=self.session.goal, requirements=self.session.requirements,
                approve=self.approve, reindex=self._reindex,
                steering_inbox=step.take_steering,
                verdict_required=True,
            )
            self._charge(gate.name, step, gate_t0)
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
                    # No routing promise: this code cannot see the try budget,
                    # and "goes back to be fixed" was said one line before a
                    # step_failed. Whether it runs again is the next event's
                    # sentence to say.
                    "message": (
                        f"{gate.title} rejected the work (check {index + 1} of "
                        f"{len(checks)}) — the chain stops here."),
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

    #: How much of the step's diff a check is handed. Big enough for any
    #: ordinary step; a diff past this is clipped and says so, with the file
    #: list still there to go look at.
    GATE_DIFF_CHARS = 16_000

    def _gate_task(self, step: Step, attempt: Attempt, gate, index: int, checks: list) -> str:
        earlier = [g for g in attempt.gate_results if g.verdict == "PASS"]
        passed = (f"\n\nAlready passed: {', '.join(g.gate for g in earlier)}."
                  if earlier else "")
        return (
            f"Verify this work by another agent (check {index + 1} of {len(checks)}):\n\n"
            f"{step.task}\n\n"
            f"What they reported:\n{step.summary}\n\n"
            f"Files they changed: {', '.join(attempt.files_written) or 'none'}{passed}"
            f"{self._gate_diff(attempt)}"
        )

    def _gate_diff(self, attempt: Attempt) -> str:
        """The change itself, in the checker's prompt.

        Measured on a delegated review before this existed: thirty internal
        turns and 355k input tokens, nearly all of them spent rediscovering by
        exploration what one `git show` already knew. With the diff in front of
        it a checker reads instead of wandering — and that holds for every
        backend, not only the one where wandering is expensive.
        """
        if not attempt.commit:
            return ""
        patch = (vcs.show(self.project, attempt.commit) or {}).get("diff", "")
        if not patch.strip():
            return ""
        clipped = len(patch) > self.GATE_DIFF_CHARS
        shown = patch[:self.GATE_DIFF_CHARS]
        return ("\n\nThe change itself:\n```diff\n" + shown + "\n```"
                + ("\n(clipped — the full change is in the files listed above)"
                   if clipped else ""))

    def _verify(self, step: Step, attempt: Attempt) -> str | None:
        """Backwards-compatible entry point."""
        return self._run_check(step, attempt)

    # --------------------------------------------------------- supervision

    def _owner_of(self, path: str) -> str | None:
        """Which agent may write this path — on the team, or in the library.

        Looking only at the team is how "nobody owns package.json" became
        "unassigned" when the answer was "add the frontend to the team".
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

    # ------------------------------------------------------------ history

    def _prepare_git(self) -> None:
        """Decide once whether this run can take checkpoints."""
        if not self.config.git_commits:
            return
        if not vcs.is_repo(self.project):
            if not self.config.git_auto_init:
                return
            result = vcs.ensure_repo(self.project)
            if not result.ok:
                self._emit("warning", payload={
                    "message": (f"No git checkpoints this run: {result.detail}. "
                                f"Steps cannot be reverted.")})
                return
            self._emit("git", payload={"action": "init", "message": result.detail})
        self._git = True
        # trance's index is binary and rewritten on every reindex, so a repo
        # that tracks it puts "Binary files differ" in the middle of every diff
        # an agent made. Ignore it, and stop tracking it in repos that already
        # do — from the index only, so nothing on disk moves and no history is
        # rewritten.
        if vcs.ignore_trance_files(self.project):
            self._emit("git", payload={"action": "ignore",
                                       "message": "Added trance's index to .gitignore."})
        dropped = vcs.untrack_ignored(self.project)
        if dropped:
            self._emit("git", payload={
                "action": "untrack", "files": dropped,
                "message": (f"Stopped tracking {len(dropped)} index file(s) — they were "
                            f"making every diff unreadable. Nothing was deleted.")})
        # Anything already in the tree is committed before the first agent runs,
        # so "revert this step" can never take a user's own edits with it.
        self._checkpoint("before the run")

    def _checkpoint(self, what: str) -> str:
        """Commit whatever is lying around and return the sha to come back to."""
        if not self._git:
            return ""
        result = vcs.commit_all(self.project, what)
        if result.ok:
            self._emit("git", payload={"action": "checkpoint", "sha": result.sha,
                                       "files": result.files, "message": what})
        return vcs.head(self.project)

    def _commit_step(self, step: Step, role_name: str, outcome: str) -> str:
        """Record what an agent did, so `git log` is the run."""
        if not self._git:
            return ""
        task = " ".join((step.task or "").split())[:72]
        result = vcs.commit_all(self.project, f"{role_name}: {task} [{outcome}]")
        if result.ok:
            self._emit("git", agent=role_name, step_id=step.id, payload={
                "action": "commit", "sha": result.sha, "files": result.files,
                "message": f"{role_name}: {task} [{outcome}]",
            })
        return result.sha

    def _revert(self, step: Step, attempt: Attempt, role_name: str) -> None:
        """Put the tree back to where this block started."""
        if not self._git or not (attempt.checkpoint or attempt.commit):
            return
        result = vcs.undo(self.project, attempt.commit, attempt.checkpoint)
        attempt.reverted = bool(result.ok)
        self._emit("git", agent=role_name, step_id=step.id, payload={
            "action": "revert", "ok": result.ok, "sha": attempt.checkpoint,
            "files": result.files, "detail": result.detail,
            "message": (f"Reverted {role_name}'s changes — the project is back to "
                        f"where this block started."
                        if result.ok else f"Could not revert: {result.detail}"),
        })
        self._reindex()          # the graph now describes files that changed back

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

    def _tries_for(self, role, step: Step) -> int:
        """How many attempts this block gets: the step's say, else the agent's.

        The agent knows what it is worth retrying — two on its usual model and
        two on its backup, by default. A step overrides that only when it was
        given an explicit number.
        """
        if step.overrides_tries:
            return step.loop_limit
        return max(1, getattr(role, "total_tries", 2))

    def _model_for(self, role, step: Step, attempt: int, force_backup: bool = False):
        """This agent's model, or its backup once it has failed enough times.

        The retry loop changes the prompt and the feedback; the model is the one
        thing it never changes, so an agent that fails the same way twice fails
        the same way a third time. This is the per-agent version of that switch,
        decided by the agent rather than globally.
        """
        after = max(1, int(getattr(role, "tries", 2) or 2))
        backup = getattr(role, "backup_preset", "") or ""
        if not backup:
            if force_backup:
                # Asked for by a loop route or a re-run button, so silence here
                # would look like the backup ran and made no difference.
                self._emit("warning", agent=role.name, step_id=step.id, payload={
                    "message": (f"{role.name} has no backup model set, so this runs "
                                f"on its usual one.")})
            return self.config.for_role(role), False
        if attempt <= after and not force_backup:
            return self.config.for_role(role), False
        if backup not in self.config.presets:
            self._emit("warning", agent=role.name, step_id=step.id, payload={
                "message": (f"{role.name}'s backup model {backup!r} is not defined — "
                            f"staying on its usual one.")})
            return self.config.for_role(role), False

        resolved = self.config.resolve(self.config.worker, preset=backup)
        self._emit("model_switched", agent=role.name, step_id=step.id, payload={
            "from": self.config.for_role(role).model, "to": resolved.model,
            "preset": backup, "after": after, "attempt": attempt,
            "message": (f"{role.name} has failed {after} time(s) — switching to its "
                        f"backup model {resolved.model} for this try."),
        })
        return resolved, True

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
