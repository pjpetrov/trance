"""trance CLI — the only entry point you need in PHASE 1.

    trance index    samples/sample-app          # build/refresh the graph
    trance symbols  samples/sample-app user     # find entry points
    trance callgraph samples/sample-app get_user_orders
    trance bundle   samples/sample-app get_user_orders --task "add pagination"
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.tree import Tree

from .curator.walker import CuratorConfig, baseline_tokens, curate
from .db import GraphDB
from .indexer.service import default_db_path, index_repo
from .model import estimate_tokens
from .trace.writer import TraceWriter, bundle_payload, graph_slice_payload

app = typer.Typer(add_completion=False, help="Context-minimizing coding agent toolkit.")
console = Console()


def _open(repo: Path) -> tuple[Path, GraphDB]:
    repo = repo.resolve()
    return repo, GraphDB(default_db_path(repo))


@app.command()
def index(
    repo: Path = typer.Argument(..., exists=True, file_okay=False),
    force: bool = typer.Option(False, "--force", help="Re-parse even unchanged files."),
    file: Optional[list[Path]] = typer.Option(None, "--file", help="Re-index only these files."),
):
    """Parse REPO with tree-sitter and persist the symbol + call graph to SQLite."""
    repo, db = _open(repo)
    result = index_repo(repo, db, paths=list(file) if file else None, force=force)
    counts = db.counts()
    console.print(f"[green]indexed[/] {repo}")
    console.print(f"  {result.summary()}")
    console.print(f"  graph: {counts['files']} files, {counts['symbols']} symbols, "
                  f"{counts['resolved_edges']}/{counts['edges']} edges resolved")
    if result.resolution:
        console.print(f"  resolution: {result.resolution}")
    console.print(f"  db: {default_db_path(repo)}")
    db.close()


@app.command()
def symbols(
    repo: Path = typer.Argument(..., exists=True, file_okay=False),
    pattern: str = typer.Argument("", help="Substring of name or qualname."),
    limit: int = typer.Option(40),
):
    """List indexed symbols matching PATTERN."""
    repo, db = _open(repo)
    rows = db.find_symbols(pattern) if pattern else list(db.all_symbols())
    table = Table("kind", "symbol", "location", box=None)
    for s in rows[:limit]:
        table.add_row(s.kind, s.qualname.split("::", 1)[-1], s.loc)
    console.print(table)
    console.print(f"[dim]{len(rows)} match(es)[/]")
    db.close()


@app.command()
def callgraph(
    repo: Path = typer.Argument(..., exists=True, file_okay=False),
    entry: str = typer.Argument(..., help="Function name or qualname."),
    hops: int = typer.Option(3, "--hops", "-n"),
    callers: bool = typer.Option(False, "--callers", help="Walk inbound edges instead."),
):
    """Print the call graph rooted at ENTRY."""
    repo, db = _open(repo)
    matches = db.find_symbols(entry)
    if not matches:
        console.print(f"[red]no symbol matches[/] {entry!r}")
        raise typer.Exit(1)
    root = matches[0]
    if len(matches) > 1:
        console.print(f"[yellow]{len(matches)} matches; using {root.qualname}[/]")

    label = "callers of" if callers else "call graph from"
    tree = Tree(f"[bold]{label}[/] [cyan]{root.name}[/] [dim]{root.loc}[/]")
    _grow(db, root, tree, hops, callers, seen={root.id})
    console.print(tree)
    db.close()


_RES_STYLE = {
    "lsp": "green", "same_file": "green", "same_dir": "cyan",
    "unique_global": "cyan", "ambiguous": "yellow", "unresolved": "red",
}


def _grow(db: GraphDB, sym, node: Tree, hops: int, callers: bool, seen: set[int]) -> None:
    if hops <= 0:
        return
    if callers:
        edges = [(s, e) for s, e in db.callers(sym.id)]
    else:
        edges = db.callees(sym.id)

    for other, edge in edges:
        style = _RES_STYLE.get(edge.resolution, "white")
        if other is None:
            node.add(f"[red]{edge.dst_name}[/] [dim]:{edge.line} unresolved[/]")
            continue
        tag = f"[{style}]{other.name}[/] [dim]{other.loc} ({edge.resolution})[/]"
        if other.id in seen:
            node.add(f"{tag} [dim]…[/]")
            continue
        seen.add(other.id)
        _grow(db, other, node.add(tag), hops - 1, callers, seen)


@app.command()
def bundle(
    repo: Path = typer.Argument(..., exists=True, file_okay=False),
    entry: str = typer.Argument(...),
    task: str = typer.Option("(no task given)", "--task", "-t"),
    hops: int = typer.Option(2, "--hops", "-n"),
    body_hops: int = typer.Option(1, "--body-hops"),
    budget: int = typer.Option(8000, "--budget", help="Token ceiling for the bundle."),
    callers: bool = typer.Option(False, "--callers"),
    show: bool = typer.Option(False, "--show", help="Print the rendered prompt."),
    trace: bool = typer.Option(True, "--trace/--no-trace", help="Write a run trace."),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
):
    """Curate a minimal context bundle for ENTRY and report the token savings."""
    repo, db = _open(repo)
    config = CuratorConfig(max_hops=hops, body_hops=body_hops, token_budget=budget,
                           include_callers=callers)
    try:
        result = curate(db, repo, task, entry, config)
    except LookupError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)

    stats = result.stats()
    baseline = baseline_tokens(repo, result)
    saved = 1 - (stats["est_tokens"] / baseline) if baseline else 0.0

    table = Table("hops", "kind", "symbol", "lines", "incl", "~tok", box=None)
    for item in result.items:
        table.add_row(str(item.hops), item.kind, item.qualname.split("::", 1)[-1],
                      f"{item.file_path}:{item.start_line}-{item.end_line}",
                      item.include, str(estimate_tokens(item.text)))
    console.print(table)
    console.print(
        f"\n[bold]{stats['symbols']}[/] symbols from [bold]{stats['files_touched']}[/] file(s)  "
        f"[green]~{stats['est_tokens']} tokens[/] vs [red]~{baseline}[/] for whole files  "
        f"([bold green]{saved:.0%} smaller[/])"
    )
    for note in result.notes:
        console.print(f"[yellow]note:[/] {note}")
    if result.unresolved:
        console.print(f"[dim]unresolved: {', '.join(result.unresolved[:12])}[/]")
    if show:
        console.print("\n[dim]" + "─" * 60 + "[/]")
        # markup=False is essential: source code contains [brackets], which rich
        # would otherwise parse as style tags and silently delete. List
        # comprehensions vanish without it.
        console.print(result.render(), markup=False, highlight=False)

    if trace:
        with TraceWriter(runs_dir, task=task, repo=repo, validate=True) as tw:
            curate_ev = tw.emit(
                "curate", actor="curator", task=task,
                context_bundle=bundle_payload(result, baseline_tokens=baseline),
                graph_slice=graph_slice_payload(db, result),
                meta={"config": config.__dict__},
            )
            # PHASE 2 replaces this with a real worker invocation.
            tw.emit(
                "agent", actor="worker", parent_event_id=curate_ev, task=task,
                model={"id": "claude-haiku-4-5-20251001"},
                usage={"input_tokens": stats["est_tokens"], "output_tokens": 0, "estimated": True},
                output={"text": None, "stop_reason": "not_implemented"},
                meta={"phase": "PHASE 2 — worker agent not wired up yet"},
            )
        console.print(f"[dim]trace: {runs_dir}/{tw.run_id}/trace.jsonl[/]")
    db.close()


@app.command()
def run(
    repo: Path = typer.Argument(..., exists=True, file_okay=False),
    entry: str = typer.Argument(..., help="Entry point: function name or qualname."),
    task: str = typer.Option(..., "--task", "-t", help="What you want done."),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="OpenAI-compatible endpoint."),
    hops: Optional[int] = typer.Option(None, "--hops", "-n"),
    budget: Optional[int] = typer.Option(None, "--budget"),
    reindex: bool = typer.Option(True, "--reindex/--no-reindex"),
    show_diff: bool = typer.Option(True, "--diff/--no-diff"),
):
    """Run the full agent: index -> curate -> work -> diff."""
    from .config import Config
    from .orchestrator import run as orchestrate

    config = Config.load(overrides={
        "worker.model": model, "provider.base_url": base_url,
        "curator.max_hops": hops, "curator.token_budget": budget,
    })
    resolved = config.resolve(config.worker)
    console.print(f"[dim]{resolved.model} @ {resolved.base_url}[/]")

    with console.status("") as status:
        result = orchestrate(repo, task, entry, config, reindex=reindex,
                            progress=lambda msg: status.update(f"[cyan]{msg}[/]"))

    if result.error:
        console.print(f"[red]run failed:[/] {result.error}")
        console.print(f"[dim]trace: {result.run_dir}[/]")
        raise typer.Exit(1)

    stats_ = result.bundle.stats()
    usage = result.worker.usage if result.worker else {}
    line = f"\n[bold]context[/] {stats_['symbols']} symbols / {stats_['files_touched']} files  "
    if result.baseline_tokens:
        saved = 1 - stats_["est_tokens"] / result.baseline_tokens
        line += (f"[green]~{stats_['est_tokens']} tok[/] vs [red]~{result.baseline_tokens}[/] "
                 f"whole-file ([bold green]{saved:.0%} smaller[/])")
    console.print(line)
    console.print(
        f"[bold]model[/] {usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out  "
        f"[bold]tool calls[/] {len(result.worker.tool_calls) if result.worker else 0}  "
        f"[bold]curate rounds[/] {result.rounds}"
    )
    for invocation in (result.worker.tool_calls if result.worker else []):
        mark = "green]hit" if invocation.hit else "yellow]miss"
        args = escape(", ".join(f"{k}={v!r}" for k, v in invocation.arguments.items()))
        console.print(f"  [dim]{invocation.name}({args}) → [/][{mark}[/]")
    for note in result.notes:
        console.print(f"[yellow]note:[/] {note}")

    if result.worker and show_diff:
        console.print("\n[dim]" + "─" * 60 + "[/]")
        # See the note in `bundle`: never let rich parse model output as markup.
        console.print(result.worker.text or "(no output)", markup=False, highlight=False)
    console.print(f"\n[dim]trace: {result.run_dir}[/]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host",
                             help="Use 0.0.0.0 to expose on the network — see the warning below."),
    port: int = typer.Option(8080, "--port"),
    reload: bool = typer.Option(False, "--reload"),
    workspace: Optional[Path] = typer.Option(
        None, "--workspace", "-w",
        help="Root for new projects. Defaults to the current directory."),
    runs_dir: Optional[Path] = typer.Option(
        None, "--runs-dir",
        help="Where sessions, providers and agents are stored. "
             "Relative paths resolve against the current directory, so running "
             "trance from elsewhere gives you a different set of sessions."),
):
    """Start the web UI: orchestrator chat, flow editor, live run inspector."""
    import uvicorn

    from .config import Config

    cfg = Config.load()
    if workspace:
        cfg.workspace = str(workspace)
    if runs_dir:
        cfg.runs_dir = str(Path(runs_dir).expanduser().resolve())
    os.environ.setdefault("TRANCE_WORKSPACE", str(cfg.workspace_root))
    os.environ.setdefault("TRANCE_RUNS_DIR", str(Path(cfg.runs_dir).resolve()))
    resolved = cfg.resolve(cfg.worker)
    console.print(f"[dim]{resolved.model} @ {resolved.base_url}[/]")
    console.print(f"[dim]workspace: {cfg.workspace_root}[/]")
    console.print(f"[dim]state:     {Path(cfg.runs_dir).resolve()}[/]")
    console.print(f"[green]UI:[/] http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    if host == "0.0.0.0":  # noqa: S104 - deliberate, and warned about
        console.print(
            "[yellow]warning:[/] bound to all interfaces. There is no authentication, and "
            "agents can write files and run commands. Only do this on a trusted network."
        )
    uvicorn.run("trance.server.app:create_app", factory=True, host=host, port=port,
                reload=reload, log_level="warning")


@app.command()
def config(
    show: bool = typer.Option(True, "--show"),
    check: bool = typer.Option(False, "--check", help="Probe the configured model endpoint."),
):
    """Show the resolved configuration, and optionally test the backend."""
    from .config import Config
    from .worker.client import BackendError, ChatClient

    cfg = Config.load()
    if show:
        console.print(json.dumps(cfg.to_dict(), indent=2))
    if check:
        console.print(f"\n[dim]probing {cfg.resolve(cfg.worker).base_url}…[/]")
        try:
            reply = ChatClient(cfg.resolve(cfg.worker)).complete(
                [{"role": "user", "content": "reply with OK"}])
            console.print(f"[green]reachable[/] — model said: {reply.text.strip()[:80]!r}")
        except BackendError as exc:
            console.print(f"[red]unreachable[/] — {exc}")
            raise typer.Exit(1)


@app.command()
def stats(repo: Path = typer.Argument(..., exists=True, file_okay=False)):
    """Show graph counts for REPO."""
    repo, db = _open(repo)
    console.print(json.dumps(db.counts(), indent=2))
    db.close()


if __name__ == "__main__":
    app()
