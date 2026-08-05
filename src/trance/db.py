"""SQLite persistence for the symbol + call graph.

One file per repo (default: <repo>/.trance/graph.db). The schema is small on
purpose: files / symbols / edges. Everything the curator needs is a couple of
indexed joins away.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .model import Edge, SourceFile, Symbol

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    lang        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    indexed_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    qualname    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    lang        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    start_byte  INTEGER NOT NULL,
    end_byte    INTEGER NOT NULL,
    signature   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualname ON symbols(qualname);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    src_id      INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    dst_id      INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
    dst_name    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    resolution  TEXT NOT NULL,
    line        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_dstname ON edges(dst_name);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SYMBOL_COLS = (
    "s.id, f.path, s.lang, s.name, s.qualname, s.kind, "
    "s.start_line, s.end_line, s.start_byte, s.end_byte, s.signature"
)


def _row_to_symbol(row: sqlite3.Row | tuple) -> Symbol:
    return Symbol(
        id=row[0],
        file_path=row[1],
        lang=row[2],
        name=row[3],
        qualname=row[4],
        kind=row[5],
        start_line=row[6],
        end_line=row[7],
        start_byte=row[8],
        end_byte=row[9],
        signature=row[10],
    )


class GraphDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "GraphDB":
        return self

    def __exit__(self, *exc) -> None:
        self.conn.commit()
        self.close()

    # ---------------------------------------------------------------- files

    def file_hashes(self) -> dict[str, str]:
        """path -> sha256 for every indexed file. Drives incremental re-index."""
        return {r["path"]: r["sha256"] for r in self.conn.execute("SELECT path, sha256 FROM files")}

    def upsert_file(self, sf: SourceFile, indexed_at: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO files(path, lang, sha256, size, indexed_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET lang=excluded.lang, sha256=excluded.sha256, "
            "size=excluded.size, indexed_at=excluded.indexed_at RETURNING id",
            (sf.path, sf.lang, sf.sha256, sf.size, indexed_at),
        )
        return cur.fetchone()[0]

    def delete_files(self, paths: Iterable[str]) -> int:
        paths = list(paths)
        if not paths:
            return 0
        # ON DELETE CASCADE clears symbols; edges from those symbols go with them.
        self.conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])
        return len(paths)

    def clear_file_symbols(self, file_id: int) -> None:
        self.conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))

    # -------------------------------------------------------------- symbols

    def insert_symbols(self, file_id: int, symbols: list[Symbol]) -> list[int]:
        ids = []
        for s in symbols:
            cur = self.conn.execute(
                "INSERT INTO symbols(file_id, name, qualname, kind, lang, start_line, end_line, "
                "start_byte, end_byte, signature) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (file_id, s.name, s.qualname, s.kind, s.lang, s.start_line, s.end_line,
                 s.start_byte, s.end_byte, s.signature),
            )
            s.id = cur.fetchone()[0]
            ids.append(s.id)
        return ids

    def insert_edges(self, edges: list[Edge]) -> None:
        self.conn.executemany(
            "INSERT INTO edges(src_id, dst_id, dst_name, kind, resolution, line) VALUES (?,?,?,?,?,?)",
            [(e.src_id, e.dst_id, e.dst_name, e.kind, e.resolution, e.line) for e in edges],
        )

    def all_symbols(self) -> Iterator[Symbol]:
        sql = f"SELECT {SYMBOL_COLS} FROM symbols s JOIN files f ON f.id = s.file_id"
        for row in self.conn.execute(sql):
            yield _row_to_symbol(row)

    def get_symbol(self, symbol_id: int) -> Optional[Symbol]:
        sql = f"SELECT {SYMBOL_COLS} FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.id = ?"
        row = self.conn.execute(sql, (symbol_id,)).fetchone()
        return _row_to_symbol(row) if row else None

    def find_symbols(self, query: str) -> list[Symbol]:
        """Look up by exact qualname, then exact name, then substring of qualname."""
        base = f"SELECT {SYMBOL_COLS} FROM symbols s JOIN files f ON f.id = s.file_id"
        for clause, arg in (
            ("WHERE s.qualname = ?", query),
            ("WHERE s.name = ?", query),
            ("WHERE s.qualname LIKE ?", f"%{query}%"),
        ):
            rows = self.conn.execute(f"{base} {clause} ORDER BY s.qualname", (arg,)).fetchall()
            if rows:
                return [_row_to_symbol(r) for r in rows]
        return []

    def symbols_in_file(self, path: str) -> list[Symbol]:
        sql = (f"SELECT {SYMBOL_COLS} FROM symbols s JOIN files f ON f.id = s.file_id "
               "WHERE f.path = ? ORDER BY s.start_line")
        return [_row_to_symbol(r) for r in self.conn.execute(sql, (path,))]

    def file_paths(self) -> list[str]:
        return [r["path"] for r in self.conn.execute("SELECT path FROM files ORDER BY path")]

    # ---------------------------------------------------------------- edges

    def callees(self, symbol_id: int) -> list[tuple[Optional[Symbol], Edge]]:
        sql = f"""
            SELECT e.id, e.src_id, e.dst_id, e.dst_name, e.kind, e.resolution, e.line, {SYMBOL_COLS}
            FROM edges e
            LEFT JOIN symbols s ON s.id = e.dst_id
            LEFT JOIN files f ON f.id = s.file_id
            WHERE e.src_id = ? ORDER BY e.line
        """
        out = []
        for r in self.conn.execute(sql, (symbol_id,)):
            edge = Edge(id=r[0], src_id=r[1], dst_id=r[2], dst_name=r[3],
                        kind=r[4], resolution=r[5], line=r[6])
            sym = _row_to_symbol(r[7:]) if r[7] is not None else None
            out.append((sym, edge))
        return out

    def callers(self, symbol_id: int) -> list[tuple[Symbol, Edge]]:
        sql = f"""
            SELECT e.id, e.src_id, e.dst_id, e.dst_name, e.kind, e.resolution, e.line, {SYMBOL_COLS}
            FROM edges e
            JOIN symbols s ON s.id = e.src_id
            JOIN files f ON f.id = s.file_id
            WHERE e.dst_id = ? ORDER BY s.qualname
        """
        out = []
        for r in self.conn.execute(sql, (symbol_id,)):
            edge = Edge(id=r[0], src_id=r[1], dst_id=r[2], dst_name=r[3],
                        kind=r[4], resolution=r[5], line=r[6])
            out.append((_row_to_symbol(r[7:]), edge))
        return out

    def unlink_all_edges(self) -> None:
        """Drop resolution so it can be recomputed after an incremental parse."""
        self.conn.execute("UPDATE edges SET dst_id = NULL, resolution = 'unresolved'")

    def resolve_edge(self, edge_id: int, dst_id: Optional[int], resolution: str) -> None:
        self.conn.execute(
            "UPDATE edges SET dst_id = ?, resolution = ? WHERE id = ?", (dst_id, resolution, edge_id)
        )

    def unresolved_edges(self) -> list[tuple[int, int, str]]:
        return [
            (r["id"], r["src_id"], r["dst_name"])
            for r in self.conn.execute(
                "SELECT id, src_id, dst_name FROM edges WHERE dst_id IS NULL"
            )
        ]

    # ----------------------------------------------------------------- misc

    def counts(self) -> dict[str, int]:
        c = self.conn.execute
        return {
            "files": c("SELECT COUNT(*) FROM files").fetchone()[0],
            "symbols": c("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "edges": c("SELECT COUNT(*) FROM edges").fetchone()[0],
            "resolved_edges": c("SELECT COUNT(*) FROM edges WHERE dst_id IS NOT NULL").fetchone()[0],
        }

    def commit(self) -> None:
        self.conn.commit()
