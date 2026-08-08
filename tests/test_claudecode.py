"""Claude Code as a model backend.

Everything here runs against a fake `claude` binary. The real one costs money
and needs a login, and neither belongs in a test suite.
"""

from __future__ import annotations

import json

import pytest

from trance.config import ModelConfig
from trance.providers.base import BackendError
from trance.providers.claudecode_client import ClaudeCodeClient

TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


def fake_cli(monkeypatch, result: str, *, usage: dict | None = None,
             code: int = 0, is_error: bool = False, capture: dict | None = None):
    """Stand in for `claude -p`, recording how it was called."""
    import subprocess

    monkeypatch.setattr("trance.providers.claudecode_client.shutil.which",
                        lambda _: "/usr/bin/claude")

    class Done:
        returncode = code
        stdout = json.dumps({"result": result, "is_error": is_error,
                             "usage": usage or {"input_tokens": 10, "output_tokens": 3},
                             "stop_reason": "end_turn"})
        stderr = ""

    def run(command, **kwargs):
        if capture is not None:
            capture["command"] = command
            capture["kwargs"] = kwargs
        return Done()

    monkeypatch.setattr(subprocess, "run", run)


def test_claude_codes_own_preamble_and_tools_are_turned_off(monkeypatch):
    """Measured, not assumed: the defaults carry ~13,100 tokens of instructions
    trance did not write, and its built-in tools would edit the project behind
    every remit and counter trance keeps."""
    seen: dict = {}
    fake_cli(monkeypatch, "OK", capture=seen)

    ClaudeCodeClient(ModelConfig(kind="claudecode", model="opus")).complete(
        [{"role": "system", "content": "You are the backend agent."},
         {"role": "user", "content": "hello"}])

    command = seen["command"]
    assert "--tools" in command and command[command.index("--tools") + 1] == ""
    assert "--system-prompt" in command
    assert "You are the backend agent." in command[command.index("--system-prompt") + 1]
    assert command[command.index("--model") + 1] == "opus"
    assert "--output-format" in command and "json" in command


def test_a_tool_call_comes_back_as_a_call_not_as_an_edit(monkeypatch):
    """The point of this backend: Claude Code answers with what it *would* do,
    and trance executes it — inside the remit, counted, with its own context."""
    fake_cli(monkeypatch, 'I will look.\n```tool_call\n'
                          '{"name": "read_file", "arguments": {"path": "src/main.js"}}\n```')

    response = ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
        [{"role": "user", "content": "what does it export?"}], tools=TOOLS)

    assert response.finish_reason == "tool_calls"
    assert [(c.name, c.arguments) for c in response.tool_calls] == [
        ("read_file", {"path": "src/main.js"})]
    assert response.text == "I will look."          # the block is not left in the prose


def test_several_calls_in_one_turn(monkeypatch):
    fake_cli(monkeypatch,
             '```tool_call\n{"name": "read_file", "arguments": {"path": "a.js"}}\n```\n'
             '```tool_call\n{"name": "read_file", "arguments": {"path": "b.js"}}\n```')
    response = ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
        [{"role": "user", "content": "read both"}], tools=TOOLS)
    assert [c.arguments["path"] for c in response.tool_calls] == ["a.js", "b.js"]
    assert len({c.id for c in response.tool_calls}) == 2      # ids are distinct


def test_a_broken_call_is_reported_rather_than_dropped(monkeypatch):
    """Silence would leave the agent believing it had acted."""
    fake_cli(monkeypatch, '```tool_call\n{"name": "read_file", "arguments": not json}\n```')
    response = ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
        [{"role": "user", "content": "x"}], tools=TOOLS)
    assert len(response.tool_calls) == 1 and response.tool_calls[0].malformed


def test_the_conversation_keeps_who_said_what(monkeypatch):
    """One prompt string is all the CLI takes, so the roles have to survive as
    labels — a tool result is as much part of the conversation as a question."""
    seen: dict = {}
    fake_cli(monkeypatch, "done", capture=seen)

    ClaudeCodeClient(ModelConfig(kind="claudecode")).complete([
        {"role": "system", "content": "You are the backend agent."},
        {"role": "user", "content": "read src/main.js"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "src/main.js"}'}}]},
        {"role": "tool", "name": "read_file", "content": "export function start() {}"},
    ], tools=TOOLS)

    prompt = seen["command"][seen["command"].index("-p") + 1]
    assert "read src/main.js" in prompt
    assert "[you said]" in prompt and "read_file" in prompt
    assert "[result of read_file]" in prompt and "export function start()" in prompt
    # The system prompt is not smuggled into the user turn.
    assert "You are the backend agent." not in prompt


