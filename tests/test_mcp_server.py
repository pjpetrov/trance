"""trance's graph, over MCP.

Driven through the wire protocol rather than by calling the functions: the point
of this module is that another program can talk to it, and only the protocol
proves that.
"""

from __future__ import annotations

import io
import json

from trance.db import GraphDB
from trance.indexer.service import default_db_path, index_repo
from trance.mcp_server import PROTOCOL_VERSION, GraphServer, serve


def indexed_project(tmp_path):
    project = tmp_path / "shop"
    (project / "src").mkdir(parents=True)
    (project / "src" / "cart.js").write_text(
        "export function total(items){ return items.reduce((a,b)=>a+b.price,0); }\n"
        "export function checkout(items){ return total(items); }\n")
    index_repo(project, GraphDB(default_db_path(project)))
    return project


def drive(project, requests):
    """Feed requests in as a client would, read the responses back."""
    out = io.StringIO()
    serve(project, stdin=io.StringIO("\n".join(json.dumps(r) for r in requests)),
          stdout=out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_a_client_can_shake_hands_and_list_the_tools(tmp_path):
    replies = drive(indexed_project(tmp_path), [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])

    # A notification gets no reply, which is the whole difference from a request.
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[0]["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert replies[0]["result"]["serverInfo"]["name"] == "trance-graph"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == [
        "get_definition", "search_symbols", "get_callers", "get_callees"]
    for tool in replies[1]["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"].strip()


def test_the_lookups_answer_over_the_wire(tmp_path):
    project = indexed_project(tmp_path)
    replies = drive(project, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_definition", "arguments": {"symbol": "total"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_callers", "arguments": {"symbol": "total"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_symbols", "arguments": {"pattern": "check"}}},
    ])
    said = [r["result"]["content"][0]["text"] for r in replies]

    assert "reduce" in said[0] and "src/cart.js" in said[0]
    assert "checkout" in said[1]                      # who calls total
    assert "checkout" in said[2]
    assert not any(r["result"]["isError"] for r in replies)


def test_a_miss_is_an_answer_not_a_broken_server(tmp_path):
    """The model should read "no such symbol" and try another, not see the
    server fall over."""
    replies = drive(indexed_project(tmp_path), [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_definition", "arguments": {"symbol": "nosuchthing"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "nonsense", "arguments": {}}},
    ])
    for reply in replies:
        assert "error" not in reply                   # not a protocol error
        assert reply["result"]["isError"] is True
        assert reply["result"]["content"][0]["text"].strip()


def test_an_unindexed_project_says_so_rather_than_crashing(tmp_path):
    bare = tmp_path / "fresh"
    bare.mkdir()
    replies = drive(bare, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_definition", "arguments": {"symbol": "total"}}}])
    text = replies[0]["result"]["content"][0]["text"]
    assert replies[0]["result"]["isError"] is True
    assert "no index yet" in text and "Read files directly" in text


def test_rubbish_on_the_pipe_does_not_stop_the_server(tmp_path):
    project = indexed_project(tmp_path)
    out = io.StringIO()
    serve(project, stdin=io.StringIO(
        "not json\n\n"
        + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"), stdout=out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [7]


def test_an_unknown_method_is_a_protocol_error(tmp_path):
    replies = drive(indexed_project(tmp_path), [
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}])
    assert replies[0]["error"]["code"] == -32601


def test_a_lookup_that_throws_is_reported_to_the_model(tmp_path, monkeypatch):
    project = indexed_project(tmp_path)
    server = GraphServer(project)
    monkeypatch.setattr(server.tools, "get_definition",
                        lambda *_: (_ for _ in ()).throw(RuntimeError("db is gone")))
    result = server.call("get_definition", {"symbol": "total"})
    assert result["isError"] is True
    assert "db is gone" in result["content"][0]["text"]


def test_every_lookup_is_written_down(tmp_path):
    """A delegated step is opaque for minutes — the tool calls happen inside
    Claude Code's process. These come back through trance, so they are worth
    keeping rather than losing."""
    import json as _json

    project = indexed_project(tmp_path)
    drive(project, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_definition", "arguments": {"symbol": "total"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_definition", "arguments": {"symbol": "nope"}}},
    ])

    from trance.mcp_server import CALL_LOG

    lines = (project / ".trance" / CALL_LOG).read_text().splitlines()
    rows = [_json.loads(line) for line in lines]
    assert [r["name"] for r in rows] == ["get_definition", "get_definition"]
    assert rows[0]["hit"] is True and rows[0]["chars"] > 0
    assert rows[1]["hit"] is False                 # a miss is worth showing too
    assert rows[0]["arguments"] == {"symbol": "total"}


