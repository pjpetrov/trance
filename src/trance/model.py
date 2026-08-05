"""Core data types shared by the indexer, curator, and trace layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

SymbolKind = Literal["function", "method", "class", "module", "variable"]
EdgeKind = Literal["call", "http"]

#: How confident we are that an edge points at the right symbol.
#: "lsp" is produced by a real language server (PHASE 2); everything else comes
#: from the name-based fallback resolver and should be treated as a hint.
Resolution = Literal["lsp", "same_file", "same_dir", "unique_global", "ambiguous", "unresolved"]


@dataclass(slots=True)
class SourceFile:
    path: str  # repo-relative, always posix-style
    lang: str
    sha256: str
    size: int
    id: Optional[int] = None


@dataclass(slots=True)
class Symbol:
    """A named, addressable chunk of code — the unit the curator ships."""

    file_path: str
    lang: str
    name: str
    qualname: str  # e.g. "backend/app/routes.py::UserService.get_user"
    kind: SymbolKind
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    start_byte: int
    end_byte: int
    signature: str
    id: Optional[int] = None

    @property
    def loc(self) -> str:
        return f"{self.file_path}:{self.start_line}"


@dataclass(slots=True)
class Edge:
    """A directed relationship between two symbols (currently: a call site)."""

    src_id: int
    dst_name: str
    kind: EdgeKind
    line: int
    dst_id: Optional[int] = None
    resolution: Resolution = "unresolved"
    id: Optional[int] = None


@dataclass(slots=True)
class BundleItem:
    """One symbol as it appears in a context bundle."""

    qualname: str
    file_path: str
    lang: str
    kind: SymbolKind
    start_line: int
    end_line: int
    hops: int  # distance from the entry point
    include: Literal["body", "signature"]
    text: str

    def render(self) -> str:
        head = f"# {self.file_path}:{self.start_line}-{self.end_line}  ({self.kind}, {self.hops} hop(s))"
        if self.include == "signature":
            return f"{head}\n{self.text}\n    ...  # body elided (signature-only at this depth)"
        return f"{head}\n{self.text}"


@dataclass(slots=True)
class ContextBundle:
    """The minimal payload handed to the worker agent."""

    task: str
    entry: str
    max_hops: int
    items: list[BundleItem] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = [f"## Task\n{self.task}", f"## Entry point\n{self.entry}", "## Relevant code"]
        parts += [item.render() for item in self.items]
        if self.unresolved:
            parts.append(
                "## Unresolved references\n"
                "These call targets could not be located statically. Use get_definition(name) if you need them.\n"
                + "\n".join(f"- {name}" for name in self.unresolved)
            )
        return "\n\n".join(parts)

    def stats(self) -> dict:
        text = self.render()
        return {
            "symbols": len(self.items),
            "files_touched": len({i.file_path for i in self.items}),
            "chars": len(text),
            "est_tokens": estimate_tokens(text),
            "unresolved": len(self.unresolved),
        }


def estimate_tokens(text: str) -> int:
    """Cheap ~4-chars-per-token estimate.

    Deliberately crude: the trace layer records it as `est_tokens` so the
    inspection UI can show relative savings on day one. Swap for a real
    tokenizer before quoting absolute numbers.
    """
    return max(1, len(text) // 4)