def test_cache_tokens_are_counted_as_input(monkeypatch):
    """They are real input tokens. Leaving them out would make this backend look
    free beside every other one."""
    fake_cli(monkeypatch, "OK", usage={"input_tokens": 2, "cache_creation_input_tokens": 5569,
                                       "cache_read_input_tokens": 7370, "output_tokens": 4})
    response = ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
        [{"role": "user", "content": "hi"}])
    assert response.usage == {"prompt_tokens": 12941, "completion_tokens": 4}


def test_a_cli_that_is_not_there_says_so(monkeypatch):
    monkeypatch.setattr("trance.providers.claudecode_client.shutil.which", lambda _: None)
    with pytest.raises(BackendError) as raised:
        ClaudeCodeClient(ModelConfig(kind="claudecode"))
    assert "not on trance's PATH" in str(raised.value)


def test_the_clis_own_complaint_is_what_reaches_the_user(monkeypatch):
    fake_cli(monkeypatch, "", code=1)
    import subprocess

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Error: not logged in. Run `claude` to authenticate."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(BackendError) as raised:
        ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
            [{"role": "user", "content": "hi"}])
    assert "not logged in" in str(raised.value)


def test_the_kind_is_selectable_and_needs_no_key_or_endpoint():
    from trance.providers.base import KIND_DEFAULTS

    spec = KIND_DEFAULTS["claudecode"]
    assert spec["needs_key"] is False and spec["base_url"] == ""
    assert "opus" in spec["models"]


def test_client_for_routes_to_it(monkeypatch):
    monkeypatch.setattr("trance.providers.claudecode_client.shutil.which",
                        lambda _: "/usr/bin/claude")
    from trance.providers import client_for

    client = client_for(ModelConfig(kind="claudecode"))
    assert type(client).__name__ == "ClaudeCodeClient"


def test_claudes_own_tool_syntax_is_understood_too(monkeypatch):
    """Claude writes tool calls in Claude's syntax because that is what it
    always writes. Insisting on ours and discarding the rest would throw away
    a turn that said exactly what it wanted to do."""
    fake_cli(monkeypatch, "I'll look first.\n"
                          "<invoke_tool>\n<tool_name>read_file</tool_name>\n"
                          '<parameter name="path">src/renderer.ts</parameter>\n'
                          "</invoke_tool>")

    response = ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
        [{"role": "user", "content": "read it"}], tools=TOOLS)

    assert [(c.name, c.arguments) for c in response.tool_calls] == [
        ("read_file", {"path": "src/renderer.ts"})]
    assert response.finish_reason == "tool_calls"
    assert response.text == "I'll look first."      # the block is not left in the prose


def test_the_asked_for_form_wins_when_both_appear(monkeypatch):
    fake_cli(monkeypatch,
             '```tool_call\n{"name": "read_file", "arguments": {"path": "a.js"}}\n```\n'
             "<invoke_tool><tool_name>read_file</tool_name>"
             '<parameter name="path">b.js</parameter></invoke_tool>')
    response = ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
        [{"role": "user", "content": "x"}], tools=TOOLS)
    assert [c.arguments["path"] for c in response.tool_calls] == ["a.js"]


def test_a_turn_that_never_reached_the_model_is_retried(monkeypatch):
    """"is_error, stop_sequence, zero tokens, zero cost" is the CLI giving up
    before it sent anything. Measured at about one call in two, on a
    conversation that then works — so it is retried, not reported."""
    import subprocess

    from trance.providers import claudecode_client as cc

    monkeypatch.setattr(cc.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(cc.time, "sleep", lambda _: None)

    aborted = json.dumps({"is_error": True, "stop_reason": "stop_sequence",
                          "result": "", "usage": {"input_tokens": 0, "output_tokens": 0}})
    answered = json.dumps({"is_error": False, "stop_reason": "end_turn",
                           "result": "OK", "usage": {"input_tokens": 5, "output_tokens": 2}})
    replies = [aborted, aborted, answered]

    def run(command, **kwargs):
        return type("D", (), {"returncode": 0, "stdout": replies.pop(0), "stderr": ""})

    monkeypatch.setattr(subprocess, "run", run)
    response = ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
        [{"role": "user", "content": "hi"}], tools=TOOLS)
    assert response.text == "OK" and not replies       # it took all three


