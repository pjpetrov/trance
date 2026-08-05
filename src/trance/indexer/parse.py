"""tree-sitter extraction: source bytes -> (symbols, raw call sites).

This layer is intentionally resolution-free. It reports *what name was called
at what line inside which symbol*; deciding which symbol that name refers to is
resolve.py's job (and, in PHASE 2, the language server's).
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node, QueryCursor

from ..model import Symbol
from .languages import LanguageSpec, get_parser, get_query


@dataclass(slots=True)
class RawCall:
    """A call site, before we know what it points at."""

    callee_name: str
    line: int  # 1-based
    start_byte: int


@dataclass(slots=True)
class ParsedFile:
    path: str
    lang: str
    symbols: list[Symbol]
    #: index into `symbols` -> calls made inside that symbol's body
    calls_by_symbol: dict[int, list[RawCall]]
    module_imports: list[str]


def _matches(query, node: Node) -> list[dict[str, list[Node]]]:
    """Per-match capture groups.

    Note: QueryCursor.captures() flattens across matches and does NOT preserve
    tree order, so pairing a @def node with its @name by containment is
    unsound. matches() keeps each pattern's captures together — always use it
    when two captures in one pattern belong to each other.
    """
    return [captures for _pattern_index, captures in QueryCursor(query).matches(node)]


def _one(captures: dict[str, list[Node]], name: str) -> Node | None:
    nodes = captures.get(name)
    return nodes[0] if nodes else None


def _text(src: bytes, node: Node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf8", errors="replace")


def _signature(src: bytes, def_node: Node) -> str:
    """Everything from the definition keyword up to (not including) the body."""
    body = def_node.child_by_field_name("body")
    end = body.start_byte if body is not None else def_node.end_byte
    text = src[def_node.start_byte : end].decode("utf8", errors="replace").rstrip()
    return text.rstrip("{:").rstrip() + (":" if text.rstrip().endswith(":") else "")


def _enclosing_definition(def_ranges: list[tuple[int, int, int]], byte_offset: int) -> int | None:
    """Innermost symbol whose byte range contains `byte_offset`.

    def_ranges is (start_byte, end_byte, symbol_index), sorted widest-first, so
    the last match wins and nested functions beat their parents.
    """
    found = None
    for start, end, idx in def_ranges:
        if start <= byte_offset < end:
            found = idx
    return found


#: Lower number wins when two patterns claim the same source range.
_KIND_SPECIFICITY = {"method": 0, "function": 1, "class": 2, "variable": 3}


def _dedupe(symbols: list[Symbol]) -> list[Symbol]:
    """Collapse symbols that different patterns claimed for the same name.

    `const handle = () => {}` matches both the arrow-function pattern and the
    module-constant pattern; the function reading is the useful one.
    """
    best: dict[tuple[str, int], Symbol] = {}
    for sym in symbols:
        key = (sym.name, sym.start_line)
        current = best.get(key)
        if current is None or _KIND_SPECIFICITY.get(sym.kind, 9) < _KIND_SPECIFICITY.get(current.kind, 9):
            # Keep the widest span among equally specific candidates so the
            # body we ship stays syntactically complete.
            if current is not None and sym.end_byte < current.end_byte and sym.kind == current.kind:
                continue
            best[key] = sym
    return sorted(best.values(), key=lambda s: (s.start_byte, -s.end_byte))


def _parent_index(symbols: list[Symbol], i: int) -> int | None:
    """Innermost symbol that strictly contains symbols[i]."""
    target = symbols[i]
    best: int | None = None
    for j, other in enumerate(symbols):
        if j == i:
            continue
        if other.start_byte <= target.start_byte and target.end_byte <= other.end_byte:
            if best is None or other.start_byte >= symbols[best].start_byte:
                best = j
    return best


def parse_source(path: str, src: bytes, spec: LanguageSpec) -> ParsedFile:
    parser = get_parser(spec.name)
    tree = parser.parse(src)
    root = tree.root_node

    # ---- definitions -----------------------------------------------------
    def_query = get_query(spec.name, spec.definitions)
    symbols: list[Symbol] = []
    for captures in _matches(def_query, root):
        kind_cap = next((c for c in captures if c.startswith("def.")), None)
        ident = _one(captures, "name")
        if kind_cap is None or ident is None:
            continue
        def_node = _one(captures, kind_cap)
        # For `const f = () => {}` the definition node is the declarator; take
        # the whole statement's span so the bundle shows valid, runnable code.
        symbols.append(
            Symbol(
                file_path=path,
                lang=spec.name,
                name=_text(src, ident),
                qualname="",  # filled in below, once nesting is known
                kind=kind_cap.split(".", 1)[1],  # function | method | class
                start_line=def_node.start_point[0] + 1,
                end_line=def_node.end_point[0] + 1,
                start_byte=def_node.start_byte,
                end_byte=def_node.end_byte,
                signature=_signature(src, def_node),
            )
        )

    symbols.sort(key=lambda s: (s.start_byte, -s.end_byte))
    symbols = _dedupe(symbols)
    ranges = [(s.start_byte, s.end_byte, i) for i, s in enumerate(symbols)]

    # Qualified name = enclosing definitions joined by "." — Class.method,
    # outer.inner for closures. O(n^2) over symbols-per-file, which is fine at
    # file granularity; revisit if a single file ever holds thousands of defs.
    for i, sym in enumerate(symbols):
        parts = [sym.name]
        j = i
        seen = {i}
        while (parent := _parent_index(symbols, j)) is not None and parent not in seen:
            parts.append(symbols[parent].name)
            seen.add(parent)
            j = parent
        sym.qualname = f"{path}::{'.'.join(reversed(parts))}"
        # Python's grammar has no `method_definition` — a method is just a
        # function whose immediate parent is a class.
        parent = _parent_index(symbols, i)
        if sym.kind == "function" and parent is not None and symbols[parent].kind == "class":
            sym.kind = "method"

    # ---- call sites ------------------------------------------------------
    call_query = get_query(spec.name, spec.calls)
    calls_by_symbol: dict[int, list[RawCall]] = {}
    for captures in _matches(call_query, root):
        callee = _one(captures, "callee")
        if callee is None:
            continue
        owner = _enclosing_definition(ranges, callee.start_byte)
        if owner is None:
            continue  # module-level call; PHASE 2 gives every file a module symbol
        calls_by_symbol.setdefault(owner, []).append(
            RawCall(
                callee_name=_text(src, callee),
                line=callee.start_point[0] + 1,
                start_byte=callee.start_byte,
            )
        )

    # ---- imports ---------------------------------------------------------
    import_query = get_query(spec.name, spec.imports)
    module_imports = [
        _text(src, node)
        for captures in _matches(import_query, root)
        if (node := _one(captures, "module")) is not None
    ]

    return ParsedFile(
        path=path,
        lang=spec.name,
        symbols=symbols,
        calls_by_symbol=calls_by_symbol,
        module_imports=module_imports,
    )
