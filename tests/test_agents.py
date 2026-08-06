"""Tests for the multi-agent layer: remits, tools, config resolution, salvage."""

from pathlib import Path

import copy

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
    refused = tools.run_command("curl http://example.com")
    assert not refused.ok and "allowlist" in refused.text
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
    fitted, dropped = fit_context(messages, budget=12000)
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
    edited = Step(role=step.role, task=step.task, check=step.checker,
                  on_fail=step.on_fail, max_loops=step.loop_limit, entry=step.entry)
    edited.id = step.id
    for key, value in changes.items():
        setattr(edited, key, value)
    return edited


def test_editing_a_failed_step_requeues_it():
    """A plan you cannot correct after it failed is not much use."""
    failed = Step(role="frontend", task="build it", status="failed",
                  check="tester", attempts=[Attempt(n=1, verdict="FAIL")])
    flow = Flow(steps=[failed])

    outcome = flow.apply_edits([_edit(failed, check="factchecker")])
    assert outcome["requeued"] == [failed.id]
    assert flow.steps[0].status == "pending"
    assert flow.steps[0].checker == "factchecker"
    assert flow.steps[0].attempts == []       # a re-queued step starts clean


def test_editing_a_finished_step_requeues_it_too():
    done = Step(role="backend", task="old task", status="done")
    flow = Flow(steps=[done])
    flow.apply_edits([_edit(done, task="new task")])
    assert flow.steps[0].status == "pending" and flow.steps[0].task == "new task"


def test_a_cosmetic_edit_does_not_requeue():
    done = Step(role="backend", task="t", status="done", max_loops=2)
    flow = Flow(steps=[done])
    outcome = flow.apply_edits([_edit(done, max_loops=5)])
    assert outcome["requeued"] == []
    assert flow.steps[0].status == "done" and flow.steps[0].loop_limit == 5


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
        def complete(self, messages, tools=None, **kwargs):
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
    assert ok.detail["kind"] == "command" and ok.detail["exit_code"] == 0
    assert ok.detail["output"] == "42"

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
    assert not refused.ok and "allowlist" in refused.text


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


def test_the_refusal_message_lists_what_is_allowed(project):
    role = AgentRole(name="r", title="R", description="", system_prompt="",
                     toolsets=["commands"], commands=["pytest"])
    refused = AgentTools(project, role).run_command("mkdir newdir")
    assert "'mkdir'" in refused.text and "Allowed: pytest" in refused.text


# ------------------------------------------- block loop: check + fixer

def _engine(tmp_path, team, bus=None):
    from trance.config import Config
    from trance.engine import FlowEngine
    from trance.events import EventBus
    from trance.session import Session

    session = Session(name="s", project_dir=str(tmp_path))
    session.team = [BUILTIN_ROLES[n] for n in team]
    return FlowEngine(session, Config.load(tmp_path / "none.toml"), bus or EventBus())


class _Turn:
    """Stands in for agents.runner.AgentTurn."""

    def __init__(self, verdict, text="", outcome=("SUCCESS", ""), transcript=None):
        self.verdict = verdict
        self.transcript = transcript or []
        self.outcome = outcome
        self.reported_outcome = True
        self.text = text or f"VERDICT: {verdict}"
        self.model_event_ids = ["ev"]
        self.files_written = []
        self.remit_violations = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.tool_calls = 0
        self.rounds = 1
        self.stop_reason = "stop"
        self.salvaged_calls = 0
        self.truncated_calls = 0


def test_a_passing_check_lets_the_flow_move_on(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker"])
    step = Step(role="backend", task="t", check="factchecker")
    order = []
    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: (order.append(kw["role"].name),
                                      _Turn("PASS" if kw["role"].name == "factchecker" else None))[1])
    engine._execute(step)
    assert order == ["backend", "factchecker"]
    assert step.status == "done"
    assert not engine.session.stopping          # the flow continues