def test_giving_up_says_it_is_the_cli_and_not_you(monkeypatch):
    import subprocess

    from trance.providers import claudecode_client as cc

    monkeypatch.setattr(cc.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(cc.time, "sleep", lambda _: None)
    aborted = json.dumps({"is_error": True, "stop_reason": "stop_sequence",
                          "result": "", "usage": {}})
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: type("D", (), {"returncode": 0, "stdout": aborted,
                                                       "stderr": ""}))
    with pytest.raises(BackendError) as raised:
        ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
            [{"role": "user", "content": "hi"}])
    said = str(raised.value)
    # It names the cause and what to do, rather than blaming the prompt.
    assert "throttling programmatic use" in said
    assert "backup" in said and "no request" in said


def test_a_real_failure_is_not_mistaken_for_an_abort():
    """Time on the wire and money spent are the signal, not tokens: a refused
    request can still report cache reads, while a turn that never left has
    neither."""
    from trance.providers.claudecode_client import _is_abort

    assert _is_abort(json.dumps({"is_error": True, "duration_api_ms": 0,
                                 "total_cost_usd": 0})) is True
    # It reached the model and came back unhappy: that is an answer, not a miss.
    assert _is_abort(json.dumps({"is_error": True, "duration_api_ms": 11345,
                                 "total_cost_usd": 0.03})) is False
    assert _is_abort(json.dumps({"is_error": True, "duration_api_ms": 0,
                                 "total_cost_usd": 0.01})) is False
    assert _is_abort(json.dumps({"is_error": False, "duration_api_ms": 0,
                                 "total_cost_usd": 0})) is False
    assert _is_abort("not json at all") is False


def test_the_cli_is_not_left_waiting_for_input(monkeypatch):
    """It waits three seconds for stdin that is never coming, on every call."""
    import subprocess

    seen: dict = {}
    fake_cli(monkeypatch, "OK", capture=seen)
    ClaudeCodeClient(ModelConfig(kind="claudecode")).complete(
        [{"role": "user", "content": "hi"}])
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL


# ------------------------------------------------- delegating the whole step

def fake_delegate(monkeypatch, result: str, *, num_turns: int = 3,
                  capture: dict | None = None, side_effect=None):
    """Stand in for the CLI only.

    Git has to keep working: this backend is judged by what the diff says it
    changed, so a fake that swallows `git` too would be testing nothing.
    """
    import subprocess

    from trance.agents import delegate

    monkeypatch.setattr("trance.providers.claudecode_client.shutil.which",
                        lambda _: "/usr/bin/claude")
    real_popen = subprocess.Popen
    body = json.dumps({"is_error": False, "result": result,
                       "num_turns": num_turns, "duration_api_ms": 900,
                       "total_cost_usd": 0.02,
                       "usage": {"input_tokens": 6,
                                 "cache_read_input_tokens": 40000,
                                 "output_tokens": 465}})

    class FakeProc:
        """Enough of Popen for the delegate: it runs it, waits, and can kill it."""

        pid = 4242
        returncode = 0

        def communicate(self, timeout=None):
            if side_effect is not None:
                side_effect()
            return body, ""

        def poll(self):
            return 0

        def kill(self):
            pass

    def popen(command, **kwargs):
        if not (command and str(command[0]).endswith("claude")):
            return real_popen(command, **kwargs)
        if capture is not None:
            capture["command"] = command
            capture["cwd"] = kwargs.get("cwd")
            capture["new_session"] = kwargs.get("start_new_session")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", popen)
    return delegate


