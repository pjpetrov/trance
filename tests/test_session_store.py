"""Session persistence and deletion."""

from trance.session import SessionStore


def test_sessions_survive_a_restart(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create("orders", "/tmp/orders")
    assert SessionStore(tmp_path).get(session.id) is not None


def test_delete_removes_the_session_from_disk(tmp_path):
    """Regression: popping the in-memory entry alone left the directory, so a
    deleted session reappeared the next time the store loaded from disk."""
    store = SessionStore(tmp_path)
    session = store.create("orders", "/tmp/orders")
    assert (tmp_path / session.id / "session.json").exists()

    assert store.delete(session.id) is True
    assert not (tmp_path / session.id).exists()
    assert store.get(session.id) is None
    assert SessionStore(tmp_path).get(session.id) is None  # stays deleted


def test_delete_signals_a_running_session_to_stop(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create("orders", "/tmp/orders")
    session.status = "running"
    store.delete(session.id)
    assert session.stopping  # the engine unwinds instead of writing to a gone dir


def test_delete_is_idempotent(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create("orders", "/tmp/orders")
    assert store.delete(session.id) is True
    assert store.delete(session.id) is False


def test_deleting_one_session_leaves_the_others(tmp_path):
    store = SessionStore(tmp_path)
    keep = store.create("keep", "/tmp/keep")
    drop = store.create("drop", "/tmp/drop")
    store.delete(drop.id)
    assert [s.id for s in SessionStore(tmp_path).all()] == [keep.id]


# --------------------------------------------------------- project dir

def test_typo_in_the_project_path_is_caught_with_a_suggestion(tmp_path):
    """Regression: '/home/ppetrov/...' surfaced as a PermissionError traceback
    partway into a run, long after the flow had been planned."""
    from trance.engine import check_project_dir

    home = tmp_path / "home"
    (home / "petrovs").mkdir(parents=True)
    home.chmod(0o555)  # not writable, like a real /home
    try:
        error, _ = check_project_dir(str(home / "ppetrov" / "web_worms"))
        assert error and "not writable" in error
        assert "petrovs" in error  # suggests the near-miss
    finally:
        home.chmod(0o755)


def test_relative_paths_are_rejected():
    from trance.engine import check_project_dir

    error, _ = check_project_dir("some/relative/dir")
    assert error and "absolute" in error


def test_a_file_where_a_directory_belongs_is_rejected(tmp_path):
    from trance.engine import check_project_dir

    target = tmp_path / "afile"
    target.write_text("x")
    error, _ = check_project_dir(str(target))
    assert error and "not a directory" in error


def test_creatable_and_existing_directories_pass(tmp_path):
    from trance.engine import check_project_dir

    assert check_project_dir(str(tmp_path))[0] is None
    assert check_project_dir(str(tmp_path / "does" / "not" / "exist" / "yet"))[0] is None


def test_tilde_is_expanded_and_path_normalized(tmp_path):
    from trance.engine import check_project_dir

    _, normalized = check_project_dir(str(tmp_path / "a" / ".." / "b"))
    assert normalized.endswith("/b") and ".." not in normalized


# ------------------------------------------------------------- rerun

def test_rerun_restarts_the_engine_when_the_run_has_finished(tmp_path, monkeypatch):
    """Regression: rerun only set the step to 'pending'. Once a run finished,
    its engine thread was gone, so the step sat pending and nothing was sent
    to the model."""
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    started = []

    class FakeEngine:
        def __init__(self, session, config, bus, on_change=None):
            self.session = session

        def start(self):
            started.append(self.session.id)
            self.session.status = "running"
            return None       # no live thread, mirroring a finished run

    monkeypatch.setattr(app_module, "FlowEngine", FakeEngine)
    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    sid = client.post("/api/sessions", json={
        "name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={
        "steps": [{"role": "backend", "task": "do it"}]})

    client.post(f"/api/sessions/{sid}/start")
    assert started == [sid]

    # Simulate the run having finished: the step is done, no thread alive.
    session = client.app.state.store.get(sid)
    session.flow.steps[0].status = "done"
    session.status = "finished"

    step_id = session.flow.steps[0].id
    body = client.post(f"/api/sessions/{sid}/steps/{step_id}/rerun").json()
    assert body["restarted"] is True
    assert started == [sid, sid]          # a second engine was launched
    assert session.flow.steps[0].status == "pending"
    assert session.flow.steps[0].attempts == []   # fresh attempt, not N+1


def test_rerun_does_not_launch_a_second_engine_while_one_is_live(tmp_path, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    started = []
    keep_going = threading.Event()

    class FakeEngine:
        def __init__(self, session, config, bus, on_change=None):
            self.session = session

        def start(self):
            started.append(self.session.id)
            thread = threading.Thread(target=keep_going.wait, daemon=True)
            thread.start()
            self.session._thread = thread
            self.session.status = "running"
            return thread

    monkeypatch.setattr(app_module, "FlowEngine", FakeEngine)
    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    try:
        sid = client.post("/api/sessions", json={
            "name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
        client.put(f"/api/sessions/{sid}/flow", json={
            "steps": [{"role": "backend", "task": "do it"}]})
        client.post(f"/api/sessions/{sid}/start")

        session = client.app.state.store.get(sid)
        step_id = session.flow.steps[0].id
        body = client.post(f"/api/sessions/{sid}/steps/{step_id}/rerun").json()
        assert body["restarted"] is False   # the live engine will pick it up
        assert started == [sid]

        assert client.post(f"/api/sessions/{sid}/start").status_code == 409
    finally:
        keep_going.set()


# ------------------------------------------------------ pause / resume

def _client(tmp_path, monkeypatch, engines):
    """A client whose FlowEngine records starts and leaves no live thread."""
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    class FakeEngine:
        def __init__(self, session, config, bus, on_change=None):
            self.session = session

        def start(self):
            engines.append(self.session.id)
            self.session.status = "running"
            return None        # no live thread, exactly like a finished/stopped run

    monkeypatch.setattr(app_module, "FlowEngine", FakeEngine)
    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    return TestClient(app_module.create_app(config, tmp_path / "sessions"))


def test_resume_after_stop_starts_a_fresh_engine(tmp_path, monkeypatch):
    """Regression: stop makes the engine thread exit, so clearing the pause flag
    resumed nothing and the run sat there looking paused."""
    engines = []
    client = _client(tmp_path, monkeypatch, engines)
    sid = client.post("/api/sessions", json={
        "name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow",
               json={"steps": [{"role": "backend", "task": "do it"}]})
    client.post(f"/api/sessions/{sid}/start")
    assert engines == [sid]

    client.post(f"/api/sessions/{sid}/stop")
    session = client.app.state.store.get(sid)
    assert session.stopping

    body = client.post(f"/api/sessions/{sid}/resume").json()
    assert body["running"] is True and body["restarted"] is True
    assert engines == [sid, sid]
    assert not session.stopping          # the flag must not survive a resume


def test_resume_while_merely_paused_does_not_start_a_second_engine(tmp_path, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    engines = []
    running = threading.Event()

    class FakeEngine:
        def __init__(self, session, config, bus, on_change=None):
            self.session = session

        def start(self):
            engines.append(self.session.id)
            thread = threading.Thread(target=running.wait, daemon=True)
            thread.start()
            self.session._thread = thread
            self.session.status = "running"
            return thread

    monkeypatch.setattr(app_module, "FlowEngine", FakeEngine)
    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    try:
        sid = client.post("/api/sessions", json={
            "name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
        client.put(f"/api/sessions/{sid}/flow",
                   json={"steps": [{"role": "backend", "task": "t"}]})
        client.post(f"/api/sessions/{sid}/start")

        client.post(f"/api/sessions/{sid}/pause")
        assert client.app.state.store.get(sid).paused

        body = client.post(f"/api/sessions/{sid}/resume").json()
        assert body["restarted"] is False and body["running"] is True
        assert engines == [sid]                 # the live engine simply continues
        assert not client.app.state.store.get(sid).paused
    finally:
        running.set()


def test_resume_with_nothing_pending_says_so(tmp_path, monkeypatch):
    engines = []
    client = _client(tmp_path, monkeypatch, engines)
    sid = client.post("/api/sessions", json={
        "name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow",
               json={"steps": [{"role": "backend", "task": "t"}]})
    session = client.app.state.store.get(sid)
    session.flow.steps[0].status = "done"

    body = client.post(f"/api/sessions/{sid}/resume").json()
    assert body["running"] is False
    assert "rerun one" in body["reason"]
    assert engines == []                        # nothing was started


# ------------------------------------------- rerun while a step is in flight

def _slow_engine_client(tmp_path, monkeypatch, engines, release):
    """A client whose engine thread lives until `release` is set, like one stuck
    inside a model call."""
    import threading

    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    class FakeEngine:
        def __init__(self, session, config, bus, on_change=None):
            self.session = session

        def start(self):
            engines.append(self.session.id)
            self.session.status = "running"
            thread = threading.Thread(target=release.wait, daemon=True)
            thread.start()
            self.session._thread = thread
            return thread

    monkeypatch.setattr(app_module, "FlowEngine", FakeEngine)
    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    return TestClient(app_module.create_app(config, tmp_path / "sessions"))


def test_rerun_after_a_stop_starts_once_the_model_call_returns(tmp_path, monkeypatch):
    """Regression: stop only lands when the in-flight model call returns. A
    rerun during that window started nothing, and nobody started anything after
    — the step sat pending until the user clicked again."""
    import threading
    import time

    engines: list[str] = []
    release = threading.Event()
    client = _slow_engine_client(tmp_path, monkeypatch, engines, release)
    try:
        sid = client.post("/api/sessions", json={
            "name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
        client.put(f"/api/sessions/{sid}/flow",
                   json={"steps": [{"role": "backend", "task": "t"}]})
        client.post(f"/api/sessions/{sid}/start")
        assert engines == [sid]

        session = client.app.state.store.get(sid)
        client.post(f"/api/sessions/{sid}/stop")           # still mid-inference
        step_id = session.flow.steps[0].id
        body = client.post(f"/api/sessions/{sid}/steps/{step_id}/rerun").json()

        assert body["restarted"] is False
        assert "model call" in body["waiting_for"]         # not silently nothing
        assert engines == [sid]

        release.set()                                      # the call returns
        for _ in range(100):
            if len(engines) > 1:
                break
            time.sleep(0.05)
        assert engines == [sid, sid]                       # handed over, no click
        assert not session.stopping
    finally:
        release.set()


def test_rerunning_a_step_un_pauses_the_session(tmp_path, monkeypatch):
    """"Run this step" and "stay paused" cannot both be honoured, and only one
    of them was just asked for."""
    engines: list[str] = []
    client = _client(tmp_path, monkeypatch, engines)
    sid = client.post("/api/sessions", json={
        "name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow",
               json={"steps": [{"role": "backend", "task": "t"}]})
    client.post(f"/api/sessions/{sid}/start")
    client.post(f"/api/sessions/{sid}/pause")

    session = client.app.state.store.get(sid)
    assert session.paused
    body = client.post(f"/api/sessions/{sid}/steps/{session.flow.steps[0].id}/rerun").json()

    assert body["resumed"] is True
    assert not session.paused
    assert body["restarted"] is True


def test_only_one_handover_is_armed_per_session(tmp_path, monkeypatch):
    """Clicking rerun five times must not queue five engines."""
    import threading

    engines: list[str] = []
    release = threading.Event()
    client = _slow_engine_client(tmp_path, monkeypatch, engines, release)
    try:
        sid = client.post("/api/sessions", json={
            "name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
        client.put(f"/api/sessions/{sid}/flow",
                   json={"steps": [{"role": "backend", "task": "t"}]})
        client.post(f"/api/sessions/{sid}/start")
        client.post(f"/api/sessions/{sid}/stop")

        step_id = client.app.state.store.get(sid).flow.steps[0].id
        for _ in range(5):
            client.post(f"/api/sessions/{sid}/steps/{step_id}/rerun")

        release.set()
        import time
        time.sleep(0.4)
        assert len(engines) == 2        # the original and exactly one successor
    finally:
        release.set()
