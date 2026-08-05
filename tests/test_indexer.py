from pathlib import Path

import pytest

from trance.db import GraphDB
from trance.indexer.service import index_repo

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "sample-app"


@pytest.fixture
def indexed(tmp_path):
    db = GraphDB(tmp_path / "graph.db")
    index_repo(SAMPLE, db)
    yield db
    db.close()


def test_extracts_python_and_typescript_symbols(indexed):
    names = {s.name for s in indexed.all_symbols()}
    assert {"get_user_orders", "load_user", "format_currency"} <= names  # python
    assert {"fetchUserOrders", "normalizeOrder", "OrderList"} <= names  # ts + tsx


def test_qualname_reflects_class_nesting(indexed):
    (sym,) = [s for s in indexed.all_symbols() if s.name == "list_for_user"]
    assert sym.qualname == "backend/app/services.py::OrderService.list_for_user"
    assert sym.kind == "method"


def test_call_edges_cross_files(indexed):
    (entry,) = indexed.find_symbols("backend/app/routes.py::get_user_orders")
    callees = {sym.name for sym, _ in indexed.callees(entry.id) if sym}
    assert {"load_user", "list_for_user", "serialize_order", "get_session"} <= callees


def test_callers_are_the_inverse_of_callees(indexed):
    (fmt,) = indexed.find_symbols("format_currency")
    assert "serialize_order" in {sym.name for sym, _ in indexed.callers(fmt.id)}


def test_unresolvable_calls_are_recorded_not_dropped(indexed):
    (entry,) = indexed.find_symbols("backend/app/routes.py::get_user_orders")
    edges = indexed.callees(entry.id)
    assert any(e.dst_id is None and e.dst_name == "HTTPException" for _, e in edges)


def test_reindex_is_incremental(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def a():\n    b()\n")
    (repo / "pkg" / "b.py").write_text("def b():\n    pass\n")
    db = GraphDB(tmp_path / "graph.db")

    first = index_repo(repo, db)
    assert first.parsed == 2 and first.skipped == 0

    unchanged = index_repo(repo, db)
    assert unchanged.parsed == 0 and unchanged.skipped == 2

    (repo / "pkg" / "a.py").write_text("def a():\n    b()\n\ndef c():\n    a()\n")
    after_edit = index_repo(repo, db)
    assert after_edit.parsed == 1 and after_edit.skipped == 1
    assert {s.name for s in db.all_symbols()} == {"a", "b", "c"}

    (repo / "pkg" / "b.py").unlink()
    after_delete = index_repo(repo, db)
    assert after_delete.deleted == 1
    assert {s.name for s in db.all_symbols()} == {"a", "c"}
    db.close()


def test_edges_survive_reindex_of_the_target_file(tmp_path):
    """Regression guard: re-parsing a file must not orphan edges pointing into it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def a():\n    b()\n")
    (repo / "b.py").write_text("def b():\n    pass\n")
    db = GraphDB(tmp_path / "graph.db")
    index_repo(repo, db)

    (repo / "b.py").write_text("# a comment\ndef b():\n    pass\n")
    index_repo(repo, db)

    (a,) = db.find_symbols("a.py::a")
    resolved = [sym for sym, _ in db.callees(a.id) if sym]
    assert [s.name for s in resolved] == ["b"]
    db.close()
