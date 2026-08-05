"""The context curator: entry point + N hops -> minimal context bundle.

The whole thesis of the project lives in this file. Instead of handing a model
whole files, we hand it the transitive closure of *what the entry point
actually touches*, bodies for the near hops and signatures for the far ones.

No LLM required — this is a graph walk plus a budget check.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ..db import GraphDB
from ..model import BundleItem, ContextBundle, Symbol, estimate_tokens


@dataclass
class CuratorConfig:
    max_hops: int = 2
    #: Hops beyond this get signature-only treatment.
    body_hops: int = 1
    #: Also walk *inbound* edges — useful for "who breaks if I change this".
    include_callers: bool = False
    #: Hard ceiling on the bundle. Symbols are dropped farthest-hop-first.
    token_budget: int = 8000
    #: Skip edges the resolver wasn't confident about.
    skip_ambiguous: bool = False
    #: Pull in module-level constants from every file the walk touches. They are
    #: tiny, they are not reachable via call edges, and they are exactly the
    #: things tasks ask you to change (PAGE_SIZE, DEFAULT_TIMEOUT, BASE_URL).
    include_module_constants: bool = True


def _read_span(repo: Path, sym: Symbol) -> str:
    try:
        data = (repo / sym.file_path).read_bytes()
    except OSError:
        return sym.signature
    return data[sym.start_byte : sym.end_byte].decode("utf8", errors="replace")


def curate(
    db: GraphDB,
    repo: Path,
    task: str,
    entry: str,
    config: CuratorConfig | None = None,
) -> ContextBundle:
    config = config or CuratorConfig()
    repo = Path(repo)

    matches = db.find_symbols(entry)
    if not matches:
        raise LookupError(f"no symbol matches {entry!r} — try `trance symbols <pattern>`")

    bundle = ContextBundle(task=task, entry=matches[0].qualname, max_hops=config.max_hops)
    if len(matches) > 1:
        bundle.notes.append(
            f"entry {entry!r} matched {len(matches)} symbols; used {matches[0].qualname}"
        )

    root = matches[0]
    seen: dict[int, int] = {root.id: 0}  # symbol id -> hops
    unresolved: list[str] = []
    queue: deque[tuple[Symbol, int]] = deque([(root, 0)])
    collected: list[tuple[Symbol, int]] = []

    while queue:
        sym, hops = queue.popleft()
        collected.append((sym, hops))
        if hops >= config.max_hops:
            continue

        neighbours: list[tuple[Symbol | None, str, str]] = [
            (dst, edge.dst_name, edge.resolution) for dst, edge in db.callees(sym.id)
        ]
        if config.include_callers:
            neighbours += [(src, src.name, edge.resolution) for src, edge in db.callers(sym.id)]

        for dst, name, resolution in neighbours:
            if dst is None:
                if name not in unresolved:
                    unresolved.append(name)
                continue
            if config.skip_ambiguous and resolution == "ambiguous":
                bundle.notes.append(f"skipped ambiguous edge {sym.name} -> {name}")
                continue
            if dst.id in seen:
                continue
            seen[dst.id] = hops + 1
            queue.append((dst, hops + 1))

    if config.include_module_constants:
        touched = {sym.file_path for sym, _ in collected}
        for path in sorted(touched):
            for const in db.symbols_in_file(path):
                if const.kind == "variable" and const.id not in seen:
                    seen[const.id] = 1
                    collected.append((const, 1))

    # Nearest hops first, so budget trimming drops the least relevant code.
    collected.sort(key=lambda pair: (pair[1], pair[0].file_path, pair[0].start_line))

    # Budget against what actually ships — the rendered prompt, headers and
    # all — not just the raw symbol text.
    bundle.unresolved = unresolved
    total = estimate_tokens(bundle.render())
    dropped = 0

    def _item(sym: Symbol, hops: int, include: str) -> BundleItem:
        return BundleItem(
            qualname=sym.qualname,
            file_path=sym.file_path,
            lang=sym.lang,
            kind=sym.kind,
            start_line=sym.start_line,
            end_line=sym.end_line,
            hops=hops,
            include=include,
            text=_read_span(repo, sym) if include == "body" else sym.signature,
        )

    for sym, hops in collected:
        item = _item(sym, hops, "body" if hops <= config.body_hops else "signature")
        cost = estimate_tokens(item.render()) + 1  # +1 for the section joiner
        if total + cost > config.token_budget and hops > 0:
            # Downgrade to a signature before dropping it entirely.
            fallback = _item(sym, hops, "signature")
            fallback_cost = estimate_tokens(fallback.render()) + 1
            if item.include == "body" and total + fallback_cost <= config.token_budget:
                item, cost = fallback, fallback_cost
            else:
                dropped += 1
                continue
        total += cost
        bundle.items.append(item)

    if dropped:
        bundle.notes.append(
            f"{dropped} symbol(s) dropped at the {config.token_budget}-token budget; "
            "the worker can pull them back with get_definition()"
        )
    return bundle


def baseline_tokens(repo: Path, bundle: ContextBundle) -> int:
    """Tokens the naive approach would have spent: every touched file, whole.

    This is the number the inspection UI compares against.
    """
    total = 0
    for path in {item.file_path for item in bundle.items}:
        try:
            total += estimate_tokens((Path(repo) / path).read_text(errors="replace"))
        except OSError:
            continue
    return total
