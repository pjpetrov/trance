"""The indexer service: walk a repo, parse what changed, persist the graph.

Incremental by content hash. A re-index of an unchanged repo touches zero
parsers; a one-file edit re-parses exactly one file.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..db import GraphDB
from ..model import Edge, SourceFile
from . import resolve
from .languages import spec_for_path
from .parse import parse_source

IGNORE_DIRS = {
    ".git", ".trance", "node_modules", "__pycache__", ".venv", "venv", "dist",
    "build", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
}
MAX_FILE_BYTES = 1_000_000


@dataclass
class IndexResult:
    parsed: int = 0
    skipped: int = 0
    deleted: int = 0
    symbols: int = 0
    edges: int = 0
    resolution: dict[str, int] = field(default_factory=dict)
    duration_s: float = 0.0

    def summary(self) -> str:
        return (
            f"parsed={self.parsed} unchanged={self.skipped} removed={self.deleted} "
            f"symbols={self.symbols} edges={self.edges} in {self.duration_s:.2f}s"
        )


def default_db_path(repo: Path) -> Path:
    return Path(repo) / ".trance" / "graph.db"


def discover(repo: Path) -> list[Path]:
    out = []
    for p in sorted(Path(repo).rglob("*")):
        if not p.is_file():
            continue
        if any(part in IGNORE_DIRS for part in p.parts):
            continue
        if spec_for_path(p.name) is None:
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        out.append(p)
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def index_repo(repo: Path, db: GraphDB, paths: list[Path] | None = None, force: bool = False) -> IndexResult:
    """Index (or re-index) `repo`.

    `paths` limits the parse to specific files — that's the hook a file watcher
    calls on change. Deletion detection only runs on a full pass.
    """
    repo = Path(repo).resolve()
    started = time.time()
    result = IndexResult()

    full_pass = paths is None
    targets = discover(repo) if full_pass else [Path(p).resolve() for p in paths]
    known = db.file_hashes()
    seen: set[str] = set()

    for abs_path in targets:
        rel = abs_path.relative_to(repo).as_posix()
        seen.add(rel)
        spec = spec_for_path(abs_path.name)
        if spec is None:
            continue

        src = abs_path.read_bytes()
        digest = _sha256(src)
        if not force and known.get(rel) == digest:
            result.skipped += 1
            continue

        parsed = parse_source(rel, src, spec)
        file_id = db.upsert_file(
            SourceFile(path=rel, lang=spec.name, sha256=digest, size=len(src)), time.time()
        )
        db.clear_file_symbols(file_id)  # cascades this file's outgoing edges
        db.insert_symbols(file_id, parsed.symbols)

        edges: list[Edge] = []
        for idx, calls in parsed.calls_by_symbol.items():
            src_symbol = parsed.symbols[idx]
            for call in calls:
                edges.append(
                    Edge(src_id=src_symbol.id, dst_name=call.callee_name, kind="call", line=call.line)
                )
        db.insert_edges(edges)

        result.parsed += 1
        result.symbols += len(parsed.symbols)
        result.edges += len(edges)

    if full_pass:
        result.deleted = db.delete_files(set(known) - seen)

    db.commit()
    result.resolution = resolve.resolve_all(db)
    result.duration_s = time.time() - started
    return result
