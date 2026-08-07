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