def test_a_log_that_cannot_be_written_is_not_fatal(tmp_path, monkeypatch):
    """An answer that failed to be written down is still an answer."""
    from trance.mcp_server import GraphServer

    project = indexed_project(tmp_path)
    server = GraphServer(project, log=tmp_path / "nope" / "deeper" / "log.jsonl")
    monkeypatch.setattr("pathlib.Path.mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    result = server.call("get_definition", {"symbol": "total"})
    assert result["isError"] is False and "reduce" in result["content"][0]["text"]


def test_the_allowlist_you_edited_is_the_one_it_obeys(tmp_path):
    """The tool server runs in its own process, so an allowlist it is not
    handed is one it falls back to guessing — and trance's built-in defaults are
    not what the person editing the list in the UI meant."""
    import io
    import json as _json
    import sys

    from trance.agents.roles import AgentRole
    from trance.mcp_server import main

    project = tmp_path / "app"
    (project / ".trance").mkdir(parents=True)
    role = AgentRole(name="tester", title="T", description="d", system_prompt="p",
                     paths=["tests/**"], toolsets=["files", "commands"],
                     command_list="tight")
    (project / ".trance" / "mcp-role.json").write_text(_json.dumps({
        "role": role.to_dict(),
        "commands": {"tight": {"allowed": ["echo"], "shell": False}},
    }))

    asked = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "run_command", "arguments": {"command": c}}}
             for c in ("echo hello", "pytest -q")]
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO("\n".join(_json.dumps(r) for r in asked))
    sys.stdout = out = io.StringIO()
    try:
        main([str(project), str(project / ".trance" / "mcp-role.json")])
    finally:
        sys.stdin, sys.stdout = stdin, stdout

    allowed, refused = [_json.loads(l)["result"] for l in out.getvalue().splitlines()]
    assert allowed["isError"] is False and "hello" in allowed["content"][0]["text"]
    assert refused["isError"] is True
    assert "not in this agent's allowlist" in refused["content"][0]["text"]


def test_a_role_file_without_lists_still_works(tmp_path):
    """Older files, and anyone calling the server by hand."""
    import json as _json

    from trance.agents.roles import AgentRole
    from trance.mcp_server import main

    project = tmp_path / "app"
    (project / ".trance").mkdir(parents=True)
    bare = project / ".trance" / "role.json"
    bare.write_text(_json.dumps(AgentRole(name="x", title="X", description="d",
                                          system_prompt="p", paths=["**"],
                                          toolsets=["files"]).to_dict()))
    import io
    import sys

    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(_json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    sys.stdout = out = io.StringIO()
    try:
        assert main([str(project), str(bare)]) == 0
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    names = [t["name"] for t in _json.loads(out.getvalue())["result"]["tools"]]
    assert "write_file" in names


def test_a_delegated_step_gets_the_agents_tool_budget(tmp_path):
    """The CLI has no turn limit of its own and each of its turns re-sends the
    whole conversation — 21 turns came to 740,000 tokens for one step. The tools
    are the only lever trance has, so that is where "enough" is said."""
    from trance.agents.roles import AgentRole
    from trance.mcp_server import GraphServer

    project = tmp_path / "app"
    project.mkdir()
    role = AgentRole(name="t", title="T", description="d", system_prompt="p",
                     paths=["**"], toolsets=["files"], tool_rounds=2)
    server = GraphServer(project, role=role)

    assert server.call("list_files", {})["isError"] is False
    assert server.call("list_files", {})["isError"] is False

    over = server.call("list_files", {})
    assert over["isError"] is True
    said = over["content"][0]["text"]
    assert "all 2 of your tool calls" in said
    assert "OUTCOME" in said and "honest unfinished" in said


def test_an_agent_with_no_budget_is_not_capped(tmp_path):
    """tool_rounds of 0 means "the default" for trance's own loop; for a
    delegated step there is nothing to default to, so it is not a cap."""
    from trance.agents.roles import AgentRole
    from trance.mcp_server import GraphServer

    project = tmp_path / "app"
    project.mkdir()
    server = GraphServer(project, role=AgentRole(
        name="t", title="T", description="d", system_prompt="p",
        paths=["**"], toolsets=["files"], tool_rounds=0))
    for _ in range(30):
        assert server.call("list_files", {})["isError"] is False
