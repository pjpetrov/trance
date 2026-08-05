"""Turn call-site *names* into call-graph *edges*.

PHASE 1 (here): name-based resolution with a locality preference. Cheap, no
external processes, good enough to prove the context-minimization thesis on a
sample repo.

PHASE 2 (indexer/lsp.py): ask pyright / typescript-language-server for the real
definition of each call site. The `Resolution` value recorded on every edge is
what lets the UI show how much of a graph is LSP-grade vs. heuristic, so the
two resolvers can coexist during the migration.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict

from ..db import GraphDB
from ..model import Symbol

#: Names that are never worth resolving — they'd match half the repo.
STOPLIST = {
    "print", "len", "str", "int", "list", "dict", "set", "get", "post", "put",
    "append", "log", "map", "filter", "then", "catch", "push", "json", "keys",
    "values", "items", "format", "join", "split", "super", "range",
}


def resolve_all(db: GraphDB) -> dict[str, int]:
    """Re-resolve every edge in the graph. Idempotent.

    Called after each incremental parse batch: parsing is the expensive part
    and stays incremental, while resolution is an in-memory name join that
    costs milliseconds even on a large graph.
    """
    symbols = list(db.all_symbols())
    by_name: dict[str, list[Symbol]] = defaultdict(list)
    for s in symbols:
        by_name[s.name].append(s)
    by_id = {s.id: s for s in symbols}

    db.unlink_all_edges()

    stats: dict[str, int] = defaultdict(int)
    for edge_id, src_id, dst_name in db.unresolved_edges():
        src = by_id.get(src_id)
        if src is None or dst_name in STOPLIST:
            stats["unresolved"] += 1
            continue

        candidates = by_name.get(dst_name, [])
        # A call is not an edge to itself unless it's genuine recursion, which
        # adds no context — skip.
        candidates = [c for c in candidates if c.id != src_id]
        if not candidates:
            stats["unresolved"] += 1
            continue

        pick, resolution = _rank(src, candidates)
        db.resolve_edge(edge_id, pick.id if pick else None, resolution)
        stats[resolution] += 1

    db.commit()
    return dict(stats)


def _rank(src: Symbol, candidates: list[Symbol]) -> tuple[Symbol | None, str]:
    same_file = [c for c in candidates if c.file_path == src.file_path]
    if len(same_file) == 1:
        return same_file[0], "same_file"
    if same_file:
        return same_file[0], "ambiguous"

    src_dir = posixpath.dirname(src.file_path)
    same_dir = [c for c in candidates if posixpath.dirname(c.file_path) == src_dir]
    if len(same_dir) == 1:
        return same_dir[0], "same_dir"

    if len(candidates) == 1:
        return candidates[0], "unique_global"

    # Ambiguous: still record the best guess so the curator has something to
    # walk, but flag it — the worker can re-check with get_definition().
    return candidates[0], "ambiguous"