def test_a_failed_outcome_sends_the_work_to_the_fixer_then_loops(tmp_path, monkeypatch):
    """The step's own outcome opens the loop — a tester that finds a real bug
    did good work and the step still failed."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker", "reviewer"])
    step = Step(role="backend", task="build it", check="factchecker",
                on_fail="reviewer", max_loops=3)
    order, prompts = [], {}

    def fake(**kw):
        name = kw["role"].name
        order.append(name)
        prompts[name] = kw["task"]
        if name == "factchecker":
            return _Turn("PASS")                      # the report is honest
        if name == "backend":
            failing = order.count("backend") == 1
            return _Turn(None, "attempted",
                         outcome=("FAILED", "divide() raises on zero") if failing
                         else ("SUCCESS", ""))
        return _Turn(None, "fixed")

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert order == ["backend", "factchecker", "reviewer", "backend", "factchecker"]
    assert step.status == "done"
    assert "divide() raises on zero" in prompts["reviewer"]


def test_a_dishonest_report_halts_instead_of_looping(tmp_path, monkeypatch):
    """Claimed success + the check disagreeing means the report cannot be
    trusted; looping would just repeat it."""
    from trance.events import EventBus
    from trance.flow import Step

    bus = EventBus()
    seen = []
    bus.subscribe_sync(seen.append)
    engine = _engine(tmp_path, ["backend", "factchecker", "reviewer"], bus)
    step = Step(role="backend", task="t", check="factchecker",
                on_fail="reviewer", max_loops=3)
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        if kw["role"].name == "factchecker":
            return _Turn("FAIL", "index.html is MISSING")
        return _Turn(None, "all done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert order == ["backend", "factchecker"]      # no fixer, no second loop
    assert step.status == "failed"
    assert engine.session.stopping
    halted = [e for e in seen if e.type == "run_halted"]
    assert halted and halted[0].payload["lied"] is True
    assert "not actually done" in halted[0].payload["message"]


def test_an_admitted_failure_loops_even_if_the_check_also_fails(tmp_path, monkeypatch):
    """It did not claim success, so there is no lie — just work to redo."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker"])
    step = Step(role="backend", task="t", check="factchecker", max_loops=2)
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        if kw["role"].name == "factchecker":
            return _Turn("FAIL", "nothing on disk")
        first = order.count("backend") == 1
        return _Turn(None, "tried",
                     outcome=("FAILED", "could not write") if first else ("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)
    assert order == ["backend", "factchecker", "backend", "factchecker"]
    assert step.status == "failed"      # second pass claimed success, check says no
    assert engine.session.stopping


def test_without_a_fixer_the_block_simply_runs_again(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])
    step = Step(role="backend", task="t", max_loops=2)
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        first = order.count("backend") == 1
        return _Turn(None, "x",
                     outcome=("FAILED", "not yet") if first else ("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)
    assert order == ["backend", "backend"]
    assert step.status == "done"


def test_exhausting_the_loop_limit_halts_the_whole_flow(tmp_path, monkeypatch):
    """The loop can only be left by the step reporting success."""
    from trance.events import EventBus
    from trance.flow import Step

    bus = EventBus()
    seen = []
    bus.subscribe_sync(seen.append)
    engine = _engine(tmp_path, ["backend", "reviewer"], bus)
    step = Step(role="backend", task="t", on_fail="reviewer", max_loops=2)

    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: _Turn(None, "tried",
                                           outcome=("FAILED", "still broken")))
    engine._execute(step)

    assert step.status == "failed"
    assert len(step.attempts) == 2
    assert engine.session.stopping and engine.session.status == "error"
    halted = [e for e in seen if e.type == "run_halted"]
    assert halted and halted[0].payload["lied"] is False


def test_no_fixer_runs_on_the_final_loop(tmp_path, monkeypatch):
    """Fixing after the last check would be work nothing ever verifies."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker", "reviewer"])
    step = Step(role="backend", task="t", check="factchecker",
                on_fail="reviewer", max_loops=1)
    order = []
    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: (order.append(kw["role"].name),
                                      _Turn("FAIL", "no") if kw["role"].name == "factchecker"
                                      else _Turn(None, "x"))[1])
    engine._execute(step)
    assert order == ["backend", "factchecker"]           # reviewer never ran
    assert step.status == "failed"


def test_a_block_without_a_check_just_runs_once(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])
    step = Step(role="backend", task="t")
    order = []
    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: (order.append(kw["role"].name), _Turn(None, "done"))[1])
    engine._execute(step)
    assert order == ["backend"] and step.status == "done"


def test_legacy_verify_with_is_read_as_the_check(tmp_path, monkeypatch):
    from trance.flow import Step

    step = Step.from_dict({"role": "backend", "task": "t", "verify_with": "tester"})
    assert step.checker == "tester" and step.fixer == "backend"

    engine = _engine(tmp_path, ["backend", "tester"])
    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: _Turn("PASS") if kw["role"].name == "tester"
                        else _Turn(None, "d"))
    engine._execute(step)
    assert step.status == "done"


# ------------------------------------------- orchestrator proposals

def _roles():
    return list(BUILTIN_ROLES.values())


def test_the_proposal_schema_only_offers_real_verifiers():
    """Regression: verify_with was a free string, so the orchestrator could —
    and did — propose itself as the verifier."""
    from trance.agents.orchestrator import propose_flow_tool

    props = (propose_flow_tool(_roles())["function"]["parameters"]
             ["properties"]["steps"]["items"]["properties"])
    assert set(props["check"]["enum"]) == {"tester", "reviewer", "factchecker"}
    assert "orchestrator" not in props["role"]["enum"]     # it assigns work, not does it
    assert "orchestrator" not in props["on_fail"]["enum"]


def test_the_schema_follows_the_live_library():
    from trance.agents.orchestrator import propose_flow_tool

    custom = AgentRole(name="auditor", title="Auditor", description="", system_prompt="",
                       toolsets=["inspect"], verifier=True)
    plain = AgentRole(name="dba", title="DBA", description="", system_prompt="",
                      toolsets=["files"], paths=["migrations/**"])
    props = (propose_flow_tool([*_roles(), custom, plain])["function"]["parameters"]
             ["properties"]["steps"]["items"]["properties"])
    assert "auditor" in props["check"]["enum"]        # custom verifier offered
    assert "dba" not in props["check"]["enum"]        # cannot verify
    assert "dba" in props["role"]["enum"]             # but can be assigned work
    assert "dba" in props["on_fail"]["enum"]          # and can be a fixer


def test_a_proposal_naming_a_non_verifier_as_the_check_drops_it():
    from trance.agents.orchestrator import _normalize

    out = _normalize({"summary": "s", "team": ["backend"], "steps": [
        {"role": "backend", "task": "t", "check": "orchestrator"},
    ]}, _roles())
    assert out["steps"][0]["check"] is None
    assert any("orchestrator" in d for d in out["dropped_checks"])


def test_a_fixer_without_a_check_is_dropped():
    from trance.agents.orchestrator import _normalize

    out = _normalize({"summary": "s", "team": [], "steps": [
        {"role": "backend", "task": "t", "on_fail": "reviewer"},
    ]}, _roles())
    assert out["steps"][0]["on_fail"] is None      # nothing could ever fail


def test_the_orchestrator_cannot_assign_work_to_itself():
    from trance.agents.orchestrator import _normalize

    out = _normalize({"summary": "s", "team": ["orchestrator", "backend"], "steps": [
        {"role": "orchestrator", "task": "do it myself"},
        {"role": "backend", "task": "real work"},
    ]}, _roles())
    assert [s["role"] for s in out["steps"]] == ["backend"]
    assert "orchestrator" not in out["team"]


def test_legacy_verify_with_in_a_proposal_becomes_the_check():
    from trance.agents.orchestrator import _normalize

    out = _normalize({"summary": "s", "team": [], "steps": [
        {"role": "backend", "task": "t", "verify_with": "tester"},
    ]}, _roles())
    assert out["steps"][0]["check"] == "tester"


def test_max_loops_is_clamped():
    from trance.agents.orchestrator import _normalize

    out = _normalize({"summary": "s", "team": [], "steps": [
        {"role": "backend", "task": "t", "check": "tester", "max_loops": 99},
    ]}, _roles())
    assert out["steps"][0]["max_loops"] == 4


def test_check_and_fixer_are_pulled_onto_the_team():
    from trance.agents.orchestrator import _normalize

    out = _normalize({"summary": "s", "team": ["backend"], "steps": [
        {"role": "backend", "task": "t", "check": "factchecker", "on_fail": "reviewer"},
    ]}, _roles())
    assert set(out["team"]) == {"backend", "factchecker", "reviewer"}



# --------------------------------------------- reasoning and truncation

def test_inline_reasoning_never_reaches_the_user():
    """Regression: the orchestrator's chain of thought was shown as its answer."""
    from trance.worker.client import split_reasoning

    visible, reasoning = split_reasoning(
        "<think>The user wants a game. Let me plan.</think>Here is the plan.")
    assert visible == "Here is the plan."
    assert "Let me plan" in reasoning


def test_an_unclosed_think_tag_means_it_was_cut_off():
    from trance.worker.client import split_reasoning

    visible, reasoning = split_reasoning("<think>Wait, the tool allows specifying")
    assert visible == ""                     # better nothing than half a thought
    assert "Wait, the tool allows" in reasoning


def test_plain_answers_are_untouched():
    from trance.worker.client import split_reasoning

    assert split_reasoning("Just an answer.") == ("Just an answer.", "")
    assert split_reasoning("") == ("", "")


def test_parse_routes_inline_reasoning_out_of_the_text():
    from trance.worker.client import _parse

    r = _parse({"choices": [{"message": {
        "content": "<thinking>hmm</thinking>The answer.",
        "reasoning_content": "channelled"}}]})
    assert r.text == "The answer."
    assert "hmm" in r.reasoning and "channelled" in r.reasoning


def test_a_truncated_orchestrator_reply_says_so(monkeypatch, tmp_path):
    from trance.agents import orchestrator
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse, ToolCall

    class Fake:
        def complete(self, messages, tools=None, **kwargs):
            return ChatResponse(
                text="", finish_reason="length",
                tool_calls=[ToolCall(id="1", name="propose_flow", arguments={},
                                     malformed=True)])

    monkeypatch.setattr(orchestrator, "client_for", lambda config: Fake())
    result = orchestrator.chat(messages=[{"role": "user", "content": "build it"}],
                               project_dir=tmp_path, config=ModelConfig(max_tokens=2048),
                               bus=EventBus(), session_id="s")
    assert result["truncated"] is True
    assert result["proposal"] is None
    assert "cut off" in result["text"] and "2048" in result["text"]
    assert "max_tokens" in result["text"]


def test_an_empty_reply_is_reported_rather_than_shown_blank(monkeypatch, tmp_path):
    from trance.agents import orchestrator
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    monkeypatch.setattr(orchestrator, "client_for",
                        lambda config: type("F", (), {
                            "complete": lambda self, m, tools=None: ChatResponse(text="")})())
    result = orchestrator.chat(messages=[{"role": "user", "content": "hi"}],
                               project_dir=tmp_path, config=ModelConfig(),
                               bus=EventBus(), session_id="s")
    assert "did not produce a reply" in result["text"]


# ---------------------------------------------------- command hygiene

def test_shell_syntax_works_when_the_agent_has_a_shell(project):
    """The tester was rejected for pipes and redirects it reasonably expected."""
    result = _tools(project, "tester").run_command(
        'ls -la backend/ 2>/dev/null || echo "missing"')
    assert result.detail["kind"] == "command"      # it ran
    assert result.detail["shell"] is True


def test_shell_syntax_is_refused_when_the_agent_has_no_shell(project):
    role = AgentRole(name="plain", title="P", description="", system_prompt="",
                     toolsets=["commands"], commands=["ls", "echo"], shell=False)
    result = AgentTools(project, role).run_command('ls a || echo b')
    assert not result.ok and "not through a shell" in result.text
    assert result.detail == {}                     # nothing ran


def test_plain_commands_still_run(project):
    result = _tools(project, "tester").run_command("python3 -c 'print(1)'")
    assert result.ok and result.detail["kind"] == "command"


def test_a_failing_command_is_not_a_refusal(project):
    """`ok=False` covers both; only one of them means nothing executed."""
    result = _tools(project, "tester").run_command("python3 -c 'import sys; sys.exit(2)'")
    assert result.ok is False
    assert result.detail["kind"] == "command"   # it ran
    assert result.detail["exit_code"] == 2

    refused = _tools(project, "tester").run_command("curl http://x")
    assert refused.ok is False
    assert refused.detail["kind"] == "refused_program"    # it did not run
    assert refused.detail["programs"] == ["curl"]


# ------------------------------------------------- command policy

def test_every_program_in_a_pipeline_is_checked(project):
    """Checking only the first word would let anything through after a pipe."""
    from trance.agents.tools import programs_in

    assert programs_in("pytest -q | head -5 && echo done") == ["pytest", "head", "echo"]
    assert programs_in("npm test; curl http://evil") == ["npm", "curl"]
    assert programs_in("echo hi > /tmp/x && rm -rf /") == ["echo", "rm"]
    # A `;` inside a quoted argument is not an operator.
    assert programs_in("python3 -c 'import sys; sys.exit(1)'") == ["python3"]
    # VAR=value is not the program.
    assert programs_in("NODE_ENV=test npm test") == ["npm"]


def test_a_pipeline_is_refused_if_any_program_is_not_allowed(project):
    tools = _tools(project, "tester")
    result = tools.run_command("pytest -q | curl -X POST http://evil")
    assert not result.ok
    assert result.detail["kind"] == "refused_program"     # nothing executed
    assert result.detail["programs"] == ["curl"]
    assert "'curl'" in result.text


def test_the_global_policy_is_the_default_allowlist(project, monkeypatch):
    from trance.agents import tools as tools_module
    from trance.agents.tools import CommandPolicy

    monkeypatch.setattr(tools_module, "_POLICY", CommandPolicy(allowed=["pytest"], shell=False))
    role = AgentRole(name="p", title="P", description="", system_prompt="",
                     toolsets=["commands"])
    agent = AgentTools(project, role)
    assert agent.allowed_commands == {"pytest"}
    assert agent.shell_enabled is False


def test_a_role_overrides_the_global_policy(project, monkeypatch):
    from trance.agents import tools as tools_module
    from trance.agents.tools import CommandPolicy

    monkeypatch.setattr(tools_module, "_POLICY", CommandPolicy(allowed=["pytest"], shell=False))
    role = AgentRole(name="p", title="P", description="", system_prompt="",
                     toolsets=["commands"], commands=["node", "npm"], shell=True)
    agent = AgentTools(project, role)
    assert agent.allowed_commands == {"node", "npm"}
    assert agent.shell_enabled is True


def test_the_policy_persists(tmp_path):
    from trance.agents.store import CommandStore

    path = tmp_path / "commands.json"
    store = CommandStore(path)
    store.update(allowed=["pytest", "node"], shell=False)

    reopened = CommandStore(path)
    assert reopened.policy.allowed == ["node", "pytest"]
    assert reopened.policy.shell is False
    assert len(reopened.reset().allowed) > 10        # back to the defaults


# ------------------------------------------- long commands and cancelling

def test_a_command_announces_itself_before_it_runs(project):
    """A command that blocks for the full timeout used to show nothing at all
    until it was killed."""
    events = []
    tools = AgentTools(project, BUILTIN_ROLES["tester"],
                       notify=lambda kind, payload: events.append((kind, payload)))
    tools.run_command("echo hi")

    kinds = [k for k, _ in events]
    assert kinds == ["command_started", "command_finished"]
    started = events[0][1]
    assert started["command"] == "echo hi" and started["command_id"]
    assert events[1][1]["command_id"] == started["command_id"]
    assert events[1][1]["exit_code"] == 0


def test_a_running_command_can_be_cancelled(project):
    import threading
    import time

    from trance.agents.tools import cancel_command, running_commands

    ids = []
    tools = AgentTools(project, BUILTIN_ROLES["tester"],
                       notify=lambda kind, p: ids.append(p["command_id"])
                       if kind == "command_started" else None)

    result = {}

    def run():
        result["outcome"] = tools.run_command("python3 -c 'import time; time.sleep(30)'")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    for _ in range(50):                     # wait for it to register
        if ids and running_commands():
            break
        time.sleep(0.1)

    assert cancel_command(ids[0]) is True
    worker.join(timeout=15)
    outcome = result["outcome"]
    assert outcome.ok is False
    assert outcome.detail["cancelled"] is True
    assert "Cancelled by the user" in outcome.text
    assert not running_commands()           # deregistered either way


def test_cancelling_an_unknown_command_is_harmless():
    from trance.agents.tools import cancel_command

    assert cancel_command("cmd_nope") is False


def test_a_timeout_points_at_background_mode(project, monkeypatch):
    from trance.agents import tools as tools_module

    monkeypatch.setattr(tools_module, "COMMAND_TIMEOUT_S", 1)
    result = AgentTools(project, BUILTIN_ROLES["tester"]).run_command(
        "python3 -c 'import time; time.sleep(30)'")
    assert result.ok is False
    assert result.detail["timed_out"] is True
    assert "background=true" in result.text


def test_a_refusal_names_the_programs_for_the_ui(project):
    result = _tools(project, "tester").run_command("curl -s http://x | jq .")
    assert result.detail["kind"] == "refused_program"
    assert result.detail["programs"] == ["curl", "jq"]
    assert result.detail["agent"] == "tester"
    assert result.detail["agent_has_own_list"] is False   # so the global list applies


def test_a_trailing_ampersand_runs_in_the_background(project):
    """`node server.js &` used to block for the full timeout: the shell exited
    at once while the real process held our output pipe."""
    import time

    from trance.agents.tools import background_commands, stop_background

    started = time.time()
    result = AgentTools(project, BUILTIN_ROLES["tester"]).run_command(
        "python3 -m http.server 0 --bind 127.0.0.1 &")
    try:
        assert time.time() - started < 10          # returns at once
        assert result.ok is True
        assert result.detail["kind"] == "background"
        assert result.detail["command_id"]
        assert "stop_command" in result.text
        assert background_commands()
    finally:
        stop_background()


def test_a_background_command_that_dies_at_once_is_reported_as_failed(project):
    from trance.agents.tools import background_commands

    result = AgentTools(project, BUILTIN_ROLES["tester"]).run_command(
        "python3 -c 'import sys; sys.exit(3)'", background=True)
    assert result.ok is False
    assert "exited immediately" in result.text
    assert not background_commands()


def test_a_background_process_can_be_cancelled_after_its_shell_exits(project):
    """The tracked process is bash, which returns immediately — cancelling has
    to target the group, or nothing happens and the UI hangs on 'cancelling'."""
    from trance.agents.tools import background_commands, cancel_command

    tools = AgentTools(project, BUILTIN_ROLES["tester"])
    result = tools.run_command("python3 -c 'import time; time.sleep(120)' &")
    command_id = result.detail["command_id"]
    assert cancel_command(command_id) is True
    assert not background_commands()


def test_background_processes_are_stopped_when_a_step_ends(project):
    from trance.agents.tools import background_commands, stop_background

    tools = AgentTools(project, BUILTIN_ROLES["tester"])
    tools.run_command("python3 -c 'import time; time.sleep(120)' &")
    assert background_commands()
    assert len(stop_background()) == 1        # would otherwise hold its port
    assert not background_commands()


def test_cancel_kills_grandchildren_too(project):
    import threading
    import time

    from trance.agents.tools import cancel_command, running_commands

    ids = []
    tools = AgentTools(project, BUILTIN_ROLES["tester"],
                       notify=lambda k, p: ids.append(p["command_id"])
                       if k == "command_started" else None)
    out = {}
    worker = threading.Thread(
        target=lambda: out.update(r=tools.run_command(
            "python3 -c 'import subprocess,time; "
            "subprocess.Popen([\"sleep\",\"60\"]); time.sleep(60)'")),
        daemon=True)
    worker.start()
    for _ in range(60):
        if ids and running_commands():
            break
        time.sleep(0.1)

    assert cancel_command(ids[0]) is True
    worker.join(timeout=20)
    assert "r" in out and out["r"].detail["cancelled"] is True


# ------------------------------------------------- outcome vs integrity

def test_outcome_is_read_from_the_agents_own_last_line():
    from trance.agents.runner import AgentTurn

    assert AgentTurn(text="done\nOUTCOME: SUCCESS").outcome == ("SUCCESS", "")
    outcome, reason = AgentTurn(text="ran it\nOUTCOME: FAILED — 2 tests failed").outcome
    assert outcome == "FAILED" and "2 tests failed" in reason


def test_an_agent_that_states_nothing_is_not_counted_as_success():
    """A tester described a real defect, stopped mid-thought, and the step was
    marked done purely because nothing said otherwise."""
    from trance.agents.runner import AgentTurn

    turn = AgentTurn(text="I've written a test file. Now I need to actually run it.")
    outcome, reason = turn.outcome
    assert outcome == "UNSTATED"
    assert "without stating an outcome" in reason
    assert turn.reported_outcome is False


def test_the_last_outcome_line_wins():
    from trance.agents.runner import AgentTurn

    text = "first attempt\nOUTCOME: FAILED — nope\nthen I fixed it\nOUTCOME: SUCCESS"
    assert AgentTurn(text=text).outcome == ("SUCCESS", "")


def test_working_agents_are_told_to_report_an_outcome():
    for name in ("backend", "frontend", "tester"):
        assert "OUTCOME: SUCCESS" in BUILTIN_ROLES[name].system_prompt
        assert "OUTCOME: FAILED" in BUILTIN_ROLES[name].system_prompt


def test_the_tester_is_told_a_caught_bug_is_a_failed_step():
    prompt = BUILTIN_ROLES["tester"].system_prompt
    assert "good work AND a failed step" in prompt
    assert "VERDICT: PASS" in prompt          # still usable as a check


def test_the_factchecker_checks_truthfulness_not_quality():
    prompt = BUILTIN_ROLES["factchecker"].system_prompt
    assert "report of its own work is TRUE" in prompt
    assert "not a reviewer" in prompt
    assert "the whole run stops" in prompt     # it knows the weight of a FAIL


def test_an_unstated_outcome_opens_the_loop(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "reviewer"])
    step = Step(role="backend", task="t", on_fail="reviewer", max_loops=2)
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        first = order.count("backend") == 1
        return _Turn(None, "x", outcome=("UNSTATED", "never said") if first
                     else ("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)
    assert order == ["backend", "reviewer", "backend"]   # it did not simply pass
    assert step.status == "done"


# ------------------------------------- XML-shaped tool calls (Qwen/Hermes)

def test_xml_tool_calls_are_recovered():
    """A tester emitted <function=run_command> as text, so the command never
    ran and the step was marked done anyway."""
    from trance.worker.client import salvage_tool_calls

    text = ("I've written a comprehensive test file. Now I need to actually run it.\n\n"
            "<tool_call>\n<function=run_command>\n<parameter=command>\n"
            "python3 tests/test_server_load.py\n</parameter>\n</function>\n</tool_call>")
    (call,) = salvage_tool_calls(text, {"run_command", "write_file"})
    assert call.name == "run_command"
    assert call.arguments == {"command": "python3 tests/test_server_load.py"}


def test_xml_salvage_handles_several_parameters():
    from trance.worker.client import salvage_tool_calls

    text = ("<function=write_file><parameter=path>a.py</parameter>"
            "<parameter=content>X = 1</parameter></function>")
    (call,) = salvage_tool_calls(text, {"write_file"})
    assert call.arguments == {"path": "a.py", "content": "X = 1"}


def test_xml_salvage_refuses_tools_the_role_lacks():
    from trance.worker.client import salvage_tool_calls

    assert salvage_tool_calls(
        "<function=run_command><parameter=command>rm -rf /</parameter></function>",
        {"write_file"}) == []


def test_json_salvage_still_works_alongside_xml():
    from trance.worker.client import salvage_tool_calls

    (call,) = salvage_tool_calls(
        '{"name": "write_file", "arguments": {"path": "a.py", "content": "x"}}',
        {"write_file"})
    assert call.name == "write_file" and call.arguments["path"] == "a.py"


def test_the_tester_is_forbidden_from_weakening_tests():
    prompt = BUILTIN_ROLES["tester"].system_prompt
    assert "Never weaken a test to make it pass" in prompt
    assert "leave the test as it is and report FAILED" in prompt
    assert "defect you were not asked about" in prompt


# ------------------------- the check never routes work to the fixer

def test_a_failing_check_never_sends_work_to_the_fixer(tmp_path, monkeypatch):
    """The check only decides whether to halt. Only the outcome loops."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker", "reviewer"])
    step = Step(role="backend", task="t", check="factchecker",
                on_fail="reviewer", max_loops=3)
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        if kw["role"].name == "factchecker":
            return _Turn("FAIL", "nothing on disk")
        return _Turn(None, "done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)
    assert "reviewer" not in order          # the fixer was never involved
    assert engine.session.stopping         # it halted instead


def test_the_fixer_runs_without_any_check_configured(tmp_path, monkeypatch):
    """A fixer is about the step's outcome, so it needs no fact check."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "reviewer"])
    step = Step(role="backend", task="t", check=None, on_fail="reviewer", max_loops=2)
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        first = order.count("backend") == 1
        return _Turn(None, "x",
                     outcome=("FAILED", "port already in use") if first else ("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)
    assert order == ["backend", "reviewer", "backend"]
    assert step.status == "done"


def test_the_fixer_is_briefed_on_the_outcome_not_the_check(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker", "reviewer"])
    step = Step(role="backend", task="t", check="factchecker",
                on_fail="reviewer", max_loops=2)
    prompts = {}

    def fake(**kw):
        name = kw["role"].name
        prompts.setdefault(name, kw["task"])
        if name == "factchecker":
            return _Turn("PASS", "files are all there")
        if name == "backend":
            first = "backend" not in prompts or len(prompts) < 3
            return _Turn(None, "tried",
                         outcome=("FAILED", "the port was already taken") if first
                         else ("SUCCESS", ""))
        return _Turn(None, "fixed")

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)
    assert "the port was already taken" in prompts["reviewer"]
    assert "files are all there" not in prompts["reviewer"]


# ------------------------------------ server-side truncated tool calls

def test_a_server_rejected_truncated_call_is_recognised():
    """llama.cpp parses tool arguments itself and 500s on a call the model did
    not finish writing."""
    from trance.worker.client import _is_truncated_tool_call

    body = ('{"error":{"code":500,"message":"Failed to parse tool call arguments as JSON: '
            '[json.exception.parse_error.101] parse error at line 1, column 15026: syntax '
            'error while parsing value - invalid string: missing closing quote; last read"}}')
    assert _is_truncated_tool_call(body) is True
    assert _is_truncated_tool_call('{"error":{"message":"out of memory"}}') is False


def test_a_truncated_call_500_does_not_abort_the_step(monkeypatch, tmp_path):
    import urllib.error

    from trance.config import ModelConfig
    from trance.providers.base import BackendError
    from trance.worker.client import ChatClient

    import io

    body = b'{"error":{"message":"Failed to parse tool call arguments as JSON"}}'

    def raise_500(*args, **kwargs):
        raise urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", raise_500)
    response = ChatClient(ModelConfig()).complete([{"role": "user", "content": "hi"}])
    assert response.provider_error == "truncated_tool_call"
    assert response.finish_reason == "length"


def test_other_500s_still_raise(monkeypatch):
    import io
    import urllib.error

    from trance.config import ModelConfig
    from trance.providers.base import BackendError
    from trance.worker.client import ChatClient

    def raise_500(*args, **kwargs):
        raise urllib.error.HTTPError(
            "u", 500, "err", {}, io.BytesIO(b'{"error":{"message":"out of memory"}}'))

    monkeypatch.setattr("urllib.request.urlopen", raise_500)
    with pytest.raises(BackendError):
        ChatClient(ModelConfig()).complete([{"role": "user", "content": "hi"}])


def test_repeated_truncation_fails_the_step_with_a_reason(monkeypatch, tmp_path):
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    class AlwaysTruncates:
        def complete(self, messages, tools=None, **kwargs):
            return ChatResponse(text="", finish_reason="length",
                                provider_error="truncated_tool_call")

    monkeypatch.setattr(runner, "client_for", lambda config: AlwaysTruncates())
    turn = runner.run_agent(
        role=BUILTIN_ROLES["backend"], task="write a big file", project=tmp_path,
        config=ModelConfig(max_tokens=4096), bus=EventBus(),
        session_id="s", step_id="st")

    assert turn.stop_reason == "truncated_tool_calls"
    assert turn.truncated_calls == 3          # tried, then gave up
    outcome, reason = turn.outcome
    assert outcome == "FAILED" and "4096" in reason


def test_one_truncated_call_is_retried_not_fatal(monkeypatch, tmp_path):
    """The step keeps whatever it already did; the agent is told to write less."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    seen = []

    class TruncatesOnce:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=None, **kwargs):
            self.calls += 1
            seen.append(messages[-1])
            if self.calls == 1:
                return ChatResponse(text="", finish_reason="length",
                                    provider_error="truncated_tool_call")
            return ChatResponse(text="OUTCOME: SUCCESS", finish_reason="stop")

    monkeypatch.setattr(runner, "client_for", lambda config: TruncatesOnce())
    turn = runner.run_agent(
        role=BUILTIN_ROLES["backend"], task="write a big file", project=tmp_path,
        config=ModelConfig(max_tokens=4096), bus=EventBus(),
        session_id="s", step_id="st")

    assert turn.truncated_calls == 1
    assert turn.outcome[0] == "SUCCESS"
    # The retry prompt must say what went wrong, or the model repeats it verbatim.
    assert "cut off" in seen[-1]["content"] and "append_file" in seen[-1]["content"]


# ------------------------------------------------ context gauge numbers

def test_context_usage_prefers_what_the_server_reported(monkeypatch, tmp_path):
    from trance.agents.runner import context_usage
    from trance.config import ModelConfig
    from trance.providers.base import ChatResponse

    config = ModelConfig(context_window=64000, max_tokens=8192)
    messages = [{"role": "user", "content": "x" * 400}]   # ~100 tokens estimated
    usage = context_usage(messages, ChatResponse(
        text="", usage={"prompt_tokens": 32000}), config)

    assert usage["tokens"] == 32000 and usage["estimated"] is False
    assert usage["percent"] == 50.0
    assert usage["budget"] == config.input_budget   # gauge and trimmer agree
    assert usage["reserved"] == 8192


def test_context_usage_falls_back_to_an_estimate(monkeypatch):
    from trance.agents.runner import context_usage
    from trance.config import ModelConfig
    from trance.providers.base import ChatResponse

    usage = context_usage([{"role": "user", "content": "x" * 4000}],
                          ChatResponse(text="", usage={}),
                          ModelConfig(context_window=64000))
    # 3.5 chars/token, not 4: code and JSON are denser than prose, and the
    # estimate ran low exactly where contexts get big.
    assert usage["tokens"] == 1142 and usage["estimated"] is True


# ------------------------------- handing a failure to the fixing agent

def _tester_transcript():
    """What a tester's turn looks like: it reads, writes a test, runs it."""
    return [
        {"tool": "read_file", "arguments": {"path": "server/game.js"}, "ok": True,
         "text": "export function step(){...}" * 200, "detail": {"kind": "read",
                                                                 "path": "server/game.js"}},
        {"tool": "write_file", "arguments": {}, "ok": True, "text": "written",
         "detail": {"kind": "write", "path": "tests/ball.test.js", "created": True,
                    "added": 42, "removed": 0,
                    "diff": "+expect(ball.vx).toBe(-3)"}},
        {"tool": "run_command", "arguments": {}, "ok": False, "text": "",
         "detail": {"kind": "command", "command": "npm test", "exit_code": 1, "seconds": 4,
                    "output": "● ball bounces\n\nExpected: -3\nReceived: 3\n"}},
    ]


def test_the_handoff_keeps_the_failure_and_drops_the_reading():
    from trance.agents.handoff import digest

    text = digest(_tester_transcript(), "OUTCOME: FAILED — the ball passes through the paddle")

    assert "Expected: -3" in text          # the actual evidence
    assert "npm test" in text
    assert "+expect(ball.vx).toBe(-3)" in text
    assert "OUTCOME: FAILED" in text
    # The file it read is named, but its contents are not reprinted — the fixer
    # can pull those itself, and they are most of the transcript.
    assert "looked at server/game.js" in text
    assert "export function step" not in text


def test_the_handoff_gives_up_routine_output_before_the_failure():
    from trance.agents.handoff import digest

    noisy = [{"tool": "run_command", "arguments": {}, "ok": True, "text": "",
              "detail": {"kind": "command", "command": f"npm run lint{i}", "exit_code": 0,
                         "seconds": 1, "output": "clean\n" * 500}}
             for i in range(6)]
    text = digest(noisy + _tester_transcript(), "OUTCOME: FAILED — bug", budget_chars=3000)

    assert len(text) <= 3600                       # roughly inside the budget
    assert "Expected: -3" in text                  # the failure survives
    assert "npm run lint5" in text                 # the routine runs are still named
    assert text.count("clean") < 100               # but not quoted at length


def test_the_closing_report_is_never_trimmed_away():
    from trance.agents.handoff import digest

    report = "OUTCOME: FAILED — " + "the paddle collision is inverted. " * 30
    text = digest(_tester_transcript(), report, budget_chars=200)
    assert report.strip() in text


def test_an_empty_turn_hands_over_nothing():
    from trance.agents.handoff import digest

    assert digest([], "") == ""


def test_the_fixer_is_shown_what_the_failing_agent_did(tmp_path, monkeypatch):
    """Regression: the fixer was told only that the step failed and why, so its
    first move was always to re-run the suite it had just been told about."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["tester", "backend"])
    step = Step(role="tester", task="test the paddle", on_fail="backend", max_loops=2)
    prompts = {}

    def fake(**kw):
        name = kw["role"].name
        prompts.setdefault(name, kw["task"])
        if name == "tester":
            passed = "backend" in prompts
            return _Turn(None, "OUTCOME: FAILED — the ball passes through",
                         outcome=("SUCCESS", "") if passed
                         else ("FAILED", "the ball passes through"),
                         transcript=_tester_transcript())
        return _Turn(None, "fixed the collision", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    brief = prompts["backend"]
    assert "npm test" in brief and "Expected: -3" in brief
    assert "tests/ball.test.js" in brief
    assert "do not repeat it" in brief          # told not to re-derive it


def test_a_rerunning_agent_is_reminded_of_its_own_previous_pass(tmp_path, monkeypatch):
    """With no fixer the same role runs again — in a fresh conversation, so it
    has forgotten the pass that just failed."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["tester"])
    step = Step(role="tester", task="test the paddle", max_loops=2)
    steering = []

    def fake(**kw):
        steering.append(kw.get("steering") or [])
        failed = len(steering) == 1
        return _Turn(None, "report",
                     outcome=("FAILED", "the ball passes through") if failed
                     else ("SUCCESS", ""),
                     transcript=_tester_transcript())

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert steering[0] == []
    second = "\n".join(steering[1])
    assert "the ball passes through" in second
    assert "Expected: -3" in second


# ---------------------------------------------- building a file in pieces

def _tools_with_approval(tmp_path, role, broker):
    from trance.agents.tools import AgentTools

    role = BUILTIN_ROLES[role] if isinstance(role, str) else role
    return AgentTools(tmp_path, role, None, notify=lambda *a, **k: None,
                      approve=broker.ask, session_id="s", step_id="st")


def _tools(tmp_path, role_name="backend"):
    from trance.agents.tools import AgentTools

    return AgentTools(tmp_path, BUILTIN_ROLES[role_name], None, notify=lambda *a, **k: None)


def test_a_file_can_be_built_up_across_several_calls(tmp_path):
    """A model cannot emit more tokens than the context it has left, so a long
    file has to arrive in pieces or not at all."""
    tools = _tools(tmp_path)
    tools.call("write_file", {"path": "server/app.js", "content": "// part 1\n"})
    outcome = tools.call("append_file", {"path": "server/app.js", "content": "// part 2\n"})

    assert outcome.ok
    assert (tmp_path / "server/app.js").read_text() == "// part 1\n// part 2\n"
    assert outcome.detail["appended"] is True
    assert outcome.detail["removed"] == 0        # appending never deletes
    assert "2 lines" in outcome.text


def test_append_creates_the_file_when_it_is_missing(tmp_path):
    tools = _tools(tmp_path)
    outcome = tools.call("append_file", {"path": "server/new.js", "content": "x\n"})
    assert outcome.ok and outcome.detail["created"] is True
    assert (tmp_path / "server/new.js").read_text() == "x\n"


def test_append_obeys_the_remit_like_any_write(tmp_path):
    """The bypass would be obvious otherwise: append to a file you may not write."""
    tools = _tools(tmp_path, "tester")           # may write tests/**, nothing else
    outcome = tools.call("append_file", {"path": "server/app.js", "content": "sneaky"})
    assert outcome.ok is False
    assert outcome.remit_violation == "server/app.js"
    assert not (tmp_path / "server/app.js").exists()


def test_an_agent_without_the_files_toolset_cannot_append(tmp_path):
    tools = _tools(tmp_path, "factchecker")
    outcome = tools.call("append_file", {"path": "a.txt", "content": "x"})
    assert outcome.ok is False and "do not have" in outcome.text


def test_agents_are_told_how_to_write_a_file_too_long_for_one_reply():
    from trance.agents.tools import permissions_brief

    brief = permissions_brief(BUILTIN_ROLES["backend"])
    assert "append_file" in brief and "cut off" in brief


# ------------------------------------------ shared memory across agents

def test_a_note_reaches_the_next_agents_prompt(tmp_path):
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    stored, message = memory.note("backend", "the API is POST /api/games returning {id, state}")
    assert stored and "every agent after you" in message
    assert "POST /api/games" in memory.for_prompt()
    assert "**backend**" in memory.for_prompt()      # who decided it, not just what


def test_memory_survives_a_new_process(tmp_path):
    from trance.agents.memory import ProjectMemory

    ProjectMemory(tmp_path).note("backend", "the server listens on port 3100")
    assert "3100" in ProjectMemory(tmp_path).for_prompt()
    assert (tmp_path / ".trance" / "memory.md").exists()   # editable by hand


def test_the_same_fact_is_not_stored_twice(tmp_path):
    """Every note is paid for by every later agent, so duplicates matter."""
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    memory.note("backend", "the server listens on port 3100")
    stored, message = memory.note("frontend", "The server listens on port 3100.")

    assert stored is False and "Already in project memory" in message
    assert len(memory.notes()) == 1


def test_the_prompt_view_is_bounded_and_keeps_the_newest(tmp_path):
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    for i in range(60):
        memory.note("backend", f"decision number {i} " + "x" * 80)

    view = memory.for_prompt(budget=1000)
    assert len(view) <= 1100
    assert "decision number 59" in view          # newest survives
    assert "decision number 0 " not in view
    assert "older note(s) omitted" in view       # and says so


def test_remember_is_offered_to_working_agents_but_not_to_the_factchecker(tmp_path):
    tools = _tools(tmp_path, "backend")
    assert "remember" in {s["function"]["name"] for s in tools.specs()}

    checker = _tools(tmp_path, "factchecker")
    assert "remember" not in {s["function"]["name"] for s in checker.specs()}
    assert checker.call("remember", {"note": "x"}).ok is False


def test_an_agent_starts_with_what_the_team_already_decided(tmp_path, monkeypatch):
    from trance.agents import runner
    from trance.agents.memory import ProjectMemory
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    ProjectMemory(tmp_path).note("backend", "the API is POST /api/games")
    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None, **kwargs):
            captured["prompt"] = messages[-1]["content"]
            return ChatResponse(text="OUTCOME: SUCCESS")

    monkeypatch.setattr(runner, "client_for", lambda config: FakeClient())
    runner.run_agent(role=BUILTIN_ROLES["frontend"], task="call the API", project=tmp_path,
                     config=ModelConfig(), bus=EventBus(), session_id="s", step_id="st")

    assert "POST /api/games" in captured["prompt"]
    assert "Project memory" in captured["prompt"]


# ----------------------------------------------- making the graph usable

def test_the_project_map_lists_what_is_indexed(tmp_path):
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import project_map

    (tmp_path / "app.py").write_text("def charge(order):\n    return 1\n\nclass Cart:\n    pass\n")
    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)

    text = project_map(db)
    assert "app.py" in text and "charge" in text and "Cart" in text


def test_the_map_is_bounded_and_says_what_it_left_out(tmp_path):
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import project_map

    for i in range(40):
        (tmp_path / f"mod{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)

    text = project_map(db, budget_chars=300)
    assert len(text) < 500
    assert "more file(s)" in text and "search_symbols" in text


def test_the_map_puts_the_files_the_task_names_first(tmp_path):
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import project_map

    (tmp_path / "aaa.py").write_text("def first():\n    pass\n")
    (tmp_path / "zzz.py").write_text("def last():\n    pass\n")
    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)

    text = project_map(db, focus="fix the bounce logic in zzz.py please")
    assert text.splitlines()[0].startswith("zzz.py")


def test_an_agent_is_shown_the_map_before_it_starts_reading(tmp_path, monkeypatch):
    """Regression: agents ignored the graph tools entirely and read whole files,
    because nothing told them which symbols existed to ask for."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None, **kwargs):
            captured["prompt"] = messages[-1]["content"]
            return ChatResponse(text="OUTCOME: SUCCESS")

    monkeypatch.setattr(runner, "client_for", lambda config: FakeClient())
    runner.run_agent(role=BUILTIN_ROLES["backend"], task="add an endpoint", project=tmp_path,
                     config=ModelConfig(), bus=EventBus(), session_id="s", step_id="st",
                     project_map="server/app.py: create_app, register_routes")

    assert "create_app" in captured["prompt"]
    assert "get_definition" in captured["prompt"]     # and what to do with it


def test_the_memory_endpoint_shows_and_edits_what_agents_see(tmp_path, monkeypatch):
    """A wrong shared fact misleads every later agent, so it must be fixable."""
    from fastapi.testclient import TestClient

    from trance.agents.memory import ProjectMemory
    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    project = tmp_path / "proj"
    project.mkdir()
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]
    ProjectMemory(project).note("backend", "the server listens on port 9999")

    body = client.get(f"/api/sessions/{sid}/memory").json()
    assert "9999" in body["raw"] and len(body["notes"]) == 1

    client.put(f"/api/sessions/{sid}/memory",
               json={"raw": "- **user**: the server listens on port 3100\n"})
    assert "3100" in ProjectMemory(project).for_prompt()


def test_memory_is_in_every_request_of_a_turn(tmp_path, monkeypatch):
    """Not just the opening prompt: a long tool loop must not drift away from
    the team's decisions."""
    from trance.agents import runner
    from trance.agents.memory import ProjectMemory
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse, ToolCall

    ProjectMemory(tmp_path).note("backend", "the API is POST /api/games")
    seen = []

    class Looper:
        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None, **kwargs):
            self.n += 1
            seen.append("\n".join(str(m.get("content")) for m in messages))
            if self.n < 4:
                return ChatResponse(text="", tool_calls=[
                    ToolCall(id=f"c{self.n}", name="list_files", arguments={})])
            return ChatResponse(text="OUTCOME: SUCCESS")

    monkeypatch.setattr(runner, "client_for", lambda config: Looper())
    runner.run_agent(role=BUILTIN_ROLES["frontend"], task="build the ui", project=tmp_path,
                     config=ModelConfig(), bus=EventBus(), session_id="s", step_id="st")

    assert len(seen) == 5          # 4 rounds, plus the end-of-step memory nudge
    assert all("POST /api/games" in prompt for prompt in seen)


def test_the_verifier_sees_the_same_memory_as_the_worker(tmp_path, monkeypatch):
    """A checker judging against different facts than the worker built to is
    just a second opinion about the wrong thing."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker"])
    engine.memory.note("backend", "the server listens on port 3100")
    step = Step(role="backend", task="build it", check="factchecker")
    prompts = {}

    def fake(**kw):
        prompts[kw["role"].name] = (kw.get("memory") or engine.memory).for_prompt()
        return _Turn("PASS", "done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert "3100" in prompts["backend"]
    assert "3100" in prompts["factchecker"]


def test_the_orchestrator_plans_against_the_teams_decisions(tmp_path, monkeypatch):
    from trance.agents import orchestrator
    from trance.agents.memory import ProjectMemory
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    ProjectMemory(tmp_path).note("backend", "the server listens on port 3100")
    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None, **kwargs):
            captured["system"] = messages[0]["content"]
            return ChatResponse(text="ok")

    monkeypatch.setattr(orchestrator, "client_for", lambda config: FakeClient())
    orchestrator.chat(messages=[{"role": "user", "content": "add a scoreboard"}],
                      project_dir=tmp_path, config=ModelConfig(), bus=EventBus(),
                      session_id="s")

    assert "3100" in captured["system"]
    assert "already decided" in captured["system"]


# --------------------------------- making every step update the memory

def _turn_with(monkeypatch, replies, role="backend", project=None, **kw):
    """Drive run_agent with a scripted sequence of model replies."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus

    prompts = []

    class Scripted:
        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None, **kwargs):
            prompts.append(str(messages[-1].get("content")))
            reply = replies[min(self.n, len(replies) - 1)]
            self.n += 1
            return reply

    monkeypatch.setattr(runner, "client_for", lambda config: Scripted())
    turn = runner.run_agent(role=BUILTIN_ROLES[role], task="do it", project=project,
                            config=ModelConfig(), bus=EventBus(),
                            session_id="s", step_id="st", **kw)
    return turn, prompts


def test_an_agent_that_did_work_is_asked_what_to_remember(tmp_path, monkeypatch):
    """Asking after the step is over is too late — the agent is gone."""
    from trance.providers.base import ChatResponse, ToolCall

    replies = [
        ChatResponse(text="", tool_calls=[ToolCall(
            id="c1", name="write_file",
            arguments={"path": "server/app.py", "content": "x = 1\n"})]),
        ChatResponse(text="Wrote the app.\n\nOUTCOME: SUCCESS"),
        ChatResponse(text="", tool_calls=[ToolCall(
            id="c2", name="remember",
            arguments={"note": "the API is POST /api/games"})]),
        ChatResponse(text="OUTCOME: SUCCESS"),
    ]
    turn, prompts = _turn_with(monkeypatch, replies, project=tmp_path)

    nudge = next(p for p in prompts if "is there a fact the next agent must match" in p)
    assert "server/app.py" in nudge          # names what it actually wrote
    assert "holds 0 note(s)" in nudge        # and closes the "already covered" excuse
    assert turn.notes_written == 1
    assert turn.outcome[0] == "SUCCESS"          # the outcome survives the nudge

    from trance.agents.memory import ProjectMemory
    assert "POST /api/games" in ProjectMemory(tmp_path).for_prompt()


def test_the_nudge_happens_once_and_takes_no_for_an_answer(tmp_path, monkeypatch):
    from trance.providers.base import ChatResponse, ToolCall

    replies = [
        ChatResponse(text="", tool_calls=[ToolCall(
            id="c1", name="write_file",
            arguments={"path": "server/app.py", "content": "x = 1\n"})]),
        ChatResponse(text="Done.\n\nOUTCOME: SUCCESS"),
        ChatResponse(text="Nothing the others need.\n\nOUTCOME: SUCCESS"),
    ]
    turn, prompts = _turn_with(monkeypatch, replies, project=tmp_path)

    asked = sum("is there a fact the next agent must match" in p for p in prompts)
    assert asked == 1                            # not once per round
    assert turn.notes_written == 0
    assert turn.outcome[0] == "SUCCESS"


def test_an_agent_that_already_remembered_is_not_asked(tmp_path, monkeypatch):
    from trance.providers.base import ChatResponse, ToolCall

    replies = [
        ChatResponse(text="", tool_calls=[ToolCall(
            id="c1", name="remember", arguments={"note": "the port is 3100"})]),
        ChatResponse(text="Done.\n\nOUTCOME: SUCCESS"),
    ]
    turn, prompts = _turn_with(monkeypatch, replies, project=tmp_path)

    assert not any("is there a fact the next agent must match" in p for p in prompts)
    assert turn.notes_written == 1


def test_an_agent_that_did_nothing_is_not_asked(tmp_path, monkeypatch):
    """No work, nothing to hand over — the extra round would be pure cost."""
    from trance.providers.base import ChatResponse

    turn, prompts = _turn_with(monkeypatch, [ChatResponse(text="OUTCOME: FAILED — blocked")],
                               project=tmp_path)
    assert not any("is there a fact the next agent must match" in p for p in prompts)
    assert turn.rounds == 1


# ------------------------------------------ keeping the memory small

def _fill(memory, n, prefix="fact"):
    for i in range(n):
        memory.note("backend", f"{prefix} number {i} that the team must follow")


def test_memory_is_compacted_once_it_outgrows_every_prompt(tmp_path):
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    _fill(memory, 30)
    assert memory.oversized() is True

    result = memory.compact(lambda text: "- **backend**: the ports are 3100 and 3101\n"
                                         "- **backend**: routes live under /api")
    assert result["compacted"] is True and result["before"] == 30 and result["after"] == 2
    assert len(memory.notes()) == 2
    assert "3100" in memory.for_prompt()
    assert memory.oversized() is False


def test_compaction_archives_what_it_replaced(tmp_path):
    """A memory you cannot audit is worse than a long one — the agents were
    told those facts, and a run gets debugged after it goes wrong."""
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    _fill(memory, 30)
    memory.compact(lambda text: "- **backend**: everything is fine")

    archive = (tmp_path / ".trance" / "memory.archive.md").read_text()
    assert "fact number 0" in archive and "fact number 29" in archive
    assert "Compacted from 30 to 1 notes" in archive


def test_a_rewrite_that_loses_the_notes_is_refused(tmp_path):
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    _fill(memory, 30)

    for bad in ("", "Sure! Here is a summary of the project.", "   "):
        result = memory.compact(lambda text, b=bad: b)
        assert result["compacted"] is False
        assert len(memory.notes()) == 30          # untouched


def test_a_rewrite_that_grew_is_refused(tmp_path):
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    _fill(memory, 30)
    result = memory.compact(lambda text: "\n".join(f"- note {i}" for i in range(60)))
    assert result["compacted"] is False and len(memory.notes()) == 30


def test_a_failing_rewrite_leaves_the_memory_alone(tmp_path):
    """The endpoint being down must not cost the team its shared facts."""
    from trance.agents.memory import ProjectMemory

    def explode(text):
        raise RuntimeError("connection refused")

    memory = ProjectMemory(tmp_path)
    _fill(memory, 30)
    result = memory.compact(explode)
    assert result["compacted"] is False and "connection refused" in result["reason"]
    assert len(memory.notes()) == 30


def test_a_small_memory_is_left_alone(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])
    engine.memory.note("backend", "the port is 3100")
    called = []
    monkeypatch.setattr(engine.memory, "compact", lambda *a, **k: called.append(1) or {})

    engine._compact_memory()
    assert called == []


