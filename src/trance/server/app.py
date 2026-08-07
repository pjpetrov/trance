"""HTTP + WebSocket server behind the inspection UI."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from uuid import uuid4
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..agents import orchestrator as orchestrator_agent
from ..agents.orchestrator import POINTS
from ..agents.approval import ALWAYS, ApprovalBroker, DECISIONS
from ..agents.memory import COMPACT_PROMPT, MAX_NOTES, ProjectMemory
from ..agents.roles import BUILTIN_ROLES, TOOLSETS, AgentRole
from ..trace.session_log import SessionLog
from ..agents.store import (
    CommandStore, DEFAULT_LIST, LoopStore, PROTECTED, RoleStore,
    validate as validate_agent,
)
from ..agents.tools import ALLOWED_COMMANDS, set_command_lists, set_command_policy
from ..config import Config
from ..engine import FlowEngine, check_project_dir
from ..events import EventBus
from ..flow import Flow, Step
from ..loops import EXITS, STOP, Loop, validate as validate_loop
from ..providers.base import list_models
from ..providers import (
    KIND_DEFAULTS, ModelPreset, ProviderConfig, ProviderStore, abort_inflight,
    client_for,
)
from ..session import ChatMessage, SessionStore
from .. import preview, vcs
from ..worker.client import BackendError

STATIC = Path(__file__).parent / "static"


def create_app(config: Config | None = None, sessions_dir: Path | None = None) -> FastAPI:
    config = config or Config.load()
    store = SessionStore(sessions_dir or Path(config.runs_dir) / "sessions")
    # trance.toml seeds the registry once; after that the JSON store is the
    # source of truth so provider edits made in the UI survive a restart.
    providers = ProviderStore(Path(config.runs_dir) / "providers.json", seed=config.providers)
    providers.seed_presets_from_providers()  # never show an empty model picker
    roles = RoleStore(Path(config.runs_dir) / "agents.json")
    commands = CommandStore(Path(config.runs_dir) / "commands.json")
    loops = LoopStore(Path(config.runs_dir) / "loops.json")
    set_command_policy(commands.policy)
    set_command_lists(commands.lists)
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
                existing = SessionLog(store.root / session_id)
                logs[session_id] = existing
            return existing

    def persist(event) -> None:
        if event.session_id and event.session_id != "system":
            log_for(event.session_id).append(event)

    bus.subscribe_sync(persist)

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
        """Make "always" mean it — the same ask must not come back next step."""
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
        _publish_commands()
        bus.emit("commands_updated", session.id, payload={"name": target, **policy.to_dict()})

    def engine_alive(session) -> bool:
        """Is a flow engine actually executing this session right now?

        `status` alone is not enough: a crashed engine can leave status at
        "running" with no thread behind it, and a finished run leaves the thread
        gone while pending steps can still be added by rerun or a flow edit.
        """
        thread = getattr(session, "_thread", None)
        return bool(thread is not None and thread.is_alive())

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
        session.error = None
        session.clear_stop()
        FlowEngine(session, config, bus, on_change=lambda: touch(session),
                   approve=broker_for(session).ask, loops=loops).start()
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
                FlowEngine(session, config, bus, on_change=lambda: touch(session),
                           approve=broker_for(session).ask, loops=loops).start()

        session._handover = threading.Thread(
            target=wait_and_start, name=f"handover-{session.id}", daemon=True)
        session._handover.start()

    def refresh_team(session):
        """Re-bind a session's team to the current library definitions."""
        session.team = roles.resolve_team(session.team)
        return session

    # ------------------------------------------------------------- static

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    class FreshStatic(StaticFiles):
        """Always revalidate. The UI changes under the user constantly, and a
        cached app.js showing behaviour that was removed is indistinguishable
        from a bug in the software."""

        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    app.mount("/static", FreshStatic(directory=STATIC), name="static")

    # ------------------------------------------------------------- config

    def _sync():
        """Re-publish the registry into the live config."""
        config.providers = {p.name: p for p in providers.all()}
        config.presets = {m.name: m for m in providers.all_presets()}

    @app.get("/api/config")
    def get_config():
        orchestrator = config.for_orchestrator()
        return {
            "config": config.to_dict(),
            "roles": {r.name: r.to_dict() for r in roles.all()},
            "presets": [m.to_dict() for m in providers.presets()],
            "kinds": KIND_DEFAULTS,
            "planning": {"max_step_points": config.max_step_points, "scale": list(POINTS),
                         "escalation_preset": config.escalation_preset,
                         "escalation_role": config.escalation_role,
                         "git_commits": config.git_commits,
                         "git_auto_init": config.git_auto_init},
            "orchestrator": {"preset": config.orchestrator.preset,
                             "provider": orchestrator.provider, "model": orchestrator.model,
                             "base_url": orchestrator.base_url,
                             "context_window": orchestrator.context_window},
        }

    # ---------------------------------------------------------- providers

    # ----------------------------------------------------------- commands

    def _publish_commands():
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
    def get_commands():
        """Every named allowlist, plus which agents use which."""
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
    def set_commands(body: dict):
        """Edit one list. Without a name, the default one."""
        name = (body.get("name") or DEFAULT_LIST).strip()
        if " " in name:
            raise HTTPException(400, "a list name cannot contain spaces")
        allowed = body.get("allowed")
        _check_programs(allowed)
        if name not in commands.lists and not allowed:
            raise HTTPException(400, "a new list needs at least one program")
        policy = commands.upsert(name, allowed=allowed, shell=body.get("shell"))
        _publish_commands()
        bus.emit("commands_updated", "system", payload={"name": name, **policy.to_dict()})
        return {"name": name, **policy.to_dict()}

    @app.delete("/api/commands/{name}")
    def delete_command_list(name: str):
        """Delete a list. Agents pointing at it fall back to the default."""
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
        _publish_commands()
        return {"deleted": name, "moved_to_default": moved}

    @app.post("/api/commands/cancel/{command_id}")
    def cancel_running_command(command_id: str):
        """Kill a command that is still running."""
        from ..agents.tools import cancel_command, running_commands

        killed = cancel_command(command_id)
        return {"cancelled": killed, "still_running": running_commands()}

    @app.post("/api/commands/allow")
    def allow_programs(body: dict):
        """Add programs to an allowlist — the global one, or an agent's own."""
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
        _publish_commands()
        bus.emit("commands_updated", "system", payload={"name": target, **policy.to_dict()})
        return {"scope": "list", "list": target, "allowed": policy.allowed}

    @app.post("/api/commands/reset")
    def reset_commands(body: dict | None = None):
        policy = commands.reset((body or {}).get("name") or DEFAULT_LIST)
        _publish_commands()
        return policy.to_dict()

    # ------------------------------------------------------------- agents

    @app.get("/api/agents")
    def list_agents():
        return {
            "verifiers": [r.name for r in roles.all() if r.verifier],
            "agents": [
                {**r.to_dict(), "protected": r.name in PROTECTED,
                 "resolved": _resolved_for(r)}
                for r in roles.all()
            ],
            "toolsets": list(TOOLSETS),
        }

    def _resolved_for(role):
        m = config.for_role(role)
        return {"model": m.model, "provider": m.provider, "context_window": m.context_window}

    @app.put("/api/agents/{name}")
    def upsert_agent(name: str, body: dict):
        # Merge onto what is stored: a partial update ("just change the command
        # list") must not blank the prompt and the remit by omission.
        existing = roles.get(name.strip())
        base = existing.to_dict() if existing else {}
        body = {**base, **body, "name": name.strip()}
        error = validate_agent(body)
        if error:
            raise HTTPException(400, error)
        if body.get("preset") and body["preset"] not in config.presets:
            raise HTTPException(400, f"unknown model {body['preset']!r}")
        saved = roles.upsert(AgentRole.from_dict(body))
        for session in store.all():          # live sessions pick up the edit
            refresh_team(session)
            touch(session)
        bus.emit("agents_updated", "system", payload={"name": saved.name})
        return {**saved.to_dict(), "protected": saved.name in PROTECTED,
                "resolved": _resolved_for(saved)}

    @app.post("/api/agents/{name}/reset")
    def reset_agent(name: str):
        """Restore a built-in agent type to its shipped prompt and permissions."""
        role = roles.reset(name)
        if role is None:
            raise HTTPException(404, f"{name!r} is not a built-in agent type")
        for session in store.all():
            refresh_team(session)
            touch(session)
        return {**role.to_dict(), "protected": True, "resolved": _resolved_for(role)}

    @app.delete("/api/agents/{name}")
    def delete_agent(name: str):
        if name in PROTECTED:
            raise HTTPException(409, f"{name!r} is a built-in agent type and cannot be deleted")
        used_by = [s.name for s in store.all() if any(r.name == name for r in s.team)]
        if used_by:
            raise HTTPException(409, f"{name!r} is on the team of: {', '.join(used_by[:6])}")
        verifying = [
            f"{s.name}:{st.role}" for s in store.all() for st in s.flow.steps
            if st.verify_with == name
        ]
        if verifying:
            raise HTTPException(409, f"{name!r} verifies: {', '.join(verifying[:6])}")
        if not roles.delete(name):
            raise HTTPException(404, "no such agent")
        return {"deleted": name}

    @app.get("/api/presets")
    def list_presets():
        return {"presets": [m.to_dict() for m in providers.all_presets()]}

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
        if not model:
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
    def delete_preset(name: str):
        in_use = [
            f"{s.name}:{r.name}" for s in store.all() for r in s.team if r.preset == name
        ]
        if in_use:
            raise HTTPException(409, f"model {name!r} is assigned to: {', '.join(in_use[:6])}")
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

    @app.put("/api/config/planning")
    def set_planning(body: dict):
        """The size a step may reach before the orchestrator breaks it up."""
        if "max_step_points" in body:
            try:
                value = int(body["max_step_points"] or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "max_step_points must be a number")
            if value < 0 or value > 13:
                raise HTTPException(400, "max_step_points must be between 0 and 13 "
                                         "(0 turns splitting off)")
            config.max_step_points = value
        if "escalation_preset" in body:
            name = (body.get("escalation_preset") or "").strip()
            if name and providers.preset(name) is None:
                raise HTTPException(400, f"unknown model {name!r}")
            config.escalation_preset = name
        if "escalation_role" in body:
            name = (body.get("escalation_role") or "").strip()
            if name and roles.get(name) is None:
                raise HTTPException(400, f"unknown agent {name!r}")
            config.escalation_role = name
        for flag in ("git_commits", "git_auto_init"):
            if flag in body:
                setattr(config, flag, bool(body[flag]))
        return {"max_step_points": config.max_step_points, "scale": list(POINTS),
                "escalation_preset": config.escalation_preset,
                "escalation_role": config.escalation_role,
                "git_commits": config.git_commits,
                "git_auto_init": config.git_auto_init}

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
        target = (root / (relative or "")).resolve()
        if target != root and root not in target.parents:
            return None
        return target

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

    #: One preview server per session, kept until it is stopped or the session
    #: goes. Restarting it for every click would give the browser a new origin
    #: each time and throw away whatever the page had in local storage.
    previews: dict[str, object] = {}
    app.state.previews = previews

    @app.post("/api/sessions/{session_id}/preview")
    def start_preview(session_id: str, body: dict | None = None):
        """Serve the folder a page lives in, and hand back its URL."""
        session = _need(store, session_id)
        root = _project_of(session)
        target = _inside(root, (body or {}).get("path") or "")
        if target is None or not target.exists():
            raise HTTPException(404, "no such file")

        web_root = preview.web_root_for(root, (body or {}).get("path") or "")
        page = target.name if target.is_file() else ""

        # A Vite or webpack app imports bare module names that only its own dev
        # server can resolve. Serving the folder statically would give a page
        # that loads and then fails, which looks like the code is broken.
        dev = preview.dev_command(root, web_root)
        if dev and dev["needed"]:
            existing = previews.get(session_id)
            if existing is not None and getattr(existing, "command", None) == dev["command"] \
                    and existing.running:
                started = existing
            else:
                if existing is not None:
                    existing.stop()
                started = preview.start_dev(Path(dev["dir"]), dev["command"])
                previews[session_id] = started
            if not started.url:
                raise HTTPException(502, (
                    f"`{dev['command']}` did not report a URL"
                    + (". It exited — is `npm install` done?" if not started.running
                       else " within 20s. Its output is in the console.")))
            bus.emit("preview", session_id, agent="you", payload={
                "url": started.url, "command": dev["command"], "root": started.root,
                "message": f"Running `{dev['command']}` — {started.url}",
            })
            return {**started.to_dict(), "open": started.url, "kind": "dev",
                    "port": 0, "command": dev["command"]}

        existing = previews.get(session_id)
        if existing is not None and getattr(existing, "root", None) == str(web_root) \
                and hasattr(existing, "port"):
            served = existing
        else:
            if existing is not None:
                existing.stop()
            served = preview.serve(web_root)
            previews[session_id] = served

        bus.emit("preview", session_id, agent="you", payload={
            "url": served.url + page, "root": served.root, "port": served.port,
            "message": f"Serving {Path(served.root).name}/ at {served.url}",
        })
        return {**served.to_dict(), "open": served.url + page, "kind": "static"}

    @app.delete("/api/sessions/{session_id}/preview")
    def stop_preview(session_id: str):
        _need(store, session_id)
        served = previews.pop(session_id, None)
        if served is not None:
            served.stop()
        return {"stopped": served is not None}

    @app.get("/api/sessions/{session_id}/preview")
    def preview_status(session_id: str):
        _need(store, session_id)
        served = previews.get(session_id)
        if served is None:
            return {"root": "", "port": 0, "url": ""}
        return {"port": 0, **served.to_dict()}

    # ------------------------------------------------------------- review

    @app.post("/api/sessions/{session_id}/review")
    def add_review_note(session_id: str, body: dict):
        """Leave a comment on one line, to be sent as work later."""
        session = _need(store, session_id)
        note = (body.get("note") or "").strip()
        path = (body.get("path") or "").strip()
        if not note or not path:
            raise HTTPException(400, "path and note are required")
        entry = {
            "id": f"rv_{uuid4().hex[:8]}", "path": path,
            "line": max(1, int(body.get("line") or 1)),
            "code": (body.get("code") or "")[:400],
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
        for note in session.review:
            by_file.setdefault(note["path"], []).append(note)
        rendered = []
        for path, notes in by_file.items():
            lines = [f"### {path}"]
            for note in sorted(notes, key=lambda n: n["line"]):
                lines.append(f"- line {note['line']}"
                             + (f" (`{note['code'].strip()}`)" if note.get("code") else "")
                             + f": {note['note']}")
            rendered.append("\n".join(lines))

        loop_name = (body or {}).get("loop") or ""
        if loop_name and loops.get(loop_name) is None:
            raise HTTPException(400, f"unknown loop {loop_name!r}")
        if not loop_name:
            loop_name = next((l.name for l in loops.all()), "")

        task = ("Address this code review. Each comment names a file and a line; make "
                "the change the comment asks for and nothing else.\n\n"
                + "\n\n".join(rendered)
                + "\n\nWhere a comment is a question rather than an instruction, answer "
                  "it in your report instead of changing code.")
        step = Step(role="" if loop_name else "backend", loop=loop_name, task=task,
                    points=3, max_loops=2)
        session.flow.steps.append(step)

        record = {
            "id": f"rev_{uuid4().hex[:8]}", "step_id": step.id,
            "notes": list(session.review),
            "before": vcs.head(_project_of(session)),
            "after": "", "files": [],
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
            "loop": loop_name,
            "message": f"Sent {len(record['notes'])} review comment(s) as a step.",
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
        }

    # -------------------------------------------------------------- loops

    def _loop_context():
        known = {r.name for r in roles.all()}
        return known, {r.name for r in roles.all() if r.verifier}

    @app.get("/api/loops")
    def list_loops():
        return {"loops": [l.to_dict() for l in loops.all()],
                "outcomes": list(EXITS), "stops": list(STOP),
                "agents": [r.name for r in roles.all() if r.name != "orchestrator"],
                "verifiers": [r.name for r in roles.all() if r.verifier]}

    @app.put("/api/loops/{name}")
    def upsert_loop(name: str, body: dict):
        loop = Loop.from_dict({**body, "name": name.strip()})
        known, verifiers = _loop_context()
        error = validate_loop(loop, known, verifiers)
        if error:
            raise HTTPException(400, error)
        saved = loops.upsert(loop)
        bus.emit("loops_updated", "system", payload={"name": saved.name})
        return saved.to_dict()

    @app.delete("/api/loops/{name}")
    def delete_loop(name: str):
        used = [s.name for s in store.all()
                if any(step.loop == name for step in s.flow.steps)]
        if used:
            raise HTTPException(409, (
                f"{name!r} is used by: {', '.join(used)}. Change those steps first."))
        if not loops.delete(name):
            raise HTTPException(404, "no such loop")
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
            raise HTTPException(400, "project_dir is required")
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

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str):
        served = previews.pop(session_id, None)
        if served is not None:
            served.stop()          # nothing should outlive the session it serves
        """Delete a session and its trace. Files the agents wrote are left alone."""
        session = _need(store, session_id)
        if session.status == "running":
            session.stop()  # let the engine unwind before the directory goes
        if not store.delete(session_id):
            raise HTTPException(404, "no such session")
        return {"deleted": session_id, "project_dir": session.project_dir}

    @app.get("/api/sessions/{session_id}/events")
    def get_events(session_id: str):
        _need(store, session_id)
        return [e.to_dict() for e in history_for(session_id)]

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
        if not text:
            raise HTTPException(400, "message is required")

        session.chat.append(ChatMessage(role="user", content=text))
        bus.emit("chat", session_id, agent="user", payload={"content": text})

        history = [
            {"role": "assistant" if m.role == "orchestrator" else "user", "content": m.content}
            for m in session.chat
        ]
        try:
            result = await asyncio.to_thread(
                orchestrator_agent.chat,
                messages=history,
                project_dir=Path(session.project_dir),
                config=config.for_orchestrator(),
                bus=bus,
                session_id=session_id,
                roles=roles.all(),
                loops=loops,
            )
        except BackendError as exc:
            bus.emit("error", session_id, payload={"message": str(exc)})
            raise HTTPException(502, str(exc))

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
            session.team = roles.resolve_team(proposal["team"])
            session.flow = Flow(steps=[Step.from_dict(s) for s in proposal["steps"]])
            session.status = "ready"
            bus.emit("flow_proposed", session_id, payload={
                "summary": proposal["summary"], "flow": session.flow.to_dict(),
                "team": [r.to_dict() for r in session.team],
                "dropped_checks": proposal.get("dropped_checks") or [],
            })
            if proposal.get("added_checks"):
                bus.emit("warning", session_id, agent="orchestrator", payload={
                    "message": (f"Added a factchecker to {proposal['added_checks']} step(s) "
                                f"that write files — it only confirms the files exist. "
                                f"Clear it on a step if you do not want one."),
                })
            if proposal.get("added_final_check"):
                bus.emit("warning", session_id, agent="orchestrator", payload={
                    "message": (f"The plan did not end by verifying itself, so a final "
                                f"{proposal['added_final_check']} step was added. Remove "
                                f"it if you really do not want one."),
                })
            if proposal.get("dropped_checks"):
                bus.emit("warning", session_id, agent="orchestrator", payload={
                    "message": ("Dropped checks that cannot verify: "
                                + ", ".join(proposal["dropped_checks"])),
                })
            # Splitting is one model call per oversized step and can run for a
            # minute on a local model. Showing the plan first and refining it
            # after beats an empty flow panel and no explanation.
            # The flow is already applied, so the proposal's steps and the
            # session's are the same list — which is how the UI can mark the
            # exact steps being worked on rather than the whole plan.
            oversized = [(step.id, raw) for step, raw
                         in zip(session.flow.steps, proposal["steps"])
                         if (raw.get("points") or 0) > config.max_step_points > 0]
            if oversized:
                bus.emit("splitting_steps", session_id, agent="orchestrator", payload={
                    "count": len(oversized), "threshold": config.max_step_points,
                    "step_ids": [step_id for step_id, _ in oversized],
                    "tasks": [raw["task"] for _, raw in oversized],
                    "message": (f"{len(oversized)} step(s) are over "
                                f"{config.max_step_points} points — breaking them up."),
                })
                _spawn(_split_in_background(session, proposal))
        touch(session)
        return session.to_dict()

    async def _split_in_background(session, proposal: dict) -> None:
        """Refine an already-visible plan. Never leaves the user with nothing."""
        try:
            refined = await asyncio.to_thread(
                orchestrator_agent.split_oversized, dict(proposal),
                roles=roles.all(), config=config.for_orchestrator(), bus=bus,
                session_id=session.id, threshold=config.max_step_points,
                project_dir=Path(session.project_dir),
            )
        except Exception as exc:  # noqa: BLE001 — the plan stands either way
            bus.emit("warning", session.id, agent="orchestrator", payload={
                "message": f"Could not split the oversized steps: {exc}"})
            return
        if not refined.get("split"):
            bus.emit("flow_updated", session.id, payload={
                "flow": session.flow.to_dict(), "split": []})
            return
        session.team = roles.resolve_team(refined["team"] or [s.name for s in session.team])
        session.flow = Flow(steps=[Step.from_dict(s) for s in refined["steps"]])
        touch(session)
        bus.emit("flow_updated", session.id, payload={
            "flow": session.flow.to_dict(), "split": refined["split"],
            "message": f"Split into {len(refined['steps'])} steps."})

    # --------------------------------------------------------------- flow

    @app.put("/api/sessions/{session_id}/flow")
    def update_flow(session_id: str, body: dict):
        session = _need(store, session_id)
        steps = [Step.from_dict(s) for s in body.get("steps", [])]
        for step in steps:
            if step.loop:
                # A step naming a loop that does not exist fails at run time,
                # after everything before it has already run.
                if loops.get(step.loop) is None:
                    raise HTTPException(400, f"unknown loop {step.loop!r}")
                continue
            if not roles.get(step.role):
                raise HTTPException(400, f"unknown agent {step.role!r}")
            if step.checker:
                role = roles.get(step.checker)
                if role is None:
                    raise HTTPException(400, f"unknown check {step.checker!r}")
                if not role.verifier:
                    allowed = [r.name for r in roles.all() if r.verifier]
                    raise HTTPException(400, (
                        f"{step.checker!r} cannot verify — it has no way to inspect a "
                        f"result. Choose one of: {', '.join(allowed) or '(none configured)'}."))
            if step.on_fail and roles.get(step.on_fail) is None:
                raise HTTPException(400, f"unknown fixing agent {step.on_fail!r}")
        # Same rule whether or not a run is live: only in-flight steps are
        # immutable. Editing a finished or failed step re-queues it.
        # Pull in every agent the flow can reach, loops included — otherwise a
        # loop calls an agent this session has never heard of.
        wanted = list(session.team)
        for step in steps:
            loop = loops.get(step.loop) if step.loop else None
            for name in (loop.roles() if loop else []):
                if all(r.name != name for r in wanted) and roles.get(name):
                    wanted.append(roles.get(name))
        session.team = roles.resolve_team(wanted)

        outcome = session.flow.apply_edits(steps)
        bus.emit("flow_updated", session_id,
                 payload={"flow": session.flow.to_dict(), **outcome})
        touch(session)
        if outcome["requeued"]:
            ensure_running(session)
        return {**session.flow.to_dict(), **outcome,
                "team": [r.to_dict() for r in session.team]}

    @app.post("/api/sessions/{session_id}/resume-pending")
    def resume_pending(session_id: str):
        """Kick the engine for any pending work (after rerun, or a flow edit)."""
        session = _need(store, session_id)
        return {"restarted": ensure_running(session), "status": session.status}

    @app.put("/api/sessions/{session_id}/team")
    def update_team(session_id: str, body: dict):
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
        FlowEngine(session, config, bus, on_change=lambda: touch(session),
                   approve=broker_for(session).ask, loops=loops).start()
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
                roles=roles.all(), config=config.for_orchestrator(), bus=bus,
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
            bus.emit("run_started", session_id,
                     payload={"reason": f"rerun of {step.role} step", "steps": 1})
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
        try:
            if session:
                # History first, then the snapshot. These events are replayed to
                # rebuild the console, and some of them carry state — an old
                # flow_updated holds the flow as it was when it fired. Sending
                # the snapshot first let that stale copy win, which is how a
                # refreshed page showed finished steps as pending.
                for event in history_for(session_id):
                    await ws.send_text(json.dumps({**event.to_dict(), "replay": True}))
                await ws.send_text(json.dumps({"type": "snapshot",
                                               "payload": session.to_dict()}))
            while True:
                event = await queue.get()
                if event.session_id != session_id:
                    continue
                await ws.send_text(json.dumps(event.to_dict()))
                if event.type in ("step_finished", "step_failed", "run_finished", "flow_updated",
                                  "step_started", "verdict", "run_stopped"):
                    current = store.get(session_id)
                    if current:
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
