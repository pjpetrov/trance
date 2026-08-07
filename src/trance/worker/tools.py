"""The worker's lazy-context tools, backed by the same SQLite graph.

These exist so an under-fetching curator costs one tool round trip instead of a
hallucination. Every call is traced with `hit` and `result_tokens`; a run with
many calls means the curator's hop limit or budget was too tight for that task.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import paths
from ..db import GraphDB
from ..model import Symbol, estimate_tokens

MAX_RESULTS = 12


def specs() -> list[dict]:
    """OpenAI-format tool definitions."""

    def _sym_tool(name: str, description: str) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Function/class name, or a qualified name like path/to/file.py::Class.method",
                        }
                    },
                    "required": ["symbol"],
                },
            },
        }

    return [
        _sym_tool("get_definition", "Return the full source code of a symbol."),
        _sym_tool("get_callers", "List the symbols that call the given symbol."),
        _sym_tool("get_callees", "List the symbols that the given symbol calls."),
        {
            "type": "function",
            "function": {
                "name": "search_symbols",
                "description": (
                    "Find indexed symbols by NAME. Matches function names, class names "
                    "and file paths — one identifier or fragment of one, e.g. "
                    "'streamBacktest' or 'binance'. This is not a text search: a phrase "
                    "or a description of behaviour will never match anything."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"pattern": {
                        "type": "string",
                        "description": "One identifier or part of one — no spaces.",
                    }},
                    "required": ["pattern"],
                },
            },
        },
    ]


@dataclass
class ToolResult:
    text: str
    hit: bool
    #: Symbols this call surfaced — the orchestrator folds them into the next
    #: bundle so a second round does not re-fetch them one at a time.
    symbols: list[str]

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


class ContextTools:
    def __init__(self, db: GraphDB, repo: Path):
        self.db = db
        self.repo = Path(repo)

    def call(self, name: str, arguments: dict) -> ToolResult:
        handler = {
            "get_definition": self.get_definition,
            "get_callers": self.get_callers,
            "get_callees": self.get_callees,
            "search_symbols": self.search_symbols,
        }.get(name)
        if handler is None:
            return ToolResult(f"No such tool: {name}", hit=False, symbols=[])
        try:
            return handler(**arguments)
        except TypeError as exc:
            return ToolResult(f"Bad arguments for {name}: {exc}", hit=False, symbols=[])

    # ---------------------------------------------------------------- tools

    def get_definition(self, symbol: str) -> ToolResult:
        symbol = self._tidy(symbol)
        if (outline := self._file_outline(symbol)) is not None:
            return outline
        matches = self.db.find_symbols(symbol)
        if not matches:
            return ToolResult(_miss(symbol), hit=False, symbols=[])
        sym = matches[0]
        body = self._source(sym)
        header = f"# {sym.file_path}:{sym.start_line}-{sym.end_line}"
        extra = ""
        if len(matches) > 1:
            others = ", ".join(m.qualname for m in matches[1:MAX_RESULTS])
            extra = f"\n\n(Also matched: {others})"
        return ToolResult(f"{header}\n{body}{extra}", hit=True, symbols=[sym.qualname])

    def get_callers(self, symbol: str) -> ToolResult:
        return self._neighbours(symbol, direction="callers")

    def get_callees(self, symbol: str) -> ToolResult:
        return self._neighbours(symbol, direction="callees")

    def search_symbols(self, pattern: str) -> ToolResult:
        pattern = self._tidy(pattern)
        matches = self.db.find_symbols(pattern)
        if not matches:
            return ToolResult(_no_match(pattern), hit=False, symbols=[])
        lines = [f"{m.kind} {m.qualname}  ({m.file_path}:{m.start_line})" for m in matches[:MAX_RESULTS]]
        if len(matches) > MAX_RESULTS:
            lines.append(f"... and {len(matches) - MAX_RESULTS} more")
        return ToolResult("\n".join(lines), hit=True, symbols=[m.qualname for m in matches[:MAX_RESULTS]])

    # -------------------------------------------------------------- helpers

    def _neighbours(self, symbol: str, direction: str) -> ToolResult:
        symbol = self._tidy(symbol)
        matches = self.db.find_symbols(symbol)
        if not matches:
            return ToolResult(_miss(symbol), hit=False, symbols=[])
        sym = matches[0]

        if direction == "callers":
            pairs = [(s, e) for s, e in self.db.callers(sym.id)]
            label = "Callers of"
        else:
            pairs = self.db.callees(sym.id)
            label = "Calls made by"

        lines, names = [], []
        for other, edge in pairs:
            if other is None:
                lines.append(f"- {edge.dst_name} (line {edge.line}, unresolved — not defined in this repo)")
                continue
            lines.append(
                f"- {other.qualname}  ({other.file_path}:{other.start_line}, {edge.resolution})"
            )
            names.append(other.qualname)
        if not lines:
            return ToolResult(f"{label} {sym.qualname}: none found.", hit=True, symbols=[])
        return ToolResult(f"{label} {sym.qualname}:\n" + "\n".join(lines), hit=True, symbols=names)

    def _tidy(self, query: str) -> str:
        """The path part of a query, as the index stores it.

        `/src/./game/scene.js::draw` and `src/game/scene.js::draw` name the same
        thing to everyone except an exact-match lookup, which is what this is.
        A bare symbol — `draw`, `Scene.update` — has no path part and is left
        exactly as it came.
        """
        head, sep, tail = (query or "").partition("::")
        if "/" in head or head.startswith("."):
            head = paths.relative(self.repo, head) or head
        return head + sep + tail

    def _file_outline(self, query: str) -> ToolResult | None:
        """Asking for a file returns its outline, not one arbitrary symbol in it.

        Without this, `get_definition("app/services.py")` matched every symbol
        whose qualname contains that path and silently returned the first —
        a confident-looking wrong answer.
        """
        if "::" in query or "/" not in query and "." not in query:
            return None
        # A file named by its tail — "game/scene.js" for "src/game/scene.js" —
        # is how a model refers to a file it has only seen in an import.
        candidates = [p for p in self.db.file_paths()
                      if p == query or p.endswith("/" + query)]
        if not candidates:
            return None
        path = candidates[0]
        syms = self.db.symbols_in_file(path)
        lines = [f"{s.kind} {s.qualname.split('::', 1)[-1]}  (lines {s.start_line}-{s.end_line})" for s in syms]
        return ToolResult(
            f"{path} is a file, not a symbol. It defines {len(syms)} symbol(s):\n"
            + "\n".join(lines)
            + "\n\nCall get_definition again with one of these names to see its source.",
            hit=True,
            symbols=[s.qualname for s in syms],
        )

    def _source(self, sym: Symbol) -> str:
        try:
            data = (self.repo / sym.file_path).read_bytes()
        except OSError:
            return sym.signature
        return data[sym.start_byte : sym.end_byte].decode("utf8", errors="replace")


def _no_match(pattern: str) -> str:
    """Say why, when the pattern was never going to match anything.

    Models reach for this as if it were a semantic search — "SSE done event
    equity curve" is a test description, not an identifier — and a bare "no
    symbols match" tells them nothing, so they try another sentence.
    """
    base = f"No symbols match {pattern!r}."
    words = pattern.split()
    if len(words) > 1:
        longest = max(words, key=len)
        return (
            f"{base} search_symbols matches indexed FUNCTION and CLASS NAMES and file "
            f"paths — it is not a full-text or semantic search, so a phrase will never "
            f"match. Search one identifier at a time (try {longest!r}), or use "
            f"list_files / read_file if you are looking for behaviour rather than a name."
        )
    return (f"{base} It may be defined in a third-party package, or spelled differently. "
            f"The project map in your prompt lists what is indexed.")


def _miss(symbol: str) -> str:
    return (
        f"No symbol named {symbol!r} is indexed. It may be from a third-party library "
        f"(not in this repo), or the name may differ — try search_symbols with a partial name."
    )


#: The map is a *pointer*, not content: it goes into every agent's prompt, so
#: it has to stay well under the cost of just reading a couple of files.
MAP_BUDGET_CHARS = 2500
#: Names only past this many per file; a 90-symbol file would otherwise crowd
#: out every other file in the project.
MAP_SYMBOLS_PER_FILE = 14


def project_map(db: GraphDB, *, budget_chars: int = MAP_BUDGET_CHARS,
                focus: str = "") -> str:
    """A one-screen index of what has been parsed: files and their symbols.

    Agents were ignoring the graph tools and reading whole files instead, and
    the reason is simple — nothing told them what was in the graph. Guessing a
    symbol name that may not exist loses to `read_file`, which always works. Seen
    against a list of real names, get_definition becomes the cheaper move.
    """
    paths = db.file_paths()
    if not paths:
        return ""

    # Files the task mentions come first; the budget bites the tail.
    hint = (focus or "").lower()
    ordered = sorted(paths, key=lambda p: (0 if p.lower() in hint else 1, p))

    lines: list[str] = []
    used = 0
    shown = 0
    for path in ordered:
        symbols = [s for s in db.symbols_in_file(path) if s.kind != "variable"]
        names = [s.qualname.split("::", 1)[-1] for s in symbols[:MAP_SYMBOLS_PER_FILE]]
        if len(symbols) > MAP_SYMBOLS_PER_FILE:
            names.append(f"+{len(symbols) - MAP_SYMBOLS_PER_FILE} more")
        line = f"{path}: {', '.join(names)}" if names else f"{path}: (no functions or classes)"
        if used + len(line) > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1

    if shown < len(ordered):
        lines.append(f"…and {len(ordered) - shown} more file(s) — use search_symbols to find them.")
    return "\n".join(lines)