def test_the_engine_compacts_between_steps_not_during_one(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])
    _fill(engine.memory, 30)
    seen = []

    def fake(**kw):
        seen.append(len(engine.memory.notes()))
        return _Turn(None, "done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    monkeypatch.setattr(engine.memory, "compact",
                        lambda rewrite: {"compacted": True, "before": 30, "after": 2})

    engine._execute(Step(role="backend", task="t"))
    assert seen == [30]        # the step ran against a stable memory


def test_compaction_can_be_triggered_from_the_ui(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trance.agents.memory import ProjectMemory
    from trance.config import Config
    from trance.providers.base import ChatResponse
    from trance.server import app as app_module

    monkeypatch.setattr(app_module, "client_for", lambda config: type(
        "C", (), {"complete": lambda self, messages, tools=None, **kwargs: ChatResponse(
            text="- **backend**: the API is POST /api/games on port 3100")})())

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    project = tmp_path / "proj"
    project.mkdir()
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]
    memory = ProjectMemory(project)
    _fill(memory, 30)
    assert client.get(f"/api/sessions/{sid}/memory").json()["oversized"] is True

    body = client.post(f"/api/sessions/{sid}/memory/compact").json()
    assert body["compacted"] is True and body["after"] == 1
    assert client.get(f"/api/sessions/{sid}/memory").json()["oversized"] is False