def test_a_delegated_step_runs_in_the_project_with_edits_allowed(tmp_path, monkeypatch):
    """One call, its own loop, its own tools — because this CLI will not answer
    round by round, and a run is five or more calls a step."""
    from trance import vcs
    from trance.agents.roles import BUILTIN_ROLES
    from trance.config import ModelConfig

    seen: dict = {}
    delegate = fake_delegate(monkeypatch, "did it\n\nOUTCOME: SUCCESS", capture=seen)

    project = tmp_path / "proj"
    (project / "public").mkdir(parents=True)
    (project / "public" / "app.js").write_text("x\n")
    vcs.ensure_repo(project)
    vcs.commit_all(project, "start")

    from trance.events import EventBus

    delegate.run_delegated(
        role=BUILTIN_ROLES["frontend"], task="add stop()", project=project,
        config=ModelConfig(kind="claudecode", model="opus"), bus=EventBus(),
        session_id="s", step_id="st", goal="a web app")

    command = seen["command"]
    assert seen["cwd"] == str(project)              # it works where the project is
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    # trance's tools, not Claude Code's: its own are switched off entirely.
    assert command[command.index("--tools") + 1] == ""
    assert "mcp__trance__write_file" in command and "mcp__trance__read_file" in command
    assert not any(t in command for t in ("Edit", "Write", "Bash"))
    prompt = command[command.index("-p") + 1]
    assert "add stop()" in prompt and "a web app" in prompt
    assert "OUTCOME: SUCCESS" in prompt              # it is told how to report
    assert "src/**" in prompt                        # ...and what it may change


def test_what_it_touched_is_read_from_git_not_from_its_report(tmp_path, monkeypatch):
    """A model saying what it did is a claim. With this backend the diff is the
    only fact available."""
    from trance import vcs
    from trance.agents.roles import BUILTIN_ROLES
    from trance.config import ModelConfig
    from trance.events import EventBus

    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.js").write_text("x\n")
    vcs.ensure_repo(project)
    vcs.commit_all(project, "start")

    # The "model" edits a file behind trance's back, as this backend can, and
    # then reports that it did nothing.
    delegate = fake_delegate(
        monkeypatch, "I changed nothing at all.",
        side_effect=lambda: (project / "src" / "app.js").write_text("edited\n"))

    out = delegate.run_delegated(
        role=BUILTIN_ROLES["frontend"], task="t", project=project,
        config=ModelConfig(kind="claudecode"), bus=EventBus(),
        session_id="s", step_id="st")

    assert out["files_written"] == ["src/app.js"]
    assert out["remit_violations"] == []


def test_writing_outside_the_remit_fails_the_step(tmp_path, monkeypatch):
    """trance cannot prevent it here — Claude Code writes files itself — so it
    is caught afterwards and named, with the checkpoint still behind it."""
    from trance import vcs
    from trance.agents.roles import BUILTIN_ROLES
    from trance.agents.runner import run_agent
    from trance.config import ModelConfig
    from trance.events import EventBus

    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "server").mkdir()
    (project / "src" / "app.js").write_text("x\n")
    (project / "server" / "app.py").write_text("y\n")
    vcs.ensure_repo(project)
    vcs.commit_all(project, "start")

    fake_delegate(monkeypatch, "done\n\nOUTCOME: SUCCESS",
                  side_effect=lambda: (project / "server" / "app.py").write_text(
                      "touched by the wrong agent\n"))

    turn = run_agent(role=BUILTIN_ROLES["frontend"], task="t", project=project,
                     config=ModelConfig(kind="claudecode"), bus=EventBus(),
                     session_id="s", step_id="st")

    assert turn.remit_violations == ["server/app.py"]
    assert turn.outcome[0] == "FAILED"
    assert "outside this agent's remit" in turn.outcome[1]
    # The work is still there to look at, not silently discarded.
    assert (project / "server" / "app.py").read_text().startswith("touched")


def test_an_agent_with_no_remit_is_told_to_change_nothing(tmp_path, monkeypatch):
    from trance.agents.roles import AgentRole
    from trance.config import ModelConfig
    from trance.events import EventBus

    seen: dict = {}
    delegate = fake_delegate(monkeypatch, "OUTCOME: SUCCESS", capture=seen)
    project = tmp_path / "proj"
    project.mkdir()

    reader = AgentRole(name="auditor", title="Auditor", description="reads",
                       system_prompt="p", paths=[], toolsets=["files"])
    delegate.run_delegated(role=reader, task="look at it", project=project,
                           config=ModelConfig(kind="claudecode"), bus=EventBus(),
                           session_id="s", step_id="st")
    prompt = seen["command"][seen["command"].index("-p") + 1]
    assert "read-only" in prompt and "change no files" in prompt


def test_only_this_backend_is_delegated():
    from trance.agents import delegate

    assert delegate.delegated("claudecode") is True
    for other in ("anthropic", "openai", "ollama", "llamacpp", ""):
        assert delegate.delegated(other) is False


