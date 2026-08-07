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