# ------------------------------------- sizing steps and splitting big ones

def _proposal(*points):
    return {"summary": "s", "team": ["backend"],
            "steps": [{"role": "backend", "task": f"task worth {p}", "check": "factchecker",
                       "on_fail": None, "max_loops": 2, "points": p} for p in points]}


def _split_client(monkeypatch, pieces_by_task):
    """An orchestrator that answers split_step with a scripted breakdown."""
    from trance.agents import orchestrator
    from trance.providers.base import ChatResponse, ToolCall

    asked = []

    class FakeClient:
        def complete(self, messages, tools=None, **kwargs):
            task = messages[-1]["content"].split("Task: ", 1)[-1].split("\n")[0]
            asked.append(task)
            pieces = pieces_by_task.get(task, [])
            return ChatResponse(text="", tool_calls=[ToolCall(
                id="c", name="split_step", arguments={"steps": pieces})])

    monkeypatch.setattr(orchestrator, "client_for", lambda config: FakeClient())
    return asked


def test_the_estimate_is_snapped_to_the_scale():
    from trance.agents.orchestrator import _points

    assert _points(8) == 8
    assert _points(4) == 3        # 4 is equidistant; the smaller claim is the safer one
    assert _points(100) == 13
    assert _points("nonsense") == 0
    assert _points(0) == 0


