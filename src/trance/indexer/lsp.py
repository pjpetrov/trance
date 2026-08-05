"""PHASE 2 — language-server-backed resolution.

Not implemented yet. The name-based resolver in resolve.py stands in for it and
tags every edge with a `resolution` value, so this can be rolled out
incrementally: run the LSP over the files you care about, upgrade those edges to
resolution="lsp", leave the rest heuristic.

Planned shape
-------------
    class LspSession:
        def __init__(self, root: Path, cmd: list[str]): ...
        def definition(self, path: str, line: int, col: int) -> Location | None
        def references(self, path: str, line: int, col: int) -> list[Location]
        def document_symbols(self, path: str) -> list[SymbolInfo]

Servers (install separately, they are not Python deps):
    python      pyright-langserver --stdio          npm i -g pyright
    typescript  typescript-language-server --stdio  npm i -g typescript-language-server typescript

Why LSP and not more tree-sitter: tree-sitter gives us *syntax* cheaply and
incrementally, which is the right tool for finding definitions and call sites.
It cannot tell you which `create_user` a call refers to across module
boundaries, through aliases, or through class hierarchies. That's a type/scope
question, and pyright already answers it correctly — reimplementing it is the
single biggest waste of effort available in this project.

Integration point: resolve.resolve_all() gains an optional `sessions` argument;
for each unresolved edge it asks textDocument/definition at the call site's
(line, col) and maps the returned location back to a symbol id via the
`symbols` byte ranges already in SQLite. Everything downstream is unchanged.
"""

from __future__ import annotations

SERVER_COMMANDS = {
    "python": ["pyright-langserver", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
    "tsx": ["typescript-language-server", "--stdio"],
}


class LspSession:  # pragma: no cover - PHASE 2
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "PHASE 2: LSP-backed resolution. Today the name-based resolver in "
            "trance.indexer.resolve handles this; edges are tagged with their "
            "resolution quality so you can see exactly what would improve."
        )
