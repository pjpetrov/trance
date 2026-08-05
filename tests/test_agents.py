"""Tests for the multi-agent layer: remits, tools, config resolution, salvage."""

from pathlib import Path

import pytest

from trance.agents.roles import BUILTIN_ROLES, AgentRole
from trance.agents.runner import TRIMMED, fit_context
from trance.agents.tools import AgentTools
from trance.config import Config
from trance.flow import Attempt, Flow, Step
from trance.worker.client import salvage_tool_calls


@pytest.fixture
def project(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "main.py").write_text("def hello():\n    return 'hi'\n")
    return tmp_path


def _tools(project, role_name="backend"):
    return AgentTools(project, BUILTIN_ROLES[role_name])


# ------------------------------------------------------------------ remits

def test_role_remit_allows_and_denies(project):
    backend = BUILTIN_ROLES["backend"]
    assert backend.may_write("backend/main.py")
    assert not backend.may_write("frontend/src/app.tsx")


def test_write_outside_remit_is_refused_and_reported(project):
    result = _tools(project).write_file("frontend/src/app.tsx", "export const x = 1;")
    assert not result.ok
    assert result.remit_violation == "frontend/src/app.tsx"
    assert not (project / "frontend").exists()  # nothing was created
    assert "frontend" in result.text and "remit" in result.text


def test_write_inside_remit_succeeds(project):
    result = _tools(project).write_file("backend/api.py", "X = 1\n")
    assert result.ok and result.files_written == ["backend/api.py"]
    assert (project / "backend" / "api.py").read_text() == "X = 1\n"


def test_paths_cannot_escape_the_project(project):
    for path in ("../../etc/passwd", "/etc/passwd"):
        assert not _tools(project).write_file(path, "pwned").ok
        assert not _tools(project).read_file(path).ok


def test_command_allowlist(project):
    tools = _tools(project, "tester")
    refused = tools.run_command("rm -rf /")
    assert not refused.ok and "not an allowed program" in refused.text
    assert tools.run_command("python3 -c 'print(1+1)'").ok


def test_large_reads_are_truncated(project):
    (project / "backend" / "big.py").write_text("# pad\n" * 20000)
    result = _tools(project).read_file("backend/big.py")
    assert result.ok and "truncated" in result.text
    assert result.tokens < 8000  # would have been ~30k untruncated


# ----------------------------------------------------------- context budget

def test_fit_context_drops_oldest_tool_results_first():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "tool", "content": "x" * 40000},
        {"role": "tool", "content": "y" * 40000},
    ]
    fitted, dropped = fit_context(messages, budget=11000)
    assert dropped == 1
    assert fitted[2]["content"] == TRIMMED  # oldest went first
    assert fitted[3]["content"].startswith("y")


def test_fit_context_never_drops_system_or_task():
    messages = [
        {"role": "system", "content": "s" * 100000},
        {"role": "user", "content": "the task"},
    ]
    fitted, dropped = fit_context(messages, budget=100)
    assert dropped == 0
    assert fitted[0]["content"].startswith("s") and fitted[1]["content"] == "the task"


def test_fit_context_is_a_noop_under_budget():
    messages = [{"role": "tool", "content": "small"}]
    fitted, dropped = fit_context(messages, budget=10_000)
    assert dropped == 0 and fitted[0]["content"] == "small"


# -------------------------------------------------------------- config

def test_per_agent_provider_resolution(tmp_path):
    cfg_file = tmp_path / "trance.toml"
    cfg_file.write_text("""
[providers.big]
base_url = "http://big/v1"
model = "big-model"
context_window = 64000

[providers.small]
base_url = "http://small/v1"
model = "small-model"
context_window = 8000

[worker]
provider = "big"

[orchestrator]
provider = "small"
""")
    config = Config.load(cfg_file)

    coder = AgentRole(name="backend", title="B", description="", system_prompt="")
    tester = AgentRole(name="tester", title="T", description="", system_prompt="", provider="small")
    override = AgentRole(name="x", title="X", description="", system_prompt="",
                         provider="small", model="custom-model")

    assert config.for_role(coder).base_url == "http://big/v1"
    assert config.for_role(coder).model == "big-model"
    assert config.for_role(tester).base_url == "http://small/v1"
    assert config.for_role(tester).context_window == 8000
    assert config.for_role(override).model == "custom-model"
    # The orchestrator is configured centrally, not per role.
    assert config.for_orchestrator().base_url == "http://small/v1"


