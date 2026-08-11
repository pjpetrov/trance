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

#: Library type declarations get a higher ceiling: Phaser ships its whole
#: public API as one ~3MB .d.ts, and skipping it would skip the one file the
#: feature exists for. Bounded per package so a pathological tree stays out.
LIB_MAX_FILE_BYTES = 8_000_000
LIB_MAX_FILES_PER_PACKAGE = 40


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


def _library_declarations(repo: Path) -> list[Path]:
    """The .d.ts type surface of the project's direct dependencies.

    Not node_modules — that would drown the graph in forty implementations of
    `update` and slow every between-step reindex. The declarations are the API
    the agents actually reach for, and they are small: a package's `types`
    entry plus the .d.ts files beside it, `@types/<name>` as the fallback.
    """
    manifest = repo / "package.json"
    try:
        import json as _json

        deps = _json.loads(manifest.read_text(encoding="utf8")).get("dependencies") or {}
    except (OSError, ValueError):
        return []

    out: list[Path] = []
    for name in sorted(deps):
        for candidate in (repo / "node_modules" / name,
                          repo / "node_modules" / "@types" / name.replace("@", "").replace("/", "__")):
            entry = _types_entry(candidate)
            if entry is None:
                continue
            found = sorted(entry.parent.rglob("*.d.ts"))[:LIB_MAX_FILES_PER_PACKAGE]
            out.extend(f for f in found if f.stat().st_size <= LIB_MAX_FILE_BYTES)
            break
    return out


def _types_entry(package_dir: Path) -> Path | None:
    """Where a package says its declarations start, or None."""
    manifest = package_dir / "package.json"
    if not manifest.is_file():
        return None
    try:
        import json as _json

        data = _json.loads(manifest.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None
    for field_name in ("types", "typings"):
        declared = data.get(field_name)
        if declared and (package_dir / declared).is_file():
            return package_dir / declared
    fallback = package_dir / "index.d.ts"
    return fallback if fallback.is_file() else None


def libraries_fingerprint(repo: Path) -> str:
    """What must change before the libraries are worth re-walking."""
    for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "package.json"):
        try:
            return _sha256((Path(repo) / name).read_bytes())
        except OSError:
            continue
    return ""


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
        # The library declarations, behind a lockfile fingerprint so the
        # between-step reindex never walks node_modules unless the
        # dependencies actually changed.
        fingerprint = libraries_fingerprint(repo)
        if fingerprint and fingerprint != db.get_meta("libraries_fingerprint"):
            for abs_path in _library_declarations(repo):
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
                    SourceFile(path=rel, lang=spec.name, sha256=digest,
                               size=len(src)), time.time())
                db.clear_file_symbols(file_id)
                db.insert_symbols(file_id, parsed.symbols)
                result.parsed += 1
                result.symbols += len(parsed.symbols)
            db.set_meta("libraries_fingerprint", fingerprint)
        # Library files are indexed only when the fingerprint moves, so a
        # routine pass must not read their absence from `seen` as deletion.
        stale = {p for p in set(known) - seen if not p.startswith("node_modules/")}
        result.deleted = db.delete_files(stale)

    db.commit()
    result.resolution = resolve.resolve_all(db)
    result.duration_s = time.time() - started
    return result