def test_the_delegated_agent_is_handed_the_graph(tmp_path, monkeypatch):
    """The one thing delegating would otherwise lose. Claude Code brings grep
    and read-the-whole-file; the index is right there and it cannot see it —
    unless it is offered as an MCP server, which is what MCP is actually for."""
    from trance import vcs
    from trance.agents.roles import BUILTIN_ROLES
    from trance.config import ModelConfig
    from trance.db import GraphDB
    from trance.events import EventBus
    from trance.indexer.service import default_db_path, index_repo

    project = tmp_path / "shop"
    (project / "src").mkdir(parents=True)
    (project / "src" / "cart.js").write_text("export function total(i){ return 1; }\n")
    index_repo(project, GraphDB(default_db_path(project)))
    vcs.ensure_repo(project)
    vcs.commit_all(project, "start")

    seen: dict = {}
    delegate = fake_delegate(monkeypatch, "OUTCOME: SUCCESS", capture=seen)
    delegate.run_delegated(role=BUILTIN_ROLES["frontend"], task="t", project=project,
                           config=ModelConfig(kind="claudecode"), bus=EventBus(),
                           session_id="s", step_id="st")

    command = seen["command"]
    config = json.loads(command[command.index("--mcp-config") + 1])
    server = config["mcpServers"]["trance"]
    assert server["args"][:2] == ["-m", "trance.mcp_server"]
    assert server["args"][2] == str(project)
    assert "--strict-mcp-config" in command      # only ours, not the user's own
    assert "mcp__trance__get_definition" in command
    prompt = command[command.index("-p") + 1]
    assert "call graph" in prompt and "get_callers" in prompt


def test_an_unindexed_project_gets_the_tools_but_not_the_graph(tmp_path, monkeypatch):
    """The file tools always come from trance — that is what makes it a trance
    step. The graph is offered only when there is one: tools that answer "there
    is no index" waste a turn."""
    from trance import vcs
    from trance.agents.roles import BUILTIN_ROLES
    from trance.config import ModelConfig
    from trance.events import EventBus

    project = tmp_path / "fresh"
    project.mkdir()
    vcs.ensure_repo(project)

    seen: dict = {}
    delegate = fake_delegate(monkeypatch, "OUTCOME: SUCCESS", capture=seen)
    delegate.run_delegated(role=BUILTIN_ROLES["frontend"], task="t", project=project,
                           config=ModelConfig(kind="claudecode"), bus=EventBus(),
                           session_id="s", step_id="st")

    command = seen["command"]
    assert "mcp__trance__write_file" in command       # its tools, always
    assert "mcp__trance__get_definition" not in command
    assert "call graph" not in command[command.index("-p") + 1]