def test_input_budget_leaves_room_to_generate():
    config = Config.load(Path("/nonexistent"))
    resolved = config.resolve(config.worker)
    assert resolved.input_budget < resolved.context_window - resolved.max_tokens + 1


def test_api_keys_are_redacted(tmp_path):
    cfg_file = tmp_path / "trance.toml"
    cfg_file.write_text('[providers.p]\napi_key = "sk-secret"\n')
    assert "sk-secret" not in str(Config.load(cfg_file).to_dict())


# --------------------------------------------------------------- salvage

@pytest.mark.parametrize("text,expected", [
    ('{"name": "write_file", "arguments": {"path": "a.py", "content": "x=1"}}', "write_file"),
    ('```json\n{"name":"read_file","arguments":{"path":"b.py"}}\n```', "read_file"),
    ('{"type":"function","function":{"name":"read_file","arguments":{"path":"c.py"}}}', "read_file"),
])
def test_salvage_recovers_printed_tool_calls(text, expected):
    calls = salvage_tool_calls(text, {"write_file", "read_file"})
    assert [c.name for c in calls] == [expected]


def test_salvage_handles_braces_inside_strings():
    text = '{"name":"write_file","arguments":{"path":"d.py","content":"def f(): return {\\"k\\": 1}"}}'
    (call,) = salvage_tool_calls(text, {"write_file"})
    assert call.arguments["content"] == 'def f(): return {"k": 1}'


def test_salvage_refuses_tools_the_role_does_not_have():
    assert salvage_tool_calls('{"name":"run_command","arguments":{"command":"rm -rf /"}}',
                              {"write_file"}) == []
    assert salvage_tool_calls("just prose about writing files", {"write_file"}) == []


# ------------------------------------------------------------------ flow

def _edit(step, **changes):
    """A copy of `step` with the same id and some fields changed."""
    edited = Step(role=step.role, task=step.task, verify_with=step.verify_with,
                  max_attempts=step.max_attempts, entry=step.entry)
    edited.id = step.id
    for key, value in changes.items():
        setattr(edited, key, value)
    return edited


def test_editing_a_failed_step_requeues_it():
    """A plan you cannot correct after it failed is not much use."""
    failed = Step(role="frontend", task="build it", status="failed",
                  verify_with="tester", attempts=[Attempt(n=1, verdict="FAIL")])
    flow = Flow(steps=[failed])

    outcome = flow.apply_edits([_edit(failed, verify_with="factchecker")])
    assert outcome["requeued"] == [failed.id]
    assert flow.steps[0].status == "pending"
    assert flow.steps[0].verify_with == "factchecker"
    assert flow.steps[0].attempts == []       # a re-queued step starts clean


def test_editing_a_finished_step_requeues_it_too():
    done = Step(role="backend", task="old task", status="done")
    flow = Flow(steps=[done])
    flow.apply_edits([_edit(done, task="new task")])
    assert flow.steps[0].status == "pending" and flow.steps[0].task == "new task"


def test_a_cosmetic_edit_does_not_requeue():
    done = Step(role="backend", task="t", status="done", max_attempts=2)
    flow = Flow(steps=[done])
    outcome = flow.apply_edits([_edit(done, max_attempts=5)])
    assert outcome["requeued"] == []
    assert flow.steps[0].status == "done" and flow.steps[0].max_attempts == 5


def test_a_step_in_flight_cannot_be_edited_or_removed():
    running = Step(role="backend", task="original", status="running")
    flow = Flow(steps=[running])

    flow.apply_edits([_edit(running, task="hijacked")])
    assert flow.steps[0].task == "original" and flow.steps[0].status == "running"

    flow.apply_edits([])                       # try to delete it mid-flight
    assert [s.id for s in flow.steps] == [running.id]


