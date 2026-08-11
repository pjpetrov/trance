"""Indexing the type surface of npm dependencies.

The agents had a hard time finding library symbols — Phaser's Scene, a socket
client's emit — and their fallback was grepping node_modules with run_command,
at run_command prices. The declarations are the API they actually need, and
they are small; the implementations stay unindexed on purpose.
"""

from __future__ import annotations

import json
import pathlib

from trance.db import GraphDB
from trance.indexer.service import index_repo
from trance.worker.tools import ContextTools, project_map


def _project_with_library(tmp_path) -> pathlib.Path:
    project = tmp_path / "game"
    (project / "src").mkdir(parents=True)
    (project / "src" / "game.js").write_text(
        "export function update(dt) { return dt; }\n", encoding="utf8")
    (project / "package.json").write_text(json.dumps({
        "name": "game", "dependencies": {"fakelib": "^1.0.0"}}), encoding="utf8")

    lib = project / "node_modules" / "fakelib"
    lib.mkdir(parents=True)
    (lib / "package.json").write_text(json.dumps({
        "name": "fakelib", "types": "index.d.ts"}), encoding="utf8")
    (lib / "index.d.ts").write_text(
        "export declare class Socket {\n"
        "  emit(event: string, payload?: unknown): void;\n"
        "  update(dt: number): void;\n"
        "}\n"
        "export declare function connect(url: string): Socket;\n", encoding="utf8")
    # An implementation file that must NOT be indexed.
    (lib / "impl.js").write_text("function secretInternal() {}\n", encoding="utf8")
    return project


def test_dependency_declarations_are_indexed_and_labelled(tmp_path):
    project = _project_with_library(tmp_path)
    db = GraphDB(project / ".trance" / "graph.db")
    index_repo(project, db)

    tools = ContextTools(db, project)
    found = tools.search_symbols("connect")
    assert found.hit
    assert "[fakelib] connect" in found.text

    definition = tools.get_definition("Socket")
    assert definition.hit
    assert "[fakelib]" in definition.text and "library type declaration" in definition.text
    assert "emit(event: string" in definition.text

    # The implementation stays out.
    assert not tools.search_symbols("secretInternal").hit


def test_the_projects_own_symbol_outranks_the_librarys(tmp_path):
    """A project's `update` must never hide behind a library's — the graph
    exists to shrink context, and forty library internals first would grow it."""
    project = _project_with_library(tmp_path)
    db = GraphDB(project / ".trance" / "graph.db")
    index_repo(project, db)

    definition = ContextTools(db, project).get_definition("update")
    assert "src/game.js" in definition.text.splitlines()[0]


def test_reindexing_does_not_delete_or_rewalk_the_libraries(tmp_path):
    """The between-step reindex runs constantly; node_modules is walked only
    when the lockfile fingerprint moves, and a routine pass must not read the
    libraries' absence from its walk as deletion."""
    project = _project_with_library(tmp_path)
    db = GraphDB(project / ".trance" / "graph.db")
    first = index_repo(project, db)
    assert first.symbols > 0

    again = index_repo(project, db)
    assert again.deleted == 0
    assert ContextTools(db, project).search_symbols("connect").hit

    # A new dependency in the manifest moves the fingerprint.
    manifest = project / "package.json"
    data = json.loads(manifest.read_text())
    data["dependencies"]["otherlib"] = "^2.0.0"
    manifest.write_text(json.dumps(data), encoding="utf8")
    other = project / "node_modules" / "otherlib"
    other.mkdir(parents=True)
    (other / "package.json").write_text(json.dumps({
        "name": "otherlib", "types": "index.d.ts"}), encoding="utf8")
    (other / "index.d.ts").write_text(
        "export declare function launch(): void;\n", encoding="utf8")

    index_repo(project, db)
    assert ContextTools(db, project).search_symbols("launch").hit


def test_the_map_says_one_line_per_library_not_one_per_symbol(tmp_path):
    project = _project_with_library(tmp_path)
    db = GraphDB(project / ".trance" / "graph.db")
    index_repo(project, db)

    drawn = project_map(db)
    assert "fakelib" in drawn
    assert "symbols" in drawn
    assert "node_modules/fakelib/index.d.ts:" not in drawn   # no per-file listing
    assert "src/game.js" in drawn
