"""Structured run traces.

Layout, one directory per run:

    runs/<run_id>/
        run.json      manifest: task, repo, timing, rolled-up token totals
        trace.jsonl   one JSON object per event, schema in schemas/trace_event.schema.json

JSON Lines because runs are append-only and the UI should be able to tail a run
in progress. Validation against the schema is opt-in (`validate=True`) — on by
default in tests, off in hot paths.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "trace_event.schema.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TraceWriter:
    def __init__(self, runs_dir: Path, task: str, repo: Path, run_id: str | None = None,
                 validate: bool = False):
        self.run_id = run_id or new_id("run")
        self.dir = Path(runs_dir) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.dir / "trace.jsonl"
        self.manifest_path = self.dir / "run.json"
        self.task = task
        self.repo = str(repo)
        self.validate = validate
        self._validator = _load_validator() if validate else None
        self._totals = {"input_tokens": 0, "output_tokens": 0}
        self._events = 0
        self._started = _now()
        self.emit("run_start", actor="orchestrator", task=task, meta={"repo": self.repo})

    # ------------------------------------------------------------------ api

    def emit(self, type_: str, *, parent_event_id: Optional[str] = None, **fields: Any) -> str:
        event = {
            "event_id": new_id("ev"),
            "run_id": self.run_id,
            "parent_event_id": parent_event_id,
            "ts": _now(),
            "type": type_,
        }
        event.update({k: v for k, v in fields.items() if v is not None})

        if self._validator is not None:
            self._validator.validate(event)

        usage = event.get("usage") or {}
        self._totals["input_tokens"] += usage.get("input_tokens", 0)
        self._totals["output_tokens"] += usage.get("output_tokens", 0)

        with self.trace_path.open("a", encoding="utf8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events += 1
        return event["event_id"]

    def close(self, status: str = "ok") -> Path:
        self.emit("run_end", actor="orchestrator", meta={"status": status})
        manifest = {
            "run_id": self.run_id,
            "task": self.task,
            "repo": self.repo,
            "started_at": self._started,
            "ended_at": _now(),
            "status": status,
            "events": self._events,
            "totals": self._totals,
            "trace": self.trace_path.name,
        }
        self.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf8")
        return self.dir

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close("error" if exc_type else "ok")


def _load_validator():
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf8"))
    return jsonschema.Draft202012Validator(schema)


def read_run(run_dir: Path) -> list[dict]:
    """Load a run's events — this is the whole UI-facing read API for now."""
    path = Path(run_dir) / "trace.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines() if line.strip()]


# ------------------------------------------------------------ payload shaping

def bundle_payload(bundle, *, baseline_tokens: int | None = None, include_rendered: bool = True) -> dict:
    """ContextBundle -> the `context_bundle` field of a trace event."""
    from ..model import estimate_tokens

    stats = bundle.stats()
    if baseline_tokens is not None:
        stats["baseline_tokens"] = baseline_tokens
    return {
        "entry": bundle.entry,
        "max_hops": bundle.max_hops,
        "items": [
            {
                "qualname": i.qualname,
                "file_path": i.file_path,
                "lang": i.lang,
                "kind": i.kind,
                "start_line": i.start_line,
                "end_line": i.end_line,
                "hops": i.hops,
                "include": i.include,
                "est_tokens": estimate_tokens(i.text),
            }
            for i in bundle.items
        ],
        "unresolved": bundle.unresolved,
        "notes": bundle.notes,
        "rendered": bundle.render() if include_rendered else None,
        "stats": stats,
    }


def graph_slice_payload(db, bundle) -> dict:
    """The call-graph subset behind a bundle, for the UI's graph view."""
    ids: dict[str, int] = {}
    nodes, edges = [], []
    for item in bundle.items:
        for sym in db.find_symbols(item.qualname):
            if sym.qualname != item.qualname:
                continue
            ids[sym.qualname] = sym.id
            nodes.append({"id": sym.id, "qualname": sym.qualname, "hops": item.hops})
            break
    node_ids = {n["id"] for n in nodes}
    for nid in node_ids:
        for dst, edge in db.callees(nid):
            edges.append({
                "src": edge.src_id,
                "dst": edge.dst_id if edge.dst_id in node_ids else None,
                "dst_name": edge.dst_name,
                "kind": edge.kind,
                "resolution": edge.resolution,
            })
    return {"nodes": nodes, "edges": edges}