def test_a_step_over_the_threshold_is_broken_up(tmp_path, monkeypatch):
    from trance.agents.orchestrator import split_oversized
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.config import ModelConfig
    from trance.events import EventBus

    asked = _split_client(monkeypatch, {"task worth 8": [
        {"role": "backend", "task": "write the model layer", "points": 3},
        {"role": "backend", "task": "write the routes on top of it", "points": 3},
    ]})
    out = split_oversized(_proposal(2, 8), roles=list(R.values()),
                          config=ModelConfig(), bus=EventBus(), session_id="s", threshold=5)

    assert asked == ["task worth 8"]                 # only the oversized one
    assert [s["task"] for s in out["steps"]] == [
        "task worth 2", "write the model layer", "write the routes on top of it"]
    assert all(s["points"] <= 5 for s in out["steps"])


def test_a_split_piece_inherits_the_check_it_was_given(tmp_path, monkeypatch):
    """Splitting a checked step into unchecked ones would quietly remove the
    verification the user asked for."""
    from trance.agents.orchestrator import split_oversized
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.config import ModelConfig
    from trance.events import EventBus

    _split_client(monkeypatch, {"task worth 8": [
        {"role": "backend", "task": "part one", "points": 3},
        {"role": "backend", "task": "part two", "points": 2},
    ]})
    out = split_oversized(_proposal(8), roles=list(R.values()), config=ModelConfig(),
                          bus=EventBus(), session_id="s", threshold=5)

    assert [s["check"] for s in out["steps"]] == ["factchecker", "factchecker"]


def test_a_step_that_cannot_be_split_is_kept(monkeypatch):
    """A refusal is information — forcing a break would invent filler steps."""
    from trance.agents.orchestrator import split_oversized
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.config import ModelConfig
    from trance.events import EventBus

    _split_client(monkeypatch, {"task worth 13": [
        {"role": "backend", "task": "task worth 13", "points": 13}]})
    out = split_oversized(_proposal(13), roles=list(R.values()), config=ModelConfig(),
                          bus=EventBus(), session_id="s", threshold=5)

    assert len(out["steps"]) == 1
    assert out["steps"][0]["task"] == "task worth 13"


def test_splitting_recurses_but_stops(monkeypatch):
    """Each pass halves; two passes is enough, and past that models pad."""
    from trance.agents.orchestrator import split_oversized
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.config import ModelConfig
    from trance.events import EventBus

    asked = _split_client(monkeypatch, {
        "task worth 13": [{"role": "backend", "task": "still big", "points": 8},
                          {"role": "backend", "task": "small bit", "points": 2}],
        "still big": [{"role": "backend", "task": "half a", "points": 3},
                      {"role": "backend", "task": "half b", "points": 3}],
    })
    out = split_oversized(_proposal(13), roles=list(R.values()), config=ModelConfig(),
                          bus=EventBus(), session_id="s", threshold=5)

    assert asked == ["task worth 13", "still big"]
    assert [s["task"] for s in out["steps"]] == ["half a", "half b", "small bit"]


def test_a_zero_threshold_turns_splitting_off(monkeypatch):
    from trance.agents.orchestrator import split_oversized
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.config import ModelConfig
    from trance.events import EventBus

    asked = _split_client(monkeypatch, {})
    out = split_oversized(_proposal(13, 8), roles=list(R.values()), config=ModelConfig(),
                          bus=EventBus(), session_id="s", threshold=0)
    assert asked == [] and len(out["steps"]) == 2      # estimates kept as information


def test_the_estimate_survives_into_the_flow():
    from trance.flow import Step

    step = Step.from_dict({"role": "backend", "task": "t", "points": 5})
    assert step.points == 5 and step.to_dict()["points"] == 5


