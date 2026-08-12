"""HTTP + WebSocket server behind the inspection UI."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..agents import orchestrator as orchestrator_agent
from ..agents.orchestrator import POINTS
from ..agents.approval import ALWAYS, ApprovalBroker, DECISIONS
from ..agents.memory import COMPACT_PROMPT, MAX_NOTES, ProjectMemory
from ..agents.roles import BUILTIN_ROLES, TOOLSETS, AgentRole, definition_differs
from ..trace.session_log import SessionLog
from ..agents.store import (
    CommandStore, DEFAULT_LIST, LoopStore, PROTECTED, RoleStore,
    validate as validate_agent,
)
from ..agents.tools import ALLOWED_COMMANDS, set_command_lists, set_command_policy
from ..agents.visual import SHOTS_DIR, available as browser_available
from ..vision import VISION_KINDS, image_block
from ..config import Config
import dataclasses
from dataclasses import replace
from ..engine import FlowEngine, check_project_dir
from ..events import EventBus
from ..flow import Flow, Step, merge_checks, seed_checks
from ..loops import EXITS, STOP, Loop, validate as validate_loop
from ..providers.base import list_models
from ..providers import (
    KIND_DEFAULTS, ModelPreset, ProviderConfig, ProviderStore, abort_inflight,
    client_for,
)
from ..session import ChatMessage, SessionStore
from .. import paths, preview, vcs
from ..usage import UsageLedger
from ..workspace import STORE_DIR, DefaultStores, Workspace, folder_for
from ..worker.client import BackendError

#: The built UI. Source lives in ui/ and the build is committed here, so a
#: clone runs with Python alone and never installs node.
STATIC = Path(__file__).parent / "ui"


#: How much history the console asks for when it opens a session. Enough to
#: rebuild what you were looking at; the rest is fetched by whoever wants it.
CONSOLE_TAIL = 400


#: Addresses the workspace-wide configuration instead of a project's. Session
#: ids are `s_…`, so this can never collide with a real one.
DEFAULTS = "defaults"


def engine_alive(session) -> bool:
    """Is a flow engine actually executing this session right now?

    `status` alone is not enough: a crashed engine can leave status at
    "running" with no thread behind it, and a finished run leaves the thread
    gone while pending steps can still be added by rerun or a flow edit — and
    a run that is very much alive can have its status written over by something
    else entirely, which is what taught this to be asked rather than assumed.
    """
    thread = getattr(session, "_thread", None)
    return bool(thread is not None and thread.is_alive())


def _adopt_runs_state(state_dir: Path, runs_dir: Path) -> None:
    """Copy the legacy runs/ files into the system dir, once.

    Three layers now, each owning its own kind of state: the *system*
    (models, settings, the Default scope, the ledger — one set per machine),
    the *workspace* (nothing but its projects and their sessions), and the
    *project* (its own agents, loops and allowlists, provisioned from the
    system defaults). The per-workspace copies this replaces were how
    workspace three greeted its user with the loop library of workspace one.
    """
    if not runs_dir.is_dir() or state_dir.resolve() == runs_dir.resolve():
        return
    for name in ("providers.json", "agents.json", "loops.json",
                 "commands.json", "settings.json", "usage.json"):
        source, target = runs_dir / name, state_dir / name
        if source.exists() and not target.exists():
            state_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def create_app(config: Config | None = None, sessions_dir: Path | None = None) -> FastAPI:
    config = config or Config.load()
    # The machine's own state — models, settings, the Default scope, the
    # ledger — at its system home. A workspace holds only its projects.
    state_dir = config.system_root
    _adopt_runs_state(state_dir, Path(config.runs_dir))
    store = SessionStore(sessions_dir or Path(config.runs_dir) / "sessions",
                         workspace=config.workspace_root)
    # trance.toml seeds the registry once; after that the JSON store is the
    # source of truth so provider edits made in the UI survive a restart.
    providers = ProviderStore(state_dir / "providers.json", seed=config.providers)
    providers.seed_presets_from_providers()  # never show an empty model picker
    # The workspace-wide files, addressable so they can be edited. Every new
    # project is a copy of these; they are not what a run reads — each project
    # keeps its own under .trance/.
    defaults_stores = DefaultStores(state_dir)
    roles = defaults_stores.roles
    commands = defaults_stores.commands
    loops = defaults_stores.loops
    workspace = Workspace(defaults=state_dir)
    set_command_policy(commands.policy)
    set_command_lists(commands.lists)

    def stores_q(session: str = ""):
        """The stores named by a `?session=` query parameter.

        Required rather than defaulted. Agents and loops belong to a project
        now, so a request that does not say which project is a request nobody
        can answer correctly — and guessing "the first one" would silently edit
        the wrong project's agents.
        """
        if session == DEFAULTS:
            return defaults_stores
        if not session:
            raise HTTPException(400, (
                "this configuration belongs to a project, so the request has to "
                f"say which: add ?session=<id>, or ?session={DEFAULTS} to edit "
                "what new projects start from."))
        return stores_of(_need(store, session))

    def stores_of(session):
        """The project's own agents, loops, allowlists and settings."""
        return workspace.stores_for(_project_of(session))

    def config_for(session) -> Config:
        """`config`, with this project's settings applied.

        The engine reads git_commits and the rest off a Config, and those are
        now per project — so it gets one that says what this project decided
        rather than what the machine last had in memory.
        """
        # Only the fields Config actually has. Settings grew plan-shaped
        # fields (what every plan opens and closes with) that the engine's
        # Config never reads, and replace() rejects strangers.
        known = {f.name for f in dataclasses.fields(Config)}
        held = stores_of(session).settings.settings.to_dict()
        return replace(config, **{k: v for k, v in held.items() if k in known})

    def publish_commands_of(session) -> None:
        """Point the tool layer at this project's allowlists.

        AgentTools reads them through module-level state, so whichever project
        is about to run has to put its own there first. One run at a time makes
        that safe; two would not, and the engine is sequential by design.
        """
        held = stores_of(session).commands
        set_command_policy(held.policy)
        set_command_lists(held.lists)
    config.providers = {p.name: p for p in providers.all()}
    config.presets = {m.name: m for m in providers.all_presets()}
    bus = EventBus()

    #: The trace on disk, one log per session. Without it a restart leaves a
    #: finished step you can no longer explain.
    logs: dict[str, SessionLog] = {}
    logs_lock = threading.Lock()

    def log_for(session_id: str) -> SessionLog:
        with logs_lock:
            existing = logs.get(session_id)
            if existing is None:
                # The trace lives with the session, in its project. Only a
                # session the store no longer knows — deleted mid-run, say —
                # falls back to the old flat dir, so its last events land
                # somewhere rather than nowhere.
                session = store.get(session_id)
                where = session.store_dir if session else store.root / session_id
                existing = SessionLog(where)
                logs[session_id] = existing
            return existing

    def persist(event) -> None:
        if event.session_id and event.session_id != "system":
            log_for(event.session_id).append(event)

    bus.subscribe_sync(persist)

    #: What each model has been asked to do — this run, and in total. Counted
    #: from the bus so the orchestrator's calls count too, not only the agents'.
    ledger = UsageLedger(state_dir / "usage.json")
    bus.subscribe_sync(ledger.on_event)

    def history_for(session_id: str):
        """This run's events, plus everything earlier runs recorded.

        The bus only knows what this process has seen, so a session that
        started before a restart has its whole first half on disk and nothing
        in memory.
        """
        live = bus.history(session_id)
        seen = {e.id for e in live}
        stored = [e for e in log_for(session_id).read() if e.id not in seen]
        return stored + live

    app = FastAPI(title="trance")
    app.state.config = config
    app.state.store = store
    app.state.bus = bus
    app.state.usage = ledger

    def touch(session):
        store.save(session)

    #: Background tasks, held until they finish. asyncio keeps only a weak
    #: reference to a task, so one that nothing else refers to can be collected
    #: mid-flight and silently cancelled — which is how a split that was really
    #: running simply never reported back.
    _background: set = set()

    def _spawn(coro):
        task = asyncio.ensure_future(coro)
        _background.add(task)
        task.add_done_callback(_background.discard)
        return task

    #: One broker per session: a refused action asks the user instead of ending
    #: the step, and the answer can widen the policy for the rest of the run.
    brokers: dict[str, ApprovalBroker] = {}
    app.state.brokers = brokers

    def broker_for(session) -> ApprovalBroker:
        existing = brokers.get(session.id)
        if existing is not None:
            existing.revive()
            return existing

        def on_request(request):
            bus.emit("approval_requested", session.id, agent=request.agent,
                     step_id=request.step_id, payload={
                         **request.to_dict(),
                         "timeout_s": config.approval_timeout_s,
                         "message": _approval_message(request)})

        def on_resolved(request):
            bus.emit("approval_resolved", session.id, agent=request.agent,
                     step_id=request.step_id, payload=request.to_dict())

        broker = ApprovalBroker(
            on_request=on_request, on_resolved=on_resolved,
            on_always=lambda request: _widen_policy(session, request),
            timeout_s=config.approval_timeout_s, enabled=config.ask_on_refusal)
        brokers[session.id] = broker
        return broker

    def _approval_message(request) -> str:
        if request.kind == "write":
            return (f"{request.agent} wants to write {request.subject}, which is outside "
                    f"its remit ({', '.join(request.detail.get('remit') or []) or 'nothing'}).")
        return (f"{request.agent} wants to run a command using "
                f"{', '.join(request.detail.get('programs') or [])}, which is not on its "
                f"allowlist.")

    def _widen_policy(session, request) -> None:
        """Make "always" mean it — the same ask must not come back next step.

        Widened for this project only. Allowing an agent one more path because
        of what it is building here should not quietly widen its remit in every
        other project too.
        """
        held = stores_of(session)
        roles, commands = held.roles, held.commands
        if request.kind == "write":
            role = roles.get(request.agent)
            if role is not None and request.subject not in role.paths:
                # The exact path, not a widened glob. The user allowed this
                # file; inferring "and everything like it" is not what they said.
                role.paths = [*role.paths, request.subject]
                roles.upsert(role)
                for other in store.all():
                    refresh_team(other)
                    touch(other)
            return

        programs = [p for p in request.detail.get("programs") or [] if p]
        role = roles.get(request.agent)
        if role is not None and role.commands:
            role.commands = sorted(set(role.commands) | set(programs))
            roles.upsert(role)
            for other in store.all():
                refresh_team(other)
                touch(other)
            return
        target = getattr(role, "command_list", "") or DEFAULT_LIST
        policy = commands.upsert(
            target, allowed=sorted(set(commands.get(target).allowed) | set(programs)))
        _publish_commands(commands)
        bus.emit("commands_updated", session.id, payload={"name": target, **policy.to_dict()})

    def ensure_running(session) -> bool:
        """Start an engine if none is live and there is pending work.

        A stopping engine counts as live — it is still inside a model call and
        will exit when that returns. Starting a second one now would run the
        same step twice, so we wait for the first to die instead. Without this
        the step sits pending forever: the old engine unwinds and nobody starts
        a new one.
        """
        if engine_alive(session):
            if session.stopping:
                _start_when_free(session)
            return False
        if session.flow.next_pending() is None:
            return False
        # The team, made whole before anyone runs. Found live: a plan whose
        # first step named the library's planner started with the *shipped*
        # planner — no preset, so the default model — because the proposal
        # path had never pulled the role onto the team and nothing between
        # the proposal and Run forced a read that would have healed it.
        refresh_team(session)
        seed_loop_checks(stores_of(session))
        session.error = None
        session.clear_stop()
        publish_commands_of(session)
        FlowEngine(session, config_for(session), bus, on_change=lambda: touch(session),
                   approve=broker_for(session).ask, loops=stores_of(session).loops).start()
        return True

    def _start_when_free(session) -> None:
        """Hand over to a fresh engine the moment the stopping one exits."""
        thread = getattr(session, "_thread", None)
        if thread is None or getattr(session, "_handover", None) is not None:
            return

        def wait_and_start():
            thread.join(timeout=900)
            session._handover = None
            if session.flow.next_pending() is not None and not engine_alive(session):
                session.clear_stop()
                session.error = None
                bus.emit("run_started", session.id, payload={
                    "reason": "the previous run finished unwinding", "steps": 1})
                publish_commands_of(session)
                FlowEngine(session, config_for(session), bus,
                           on_change=lambda: touch(session),
                           approve=broker_for(session).ask,
                           loops=stores_of(session).loops).start()

        session._handover = threading.Thread(
            target=wait_and_start, name=f"handover-{session.id}", daemon=True)
        session._handover.start()

    def checks_for(session):
        """What each agent's steps are checked by — the only source there is.

        The plan used to choose a verifier per step, which meant a model
        picking one from the shape of a sentence, differently each time it was
        asked. It is a property of the agent: set once, in one place, by a
        person who knows what this project needs.
        """
        def of(name: str) -> list[str]:
            role = session.role(name) or stores_of(session).roles.get(name)
            return list(getattr(role, "checks", None) or [])
        return of

    def pull_flow_roles(session) -> bool:
        """Put every agent the flow names onto the team. True if any was missing.

        The team pull used to reach through loops only, so a plain step's own
        role never joined — masked for months because the engine falls back to
        the built-ins, until the first *custom* agent ("claude", on a step,
        saved fine, then "unknown role 'claude'" at run time — the library knew
        it and the session did not).
        """
        roles = stores_of(session).roles
        loop_store = stores_of(session).loops
        wanted = list(session.team)
        added = False
        names: list[str] = []
        for step in session.flow.steps:
            loop = loop_store.get(step.loop) if step.loop else None
            names += list(loop.roles()) if loop else [step.role]
            names += list(step.checks) + ([step.on_fail] if step.on_fail else [])
        for name in names:
            if name and all(r.name != name for r in wanted) and roles.get(name):
                wanted.append(roles.get(name))
                added = True
        if added:
            session.team = roles.resolve_team(wanted)
        return added

    def refresh_team(session):
        """Re-bind a session's team to its project's agent definitions.

        And copy each agent's standing checks onto its steps, once, so the plan
        shows what will actually run and can change it. Here because it is the
        one place every read and every agent edit passes through: a check added
        to an agent reaches the steps already planned, and a check taken off a
        step stays off.
        """
        session.team = stores_of(session).roles.resolve_team(session.team)
        # Heals sessions saved before the team pull below existed, on the next
        # read rather than on the next edit.
        grew = pull_flow_roles(session)
        if seed_checks(session.flow, checks_for(session)) or grew:
            touch(session)
        return session

    # ------------------------------------------------------------- static

    @app.get("/")
    def index():
        if not (STATIC / "index.html").is_file():
            # The UI is built from ui/ into this directory and the build is
            # committed, so a missing one means a source checkout that has not
            # been built rather than a broken install. Say which.
            raise HTTPException(503, (
                "the UI has not been built. From the repository root: "
                "cd ui && npm install && npm run build"))
        return FileResponse(STATIC / "index.html",
                            headers={"Cache-Control": "no-cache, must-revalidate"})

    class BuiltUI(StaticFiles):
        """Cache the hashed assets hard, and never cache the page.

        The bundler names every asset by the hash of its contents, so a given
        URL can only ever mean one file — those are safe to keep forever, and a
        run leaves the browser fetching nothing but data. index.html is the one
        file whose URL is stable, and it is what points at the current hashes;
        serving a cached copy of it after a rebuild loads assets that no longer
        exist. The old UI had neither property — one unhashed app.js, which had
        to revalidate on every load or show behaviour that had been removed.
        """

        IMMUTABLE = "public, max-age=31536000, immutable"

        def file_response(self, full_path, *args, **kwargs):
            response = super().file_response(full_path, *args, **kwargs)
            hashed = "/assets/" in str(full_path).replace("\\", "/")
            response.headers["Cache-Control"] = (
                self.IMMUTABLE if hashed else "no-cache, must-revalidate")
            return response

    app.mount("/static", BuiltUI(directory=STATIC), name="static")

    # ------------------------------------------------------------- config

    def _sync():
        """Re-publish the registry into the live config."""
        config.providers = {p.name: p for p in providers.all()}
        config.presets = {m.name: m for m in providers.all_presets()}

    def _follow_orchestrator(role) -> None:
        """Keep the orchestrator's model where every other agent's model is.

        It was set in two places that never agreed: the agent card had a model
        picker that nothing read, and the real setting lived under Settings. One
        of those had to be the truth, and the agent card is where you choose a
        model for every other agent — so the role wins and Settings stops
        offering a second answer.
        """
        if role.name != "orchestrator":
            return
        if role.preset and role.preset in config.presets:
            config.orchestrator.preset = role.preset

    # The role is the source of truth from startup, not only after an edit.
    _orchestrator_role = roles.get("orchestrator")
    if _orchestrator_role is not None:
        _follow_orchestrator(_orchestrator_role)

    #: When this process started. Anything under src/ newer than this is code
    #: the running server has never executed.
    STARTED_AT = time.time()
    SOURCE_ROOT = Path(__file__).resolve().parent.parent

    def _code_changed_since_start() -> bool:
        """Is the server running code older than what is on disk?

        Every fix in a session like this one is followed by "still broken" until
        somebody remembers the process is from before it. The server can see
        that for itself, so it says so rather than letting the UI look wrong.
        """
        try:
            for path in SOURCE_ROOT.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                if path.stat().st_mtime > STARTED_AT:
                    return True
        except OSError:
            pass
        return False

    @app.get("/api/config")
    def get_config():
        orchestrator = config.for_orchestrator()
        lifetime = ledger.lifetime()
        return {
            "config": config.to_dict(),
            # all_presets, not presets: the latter hides anything whose
            # *provider* is disabled, and providers no longer exist — so it
            # returns nothing, and the UI that reads this emptied its model
            # picker every time it refreshed.
            "presets": [{**m.to_dict(), "spend": lifetime.get(m.name)}
                        for m in providers.all_presets()],
            "kinds": KIND_DEFAULTS,
            # Run settings are not here any more: they belong to a project, and
            # this endpoint does not know which one. GET a session's /settings.
            "scale": list(POINTS),
            # Reported, never required. Without a browser the visual toolset is
            # simply unavailable and every other toolset works as before. There
            # is no vision model setting: screenshots go to the model the agent
            # itself is configured with.
            "visual": {"browser": browser_available()},
            "orchestrator": {"preset": config.orchestrator.preset,
                             "provider": orchestrator.provider, "model": orchestrator.model,
                             "base_url": orchestrator.base_url,
                             "context_window": orchestrator.context_window},
            "stale": _code_changed_since_start(),
        }

    # ---------------------------------------------------------- providers

    # ----------------------------------------------------------- commands

    def _publish_commands(commands):
        """Point the tool layer at these lists.

        Editing an allowlist for the project that is running should take effect
        without waiting for the next run; editing another project's must not
        touch what is running. The caller has the right store either way.
        """
        set_command_policy(commands.policy)
        set_command_lists(commands.lists)

    def _check_programs(allowed):
        if allowed is None:
            return
        if not isinstance(allowed, list):
            raise HTTPException(400, "allowed must be a list of program names")
        bad = [c for c in allowed if "/" in str(c) or " " in str(c)]
        if bad:
            raise HTTPException(400, (
                f"program names only, no paths or arguments: {', '.join(map(str, bad[:4]))}"))

    @app.get("/api/commands")
    def get_commands(session: str = ""):
        """Every named allowlist, plus which agents use which."""
        held = stores_q(session)
        commands, roles = held.commands, held.roles
        return {
            **commands.policy.to_dict(),              # the default, for older callers
            "lists": {n: p.to_dict() for n, p in commands.lists.items()},
            "names": commands.names(),
            "default": DEFAULT_LIST,
            "defaults": sorted(ALLOWED_COMMANDS),
            "usage": {r.name: (r.command_list or DEFAULT_LIST)
                      for r in roles.all() if "commands" in r.toolsets},
            "overrides": {
                r.name: {"commands": r.commands, "shell": r.shell, "workdir": r.workdir}
                for r in roles.all()
                if r.commands or r.shell is not None or r.workdir
            },
            "agents_with_commands": [r.name for r in roles.all() if "commands" in r.toolsets],
        }

    @app.put("/api/commands")
    def set_commands(body: dict, session: str = ""):
        """Edit one list. Without a name, the default one."""
        held = stores_q(session)
        commands, roles = held.commands, held.roles
        name = (body.get("name") or DEFAULT_LIST).strip()
        if " " in name:
            raise HTTPException(400, "a list name cannot contain spaces")
        allowed = body.get("allowed")
        _check_programs(allowed)
        if name not in commands.lists and not allowed:
            raise HTTPException(400, "a new list needs at least one program")
        policy = commands.upsert(name, allowed=allowed, shell=body.get("shell"))
        _publish_commands(commands)
        bus.emit("commands_updated", "system", payload={"name": name, **policy.to_dict()})
        return {"name": name, **policy.to_dict()}

    @app.delete("/api/commands/{name}")
    def delete_command_list(name: str, session: str = ""):
        """Delete a list. Agents pointing at it fall back to the default."""
        held = stores_q(session)
        commands, roles = held.commands, held.roles
        if not commands.delete(name):
            raise HTTPException(400, "the default list cannot be deleted")
        moved = []
        for role in roles.all():
            if role.command_list == name:
                role.command_list = ""
                roles.upsert(role)
                moved.append(role.name)
        for session in store.all():
            refresh_team(session)
            touch(session)
        _publish_commands(commands)
        return {"deleted": name, "moved_to_default": moved}

    @app.post("/api/commands/cancel/{command_id}")
    def cancel_running_command(command_id: str):
        """Kill a command that is still running."""
        from ..agents.tools import cancel_command, running_commands

        killed = cancel_command(command_id)
        return {"cancelled": killed, "still_running": running_commands()}

    @app.post("/api/commands/allow")
    def allow_programs(body: dict, session: str = ""):
        """Add programs to an allowlist — the project's, or an agent's own."""
        held = stores_q(session)
        commands, roles = held.commands, held.roles
        programs = [str(p).strip() for p in (body.get("programs") or []) if str(p).strip()]
        if not programs:
            raise HTTPException(400, "programs is required")
        bad = [p for p in programs if "/" in p or " " in p]
        if bad:
            raise HTTPException(400, f"program names only: {', '.join(bad[:4])}")

        agent_name = body.get("agent")
        agent = roles.get(agent_name) if agent_name else None
        # Adding to the global list has no effect on an agent that overrides it.
        if agent is not None and agent.commands:
            agent.commands = sorted(set(agent.commands) | set(programs))
            roles.upsert(agent)
            for session in store.all():
                refresh_team(session)
                touch(session)
            return {"scope": "agent", "agent": agent.name, "allowed": agent.commands}

        target = getattr(agent, "command_list", "") or DEFAULT_LIST
        policy = commands.upsert(
            target, allowed=sorted(set(commands.get(target).allowed) | set(programs)))
        _publish_commands(commands)
        bus.emit("commands_updated", "system", payload={"name": target, **policy.to_dict()})
        return {"scope": "list", "list": target, "allowed": policy.allowed}

    @app.post("/api/commands/reset")
    def reset_commands(body: dict | None = None, session: str = ""):
        commands = stores_q(session).commands
        policy = commands.reset((body or {}).get("name") or DEFAULT_LIST)
        _publish_commands(commands)
        return policy.to_dict()

    # ------------------------------------------------------------- agents

    @app.get("/api/agents")
    def list_agents(session: str = ""):
        roles = stores_q(session).roles
        return {
            "verifiers": [r.name for r in roles.all() if r.verifier],
            "agents": [
                {**r.to_dict(), "protected": r.name in PROTECTED,
                 # Which definition fields differ from what a reset would
                 # restore: the Default copy for a session, shipped for the
                 # Default scope. An old frozen copy and a deliberate edit
                 # look identical from here — both are worth the marker.
                 "differs": definition_differs(
                     r, (BUILTIN_ROLES.get(r.name) if session == DEFAULTS
                         else (defaults_stores.roles.get(r.name)
                               or BUILTIN_ROLES.get(r.name)))),
                 "resolved": _resolved_for(r)}
                for r in roles.all()
            ],
            "toolsets": list(TOOLSETS),
        }

    def _resolved_for(role):
        m = config.for_role(role)
        return {"model": m.model, "provider": m.provider, "context_window": m.context_window}

    @app.put("/api/agents/{name}")
    def upsert_agent(name: str, body: dict, session: str = ""):
        roles = stores_q(session).roles
        # Merge onto what is stored: a partial update ("just change the command
        # list") must not blank the prompt and the remit by omission.
        existing = roles.get(name.strip())
        # A partial body aimed at a name that is not there is a typo or a
        # retired agent, not a request to create a husk — and letting it fall
        # through returned a 500 from the dataclass rather than an answer.
        if existing is None and not (body.get("title") and body.get("system_prompt")):
            raise HTTPException(404, (
                f"no agent named {name.strip()!r} to update — creating a new "
                f"one needs at least a title and a system_prompt."))
        base = existing.to_dict() if existing else {"description": ""}
        body = {**base, **body, "name": name.strip()}
        error = validate_agent(body)
        if error:
            raise HTTPException(400, error)
        if body.get("preset") and body["preset"] not in config.presets:
            raise HTTPException(400, f"unknown model {body['preset']!r}")
        saved = roles.upsert(AgentRole.from_dict(body))
        _follow_orchestrator(saved)
        for session in store.all():          # live sessions pick up the edit
            refresh_team(session)
            touch(session)
        bus.emit("agents_updated", "system", payload={"name": saved.name})
        return {**saved.to_dict(), "protected": saved.name in PROTECTED,
                "resolved": _resolved_for(saved)}

    @app.post("/api/agents/{name}/reset")
    def reset_agent(name: str, session: str = ""):
        """Restore an agent's definition to its default.

        One chain, one hop at a time: a session resets to the Default scope's
        copy — which for a custom agent kept in the defaults works too — and
        the Default scope resets to shipped. Never skips over the user's own
        defaults, which is what made "restore" two different words before.
        """
        roles = stores_q(session).roles
        source = None if session == DEFAULTS else defaults_stores.roles.get(name)
        role = roles.reset(name, source=source)
        if role is None:
            raise HTTPException(404, f"{name!r} has no default to reset to")
        for session in store.all():
            refresh_team(session)
            touch(session)
        return {**role.to_dict(), "protected": True, "resolved": _resolved_for(role)}

    def _sessions_of(held) -> list:
        """The sessions that actually read this store — same project, no more.

        Deletion used to be refused because *some other project's* session
        named the same agent, which was a leftover from the one-shared-library
        era: those sessions read their own copies, and deleting here cannot
        touch them.
        """
        mine = Path(getattr(held, "project", "")).resolve()
        return [s for s in store.all()
                if Path(s.project_dir).expanduser().resolve() == mine]

    @app.delete("/api/agents/{name}")
    def delete_agent(name: str, session: str = "", force: bool = False):
        """Delete an agent from this scope.

        Usage inside the same scope is a warning, not a wall: the first call
        answers 409 naming what still points at it, and force=true is the
        approval — after which a step that still names it fails at run time
        saying the agent was deleted, rather than silently misbehaving.
        """
        held = stores_q(session)
        roles = held.roles
        if name in PROTECTED:
            raise HTTPException(409, f"{name!r} is a built-in agent type and cannot be deleted")
        if roles.get(name) is None:
            raise HTTPException(404, "no such agent")

        if not force:
            usage = []
            for s in _sessions_of(held):
                for n, st in enumerate(s.flow.steps, 1):
                    if st.role == name:
                        usage.append(f"step {n} of {s.name}")
                    elif name in st.checks:
                        usage.append(f"a check on step {n} of {s.name}")
            for loop in held.loops.all():
                if any(node.role == name for node in loop.nodes):
                    usage.append(f"the {loop.name} loop")
            if usage:
                raise HTTPException(409, (
                    f"{name!r} is still used by: {', '.join(usage[:8])}. "
                    f"Delete it anyway and those steps will fail when they run, "
                    f"saying the agent was deleted."))

        roles.delete(name)
        return {"deleted": name}

    @app.get("/api/presets")
    def list_presets():
        lifetime = ledger.lifetime()
        return {"presets": [{**m.to_dict(), "spend": lifetime.get(m.name)}
                            for m in providers.all_presets()]}

    @app.put("/api/presets/{name}")
    def upsert_preset(name: str, body: dict):
        """A model: the API it speaks, where it lives, and what it is called."""
        name = name.strip()
        if not name or " " in name:
            raise HTTPException(400, "name must be non-empty and contain no spaces")

        # Merge onto what is stored, so saving one field does not drop the key.
        stored = providers.preset(name)
        body = {**(stored.to_dict(redact=False) if stored else {}), **body}
        body.pop("has_key", None)
        body.pop("self_contained", None)

        kind = (body.get("kind") or "").strip()
        if kind not in KIND_DEFAULTS:
            raise HTTPException(400, (
                f"choose the API this model speaks: {', '.join(KIND_DEFAULTS)}"))
        candidate = ModelPreset.from_dict({**body, "kind": kind, "name": name})
        model = (candidate.model or "").strip()
        # Claude Code picks up whatever the CLI is signed in to when no id is
        # named, which is the normal way to use it — so an empty id is a choice
        # there rather than an omission.
        if not model and kind != "claudecode":
            raise HTTPException(400, "a model id is required")
        window = candidate.context_window
        reserved = int(body.get("max_tokens") or 0)
        # Output room is taken *out of* the window. Past half of it the agent has
        # less context than reply space, which is the opposite of the point.
        if reserved and reserved > window // 2:
            raise HTTPException(400, (
                f"max output {reserved} leaves only {max(0, window - reserved)} tokens of "
                f"a {window}-token window for context. Keep it under {window // 2}."))
        saved = providers.upsert_preset(ModelPreset.from_dict(
            {**body, "name": name, "model": model, "context_window": window}))
        _sync()
        return saved.to_dict()

    @app.post("/api/presets/{name}/rename")
    def rename_preset(name: str, body: dict):
        """Rename a model and re-point everything that referenced it.

        Agents store the model by name, so a rename that only touched the
        registry would silently drop every agent back to the default model.
        """
        new = (body.get("name") or "").strip()
        if not new or " " in new:
            raise HTTPException(400, "name must be non-empty and contain no spaces")
        if new == name:
            return providers.preset(name).to_dict()
        if providers.preset(new) is not None:
            raise HTTPException(409, f"a model named {new!r} already exists")
        renamed = providers.rename_preset(name, new)
        if renamed is None:
            raise HTTPException(404, "no such model")

        repointed = []
        for session in store.all():
            touched = False
            for role in session.team:
                if role.preset == name:
                    role.preset = new
                    touched = True
            if touched:
                store.save(session)
                repointed.append(session.name)
        if config.orchestrator.preset == name:
            config.orchestrator.preset = new
        _sync()
        return {**renamed.to_dict(), "repointed_sessions": repointed}

    @app.delete("/api/presets/{name}")
    def delete_preset(name: str, force: bool = False):
        # Presets are global, so the usage list is too — but it is a warning
        # to approve past, not a wall: an agent whose preset is gone falls
        # back to the default worker model, which is survivable and said.
        if not force:
            in_use = sorted({f"{r.name} ({s.name})" for s in store.all()
                             for r in s.team if r.preset == name})
            if in_use:
                raise HTTPException(409, (
                    f"model {name!r} is assigned to: {', '.join(in_use[:6])}. "
                    f"Delete it anyway and those agents fall back to the "
                    f"default worker model."))
        if not providers.delete_preset(name):
            raise HTTPException(404, "no such model")
        _sync()
        return {"deleted": name}

    @app.post("/api/models/discover")
    def discover_models(body: dict):
        """Ask an endpoint what it can run, so the model id can be a list.

        Takes the fields as typed rather than a saved model, so the list is
        there before anything is saved. A stored key is used when the form
        still holds the redacted placeholder.
        """
        kind = (body.get("kind") or "").strip()
        base_url = (body.get("base_url") or "").strip()
        if not base_url and kind in KIND_DEFAULTS:
            base_url = KIND_DEFAULTS[kind]["base_url"]

        key = body.get("api_key")
        if not key or key == "***":
            stored = providers.preset((body.get("name") or "").strip())
            key = stored.api_key if stored else None

        models, note = list_models(kind, base_url, key)
        return {"models": models, "note": note, "endpoint": base_url,
                "listed": bool(models)}

    @app.post("/api/presets/{name}/check")
    def check_preset(name: str):
        """Send a one-token probe so a key or URL error surfaces here."""
        if providers.preset(name) is None:
            raise HTTPException(404, "no such model")
        resolved = config.resolve(config.worker, preset=name)
        resolved.max_tokens = 16
        client = client_for(resolved)
        # The URL actually called, not the one that was typed. Most failures
        # here are a base URL with a path too many or too few, and saying which
        # address answered is the difference between a fix and a guess.
        endpoint = getattr(client, "endpoint", resolved.base_url)
        try:
            reply = client.complete([{"role": "user", "content": "Reply with OK."}])
        except BackendError as exc:
            return {"ok": False, "error": str(exc), "endpoint": endpoint}
        except Exception as exc:  # SDK-specific failures shouldn't 500 the UI
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "endpoint": endpoint}
        return {"ok": True, "model": resolved.model, "endpoint": endpoint,
                "reply": reply.text.strip()[:80]}

    @app.put("/api/config/orchestrator")
    def set_orchestrator(body: dict):
        """Change the orchestrator's provider/model from main settings."""
        if "preset" in body:
            config.orchestrator.preset = body["preset"] or None
        if body.get("provider"):
            config.orchestrator.provider = body["provider"]
        if "model" in body:
            config.orchestrator.model = body["model"] or None
        resolved = config.for_orchestrator()
        return {"preset": config.orchestrator.preset, "provider": resolved.provider,
                "model": resolved.model, "base_url": resolved.base_url}

    @app.get("/api/sessions/{session_id}/settings")
    def get_settings(session_id: str):
        """This project's run settings, and whether it inherited them."""
        held = (defaults_stores if session_id == DEFAULTS
                else stores_of(_need(store, session_id)))
        return {**held.settings.settings.to_dict(), "scale": list(POINTS),
                # True the first time a project is opened after this shipped:
                # its configuration was just carried in from the workspace.
                "migrated": held.migrated}

    @app.put("/api/config/planning")
    def set_planning(body: dict, session: str = ""):
        """Run settings for one project, written down so they survive a restart.

        They never were before: they lived in memory, so turning commits off
        lasted until the next restart — and this is a thing people restart.
        """
        held = stores_q(session)
        roles = held.roles
        changes: dict = {}

        if "max_step_points" in body:
            try:
                value = int(body["max_step_points"] or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "max_step_points must be a number")
            if value < 0 or value > 13:
                raise HTTPException(400, "max_step_points must be between 0 and 13 "
                                         "(0 turns splitting off)")
            changes["max_step_points"] = value
        if "escalation_preset" in body:
            name = (body.get("escalation_preset") or "").strip()
            if name and providers.preset(name) is None:
                raise HTTPException(400, f"unknown model {name!r}")
            changes["escalation_preset"] = name
        if "escalation_role" in body:
            name = (body.get("escalation_role") or "").strip()
            if name and roles.get(name) is None:
                raise HTTPException(400, f"unknown agent {name!r}")
            changes["escalation_role"] = name
        for flag in ("git_commits", "git_auto_init"):
            if flag in body:
                changes[flag] = bool(body[flag])
        for frame in ("plan_open", "plan_close"):
            if frame in body:
                name = (body.get(frame) or "").strip()
                if name and roles.get(name) is None and held.loops.get(name) is None:
                    raise HTTPException(400, f"unknown agent or loop {name!r}")
                changes[frame] = name

        settings = held.settings.update(**changes)
        return {**settings.to_dict(), "scale": list(POINTS)}

    # -------------------------------------------------------------- files

    #: Never listed and never opened. Reading a 40MB lockfile into the browser
    #: helps nobody, and node_modules is not what anyone came to review.
    HIDDEN_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".trance",
                   "dist", "build", ".pytest_cache", ".mypy_cache", ".next"}
    MAX_FILE_BYTES = 400_000

    def _project_of(session) -> Path:
        return Path(session.project_dir).expanduser().resolve()

    def _inside(root: Path, relative: str) -> Path | None:
        """Resolve a path and refuse anything that escapes the project."""
        return paths.inside(root, relative)

    @app.get("/api/sessions/{session_id}/files")
    def list_project_files(session_id: str):
        """The tree, flat, with sizes — the browser builds the shape."""
        session = _need(store, session_id)
        root = _project_of(session)
        if not root.exists():
            return {"root": str(root), "files": [], "error": "the project directory is gone"}

        found = []
        for path in sorted(root.rglob("*")):
            if any(part in HIDDEN_DIRS for part in path.relative_to(root).parts):
                continue
            if path.is_dir():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            found.append({"path": path.relative_to(root).as_posix(), "bytes": size,
                          "lines": _count_lines(path, size)})
            if len(found) >= 2000:
                break
        return {"root": str(root), "files": found, "totals": _by_extension(found)}

    #: Counting lines means reading the file, so anything this size is reported
    #: by bytes alone rather than holding up the listing.
    COUNT_UNDER_BYTES = 2_000_000

    def _count_lines(path: Path, size: int) -> int:
        if size > COUNT_UNDER_BYTES:
            return 0
        try:
            with path.open("rb") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def _by_extension(files: list[dict]) -> list[dict]:
        """Lines and bytes per file type, biggest first.

        What is actually in a project is a question about kinds of file, not
        about a total: 4,000 lines of JavaScript and 300 of CSS says something,
        and "4,300 lines" says almost nothing.
        """
        totals: dict[str, dict] = {}
        for item in files:
            name = item["path"].rsplit("/", 1)[-1]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name[1:] else "(no suffix)"
            entry = totals.setdefault(ext, {"ext": ext, "files": 0, "lines": 0, "bytes": 0})
            entry["files"] += 1
            entry["lines"] += item["lines"]
            entry["bytes"] += item["bytes"]
        return sorted(totals.values(), key=lambda e: (-e["lines"], -e["bytes"], e["ext"]))

    @app.get("/api/sessions/{session_id}/file")
    def read_project_file(session_id: str, path: str):
        session = _need(store, session_id)
        root = _project_of(session)
        target = _inside(root, path)
        if target is None or not target.is_file():
            raise HTTPException(404, f"no such file: {path}")
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise HTTPException(413, f"{path} is {size:,} bytes — too large to open here")
        try:
            text = target.read_text(encoding="utf8")
        except (OSError, UnicodeDecodeError):
            raise HTTPException(415, f"{path} is not text")
        return {"path": path, "content": text, "bytes": size,
                "lines": len(text.splitlines())}

    @app.get("/api/sessions/{session_id}/shot/{shot:path}")
    def read_screenshot(session_id: str, shot: str):
        """A screenshot a visual step took.

        Served from disk rather than carried in the event, because the history
        panel reads a step's events back whole and a base64 PNG in each one is
        what made that panel unusable the last time payloads grew.
        """
        session = _need(store, session_id)
        root = _project_of(session)
        target = _inside(root / SHOTS_DIR, shot)
        if target is None or not target.is_file() or target.suffix.lower() != ".png":
            raise HTTPException(404, f"no such screenshot: {shot}")
        # Shots are written once under a name that includes the step, so they
        # never change and the browser should not keep asking.
        return FileResponse(target, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.delete("/api/sessions/{session_id}/files")
    def clear_files(session_id: str):
        """Remove everything the run generated, keeping the project's state.

        .trance stays — it is trance's own memory of the project, and the
        request was to clear what the agents built, not what trance knows.
        .git stays too, deliberately: the wipe is committed, so "clear all"
        is an undoable act rather than a shredder, and the history of how
        the files came to be survives them.
        """
        session = _need(store, session_id)
        if session.status == "running":
            raise HTTPException(409, "the run is writing files right now — stop it first")
        project = _project_of(session)
        if not project.is_dir():
            return {"removed": 0}

        # Nothing keeps serving files that are about to not exist.
        _forget_preview(session)
        served = previews.pop(session_id, None)
        if served is not None:
            served.stop()

        removed = 0
        for entry in sorted(project.iterdir()):
            if entry.name in (STORE_DIR, ".git"):
                continue
            if entry.is_dir() and not entry.is_symlink():
                removed += sum(1 for f in entry.rglob("*") if f.is_file())
                shutil.rmtree(entry, ignore_errors=True)
            else:
                removed += 1
                entry.unlink(missing_ok=True)

        committed = False
        if vcs.is_repo(project) and config_for(session).git_commits:
            committed = bool(vcs.commit_all(project, "user: cleared the generated files"))
        bus.emit("files_cleared", session_id, agent="you", payload={
            "removed": removed, "committed": committed,
            "message": (f"Cleared {removed} generated file(s). .trance and the git "
                        f"history stay"
                        + (" — the wipe is a commit, so it can be undone."
                           if committed else ".")),
        })
        touch(session)
        return {"removed": removed, "committed": committed}

    @app.put("/api/sessions/{session_id}/file")
    def write_project_file(session_id: str, body: dict):
        """Your own edit. Committed like an agent's, so it is in the history."""
        session = _need(store, session_id)
        root = _project_of(session)
        target = _inside(root, body.get("path") or "")
        if target is None:
            raise HTTPException(400, "that path is outside the project")
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(400, "content is required")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf8")

        result = vcs.commit_all(root, f"you: edited {body['path']}") \
            if config.git_commits and vcs.is_repo(root) else None
        bus.emit("file_edited", session_id, agent="you", payload={
            "path": body["path"], "bytes": len(content),
            "sha": result.sha if result and result.ok else "",
            "message": f"You edited {body['path']}.",
        })
        return {"path": body["path"], "bytes": len(content),
                "committed": bool(result and result.ok)}

    @app.delete("/api/sessions/{session_id}/file")
    def delete_project_file(session_id: str, path: str):
        """Remove a file from the project. Committed, like an agent's change.

        Only files: removing a directory from here would take everything under
        it on one click, and nothing in this UI shows you what that would be.
        """
        session = _need(store, session_id)
        root = _project_of(session)
        target = _inside(root, path)
        if target is None or not target.exists():
            raise HTTPException(404, f"no such file: {path}")
        if target.is_dir():
            raise HTTPException(400, (
                f"{path} is a directory. Delete the files in it, or remove it "
                f"outside trance where you can see what goes with it."))
        target.unlink()

        result = vcs.commit_all(root, f"you: deleted {path}") \
            if config.git_commits and vcs.is_repo(root) else None
        bus.emit("file_deleted", session_id, agent="you", payload={
            "path": path, "sha": result.sha if result and result.ok else "",
            "message": f"You deleted {path}.",
        })
        return {"deleted": path, "committed": bool(result and result.ok)}

    #: The port a folder was last served on, so re-opening a page comes back at
    #: the same address. Anything pointed at the old one — a tunnel, a link you
    #: sent someone — survives the preview being restarted.
    preview_ports: dict[tuple, int] = {}

    #: One preview server per session, kept until it is stopped or the session
    #: goes. Restarting it for every click would give the browser a new origin
    #: each time and throw away whatever the page had in local storage.
    previews: dict[str, object] = {}
    app.state.previews = previews

    def _preview_state(session) -> Path:
        return Path(session.project_dir).expanduser() / ".trance" / "preview.json"

    def _remember_preview(session, payload: dict) -> None:
        """Write what is being served into the project's own state.

        A dev server runs in its own session and survives trance dying; the
        record is how the next trance knows it exists — rather than the port
        being held by a process no page mentions and no button can stop.
        """
        path = _preview_state(session)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf8")
        except OSError:
            pass                            # a preview that cannot persist still serves

    def _forget_preview(session) -> None:
        try:
            _preview_state(session).unlink(missing_ok=True)
        except OSError:
            pass

    def _revive_preview(session_id: str, session):
        """Re-attach what a previous trance was serving, if anything.

        The user pressed play and never pressed stop; a harness restart in
        between is trance's business, not theirs. A dev server that survived
        is adopted by pid; a static one died with the process, so it is simply
        served again from the recorded folder, on the recorded port where
        possible.
        """
        if session_id in previews:
            return previews.get(session_id)
        path = _preview_state(session)
        try:
            record = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return None
        if record.get("mode") == "dev":
            adopted = preview.adopt_dev(record)
            if adopted is None:
                _forget_preview(session)    # it died while trance was away
                return None
            previews[session_id] = adopted
            return adopted
        root = Path(record.get("root") or "")
        if not root.is_dir():
            _forget_preview(session)
            return None
        served = preview.serve(root, port=int(record.get("port") or 0))
        previews[session_id] = served
        preview_ports[(session_id, str(root))] = served.port
        return served

    @app.post("/api/sessions/{session_id}/preview/plan")
    async def plan_preview(session_id: str):
        """Ask the orchestrator how this project is started.

        Reading the README rather than pattern-matching package.json: a dev
        command is not always `npm run dev`, and a README that names a script
        usually names it because the obvious one does not work.
        """
        session = _need(store, session_id)
        try:
            answer = await asyncio.to_thread(
                orchestrator_agent.how_to_run, _project_of(session),
                config=config.for_orchestrator(), bus=bus, session_id=session_id)
        except BackendError as exc:
            raise HTTPException(502, str(exc)) from exc
        if not answer["command"] and not answer["static_instead"]:
            raise HTTPException(502, (
                "the orchestrator could not work out how this project is started — "
                "serve it as files, or add the command to the README."))
        return answer

    @app.post("/api/sessions/{session_id}/preview")
    async def start_preview(request: Request, session_id: str, body: dict | None = None):
        """Serve the folder a page lives in, and hand back its URL.

        With `of_message`, serve the project *as one iteration left it*: a
        detached worktree of that commit under .trance/versions becomes the
        root, and everything after — dev server or static, remembering,
        stopping, sharing — is this same procedure, deliberately not a second
        one. The worktree sits inside the project so a dev server finds
        node_modules by walking up.
        """
        session = _need(store, session_id)
        root = _project_of(session)
        body = body or {}

        of_message = str(body.get("of_message") or "").strip()
        version = ""
        if of_message:
            from ..agents.visual import default_page

            reply = next((m for m in session.chat if m.id == of_message), None)
            if reply is None or not reply.base:
                raise HTTPException(404, "no such request")
            target = _range_end(session, of_message)
            if not target:
                raise HTTPException(400, "this request's end point is not recorded")
            copy_dir = root / ".trance" / "versions" / target[:8]
            made = vcs.worktree_add(root, copy_dir, target)
            if not made:
                raise HTTPException(
                    409, f"could not check out {target[:8]}: {made.detail}")
            version, root = target[:8], copy_dir
            if not body.get("mode"):
                # Decided the way the visual tester decides: the version's own
                # dev server when its manifest wants one, static otherwise.
                page = default_page(root)
                wants = preview.dev_command(root, preview.web_root_for(root, page))
                if wants and wants.get("needed"):
                    body = {**body, "mode": "dev", "command": wants["command"],
                            "dir": str(Path(wants["dir"]).relative_to(root))}
                else:
                    body = {**body, "path": page}

        # Running the project rather than serving its files. Asked for
        # explicitly and with a command someone has seen: this starts a build
        # on the machine trance is running on, which is not something a preview
        # button should ever do by itself.
        if (body.get("mode") or "") == "dev":
            command = (body.get("command") or "").strip()
            if not command:
                raise HTTPException(400, "which command? ask /preview/plan first.")
            where = _inside(root, body.get("dir") or "") or root
            if not where.is_dir():
                raise HTTPException(400, f"{body.get('dir')!r} is not a directory here.")
            # Revive before replacing, exactly as stop does: after a restart
            # the registry is empty while the survivor from before it is still
            # running — replacing only the record leaked a whole dev tree.
            _revive_preview(session_id, session)
            existing = previews.pop(session_id, None)
            if existing is not None:
                existing.stop()
            try:
                running = await asyncio.to_thread(
                    preview.run_dev, where, command,
                    log_dir=Path(session.project_dir).expanduser() / ".trance")
            except preview.DevServerFailed as exc:
                bus.emit("preview_failed", session_id, agent="you", payload={
                    "command": command, "output": exc.output, "message": str(exc)})
                raise HTTPException(502, f"{exc}\n\n{exc.output}"[:4000]) from exc
            previews[session_id] = running
            _remember_preview(session, {
                "mode": "dev", "command": command, "root": running.root,
                "port": running.port, "pid": running.pid, "log": running.log,
                **({"version": version, "of_message": of_message} if version else {}),
            })
            here = running.at(request.url.hostname)
            bus.emit("preview", session_id, agent="you", payload={
                "url": here, "root": running.root, "port": running.port,
                "command": command,
                "message": (f"`{command}` is serving {Path(running.root).name}/ "
                            f"at {here}"),
            })
            shared = preview.public_url(running.port)
            return {**running.to_dict(), "open": here,
                    "network": running.url, "public": shared or "",
                    "version": version, "of_message": of_message,
                    "needs_build": False, "blocked_by": [], "build_command": "",
                    "hint": (preview.allowed_hosts_note(Path(running.root), shared)
                             if shared else "")}

        target = _inside(root, body.get("path") or "")
        if target is None or not target.exists():
            raise HTTPException(404, "no such file")

        web_root = preview.web_root_for(root, body.get("path") or "")
        page = target.name if target.is_file() else ""

        existing = previews.get(session_id)
        if existing is not None and existing.root == str(web_root):
            served = existing
        else:
            if existing is not None:
                existing.stop()
            served = preview.serve(
                web_root, port=preview_ports.get((session_id, str(web_root)), 0))
            previews[session_id] = served
            preview_ports[(session_id, str(web_root))] = served.port
        _remember_preview(session, {
            "mode": "static", "root": str(web_root), "port": served.port,
            **({"version": version, "of_message": of_message} if version else {}),
        })

        # Opened at the host this browser already used to reach trance, so a
        # phone on the same network gets a link that works from where it is.
        here = served.at(request.url.hostname) + page

        # Whether this page works as files is answerable by looking, so it is
        # looked at: a bare `import ... from "three"` is a specifier only a
        # bundler can resolve, and the page will load and then die on it.
        # Saying so is the whole of trance's business here — starting the dev
        # server is the user's call.
        dev = preview.dev_command(root, web_root)
        blockers = preview.bare_imports(web_root)
        needs_build = bool(blockers)
        bus.emit("preview", session_id, agent="you", payload={
            "url": here, "root": served.root, "port": served.port,
            "message": (f"Serving {Path(served.root).name}/ at {here} "
                        f"(on the network at {served.url})"
                        + (f" — but {blockers[0]['file']} imports "
                           f"'{blockers[0]['specifier']}', which only a build step "
                           f"can resolve." if needs_build else "")),
        })
        # If a tunnel is already pointed at this port, its URL is the one worth
        # handing to someone else — so it is offered here rather than left to be
        # found in the terminal window that started it.
        shared = preview.public_url(served.port)
        return {**served.to_dict(), "open": here, "network": served.url + page,
                "public": (shared + "/" + page) if shared else "",
                "version": version, "of_message": of_message,
                "needs_build": needs_build, "blocked_by": blockers,
                "build_command": (dev or {}).get("command", "")}

    #: Sessions whose orchestrator is mid-answer. One at a time per session:
    #: two proposals racing to replace the same flow is how one of them
    #: silently loses.
    thinking: set[str] = set()

    #: Tunnels trance started, so it can stop them again. One per session: a
    #: second public URL for the same folder is only confusing.
    tunnels: dict[str, object] = {}
    app.state.tunnels = tunnels

    @app.post("/api/sessions/{session_id}/share")
    def start_share(session_id: str, body: dict | None = None):
        """Publish the running preview, and hand back the link to send."""
        _need(store, session_id)
        served = previews.get(session_id)
        if served is None:
            raise HTTPException(409, "nothing is being served — open a page with ▷ first")

        existing = tunnels.get(session_id)
        if existing is not None and existing.running and existing.port == served.port:
            return existing.to_dict()
        if existing is not None:
            existing.stop()

        # Off by default. A tunnel with a password on it is the safer thing, and
        # the one the script writes; sharing with someone who has no password is
        # the deliberate choice, made here rather than assumed.
        policy = ""
        if (body or {}).get("protected"):
            policy = str(Path.home() / ".config" / "ngrok" / "trance-preview.yml")
            if not Path(policy).is_file():
                raise HTTPException(400, (
                    f"No traffic policy at {policy}. Run tools/preview-tunnel.sh once "
                    f"to have one written with a password."))
        try:
            tunnel = preview.start_tunnel(served.port, policy=policy)
        except preview.NoTunnelTool as exc:
            raise HTTPException(501, str(exc)) from exc
        except preview.TunnelBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

        # One agent, one tunnel: sharing this session re-aimed the same public
        # URL, so any other session's share just stopped being what it was.
        # Its badge kept saying "public link" while the link served someone
        # else's project — dropped and announced instead, to whoever is
        # watching that session.
        for other_id, other in list(tunnels.items()):
            if other_id == session_id:
                continue
            tunnels.pop(other_id, None)
            bus.emit("share_replaced", other_id, agent="you", payload={
                "url": other.url, "now_serving": served.root,
                "message": (f"The public link now serves "
                            f"{Path(served.root).name}/ — sharing moved to another "
                            f"session (one ngrok tunnel is all the account allows). "
                            f"Anyone holding {other.url} sees that project now."),
            })

        tunnels[session_id] = tunnel
        # A tunnel to a dev server is refused until Vite knows the host —
        # "Blocked request", which reads as a broken tunnel. A one-line edit
        # with a known shape does not need a person or an agent: the host is
        # written into the config here, and Vite restarts itself on config
        # changes. The pasteable hint remains for the shapes the edit cannot
        # recognise.
        allowed = preview.allow_host(Path(served.root), tunnel.url)
        hint = ("" if allowed["edited"] or allowed["note"]
                else preview.allowed_hosts_note(Path(served.root), tunnel.url))
        if allowed["edited"]:
            bus.emit("preview", session_id, agent="you", payload={
                "url": tunnel.url, "root": served.root, "port": served.port,
                "message": allowed["note"].capitalize() + ".",
            })
        bus.emit("preview", session_id, agent="you", payload={
            "url": tunnel.url, "root": served.root, "port": served.port,
            "message": (f"Sharing {Path(served.root).name}/ at {tunnel.url}"
                        + ("" if policy else " — anyone with the link can open it.")
                        + (f"\n\n{hint}" if hint else "")),
        })
        return {**tunnel.to_dict(), "hint": hint}

    @app.delete("/api/sessions/{session_id}/share")
    def stop_share(session_id: str):
        _need(store, session_id)
        tunnel = tunnels.pop(session_id, None)
        if tunnel is not None:
            tunnel.stop()
        return {"stopped": tunnel is not None}

    @app.delete("/api/sessions/{session_id}/preview")
    def stop_preview(session_id: str):
        session = _need(store, session_id)
        _forget_preview(session)
        # Revive before stopping: a dev server from before the restart is not
        # in the registry, and "stop" that leaves it running is the old bug
        # with a button on it.
        _revive_preview(session_id, session)
        served = previews.pop(session_id, None)
        if served is not None:
            served.stop()
        # A tunnel to a server that is gone is a link that answers 502.
        tunnel = tunnels.pop(session_id, None)
        if tunnel is not None:
            tunnel.stop()
        return {"stopped": served is not None}

    @app.get("/api/sessions/{session_id}/preview")
    def preview_status(session_id: str):
        session = _need(store, session_id)
        served = _revive_preview(session_id, session)
        if served is None:
            return {"root": "", "port": 0, "url": "", "public": ""}
        if not served.alive():
            # It died on its own while nobody was serving the page. Forgetting
            # it here keeps "running" honest.
            previews.pop(session_id, None)
            _forget_preview(session)
            return {"root": "", "port": 0, "url": "", "public": ""}
        try:
            record = json.loads(_preview_state(session).read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            record = {}
        return {"port": 0, **served.to_dict(),
                "version": record.get("version", ""),
                "of_message": record.get("of_message", ""),
                "public": preview.public_url(served.port)}

    # ------------------------------------------------------------- review

    @app.post("/api/sessions/{session_id}/review")
    def add_review_note(session_id: str, body: dict):
        """Leave a comment, to be sent as work later.

        A path and line are optional. Plenty of review is about the thing as a
        whole — "the controls are unusable on a phone" — and making that fit a
        line number means picking one arbitrarily, or not writing it down.
        """
        session = _need(store, session_id)
        note = (body.get("note") or "").strip()
        path = (body.get("path") or "").strip()
        if not note:
            raise HTTPException(400, "a note is required")
        entry = {
            "id": f"rv_{uuid4().hex[:8]}", "path": path,
            "line": max(1, int(body.get("line") or 1)) if path else 0,
            "code": (body.get("code") or "")[:400] if path else "",
            "note": note,
        }
        session.review.append(entry)
        touch(session)
        bus.emit("review_note", session_id, agent="you", payload=entry)
        return entry

    @app.delete("/api/sessions/{session_id}/review/{note_id}")
    def drop_review_note(session_id: str, note_id: str):
        session = _need(store, session_id)
        before = len(session.review)
        session.review = [n for n in session.review if n["id"] != note_id]
        if len(session.review) == before:
            raise HTTPException(404, "no such note")
        touch(session)
        return {"deleted": note_id, "left": len(session.review)}

    @app.post("/api/sessions/{session_id}/review/finish")
    def finish_review(session_id: str, body: dict | None = None):
        """Turn the comments into a step the flow will actually run."""
        session = _need(store, session_id)
        if not session.review:
            raise HTTPException(400, "there are no review comments to send")

        by_file: dict[str, list[dict]] = {}
        general: list[dict] = []
        for note in session.review:
            (general if not note.get("path") else
             by_file.setdefault(note["path"], [])).append(note)
        rendered = []
        # First, because a comment about the whole thing is usually the point,
        # and the line-by-line ones are details under it.
        if general:
            rendered.append("### About this change overall\n"
                            + "\n".join(f"- {n['note']}" for n in general))
        for path, notes in by_file.items():
            lines = [f"### {path}"]
            for note in sorted(notes, key=lambda n: n["line"]):
                lines.append(f"- line {note['line']}"
                             + (f" (`{note['code'].strip()}`)" if note.get("code") else "")
                             + f": {note['note']}")
            rendered.append("\n".join(lines))

        held = stores_of(session)
        roles, loops = held.roles, held.loops
        loop_name = (body or {}).get("loop") or ""
        if loop_name and loops.get(loop_name) is None:
            raise HTTPException(400, f"unknown loop {loop_name!r}")
        if not loop_name:
            loop_name = _loop_for_review(session)

        task = ("Address this code review. Comments under a filename name a line; "
                "make the change each one asks for and nothing else. Comments under "
                "\"About this change overall\" are about the result rather than any "
                "one line — find what they refer to before changing anything.\n\n"
                + "\n\n".join(rendered)
                + "\n\nWhere a comment is a question rather than an instruction, answer "
                  "it in your report instead of changing code.")
        step = Step(role="" if loop_name else "developer", loop=loop_name, task=task,
                    points=3, max_loops=2)
        # Next, not last. The comments name lines in the code as it is right
        # now; run them after everything else and those lines have moved.
        position = session.flow.insert_next(step)

        record = {
            "id": f"rev_{uuid4().hex[:8]}", "step_id": step.id,
            "notes": list(session.review),
            "before": vcs.head(_project_of(session)),
            "after": "", "files": [],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        session.reviews.append(record)
        session.review = []

        for name in (loops.get(loop_name).roles() if loops.get(loop_name) else [step.role]):
            if name and all(r.name != name for r in session.team) and roles.get(name):
                session.team.append(roles.get(name))
        session.team = roles.resolve_team(session.team)

        touch(session)
        bus.emit("review_sent", session_id, agent="you", payload={
            "review": record["id"], "step_id": step.id, "notes": len(record["notes"]),
            "loop": loop_name, "position": position,
            "message": (f"Sent {len(record['notes'])} review comment(s) as step "
                        f"{position} — it runs next."),
        })
        bus.emit("flow_updated", session_id, payload={"flow": session.flow.to_dict()})
        started = ensure_running(session)
        return {**record, "started": started, "flow": session.flow.to_dict()}

    @app.get("/api/sessions/{session_id}/review/changes")
    def review_changes(session_id: str, review: str = ""):
        """What was actually done about a review, from git."""
        session = _need(store, session_id)
        root = _project_of(session)
        record = next((r for r in reversed(session.reviews)
                       if not review or r["id"] == review), None)
        if record is None:
            return {"review": None, "files": [], "diff": ""}

        step = session.flow.find(record["step_id"])
        done = step is not None and step.status in Flow.TERMINAL
        after = record["after"] or (vcs.head(root) if done else "")
        if done and not record["after"]:
            record["after"] = after
            record["files"] = vcs.changed_between(root, record["before"], after)
            touch(session)
        return {
            "review": record["id"], "status": step.status if step else "gone",
            "notes": record["notes"], "before": record["before"], "after": after,
            "files": record["files"] or vcs.changed_between(root, record["before"], after),
            "diff": vcs.diff(root, record["before"], after) if after else "",
            # One commit per step, so this is what each agent did and in what
            # order — more use than one combined diff when several ran.
            "commits": vcs.commits_between(root, record["before"], after) if after else [],
        }

    @app.post("/api/agents/draft-prompt")
    async def draft_agent_prompt(body: dict):
        """A first draft of an agent's system prompt, from its name."""
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "a name is required to write a prompt about")
        session = store.get(body.get("session") or "") if body.get("session") else None
        try:
            text = await asyncio.to_thread(
                orchestrator_agent.draft_agent_prompt, name,
                description=(body.get("description") or "").strip(),
                goal=(session.goal if session else ""),
                config=config.for_orchestrator(), bus=bus,
                session_id=session.id if session else "")
        except BackendError as exc:
            raise HTTPException(502, str(exc)) from exc
        text = (text or "").strip()
        if not text:
            raise HTTPException(502, "the model returned nothing to use")
        return {"name": name, "system_prompt": text}

    @app.get("/api/usage")
    def lifetime_usage():
        """What every model has been asked to do, across every session.

        Read from the ledger rather than assembled from the presets, which
        carry the same numbers: a model you have since deleted still spent what
        it spent, and a total that quietly drops it is not a total.
        """
        rows = [{"model": name, **spend} for name, spend in ledger.lifetime().items()]
        rows.sort(key=lambda row: -row["total"])
        return {"models": rows,
                "total": sum(row["total"] for row in rows),
                "calls": sum(row["calls"] for row in rows)}

    @app.get("/api/sessions/{session_id}/usage")
    def session_usage(session_id: str):
        """What this run has asked of each model."""
        _need(store, session_id)
        rows = ledger.for_session(session_id)
        return {"models": rows,
                "total": sum(r["total"] for r in rows),
                "calls": sum(r["calls"] for r in rows)}

    def _loop_for_review(session) -> str:
        held = stores_of(session)
        roles, loops = held.roles, held.loops
        """Which loop should answer a review.

        It used to be whichever loop sorted first, which is how a review ended
        up in a review loop: a reviewer reviewing the answer to a review, with
        nothing run and nothing proved.

        A review asks for changes, and what shows a change landed is running the
        project's tests. So: a loop that contains an agent which can both verify
        and run commands — a tester, whatever it is called. Preferring one the
        flow already uses keeps a frontend review with the frontend's own loop
        rather than a general one.
        """
        can_test = {r.name for r in roles.all()
                    if r.verifier and "commands" in (r.toolsets or [])}
        testing = [loop for loop in loops.all()
                   if can_test & set(loop.roles())]
        if not testing:
            return next((loop.name for loop in loops.all()), "")

        in_use = [s.loop for s in session.flow.steps if s.loop]
        for name in reversed(in_use):            # the most recent one first
            for loop in testing:
                if loop.name == name:
                    return loop.name
        return testing[0].name

    @app.get("/api/sessions/{session_id}/reviews")
    def review_history(session_id: str):
        """Every review sent for this session, newest first.

        Summaries only — what was asked for, whether it has run, and the commits
        it produced. The patches come one at a time, when a commit is opened.
        """
        session = _need(store, session_id)
        root = _project_of(session)

        out = []
        for record in reversed(session.reviews):
            step = session.flow.find(record["step_id"])
            done = step is not None and step.status in Flow.TERMINAL
            after = record["after"] or (vcs.head(root) if done else "")
            if done and not record["after"]:      # settle it once, on first sight
                record["after"] = after
                record["files"] = vcs.changed_between(root, record["before"], after)
                touch(session)
            out.append({
                "review": record["id"], "at": record.get("at", ""),
                "status": step.status if step else "gone",
                "notes": record["notes"], "before": record["before"], "after": after,
                "files": record["files"] or vcs.changed_between(root, record["before"], after),
                "commits": vcs.commits_between(root, record["before"], after) if after
                           else vcs.commits_between(root, record["before"]),
            })
        return {"reviews": out}

    def _range_end(session, message_id: str) -> str:
        """Where a proposal's work ends: the next proposal's base, else HEAD."""
        root = Path(session.project_dir).expanduser()
        proposals = [m for m in session.chat if m.base]
        for earlier, later in zip(proposals, proposals[1:]):
            if earlier.id == message_id:
                return later.base
        return vcs.head(root) if vcs.is_repo(root) else ""

    def _request_of(session, reply) -> str:
        """The user message a proposal answered — the text a person recognises."""
        at = next((i for i, m in enumerate(session.chat) if m.id == reply.id), -1)
        for earlier in reversed(session.chat[:at]):
            if earlier.role == "user":
                return earlier.content
        return ""

    @app.get("/api/sessions/{session_id}/requests")
    def request_history(session_id: str):
        """Every iteration the user asked for, newest first — one item each.

        The card the commits page draws collapsed: the request's own words as
        the title, the screenshots its run produced as the face, and enough
        counts to know whether expanding is worth it. The expanded detail
        stays with /messages/{id}/commits.
        """
        session = _need(store, session_id)
        root = Path(session.project_dir).expanduser()
        shots_root = root / ".trance" / "shots"
        by_id = {step.id: step for step in session.flow.steps}
        items = []
        for reply in session.chat:
            if not reply.base:
                continue
            after = _range_end(session, reply.id)
            commits = (vcs.commits_between(root, reply.base, after)
                       if reply.base and after else [])
            files = (vcs.changed_between(root, reply.base, after)
                     if reply.base and after else [])
            # The pictures this iteration produced: what the user attached to
            # the request, then what the visual steps photographed.
            shots = list(reply.images or [])
            for step_id in reply.steps or []:
                folder = shots_root / re.sub(r"[^A-Za-z0-9._-]+", "-", step_id)
                if folder.is_dir():
                    shots += [f"{folder.name}/{p.name}"
                              for p in sorted(folder.glob("*.png"))[-4:]]
            steps = [by_id[i] for i in (reply.steps or []) if i in by_id]
            items.append({
                "reply_id": reply.id, "ts": reply.ts,
                "request": _request_of(session, reply),
                "base": reply.base, "after": after,
                "commit_count": len(commits), "file_count": len(files),
                "still_to_run": sum(1 for s in steps
                                    if s.status not in Flow.TERMINAL),
                "worked_seconds": round(sum(s.seconds or 0 for s in steps), 1),
                "shots": shots[:8],
            })
        return {"requests": list(reversed(items))}

    @app.post("/api/sessions/{session_id}/messages/{message_id}/rewind")
    def rewind_to_message(session_id: str, message_id: str):
        """Put the project back to exactly where this iteration left it.

        The abandoned tip is saved on a branch first — a hard reset with no
        way back is deletion — then the branch and tree move to this
        request's end, and the chat and plan below it are trimmed so the
        session continues from that point. The trimmed chat is archived in
        the session's own directory, beside its trace.
        """
        session = _need(store, session_id)
        if session.status == "running":
            raise HTTPException(409, "the run is writing right now — stop it first")
        reply = next((m for m in session.chat if m.id == message_id), None)
        if reply is None or not reply.base:
            raise HTTPException(404, "no such request")
        project = _project_of(session)
        if not vcs.is_repo(project):
            raise HTTPException(400, "this project is not a git repository")
        target = _range_end(session, message_id)
        if not target:
            raise HTTPException(400, "this request's end point is not recorded")

        # Whatever is lying around belongs to the tip being left behind.
        vcs.commit_all(project, "before rewinding")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        keep = f"trance/pre-rewind-{stamp}"
        saved = vcs.make_branch(project, keep)
        if not saved:
            raise HTTPException(409, f"could not save the current tip: {saved.detail}")
        moved = vcs.reset_hard(project, target)
        if not moved:
            raise HTTPException(409, f"the reset did not apply: {moved.detail}")

        # The history below this point disappears from the session — archived,
        # not destroyed, like the code on its branch.
        at = next(i for i, m in enumerate(session.chat) if m.id == message_id)
        trimmed = session.chat[at + 1:]
        if trimmed:
            archive = session.store_dir / f"chat-rewound-{stamp}.json"
            archive.write_text(json.dumps([m.__dict__ for m in trimmed],
                                          default=str, indent=2), encoding="utf8")
            session.chat = session.chat[:at + 1]
            gone_steps = {sid for m in trimmed for sid in (m.steps or [])}
            session.flow.steps = [s for s in session.flow.steps
                                  if s.id not in gone_steps]
        touch(session)
        bus.emit("rewound", session_id, agent="you", payload={
            "to": target, "kept": keep,
            "message": (f"Rewound the project to {target[:8]} — where this request "
                        f"left it. Everything after is on branch {keep}."),
        })
        bus.emit("flow_updated", session_id, payload={"flow": session.flow.to_dict()})
        return {"to": target, "kept_branch": keep,
                "trimmed_messages": len(trimmed)}

    @app.get("/api/sessions/{session_id}/messages/{message_id}/commits")
    def commits_for_message(session_id: str, message_id: str):
        """What came of one thing the orchestrator said it would do.

        A request becomes a plan, the plan becomes a run, and the run becomes
        commits — and until now the only way back from the last to the first
        was to remember. The reply records where the code stood when it was
        written; the range ends at the next reply that proposed something, so
        two requests in one session do not claim each other's work.
        """
        session = _need(store, session_id)
        message = next((m for m in session.chat if m.id == message_id), None)
        if message is None:
            raise HTTPException(404, "no such message")

        root = Path(session.project_dir).expanduser()
        after = _range_end(session, message_id)

        steps = [step.to_dict() for step in session.flow.steps
                 if step.id in (message.steps or [])]
        pending = [s for s in steps if s["status"] not in Flow.TERMINAL]
        return {
            "message": {"id": message.id, "role": message.role,
                        "content": message.content, "ts": message.ts},
            "base": message.base,
            "after": after,
            "steps": steps,
            "still_to_run": len(pending),
            "commits": (vcs.commits_between(root, message.base, after)
                        if message.base and after else []),
            "files": (vcs.changed_between(root, message.base, after)
                      if message.base and after else []),
        }

    @app.get("/api/sessions/{session_id}/commits")
    def commit_log(session_id: str, limit: int = 100):
        """The project's git history, plain. The by-request view answers "what
        came of what I asked"; this answers "what is actually in the repo" —
        including the user's own commits and the clears, which no request owns.
        """
        session = _need(store, session_id)
        project = _project_of(session)
        if not vcs.is_repo(project):
            return {"commits": []}
        rows = vcs.log(project, limit=max(1, min(int(limit), 500)))
        return {"commits": [{**row, "short": row["sha"][:8]} for row in rows]}

    @app.get("/api/sessions/{session_id}/commit/{sha}")
    def show_commit(session_id: str, sha: str):
        """One commit of this project: its message, its stat, its patch."""
        session = _need(store, session_id)
        found = vcs.show(_project_of(session), sha)
        if not found:
            raise HTTPException(404, f"no such commit: {sha}")
        return found

    # -------------------------------------------------------------- loops

    def _loop_context(roles):
        known = {r.name for r in roles.all()}
        return known, {r.name for r in roles.all() if r.verifier}

    def seed_loop_checks(held) -> None:
        """Copy each node's agent's standing checks onto the node, once.

        The same honesty the plan got: merged at run time the chain was
        invisible — the loops editor showed one thing, the engine ran another.
        Copied onto the node, the editor shows what will run and can change
        it, and a check taken off a loop stays off.
        """
        loops, roles = held.loops, held.roles
        for loop in loops.all():
            grew = False
            for node in loop.nodes:
                if node.checks_seeded or not node.role:
                    continue
                always = list(getattr(roles.get(node.role), "checks", None) or [])
                if not always:
                    continue
                node.checks_chain = merge_checks(node.checks, always)
                node.checks_seeded = True
                grew = True
            if grew:
                loops.upsert(loop)

    @app.get("/api/loops")
    def list_loops(session: str = ""):
        held = stores_q(session)
        seed_loop_checks(held)
        loops, roles = held.loops, held.roles
        return {"loops": [l.to_dict() for l in loops.all()],
                "outcomes": list(EXITS), "stops": list(STOP),
                "agents": [r.name for r in roles.all() if r.name != "orchestrator"],
                "verifiers": [r.name for r in roles.all() if r.verifier]}

    @app.put("/api/loops/{name}")
    def upsert_loop(name: str, body: dict, session: str = ""):
        held = stores_q(session)
        loops, roles = held.loops, held.roles
        loop = Loop.from_dict({**body, "name": name.strip()})
        known, verifiers = _loop_context(roles)
        error = validate_loop(loop, known, verifiers)
        if error:
            raise HTTPException(400, error)
        saved = loops.upsert(loop)
        bus.emit("loops_updated", "system", payload={"name": saved.name})
        return saved.to_dict()

    @app.delete("/api/loops/{name}")
    def delete_loop(name: str, session: str = "", force: bool = False):
        held = stores_q(session)
        loops = held.loops
        if loops.get(name) is None:
            raise HTTPException(404, "no such loop")
        if not force:
            used = [f"step {n} of {s.name}" for s in _sessions_of(held)
                    for n, step in enumerate(s.flow.steps, 1) if step.loop == name]
            if used:
                raise HTTPException(409, (
                    f"{name!r} is still used by: {', '.join(used[:8])}. "
                    f"Delete it anyway and those steps will fail when they run, "
                    f"saying the loop was deleted."))
        loops.delete(name)
        return {"deleted": name}

    # ----------------------------------------------------------- sessions

    @app.get("/api/workspace")
    def get_workspace():
        """Where new projects go, and a free name/directory to start from."""
        root = config.workspace_root
        taken = {Path(s.project_dir).name for s in store.all()}
        base = "project"
        name = base
        n = 1
        while name in taken or (root / name).exists():
            n += 1
            name = f"{base}-{n}"
        return {
            "workspace": str(root),
            "state_dir": str(Path(config.runs_dir).resolve()),
            "writable": os.access(root, os.W_OK),
            "suggested_name": name,
            "suggested_dir": str(root / name),
        }

    @app.post("/api/check-path")
    def check_path(body: dict):
        """Validate a project directory before the user commits to it."""
        problem, normalized = check_project_dir((body.get("project_dir") or "").strip())
        return {"ok": problem is None, "error": problem, "path": normalized}

    @app.get("/api/sessions")
    def list_sessions():
        return [s.to_dict(include_flow=False) for s in store.all()]

    @app.post("/api/sessions")
    async def create_session(body: dict):
        name = (body.get("name") or "untitled").strip()
        project_dir = (body.get("project_dir") or "").strip()
        if not project_dir:
            # No path given: that is the normal case. The workspace is where
            # projects go and the name is the folder — asking for an absolute
            # path as well was asking the same question twice.
            root = config.workspace_root
            project_dir = str(root / folder_for(name))
            if not Path(project_dir).resolve().is_relative_to(root.resolve()):
                raise HTTPException(400, f"{name!r} does not name a folder.")
        problem, project_dir = check_project_dir(project_dir)
        if problem:
            raise HTTPException(400, problem)
        session = store.create(name, project_dir)
        bus.emit("session_created", session.id, payload=session.to_dict())
        return session.to_dict()

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        session = refresh_team(_need(store, session_id))
        data = session.to_dict()
        data["resolved_models"] = {
            role.name: {"preset": role.preset, "provider": (m := config.for_role(role)).provider,
                        "model": m.model, "base_url": m.base_url,
                        "context_window": m.context_window}
            for role in session.team
        }
        return data

    def _safe_to_remove(project: Path) -> str:
        """Why this directory must not be deleted, or "" if it may be.

        Deleting a directory the user named by hand is the one action here that
        cannot be undone from inside trance, so it is allowed only for a project
        that lives under the workspace root — a folder trance made. A path they
        typed themselves, a home directory, or the workspace itself are refused
        whatever the request says.
        """
        root = config.workspace_root.resolve()
        try:
            target = project.resolve()
        except OSError as exc:
            return str(exc)
        if target == target.parent:
            return "that is the filesystem root"
        if target == Path.home().resolve():
            return "that is your home directory"
        if target == root:
            return "that is the workspace itself, not a project in it"
        if root not in target.parents:
            return (f"{target} is outside the workspace ({root}). Delete it yourself, "
                    f"where you can see what is in it.")
        return ""

    def _shared_with(session) -> list[str]:
        """Other sessions working in the same directory. Two sessions on one
        folder are two views of one project, and deleting one of them must not
        pull the floor from under the other."""
        mine = Path(session.project_dir).expanduser().resolve()
        return [other.name for other in store.all()
                if other.id != session.id
                and Path(other.project_dir).expanduser().resolve() == mine]
        return ""

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str, files: bool = False):
        """Delete a session and its trace.

        `files=true` takes the project directory with it. Off by default: the
        agents' work outliving the session that made it is the safe way round,
        and a session is cheap to recreate over the same folder.
        """
        # Nothing should outlive the session it belongs to — least of all a
        # public URL to a folder whose session no longer exists.
        for registry in (previews, tunnels):
            running = registry.pop(session_id, None)
            if running is not None:
                running.stop()
        ledger.forget(session_id)
        session = _need(store, session_id)
        if session.status == "running":
            session.stop()  # let the engine unwind before the directory goes
        project = _project_of(session)
        refusal = _safe_to_remove(project) if files else ""
        if files and not refusal:
            sharing = _shared_with(session)
            if sharing:
                refusal = (f"the session(s) {', '.join(sharing[:4])} work in the same "
                           f"directory. Delete them too, or delete this session "
                           f"without its files.")
        if files and refusal:
            raise HTTPException(400, f"the session was not deleted: {refusal}")

        if not store.delete(session_id):
            raise HTTPException(404, "no such session")

        removed = False
        if files and project.exists():
            shutil.rmtree(project, ignore_errors=True)
            removed = not project.exists()
            workspace.forget(project)
        return {"deleted": session_id, "project_dir": session.project_dir,
                "files_deleted": removed}


    #: A pasted screenshot, capped. Four is more than any bug report needs, and
    #: an 8MB image costs more in tokens than it can possibly say.
    MAX_CHAT_IMAGES = 4
    MAX_CHAT_IMAGE_BYTES = 8_000_000

    def _save_chat_images(session, raw: list) -> list[str]:
        """Store pasted images beside the run's other screenshots.

        Under .trance/shots so the endpoint that already serves screenshots
        serves these too, and so they travel with the project rather than
        living in a session file as base64.
        """
        if not isinstance(raw, list) or not raw:
            return []
        folder = _project_of(session) / SHOTS_DIR / "chat"
        folder.mkdir(parents=True, exist_ok=True)

        out: list[str] = []
        for item in raw[:MAX_CHAT_IMAGES]:
            data = str(item or "")
            if "," in data and data.startswith("data:"):
                data = data.split(",", 1)[1]
            try:
                blob = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(400, "an attached image was not valid base64")
            if not blob:
                continue
            if len(blob) > MAX_CHAT_IMAGE_BYTES:
                raise HTTPException(413, (
                    f"an attached image is {len(blob):,} bytes; the limit is "
                    f"{MAX_CHAT_IMAGE_BYTES:,}"))
            if not blob.startswith(b"\x89PNG\r\n\x1a\n") and not blob.startswith(b"\xff\xd8"):
                raise HTTPException(400, "attached images must be PNG or JPEG")
            name = f"chat/{uuid4().hex[:10]}.png"
            (_project_of(session) / SHOTS_DIR / name).write_bytes(blob)
            out.append(name)
        return out

    def _chat_history(session) -> list[dict]:
        """The conversation, with pictures in it where the model can take them.

        A model that cannot see gets told an image was attached rather than
        having it silently dropped — an orchestrator answering about a
        screenshot it never received is worse than one that says it cannot.
        """
        kind = getattr(config.for_orchestrator(), "kind", "") or "llamacpp"
        sees = kind in VISION_KINDS
        root = _project_of(session) / SHOTS_DIR

        history: list[dict] = []
        for message in session.chat:
            role = "assistant" if message.role == "orchestrator" else "user"
            images = list(getattr(message, "images", None) or [])
            if not images:
                history.append({"role": role, "content": message.content})
                continue
            if not sees:
                history.append({"role": role, "content": (
                    message.content
                    + f"\n\n[{len(images)} screenshot(s) were attached, but this model "
                      f"cannot be shown images — ask about them in words.]")})
                continue
            blocks: list[dict] = [{"type": "text", "text": message.content or
                                   "(a screenshot, with nothing written with it)"}]
            for name in images:
                target = paths.inside(root, name)
                if target is None or not target.is_file():
                    continue
                blocks.append(image_block(target.read_bytes(), kind))
            history.append({"role": role, "content": blocks})
        return history

    #: Fields worth megabytes that a history panel never shows: the prompt that
    #: went out, the reasoning behind it, the whole file that came back. One
    #: step of a long run is 13MB and three quarters of it is `messages`.
    HEAVY = ("messages", "reasoning", "rendered", "raw")

    def _state_of(session) -> tuple:
        """What a page draws from the session, as one comparable value.

        Not the whole snapshot: run_seconds ticks every second and would make
        every event look like a change, which is the megabyte-a-second version
        of never updating at all.
        """
        return (
            session.status, session.paused, session.error,
            len(session.chat), len(session.review),
            tuple((step.id, step.status, step.runs) for step in session.flow.steps),
        )

    def _slim(event: dict) -> dict:
        """One event, minus what only the inspector opens."""
        payload = dict(event.get("payload") or {})
        dropped = {}
        for key in HEAVY:
            value = payload.get(key)
            if value in (None, "", [], {}):
                continue
            dropped[key] = len(json.dumps(value, default=str))
            payload.pop(key)
        if payload.get("result") and len(str(payload["result"])) > 4000:
            payload["result"] = str(payload["result"])[:4000] + "…"
        if dropped:
            payload["_omitted"] = dropped
        return {**event, "payload": payload}

    @app.get("/api/sessions/{session_id}/events")
    def get_events(session_id: str, step: str = "", limit: int = 0,
                   tail: bool = False, full: bool = False):
        """This session's events, optionally only one step's.

        A finished run is thousands of events and tens of megabytes, nearly all
        of it prompts. Asking for the step you are looking at costs a fraction
        of that, which is what the step detail does rather than holding the lot
        in the browser.
        """
        _need(store, session_id)
        events = history_for(session_id)
        if step:
            events = [e for e in events if e.step_id == step]
        if tail and limit <= 0:
            limit = CONSOLE_TAIL
        total = len(events)
        if limit > 0:
            events = events[-limit:]
        rows = [e.to_dict() for e in events]
        # Slim unless asked otherwise. A panel that lists what happened does not
        # need the prompts, and shipping them is the difference between a step
        # opening at once and a tab that stops responding.
        if not full:
            rows = [_slim(r) for r in rows]
        return ({"events": rows, "total": total, "shown": len(rows)} if tail
                else rows)

    @app.get("/api/sessions/{session_id}/events/{event_id}")
    def get_one_event(session_id: str, event_id: str):
        """One event in full, for when the inspector actually opens it."""
        _need(store, session_id)
        found = next((e for e in history_for(session_id) if e.id == event_id), None)
        if found is None:
            raise HTTPException(404, "no such event")
        return found.to_dict()

    @app.get("/api/sessions/{session_id}/memory")
    def get_memory(session_id: str):
        """What the team has written down — the same text every agent is given."""
        session = _need(store, session_id)
        memory = ProjectMemory(Path(session.project_dir).expanduser())
        return {"path": str(memory.path), "notes": memory.notes(),
                "raw": memory.raw(), "prompt_view": memory.for_prompt(),
                "oversized": memory.oversized(), "max_notes": MAX_NOTES}

    @app.post("/api/sessions/{session_id}/memory/compact")
    def compact_memory(session_id: str):
        """Compact on demand, using the orchestrator's model."""
        session = _need(store, session_id)
        memory = ProjectMemory(Path(session.project_dir).expanduser())
        model_config = config.for_orchestrator()

        def rewrite(text: str) -> str:
            return client_for(model_config).complete([
                {"role": "system", "content": COMPACT_PROMPT},
                {"role": "user", "content": text},
            ]).text

        result = memory.compact(rewrite)
        bus.emit("memory_compacted", session_id, agent="orchestrator", payload=result)
        return {**result, "raw": memory.raw(), "notes": memory.notes()}

    @app.put("/api/sessions/{session_id}/memory")
    def put_memory(session_id: str, body: dict):
        """Let the user correct it. A wrong shared fact misleads every agent."""
        session = _need(store, session_id)
        memory = ProjectMemory(Path(session.project_dir).expanduser())
        memory.path.parent.mkdir(parents=True, exist_ok=True)
        memory.path.write_text(body.get("raw") or "", encoding="utf8")
        return {"notes": memory.notes(), "raw": memory.raw()}

    # --------------------------------------------------------------- chat

    @app.post("/api/sessions/{session_id}/chat")
    async def chat(session_id: str, body: dict):
        session = _need(store, session_id)
        text = (body.get("message") or "").strip()
        saved = _save_chat_images(session, body.get("images") or [])
        if not text and not saved:
            raise HTTPException(400, "message is required")

        # One at a time. Two questions in flight mean two proposals racing to
        # replace the same flow, and the loser's work is silently gone — the
        # second answer is written against a conversation the first has already
        # changed.
        if session_id in thinking:
            raise HTTPException(409, (
                "The orchestrator is still answering your last message. "
                "Wait for it — asking again now would give you two plans "
                "written against different conversations."))
        thinking.add(session_id)

        session.chat.append(ChatMessage(role="user", content=text, images=saved))
        bus.emit("chat", session_id, agent="user",
                 payload={"content": text, "images": saved})

        history = _chat_history(session)
        try:
            result = await asyncio.to_thread(
                orchestrator_agent.chat,
                messages=history,
                project_dir=Path(session.project_dir),
                config=config.for_orchestrator(),
                bus=bus,
                session_id=session_id,
                roles=stores_of(session).roles.all(),
                loops=stores_of(session).loops,
                settings=stores_of(session).settings.settings,
            )
        except BackendError as exc:
            bus.emit("error", session_id, payload={"message": str(exc)})
            raise HTTPException(502, str(exc))
        finally:
            # However it ended. A lock that outlives a failure locks the
            # orchestrator out of the session for good.
            thinking.discard(session_id)

        session.chat.append(ChatMessage(role="orchestrator", content=result["text"]))
        bus.emit("chat", session_id, agent="orchestrator",
                 payload={"content": result["text"], "truncated": result.get("truncated")})
        if result.get("truncated"):
            bus.emit("warning", session_id, agent="orchestrator", payload={
                "message": ("The orchestrator's reply hit its output limit. Raise "
                            "max_tokens for the orchestrator model in settings."),
            })

        if result["proposal"]:
            proposal = result["proposal"]
            session.goal = proposal.get("summary") or session.goal
            # Added to rather than replaced, for the same reason steps are: a
            # proposal for a new feature should not drop what the rest of the
            # project still has to do.
            for wanted in proposal.get("requirements") or []:
                if wanted not in session.requirements:
                    session.requirements.append(wanted)
            session.team = stores_of(session).roles.resolve_team(proposal["team"])
            # Added to, not replaced. A new plan proposed halfway through used
            # to delete every finished step and its history with it — the
            # orchestrator is proposing what to do next, not editing what
            # already happened.
            was = {step.id for step in session.flow.steps}
            change = session.flow.keep_finished(
                [Step.from_dict(s) for s in proposal["steps"]])
            # The third door into the plan, and the one Generate walks through.
            # The read and the edit already seed; without this the proposed
            # steps go out on the socket carrying only the floor check, and the
            # plan screen shows that until a full reload — which reads as the
            # agent's checks being ignored.
            seed_checks(session.flow, checks_for(session))
            # And every role the flow names joins the team here too — the
            # framed plan can open with an agent the orchestrator's own team
            # list never mentioned.
            pull_flow_roles(session)
            # Pin the reply to the code as it stands. Everything committed from
            # here until the next proposal is what this answer turned into, so
            # "show me what came of this" is a range rather than a guess.
            reply = session.chat[-1]
            reply.steps = [step.id for step in session.flow.steps
                           if step.id not in was]
            # The screenshots that came with the request reach the agents who
            # will act on it. The orchestrator saw them; the frontend dev
            # fixing what the picture shows was working from a one-sentence
            # paraphrase of it.
            asked_with = next(
                (list(m.images) for m in reversed(session.chat[:-1])
                 if m.role == "user"), [])
            if asked_with:
                for step in session.flow.steps:
                    if step.id in reply.steps:
                        step.images = asked_with[:3]
            root = Path(session.project_dir).expanduser()
            reply.base = vcs.head(root) if vcs.is_repo(root) else ""
            # Not while it is running. The orchestrator can propose more work
            # in the middle of a run, and saying "ready" then told the page
            # nothing was running: it offered Start, and Start answered 409
            # already running. Adding steps to a live flow does not stop it.
            if not engine_alive(session):
                session.status = "ready"
            if change["kept"]:
                bus.emit("flow_extended", session_id, agent="orchestrator", payload={
                    **change,
                    "message": (f"Added {change['added']} step(s); kept the "
                                f"{change['kept']} that already ran"
                                + (f", and skipped {change['dropped']} that matched "
                                   f"work already done." if change["dropped"] else ".")),
                })
            bus.emit("flow_proposed", session_id, payload={
                "summary": proposal["summary"], "flow": session.flow.to_dict(),
                "team": [r.to_dict() for r in session.team],
                "dropped_checks": proposal.get("dropped_checks") or [],
            })
            if proposal.get("added_final_check"):
                bus.emit("warning", session_id, agent="orchestrator", payload={
                    "message": (f"The plan did not end by verifying itself, so a final "
                                f"{proposal['added_final_check']} step was added. Remove "
                                f"it if you really do not want one."),
                })
            if proposal.get("dropped_checks"):
                bus.emit("warning", session_id, agent="orchestrator", payload={
                    "message": ("Ignored what the plan said about checking: "
                                + ", ".join(proposal["dropped_checks"])
                                + ". Each agent's own checks are used instead."),
                })
            # Oversized steps are pointed out, not acted on. Splitting rewrites
            # the plan you are reading, takes a model call per step, and is a
            # judgement — five points may be exactly the step you wanted. The
            # split button on the step is where that judgement belongs.
            oversized = [step.id for step, raw
                         in zip(session.flow.steps, proposal["steps"])
                         if (raw.get("points") or 0) > config.max_step_points > 0]
            if oversized:
                bus.emit("oversized_steps", session_id, agent="orchestrator", payload={
                    "count": len(oversized), "threshold": config.max_step_points,
                    "step_ids": oversized,
                    "message": (f"{len(oversized)} step(s) came out over "
                                f"{config.max_step_points} points. Split any of them "
                                f"from the step itself if you want them broken up."),
                })
        touch(session)
        return session.to_dict()

    @app.put("/api/sessions/{session_id}/flow")
    def update_flow(session_id: str, body: dict):
        session = _need(store, session_id)
        held = stores_of(session)
        roles, loops = held.roles, held.loops
        steps = [Step.from_dict(s) for s in body.get("steps", [])]
        # Dangling names are warnings, not a wall. Rejecting the whole save
        # because one step names a deleted loop made an approved deletion
        # poison the plan: adding an unrelated step answered 400 until every
        # old reference was hunted down. The deal deletion made is honoured
        # here too — the save lands, and the step that still names the missing
        # thing fails at run time saying it was deleted.
        missing: list[str] = []
        for n, step in enumerate(steps, 1):
            if step.loop:
                if loops.get(step.loop) is None:
                    missing.append(f"step {n} runs the loop {step.loop!r}")
                continue
            if not roles.get(step.role):
                missing.append(f"step {n} is assigned to {step.role!r}")
            for name in step.checks:
                gate = roles.get(name)
                if gate is None:
                    missing.append(f"step {n} is checked by {name!r}")
                elif not gate.verifier:
                    missing.append(f"step {n} is checked by {name!r}, which cannot verify")
            if step.on_fail and roles.get(step.on_fail) is None:
                missing.append(f"step {n} is fixed by {step.on_fail!r}")
        if missing:
            bus.emit("warning", session_id, payload={
                "message": ("The plan names things that no longer exist: "
                            + "; ".join(missing[:6])
                            + ". Those steps will fail when run — reassign them, "
                            + "or recreate what they name."),
            })
        # Same rule whether or not a run is live: only in-flight steps are
        # immutable. Editing a finished or failed step re-queues it.
        outcome = session.flow.apply_edits(steps)
        # Pull in every agent the flow can reach — the steps' own roles, their
        # checks, their fixers, and everything inside loops. A session that can
        # name an agent it has never heard of fails at run time, after
        # everything before it has already run.
        pull_flow_roles(session)
        # A step added here is answered with its agent's checks already on it,
        # rather than showing an empty row until the next read seeds it.
        seed_checks(session.flow, checks_for(session))
        bus.emit("flow_updated", session_id,
                 payload={"flow": session.flow.to_dict(), **outcome})
        touch(session)
        if outcome["requeued"]:
            ensure_running(session)
        return {**session.flow.to_dict(), **outcome, "missing": missing,
                "team": [r.to_dict() for r in session.team]}

    @app.post("/api/sessions/{session_id}/steps/{step_id}/revert")
    def revert_step(session_id: str, step_id: str):
        """Undo everything a step committed, as one inverse commit.

        Failing costs nothing but the click: a conflict aborts cleanly, the
        tree stays as it was, and the button remains for another try — after
        whatever caused the conflict is dealt with.
        """
        session = _need(store, session_id)
        if session.status == "running":
            raise HTTPException(409, "the run is writing right now — stop it first")
        step = next((s for s in session.flow.steps if s.id == step_id), None)
        if step is None:
            raise HTTPException(404, "no such step")
        # Attempts already undone — a loop's revert_on_fail, or a block rewind —
        # are skipped: reverting an already-reverted commit would apply it back.
        shas = list(dict.fromkeys(
            a.commit for a in step.attempts if a.commit and not a.reverted))
        if not shas:
            raise HTTPException(400, "this step recorded no commits to revert")

        project = _project_of(session)
        label = (step.task or step_id).strip()[:60]
        made = vcs.revert_commits(project, shas, f"user: reverted step — {label}")
        if not made:
            bus.emit("warning", session_id, step_id=step_id, payload={
                "message": f"Revert failed and nothing was changed: {made.detail[:300]}"})
            raise HTTPException(409, (
                f"the revert did not apply cleanly — nothing was changed. "
                f"{made.detail[:400]}"))

        step.reverted_sha = made.sha
        bus.emit("step_reverted", session_id, agent="you", step_id=step_id, payload={
            "commits": shas, "sha": made.sha,
            "message": (f"Reverted this step's {len(shas)} commit(s) as {made.sha[:8]}. "
                        f"The step's work and the undo are both in history."),
        })
        touch(session)
        return {"reverted": shas, "sha": made.sha}

    @app.post("/api/sessions/{session_id}/steps/{step_id}/apply")
    def apply_step(session_id: str, step_id: str):
        """Take a revert back — for the one that was a mistake.

        Reverting the inverse commit puts the step's work back, and all three
        states stay in history: the work, the revert, the change of mind.
        """
        session = _need(store, session_id)
        if session.status == "running":
            raise HTTPException(409, "the run is writing right now — stop it first")
        step = next((s for s in session.flow.steps if s.id == step_id), None)
        if step is None:
            raise HTTPException(404, "no such step")
        if not step.reverted_sha:
            raise HTTPException(400, "this step has no revert to apply back")

        project = _project_of(session)
        label = (step.task or step_id).strip()[:60]
        made = vcs.revert_commits(project, [step.reverted_sha],
                                  f"user: re-applied step — {label}")
        if not made:
            bus.emit("warning", session_id, step_id=step_id, payload={
                "message": f"Re-apply failed and nothing was changed: {made.detail[:300]}"})
            raise HTTPException(409, (
                f"the re-apply did not go cleanly — nothing was changed. "
                f"{made.detail[:400]}"))

        step.reverted_sha = ""
        bus.emit("step_reverted", session_id, agent="you", step_id=step_id, payload={
            "sha": made.sha, "applied": True,
            "message": f"Re-applied this step's commits as {made.sha[:8]}.",
        })
        touch(session)
        return {"applied": True, "sha": made.sha}

    @app.post("/api/sessions/{session_id}/steps/{step_id}/blocks/{attempt_n}/rerun")
    def rerun_block(session_id: str, step_id: str, attempt_n: int):
        """Go back to one block of a loop and run it again from there.

        For when the tester described the fault precisely and the fixer
        fumbled it: change the fixer's model in Agents if you like, then rerun
        its block — it gets the very handoff it got the first time, on the
        very code the previous block judged. Commits from that block on are
        undone first as one inverse commit, and routing continues normally
        afterwards, so the fix still has to face its judge.
        """
        session = _need(store, session_id)
        if session.status == "running":
            raise HTTPException(409, "the run is writing right now — stop it first")
        step = session.flow.find(step_id)
        if step is None:
            raise HTTPException(404, "no such step")
        if not step.runs_a_loop:
            raise HTTPException(400, "only loop steps have blocks — use rerun for this step")
        attempt = next((a for a in step.attempts if a.n == attempt_n), None)
        if attempt is None or not attempt.node:
            raise HTTPException(404, (
                "this block was not recorded with its place in the loop — "
                "blocks run before this trance version cannot be rerun"))

        later = [a for a in step.attempts if a.n >= attempt_n]
        shas = list(dict.fromkeys(
            a.commit for a in later if a.commit and not a.reverted))
        if shas:
            project = _project_of(session)
            made = vcs.revert_commits(
                project, shas, f"user: rewound to block {attempt_n} to rerun it")
            if not made:
                raise HTTPException(409, (
                    f"could not rewind to that block — nothing was changed. "
                    f"{made.detail[:400]}"))
            for undone in later:
                if undone.commit:
                    undone.reverted = True
            bus.emit("git", session_id, agent="you", step_id=step_id, payload={
                "action": "revert", "ok": True, "sha": made.sha,
                "message": (f"Rewound {len(shas)} commit(s) as {made.sha[:8]} — the "
                            f"code is back to what this block started from."),
            })

        step.resume_node = attempt.node
        step.resume_handoff = attempt.handoff
        step.resume_shots = list(attempt.shots)
        step.status = "pending"
        was_paused = session.paused
        session.resume()
        bus.emit("flow_updated", session_id, payload={"flow": session.flow.to_dict()})
        touch(session)
        restarted = ensure_running(session)
        if restarted:
            bus.emit("run_started", session_id, payload={
                "reason": f"rerun of a {step.loop} block", "steps": 1,
                "message": f"Rerunning a {step.loop} block: {(step.task or '')[:60]}"})
        return {**step.to_dict(), "restarted": restarted, "rewound": shas,
                "resumed": was_paused, "status_now": session.status}

    @app.post("/api/sessions/{session_id}/resume-pending")
    def resume_pending(session_id: str):
        """Kick the engine for any pending work (after rerun, or a flow edit)."""
        session = _need(store, session_id)
        return {"restarted": ensure_running(session), "status": session.status}

    @app.put("/api/sessions/{session_id}/team")
    def update_team(session_id: str, body: dict):
        roles = stores_of(_need(store, session_id)).roles
        """Set which agent types are on this project's team.

        Accepts names or full objects; either way the definitions come from the
        library, so a session can never drift from the agent it names.
        """
        session = _need(store, session_id)
        incoming = body.get("team", [])
        session.team = roles.resolve_team(
            [r if isinstance(r, str) else r.get("name") for r in incoming]
        )
        bus.emit("team_updated", session_id, payload={"team": [r.to_dict() for r in session.team]})
        touch(session)
        return [r.to_dict() for r in session.team]

    # -------------------------------------------------------------- control

    @app.post("/api/sessions/{session_id}/start")
    def start(session_id: str):
        session = _need(store, session_id)
        if engine_alive(session):
            raise HTTPException(409, "already running")
        if not session.flow.steps:
            raise HTTPException(400, "flow is empty")
        if session.flow.next_pending() is None:
            raise HTTPException(409, "every step is finished — rerun one first")
        session.error = None
        # The project's own loops, not the workspace defaults — the same store
        # ensure_running hands over, so both start paths run the same wiring.
        FlowEngine(session, config, bus, on_change=lambda: touch(session),
                   approve=broker_for(session).ask,
                   loops=stores_of(session).loops).start()
        return session.to_dict()

    @app.get("/api/sessions/{session_id}/approvals")
    def list_approvals(session_id: str):
        """Outstanding asks, so a reloaded page does not lose the question."""
        session = _need(store, session_id)
        broker = brokers.get(session.id)
        return {"pending": [r.to_dict() for r in (broker.pending() if broker else [])],
                "enabled": config.ask_on_refusal, "timeout_s": config.approval_timeout_s}

    @app.post("/api/sessions/{session_id}/approvals/{request_id}")
    def resolve_approval(session_id: str, request_id: str, body: dict):
        """once = do it now; always = do it and widen the policy; deny = refuse."""
        session = _need(store, session_id)
        decision = (body or {}).get("decision", "")
        if decision not in DECISIONS:
            raise HTTPException(400, f"decision must be one of {', '.join(DECISIONS)}")
        broker = brokers.get(session.id)
        request = broker.resolve(request_id, decision) if broker else None
        if request is None:
            raise HTTPException(404, "that request is no longer waiting — it may have "
                                     "timed out, in which case it was denied")
        return {**request.to_dict(), "widened": decision == ALWAYS}

    @app.post("/api/sessions/{session_id}/pause")
    def pause(session_id: str):
        session = _need(store, session_id)
        session.pause()
        session.status = "paused"
        bus.emit("paused", session_id, payload={})
        return {"paused": True}

    @app.post("/api/sessions/{session_id}/resume")
    def resume(session_id: str):
        """Resume a paused run, or restart one that was stopped.

        Clearing the pause flag is only enough while the engine thread is still
        alive. `stop` makes it exit, so resuming after a stop needs a new engine
        — otherwise nothing happens and the session sits there looking paused.
        """
        session = _need(store, session_id)
        session.resume()
        session.clear_stop()

        if engine_alive(session):
            session.status = "running"
            bus.emit("resumed", session_id, payload={"restarted": False})
            return {"paused": False, "running": True, "restarted": False}

        if ensure_running(session):
            session.status = "running"
            bus.emit("resumed", session_id, payload={"restarted": True})
            return {"paused": False, "running": True, "restarted": True}

        # Nothing pending: say so rather than reporting a run that isn't running.
        reason = ("every step is finished, failed or skipped — rerun one, or edit a "
                  "step to re-queue it")
        session.status = "finished" if session.status != "error" else session.status
        bus.emit("warning", session_id, payload={
            "message": f"Nothing to resume: {reason}."})
        return {"paused": False, "running": False, "restarted": False, "reason": reason}

    @app.post("/api/sessions/{session_id}/stop")
    def stop(session_id: str):
        session = _need(store, session_id)
        # Release anything blocked on a question first, or the engine cannot
        # reach its own stop check to notice the stop.
        if session.id in brokers:
            brokers[session.id].abandon()
        session.stop()
        # And break off the generation itself. The stop flag is only read
        # between rounds, so without this the button does nothing visible until
        # the model finishes — minutes, on a local one.
        aborted = abort_inflight(session.id)
        if aborted:
            bus.emit("run_stopped", session_id, payload={
                "reason": "stopped by user", "aborted_model_calls": aborted,
                "message": "Stopped, and the model call in flight was broken off."})
        # And everything the agents started. The engine sweeps too when its
        # loop notices the stop — but a foreground command mid-flight holds
        # that loop for up to its whole timeout, and Stop means now.
        from ..agents.tools import stop_everything

        for command in stop_everything():
            bus.emit("background_stopped", session_id, payload={
                "command": command,
                "message": f"Stopped a process an agent left running: {command}"})
        bus.emit("stopping", session_id, payload={
            "message": ("Stopping after the current agent turn. Resume will start a "
                        "fresh engine from the next pending step.")})
        return {"stopping": True}

    @app.post("/api/sessions/{session_id}/steer")
    def steer(session_id: str, body: dict):
        """Queue a steering note onto a step's next prompt."""
        session = _need(store, session_id)
        note = (body.get("note") or "").strip()
        step_id = body.get("step_id")
        if not note:
            raise HTTPException(400, "note is required")
        if step_id:
            targets = [session.flow.find(step_id)]
        else:
            # The step you are watching is the one you want to correct, and it
            # is running — which is exactly the case this used to refuse.
            running = [s for s in session.flow.steps
                       if s.status in ("running", "verifying")]
            targets = running or [s for s in session.flow.steps if s.status == "pending"][:1]
        targets = [t for t in targets if t is not None]
        if not targets:
            raise HTTPException(404, "nothing running or pending to steer")
        for step in targets:
            step.steering.append(note)
        live = any(t.status in ("running", "verifying") for t in targets)
        bus.emit("steering", session_id, step_id=step_id, payload={
            "note": note, "steps": [t.id for t in targets], "delivering": live,
        })
        touch(session)
        return {"steered": [t.id for t in targets], "delivering": live}

    @app.post("/api/sessions/{session_id}/steps/{step_id}/split")
    async def split_one_step(session_id: str, step_id: str, body: dict | None = None):
        """Break one step up on demand, whatever it was estimated at."""
        session = _need(store, session_id)
        step = session.flow.find(step_id)
        if step is None:
            raise HTTPException(404, "no such step")
        if step.status in Flow.LOCKED:
            raise HTTPException(409, "that step is running — pause it first")

        threshold = int((body or {}).get("threshold") or config.max_step_points or 3)
        target = dict(step.to_dict())
        # Splitting an unrated step still needs a number to argue against.
        target["points"] = step.points or threshold + 1
        try:
            result = await asyncio.to_thread(
                orchestrator_agent.split_oversized, {"steps": [target], "team": []},
                roles=stores_of(session).roles.all(), config=config.for_orchestrator(), bus=bus,
                session_id=session_id, threshold=threshold,
                project_dir=Path(session.project_dir),
            )
        except BackendError as exc:
            raise HTTPException(502, str(exc))

        pieces = [Step.from_dict(s) for s in result["steps"]]
        if len(pieces) < 2:
            return {"split": False,
                    "reason": "the orchestrator did not find a smaller shape for it"}

        index = session.flow.steps.index(step)
        session.flow.steps[index:index + 1] = pieces
        bus.emit("flow_updated", session_id, payload={"flow": session.flow.to_dict()})
        touch(session)
        return {"split": True, "into": [s.to_dict() for s in pieces],
                "flow": session.flow.to_dict()}

    @app.post("/api/sessions/{session_id}/steps/{step_id}/rerun")
    def rerun(session_id: str, step_id: str, body: dict | None = None):
        session = _need(store, session_id)
        step = session.flow.find(step_id)
        if step is None:
            raise HTTPException(404, "no such step")

        # "Rerun on the backup" is for when you have watched the usual model
        # fail and know it will again — working up to the backup would just
        # spend its tries first.
        on_backup = bool((body or {}).get("on_backup"))
        role = session.role(step.role) if step.role else None
        if on_backup and not step.loop and not (role and role.backup_preset):
            raise HTTPException(400, (
                f"{step.role} has no backup model — set one in Agents first."))
        step.start_on_backup = on_backup
        step.status = "pending"
        step.attempts = []          # a rerun is a fresh attempt, not attempt N+1
        # "Run this step" and "stay paused" cannot both be honoured, and only
        # one of them was asked for just now.
        was_paused = session.paused
        session.resume()
        bus.emit("flow_updated", session_id, payload={"flow": session.flow.to_dict()})
        touch(session)
        # Marking it pending is not enough: if the previous run already
        # finished, its engine thread is gone and nothing would pick this up.
        restarted = ensure_running(session)
        if restarted:
            bus.emit("run_started", session_id, payload={
                "reason": f"rerun of {step.role} step", "steps": 1,
                "message": f"Started {step.role}: {(step.task or '')[:60]}"})
        # A stopping engine is still inside a model call; a live one will reach
        # this step on its own. Either way the click did something, and saying
        # which is the difference between "queued" and "broken".
        pending_on = ("the current model call to finish" if session.stopping
                      else "the running step to finish" if engine_alive(session)
                      else "")
        return {**step.to_dict(), "restarted": restarted, "on_backup": on_backup,
                "resumed": was_paused, "waiting_for": pending_on,
                "status_now": session.status}

    @app.post("/api/sessions/{session_id}/steps/{step_id}/skip")
    def skip(session_id: str, step_id: str):
        session = _need(store, session_id)
        step = session.flow.find(step_id)
        if step is None:
            raise HTTPException(404, "no such step")
        step.status = "skipped"
        bus.emit("flow_updated", session_id, payload={"flow": session.flow.to_dict()})
        touch(session)
        return step.to_dict()

    # ---------------------------------------------------------- websocket

    @app.websocket("/ws/{session_id}")
    async def websocket(ws: WebSocket, session_id: str):
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue = bus.subscribe_async(loop)
        session = store.get(session_id)
        last_state = _state_of(session) if session else None
        try:
            if session:
                # The socket carries what happens from now on. History is not
                # pushed at all: it is thousands of events and tens of megabytes
                # of prompts, and every screen that wants some of it knows which
                # some — the console asks for a tail, a step asks for its own.
                # Pushing the lot made both wait for neither.
                await ws.send_text(json.dumps({"type": "snapshot",
                                               "payload": session.to_dict()}))
            while True:
                event = await queue.get()
                if event.session_id != session_id:
                    continue
                # Slimmed like the list is. A model_call carries its whole
                # prompt — 187KB is normal — and the console never shows it
                # until a line is opened, which fetches the event in full.
                # Sending it to every socket on every call is the same
                # megabytes the history panel was taught not to ask for.
                await ws.send_text(json.dumps(_slim(event.to_dict())))
                # And the session itself whenever it has actually changed.
                #
                # This was a list of event types, and a list is a thing that
                # goes out of date: a loop step announces itself with loop_node
                # and step_outcome, neither of which was on it, so a page could
                # watch a loop run through six blocks and still show the step
                # it opened on. Comparing what the page was last told against
                # what is true now cannot fall behind a new event type.
                current = store.get(session_id)
                if current:
                    now = _state_of(current)
                    if now != last_state:
                        last_state = now
                        await ws.send_text(json.dumps({"type": "snapshot",
                                                        "payload": current.to_dict()}))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            bus.unsubscribe_async(queue)

    @app.exception_handler(BackendError)
    async def backend_error(_request, exc: BackendError):
        return JSONResponse({"error": str(exc)}, status_code=502)

    return app


def _need(store: SessionStore, session_id: str):
    session = store.get(session_id)
    if session is None:
        raise HTTPException(404, "no such session")
    return session