def test_editing_keeps_queued_steering_on_a_pending_step():
    todo = Step(role="backend", task="t")
    todo.steering.append("use SQLModel")
    flow = Flow(steps=[todo])
    flow.apply_edits([_edit(todo, task="t revised")])
    assert flow.steps[0].steering == ["use SQLModel"]
    assert flow.steps[0].task == "t revised"


def test_steps_can_be_added_reordered_and_deleted():
    a = Step(role="backend", task="a", status="done")
    b = Step(role="tester", task="b")
    flow = Flow(steps=[a, b])
    fresh = Step(role="frontend", task="c")

    flow.apply_edits([fresh, _edit(b), _edit(a)])
    assert [s.task for s in flow.steps] == ["c", "b", "a"]

    flow.apply_edits([_edit(b)])
    assert [s.task for s in flow.steps] == ["b"]


# ------------------------------------------------------------ agent library

def test_library_seeds_from_builtins(tmp_path):
    from trance.agents.store import RoleStore

    store = RoleStore(tmp_path / "agents.json")
    assert {"backend", "frontend", "tester", "reviewer"} <= {r.name for r in store.all()}


def test_edits_persist_and_builtins_are_topped_up(tmp_path):
    """An edited built-in keeps its edit; a missing one is restored."""
    from trance.agents.store import RoleStore

    path = tmp_path / "agents.json"
    store = RoleStore(path)
    edited = store.get("backend")
    edited.paths = ["services/**"]
    edited.toolsets = ["files", "graph", "commands"]
    store.upsert(edited)

    reopened = RoleStore(path)
    assert reopened.get("backend").paths == ["services/**"]
    assert "commands" in reopened.get("backend").toolsets
    assert reopened.get("tester") is not None  # untouched builtin still there


def test_custom_agent_types_round_trip(tmp_path):
    from trance.agents.roles import AgentRole
    from trance.agents.store import RoleStore

    path = tmp_path / "agents.json"
    store = RoleStore(path)
    store.upsert(AgentRole(name="dba", title="DBA", description="schema work",
                           system_prompt="You own migrations.", paths=["migrations/**"],
                           toolsets=["files"], preset="cheap"))
    restored = RoleStore(path).get("dba")
    assert restored.paths == ["migrations/**"] and restored.preset == "cheap"


def test_builtins_cannot_be_deleted(tmp_path):
    from trance.agents.roles import AgentRole
    from trance.agents.store import RoleStore

    store = RoleStore(tmp_path / "agents.json")
    assert store.delete("backend") is False
    store.upsert(AgentRole(name="dba", title="D", description="", system_prompt=""))
    assert store.delete("dba") is True


def test_resolve_team_rebinds_to_the_library(tmp_path):
    """A session must never keep running a stale copy of an edited agent."""
    from trance.agents.roles import AgentRole
    from trance.agents.store import RoleStore

    store = RoleStore(tmp_path / "agents.json")
    stale = AgentRole(name="backend", title="old", description="", system_prompt="",
                      paths=["old/**"])
    (team,) = store.resolve_team([stale])
    assert team.paths != ["old/**"]        # library wins
    assert team is store.get("backend")


def test_resolve_team_accepts_names_and_drops_unknowns(tmp_path):
    from trance.agents.store import RoleStore

    store = RoleStore(tmp_path / "agents.json")
    team = store.resolve_team(["backend", "ghost", "tester", "backend"])
    assert [r.name for r in team] == ["backend", "tester"]  # deduped, unknown dropped


def test_validation_rejects_unusable_definitions():
    from trance.agents.store import validate

    assert validate({"name": "", "toolsets": []})
    assert validate({"name": "two words", "toolsets": []})
    assert validate({"name": "x", "toolsets": ["magic"]})
    # files without a remit means every write is refused — catch it at save time
    assert validate({"name": "x", "toolsets": ["files"], "paths": []})
    assert validate({"name": "x", "toolsets": ["files"], "paths": ["src/**"]}) is None
    assert validate({"name": "x", "toolsets": ["graph"]}) is None


