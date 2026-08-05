"""HTTP + WebSocket server behind the inspection UI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..agents import orchestrator as orchestrator_agent
from ..agents.roles import BUILTIN_ROLES, TOOLSETS, AgentRole
from ..agents.store import PROTECTED, RoleStore, validate as validate_agent
from ..config import Config
from ..engine import FlowEngine, check_project_dir
from ..events import EventBus
from ..flow import Flow, Step
from ..providers import KIND_DEFAULTS, ModelPreset, ProviderConfig, ProviderStore
from ..session import ChatMessage, SessionStore
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
    config.providers = {p.name: p for p in providers.all()}
    config.presets = {m.name: m for m in providers.all_presets()}
    bus = EventBus()
    app = FastAPI(title="trance")
    app.state.config = config
    app.state.store = store
    app.state.bus = bus

    def touch(session):
        store.save(session)

    def engine_alive(session) -> bool:
        """Is a flow engine actually executing this session right now?

        `status` alone is not enough: a crashed engine can leave status at
        "running" with no thread behind it, and a finished run leaves the thread
        gone while pending steps can still be added by rerun or a flow edit.
        """
        thread = getattr(session, "_thread", None)
        return bool(thread is not None and thread.is_alive())

    def ensure_running(session) -> bool:
        """Start an engine if none is live and there is pending work."""
        if engine_alive(session):
            return False
        if session.flow.next_pending() is None:
            return False
        session.error = None
        FlowEngine(session, config, bus, on_change=lambda: touch(session)).start()
        return True

    def refresh_team(session):
        """Re-bind a session's team to the current library definitions."""
        session.team = roles.resolve_team(session.team)
        return session

    # ------------------------------------------------------------- static

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

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
            "providers": [p.to_dict() for p in providers.all(enabled_only=True)],
            "presets": [m.to_dict() for m in providers.presets()],
            "kinds": KIND_DEFAULTS,
            "orchestrator": {"preset": config.orchestrator.preset,
                             "provider": orchestrator.provider, "model": orchestrator.model,
                             "base_url": orchestrator.base_url,
                             "context_window": orchestrator.context_window},
        }

    # ---------------------------------------------------------- providers

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
        body = {**body, "name": name.strip()}
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
        """A preset names a (provider, model) pair — the unit an agent picks."""
        name = name.strip()
        if not name or " " in name:
            raise HTTPException(400, "name must be non-empty and contain no spaces")
        provider = providers.get(body.get("provider"))
        if provider is None:
            raise HTTPException(400, f"unknown provider {body.get('provider')!r}")
        model = (body.get("model") or "").strip() or provider.model
        saved = providers.upsert_preset(ModelPreset.from_dict({**body, "name": name, "model": model}))
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

    @app.get("/api/providers")
    def list_providers():
        """Every provider, including disabled ones (the settings view)."""
        return {"providers": [p.to_dict() for p in providers.all()], "kinds": KIND_DEFAULTS}

    @app.put("/api/providers/{name}")
    def upsert_provider(name: str, body: dict):
        body = {**body, "name": name}
        if body.get("kind") not in KIND_DEFAULTS:
            raise HTTPException(400, f"kind must be one of {', '.join(KIND_DEFAULTS)}")
        if not name.strip() or " " in name:
            raise HTTPException(400, "shortname must be non-empty and contain no spaces")
        saved = providers.upsert(ProviderConfig.from_dict(body))
        _sync()
        bus.emit("providers_updated", "system", payload={"name": saved.name})
        return saved.to_dict()

    @app.delete("/api/providers/{name}")
    def delete_provider(name: str):
        by_preset = [m.name for m in providers.all_presets() if m.provider == name]
        if by_preset:
            raise HTTPException(
                409, f"provider {name!r} backs these models: {', '.join(by_preset[:6])}. "
                     "Delete or repoint them first.")
        in_use = [
            f"{s.name}:{r.name}" for s in store.all() for r in s.team if r.provider == name
        ]
        if in_use:
            raise HTTPException(409, f"provider {name!r} is attached to: {', '.join(in_use[:6])}")
        if not providers.delete(name):
            raise HTTPException(404, "no such provider")
        _sync()
        return {"deleted": name}

    @app.post("/api/providers/{name}/check")
    def check_provider(name: str):
        """Send a one-token probe so a key or URL error surfaces here."""
        from ..providers import client_for

        provider = providers.get(name)
        if provider is None:
            raise HTTPException(404, "no such provider")
        resolved = config.resolve(config.worker, provider=name)
        resolved.max_tokens = 16
        try:
            reply = client_for(resolved).complete([{"role": "user", "content": "Reply with OK."}])
        except BackendError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # SDK-specific failures shouldn't 500 the UI
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "model": resolved.model, "reply": reply.text.strip()[:80]}

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
        return [e.to_dict() for e in bus.history(session_id)]

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
            )
        except BackendError as exc:
            bus.emit("error", session_id, payload={"message": str(exc)})
            raise HTTPException(502, str(exc))

        session.chat.append(ChatMessage(role="orchestrator", content=result["text"]))
        bus.emit("chat", session_id, agent="orchestrator", payload={"content": result["text"]})

        if result["proposal"]:
            proposal = result["proposal"]
            session.team = roles.resolve_team(proposal["team"])
            session.flow = Flow(steps=[Step.from_dict(s) for s in proposal["steps"]])
            session.status = "ready"
            bus.emit("flow_proposed", session_id, payload={
                "summary": proposal["summary"], "flow": session.flow.to_dict(),
                "team": [r.to_dict() for r in session.team],
            })
        touch(session)
        return session.to_dict()

    # --------------------------------------------------------------- flow

    @app.put("/api/sessions/{session_id}/flow")
    def update_flow(session_id: str, body: dict):
        session = _need(store, session_id)
        steps = [Step.from_dict(s) for s in body.get("steps", [])]
        for step in steps:
            for gate in step.checks:
                role = roles.get(gate)
                if role is None:
                    raise HTTPException(400, f"unknown check {gate!r}")
                if not role.verifier:
                    allowed = [r.name for r in roles.all() if r.verifier]
                    raise HTTPException(400, (
                        f"{gate!r} cannot verify — it has no way to inspect a result. "
                        f"Choose one of: {', '.join(allowed) or '(none configured)'}."))
            if len(step.checks) != len(set(step.checks)):
                raise HTTPException(400, "a check may only appear once in a step's chain")
        # Same rule whether or not a run is live: only in-flight steps are
        # immutable. Editing a finished or failed step re-queues it.
        outcome = session.flow.apply_edits(steps)
        bus.emit("flow_updated", session_id,
                 payload={"flow": session.flow.to_dict(), **outcome})
        touch(session)
        if outcome["requeued"]:
            ensure_running(session)
        return {**session.flow.to_dict(), **outcome}

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
        FlowEngine(session, config, bus, on_change=lambda: touch(session)).start()
        return session.to_dict()

    @app.post("/api/sessions/{session_id}/pause")
    def pause(session_id: str):
        session = _need(store, session_id)
        session.pause()
        session.status = "paused"
        bus.emit("paused", session_id, payload={})
        return {"paused": True}

    @app.post("/api/sessions/{session_id}/resume")
    def resume(session_id: str):
        session = _need(store, session_id)
        session.resume()
        session.status = "running"
        bus.emit("resumed", session_id, payload={})
        return {"paused": False}

    @app.post("/api/sessions/{session_id}/stop")
    def stop(session_id: str):
        session = _need(store, session_id)
        session.stop()
        bus.emit("stopping", session_id, payload={})
        return {"stopping": True}

    @app.post("/api/sessions/{session_id}/steer")
    def steer(session_id: str, body: dict):
        """Queue a steering note onto a step's next prompt."""
        session = _need(store, session_id)
        note = (body.get("note") or "").strip()
        step_id = body.get("step_id")
        if not note:
            raise HTTPException(400, "note is required")
        targets = [session.flow.find(step_id)] if step_id else [
            s for s in session.flow.steps if s.status == "pending"
        ]
        targets = [t for t in targets if t is not None]
        if not targets:
            raise HTTPException(404, "no pending step to steer")
        for step in targets:
            step.steering.append(note)
        bus.emit("steering", session_id, step_id=step_id,
                 payload={"note": note, "steps": [t.id for t in targets]})
        touch(session)
        return {"steered": [t.id for t in targets]}

    @app.post("/api/sessions/{session_id}/steps/{step_id}/rerun")
    def rerun(session_id: str, step_id: str):
        session = _need(store, session_id)
        step = session.flow.find(step_id)
        if step is None:
            raise HTTPException(404, "no such step")
        step.status = "pending"
        step.attempts = []          # a rerun is a fresh attempt, not attempt N+1
        bus.emit("flow_updated", session_id, payload={"flow": session.flow.to_dict()})
        touch(session)
        # Marking it pending is not enough: if the previous run already
        # finished, its engine thread is gone and nothing would pick this up.
        restarted = ensure_running(session)
        if restarted:
            bus.emit("run_started", session_id,
                     payload={"reason": f"rerun of {step.role} step", "steps": 1})
        return {**step.to_dict(), "restarted": restarted, "status_now": session.status}

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
                await ws.send_text(json.dumps({"type": "snapshot", "payload": session.to_dict()}))
                for event in bus.history(session_id):
                    await ws.send_text(json.dumps(event.to_dict()))
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
