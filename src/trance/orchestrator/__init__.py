"""Orchestrator: task -> curate -> work -> (bounded) re-curate.

Sequential, single-threaded, one TraceWriter per run so a run is exactly one
directory on disk. The re-curate loop is capped — an unbounded one is just
"load the whole repo" with extra steps, which defeats the point of the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..curator.walker import CuratorConfig, baseline_tokens, curate
from ..db import GraphDB
from ..indexer.service import default_db_path, index_repo
from ..model import ContextBundle
from ..trace.writer import TraceWriter, bundle_payload, graph_slice_payload
from ..worker import agent as worker_agent


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    bundle: ContextBundle
    worker: "worker_agent.WorkerResult | None"
    rounds: int
    baseline_tokens: int
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def run(
    repo: Path,
    task: str,
    entry: str,
    config: Config | None = None,
    reindex: bool = True,
    progress=None,
) -> RunResult:
    config = config or Config.load()
    repo = Path(repo).resolve()
    worker_model = config.resolve(config.worker)
    say = progress or (lambda _msg: None)

    db = GraphDB(default_db_path(repo))
    trace = TraceWriter(Path(config.runs_dir), task=task, repo=repo, validate=True)
    try:
        # ---- index (incremental; cheap when nothing changed) -------------
        if reindex:
            say("indexing…")
            index = index_repo(repo, db)
            trace.emit(
                "index",
                actor="indexer",
                duration_ms=round(index.duration_s * 1000, 1),
                meta={
                    "parsed": index.parsed, "unchanged": index.skipped,
                    "removed": index.deleted, "resolution": index.resolution,
                    **db.counts(),
                },
            )

        curator_config = CuratorConfig(**vars(config.curator))
        extra_roots: list[str] = []
        bundle = None
        result = None
        rounds = 0

        for rounds in range(1, config.max_recurate_rounds + 2):
            say(f"curating (round {rounds})…")
            bundle = _curate(db, repo, task, entry, curator_config, extra_roots)
            baseline = baseline_tokens(repo, bundle)
            curate_event = trace.emit(
                "curate",
                actor="curator",
                task=task,
                context_bundle=bundle_payload(bundle, baseline_tokens=baseline),
                graph_slice=graph_slice_payload(db, bundle),
                meta={"round": rounds, "extra_roots": extra_roots, "config": vars(curator_config)},
            )

            say(f"working ({worker_model.model})…")
            result = _invoke_worker(trace, curate_event, db, repo, task, bundle, config)

            if not result.needs_more_context or rounds > config.max_recurate_rounds:
                break
            say(f"worker requested more context: {', '.join(result.requested_context)}")
            extra_roots = result.requested_context

        if result and result.diff:
            files = _files_in_diff(result.diff)
            trace.emit(
                "diff",
                actor="worker",
                diff={
                    "unified": result.diff,
                    "files_changed": files,
                    "insertions": sum(1 for l in result.diff.splitlines() if l.startswith("+") and not l.startswith("+++")),
                    "deletions": sum(1 for l in result.diff.splitlines() if l.startswith("-") and not l.startswith("---")),
                    "applied": False,
                },
            )

        notes = []
        if result and result.stop_reason == "max_tool_rounds":
            notes.append(f"worker hit max_tool_rounds ({worker_model.max_tool_rounds}) without settling")
        if result and result.needs_more_context:
            notes.append(f"still missing context after {rounds} round(s): {', '.join(result.requested_context)}")

        run_dir = trace.close("ok")
        return RunResult(
            run_id=trace.run_id, run_dir=run_dir, bundle=bundle, worker=result,
            rounds=rounds, baseline_tokens=baseline_tokens(repo, bundle) if bundle else 0,
            notes=notes,
        )
    except Exception as exc:
        trace.emit("error", error={"kind": type(exc).__name__, "message": str(exc)})
        run_dir = trace.close("error")
        return RunResult(
            run_id=trace.run_id, run_dir=run_dir, bundle=None, worker=None,
            rounds=0, baseline_tokens=0, error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        db.close()


def _curate(db, repo, task, entry, curator_config, extra_roots) -> ContextBundle:
    """Curate from the entry point, merging in any symbols the worker asked for."""
    bundle = curate(db, repo, task, entry, curator_config)
    seen = {item.qualname for item in bundle.items}
    for root in extra_roots:
        try:
            extra = curate(db, repo, task, root, curator_config)
        except LookupError:
            bundle.notes.append(f"requested symbol {root!r} is not indexed")
            continue
        for item in extra.items:
            if item.qualname not in seen:
                seen.add(item.qualname)
                bundle.items.append(item)
        bundle.notes.append(f"expanded with {root!r} at the worker's request")
    return bundle


def _invoke_worker(trace, parent, db, repo, task, bundle, config):
    worker_model = config.resolve(config.worker)
    stats = bundle.stats()
    agent_event = trace.emit(
        "agent",
        actor="worker",
        parent_event_id=parent,
        task=task,
        model={"id": worker_model.model, "temperature": worker_model.temperature,
               "max_tokens": worker_model.max_tokens},
        meta={"base_url": worker_model.base_url, "bundle_est_tokens": stats["est_tokens"]},
    )

    def on_tool_call(invocation):
        trace.emit(
            "tool_call",
            actor="worker",
            parent_event_id=agent_event,
            tool={
                "name": invocation.name,
                "arguments": invocation.arguments,
                "hit": invocation.hit,
                "result_tokens": invocation.result_tokens,
                "result_summary": invocation.result_summary,
            },
        )

    result = worker_agent.run(bundle, db, repo, worker_model, on_tool_call=on_tool_call)
    trace.emit(
        "agent",
        actor="worker",
        parent_event_id=agent_event,
        model={"id": worker_model.model},
        usage={**result.usage, "estimated": False},
        output={
            "text": result.text,
            "stop_reason": result.stop_reason,
            "requested_context": result.requested_context,
        },
        meta={"rounds": result.rounds, "tool_calls": len(result.tool_calls),
              "reasoning_chars": len(result.reasoning)},
    )
    return result


def _files_in_diff(diff: str) -> list[str]:
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ ") and not line.endswith("/dev/null"):
            path = line[4:].strip()
            files.append(path[2:] if path.startswith(("a/", "b/")) else path)
    return files