def test_stop_kills_a_delegated_step(tmp_path, monkeypatch):
    """Stop aborts every model call a session has open by shutting its socket.
    A delegated step is not a socket, it is a process that runs for minutes —
    so Stop said "stopping after the current agent turn" and then waited for a
    turn nothing could interrupt."""
    import subprocess
    import threading

    from trance.agents import delegate
    from trance.agents.roles import BUILTIN_ROLES
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import Cancelled, abort_inflight

    monkeypatch.setattr("trance.providers.claudecode_client.shutil.which",
                        lambda _: "/usr/bin/claude")
    killed = threading.Event()
    started = threading.Event()

    class SlowProc:
        pid = 4242
        returncode = 0

        def communicate(self, timeout=None):
            started.set()
            killed.wait(5)                     # as if the CLI were thinking
            return "", ""

        def kill(self):
            killed.set()

    # Only the CLI. subprocess.run() uses Popen as a context manager, so a
    # blanket patch breaks every git call this makes before it gets going.
    real_popen = subprocess.Popen

    def popen(command, **kwargs):
        if command and str(command[0]).endswith("claude"):
            return SlowProc()
        return real_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(delegate.os, "killpg",
                        lambda *a: (_ for _ in ()).throw(ProcessLookupError()))

    project = tmp_path / "proj"
    project.mkdir()
    outcome = {}

    def run():
        try:
            delegate.run_delegated(
                role=BUILTIN_ROLES["frontend"], task="t", project=project,
                config=ModelConfig(kind="claudecode"), bus=EventBus(),
                session_id="s1", step_id="st")
        except Cancelled as stopped:
            outcome["stopped"] = str(stopped)
        except Exception as exc:               # noqa: BLE001 — reported below
            outcome["other"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(5), "the delegated call never started"

    assert abort_inflight("s1") == 1           # the stop button's own path
    worker.join(timeout=5)

    assert killed.is_set(), "stopping did not kill the process"
    assert "stopped" in outcome, outcome


def test_a_delegated_steps_graph_lookups_are_reported(tmp_path, monkeypatch):
    """"How do I know it is working?" — for minutes, you could not. What it
    asked the graph comes back through trance, so it is shown."""
    import json as _json

    from trance import vcs
    from trance.agents.roles import BUILTIN_ROLES
    from trance.config import ModelConfig
    from trance.db import GraphDB
    from trance.events import Event, EventBus
    from trance.indexer.service import default_db_path, index_repo
    from trance.mcp_server import CALL_LOG

    project = tmp_path / "shop"
    (project / "src").mkdir(parents=True)
    (project / "src" / "cart.js").write_text("export function total(i){ return 1; }\n")
    index_repo(project, GraphDB(default_db_path(project)))
    vcs.ensure_repo(project)
    vcs.commit_all(project, "start")

    # One lookup from an earlier step: it must not be reported again.
    log = project / ".trance" / CALL_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(_json.dumps({"name": "search_symbols", "arguments": {"pattern": "old"},
                                "hit": True, "chars": 10}) + "\n")

    def lookups():
        with log.open("a", encoding="utf8") as handle:
            handle.write(_json.dumps({"name": "get_callers",
                                      "arguments": {"symbol": "total"},
                                      "hit": True, "chars": 200}) + "\n")

    delegate = fake_delegate(monkeypatch, "OUTCOME: SUCCESS", side_effect=lookups)
    seen: list[Event] = []
    bus = EventBus()
    bus.subscribe_sync(seen.append)

    delegate.run_delegated(role=BUILTIN_ROLES["frontend"], task="t", project=project,
                           config=ModelConfig(kind="claudecode"), bus=bus,
                           session_id="s", step_id="st")

    graph_calls = [e for e in seen if e.type == "tool_call"
                   and (e.payload.get("detail") or {}).get("via") == "mcp"]
    assert len(graph_calls) == 1, "only this step's lookups belong to this step"
    assert graph_calls[0].payload["name"] == "get_callers"
    assert graph_calls[0].payload["arguments"] == {"symbol": "total"}
    assert graph_calls[0].payload["ok"] is True


def test_a_delegated_edit_carries_its_diff(tmp_path, monkeypatch):
    """The console showed that a file had been edited without showing the edit:
    the tool outcome's detail — the diff, a command's exit code and output —
    was being dropped on the way out of the tool server."""
    import io
    import json as _json

    from trance.agents.delegate import _lookups_logged, _report_calls
    from trance.agents.roles import BUILTIN_ROLES
    from trance.events import Event, EventBus
    from trance.mcp_server import serve

    project = tmp_path / "app"
    (project / "src").mkdir(parents=True)
    (project / "src" / "a.js").write_text("const PORT = 3000;\nstart();\n")

    serve(project, role=BUILTIN_ROLES["frontend"], stdout=io.StringIO(),
          stdin=io.StringIO(_json.dumps({
              "jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "edit_file", "arguments": {
                  "path": "src/a.js", "find": "const PORT = 3000;",
                  "replace": "const PORT = process.env.PORT || 3000;"}}})))

    rows = _lookups_logged(project)
    assert rows and rows[-1]["detail"]["diff"].startswith("--- a/src/a.js")

    seen: list[Event] = []
    bus = EventBus()
    bus.subscribe_sync(seen.append)
    _report_calls(rows, bus, "s", "st", "frontend")

    call = next(e for e in seen if e.type == "tool_call")
    detail = call.payload["detail"]
    assert detail["kind"] == "write"                  # not flattened to "delegated"
    assert detail["via"] == "mcp"
    assert "+const PORT = process.env.PORT" in detail["diff"]
    assert detail["added"] == 1 and detail["removed"] == 1

    # ...and the file it wrote is announced, as any other agent's would be.
    wrote = [e for e in seen if e.type == "file_written"]
    assert [e.payload["path"] for e in wrote] == ["src/a.js"]