# ------------------------------------------------- permissions in the prompt

def test_permissions_brief_matches_what_is_enforced():
    """The prompt text is generated from the enforcement constants, so it can
    only be wrong if the enforcement is wrong."""
    from trance.agents.tools import ALLOWED_COMMANDS, permissions_brief

    brief = permissions_brief(BUILTIN_ROLES["tester"])
    role = BUILTIN_ROLES["tester"]

    for glob in role.paths:
        assert glob in brief                      # every enforced glob is stated
    for command in ("pytest", "npm"):
        assert command in brief and command in ALLOWED_COMMANDS
    assert "REFUSED" in brief


def test_brief_states_the_absence_of_permissions_too():
    from trance.agents.tools import permissions_brief

    backend = permissions_brief(BUILTIN_ROLES["backend"])
    assert "may NOT run commands" in backend       # no `commands` toolset
    assert "pytest" not in backend                 # so no allowlist is advertised

    reviewer = permissions_brief(BUILTIN_ROLES["reviewer"])
    assert "may NOT write any file" in reviewer    # files toolset, empty remit

    planner = permissions_brief(BUILTIN_ROLES["planner"])
    assert "NO file access" in planner


def test_brief_reflects_a_custom_agents_actual_permissions():
    from trance.agents.tools import permissions_brief

    role = AgentRole(name="dba", title="DBA", description="", system_prompt="",
                     paths=["migrations/**"], toolsets=["files", "commands"])
    brief = permissions_brief(role)
    assert "migrations/**" in brief
    assert "may run commands" in brief
    assert "code graph" not in brief               # no graph toolset


