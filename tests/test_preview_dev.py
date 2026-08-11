"""Running a project rather than serving its files.

The preview served a folder and refused to run anything, which is right for a
page of HTML and wrong for every app with a build step: it loads, then dies on
its first bare import. Running it is now offered — asked for explicitly, with
the command shown first, because it starts a build on the machine trance runs
on.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from trance import preview


def _project() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())


# --------------------------------------------------------------- the runner

def test_the_port_comes_from_what_the_server_says_not_from_its_config():
    """A dev server settles on another port when its configured one is taken,
    and a preview pointed at the configured port shows someone else's app."""
    project = _project()
    (project / "server.py").write_text(
        "import time\n"
        "print('  \\u279c  Local:   http://localhost:5199/')\n"
        "import sys; sys.stdout.flush()\n"
        "time.sleep(30)\n", encoding="utf8")

    running = preview.run_dev(project, "python3 server.py", wait_s=15)
    try:
        assert running.port == 5199
        assert running.alive() is True
        assert running.to_dict()["dev"] is True
    finally:
        running.stop()
    assert running.alive() is False


def test_a_command_that_exits_is_reported_with_what_it_said():
    """"It did not work" is not worth reading. The last thing the command
    printed is usually the whole diagnosis — a missing dependency, a port in
    use, a script that does not exist."""
    project = _project()
    (project / "server.py").write_text(
        "import sys\nprint('Error: Cannot find module vite')\nsys.exit(1)\n",
        encoding="utf8")

    with pytest.raises(preview.DevServerFailed) as raised:
        preview.run_dev(project, "python3 server.py", wait_s=15)
    assert "exited before it served anything" in str(raised.value)
    assert "Cannot find module vite" in raised.value.output


def test_a_server_that_never_says_where_it_is_gets_stopped():
    """A process nobody can reach is a process nobody will reap. Waiting
    forever for an address that is not coming leaves it running after the tab
    is closed."""
    project = _project()
    (project / "server.py").write_text(
        "import time\nprint('starting up, no address here')\ntime.sleep(30)\n",
        encoding="utf8")

    with pytest.raises(preview.DevServerFailed) as raised:
        preview.run_dev(project, "python3 server.py", wait_s=2)
    assert "did not print an address" in str(raised.value)
    assert "It has been stopped." in str(raised.value)


def test_the_whole_tree_is_stopped_not_just_the_shell():
    """npm spawns node, node spawns esbuild. Killing only what trance started
    leaves the rest of the tree holding the port."""
    project = _project()
    (project / "child.py").write_text("import time\ntime.sleep(30)\n", encoding="utf8")
    (project / "server.py").write_text(
        "import subprocess, sys, time\n"
        "kid = subprocess.Popen([sys.executable, 'child.py'])\n"
        "print('Local: http://127.0.0.1:5300/'); sys.stdout.flush()\n"
        "open('kid.pid', 'w').write(str(kid.pid))\n"
        "time.sleep(30)\n", encoding="utf8")

    running = preview.run_dev(project, "python3 server.py", wait_s=15)
    kid = int((project / "kid.pid").read_text())
    running.stop()

    import os
    import time as _time
    for _ in range(40):
        try:
            os.kill(kid, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.1)
    else:
        os.kill(kid, 9)
        pytest.fail("the child outlived the stop")


# ------------------------------------------------------- the vite host note

def test_a_vite_project_is_told_what_a_tunnel_needs():
    """Vite answers a tunnel with "Blocked request" until its host is allowed,
    which reads as a broken tunnel rather than one missing line of config."""
    project = _project()
    (project / "vite.config.ts").write_text("export default {}", encoding="utf8")

    said = preview.allowed_hosts_note(project, "https://abc-123.ngrok-free.app")
    assert "vite.config.ts" in said
    assert "allowedHosts: ['abc-123.ngrok-free.app']" in said


def test_nothing_is_said_about_a_project_that_has_no_vite():
    """A static folder behind a tunnel needs no configuration, and advice about
    a file that is not there is worse than silence."""
    project = _project()
    assert preview.allowed_hosts_note(project, "https://abc.ngrok-free.app") == ""
    # Nor when there is no tunnel to allow.
    (project / "vite.config.js").write_text("export default {}", encoding="utf8")
    assert preview.allowed_hosts_note(project, "") == ""


# --------------------------------------------------- through the whole server