def test_the_split_threshold_is_configurable_and_can_be_turned_off(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    assert client.get("/api/config").json()["planning"]["max_step_points"] == 5

    assert client.put("/api/config/planning",
                      json={"max_step_points": 3}).json()["max_step_points"] == 3
    assert client.put("/api/config/planning",
                      json={"max_step_points": 0}).json()["max_step_points"] == 0
    assert client.put("/api/config/planning", json={"max_step_points": 99}).status_code == 400


def test_a_step_can_be_split_from_the_flow_editor(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.providers.base import ChatResponse, ToolCall
    from trance.server import app as app_module

    class FakeClient:
        def complete(self, messages, tools=None, **kwargs):
            return ChatResponse(text="", tool_calls=[ToolCall(
                id="c", name="split_step", arguments={"steps": [
                    {"role": "backend", "task": "write the model layer", "points": 3},
                    {"role": "backend", "task": "write the routes", "points": 2}]})])

    monkeypatch.setattr("trance.agents.orchestrator.client_for", lambda config: FakeClient())
    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={"steps": [
        {"role": "backend", "task": "build the whole api", "points": 8},
        {"role": "tester", "task": "test it", "points": 2}]})

    session = client.app.state.store.get(sid)
    step_id = session.flow.steps[0].id
    body = client.post(f"/api/sessions/{sid}/steps/{step_id}/split").json()

    assert body["split"] is True
    assert [s["task"] for s in body["flow"]["steps"]] == [
        "write the model layer", "write the routes", "test it"]   # in place, order kept


def test_the_plan_is_returned_before_the_splitting_finishes(tmp_path, monkeypatch):
    """Regression: splitting ran inside the chat request, so a proposal that
    needed two 40-second split calls left the flow panel saying 'no steps yet'
    with no explanation."""
    import threading

    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.providers.base import ChatResponse, ToolCall
    from trance.server import app as app_module

    splitting = threading.Event()
    proposal = {"summary": "s", "team": ["backend"], "steps": [
        {"role": "backend", "task": "build the whole api", "points": 8},
        {"role": "backend", "task": "add a health check", "points": 1}]}

    def fake_chat(**kw):
        return {"text": "here is the plan", "proposal": proposal, "truncated": False}

    def slow_split(*args, **kwargs):
        splitting.wait(5)                     # still working when we assert
        return {**proposal, "split": []}

    monkeypatch.setattr(app_module.orchestrator_agent, "chat", fake_chat)
    monkeypatch.setattr(app_module.orchestrator_agent, "split_oversized", slow_split)

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    try:
        sid = client.post("/api/sessions",
                          json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
        body = client.post(f"/api/sessions/{sid}/chat", json={"message": "build it"}).json()

        # The plan is there straight away, unsplit, rather than after the wait.
        assert [s["task"] for s in body["flow"]["steps"]] == [
            "build the whole api", "add a health check"]
        assert body["status"] == "ready"
    finally:
        splitting.set()


def test_the_user_is_told_splitting_is_still_running(tmp_path, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    done = threading.Event()
    proposal = {"summary": "s", "team": ["backend"], "steps": [
        {"role": "backend", "task": "build the whole api", "points": 8}]}

    monkeypatch.setattr(app_module.orchestrator_agent, "chat",
                        lambda **kw: {"text": "plan", "proposal": proposal})
    monkeypatch.setattr(app_module.orchestrator_agent, "split_oversized",
                        lambda *a, **k: done.wait(5) or {**proposal, "split": []})

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)
    try:
        sid = client.post("/api/sessions",
                          json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
        client.post(f"/api/sessions/{sid}/chat", json={"message": "build it"})

        kinds = [e.type for e in app.state.bus.history(sid)]
        assert "splitting_steps" in kinds        # so the UI can say what it is waiting for
        notice = next(e for e in app.state.bus.history(sid) if e.type == "splitting_steps")
        assert notice.payload["count"] == 1 and notice.payload["threshold"] == 5
    finally:
        done.set()


# ------------------------------- orientation: the goal and what comes next

def test_an_agent_is_told_the_goal_and_what_its_task_is_not(tmp_path, monkeypatch):
    """An agent that does not know the goal makes locally sensible, globally
    wrong choices — an API shape nothing downstream can use."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "frontend", "tester"])
    engine.session.goal = "A crypto backtester with TradingView charts."
    engine.session.flow.steps = [
        Step(role="backend", task="write the OHLC loader", id="s1"),
        Step(role="frontend", task="draw the chart", id="s2"),
        Step(role="tester", task="test the loader", id="s3"),
    ]
    prompts = {}

    def fake(**kw):
        prompts[kw["role"].name] = (kw.get("goal", ""), kw.get("placement", ""))
        return _Turn(None, "done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(engine.session.flow.steps[0])

    goal, placement = prompts["backend"]
    assert "crypto backtester" in goal
    assert "step 1 of 3" in placement
    assert "draw the chart" in placement          # it knows what it must serve
    assert "not\nto you" in placement or "not to you" in placement.replace("\n", " ")
    assert "Do not start it" in placement


def test_the_last_step_is_told_it_is_the_last(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])
    engine.session.flow.steps = [Step(role="backend", task="finish it", id="s1")]
    seen = {}

    def fake(**kw):
        seen["placement"] = kw.get("placement", "")
        return _Turn(None, "done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(engine.session.flow.steps[0])
    assert "last step" in seen["placement"]
    assert "finished state" in seen["placement"]


def test_only_the_next_two_steps_are_shown(tmp_path, monkeypatch):
    """The whole list is an invitation to do someone else's step; the next
    couple is orientation."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])
    engine.session.flow.steps = [
        Step(role="backend", task=f"task {i}", id=f"s{i}") for i in range(6)]
    seen = {}

    def fake(**kw):
        seen["placement"] = kw.get("placement", "")
        return _Turn(None, "done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(engine.session.flow.steps[0])

    assert "task 1" in seen["placement"] and "task 2" in seen["placement"]
    assert "task 3" not in seen["placement"]


def test_the_goal_survives_a_restart(tmp_path):
    from trance.session import SessionStore

    store = SessionStore(tmp_path)
    session = store.create("p", "/tmp/p")
    session.goal = "A crypto backtester."
    store.save(session)
    assert SessionStore(tmp_path).get(session.id).goal == "A crypto backtester."


def test_the_plan_is_written_where_a_person_can_read_it(tmp_path):
    from trance.agents.memory import write_plan
    from trance.flow import Step

    path = write_plan(tmp_path, "A crypto backtester.", [
        Step(role="backend", task="write the loader", points=3, check="factchecker",
             status="done"),
        Step(role="frontend", task="draw the chart", points=5),
    ])
    text = path.read_text()

    assert path.name == "PLAN.md" and path.parent.name == ".trance"
    assert "A crypto backtester." in text
    assert "[x]" in text and "write the loader" in text     # progress is visible
    assert "checked by factchecker" in text
    # Never the project's own README: that belongs to whoever reads the repo.
    assert not (tmp_path / "README.md").exists()


# --------------------------- files that belong to nobody: the devops agent

def test_the_scaffolding_files_have_an_owner():
    """Regression: a step asked backend to create package.json and .gitignore.
    Neither was in any agent's remit, so every write was refused and the run
    halted after burning its loop limit."""
    devops = BUILTIN_ROLES["devops"]
    for path in ["package.json", ".gitignore", "README.md", "Dockerfile",
                 "tsconfig.json", "requirements.txt", ".github/workflows/ci.yml"]:
        assert devops.may_write(path), path


def test_devops_does_not_become_a_way_around_every_remit():
    """A role that may write anything is a bypass the orchestrator will reach
    for the moment a step is awkward."""
    devops = BUILTIN_ROLES["devops"]
    for path in ["server/routes/data.js", "src/App.tsx", "services/binance.js",
                 "tests/test_api.py"]:
        assert not devops.may_write(path), path


def test_the_orchestrator_is_told_what_each_agent_may_write(tmp_path, monkeypatch):
    """It cannot plan around a remit it cannot see — which is how a scaffolding
    task got assigned to backend."""
    from trance.agents import orchestrator
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None, **kwargs):
            captured["system"] = messages[0]["content"]
            return ChatResponse(text="ok")

    monkeypatch.setattr(orchestrator, "client_for", lambda config: FakeClient())
    orchestrator.chat(messages=[{"role": "user", "content": "build a node app"}],
                      project_dir=tmp_path, config=ModelConfig(), bus=EventBus(),
                      session_id="s")

    assert "may write: package.json" in captured["system"]
    assert "REFUSED by the system" in captured["system"]
    assert "devops" in captured["system"]


def test_a_step_refused_by_the_remit_says_who_owns_the_files(tmp_path, monkeypatch):
    """'Raise the loop limit' is the wrong advice when the writes are refused
    rather than failing — no number of loops makes them land."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "devops"])
    step = Step(role="backend", task="create package.json and .gitignore", max_loops=2)

    def fake(**kw):
        turn = _Turn(None, "could not write them", outcome=("FAILED", "refused"))
        turn.remit_violations = ["package.json", ".gitignore"]
        return turn

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert step.status == "failed"
    halted = next(e for e in engine.bus.history(engine.session.id)
                  if e.type == "run_halted")
    assert "package.json" in halted.payload["message"]
    assert "devops owns" in halted.payload["hint"]
    assert "Looping cannot fix this" in halted.payload["hint"]


def test_the_owner_is_found_even_when_it_is_not_on_the_team(tmp_path, monkeypatch):
    """Answering 'unassigned' when the answer is 'add devops' helps nobody."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])          # no devops on this team
    assert engine._owner_of(".gitignore") == "devops"
    assert engine._owner_of("server/app.js") == "backend"
    assert engine._owner_of("nothing/owns/this.weird") is None


def test_a_manifest_has_exactly_one_owner():
    """Two owners for package.json is how a dependency gets added by one agent
    and overwritten by the other."""
    owners = [name for name, role in BUILTIN_ROLES.items() if role.may_write("package.json")]
    assert owners == ["devops"]


def test_an_empty_memory_says_so_rather_than_being_hidden(tmp_path, monkeypatch):
    """Regression: the section was omitted when empty, so an agent could not
    tell 'nothing recorded' from 'not shown' — one declined to write a note
    because 'the existing memory notes already cover' it, with none existing."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None, **kwargs):
            captured["prompt"] = messages[-1]["content"]
            return ChatResponse(text="OUTCOME: SUCCESS")

    monkeypatch.setattr(runner, "client_for", lambda config: FakeClient())
    runner.run_agent(role=BUILTIN_ROLES["backend"], task="t", project=tmp_path,
                     config=ModelConfig(), bus=EventBus(), session_id="s", step_id="st")

    assert "## Project memory" in captured["prompt"]
    assert "empty — nothing has been written down yet" in captured["prompt"]


def test_memory_is_shown_before_the_toolset(tmp_path, monkeypatch):
    """The decisions constrain the work; the toolset only constrains how."""
    from trance.agents import runner
    from trance.agents.memory import ProjectMemory
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    ProjectMemory(tmp_path).note("backend", "the port is 3100")
    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None, **kwargs):
            captured["prompt"] = messages[-1]["content"]
            return ChatResponse(text="OUTCOME: SUCCESS")

    monkeypatch.setattr(runner, "client_for", lambda config: FakeClient())
    runner.run_agent(role=BUILTIN_ROLES["backend"], task="t", project=tmp_path,
                     config=ModelConfig(), bus=EventBus(), session_id="s", step_id="st")

    prompt = captured["prompt"]
    assert prompt.index("## Project memory") < prompt.index("## Your permissions")
    assert prompt.index("## Your task") < prompt.index("## Project memory")


# -------------------------- an outcome line that states neither verdict

def test_a_success_worded_without_the_keyword_is_not_read_as_failure():
    """Regression: "OUTCOME: Verified index.html is complete and correct — no
    changes needed" was filed as FAILED with itself as the reason, so a step
    that went fine looped and eventually halted the run."""
    from trance.agents.runner import AgentTurn

    turn = AgentTurn(text="OUTCOME: Verified `server/public/index.html` is complete "
                          "and correct — no changes needed.")
    state, detail = turn.outcome
    assert state == "UNCLEAR"          # not FAILED, and not silently SUCCESS
    assert turn.needs_outcome is True


def test_common_synonyms_are_accepted_without_another_round():
    from trance.agents.runner import AgentTurn

    for line in ["OUTCOME: SUCCESS", "OUTCOME: DONE", "OUTCOME: Complete",
                 "OUTCOME: ok", "OUTCOME: PASSED", "OUTCOME: SUCCESS — with a note"]:
        assert AgentTurn(text=line).outcome[0] == "SUCCESS", line

    for line in ["OUTCOME: FAILED — port taken", "OUTCOME: ERROR", "OUTCOME: blocked",
                 "OUTCOME: incomplete — ran out of time"]:
        assert AgentTurn(text=line).outcome[0] == "FAILED", line


def test_a_verdict_word_only_counts_as_the_first_word():
    """"the feature is not complete" contains "complete"; reading that as
    success is the one mistake this mechanism exists to prevent."""
    from trance.agents.runner import AgentTurn

    assert AgentTurn(text="OUTCOME: the feature is not complete").outcome[0] == "UNCLEAR"
    assert AgentTurn(text="OUTCOME: nothing was done successfully").outcome[0] == "UNCLEAR"


def test_an_unreadable_outcome_is_asked_again_and_then_accepted(tmp_path, monkeypatch):
    from trance.providers.base import ChatResponse

    replies = [
        ChatResponse(text="Verified the file.\n\nOUTCOME: Verified index.html is "
                          "complete and correct — no changes needed."),
        ChatResponse(text="OUTCOME: SUCCESS"),
    ]
    turn, prompts = _turn_with(monkeypatch, replies, project=tmp_path)

    assert any("did not say SUCCESS or FAILED" in p for p in prompts)
    assert turn.outcome[0] == "SUCCESS"


def test_an_outcome_that_stays_unreadable_fails_with_that_as_the_reason(tmp_path, monkeypatch):
    """Fail closed — a result nobody can read is not evidence of success — but
    do not present the agent's prose as if it were a failure reason."""
    from trance.providers.base import ChatResponse

    replies = [ChatResponse(text="OUTCOME: Verified and looking good"),
               ChatResponse(text="It all looks fine to me, honestly.")]
    turn, prompts = _turn_with(monkeypatch, replies, project=tmp_path)

    state, reason = turn.outcome
    assert state == "FAILED"
    assert "asked twice" in reason and "no readable result" in reason
    assert "looking good" not in reason


def test_the_re_ask_says_that_no_changes_needed_is_success(tmp_path, monkeypatch):
    """The observed case: the agent found the work already correct and had no
    word for it."""
    from trance.providers.base import ChatResponse

    _turn_with(monkeypatch, [ChatResponse(text="OUTCOME: already fine"),
                             ChatResponse(text="OUTCOME: SUCCESS")], project=tmp_path)


# ------------------------------ asking instead of refusing outright

def test_a_refused_write_asks_before_it_refuses(tmp_path):
    """The tester needed jest.config.js, nobody had given it one, and the
    refusal cost a whole step. A remit is a good default and a bad absolute."""
    import threading

    from trance.agents.approval import ONCE, ApprovalBroker

    broker = ApprovalBroker(on_request=lambda r: threading.Timer(
        0.01, broker.resolve, args=(r.id, ONCE)).start())
    tools = _tools_with_approval(tmp_path, "tester", broker)

    outcome = tools.call("write_file", {"path": "jest.config.js", "content": "module.exports={}"})
    assert outcome.ok is True
    assert (tmp_path / "jest.config.js").read_text() == "module.exports={}"
    assert outcome.remit_violation is None


def test_a_denied_write_still_refuses_exactly_as_before(tmp_path):
    import threading

    from trance.agents.approval import DENY, ApprovalBroker

    broker = ApprovalBroker(on_request=lambda r: threading.Timer(
        0.01, broker.resolve, args=(r.id, DENY)).start())
    tools = _tools_with_approval(tmp_path, "tester", broker)

    outcome = tools.call("write_file", {"path": "jest.config.js", "content": "x"})
    assert outcome.ok is False
    assert outcome.remit_violation == "jest.config.js"
    assert not (tmp_path / "jest.config.js").exists()


def test_a_refused_command_asks_too(tmp_path):
    import threading

    from trance.agents.approval import ONCE, ApprovalBroker

    asked = []
    broker = ApprovalBroker(on_request=lambda r: (asked.append(r), threading.Timer(
        0.01, broker.resolve, args=(r.id, ONCE)).start()))
    role = copy.deepcopy(BUILTIN_ROLES["tester"])
    role.commands = ["echo"]                       # npx is not on its list
    tools = _tools_with_approval(tmp_path, role, broker)

    outcome = tools.call("run_command", {"command": "npx --version"})
    assert asked and asked[0].kind == "command"
    assert "npx" in asked[0].detail["programs"]
    assert outcome.detail.get("kind") == "command"        # it actually ran


def test_no_answer_in_time_denies(tmp_path):
    """Blocking a worker on a human is only safe with a way out."""
    from trance.agents.approval import ApprovalBroker

    broker = ApprovalBroker(on_request=lambda r: None, timeout_s=0.05)
    tools = _tools_with_approval(tmp_path, "tester", broker)

    outcome = tools.call("write_file", {"path": "jest.config.js", "content": "x"})
    assert outcome.ok is False and outcome.remit_violation == "jest.config.js"


def test_stopping_releases_an_agent_waiting_on_an_answer(tmp_path):
    """Otherwise the engine cannot reach its own stop check."""
    import threading
    import time

    from trance.agents.approval import ApprovalBroker

    broker = ApprovalBroker(on_request=lambda r: None, timeout_s=30)
    tools = _tools_with_approval(tmp_path, "tester", broker)
    result = {}

    worker = threading.Thread(
        target=lambda: result.update(
            outcome=tools.call("write_file", {"path": "jest.config.js", "content": "x"})))
    worker.start()
    for _ in range(200):
        if broker.pending():
            break
        time.sleep(0.01)

    broker.abandon()
    worker.join(timeout=5)
    assert not worker.is_alive()                   # not stuck for 30s
    assert result["outcome"].ok is False


def test_asking_can_be_turned_off_entirely(tmp_path):
    """An unattended run wants the old behaviour, immediately."""
    from trance.agents.approval import ApprovalBroker

    asked = []
    broker = ApprovalBroker(on_request=lambda r: asked.append(r), enabled=False)
    tools = _tools_with_approval(tmp_path, "tester", broker)

    outcome = tools.call("write_file", {"path": "jest.config.js", "content": "x"})
    assert outcome.ok is False and asked == []


def test_always_writes_the_answer_into_the_policy(tmp_path):
    """"always" has to mean it — the same question must not come back next step."""
    import threading
    import time

    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={"steps": [{"role": "tester", "task": "t"}]})
    client.post(f"/api/sessions/{sid}/start")
    app.state.store.get(sid).stop()

    broker = app.state.brokers[sid]
    answers = {}

    def ask():
        answers["request"] = broker.ask(
            kind="write", agent="tester", session_id=sid, step_id="st",
            subject="jest.config.js", detail={"remit": ["tests/**"]})

    worker = threading.Thread(target=ask)
    worker.start()
    for _ in range(200):
        if broker.pending():
            break
        time.sleep(0.01)

    pending = client.get(f"/api/sessions/{sid}/approvals").json()["pending"]
    assert [r["subject"] for r in pending] == ["jest.config.js"]

    body = client.post(f"/api/sessions/{sid}/approvals/{pending[0]['id']}",
                       json={"decision": "always"}).json()
    worker.join(timeout=5)

    assert body["widened"] is True
    assert answers["request"].allowed is True
    # The exact path, not a widened glob: the user allowed this file.
    tester = next(r for r in client.get("/api/agents").json()["agents"]
                  if r["name"] == "tester")
    assert "jest.config.js" in tester["paths"]


def test_an_unknown_approval_is_a_404_not_a_hang(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]

    gone = client.post(f"/api/sessions/{sid}/approvals/ap_nope", json={"decision": "once"})
    assert gone.status_code == 404 and "timed out" in gone.json()["detail"]
    bad = client.post(f"/api/sessions/{sid}/approvals/ap_1", json={"decision": "maybe"})
    assert bad.status_code == 400


# ------------------ chat habits leaking into a tool argument

def test_a_filename_header_is_stripped_before_the_file_is_written(tmp_path):
    """Regression: agents prefix files with `# server/app.js`, which in a .js
    file is a syntax error rather than a blemish."""
    tools = _tools(tmp_path)
    outcome = tools.call("write_file", {
        "path": "server/app.js", "content": "# server/app.js\nconst x = 1;\n"})

    assert (tmp_path / "server/app.js").read_text() == "const x = 1;\n"
    assert "removed" in outcome.text          # and the agent is told, so it learns
    assert outcome.detail["stripped"]


def test_a_code_fence_around_the_whole_file_is_unwrapped(tmp_path):
    tools = _tools(tmp_path)
    tools.call("write_file", {
        "path": "server/app.js", "content": "```javascript\nconst x = 1;\n```"})
    assert (tmp_path / "server/app.js").read_text() == "const x = 1;"


def test_stripping_never_touches_real_code():
    """Being wrong here silently deletes someone's first line."""
    from trance.agents.tools import strip_wrappers

    for text, rel in [
        ("#!/usr/bin/env python3\nprint(1)\n", "run.py"),        # a shebang
        ("# Copyright 2026 Acme\nprint(1)\n", "run.py"),         # a real comment
        ("# server/other.js\nconst x = 1;\n", "server/app.js"),  # a different file
        ("const x = 1;\n", "app.js"),
        ("// eslint-disable-next-line\nconst x = 1;\n", "app.js"),
    ]:
        assert strip_wrappers(text, rel) == (text, []), rel


def test_a_markdown_file_keeps_its_fences(tmp_path):
    """A fence in a README is content, not a wrapper."""
    tools = _tools(tmp_path, "devops")
    body = "```bash\nnpm test\n```"
    tools.call("write_file", {"path": "README.md", "content": body})
    assert (tmp_path / "README.md").read_text() == body


# ---------------------------------- one last try on a stronger model

def _escalating_engine(tmp_path, team, preset="big"):
    from trance.providers.base import ModelPreset, ProviderConfig

    engine = _engine(tmp_path, team)
    engine.config.providers["p"] = ProviderConfig(name="p", kind="llamacpp")
    engine.config.presets[preset] = ModelPreset(name=preset, provider="p", model="big-model")
    engine.config.escalation_preset = preset
    return engine


def test_an_exhausted_block_gets_one_try_on_the_stronger_model(tmp_path, monkeypatch):
    """Regression: 16 attempts on the same model, all failing the same way. The
    loop varies the prompt and the fixer; it never varies the model."""
    from trance.flow import Step

    engine = _escalating_engine(tmp_path, ["backend", "factchecker"])
    step = Step(role="backend", task="fix the socket handling", max_loops=2)
    models = []

    def fake(**kw):
        models.append(kw["config"].model)
        succeed = kw["config"].model == "big-model"
        return _Turn("PASS", "done" if succeed else "still broken",
                     outcome=("SUCCESS", "") if succeed else ("FAILED", "socket hangs"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert models[-1] == "big-model"
    assert step.status == "done"
    assert step.escalated is True
    assert engine.session.status != "error"      # the run was not halted


def test_the_escalated_attempt_is_shown_every_earlier_failure(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _escalating_engine(tmp_path, ["backend"])
    step = Step(role="backend", task="fix the socket handling", max_loops=2)
    tasks = []

    def fake(**kw):
        tasks.append(kw["task"])
        big = kw["config"].model == "big-model"
        return _Turn(None, "x", outcome=("SUCCESS", "") if big else ("FAILED", "socket hangs"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    brief = tasks[-1]
    assert "already failed 2 times" in brief
    assert "socket hangs" in brief
    assert "Do not repeat the approach that failed" in brief


def test_escalation_happens_once_and_then_the_run_halts(tmp_path, monkeypatch):
    """Escalation that can loop is just a longer loop with a bigger bill."""
    from trance.flow import Step

    engine = _escalating_engine(tmp_path, ["backend"])
    step = Step(role="backend", task="fix it", max_loops=2)
    calls = []

    def fake(**kw):
        calls.append(kw["config"].model)
        return _Turn(None, "no", outcome=("FAILED", "still broken"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert calls.count("big-model") == 1
    assert step.status == "failed"
    assert engine.session.status == "error"


def test_without_an_escalation_model_nothing_changes(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])          # no escalation configured
    step = Step(role="backend", task="fix it", max_loops=2)
    calls = []

    def fake(**kw):
        calls.append(kw["config"].model)
        return _Turn(None, "no", outcome=("FAILED", "still broken"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert len(calls) == 2 and step.status == "failed"
    assert step.escalated is False


def test_compaction_never_rewrites_what_the_user_wrote(tmp_path):
    """A user note is an instruction, not an observation. Handing it to a model
    to "merge and shorten" is how a deliberate correction gets summarised away
    by the agents it was meant to correct."""
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    _fill(memory, 20)
    memory.note("user", "THE TEST SUITE PASSES — stop trying to fix a socket bug")
    memory.note("backend", "one more agent observation")

    seen = {}

    def rewrite(text):
        seen["text"] = text
        return "- **backend**: everything merged into one line"

    result = memory.compact(rewrite)
    assert result["compacted"] is True

    assert "THE TEST SUITE PASSES" not in seen["text"]      # never sent to the model
    notes = memory.notes()
    assert any("THE TEST SUITE PASSES" in n for n in notes)  # and still there
    assert notes[0].startswith("- **user**")                # kept, and leading


def test_a_memory_of_only_user_notes_is_left_alone(tmp_path):
    from trance.agents.memory import ProjectMemory

    memory = ProjectMemory(tmp_path)
    for i in range(30):
        memory.note("user", f"instruction {i} that the agents must follow exactly")

    result = memory.compact(lambda text: "- **backend**: nope")
    assert result["compacted"] is False
    assert len(memory.notes()) == 30


# ------------------------------- a lookup that found nothing is not a refusal

def test_a_graph_miss_is_not_reported_as_a_refusal(tmp_path):
    from trance.agents.tools import AgentTools

    """Regression: search_symbols misses rendered as "search_symbols refused",
    in red, next to writes that had actually been blocked."""
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import ContextTools

    (tmp_path / "app.py").write_text("def charge(order):\n    return 1\n")
    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)
    tools = AgentTools(tmp_path, BUILTIN_ROLES["backend"], ContextTools(db, tmp_path),
                       notify=lambda *a, **k: None)

    miss = tools.call("search_symbols", {"pattern": "nothing_like_this"})
    assert miss.detail["kind"] == "graph"      # the UI keys off this, not off ok
    assert miss.detail["hit"] is False

    hit = tools.call("search_symbols", {"pattern": "charge"})
    assert hit.detail["hit"] is True and "charge" in hit.text


def test_a_phrase_search_explains_itself(tmp_path):
    """The model was passing test descriptions — "SSE done event equity curve"
    — and a bare "no symbols match" just invited another sentence."""
    from trance.worker.tools import _no_match

    message = _no_match("SSE done event equity curve")
    assert "not a full-text or semantic search" in message
    assert "'equity'" in message                  # a concrete next move
    assert "read_file" in message

    single = _no_match("streamBacktst")
    assert "full-text" not in single              # not the same advice
    assert "project map" in single


def test_the_tool_description_says_it_matches_names(tmp_path):
    from trance.worker.tools import specs

    search = next(s for s in specs() if s["function"]["name"] == "search_symbols")
    assert "not a text search" in search["function"]["description"]
    assert "no spaces" in (search["function"]["parameters"]["properties"]["pattern"]
                           ["description"])


# ------------------------- the biggest thing in the window is your own output

def test_a_file_already_written_stops_costing_context():
    """An agent that writes three 10KB files has 30KB of its own output pinned
    in the conversation forever — the assistant messages holding those calls are
    never trimmed, and the bytes are already on disk."""
    import json

    from trance.agents.runner import WRITTEN, shrink_written_files

    def write_call(path, size):
        return {"role": "assistant", "tool_calls": [{
            "id": "c", "function": {"name": "write_file", "arguments": json.dumps(
                {"path": path, "content": "x" * size})}}]}

    messages = [
        {"role": "user", "content": "task"},
        write_call("a.js", 10000),
        {"role": "tool", "content": "Created a.js"},
        write_call("b.js", 10000),
        {"role": "tool", "content": "Created b.js"},
        write_call("c.js", 10000),
    ]
    before = len(str(messages))
    shrunk = shrink_written_files(messages)

    assert shrunk == 2                       # the most recent one is left intact
    assert len(str(messages)) < before - 19000
    args = json.loads(messages[1]["tool_calls"][0]["function"]["arguments"])
    assert args["content"] == WRITTEN and args["path"] == "a.js"   # the path stays
    last = json.loads(messages[5]["tool_calls"][0]["function"]["arguments"])
    assert last["content"] == "x" * 10000


def test_written_files_are_given_up_before_tool_results():
    """A file on disk can be read back exactly; a command's output cannot."""
    import json

    from trance.agents.runner import WRITTEN, fit_context

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "tool_calls": [{"id": "c", "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": "a.js", "content": "x" * 40000})}}]},
        {"role": "tool", "content": "the test output that matters"},
        {"role": "assistant", "tool_calls": [{"id": "d", "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": "b.js", "content": "y" * 400})}}]},
    ]
    fitted, dropped = fit_context(messages, budget=2000)

    shrunk = json.loads(messages[2]["tool_calls"][0]["function"]["arguments"])
    assert shrunk["content"] == WRITTEN
    assert messages[3]["content"] == "the test output that matters"   # kept


def test_the_estimate_is_calibrated_against_the_endpoints_own_count(tmp_path, monkeypatch):
    """Trimming against a guess is how a "55k" prompt arrived as 61k and filled
    the window it was supposed to stay inside."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse, ToolCall

    sent = []

    class DenseCounter:
        """An endpoint whose tokenizer runs at 2 chars/token, not 3.5."""

        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None, **kwargs):
            chars = sum(len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
                        for m in messages)
            sent.append(chars)
            self.n += 1
            usage = {"prompt_tokens": chars // 2}
            if self.n < 3:
                return ChatResponse(text="", usage=usage, tool_calls=[ToolCall(
                    id=f"c{self.n}", name="read_file", arguments={"path": "big.txt"})])
            return ChatResponse(text="OUTCOME: SUCCESS", usage=usage)

    (tmp_path / "big.txt").write_text("z" * 20000)
    monkeypatch.setattr(runner, "client_for", lambda config: DenseCounter())
    runner.run_agent(role=BUILTIN_ROLES["backend"], task="t", project=tmp_path,
                     config=ModelConfig(context_window=20000, max_tokens=4000),
                     bus=EventBus(), session_id="s", step_id="st")

    # Budget is 20000-4000-1000 = 15000 tokens; at the endpoint's real 2
    # chars/token that is 30000 chars. The last prompt must respect the real
    # ratio, not the 3.5 guess (which would have allowed 52500 chars).
    assert sent[-1] <= 30000 * 1.05


# ------------------------- the same lookup, over and over, in one turn

def _reader(monkeypatch, tmp_path, reads, final="OUTCOME: SUCCESS"):
    """Drive an agent that issues the given read_file calls, one per round."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse, ToolCall

    results = []

    class Repeater:
        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None, **kwargs):
            results.append([m for m in messages if m.get("role") == "tool"])
            if self.n < len(reads):
                args = reads[self.n]
                self.n += 1
                return ChatResponse(text="", tool_calls=[
                    ToolCall(id=f"c{self.n}", name="read_file", arguments=args)])
            return ChatResponse(text=final)

    monkeypatch.setattr(runner, "client_for", lambda config: Repeater())
    turn = runner.run_agent(role=BUILTIN_ROLES["backend"], task="t", project=tmp_path,
                            config=ModelConfig(), bus=EventBus(),
                            session_id="s", step_id="st")
    return turn, results