def test_the_brief_is_in_every_agent_prompt(monkeypatch, tmp_path):
    """Regression guard: the permissions block must reach the model."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None):
            captured["messages"] = messages
            return ChatResponse(text="done", finish_reason="stop")

    monkeypatch.setattr(runner, "client_for", lambda config: FakeClient())
    runner.run_agent(
        role=BUILTIN_ROLES["backend"], task="build it", project=tmp_path,
        config=ModelConfig(), bus=EventBus(), session_id="s", step_id="st",
    )
    user_message = captured["messages"][1]["content"]
    assert "## Your permissions (enforced by the system)" in user_message
    assert "backend/**" in user_message
    assert "may NOT run commands" in user_message


def test_reset_restores_a_builtin(tmp_path):
    from trance.agents.store import RoleStore

    store = RoleStore(tmp_path / "agents.json")
    edited = store.get("backend")
    edited.system_prompt = "you do nothing"
    edited.paths = []
    store.upsert(edited)

    restored = store.reset("backend")
    assert restored.system_prompt == BUILTIN_ROLES["backend"].system_prompt
    assert restored.paths == BUILTIN_ROLES["backend"].paths
    assert RoleStore(tmp_path / "agents.json").get("backend").paths  # persisted

    assert store.reset("not-a-builtin") is None


def test_reset_does_not_mutate_the_shipped_definition(tmp_path):
    """A deepcopy bug here would corrupt the builtin for every later store."""
    from trance.agents.store import RoleStore

    store = RoleStore(tmp_path / "agents.json")
    role = store.reset("backend")
    role.paths.append("everything/**")
    assert "everything/**" not in BUILTIN_ROLES["backend"].paths


def test_editing_an_agent_never_mutates_the_shipped_default(tmp_path):
    """Regression: the store handed out the BUILTIN_ROLES object itself, so an
    edit rewrote the module-level default and reset() restored the edit."""
    from trance.agents.store import RoleStore

    before = list(BUILTIN_ROLES["backend"].paths)
    store = RoleStore(tmp_path / "agents.json")
    role = store.get("backend")
    role.paths = ["nonsense/**"]
    role.toolsets = []
    store.upsert(role)

    assert BUILTIN_ROLES["backend"].paths == before
    assert RoleStore(tmp_path / "other.json").get("backend").paths == before


# ------------------------------------------------------- console detail

def test_write_captures_a_diff_without_spending_model_context(project):
    """The diff is for the UI. Putting it in `text` would make the model
    re-read a diff of what it just wrote, on every single edit."""
    tools = _tools(project)
    tools.write_file("backend/calc.py", "def add(a, b):\n    return a + b\n")
    result = tools.write_file("backend/calc.py", "def add(a, b):\n    return a - b\n")

    assert result.detail["kind"] == "write"
    assert result.detail["created"] is False
    assert result.detail["added"] == 1 and result.detail["removed"] == 1
    assert "-    return a + b" in result.detail["diff"]
    assert "+    return a - b" in result.detail["diff"]
    assert "diff" not in result.text and "+++" not in result.text


def test_new_file_is_marked_created_with_a_full_add_diff(project):
    result = _tools(project).write_file("backend/new.py", "X = 1\nY = 2\n")
    assert result.detail["created"] is True
    assert result.detail["added"] == 2 and result.detail["removed"] == 0


def test_huge_diffs_are_truncated_for_display(project):
    from trance.agents.tools import MAX_DIFF_LINES

    tools = _tools(project)
    tools.write_file("backend/big.py", "")
    result = tools.write_file("backend/big.py", "\n".join(f"line {i}" for i in range(2000)))
    assert result.detail["truncated"] is True
    assert len(result.detail["diff"].splitlines()) <= MAX_DIFF_LINES


def test_command_detail_carries_exit_code_and_output(project):
    tools = _tools(project, "tester")
    ok = tools.run_command("python3 -c 'print(42)'")
    assert ok.detail == {"kind": "command", "command": "python3 -c 'print(42)'",
                         "exit_code": 0, "output": "42"}

    bad = tools.run_command("python3 -c 'import sys; sys.exit(3)'")
    assert bad.detail["exit_code"] == 3 and bad.ok is False


def test_refused_write_carries_no_diff(project):
    result = _tools(project).write_file("frontend/x.tsx", "nope")
    assert result.remit_violation and result.detail == {}


# ------------------------------------------------------- fact checker

def test_factchecker_cannot_write_or_run_anything(project):
    """The point of this role: a verifier that *can* write files will write
    files. This one structurally cannot."""
    tools = AgentTools(project, BUILTIN_ROLES["factchecker"])
    offered = {s["function"]["name"] for s in tools.specs()}
    assert offered == {"check_file", "check_files", "list_files"}
    assert "write_file" not in offered and "run_command" not in offered
    assert "read_file" not in offered          # no contents, so no cheap way to review

    # Refused even if the model invents the tool name.
    assert not tools.call("write_file", {"path": "a.py", "content": "x"}).ok
    assert not tools.call("run_command", {"command": "pytest"}).ok
    assert not tools.call("read_file", {"path": "backend/main.py"}).ok
    assert not (project / "a.py").exists()


def test_check_file_reports_presence_and_emptiness(project):
    tools = AgentTools(project, BUILTIN_ROLES["factchecker"])
    (project / "backend" / "empty.py").write_text("")
    (project / "backend" / "blank.py").write_text("\n   \n")

    assert "has content" in tools.check_file("backend/main.py").text
    assert "EMPTY" in tools.check_file("backend/empty.py").text
    assert "EMPTY" in tools.check_file("backend/blank.py").text   # whitespace only
    assert "MISSING" in tools.check_file("backend/nope.py").text


def test_check_file_never_returns_contents(project):
    (project / "backend" / "secret.py").write_text("API_KEY = 'sk-do-not-leak'\n")
    result = AgentTools(project, BUILTIN_ROLES["factchecker"]).check_file("backend/secret.py")
    assert "sk-do-not-leak" not in result.text
    assert result.detail["files"][0]["size_bytes"] == len("API_KEY = 'sk-do-not-leak'\n")


def test_check_files_batches(project):
    tools = AgentTools(project, BUILTIN_ROLES["factchecker"])
    result = tools.check_files(["backend/main.py", "backend/gone.py"])
    assert "has content" in result.text and "MISSING" in result.text
    assert [f["path"] for f in result.detail["files"]] == ["backend/main.py", "backend/gone.py"]


def test_check_file_cannot_escape_the_project(project):
    result = AgentTools(project, BUILTIN_ROLES["factchecker"]).check_file("../../etc/passwd")
    assert "outside the project" in result.text


def test_an_inspect_only_agent_is_valid_without_a_remit():
    from trance.agents.store import validate

    assert validate({"name": "fc", "toolsets": ["inspect"], "paths": []}) is None


# ---------------------------------------------------------- verifiers

def test_only_inspection_capable_agents_are_verifiers():
    verifiers = {n for n, r in BUILTIN_ROLES.items() if r.verifier}
    assert verifiers == {"tester", "reviewer", "factchecker"}
    # The orchestrator has no toolsets at all — it could only ever guess.
    assert BUILTIN_ROLES["orchestrator"].verifier is False
    assert BUILTIN_ROLES["backend"].verifier is False


def test_a_non_verifier_is_not_asked_to_verify(tmp_path, monkeypatch):
    """Regression: any role could be named as a verifier, including the
    tool-less orchestrator, which would emit a verdict it never checked."""
    from trance.config import Config
    from trance.engine import FlowEngine
    from trance.events import EventBus
    from trance.flow import Attempt, Step
    from trance.session import Session

    session = Session(name="s", project_dir=str(tmp_path))
    session.team = [BUILTIN_ROLES["backend"], BUILTIN_ROLES["orchestrator"]]
    step = Step(role="backend", task="t", verify_with="orchestrator")

    bus = EventBus()
    seen = []
    bus.subscribe_sync(seen.append)
    engine = FlowEngine(session, Config.load(tmp_path / "none.toml"), bus)

    called = []
    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: called.append(kw))

    verdict = engine._verify(step, Attempt(n=1))
    assert verdict == "UNKNOWN"          # unverified, so the step is blocked
    assert not called                    # the agent was never run
    assert any("not marked as a verifier" in (e.payload.get("message") or "")
               for e in seen if e.type == "warning")


def test_a_real_verifier_is_still_invoked(tmp_path, monkeypatch):
    from trance.config import Config
    from trance.engine import FlowEngine
    from trance.events import EventBus
    from trance.flow import Attempt, Step
    from trance.session import Session

    session = Session(name="s", project_dir=str(tmp_path))
    session.team = [BUILTIN_ROLES["frontend"], BUILTIN_ROLES["factchecker"]]
    step = Step(role="frontend", task="t", verify_with="factchecker")
    engine = FlowEngine(session, Config.load(tmp_path / "none.toml"), EventBus())

    class Turn:
        verdict = "PASS"
        text = "VERDICT: PASS"
        model_event_ids = ["ev"]

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: Turn())
    assert engine._verify(step, Attempt(n=1)) == "PASS"


# ------------------------------------------- malformed tool arguments

def test_unparsable_arguments_are_not_reported_as_empty():
    """Regression: a tool call truncated at max_tokens arrived as `{}`, so the
    agent was told it forgot 'path' and 'content' rather than that its output
    was cut off."""
    from trance.worker.client import _parse

    response = _parse({"choices": [{"finish_reason": "length", "message": {"tool_calls": [
        {"id": "1", "function": {"name": "write_file",
                                 "arguments": '{"path": "a.html", "content": "<html'}}]}}]})
    call = response.tool_calls[0]
    assert call.malformed is True
    assert call.arguments == {}
    assert call.raw_arguments.startswith('{"path"')   # kept for diagnosis
    assert response.finish_reason == "length"


def test_wellformed_arguments_are_not_flagged():
    from trance.worker.client import _parse

    response = _parse({"choices": [{"message": {"tool_calls": [
        {"id": "1", "function": {"name": "write_file",
                                 "arguments": '{"path": "a.py", "content": "x"}'}}]}}]})
    assert response.tool_calls[0].malformed is False
    assert response.tool_calls[0].arguments == {"path": "a.py", "content": "x"}


def test_truncated_call_tells_the_agent_what_actually_happened():
    from trance.agents.runner import _malformed_call_outcome
    from trance.providers.base import ToolCall

    call = ToolCall(id="1", name="write_file", arguments={}, malformed=True)
    truncated = _malformed_call_outcome(call, truncated=True, max_tokens=4096).text
    assert "cut off" in truncated and "4096" in truncated
    assert "Do not retry the same call" in truncated
    assert "smaller pieces" in truncated

    invalid = _malformed_call_outcome(call, truncated=False, max_tokens=4096).text
    assert "not valid JSON" in invalid


def test_bad_arguments_get_a_schema_error_not_a_python_traceback(project):
    result = _tools(project, "frontend").call("write_file", {})
    assert "missing required argument(s): path, content" in result.text
    assert "Expected arguments:" in result.text
    assert "positional" not in result.text     # no leaked Python signature

    wrong = _tools(project, "frontend").call("write_file", {"file": "a.js", "body": "x"})
    assert "unexpected argument(s): file, body" in wrong.text


def test_a_step_without_a_verifier_says_so(tmp_path):
    """Silence read as 'verified and passed'."""
    from trance.config import Config
    from trance.engine import FlowEngine
    from trance.events import EventBus
    from trance.flow import Attempt, Step
    from trance.session import Session

    bus = EventBus()
    seen = []
    bus.subscribe_sync(seen.append)
    engine = FlowEngine(Session(name="s", project_dir=str(tmp_path)),
                        Config.load(tmp_path / "none.toml"), bus)

    assert engine._verify(Step(role="backend", task="t"), Attempt(n=1)) is None
    assert any(e.type == "verification_skipped" for e in seen)


# ------------------------------------------------ per-agent commands

def test_write_file_creates_parent_directories(project):
    """The tester reached for mkdir because nothing told it this happens."""
    result = _tools(project).write_file("backend/deep/nested/new.py", "X = 1\n")
    assert result.ok
    assert (project / "backend" / "deep" / "nested" / "new.py").read_text() == "X = 1\n"


def test_agents_are_told_they_do_not_need_mkdir(project):
    from trance.agents.tools import permissions_brief

    brief = permissions_brief(BUILTIN_ROLES["tester"])
    assert "never need to create a folder before writing" in brief
    spec = next(s for s in _tools(project, "tester").specs()
                if s["function"]["name"] == "write_file")
    assert "created automatically" in spec["function"]["description"]


def test_a_role_can_narrow_its_own_allowlist(project):
    role = AgentRole(name="narrow", title="N", description="", system_prompt="",
                     toolsets=["files", "commands"], paths=["tests/**"], commands=["pytest"])
    tools = AgentTools(project, role)
    assert tools.allowed_commands == {"pytest"}
    refused = tools.run_command("python3 -c 'print(1)'")
    assert not refused.ok and "not an allowed program for this agent" in refused.text


def test_a_role_can_extend_its_allowlist(project):
    role = AgentRole(name="wide", title="W", description="", system_prompt="",
                     toolsets=["commands"], commands=["python3", "git"])
    assert AgentTools(project, role).run_command("python3 -c 'print(7)'").ok


def test_commands_can_be_pinned_to_a_subdirectory(project):
    (project / "tests").mkdir()
    role = AgentRole(name="pinned", title="P", description="", system_prompt="",
                     toolsets=["commands"], commands=["pwd"], workdir="tests")
    result = AgentTools(project, role).run_command("pwd")
    assert result.ok and result.detail["output"].endswith("/tests")


def test_a_workdir_cannot_escape_the_project(project):
    role = AgentRole(name="escape", title="E", description="", system_prompt="",
                     toolsets=["commands"], commands=["pwd"], workdir="../../")
    tools = AgentTools(project, role)
    assert tools.command_cwd == project          # falls back, never escapes


def test_the_refusal_message_points_at_write_file(project):
    role = AgentRole(name="r", title="R", description="", system_prompt="",
                     toolsets=["commands"], commands=["pytest"])
    refused = AgentTools(project, role).run_command("mkdir newdir")
    assert "write_file creates parent directories" in refused.text