def _serve(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    config.workspace = str(tmp_path / "ws")
    (tmp_path / "ws").mkdir(exist_ok=True)
    app = app_module.create_app(config, tmp_path / "sessions")
    return TestClient(app), app


def test_running_a_project_needs_a_command_someone_has_seen(tmp_path):
    """The endpoint will not guess one. Every path to it goes through the
    proposal, which goes through a person."""
    client, _ = _serve(tmp_path)
    sid = client.post("/api/sessions", json={"name": "app"}).json()["id"]

    answer = client.post(f"/api/sessions/{sid}/preview", json={"mode": "dev"})
    assert answer.status_code == 400
    assert "which command" in answer.json()["detail"]


def test_a_dev_server_replaces_the_static_one_and_is_stopped_with_it(tmp_path):
    client, app = _serve(tmp_path)
    made = client.post("/api/sessions", json={"name": "app"}).json()
    sid, project = made["id"], pathlib.Path(made["project_dir"])
    project.mkdir(parents=True, exist_ok=True)
    (project / "index.html").write_text("<h1>hi</h1>", encoding="utf8")
    (project / "server.py").write_text(
        "import time\nprint('Local: http://127.0.0.1:5321/')\n"
        "import sys; sys.stdout.flush()\ntime.sleep(30)\n", encoding="utf8")

    client.post(f"/api/sessions/{sid}/preview", json={"path": "index.html"})
    static_port = app.state.previews[sid].port

    body = client.post(f"/api/sessions/{sid}/preview",
                       json={"mode": "dev", "command": "python3 server.py"}).json()
    assert body["dev"] is True
    assert body["port"] == 5321
    assert body["command"] == "python3 server.py"
    assert app.state.previews[sid].port != static_port      # the static one is gone

    client.delete(f"/api/sessions/{sid}/preview")
    assert sid not in app.state.previews


def test_a_command_that_fails_says_what_it_printed(tmp_path):
    client, _ = _serve(tmp_path)
    made = client.post("/api/sessions", json={"name": "app"}).json()
    project = pathlib.Path(made["project_dir"])
    project.mkdir(parents=True, exist_ok=True)
    (project / "server.py").write_text(
        "import sys\nprint('Error: port 5173 already in use')\nsys.exit(1)\n",
        encoding="utf8")

    answer = client.post(f"/api/sessions/{made['id']}/preview",
                         json={"mode": "dev", "command": "python3 server.py"})
    assert answer.status_code == 502
    assert "port 5173 already in use" in answer.json()["detail"]


def test_the_proposal_reads_the_readme(tmp_path, monkeypatch):
    """A dev command is not always `npm run dev`. When a README names one, it
    names it because the obvious one does not work."""
    from trance.agents import orchestrator as orchestrator_agent

    client, _ = _serve(tmp_path)
    made = client.post("/api/sessions", json={"name": "app"}).json()
    project = pathlib.Path(made["project_dir"])
    project.mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text(
        "# App\n\nRun it with `pnpm --filter web dev` (npm run dev builds the "
        "wrong workspace).\n", encoding="utf8")

    seen = {}

    def _asked(project_dir, *, config, bus, session_id=""):
        readme = (project_dir / "README.md").read_text()
        seen["prompt_had_readme"] = "pnpm --filter web dev" in readme
        return {"command": "pnpm --filter web dev", "dir": "", "why": "the README says so",
                "static_instead": False, "read_readme": True}

    monkeypatch.setattr(orchestrator_agent, "how_to_run", _asked)
    body = client.post(f"/api/sessions/{made['id']}/preview/plan").json()

    assert body["command"] == "pnpm --filter web dev"
    assert body["read_readme"] is True
    assert seen["prompt_had_readme"] is True


# ----------------------------------- a preview survives the harness restarting

def _sleeper():
    import subprocess

    return subprocess.Popen(["sleep", "60"], start_new_session=True,
                            stdout=subprocess.DEVNULL)


def test_a_dev_preview_is_found_again_after_a_restart(tmp_path, monkeypatch):
    """The dev server runs in its own session and survives trance dying — so a
    restart used to leave it running *and* forgotten: holding its port, absent
    from the UI, with no button anywhere that could stop it. The record in the
    project's .trance is how the next trance finds it."""
    import os
    from types import SimpleNamespace

    client, app = _serve(tmp_path)
    made = client.post("/api/sessions", json={"name": "app"}).json()
    sid, project = made["id"], pathlib.Path(made["project_dir"])
    project.mkdir(parents=True, exist_ok=True)

    proc = _sleeper()
    monkeypatch.setattr(
        preview, "run_dev",
        lambda where, command, log_dir=None: preview.DevServer(
            command=command, root=str(where), port=5177, process=proc,
            log=str(log_dir / "dev-server.log")))
    client.post(f"/api/sessions/{sid}/preview",
                json={"mode": "dev", "command": "npm run dev"})
    assert (project / ".trance" / "preview.json").is_file()

    # The restart: the registry is what dies with the process.
    app.state.previews.clear()

    body = client.get(f"/api/sessions/{sid}/preview").json()
    assert body["dev"] is True
    assert body["port"] == 5177
    assert body["command"] == "npm run dev"

    # And stop reaches the adopted process, not just the registry.
    client.delete(f"/api/sessions/{sid}/preview")
    proc.wait(timeout=10)
    assert not (project / ".trance" / "preview.json").exists()


def test_a_dev_server_that_died_while_trance_was_away_is_forgotten(tmp_path):
    client, app = _serve(tmp_path)
    made = client.post("/api/sessions", json={"name": "app"}).json()
    sid, project = made["id"], pathlib.Path(made["project_dir"])
    (project / ".trance").mkdir(parents=True, exist_ok=True)

    dead = _sleeper()
    dead.kill(); dead.wait()
    (project / ".trance" / "preview.json").write_text(json.dumps({
        "mode": "dev", "command": "npm run dev", "root": str(project),
        "port": 5178, "pid": dead.pid}), encoding="utf8")

    body = client.get(f"/api/sessions/{sid}/preview").json()
    assert body["url"] == "" and body["port"] == 0
    # The stale record is cleaned, not re-tried on every poll.
    assert not (project / ".trance" / "preview.json").exists()


def test_a_static_preview_is_served_again_after_a_restart(tmp_path):
    """The static server is in-process and dies with trance; the user pressed
    play and never pressed stop, so it is simply served again."""
    client, app = _serve(tmp_path)
    made = client.post("/api/sessions", json={"name": "app"}).json()
    sid, project = made["id"], pathlib.Path(made["project_dir"])
    project.mkdir(parents=True, exist_ok=True)
    (project / "index.html").write_text("<h1>hi</h1>", encoding="utf8")

    client.post(f"/api/sessions/{sid}/preview", json={"path": "index.html"})
    first = app.state.previews[sid]
    port = first.port
    first.stop()                              # what the restart does to it
    app.state.previews.clear()

    body = client.get(f"/api/sessions/{sid}/preview").json()
    assert body["url"]
    assert body["port"] == port               # same address where possible
    assert app.state.previews[sid].alive()
    client.delete(f"/api/sessions/{sid}/preview")