def test_re_reading_the_same_file_points_at_the_copy_already_in_context(tmp_path, monkeypatch):
    """Regression: the frontend agent read tests/integration.test.js five times
    in one minute — five copies of a 24KB page in a 64k window."""
    (tmp_path / "big.js").write_text("const x = 1;\n" * 2000)
    turn, rounds = _reader(monkeypatch, tmp_path, [{"path": "big.js"}] * 4)

    assert turn.deduped_lookups == 3          # the first read is the real one
    contents = [m["content"] for m in rounds[-1]]
    assert sum("const x = 1;" in c for c in contents) == 1
    assert sum("already ran this exact lookup" in c for c in contents) == 3


def test_a_file_that_changed_is_read_again_in_full(tmp_path, monkeypatch):
    """Pointing at a stale copy would be worse than the duplication."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse, ToolCall

    path = tmp_path / "server" / "app.py"
    path.parent.mkdir(parents=True)
    path.write_text("first version\n")

    class Rewriter:
        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None, **kwargs):
            self.n += 1
            if self.n == 1:
                return ChatResponse(text="", tool_calls=[ToolCall(
                    id="c1", name="read_file", arguments={"path": "server/app.py"})])
            if self.n == 2:
                return ChatResponse(text="", tool_calls=[ToolCall(
                    id="c2", name="write_file",
                    arguments={"path": "server/app.py",
                               "content": "second version, much longer now\n"})])
            if self.n == 3:
                return ChatResponse(text="", tool_calls=[ToolCall(
                    id="c3", name="read_file", arguments={"path": "server/app.py"})])
            self.last = messages
            return ChatResponse(text="OUTCOME: SUCCESS")

    client = Rewriter()
    monkeypatch.setattr(runner, "client_for", lambda config: client)
    turn = runner.run_agent(role=BUILTIN_ROLES["backend"], task="t", project=tmp_path,
                            config=ModelConfig(), bus=EventBus(),
                            session_id="s", step_id="st")

    assert turn.deduped_lookups == 0
    assert any("second version" in str(m.get("content")) for m in client.last)


def test_a_different_page_of_the_same_file_is_not_deduped(tmp_path, monkeypatch):
    (tmp_path / "big.js").write_text("\n".join(f"line {i}" for i in range(3000)))
    turn, rounds = _reader(monkeypatch, tmp_path,
                           [{"path": "big.js"}, {"path": "big.js", "start_line": 900}])

    assert turn.deduped_lookups == 0
    contents = " ".join(m["content"] for m in rounds[-1])
    assert "line 0" in contents and "line 900" in contents


def test_a_command_is_never_deduped(tmp_path, monkeypatch):
    """Running the suite twice is a different question, not a repeat."""
    from trance.agents.runner import _lookup_key
    from trance.agents.tools import ToolOutcome

    outcome = ToolOutcome("out", detail={"kind": "command"})
    assert _lookup_key("run_command", {"command": "npm test"}, outcome) is None
    assert _lookup_key("write_file", {"path": "a.js"}, outcome) is None
    assert _lookup_key("read_file", {"path": "a.js"}, outcome) is not None


def test_a_trimmed_result_may_be_fetched_again(tmp_path, monkeypatch):
    """Once its copy is gone from the window, re-reading is the right move."""
    from trance.agents.runner import TRIMMED

    assert "call the tool again" not in TRIMMED       # no longer an invitation
    assert "only fetch this again if you cannot proceed" in TRIMMED


# ------------------- large files answer with their shape, not their bulk

def _indexed_tools(tmp_path, role="frontend"):
    from trance.agents.tools import AgentTools
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import ContextTools

    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)
    return AgentTools(tmp_path, BUILTIN_ROLES[role], ContextTools(db, tmp_path),
                      notify=lambda *a, **k: None)


def test_a_large_indexed_file_returns_an_outline(tmp_path):
    """A 10KB file is ~2,600 tokens; its outline is ~150, and the agent nearly
    always wanted one function out of it."""
    body = "import os\n\n" + "\n\n".join(
        f"def handler_{i}(request):\n" + "    pass\n" * 40 for i in range(12))
    (tmp_path / "app.py").write_text(body)
    assert len(body) > 4000

    outcome = _indexed_tools(tmp_path).call("read_file", {"path": "app.py"})

    assert outcome.detail["outline"] is True
    assert "handler_0" in outcome.text and "handler_11" in outcome.text
    assert "import os" in outcome.text            # the top of the file comes too
    assert "pass" not in outcome.text             # but not the bodies
    assert len(outcome.text) < len(body) // 4


def test_a_small_file_still_comes_back_whole(tmp_path):
    (tmp_path / "app.py").write_text("def charge(order):\n    return order.total\n")
    outcome = _indexed_tools(tmp_path).call("read_file", {"path": "app.py"})
    assert not outcome.detail.get("outline")
    assert "return order.total" in outcome.text


def test_a_file_with_nothing_indexed_comes_back_whole(tmp_path):
    """An outline of nothing would be worse than the file."""
    (tmp_path / "data.json").write_text('{"a": ' + '"x",' * 2000 + '"b": 1}')
    outcome = _indexed_tools(tmp_path).call("read_file", {"path": "data.json"})
    assert not outcome.detail.get("outline")


def test_full_true_is_the_way_to_rewrite_a_file(tmp_path):
    """An agent about to rewrite a file genuinely needs every line of it."""
    body = "import os\n\n" + "\n\n".join(
        f"def handler_{i}(request):\n" + "    pass\n" * 40 for i in range(12))
    (tmp_path / "app.py").write_text(body)
    tools = _indexed_tools(tmp_path)

    assert tools.call("read_file", {"path": "app.py"}).detail["outline"] is True
    whole = tools.call("read_file", {"path": "app.py", "full": True})
    assert not whole.detail.get("outline")
    assert whole.text.count("def handler_") == 12


def test_paging_past_line_one_is_never_an_outline(tmp_path):
    body = "import os\n\n" + "\n\n".join(
        f"def handler_{i}(request):\n" + "    pass\n" * 40 for i in range(12))
    (tmp_path / "app.py").write_text(body)

    page = _indexed_tools(tmp_path).call("read_file", {"path": "app.py", "start_line": 200})
    assert not page.detail.get("outline")
    assert "pass" in page.text


def test_an_agent_without_the_graph_reads_whole_files(tmp_path):
    """The outline comes from the index; with no index there is nothing to
    offer instead."""
    body = "def a():\n" + "    pass\n" * 900
    (tmp_path / "app.py").write_text(body)
    tools = _tools(tmp_path)                       # no graph tools attached
    assert not tools.call("read_file", {"path": "app.py"}).detail.get("outline")


# ============================== loops: a reusable block of agents

def _loop_engine(tmp_path, team, loop):
    from trance.agents.store import LoopStore

    engine = _engine(tmp_path, team)
    store = LoopStore(tmp_path / "loops.json", seed=False)
    store.upsert(loop)
    engine.loops = store
    return engine


def _tf_loop(**kw):
    """tester → (fail) → backend → (success) → tester, exit when tests pass."""
    from trance.loops import (
        CHECK_FAILED, EXIT_LOOP, FAILED, FAIL_LOOP, SUCCESS, Edge, Loop, LoopNode)

    test = LoopNode(id="n_test", role="tester", focus="run the tests",
                    on={SUCCESS: Edge(EXIT_LOOP), FAILED: Edge("n_fix", max_visits=3)})
    fix = LoopNode(id="n_fix", role="backend", focus="fix the code under test",
                   on={SUCCESS: Edge("n_test", max_visits=3), FAILED: Edge(FAIL_LOOP)})
    return Loop(name="test-and-fix", nodes=[test, fix], start="n_test", **kw)


def test_a_loop_walks_from_agent_to_agent_by_outcome(tmp_path, monkeypatch):
    """The shape people build by hand: tester finds a bug, developer fixes it,
    tester runs again."""
    from trance.flow import Step

    engine = _loop_engine(tmp_path, ["tester", "backend"], _tf_loop())
    step = Step(role="", loop="test-and-fix", task="make the paddle bounce")
    order = []

    def fake(**kw):
        name = kw["role"].name
        order.append(name)
        # The tester fails once, the developer fixes, then the tester passes.
        failing = name == "tester" and order.count("tester") == 1
        return _Turn(None, "x", outcome=("FAILED", "ball passes through") if failing
                     else ("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert order == ["tester", "backend", "tester"]
    assert step.status == "done"


def test_each_agent_in_a_loop_gets_the_step_task_and_its_own_focus(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _loop_engine(tmp_path, ["tester", "backend"],
                          _tf_loop(prompt="This block ends when the tests pass."))
    engine.session.goal = "A crypto backtester."
    step = Step(role="", loop="test-and-fix", task="make the paddle bounce")
    seen = {}

    def fake(**kw):
        seen[kw["role"].name] = (kw["task"], kw.get("goal", ""))
        return _Turn(None, "x", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    task, goal = seen["tester"]
    assert "make the paddle bounce" in task        # the step's prompt
    assert "run the tests" in task                 # and this agent's part in it
    assert "A crypto backtester." in goal          # the project
    assert "ends when the tests pass" in goal      # and what the loop is for


def test_a_loop_that_will_not_converge_stops(tmp_path, monkeypatch):
    """max_visits on an edge is what makes a loop finite."""
    from trance.flow import Step

    engine = _loop_engine(tmp_path, ["tester", "backend"], _tf_loop())
    step = Step(role="", loop="test-and-fix", task="t")
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        # Nothing ever gets fixed: the tester always fails, the fixer always
        # claims success, and the pair would run forever.
        return _Turn(None, "x", outcome=("FAILED", "still broken")
                     if kw["role"].name == "tester" else ("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert step.status == "failed"
    assert engine.session.status == "error"        # it halts rather than moving on
    assert 4 <= len(order) <= 10                   # bounded, not forever
    assert any(e.type == "loop_exhausted" for e in engine.bus.history(engine.session.id))


def test_an_unrouted_outcome_ends_the_loop_rather_than_guessing(tmp_path, monkeypatch):
    """A missing exit means the author did not think about it, which is not a
    reason to invent a destination."""
    from trance.flow import Step
    from trance.loops import EXIT_LOOP, SUCCESS, Edge, Loop, LoopNode

    lonely = Loop(name="one-shot", nodes=[LoopNode(
        id="n1", role="backend", on={SUCCESS: Edge(EXIT_LOOP)})])
    engine = _loop_engine(tmp_path, ["backend"], lonely)
    step = Step(role="", loop="one-shot", task="t")

    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: _Turn(None, "x", outcome=("FAILED", "nope")))
    engine._execute(step)
    assert step.status == "failed"


def test_a_failed_check_routes_differently_from_a_failed_outcome(tmp_path, monkeypatch):
    """Three exits, not two: the agent said it worked and the checker disagreed
    is a different situation from the agent saying it failed."""
    from trance.flow import Step
    from trance.loops import CHECK_FAILED, EXIT_LOOP, FAILED, FAIL_LOOP, SUCCESS, Edge, Loop, LoopNode

    loop = Loop(name="checked", start="n1", nodes=[
        LoopNode(id="n1", role="backend", check="factchecker",
                 on={SUCCESS: Edge(EXIT_LOOP), FAILED: Edge(FAIL_LOOP),
                     CHECK_FAILED: Edge("n2", max_visits=2)}),
        LoopNode(id="n2", role="reviewer", on={SUCCESS: Edge(EXIT_LOOP),
                                               FAILED: Edge(FAIL_LOOP)}),
    ])
    engine = _loop_engine(tmp_path, ["backend", "factchecker", "reviewer"], loop)
    step = Step(role="", loop="checked", task="t")
    order = []

    def fake(**kw):
        name = kw["role"].name
        order.append(name)
        if name == "factchecker":
            return _Turn("FAIL", "the file is empty")
        return _Turn(None, "x", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert order[:2] == ["backend", "factchecker"]
    assert "reviewer" in order            # CHECK_FAILED took its own route
    assert step.status == "done"


def test_an_unknown_loop_halts_instead_of_running_nothing(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _loop_engine(tmp_path, ["backend"], _tf_loop())
    step = Step(role="", loop="does-not-exist", task="t")
    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: _Turn(None, "x", outcome=("SUCCESS", "")))

    engine._execute(step)
    assert step.status == "failed" and "unknown loop" in step.summary


def test_a_loop_is_validated_before_it_can_be_saved():
    """A loop that dead-ends is otherwise discovered halfway through a run,
    with the work already done."""
    from trance.loops import EXIT_LOOP, FAILED, SUCCESS, CHECK_FAILED, Edge, Loop, LoopNode, validate

    roles = {"tester", "backend", "factchecker"}
    verifiers = {"factchecker"}

    ok = Loop(name="fine", nodes=[LoopNode(id="a", role="tester",
                                           on={SUCCESS: Edge(EXIT_LOOP)})])
    assert validate(ok, roles, verifiers) is None

    assert "at least one agent" in validate(Loop(name="empty"), roles, verifiers)
    assert "no spaces" in validate(Loop(name="two words", nodes=ok.nodes), roles, verifiers)

    unknown = Loop(name="x", nodes=[LoopNode(id="a", role="nobody",
                                             on={SUCCESS: Edge(EXIT_LOOP)})])
    assert "unknown agent" in validate(unknown, roles, verifiers)

    dangling = Loop(name="x", nodes=[LoopNode(id="a", role="tester",
                                              on={SUCCESS: Edge(EXIT_LOOP),
                                                  FAILED: Edge("gone")})])
    assert "points nowhere" in validate(dangling, roles, verifiers)

    no_exit = Loop(name="x", nodes=[LoopNode(id="a", role="tester",
                                             on={SUCCESS: Edge("a")})])
    assert "exits successfully" in validate(no_exit, roles, verifiers)

    bad_check = Loop(name="x", nodes=[LoopNode(id="a", role="tester", check="backend",
                                               on={SUCCESS: Edge(EXIT_LOOP)})])
    assert "cannot check work" in validate(bad_check, roles, verifiers)

    impossible = Loop(name="x", nodes=[LoopNode(id="a", role="tester",
                                                on={SUCCESS: Edge(EXIT_LOOP),
                                                    CHECK_FAILED: Edge(EXIT_LOOP)})])
    assert "can never happen" in validate(impossible, roles, verifiers)


def test_the_seeded_loop_is_valid_and_describes_the_common_shape(tmp_path):
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.agents.store import LoopStore
    from trance.loops import validate

    loops = LoopStore(tmp_path / "loops.json").all()
    assert [l.name for l in loops] == ["test-and-fix"]
    assert validate(loops[0], set(R), {n for n, r in R.items() if r.verifier}) is None
    assert loops[0].roles() == ["tester", "backend"]


def test_a_flow_step_can_name_a_loop_and_pulls_in_its_agents(tmp_path):
    """A loop calling an agent the session has never heard of fails at run
    time, after everything before it has run."""
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)

    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    body = client.put(f"/api/sessions/{sid}/flow", json={"steps": [
        {"role": "", "loop": "test-and-fix", "task": "make it bounce"}]}).json()

    assert body["steps"][0]["runs_a_loop"] is True
    team = {r["name"] for r in body["team"]}
    assert {"tester", "backend"} <= team

    bad = client.put(f"/api/sessions/{sid}/flow",
                     json={"steps": [{"role": "", "loop": "nope", "task": "t"}]})
    assert bad.status_code == 400 and "unknown loop" in bad.json()["detail"]


def test_a_loop_in_use_cannot_be_deleted(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow",
               json={"steps": [{"role": "", "loop": "test-and-fix", "task": "t"}]})

    blocked = client.delete("/api/loops/test-and-fix")
    assert blocked.status_code == 409 and "used by" in blocked.json()["detail"]


def test_an_invalid_loop_is_refused_by_the_api(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    response = client.put("/api/loops/broken", json={"nodes": [
        {"id": "a", "role": "tester", "on": {"SUCCESS": {"target": "a"}}}]})
    assert response.status_code == 400
    assert "exits successfully" in response.json()["detail"]
