"""Per-project configuration.

The point of the change: copying a project's folder copies the way it is built.
So the tests are mostly about where things land, what is carried in, and what is
deliberately left behind — the API keys.
"""

from __future__ import annotations

import json
from pathlib import Path

from trance.workspace import ProjectStores, Settings, SettingsStore, Workspace, seed


def _global(tmp_path):
    """A workspace-wide runs/ directory, as it looks before any of this."""
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "agents.json").write_text(json.dumps({"agents": [{
        "name": "powerdev", "title": "Power dev", "description": "mine",
        "system_prompt": "do it well", "paths": ["**"], "toolsets": ["files"],
    }]}), encoding="utf8")
    (runs / "loops.json").write_text(json.dumps({"loops": [{
        "name": "my-loop", "description": "mine", "prompt": "", "start": "n1",
        "max_steps": 4, "nodes": [{"id": "n1", "role": "tester", "focus": "",
                                   "check": None, "on": {}}],
    }]}), encoding="utf8")
    (runs / "commands.json").write_text(json.dumps({
        "lists": {"default": {"allowed": ["pytest", "npm"], "shell": True}}}),
        encoding="utf8")
    return runs


def test_a_project_keeps_its_configuration_in_its_own_folder(tmp_path):
    project = tmp_path / "game"
    stores = ProjectStores(project, defaults=_global(tmp_path))

    for name in ("agents.json", "loops.json", "commands.json"):
        assert (project / ".trance" / name).is_file(), name
    # Beside the graph index, the plan and the screenshots — everything trance
    # knows about the project, in one directory that can be copied.
    assert stores.dir == project / ".trance"


def test_a_new_project_starts_from_the_setup_you_already_have(tmp_path):
    """Not from the shipped defaults: powerdev and the custom loops are what
    someone actually works with, and re-creating them per project is work."""
    stores = ProjectStores(tmp_path / "game", defaults=_global(tmp_path))

    assert stores.roles.get("powerdev") is not None
    assert stores.roles.get("powerdev").system_prompt == "do it well"
    assert stores.loops.get("my-loop") is not None
    # The store sorts them; what matters is that the project got the list.
    assert set(stores.commands.lists["default"].allowed) == {"pytest", "npm"}
    assert stores.migrated is True

    # And the builtins are still there, because the store restores what is
    # missing rather than trusting the file to be complete.
    assert stores.roles.get("developer") is not None


def test_a_project_that_has_diverged_is_never_overwritten(tmp_path):
    defaults = _global(tmp_path)
    project = tmp_path / "game"

    first = ProjectStores(project, defaults=defaults)
    tuned = first.roles.get("powerdev")
    tuned.system_prompt = "do it MY way"
    first.roles.upsert(tuned)

    # The workspace-wide file changes afterwards; the project must not notice.
    (defaults / "agents.json").write_text(json.dumps({"agents": [{
        "name": "powerdev", "title": "P", "description": "", "system_prompt": "changed",
        "paths": [], "toolsets": [],
    }]}), encoding="utf8")

    again = ProjectStores(project, defaults=defaults)
    assert again.roles.get("powerdev").system_prompt == "do it MY way"
    assert again.migrated is False


def test_a_project_with_no_workspace_files_still_gets_the_builtins(tmp_path):
    stores = ProjectStores(tmp_path / "fresh", defaults=None)
    assert stores.roles.get("developer") is not None
    assert stores.loops.get("test-and-fix") is not None
    assert "default" in stores.commands.lists
    assert stores.migrated is False


def test_seeding_never_overwrites_and_survives_a_missing_source(tmp_path):
    target = tmp_path / ".trance" / "agents.json"
    assert seed(target, tmp_path / "nothing-here.json") is False
    assert not target.exists()

    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf8")
    assert seed(target, source) is True
    target.write_text('{"mine": true}', encoding="utf8")
    assert seed(target, source) is False
    assert json.loads(target.read_text()) == {"mine": True}


# ------------------------------------------------------------------ settings

