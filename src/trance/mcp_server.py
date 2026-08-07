"""trance's call graph, as an MCP server.

This is the direction MCP actually runs: a server that a model host — Claude
Code — connects to and calls. It exists because of what delegating a step costs.
When Claude Code runs the step itself it brings its own tools, and those are
grep and read-the-whole-file. The index trance built is right there and it
cannot see it.

So these four tools are handed over: the symbol lookup, the outline, and the two
directions of the call graph. A 33KB file is ~8,400 tokens; the function you
actually want is 150, and asking for it by name is the entire argument of this
project. It applies to a delegated agent exactly as much as to trance's own.

Read-only, deliberately. Editing is Claude Code's business in that mode, and a
second way to write the same files is a way to write them inconsistently.

Speaks JSON-RPC 2.0 over stdin/stdout: `initialize`, `tools/list`, `tools/call`,
and the notifications that go with them. Hand-written because it is a hundred
lines and the alternative is a dependency for four functions.

    python -m trance.mcp_server /path/to/project
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .db import GraphDB
from .indexer.service import default_db_path
from .worker.tools import ContextTools

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "get_definition",
        "description": (
            "The source of one symbol, by name — 'handleOrder', 'Cart.total', or "
            "'src/cart.js::total'. Give a file path instead and you get that file's "
            "outline: its symbols and their line ranges. Prefer this over reading a "
            "whole file: the file is thousands of tokens, the function is a hundred."),
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string",
                                      "description": "symbol name, or a file path"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "search_symbols",
        "description": (
            "Find symbols whose name contains this text. Identifiers, not prose — "
            "'checkout' finds handleCheckout; 'the checkout flow' finds nothing."),
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "get_callers",
        "description": (
            "What calls this symbol. The question to ask before changing a "
            "signature, and the one grep answers badly."),
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_callees",
        "description": "What this symbol calls — its immediate dependencies.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
]


class GraphServer:
    """The four lookups, over MCP."""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()
        self.tools: ContextTools | None = None
        db_path = default_db_path(self.project)
        if db_path.exists():
            self.tools = ContextTools(GraphDB(db_path), self.project)

    # ------------------------------------------------------------ dispatch

    def handle(self, request: dict) -> dict | None:
        """One request in, one response out. None for a notification."""
        method = request.get("method") or ""
        request_id = request.get("id")

        if method == "initialize":
            return _ok(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "trance-graph", "version": "1"},
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None                      # nothing to say, and nothing to send
        if method == "tools/list":
            return _ok(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = request.get("params") or {}
            return _ok(request_id, self.call(params.get("name") or "",
                                             params.get("arguments") or {}))
        if method == "ping":
            return _ok(request_id, {})
        return _error(request_id, -32601, f"no such method: {method}")

    def call(self, name: str, arguments: dict) -> dict:
        """Run one lookup. Failures are results, not protocol errors: the model
        should read "no such symbol" and try another, not see a broken server."""
        if self.tools is None:
            return _content(
                "This project has no index yet, so there is no call graph to search. "
                "Read files directly instead.", is_error=True)

        try:
            if name == "get_definition":
                found = self.tools.get_definition(str(arguments.get("symbol") or ""))
            elif name == "search_symbols":
                found = self.tools.search_symbols(str(arguments.get("pattern") or ""))
            elif name == "get_callers":
                found = self.tools.get_callers(str(arguments.get("symbol") or ""))
            elif name == "get_callees":
                found = self.tools.get_callees(str(arguments.get("symbol") or ""))
            else:
                return _content(f"no such tool: {name}", is_error=True)
        except Exception as exc:            # noqa: BLE001 — never kill the server
            return _content(f"the lookup failed: {type(exc).__name__}: {exc}",
                            is_error=True)
        return _content(found.text, is_error=not found.hit)


def _ok(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text or ""}], "isError": is_error}


def serve(project: Path, stdin=None, stdout=None) -> None:
    """Read requests until stdin closes. One JSON object per line."""
    server = GraphServer(project)
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue                        # not ours to fix; wait for the next
        response = server.handle(request)
        if response is None:
            continue
        sink.write(json.dumps(response) + "\n")
        sink.flush()


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("usage: python -m trance.mcp_server <project-dir>", file=sys.stderr)
        return 2
    serve(Path(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
