import json
from pathlib import Path

import pytest

from trance.curator.walker import CuratorConfig, baseline_tokens, curate
from trance.db import GraphDB
from trance.indexer.service import index_repo
from trance.trace.writer import TraceWriter, bundle_payload, graph_slice_payload, read_run

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "sample-app"


@pytest.fixture
def db(tmp_path):
    db = GraphDB(tmp_path / "graph.db")
    index_repo(SAMPLE, db)
    yield db
    db.close()


def test_hop_limit_bounds_the_walk(db):
    one = curate(db, SAMPLE, "t", "get_user_orders", CuratorConfig(max_hops=1))
    two = curate(db, SAMPLE, "t", "get_user_orders", CuratorConfig(max_hops=2))
    assert max(i.hops for i in one.items) == 1
    assert len(two.items) > len(one.items)


def test_far_hops_are_signature_only(db):
    bundle = curate(db, SAMPLE, "t", "get_user_orders", CuratorConfig(max_hops=2, body_hops=1))
    assert {i.include for i in bundle.items if i.hops <= 1} == {"body"}
    assert {i.include for i in bundle.items if i.hops == 2} == {"signature"}


def test_bundle_is_smaller_than_the_files_it_draws_from(db):
    bundle = curate(db, SAMPLE, "t", "get_user_orders", CuratorConfig(max_hops=2))
    assert bundle.stats()["est_tokens"] < baseline_tokens(SAMPLE, bundle)


def test_token_budget_is_enforced(db):
    bundle = curate(db, SAMPLE, "t", "get_user_orders", CuratorConfig(max_hops=3, token_budget=300))
    assert bundle.stats()["est_tokens"] <= 300
    assert any("dropped" in n for n in bundle.notes)


def test_entry_point_always_survives_the_budget(db):
    bundle = curate(db, SAMPLE, "t", "get_user_orders", CuratorConfig(max_hops=3, token_budget=1))
    assert bundle.items and bundle.items[0].hops == 0


def test_unknown_entry_point_raises(db):
    with pytest.raises(LookupError):
        curate(db, SAMPLE, "t", "no_such_function_anywhere", CuratorConfig())


def test_trace_events_validate_against_the_schema(db, tmp_path):
    bundle = curate(db, SAMPLE, "t", "get_user_orders", CuratorConfig(max_hops=2))
    with TraceWriter(tmp_path / "runs", task="t", repo=SAMPLE, validate=True) as tw:
        tw.emit(
            "curate",
            actor="curator",
            context_bundle=bundle_payload(bundle, baseline_tokens=baseline_tokens(SAMPLE, bundle)),
            graph_slice=graph_slice_payload(db, bundle),
        )
        tw.emit(
            "tool_call",
            actor="worker",
            tool={"name": "get_definition", "arguments": {"symbol": "format_currency"},
                  "hit": True, "result_tokens": 40},
        )
        run_id = tw.run_id

    events = read_run(tmp_path / "runs" / run_id)
    assert [e["type"] for e in events] == ["run_start", "curate", "tool_call", "run_end"]
    manifest = json.loads((tmp_path / "runs" / run_id / "run.json").read_text())
    assert manifest["status"] == "ok" and manifest["events"] == 4


def test_trace_rejects_events_that_violate_the_schema(tmp_path):
    import jsonschema

    with TraceWriter(tmp_path / "runs", task="t", repo=Path("."), validate=True) as tw:
        with pytest.raises(jsonschema.ValidationError):
            tw.emit("curate", actor="not_a_real_actor")