def test_settings_survive_a_restart(tmp_path):
    """They never did: they lived in memory, so turning off commits lasted
    until the next restart — and this user restarts constantly."""
    path = tmp_path / ".trance" / "settings.json"
    SettingsStore(path).update(git_commits=False, max_step_points=8)

    reopened = SettingsStore(path)
    assert reopened.settings.git_commits is False
    assert reopened.settings.max_step_points == 8
    # Untouched fields keep their defaults rather than being blanked.
    assert reopened.settings.git_auto_init is True


def test_a_corrupt_settings_file_is_not_a_crash(tmp_path):
    path = tmp_path / ".trance" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf8")
    assert SettingsStore(path).settings == Settings()


def test_unknown_settings_keys_are_ignored(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    store.update(git_commits=False, something_invented=True)
    assert store.settings.git_commits is False
    assert not hasattr(store.settings, "something_invented")


# ----------------------------------------------------------------- workspace

def test_two_sessions_on_one_directory_share_its_stores(tmp_path):
    """They are two views of one project. Separate stores would let them
    disagree about what the agents are."""
    workspace = Workspace(defaults=_global(tmp_path))
    first = workspace.stores_for(tmp_path / "game")
    second = workspace.stores_for(str(tmp_path / "game") + "/")
    assert first is second


def test_a_deleted_project_is_forgotten(tmp_path):
    workspace = Workspace()
    held = workspace.stores_for(tmp_path / "gone")
    workspace.forget(tmp_path / "gone")
    assert workspace.stores_for(tmp_path / "gone") is not held


# --------------------------------------------------- through the whole server

def _serve(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    config.workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    return TestClient(app_module.create_app(config, tmp_path / "sessions")), config


def _make(client, name, tmp_path):
    return client.post("/api/sessions", json={
        "name": name, "project_dir": str(tmp_path / "workspace" / name)}).json()["id"]


def test_two_projects_tune_the_same_agent_differently(tmp_path):
    """The thing that was impossible before: one shared library meant tuning an
    agent for one project changed it for all of them."""
    client, _ = _serve(tmp_path)
    game = _make(client, "game", tmp_path)
    site = _make(client, "site", tmp_path)

    client.put("/api/agents/developer", json={"system_prompt": "canvas games"},
               params={"session": game})
    client.put("/api/agents/developer", json={"system_prompt": "accessible html"},
               params={"session": site})

    def prompt(sid):
        agents = client.get("/api/agents", params={"session": sid}).json()["agents"]
        return next(a for a in agents if a["name"] == "developer")["system_prompt"]

    assert prompt(game) == "canvas games"
    assert prompt(site) == "accessible html"
    # And each is written where the project is, not in a shared file.
    assert (tmp_path / "workspace" / "game" / ".trance" / "agents.json").is_file()
    assert (tmp_path / "workspace" / "site" / ".trance" / "agents.json").is_file()


def test_settings_are_per_project_and_survive_a_restart(tmp_path):
    client, config = _serve(tmp_path)
    game = _make(client, "game", tmp_path)
    site = _make(client, "site", tmp_path)

    client.put("/api/config/planning", json={"git_commits": False},
               params={"session": game})
    assert client.get(f"/api/sessions/{game}/settings").json()["git_commits"] is False
    assert client.get(f"/api/sessions/{site}/settings").json()["git_commits"] is True

    # A new server, the same folders: the answer has to be the same.
    from fastapi.testclient import TestClient
    from trance.server import app as app_module
    again = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    assert again.get(f"/api/sessions/{game}/settings").json()["git_commits"] is False


def test_configuration_endpoints_refuse_to_guess_which_project(tmp_path):
    """Answering "the first one" would silently edit the wrong project."""
    client, _ = _serve(tmp_path)
    _make(client, "game", tmp_path)

    for call in (lambda: client.get("/api/agents"),
                 lambda: client.get("/api/loops"),
                 lambda: client.get("/api/commands"),
                 lambda: client.put("/api/config/planning", json={"git_commits": False})):
        response = call()
        assert response.status_code == 400
        assert "which" in response.json()["detail"]


def test_models_stay_on_the_machine(tmp_path):
    """They carry API keys, and a folder you copy, zip and share is the last
    place a key should be."""
    client, _ = _serve(tmp_path)
    game = _make(client, "game", tmp_path)
    client.put("/api/presets/claude",
               json={"kind": "anthropic", "model": "claude-opus-5", "api_key": "sk-secret"})

    project = tmp_path / "workspace" / "game" / ".trance"
    assert not (project / "providers.json").exists()
    assert not (project / "presets.json").exists()
    for path in project.glob("*.json"):
        assert "sk-secret" not in path.read_text(), path
    # And the project still names the model; it resolves against this machine's.
    client.put("/api/agents/developer", json={"preset": "claude"}, params={"session": game})
    agents = client.get("/api/agents", params={"session": game}).json()["agents"]
    assert next(a for a in agents if a["name"] == "developer")["preset"] == "claude"


def test_a_new_session_needs_only_a_name(tmp_path):
    """Typing an absolute path was the ceremony between "I want to build this"
    and building it — and it was the same path every time, with the name on the
    end."""
    client, _ = _serve(tmp_path)
    made = client.post("/api/sessions", json={"name": "Chicken Invaders"}).json()

    assert made["project_dir"] == str(tmp_path / "workspace" / "chicken-invaders")
    # And it is a real project directory, configured like any other.
    client.put("/api/agents/developer", json={"system_prompt": "canvas games"},
               params={"session": made["id"]})
    assert (tmp_path / "workspace" / "chicken-invaders" / ".trance" / "agents.json").is_file()


def test_the_same_name_is_the_same_project(tmp_path):
    """A second session on a project you already have should join it, not start
    an empty copy beside it — which is what the stores assume, holding a project
    by its path."""
    client, _ = _serve(tmp_path)
    first = client.post("/api/sessions", json={"name": "pacman"}).json()
    client.put("/api/agents/developer", json={"system_prompt": "mazes"},
               params={"session": first["id"]})

    second = client.post("/api/sessions", json={"name": "Pacman"}).json()
    assert second["project_dir"] == first["project_dir"]
    agents = client.get("/api/agents", params={"session": second["id"]}).json()["agents"]
    assert next(a for a in agents if a["name"] == "developer")["system_prompt"] == "mazes"


def test_a_name_cannot_reach_outside_the_workspace(tmp_path):
    """The name becomes a path, so it is input to a path — and the folder it
    names has files written into it."""
    client, _ = _serve(tmp_path)
    workspace = (tmp_path / "workspace").resolve()

    for name in ("../escaped", "/etc/passwd", "..", ".", "  ", "..%2F..%2Fetc"):
        made = client.post("/api/sessions", json={"name": name})
        assert made.status_code == 200, (name, made.text)
        landed = Path(made.json()["project_dir"]).resolve()
        assert landed.is_relative_to(workspace), (name, landed)
        assert landed != workspace, name          # never the workspace itself


def test_an_explicit_directory_is_still_honoured(tmp_path):
    """Pointing trance at a repository that already exists, anywhere on disk, is
    the other half of what it is for."""
    client, _ = _serve(tmp_path)
    elsewhere = tmp_path / "some" / "checkout"
    made = client.post("/api/sessions",
                       json={"name": "work", "project_dir": str(elsewhere)}).json()
    assert made["project_dir"] == str(elsewhere)


def test_a_project_handed_over_arrives_configured(tmp_path):
    """The whole point: copy the folder, and the way it is built comes with it."""
    import shutil

    client, _ = _serve(tmp_path)
    game = _make(client, "game", tmp_path)
    client.put("/api/agents/developer", json={"system_prompt": "canvas games"},
               params={"session": game})
    client.put("/api/config/planning", json={"git_commits": False},
               params={"session": game})

    # Another machine, another trance, no shared runs/ at all.
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(tmp_path / "workspace" / "game", elsewhere / "game")

    from fastapi.testclient import TestClient
    from trance.config import Config
    from trance.server import app as app_module
    other = Config.load(tmp_path / "none.toml")
    other.runs_dir = str(tmp_path / "other-runs")
    other.workspace = str(elsewhere)
    theirs = TestClient(app_module.create_app(other, tmp_path / "other-sessions"))
    sid = theirs.post("/api/sessions", json={
        "name": "game", "project_dir": str(elsewhere / "game")}).json()["id"]

    agents = theirs.get("/api/agents", params={"session": sid}).json()["agents"]
    assert next(a for a in agents if a["name"] == "developer")["system_prompt"] == "canvas games"
    assert theirs.get(f"/api/sessions/{sid}/settings").json()["git_commits"] is False


# ------------------------------------------- editing what new projects start from

def test_the_defaults_can_be_edited_and_reach_the_next_project(tmp_path):
    """Per-project stores made tuning an agent for one project safe, and left
    no way to change what the *next* project starts from — so a prompt improved
    in four projects had to be improved a fifth time in the fifth."""
    client, _ = _serve(tmp_path)

    client.put("/api/agents/developer", json={"system_prompt": "always canvas games"},
               params={"session": "defaults"})

    made = client.post("/api/sessions", json={"name": "brand new"}).json()
    agents = client.get("/api/agents", params={"session": made["id"]}).json()["agents"]
    assert next(a for a in agents if a["name"] == "developer")["system_prompt"] \
        == "always canvas games"


def test_editing_the_defaults_leaves_existing_projects_alone(tmp_path):
    """They were copied at creation and have moved on since. A template that
    reached back into them would undo work nobody asked it to touch."""
    client, _ = _serve(tmp_path)
    already = client.post("/api/sessions", json={"name": "older"}).json()["id"]
    client.put("/api/agents/developer", json={"system_prompt": "tuned for this one"},
               params={"session": already})

    client.put("/api/agents/developer", json={"system_prompt": "the new default"},
               params={"session": "defaults"})

    agents = client.get("/api/agents", params={"session": already}).json()["agents"]
    assert next(a for a in agents if a["name"] == "developer")["system_prompt"] \
        == "tuned for this one"


def test_the_defaults_are_a_real_place_on_disk(tmp_path):
    """In the workspace's own .trance — each workspace tunes what its
    projects are provisioned from."""
    import json as _json

    client, config = _serve(tmp_path)
    client.put("/api/agents/developer", json={"tool_rounds": 44},
               params={"session": "defaults"})

    stored = _json.loads(
        (Path(config.workspace) / ".trance" / "agents.json").read_text())
    role = next(a for a in stored["agents"] if a["name"] == "developer")
    assert role["tool_rounds"] == 44


def test_a_request_with_no_session_still_refuses_to_guess(tmp_path):
    """The point of requiring it stands: answering "the first one" would edit
    the wrong project. The message now says how to ask for the defaults."""
    client, _ = _serve(tmp_path)
    answer = client.get("/api/agents")
    assert answer.status_code == 400
    assert "defaults" in answer.json()["detail"]


def test_the_defaults_have_their_own_settings(tmp_path):
    client, _ = _serve(tmp_path)
    client.put("/api/config/planning", json={"git_commits": False},
               params={"session": "defaults"})
    assert client.get("/api/sessions/defaults/settings").json()["git_commits"] is False

    made = client.post("/api/sessions", json={"name": "inherits it"}).json()["id"]
    assert client.get(f"/api/sessions/{made}/settings").json()["git_commits"] is False


# --------------- three layers: system, workspace, project

def _layered(tmp_path, ws):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    config.system_dir = str(tmp_path / "system")
    config.workspace = str(ws)
    ws.mkdir(exist_ok=True)
    return TestClient(app_module.create_app(config, tmp_path / "sessions"))


def test_models_are_the_machines_and_the_library_is_the_workspaces(tmp_path):
    """Models and settings are system-wide — configure the endpoint once.
    The agents, loops and allowlists are the workspace's own: tuning one
    workspace's developer never leaks into another, and a fresh workspace
    starts from shipped — the only arrangement under which no workspace can
    inherit another's library."""
    first = _layered(tmp_path, tmp_path / "ws_one")
    first.put("/api/presets/local-qwen",
              json={"kind": "llamacpp", "model": "qwen-27b"})
    first.put("/api/agents/developer", json={"tool_rounds": 44},
              params={"session": "defaults"})

    other = _layered(tmp_path, tmp_path / "ws_two")
    # The model reached the second workspace; the agent tuning did not.
    assert "local-qwen" in {p["name"] for p in
                            other.get("/api/presets").json()["presets"]}
    dev = next(a for a in other.get("/api/agents",
                                    params={"session": "defaults"}).json()["agents"]
               if a["name"] == "developer")
    assert dev["tool_rounds"] != 44
    assert (tmp_path / "system" / "providers.json").exists()
    assert (tmp_path / "ws_one" / ".trance" / "agents.json").exists()
    assert not (tmp_path / "system" / "agents.json").exists()


def test_a_new_project_is_provisioned_from_the_system_defaults(tmp_path):
    client = _layered(tmp_path, tmp_path / "ws")
    sid = client.post("/api/sessions", json={"name": "game"}).json()["id"]

    loops = client.get("/api/loops", params={"session": sid}).json()["loops"]
    assert {l["name"] for l in loops} == {"test-and-fix", "visual-test-and-fix"}
    agents = client.get("/api/agents", params={"session": sid}).json()["agents"]
    assert {a["name"] for a in agents} == {"developer", "orchestrator", "planner",
                                           "regression", "reviewer", "tester",
                                           "visual-tester"}


def test_the_legacy_runs_state_is_adopted_into_the_system_dir_once(tmp_path):
    import json

    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    (runs / "providers.json").write_text(json.dumps({"providers": [
        {"name": "old-local", "kind": "llamacpp", "model": "qwen"}], "presets": []}))

    client = _layered(tmp_path, tmp_path / "ws")
    assert (tmp_path / "system" / "providers.json").exists()
    assert "old-local" in {p["name"] for p in
                           client.get("/api/presets").json()["presets"]}
    assert (runs / "providers.json").exists()   # the legacy file is left alone


def test_an_unedited_builtin_in_a_project_tracks_shipped(tmp_path):
    """The Default scope's contract, extended to projects: found live twice —
    a tester running a frozen months-old prompt, then a new tool whose prompt
    guidance reached no existing project, because every copy froze at
    creation day. Edited copies are the user's and stay frozen."""
    import json

    from trance.agents.roles import BUILTIN_ROLES
    from trance.workspace import ProjectStores

    project = tmp_path / "game"
    stores = ProjectStores(project)
    stale = json.loads((project / ".trance" / "agents.json").read_text())
    for agent in stale["agents"]:
        if agent["name"] == "visual-tester":
            agent["system_prompt"] = "an old frozen prompt"
            agent["preset"] = "local-qwen"                # the user's wiring
            agent["definition_edited"] = False
        if agent["name"] == "tester":
            agent["system_prompt"] = "my own tester prompt"
            agent["definition_edited"] = True             # a real edit
    (project / ".trance" / "agents.json").write_text(json.dumps(stale))

    again = ProjectStores(project)
    tracked = again.roles.get("visual-tester")
    assert tracked.system_prompt == BUILTIN_ROLES["visual-tester"].system_prompt
    assert "move_mouse" in tracked.system_prompt          # improvements arrive
    assert tracked.preset == "local-qwen"                 # wiring is kept
    assert again.roles.get("tester").system_prompt == "my own tester prompt"


def test_an_unedited_builtin_in_a_project_tracks_shipped(tmp_path):
    """The Default scope's contract, extended to projects: found live twice —
    a tester running a frozen months-old prompt, then a new tool whose prompt
    guidance reached no existing project, because every copy froze at
    creation day. Edited copies are the user's and stay frozen."""
    import json

    from trance.agents.roles import BUILTIN_ROLES
    from trance.workspace import ProjectStores

    project = tmp_path / "game"
    stores = ProjectStores(project)
    stale = json.loads((project / ".trance" / "agents.json").read_text())
    for agent in stale["agents"]:
        if agent["name"] == "visual-tester":
            agent["system_prompt"] = "an old frozen prompt"
            agent["preset"] = "local-qwen"                # the user's wiring
            agent["definition_edited"] = False
        if agent["name"] == "tester":
            agent["system_prompt"] = "my own tester prompt"
            agent["definition_edited"] = True             # a real edit
    (project / ".trance" / "agents.json").write_text(json.dumps(stale))

    again = ProjectStores(project)
    tracked = again.roles.get("visual-tester")
    assert tracked.system_prompt == BUILTIN_ROLES["visual-tester"].system_prompt
    assert "move_mouse" in tracked.system_prompt          # improvements arrive
    assert tracked.preset == "local-qwen"                 # wiring is kept
    assert again.roles.get("tester").system_prompt == "my own tester prompt"


